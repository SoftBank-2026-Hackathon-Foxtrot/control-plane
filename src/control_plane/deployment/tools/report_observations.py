#!/usr/bin/env python3
"""Export bounded preparation evidence with the official OpenTelemetry SDK."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def timestamp(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None or at > datetime.now(UTC):
        raise ValueError("Use the original timezone-aware measurement time")
    return at.isoformat()


def plan_counts(plan):
    # terraform show -json PLANFILE. Never forward values, sensitive outputs or addresses.
    if not plan.get("format_version") or not isinstance(plan.get("resource_changes"), list):
        raise ValueError("Expected terraform show -json output with resource_changes")
    counts = dict.fromkeys(("create", "update", "delete", "replace"), 0)
    for resource in plan["resource_changes"]:
        actions = resource["change"]["actions"]
        if "create" in actions and "delete" in actions:
            counts["replace"] += 1
        elif actions in (["create"], ["update"], ["delete"]):
            counts[actions[0]] += 1
    return counts


def infra_report(workflow, job, plan=None, handoff=None):
    if job.get("migration_id") != workflow or job.get("type") not in ("PLAN", "APPLY"):
        raise ValueError("Job must belong to this workflow and be PLAN or APPLY")
    if job.get("status") not in (
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "UNKNOWN",
        "NEEDS_RECONCILIATION",
    ):
        raise ValueError("Unsupported job status")
    # The repository examples lack observed_at and cannot be submitted as completed work.
    observed_at = timestamp(job["observed_at"])
    data = {
        "workflowId": workflow,
        "stage": job["type"],
        "status": job["status"],
        "jobId": job["id"],
        "observedAt": observed_at,
        "changes": plan_counts(plan) if plan else None,
    }
    for source, target in (
        ("requested_at", "requestedAt"),
        ("started_at", "startedAt"),
        ("finished_at", "finishedAt"),
    ):
        data[target] = timestamp(job[source]) if job.get(source) else None
    if job["status"] == "SUCCEEDED" and not data["finishedAt"]:
        raise ValueError("Successful jobs require their real finished_at")
    if handoff:
        if (
            handoff.get("migration_id") != workflow
            or job["type"] != "APPLY"
            or job["status"] != "SUCCEEDED"
            or handoff.get("readiness") != "INFRA_APPLIED"
        ):
            raise ValueError("Handoff requires a successful APPLY for the same workflow")
        data.update(
            targetRef=handoff["target"]["id"],
            cloud=handoff["target"]["cloud"],
            vmId=handoff["outputs"]["compute"]["id"],
        )
    return {"resource": "INFRA", "data": data}


def provisioner_reports(workflow, environment, job):
    """Project real /v1/environments and /v1/jobs responses, without secrets or log tails."""
    if environment.get("workflowId") != workflow or job.get("environmentId") != environment.get(
        "environmentId"
    ):
        raise ValueError("Environment and job must belong to this workflow")
    readiness = environment.get("readiness") or {}
    infra, app = readiness.get("infrastructure", {}), readiness.get("application", {})
    handoff = environment.get("handoff") or {}
    base = {
        "workflowId": workflow,
        "targetRef": environment["environmentId"],
        "cloud": environment["cloud"],
        "vmId": handoff.get("compute", {}).get("id"),
    }
    counts = dict.fromkeys(("create", "update", "delete", "replace"), 0)
    plan = environment.get("plan")
    for action, value in (plan or {}).get("changes", {}).items():
        key = "replace" if "create" in action and "delete" in action else action
        if key in counts:
            counts[key] += value
    infra_at = (
        infra.get("observedAt") or job.get("finishedAt") or job.get("startedAt") or job["createdAt"]
    )
    infra_ready = infra.get("status") == "READY" and bool(infra.get("observedAt"))
    infra_status = (
        "SUCCEEDED" if infra_ready else ("UNKNOWN" if job["kind"] == "CHECK_APP" else job["status"])
    )
    # Keep adapter COMPUTE evidence intact. The existing preparation contract accepts a list
    # of scalar checks, so both readiness results fit without replacing adapter operations.
    checks = [
        f"INFRA={infra.get('status', 'UNKNOWN')}; observedAt={infra.get('observedAt')}",
        f"APP={app.get('status', 'UNKNOWN')}; HTTP={app.get('httpStatus')}; "
        f"observedAt={app.get('observedAt')}; scope=APP_HTTP_READINESS",
    ]
    return [
        {
            "resource": "INFRA",
            "data": {
                **base,
                "stage": "APPLY" if infra_ready else job["kind"],
                "status": infra_status,
                "jobId": infra.get("jobId") or job["jobId"],
                "observedAt": timestamp(infra_at),
                "finishedAt": timestamp(infra["observedAt"]) if infra.get("observedAt") else None,
                "changes": counts if plan else None,
                "check": checks,
            },
        }
    ]


def compute_report(workflow, migration):
    if migration.get("workflowId") != workflow or migration.get("kind") != "COMPUTE":
        raise ValueError("Compute migration belongs to a different workflow")
    details = migration.get("details", {}).get("compute", {})
    data = {
        "workflowId": workflow,
        "migrationId": migration["migrationId"],
        "stage": migration["lifecycle"],
        "status": migration["health"],
        "observedAt": timestamp(migration["observed"]["observedAt"]),
    }
    for field in ("targetRef", "vmId", "imageId", "configVersion", "containerId", "check"):
        data[field] = details.get(field)
    fields = (
        "operationId",
        "migrationId",
        "action",
        "status",
        "requestedAt",
        "startedAt",
        "finishedAt",
    )
    data["operations"] = [
        {key: op.get(key) for key in fields} for op in details.get("operations", [])[:20]
    ]
    return {"resource": "COMPUTE", "data": data}


def request(url, token, body=None):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) endpoint without embedded credentials")
    req = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def load(path):
    return json.loads(Path(path).read_text()) if path else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True)
    commands = parser.add_subparsers(dest="resource", required=True)
    infra = commands.add_parser("infra")
    infra.add_argument("--job-json", required=True)
    infra.add_argument("--plan-json")
    infra.add_argument("--handoff-json")
    provisioner = commands.add_parser("provisioner")
    provisioner.add_argument("--environment-json", required=True)
    provisioner.add_argument("--job-json", required=True)
    compute = commands.add_parser("compute")
    compute.add_argument("--adapter-url", required=True)
    compute.add_argument("--migration-id", required=True)
    compute.add_argument("--adapter-token-env", default="COMPUTE_TOKEN")
    compute.add_argument(
        "--watch", action="store_true", help="Poll every five seconds until interrupted"
    )
    args = parser.parse_args()
    if __package__:
        from .telemetry import get_telemetry
    else:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from control_plane.deployment.tools.telemetry import get_telemetry
    telemetry = get_telemetry("iac")
    if not telemetry.enabled:
        parser.error("Set OTEL_EXPORTER_OTLP_ENDPOINT to the Collector")
    while True:
        if args.resource == "provisioner":
            for report in provisioner_reports(
                args.workflow, load(args.environment_json), load(args.job_json)
            ):
                telemetry.snapshot(report["resource"], report["data"])
            print(
                f"Infrastructure and app observations queued to OTEL for {args.workflow}",
                flush=True,
            )
            return
        if args.resource == "infra":
            report = infra_report(
                args.workflow, load(args.job_json), load(args.plan_json), load(args.handoff_json)
            )
        else:
            migration = request(
                args.adapter_url.rstrip("/")
                + "/v1/migrations/"
                + quote(args.migration_id, safe=""),
                os.environ[args.adapter_token_env],
            )
            report = compute_report(args.workflow, migration)
        telemetry.snapshot(report["resource"], report["data"])
        print(f"{report['resource']} observation queued to OTEL for {args.workflow}", flush=True)
        if args.resource != "compute" or not args.watch:
            return
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Avoid echoing request headers, output files or provider error bodies.
        raise SystemExit(
            f"Observation report failed ({type(error).__name__}); verify inputs and connectivity"
        ) from None
