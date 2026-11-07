"""Change an explicitly matched target before any migration adapter starts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from control_plane.adapters.client import AdapterError, AdapterUnreachable
from control_plane.deployment.provisioner.service import Conflict
from control_plane.deployment.provisioner.spec import RequestError
from control_plane.deployment.tools.render_inputs import POLICY
from control_plane.domain.models import (
    Endpoint,
    EndpointPair,
    PreparedTargetConfiguration,
    ResourceKind,
    TargetConfiguration,
)
from control_plane.engine.context import Outcome, Passed, Stop, Wait
from control_plane.errors import ApiError
from control_plane.traffic import TrafficError

if TYPE_CHECKING:
    from control_plane.api.dependencies import ApiContext
    from control_plane.engine.context import StageContext


def identity(env: dict[str, Any]) -> dict[str, Any]:
    handoff = env.get("handoff") or {}
    keys = {"id", "host", "port", "name", "bucket", "endpoint", "url", "provider", "region"}
    return {
        "appUrl": str((handoff.get("app") or {}).get("url") or "").rstrip("/"),
        **{
            kind: {key: value for key, value in (handoff.get(kind) or {}).items() if key in keys}
            for kind in ("database", "cache", "storage")
        },
    }


def matches(context: ApiContext, env: dict[str, Any], owner: str, target: str) -> bool:
    from control_plane.deployment.api import APP_TEMPLATE

    wanted = context.seed.environments[target].app_url.rstrip("/")
    other_urls = {
        value.app_url.rstrip("/")
        for name, value in context.seed.environments.items()
        if name != target
    }
    return bool(
        env["org_id"] == owner
        and env["template"] == APP_TEMPLATE
        and env["cloud"] == target
        and identity(env)["appUrl"] == wanted
        and wanted not in other_urls
        and env["status"] != "DESTROYED"
    )


def migration_endpoint(seed: Any, kind: ResourceKind, side: str = "target") -> dict[str, Any]:
    """Read public connection metadata only; never resolve or parse credential DSNs."""
    if kind not in seed.managed:
        return {}
    resource = seed.resources.of(kind)
    endpoint = resource.target if side == "target" else resource.source
    if endpoint is not None:
        candidate = endpoint.wire(kind)
    elif kind is ResourceKind.DATABASE and resource.catalog is not None:
        reference = resource.target_ref if side == "target" else resource.source_ref
        candidate = resource.catalog.databases.get(reference, {})
    else:
        candidate = {}
    return {
        key: candidate[key]
        for key in ("host", "port", "database", "dbname", "name")
        if key in candidate
    }


def same_address(kind: ResourceKind, target: dict[str, Any], actual: dict[str, Any]) -> bool:
    default_port = 5432 if kind is ResourceKind.DATABASE else 6379
    host = target.get("host")
    actual_host = actual.get("host")
    if not isinstance(host, str) or not isinstance(actual_host, str) or not host or not actual_host:
        return False
    port, actual_port = target.get("port", default_port), actual.get("port")
    if type(port) is not int or type(actual_port) is not int:
        return False
    return host.lower().rstrip(".") == actual_host.lower().rstrip(".") and port == actual_port


def same_resource(kind: ResourceKind, target: dict[str, Any], actual: dict[str, Any]) -> bool:
    if not same_address(kind, target, actual):
        return False
    if kind is ResourceKind.DATABASE:
        name = target.get("database") or target.get("dbname") or target.get("name")
        actual_name = actual.get("name") or actual.get("database")
        return isinstance(name, str) and bool(name) and name == actual_name
    # Redis capacity belongs to the endpoint instance, across its logical DB indexes.
    return True


def editable_parameters(seed: Any, env: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    handoff = env.get("handoff") or {}
    database = same_resource(
        ResourceKind.DATABASE,
        migration_endpoint(seed, ResourceKind.DATABASE),
        handoff.get("database") or {},
    ) and not same_address(
        ResourceKind.DATABASE,
        migration_endpoint(seed, ResourceKind.DATABASE, "source"),
        handoff.get("database") or {},
    )
    cache = same_resource(
        ResourceKind.REDIS,
        migration_endpoint(seed, ResourceKind.REDIS),
        handoff.get("cache") or {},
    ) and not same_address(
        ResourceKind.REDIS,
        migration_endpoint(seed, ResourceKind.REDIS, "source"),
        handoff.get("cache") or {},
    )
    return {
        key: value
        for key, value in rules.items()
        if (key.startswith("db_") and database) or (key.startswith("cache_") and cache)
    }


async def options(context: ApiContext, engine: Any, user: str, target: str) -> dict[str, Any]:
    from control_plane.deployment.api import org_id

    if target not in context.seed.environments:
        raise ApiError(422, "UNKNOWN_ENVIRONMENT", "target environment is not registered")
    rules = POLICY["clouds"].get(target, {})
    result: dict[str, Any] = {
        "applicationId": context.seed.application,
        "targetEnv": target,
        "cloud": target,
        "supported": False,
        "blockedReason": None,
        "deployment": None,
        "parameters": {},
        "appOptions": {
            "cpu": [0.25, 0.5, 1, 2, 4] if target == "aws" else [1, 2, 4],
            "memoryStepMiB": 512,
        },
    }
    if target not in {"aws", "gcp"}:
        result["blockedReason"] = "온프렘 사양 변경은 현재 지원하지 않습니다."
        return result
    if engine is None:
        result["blockedReason"] = "대상 사양을 변경할 배포 실행기가 설정되지 않았습니다."
        return result
    owner = org_id(user, context.seed.application)
    records = await asyncio.to_thread(engine.store.environments)
    found = [env for env in records if matches(context, env, owner, target)]
    if len(found) != 1:
        result["blockedReason"] = "마이그레이션 대상 주소와 일치하는 본인 배포가 하나여야 합니다."
        return result
    env = found[0]
    result["parameters"] = editable_parameters(context.seed, env, rules)
    current = env["tfvars"].get("app", {})
    result["deployment"] = {
        "environmentId": env["id"],
        "status": env["status"],
        "updatedAt": env["updated_at"],
        "sizing": env["tfvars"]["sizing"],
        "app": {
            key: current.get(internal, default)
            for key, internal, default in (
                ("cpu", "cpu", 1),
                ("memoryMiB", "memory_mib", 2048),
                ("workerCount", "worker_count", 2),
                ("apiWorkers", "api_workers", 4),
            )
        },
    }
    if await context.repo.active_workflow_id():
        result["blockedReason"] = "마이그레이션 계획이 진행 중이라 사양을 변경할 수 없습니다."
    elif env["status"] != "READY" or await asyncio.to_thread(engine.store.busy, env["id"]):
        result["blockedReason"] = "대상 배포가 준비 상태가 될 때까지 기다려 주세요."
    elif not current.get("entrypoint"):
        result["blockedReason"] = "이전 방식으로 배포한 앱은 사양 변경을 지원하지 않습니다."
    else:
        result["supported"] = True
        candidates = []
        for cpu in result["appOptions"]["cpu"]:
            memory = 512 if target == "aws" and cpu == 0.25 else 1024
            while memory <= 32768:
                try:
                    await asyncio.to_thread(
                        engine.validate_update,
                        env["id"],
                        {"app": {"cpu": cpu, "memoryMiB": memory}},
                    )
                except (RequestError, ValueError, Conflict):
                    memory += 512
                    continue
                candidates.append(cpu)
                break
        result["appOptions"]["cpu"] = candidates
    return result


async def validate(
    context: ApiContext,
    engine: Any,
    user: str,
    target: str,
    configuration: TargetConfiguration,
    resources: dict[ResourceKind, EndpointPair] | None = None,
) -> PreparedTargetConfiguration:
    from control_plane.deployment.api import org_id

    available = await options(context, engine, user, target)
    selected = available["deployment"]
    if (
        not available["supported"]
        or not selected
        or selected["environmentId"] != configuration.environment_id
    ):
        raise ApiError(
            409,
            "TARGET_SIZING_UNAVAILABLE",
            str(available["blockedReason"] or "target deployment differs"),
        )
    if selected["updatedAt"] != configuration.expected_updated_at:
        raise ApiError(
            409, "TARGET_CONFIGURATION_CHANGED", "대상 사양이 변경됐습니다. 다시 조회하세요."
        )
    env = await asyncio.to_thread(engine.store.environment, configuration.environment_id)
    for kind, endpoints in (resources or {}).items():
        if kind not in {ResourceKind.DATABASE, ResourceKind.REDIS}:
            continue
        actual = (env.get("handoff") or {}).get(
            "database" if kind is ResourceKind.DATABASE else "cache"
        ) or {}
        if not same_resource(kind, endpoints.target.wire(kind), actual):
            raise ApiError(
                422,
                "MIGRATION_TARGET_MISMATCH",
                "마이그레이션 대상과 사양을 변경할 리소스 주소가 다릅니다.",
            )
    if set(configuration.sizing) - available["parameters"].keys():
        raise ApiError(422, "INVALID_TARGET_SIZING", "변경할 수 없는 사양 항목입니다.")
    for key in ("db_storage_gib", "db_max_storage_gib"):
        value = configuration.sizing.get(key, selected["sizing"].get(key))
        if not isinstance(value, (int, float)) or value < selected["sizing"].get(key, 0):
            raise ApiError(422, "INVALID_TARGET_SIZING", "DB 저장 공간은 줄일 수 없습니다.")
    app = configuration.app.model_dump(by_alias=True, exclude_none=True)
    if any(
        key in app and app[key] != selected["app"][key] for key in ("workerCount", "apiWorkers")
    ):
        raise ApiError(
            422, "INVALID_TARGET_SIZING", "마이그레이션의 업무 참여자 개수는 변경할 수 없습니다."
        )
    try:
        await asyncio.to_thread(
            engine.validate_update,
            configuration.environment_id,
            {"app": app, "sizing": configuration.sizing},
        )
    except (RequestError, ValueError) as error:
        raise ApiError(422, "INVALID_TARGET_SIZING", str(error)) from error
    except Conflict as error:
        raise ApiError(409, error.code, str(error)) from error
    return PreparedTargetConfiguration(
        **configuration.model_dump(),
        owner_id=org_id(user, context.seed.application),
        identity=identity(env),
    )


async def prepare_target(ctx: StageContext) -> Outcome:
    config = ctx.workflow.target_configuration
    assert config is not None
    engine = ctx.services.deployment
    if engine is None:
        return Stop("DEPLOYMENT_NOT_CONFIGURED", "target sizing requires the deployment service")
    data = ctx.stage.data
    if data.get("target_sizing_ready"):
        return Passed()
    env = await asyncio.to_thread(engine.store.environment, config.environment_id)
    if env is None or env["org_id"] != config.owner_id or env["cloud"] != ctx.workflow.target_env:
        return Stop(
            "TARGET_CONFIGURATION_CHANGED", "the target deployment no longer matches the plan"
        )
    key = f"migration-sizing:{ctx.workflow.id}"
    remembered = await asyncio.to_thread(engine.store.remembered, key)
    job_id = data.get("target_sizing_job_id") or (remembered[2]["jobId"] if remembered else None)
    if job_id is None:
        if (
            env["status"] != "READY"
            or env["updated_at"] != config.expected_updated_at
            or identity(env) != config.identity
        ):
            return Stop(
                "TARGET_CONFIGURATION_CHANGED",
                "대상 배포가 계획 이후 변경됐습니다. 새 계획을 만드세요.",
            )
        allowed = editable_parameters(
            ctx.seed, env, POLICY["clouds"].get(ctx.workflow.target_env, {})
        )
        if set(config.sizing) - allowed.keys():
            return Stop(
                "TARGET_RESOURCE_MISMATCH",
                "사양을 변경할 DB·Redis와 마이그레이션 대상의 연결을 확인하지 못했습니다.",
            )
        try:
            await ctx.services.traffic.require_source_only(
                source_pool=ctx.source_env.traffic_pool, target_pool=ctx.target_env.traffic_pool
            )
        except TrafficError as error:
            return Stop("TARGET_TRAFFIC_ACTIVE", str(error))
        source = await _source_binding(ctx)
        if not isinstance(source, Passed):
            return source
        body = {
            "sizing": config.sizing,
            "app": config.app.model_dump(by_alias=True, exclude_none=True),
        }
        try:
            _, accepted = await asyncio.to_thread(engine.update, config.environment_id, body, key)
        except (Conflict, RequestError, ValueError) as error:
            return Stop("TARGET_SIZING_REJECTED", str(error))
        job_id = accepted["jobId"]
    data["target_sizing_job_id"] = job_id
    data["phase"] = "TARGET_SIZING"
    job = await asyncio.to_thread(engine.store.job, job_id)
    if (
        job is None
        or job["status"] in {"FAILED", "NEEDS_RECONCILIATION"}
        or env["status"] in {"FAILED", "NEEDS_RECONCILIATION", "DESTROYED"}
    ):
        return Stop(
            "TARGET_SIZING_FAILED", "대상 사양 적용을 확인하지 못해 마이그레이션을 중지했습니다."
        )
    if env["status"] != "READY" or await asyncio.to_thread(
        engine.store.busy, config.environment_id
    ):
        # CHECK_APP has its own job; a successful apply alone must not start CDC.
        if env["status"] in {"APP_NOT_READY", "INFRA_READY"} and not await asyncio.to_thread(
            engine.store.busy, config.environment_id
        ):
            return Stop("TARGET_APP_NOT_READY", "사양 적용 후 대상 앱 준비를 확인하지 못했습니다.")
        return Wait("대상 사양 적용과 앱 준비를 기다리는 중입니다.")
    if job["status"] != "SUCCEEDED" or identity(env) != config.identity:
        return Stop(
            "TARGET_ADDRESS_CHANGED", "대상 주소가 변경되어 기존 연결로 복제를 시작할 수 없습니다."
        )
    data["target_sizing_ready"] = True
    data["phase"] = ""
    await ctx.emit("TARGET_SIZING_APPLIED", str(job_id))
    return Passed()


async def _source_binding(ctx: StageContext) -> Outcome:
    """Reuse the existing proxy's observed source before touching the other database."""
    if ResourceKind.DATABASE not in ctx.seed.managed:
        return Passed()
    from control_plane.engine.stages import _earlier_binding

    resource = await ctx.resource(ResourceKind.DATABASE)
    database = ctx.seed.resources.database
    assert database is not None
    binding = await ctx.services.repo.application_binding(
        ctx.seed.application, ResourceKind.DATABASE
    )
    identifier = resource.binding_ref or (binding or {}).get("bindingId") or database.binding_ref
    if identifier is None:
        identifier = await _earlier_binding(ctx)
    if identifier is None:
        return Passed()  # The initial binding is checked by the existing registration stage.
    try:
        current = await ctx.client(ResourceKind.DATABASE).binding(str(identifier))
        expected = (resource.plan or {}).get("source")
        if expected:
            correct = Endpoint.model_validate(current.get("source")).identity(
                ResourceKind.DATABASE
            ) == Endpoint.model_validate(expected).identity(ResourceKind.DATABASE)
        else:
            correct = current.get("databaseRef") == database.source_ref
        if current.get("status") != "ACTIVE" or not correct:
            return Stop("SOURCE_BINDING_MISMATCH", "DB 프록시의 현재 원본이 계획과 다릅니다.")
    except (AdapterError, AdapterUnreachable, ValueError):
        return Stop("SOURCE_BINDING_UNCONFIRMED", "DB 프록시의 현재 원본을 확인하지 못했습니다.")
    return Passed()
