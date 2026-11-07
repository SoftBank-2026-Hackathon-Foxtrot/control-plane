"""Authenticated on-prem preparation and recovery, alongside the fixed-app deployment API."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from fastapi import APIRouter, Request, Response

from control_plane.api.dependencies import CheckedSession, Context, CurrentSession, checked_change
from control_plane.errors import ApiError

from .api import call, fixed_application, org_id, service

router = APIRouter()


def idempotency(request: Request, user: str, application: str) -> str:
    key = request.headers.get("Idempotency-Key")
    if not key or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise ApiError(422, "IDEMPOTENCY_KEY_REQUIRED", "use a stable Idempotency-Key for retries")
    return hashlib.sha256(f"{user}:{application}:{key}".encode()).hexdigest()


@router.get("/v1/applications/{application_id}/deployment-targets")
async def deployment_targets(
    application_id: str, request: Request, context: Context, session: CurrentSession
) -> dict[str, Any]:
    fixed_application(context, application_id)
    owner = org_id(str(session.user.id), application_id)
    nodes = service(request).config.get("clouds", {}).get("onprem", {}).get("nodes", {})
    return {
        "onprem": [
            {
                "nodeId": name,
                "name": node.get("name", name),
                "requiresPreparation": bool(node.get("preparation")),
            }
            for name, node in nodes.items()
            if owner in node.get("orgs", [])
        ],
        "orgId": owner,
    }


@router.post("/v1/applications/{application_id}/onprem-preparations", status_code=202)
async def prepare_onprem(
    application_id: str,
    body: dict[str, Any],
    request: Request,
    response: Response,
    context: Context,
    session: CheckedSession,
) -> dict[str, Any]:
    fixed_application(context, application_id)
    engine = service(request)
    if engine.onprem is None:
        raise ApiError(503, "ONPREM_NOT_CONFIGURED", "on-prem preparation is not configured")
    owner = org_id(str(session.user.id), application_id)
    nodes = engine.config.get("clouds", {}).get("onprem", {}).get("nodes", {})
    if set(body) - {"nodeId", "mode"} or not isinstance(body.get("nodeId"), str):
        raise ApiError(422, "INVALID_DEPLOYMENT", "provide nodeId and optional mode")
    node = nodes.get(body["nodeId"], {})
    if owner not in node.get("orgs", []) or not node.get("preparation"):
        raise ApiError(404, "NOT_FOUND", "preparation target not found")
    payload = {
        **node["preparation"],
        "org": owner,
        "driver": "ansible",
        "mode": body.get("mode", "plan"),
    }
    code, result = await call(
        engine.onprem.create, payload, idempotency(request, str(session.user.id), application_id)
    )
    engine.wake.set()
    response.status_code = code
    response.headers["Location"] = f"/v1/provisioning/onprem/jobs/{result['jobId']}"
    return dict(result)


@router.api_route("/v1/provisioning/onprem/{path:path}", methods=["GET", "POST"])
async def preparation_jobs(
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
    return await onprem_provisioning(
        path,
        request,
        response,
        service(request),
        org_id(str(session.user.id), context.seed.application),
    )


@router.post("/v1/provisioning/environments/{environment_id}/reconcile")
@router.post("/v1/provisioning/environments/{environment_id}/destroy")
async def application_recovery(
    environment_id: str,
    request: Request,
    response: Response,
    context: Context,
    session: CheckedSession,
) -> dict[str, Any]:
    engine = service(request)
    env = await call(engine.store.environment, environment_id)
    owner = org_id(str(session.user.id), context.seed.application)
    if env is None or env["org_id"] != owner:
        raise ApiError(404, "NOT_FOUND", "deployment not found")
    if env["cloud"] != "onprem":
        raise ApiError(422, "INVALID_DEPLOYMENT", "this endpoint handles on-prem apps only")
    action = request.url.path.rsplit("/", 1)[-1]
    if action == "destroy" and (workflow_id := env["request"].get("workflowId")):
        workflow = await context.repo.workflow(workflow_id)
        if workflow is None or not workflow.status.terminal:
            raise ApiError(409, "WORKFLOW_BOUND", "finish the migration before removing its app")
    code, result = await call(
        engine.destroy if action == "destroy" else engine.reconcile, environment_id
    )
    response.status_code = code
    return dict(result)


async def onprem_provisioning(
    path: str, request: Request, response: Response, engine: Any, owner: str
) -> Any:
    prep = engine.onprem
    if prep is None:
        raise ApiError(503, "ONPREM_NOT_CONFIGURED", "on-prem preparation is not configured")
    if path == "environments" and request.method == "GET":
        return {
            "environments": [env for env in await call(prep.environments) if env["org"] == owner]
        }
    match = re.fullmatch(
        r"environments/(env-[a-f0-9]{12})(?:/(plan|apply|check-app|reconcile))?", path
    )
    if match:
        env_id, action = match.groups()
        env = await call(prep.get, env_id)
        if env["org"] != owner:
            raise ApiError(404, "NOT_FOUND", "preparation not found")
        if request.method == "GET" and action is None:
            return env
        if request.method == "POST" and action:
            try:
                body = await request.json()
            except ValueError as error:
                raise ApiError(422, "INVALID_DEPLOYMENT", "expected a JSON object") from error
            code, result = await call(
                prep.command, env_id, action, body, request.headers.get("Idempotency-Key")
            )
            engine.wake.set()
            response.status_code = code
            return result
    match = re.fullmatch(r"jobs/(job-[a-f0-9]{12})", path)
    if match and request.method == "GET":
        job = await call(prep.job, match.group(1))
        env = await call(prep.get, job["environmentId"])
        if env["org"] != owner:
            raise ApiError(404, "NOT_FOUND", "preparation not found")
        return job
    raise ApiError(404, "NOT_FOUND", "unknown on-prem provisioning endpoint")
