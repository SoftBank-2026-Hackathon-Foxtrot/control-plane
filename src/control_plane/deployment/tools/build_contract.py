"""Generate a small contract for managed environments; no API/runner implementation."""

import argparse
import copy
import json

import yaml

from control_plane.deployment.paths import iac_root

ROOT = iac_root()


class ReadableDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def obj(properties, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def string(**kwargs):
    return {"type": "string", **kwargs}


def array(items):
    return {"type": "array", "items": items}


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


def make_artifacts():
    policy = json.loads((ROOT / "catalog/sizing-policy.json").read_text(encoding="utf-8"))
    defaults = {
        c: json.loads(
            (ROOT / f"environments/{c}/deployment.example.tfvars.json").read_text(encoding="utf-8")
        )
        for c in ("aws", "gcp")
    }
    identifier = string(pattern="^[a-zA-Z0-9_-]{3,100}$")
    schemas = {}
    for cloud, rules in policy["clouds"].items():
        schemas[f"{cloud.title()}Sizing"] = obj(copy.deepcopy(rules), required=[])
    schemas["Sizing"] = {
        "description": "Partial sizing only. Server validates the TARGET cloud and merged baseline. Omitted fields retain managed values.",
        # Common fields and {} match both clouds; oneOf would reject them.
        "anyOf": [ref("AwsSizing"), ref("GcpSizing")],
    }
    schemas["Environment"] = obj({"id": identifier, "cloud": string(enum=["aws", "gcp"])})
    schemas["Context"] = obj(
        {
            "migration_id": identifier,
            "source": ref("Environment"),
            "target": ref("Environment"),
            "target_sizing": ref("Sizing"),
            "sizing_schema": {
                "type": "object",
                "description": "Standalone JSON Schema for the target's editable fields, from the reviewed catalog.",
            },
        }
    )
    schemas["PlanRequest"] = obj({"overrides": ref("Sizing")})
    schemas["ResourceChange"] = obj(
        {
            "address": string(),
            "action": string(enum=["CREATE", "UPDATE", "DELETE", "REPLACE", "NO_OP"]),
            "summary": string(
                description="Redacted change and interruption implications; never raw provider data."
            ),
        }
    )
    schemas["Plan"] = obj(
        {
            "id": identifier,
            "migration_id": identifier,
            "target": ref("Environment"),
            "status": string(enum=["REVIEWABLE", "BLOCKED"]),
            "effective_sizing": ref("Sizing"),
            "changes": array(ref("ResourceChange")),
            "warnings": array(string()),
            "blockers": array(string()),
            "expires_at": string(format="date-time"),
        }
    )
    schemas["Handoff"] = obj(
        {
            "migration_id": identifier,
            "target": ref("Environment"),
            "readiness": {"const": "INFRA_APPLIED"},
            "outputs": {
                "type": "object",
                "additionalProperties": False,
                "description": "Only terraform output -json handoff (schema_version 1). Native fields: docs/platform-contract.md and root outputs.tf. Never full state, plan or credentials.",
                "required": [
                    "schema_version",
                    "cloud",
                    "deployment_id",
                    "compute",
                    "database",
                    "cache",
                    "storage",
                    "network",
                    "metadata",
                    "pending_checks",
                ],
                "properties": {
                    "schema_version": {"const": 1},
                    "cloud": string(enum=["aws", "gcp"]),
                    "deployment_id": string(),
                    **{
                        name: {"type": "object"}
                        for name in (
                            "compute",
                            "database",
                            "cache",
                            "storage",
                            "network",
                            "metadata",
                        )
                    },
                    "pending_checks": array(string()),
                },
            },
        }
    )
    schemas["Error"] = obj({"code": string(), "message": string()})
    schemas["Job"] = obj(
        {
            "id": identifier,
            "migration_id": identifier,
            "type": string(enum=["PLAN", "APPLY"]),
            "status": string(
                enum=["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "NEEDS_RECONCILIATION"]
            ),
            "message": string(),
            "result": {"oneOf": [ref("Plan"), ref("Handoff"), {"type": "null"}]},
            "error": {"oneOf": [ref("Error"), {"type": "null"}]},
        }
    )
    params = {
        name.title().replace("_", ""): {
            "name": name,
            "in": "path",
            "required": True,
            "schema": identifier,
        }
        for name in ("migration_id", "plan_id", "job_id")
    }
    params["IdempotencyKey"] = {
        "name": "Idempotency-Key",
        "in": "header",
        "required": True,
        "schema": string(minLength=8, maxLength=128),
        "description": "PLAN/APPLY retries with the same key and body return the same job; changed body is 409. A plan executes at most once even with a new key.",
    }

    def operation(name, summary, output, path_param, request=None, async_job=False):
        op = {
            "operationId": name,
            "summary": summary,
            "parameters": [{"$ref": "#/components/parameters/" + path_param}],
            "responses": {
                "202" if async_job else "200": {
                    "description": "Poll job for result." if async_job else "Success",
                    "content": {"application/json": {"schema": ref(output)}},
                },
                "default": {"$ref": "#/components/responses/Error"},
            },
        }
        if request:
            op["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": ref(request)}},
            }
        if async_job:
            op["parameters"].append({"$ref": "#/components/parameters/IdempotencyKey"})
        return op

    paths = {
        "/migrations/{migration_id}": {
            "get": operation(
                "getProvisioningContext",
                "관리 중인 원본·대상과 조정 가능한 사양 조회",
                "Context",
                "MigrationId",
            )
        },
        "/migrations/{migration_id}/plans": {
            "post": operation(
                "createPlan",
                "대상 baseline에 변경값을 병합해 plan 생성",
                "Job",
                "MigrationId",
                "PlanRequest",
                True,
            )
        },
        "/plans/{plan_id}/apply": {
            "post": operation(
                "applyPlan",
                "검토한 불변 saved plan 실행; 요청 본문 없음",
                "Job",
                "PlanId",
                async_job=True,
            )
        },
        "/jobs/{job_id}": {
            "get": operation("getJob", "진행 상태와 plan 또는 인프라 출력 조회", "Job", "JobId")
        },
    }
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "Foxtrot Managed Provisioning — DRAFT",
            "version": "0.3.0",
            "description": "Source and target code, baseline, state and credentials are already managed by the platform. Only target sizing can change. Specification only; no API or runner implemented. See docs/provisioning-interface.md.",
        },
        "servers": [{"url": "/api/v1/provisioning"}],
        "security": [{"platformBearer": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "platformBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Reuse platform auth; authorize migration/plan/job references and target execution on every request.",
                }
            },
            "parameters": params,
            "responses": {
                "Error": {
                    "description": "400 malformed; 401 unauthenticated; 403 forbidden; 404 unknown reference; 409 stale/blocked/busy/idempotency conflict; 422 invalid sizing; 429 limited; 500/503 unavailable.",
                    "content": {"application/json": {"schema": ref("Error")}},
                }
            },
            "schemas": schemas,
        },
    }
    examples, index = {}, []

    def example(name, schema, value):
        examples[f"contracts/examples/{name}.json"] = (
            json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        )
        index.append({"file": f"examples/{name}.json", "schema": schema})

    for cloud in ("aws", "gcp"):
        other = "gcp" if cloud == "aws" else "aws"
        target = {"id": f"env_{cloud}_target", "cloud": cloud}
        migration_id = f"mig_to_{cloud}"
        example(
            f"{cloud}-context-response",
            "Context",
            {
                "migration_id": migration_id,
                "source": {"id": f"env_{other}_source", "cloud": other},
                "target": target,
                "target_sizing": defaults[cloud]["sizing"],
                "sizing_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    **schemas[f"{cloud.title()}Sizing"],
                },
            },
        )
        key = "compute_instance_type" if cloud == "aws" else "compute_machine_type"
        example(
            f"{cloud}-plan-request",
            "PlanRequest",
            {"overrides": {key: "t3.medium" if cloud == "aws" else "e2-medium"}},
        )
        native = {
            "schema_version": 1,
            "cloud": cloud,
            "deployment_id": defaults[cloud]["deployment_id"],
            "metadata": {
                **defaults[cloud]["metadata"],
                "managed_by": "terraform",
                "template_version": "v0-1",
                "security_profile": "public-app-private-data-v1",
            },
            "compute": {
                "id": "example-vm-id",
                "public_ip": "192.0.2.10",
                "private_ip": "10.20.0.10" if cloud == "aws" else "10.30.0.10",
                "identity": "example-workload-identity",
            },
            "database": {
                "id": "example-db-id",
                "host": "example.private",
                "port": 5432,
                "name": "app",
                "tls_required": True,
            },
            "cache": {
                "id": "example-cache-id",
                "host": "example.redis.private",
                "port": 6379 if cloud == "aws" else 6378,
                "tls_required": True,
                "auth_source": "external-secret-injection"
                if cloud == "aws"
                else "memorystore-get-auth-string-via-authorized-bootstrap",
            },
            "storage": {"id": "example-storage-id", "name": "example-bucket"},
            "network": {"id": "example-network-id"},
            "pending_checks": [
                "application-deployment",
                "database-grants-and-schema",
                "replication-connectivity",
                "application-smoke-test",
            ],
        }
        if cloud == "aws":
            native["database"]["admin_secret_ref"] = (
                "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:example"
            )
            native["network"]["app_security_group_id"] = "sg-example"
        else:
            native["database"].update(
                connection_name="example-project:asia-northeast3:example-db",
                app_db_user="example@example-project.iam",
                auth_mode="cloud-sql-iam-connector",
            )
            native["cache"]["server_ca_certs"] = []
            native["network"]["subnetwork_id"] = "example-subnet-id"
            native["pending_checks"].append("cache-auth-delivery")
        example(
            f"{cloud}-handoff-response",
            "Handoff",
            {
                "migration_id": migration_id,
                "target": target,
                "readiness": "INFRA_APPLIED",
                "outputs": native,
            },
        )

    plan = {
        "id": "plan_001",
        "migration_id": "mig_to_gcp",
        "target": {"id": "env_gcp_target", "cloud": "gcp"},
        "status": "REVIEWABLE",
        "effective_sizing": {**defaults["gcp"]["sizing"], "compute_machine_type": "e2-medium"},
        "changes": [
            {
                "address": "module.compute.google_compute_instance.this",
                "action": "UPDATE",
                "summary": "앱 VM 사양 변경. 대상 VM 정지가 필요할 수 있어 트래픽 전환 전에 실행.",
            }
        ],
        "warnings": ["가상의 일부 변경 예시. 실제 응답은 모든 자원 변경을 포함해야 함."],
        "blockers": [],
        "expires_at": "2026-11-03T00:16:00Z",
    }
    example(
        "plan-job-response",
        "Job",
        {
            "id": "job_plan_001",
            "migration_id": "mig_to_gcp",
            "type": "PLAN",
            "status": "SUCCEEDED",
            "message": "대상 계획을 확인해 주세요.",
            "result": plan,
            "error": None,
        },
    )
    example(
        "apply-job-response",
        "Job",
        {
            "id": "job_apply_001",
            "migration_id": "mig_to_gcp",
            "type": "APPLY",
            "status": "RUNNING",
            "message": "검토한 계획을 실행 중입니다.",
            "result": None,
            "error": None,
        },
    )
    example(
        "stale-plan-error",
        "Error",
        {
            "code": "PLAN_STALE",
            "message": "대상 설정 또는 실행 조건이 변경되었습니다. 새 계획을 확인해 주세요.",
        },
    )
    return {
        "contracts/provisioning.openapi.yaml": yaml.dump(
            spec, Dumper=ReadableDumper, allow_unicode=True, sort_keys=False, width=120
        ),
        "contracts/examples/index.json": json.dumps(index, indent=2) + "\n",
        **examples,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifacts = make_artifacts()
    stale = []
    for path, content in artifacts.items():
        target = ROOT / path
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                stale.append(path)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        raise SystemExit(
            "Stale generated contracts; run python tools/build_contract.py:\n" + "\n".join(stale)
        )
    print(
        f"{'Checked' if args.check else 'Wrote'} {len(artifacts)} contract artifacts (specification only)."
    )


if __name__ == "__main__":
    main()
