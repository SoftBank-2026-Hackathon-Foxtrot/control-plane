"""Authenticated deployment jobs for the application's fixed server configuration."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from fastapi import APIRouter, Request, Response

from control_plane.api.dependencies import CheckedSession, Context, CurrentSession, checked_change
from control_plane.errors import ApiError

router = APIRouter()
APP_TEMPLATE = "chiikawa-groupbuy"
FIXED_APP_FIELDS = frozenset({"template", "image", "port", "healthPath"})


def fixed_application(context: Any, application: str) -> None:
    if application != context.seed.application:
        raise ApiError(404, "NOT_FOUND", "application not found")


def check_app_overrides(body: Any) -> None:
    if isinstance(body, dict) and isinstance(body.get("app"), dict):
        fields = FIXED_APP_FIELDS.intersection(body["app"])
        if fields:
            raise ApiError(
                422,
                "INVALID_DEPLOYMENT",
                f"app fields are configured on the server: {', '.join(sorted(fields))}",
            )


def org_id(user: str, application: str) -> str:
    return "app-" + hashlib.sha256(f"{user}:{application}".encode()).hexdigest()[:20]


def service(request: Request) -> Any:
    value = getattr(request.app.state, "deployment_service", None)
    if value is None:
        raise ApiError(503, "DEPLOYMENT_NOT_CONFIGURED", "platform deployment is not configured")
    return value


def onprem_actor_counts(engine: Any, context: Any, payload: dict[str, Any]) -> None:
    node_id = payload.get("target", {}).get("nodeId")
    nodes = engine.config["clouds"]["onprem"].get("nodes", {})
    node = nodes.get(node_id) if isinstance(node_id, str) else None
    if not node or payload["org"]["id"] not in node.get("orgs", []):
        raise ApiError(404, "NOT_FOUND", "deployment target not found")
    target = context.seed.environments.get("onprem")
    actors = target.actors if target else {}
    api = node.get("apiEnvironment", {}).get("WORKLOAD_ACTOR")
    worker = node.get("workerEnvironment", {}).get("WORKLOAD_ACTOR")
    if not api or not worker or api == worker or api not in actors or worker not in actors:
        raise ApiError(
            422,
            "ONPREM_ACTORS_REQUIRED",
            "register distinct API and worker actors in the on-prem environment",
        )
    for field, actor in (("apiWorkers", api), ("workerCount", worker)):
        count = actors[actor]
        if not 1 <= count <= 8 or payload["app"].setdefault(field, count) != count:
            raise ApiError(
                422,
                "ONPREM_ACTOR_COUNT_MISMATCH",
                f"{field} must match {actor} in the on-prem seed",
            )


async def call(function: Any, *args: Any) -> Any:
    from .provisioner.service import Conflict, NotFound
    from .provisioner.spec import RequestError

    try:
        return await asyncio.to_thread(function, *args)
    except RequestError as error:
        raise ApiError(422, "INVALID_DEPLOYMENT", str(error)) from error
    except Conflict as error:
        raise ApiError(409, error.code, str(error)) from error
    except NotFound as error:
        raise ApiError(404, "NOT_FOUND", "deployment not found") from error


@router.post("/v1/applications/{application_id}/deployments", status_code=202)
async def deploy(
    application_id: str,
    body: dict[str, Any],
    request: Request,
    response: Response,
    context: Context,
    session: CheckedSession,
) -> dict[str, Any]:
    fixed_application(context, application_id)
    if set(body) - {"cloud", "target", "sizing", "app", "access", "mode", "workflowId"}:
        raise ApiError(422, "INVALID_DEPLOYMENT", "unknown deployment field")
    for field in ("app", "target", "sizing", "access"):
        if field in body and not isinstance(body[field], dict):
            raise ApiError(422, "INVALID_DEPLOYMENT", f"{field} must be an object")
    check_app_overrides(body)
    workflow_id = body.get("workflowId")
    if workflow_id is not None:
        if not isinstance(workflow_id, str):
            raise ApiError(422, "INVALID_DEPLOYMENT", "workflowId must be a string")
        workflow = await context.repo.workflow(workflow_id)
        if workflow is None or workflow.application != application_id:
            raise ApiError(404, "NOT_FOUND", "workflow not found")
        owner = workflow.created_by
        if body.get("cloud") == "onprem":
            if workflow.target_env != "onprem":
                raise ApiError(404, "NOT_FOUND", "workflow not found")
            owner = workflow.created_by_id or workflow.created_by
        if owner != session.user.id:
            raise ApiError(404, "NOT_FOUND", "workflow not found")
    payload = dict(body)
    payload["org"] = {"id": org_id(str(session.user.id), application_id), "name": application_id}
    payload["app"] = {"template": APP_TEMPLATE, **body.get("app", {})}
    engine = service(request)
    if body.get("cloud") == "onprem":
        image = engine.config["clouds"].get("onprem", {}).get("images", {}).get(APP_TEMPLATE)
        if not image:
            raise ApiError(
                503, "DEPLOYMENT_NOT_CONFIGURED", "no on-prem application image configured"
            )
        payload["app"]["image"] = image
        if workflow_id is not None:
            onprem_actor_counts(engine, context, payload)
    key = request.headers.get("Idempotency-Key")
    if not key or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise ApiError(
            422, "IDEMPOTENCY_KEY_REQUIRED", "use a stable Idempotency-Key for deployment retries"
        )
    scoped_key = hashlib.sha256(f"{session.user.id}:{application_id}:{key}".encode()).hexdigest()
    code, result = await call(engine.create, payload, scoped_key)
    response.status_code = code
    response.headers["Location"] = f"/v1/provisioning/jobs/{result['jobId']}"
    return dict(result)


@router.get("/v1/applications/{application_id}/deployments")
async def deployments(
    application_id: str, request: Request, context: Context, session: CurrentSession
) -> dict[str, Any]:
    fixed_application(context, application_id)
    owner = org_id(str(session.user.id), application_id)
    engine = service(request)
    items = await call(engine.store.environments)
    return {"environments": [engine.view(item) for item in items if item["org_id"] == owner]}


@router.api_route("/v1/provisioning/{path:path}", methods=["GET", "POST"])
async def provisioning(
    path: str, request: Request, response: Response, context: Context, session: CurrentSession
) -> Any:
    if request.method != "GET":
        await checked_change(
            request,
            session,
            context,
            request.headers.get("X-CSRF-Token"),
            request.headers.get("Origin"),
        )
    engine = service(request)
    if path == "templates" and request.method == "GET":
        return {
            "templates": {APP_TEMPLATE: engine.catalog["templates"][APP_TEMPLATE]},
            "clouds": sorted(engine.config["clouds"]),
        }
    owner = org_id(str(session.user.id), context.seed.application)
    if path == "environments" and request.method == "GET":
        return {
            "environments": [
                engine.view(item)
                for item in await call(engine.store.environments)
                if item["org_id"] == owner
            ]
        }
    match = re.fullmatch(r"environments/(env-[a-f0-9]{12})(?:/(apply|update|check-app))?", path)
    if match:
        env_id, action = match.groups()
        env = await call(engine.store.environment, env_id)
        if env is None or env["org_id"] != owner:
            raise ApiError(404, "NOT_FOUND", "deployment not found")
        if request.method == "GET" and action is None:
            return engine.view(env)
        if request.method == "POST" and action in {"apply", "update", "check-app"}:
            args = [env_id]
            if action == "update":
                try:
                    args.append(await request.json())
                except ValueError as error:
                    raise ApiError(422, "INVALID_DEPLOYMENT", "expected a JSON object") from error
                check_app_overrides(args[-1])
            fn = {"apply": engine.apply, "update": engine.update, "check-app": engine.check_app}[
                action
            ]
            code, result = await call(fn, *args)
            response.status_code = code
            return result
    job = re.fullmatch(r"jobs/(job-[a-f0-9]{12})", path)
    if job and request.method == "GET":
        saved = await call(engine.store.job, job.group(1))
        env = await call(engine.store.environment, saved["environment_id"]) if saved else None
        if env is None or env["org_id"] != owner:
            raise ApiError(404, "NOT_FOUND", "job not found")
        result = await call(engine.job, job.group(1))
        result.pop("logTail", None)
        return result
    raise ApiError(404, "NOT_FOUND", "unknown provisioning endpoint")
