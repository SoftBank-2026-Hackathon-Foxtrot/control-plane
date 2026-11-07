"""Authenticated application registration and durable deployment jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import Field

from control_plane.api.dependencies import CheckedSession, Context, CurrentSession, checked_change
from control_plane.domain.models import Model
from control_plane.errors import ApiError
from control_plane.store.table import VersionConflict

router = APIRouter()


class RegisterApplication(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,23}$")
    name: str = Field(min_length=1, max_length=100)
    image: str = Field(min_length=1, max_length=512)
    template: str = "chiikawa-groupbuy"
    port: int = Field(default=8000, ge=1, le=65535)


def org_id(user: str, application: str) -> str:
    return "app-" + hashlib.sha256(f"{user}:{application}".encode()).hexdigest()[:20]


async def registered(context: Any, user: str) -> list[dict[str, Any]]:
    page = await context.repo.table.query(f"USER#{user}", "APPLICATION#", limit=100)
    return [json.loads(item.body) for item in page.items]


async def owned(context: Any, user: str, application: str) -> dict[str, Any]:
    item = await context.repo.table.get(f"USER#{user}", f"APPLICATION#{application}")
    if item is None:
        raise ApiError(404, "NOT_FOUND", "application not found")
    return dict(json.loads(item.body))


def service(request: Request) -> Any:
    value = getattr(request.app.state, "deployment_service", None)
    if value is None:
        raise ApiError(503, "DEPLOYMENT_NOT_CONFIGURED", "platform deployment is not configured")
    return value


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


@router.post("/v1/applications", status_code=201)
async def register(
    body: RegisterApplication, context: Context, session: CheckedSession
) -> dict[str, Any]:
    value = body.model_dump(by_alias=True)
    value["ownerUserId"] = session.user.id
    value["createdAt"] = context.clock.now().isoformat()
    try:
        await context.repo.table.put(
            f"USER#{session.user.id}",
            f"APPLICATION#{body.id}",
            json.dumps(value),
            expected_version=0,
        )
    except VersionConflict as error:
        raise ApiError(409, "APPLICATION_EXISTS", "application is already registered") from error
    return value


@router.post("/v1/applications/{application_id}/deployments", status_code=202)
async def deploy(
    application_id: str,
    body: dict[str, Any],
    request: Request,
    response: Response,
    context: Context,
    session: CheckedSession,
) -> dict[str, Any]:
    app = await owned(context, str(session.user.id), application_id)
    if set(body) - {"cloud", "target", "sizing", "app", "access", "mode", "workflowId"}:
        raise ApiError(422, "INVALID_DEPLOYMENT", "unknown deployment field")
    for field in ("app", "target", "sizing", "access"):
        if field in body and not isinstance(body[field], dict):
            raise ApiError(422, "INVALID_DEPLOYMENT", f"{field} must be an object")
    workflow_id = body.get("workflowId")
    if workflow_id is not None:
        if not isinstance(workflow_id, str):
            raise ApiError(422, "INVALID_DEPLOYMENT", "workflowId must be a string")
        workflow = await context.repo.workflow(workflow_id)
        if (
            workflow is None
            or workflow.created_by != str(session.user.id)
            or workflow.application != application_id
        ):
            raise ApiError(404, "NOT_FOUND", "workflow not found")
    payload = dict(body)
    payload["org"] = {"id": org_id(str(session.user.id), application_id), "name": app["name"]}
    payload["app"] = {
        "template": app["template"],
        "image": app["image"],
        "port": app["port"],
        **body.get("app", {}),
    }
    key = request.headers.get("Idempotency-Key")
    if not key or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise ApiError(
            422, "IDEMPOTENCY_KEY_REQUIRED", "use a stable Idempotency-Key for deployment retries"
        )
    scoped_key = hashlib.sha256(f"{session.user.id}:{application_id}:{key}".encode()).hexdigest()
    code, result = await call(service(request).create, payload, scoped_key)
    response.status_code = code
    response.headers["Location"] = f"/v1/provisioning/jobs/{result['jobId']}"
    return dict(result)


@router.get("/v1/applications/{application_id}/deployments")
async def deployments(
    application_id: str, request: Request, context: Context, session: CurrentSession
) -> dict[str, Any]:
    await owned(context, str(session.user.id), application_id)
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
        return {"templates": engine.catalog["templates"], "clouds": sorted(engine.config["clouds"])}
    applications = await registered(context, str(session.user.id))
    owners = {org_id(str(session.user.id), app["id"]) for app in applications}
    if path == "environments" and request.method == "GET":
        return {
            "environments": [
                engine.view(item)
                for item in await call(engine.store.environments)
                if item["org_id"] in owners
            ]
        }
    match = re.fullmatch(r"environments/(env-[a-f0-9]{12})(?:/(apply|update|check-app))?", path)
    if match:
        env_id, action = match.groups()
        env = await call(engine.store.environment, env_id)
        if env is None or env["org_id"] not in owners:
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
        if env is None or env["org_id"] not in owners:
            raise ApiError(404, "NOT_FOUND", "job not found")
        result = await call(engine.job, job.group(1))
        result.pop("logTail", None)
        return result
    raise ApiError(404, "NOT_FOUND", "unknown provisioning endpoint")
