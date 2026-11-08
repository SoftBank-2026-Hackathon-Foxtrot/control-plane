"""The HTTP surface. Validates, delegates and shapes the answer; decisions live elsewhere.

The browser calls `/api/...`; the gateway, or Vite in development, removes the prefix.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse

from control_plane.api.dependencies import (
    ApiContext,
    CheckedSession,
    Context,
    CurrentSession,
    observation_caller,
    workload_caller,
)
from control_plane.api.idempotency import once
from control_plane.api.resource_observations import enrich_observed, read_resources
from control_plane.api.schemas import (
    ActorView,
    ApplicationView,
    CreateWorkflow,
    DirectiveView,
    EnvironmentView,
    EventPage,
    Heartbeat,
    HoldView,
    InitializeBindings,
    Me,
    PreparationReport,
    ResourceView,
    StageView,
    WorkflowCommand,
    WorkflowDetail,
    Workspace,
    allowed_commands,
)
from control_plane.domain.models import ResourceKind, ResourceState, Workflow
from control_plane.errors import ApiError
from control_plane.observation_projection import freshness, present, timestamp
from control_plane.observations import ObservationStore, observation_state
from control_plane.onprem_plans import ONPREM_PREFIX
from control_plane.workflows import CommandInput

router = APIRouter()
LOGIN_COOKIE_SECONDS = 300


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- login ------------------------------------------------------------------------------------
@router.get("/auth/github/start")
async def github_start(
    context: Context, return_to: Annotated[str | None, Query(alias="returnTo")] = None
) -> Response:
    login = await context.auth.start_login(return_to)
    response = RedirectResponse(login.redirect_url, status_code=302)
    response.set_cookie(
        context.login_cookie,
        login.browser_binding,
        max_age=LOGIN_COOKIE_SECONDS,
        httponly=True,
        secure=context.settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/github/callback")
async def github_callback(
    request: Request, context: Context, code: str | None = None, state: str | None = None
) -> Response:
    if not code or not state:
        # GitHub sends the person back without a code when they decline.
        return RedirectResponse(
            f"{context.settings.public_url}/login?error=login_cancelled", status_code=303
        )
    issued, return_to = await context.auth.finish_login(
        code=code, state=state, browser_binding=request.cookies.get(context.login_cookie)
    )
    previous = request.cookies.get(context.session_cookie)
    if previous:
        await context.auth.logout(previous)
    response = RedirectResponse(f"{context.settings.public_url}{return_to}", status_code=303)
    _expire(response, context, context.login_cookie)
    response.set_cookie(
        context.session_cookie,
        issued.token,
        max_age=context.settings.session_hours * 3600,
        httponly=True,
        secure=context.settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, context: Context, _: CheckedSession) -> Response:
    token = request.cookies.get(context.session_cookie)
    if token:
        await context.auth.logout(token)
    response = Response(status_code=204)
    _expire(response, context, context.session_cookie)
    return response


@router.get("/v1/me", response_model_by_alias=True)
async def me(session: CurrentSession, response: Response) -> Me:
    response.headers["Cache-Control"] = "no-store"
    return Me(user=session.user, expires_at=session.expires_at, csrf_token=session.csrf_token)


@router.get("/v1/workspaces", response_model_by_alias=True)
async def workspaces(session: CurrentSession) -> list[Workspace]:
    return [Workspace(id=f"personal:{session.user.id}", name=session.user.login, role="owner")]


# ---- what is managed -------------------------------------------------------------------------
@router.get("/v1/applications", response_model_by_alias=True)
async def applications(_: CurrentSession, context: Context) -> list[ApplicationView]:
    seed = context.seed
    environments = [
        EnvironmentView(name=name, traffic_pool=env.traffic_pool, actors=env.actors)
        for name, env in seed.environments.items()
    ]
    if context.settings.onprem_planning and "onprem" not in seed.environments:
        environments.append(EnvironmentView(name="onprem", traffic_pool="onprem", actors={}))
    return [
        ApplicationView(
            id=seed.application,
            scope=seed.scope,
            environments=environments,
            resources=[kind for kind in ResourceKind if kind in seed.managed],
        )
    ]


# ---- workflows --------------------------------------------------------------------------------
@router.post("/v1/applications/{application_id}/bindings")
async def initialize_bindings(
    application_id: str, body: InitializeBindings, _: CheckedSession, context: Context
) -> dict[ResourceKind, dict[str, Any]]:
    if application_id != context.seed.application:
        raise ApiError(404, "NOT_FOUND", f"no application {application_id}")
    return await context.workflows.bindings.initialize(body.sources)


@router.get("/v1/applications/{application_id}/bindings")
async def application_bindings(
    application_id: str, _: CurrentSession, context: Context
) -> dict[ResourceKind, dict[str, Any]]:
    if application_id != context.seed.application:
        raise ApiError(404, "NOT_FOUND", f"no application {application_id}")
    return await context.workflows.bindings.list()


@router.get("/v1/workflows", response_model_by_alias=True)
async def list_workflows(session: CurrentSession, context: Context) -> list[Workflow]:
    found = [await context.repo.workflow(i) for i in await context.repo.workflow_ids()]
    if context.settings.onprem_planning:
        found.extend(await context.onprem_plans.list(session.user.id))
    return sorted(
        (workflow for workflow in found if workflow),
        key=lambda workflow: workflow.created_at,
        reverse=True,
    )


@router.post("/v1/workflows", status_code=201)
async def create_workflow(
    body: CreateWorkflow,
    request: Request,
    session: CheckedSession,
    context: Context,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Response:
    onprem = context.settings.onprem_planning and "onprem" in (body.source_env, body.target_env)

    async def execute() -> WorkflowDetail:
        if onprem:
            plans = context.onprem_plans
            return await plans.describe(await plans.create(body, session.user))
        # The provisioner has one process/store. This admission lock serializes its public
        # updates with claiming the workflow's durable active slot.
        async with context.mutation_lock:
            engine = getattr(request.app.state, "deployment_service", None)
            prepared = None
            if body.target_configuration is not None:
                from control_plane.deployment.migration_sizing import validate

                if body.source_env == body.target_env:
                    raise ApiError(422, "SAME_ENVIRONMENT", "source and target are the same")
                prepared = await validate(
                    context,
                    engine,
                    str(session.user.id),
                    body.target_env,
                    body.target_configuration,
                    body.resources,
                )
            if engine is not None:
                for env in await asyncio.to_thread(engine.store.environments):
                    if await asyncio.to_thread(engine.store.busy, env["id"]):
                        raise ApiError(
                            409, "DEPLOYMENT_BUSY", "배포 작업이 끝난 뒤 마이그레이션을 시작하세요."
                        )
            created = await context.workflows.create(
                source_env=body.source_env,
                target_env=body.target_env,
                actor=session.user.login,
                actor_id=session.user.id if body.target_env == "onprem" else None,
                resources=body.resources,
                target_configuration=prepared,
            )
        return await _detail(context, created)

    return await once(
        context.repo,
        user_id=session.user.id,
        path="/v1/onprem/workflows" if onprem else "/v1/workflows",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=201,
        execute=execute,
    )


@router.get("/v1/workflows/{workflow_id}", response_model_by_alias=True)
async def get_workflow(
    workflow_id: str, session: CurrentSession, context: Context
) -> WorkflowDetail:
    if workflow_id.startswith(ONPREM_PREFIX):
        plans = context.onprem_plans
        return await plans.describe(await plans.get(workflow_id, session.user.id))
    workflow = await _workflow(context, workflow_id)
    detail = await _detail(context, workflow)
    # Newer release branches already collect and project observations independently (including
    # OTEL). Preserve that pipeline rather than overwriting it with direct polling.
    if "observation" in ResourceView.model_fields:
        return detail.model_copy(
            update={
                "resources": [
                    enrich_observed(resource, workflow, context.clock.now())
                    for resource in detail.resources
                ]
            }
        )
    return detail.model_copy(update={"resources": await read_resources(context, workflow)})


@router.get("/v1/workflows/{workflow_id}/events", response_model_by_alias=True)
async def workflow_events(
    workflow_id: str,
    session: CurrentSession,
    context: Context,
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> EventPage:
    if workflow_id.startswith(ONPREM_PREFIX):
        return (await context.onprem_plans.get(workflow_id, session.user.id)).page(after, limit)
    await _workflow(context, workflow_id)
    events, next_cursor = await context.repo.events(workflow_id, after=after, limit=limit)
    return EventPage(events=events, next_cursor=next_cursor)


@router.post("/v1/workflows/{workflow_id}/commands")
async def workflow_command(
    workflow_id: str,
    body: WorkflowCommand,
    session: CheckedSession,
    context: Context,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Response:
    async def execute() -> WorkflowDetail:
        if workflow_id.startswith(ONPREM_PREFIX):
            plans = context.onprem_plans
            return await plans.describe(await plans.command(workflow_id, body, session.user))
        updated = await context.workflows.apply(
            workflow_id,
            CommandInput(
                command=body.command,
                expected_version=body.expected_version,
                resource=body.resource_id,
                mode=body.mode,
                confirm_target_failure=body.confirm_target_failure,
            ),
            actor=session.user.login,
        )
        return await _detail(context, updated)

    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/workflows/{workflow_id}/commands",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=200,
        execute=execute,
    )


# ---- called by the application's own processes -------------------------------------------------
@router.post(
    "/internal/v1/workloads/{scope}/heartbeat",
    dependencies=[Depends(workload_caller)],
    response_model_by_alias=True,
)
async def heartbeat(scope: str, body: Heartbeat, context: Context) -> DirectiveView:
    if scope != context.seed.scope:
        raise ApiError(404, "NOT_FOUND", f"no workload {scope}")
    if body.actor_id not in context.seed.all_actors():
        raise ApiError(422, "UNKNOWN_ACTOR", f"{body.actor_id} is not registered")
    directive = await context.coordinator.heartbeat(
        actor_id=body.actor_id,
        instance_id=body.instance_id,
        generation=body.generation,
        state=body.state,
        in_flight=body.in_flight,
        active_web_sockets=body.active_web_sockets,
        redis_route=body.redis_route,
        redis_route_generation=body.redis_route_generation,
        stale_connections=body.stale_connections,
    )
    return DirectiveView(
        desired=directive.desired,
        generation=directive.generation,
        object_migration_id=directive.object_migration_id,
        poll_after_ms=directive.poll_after_ms,
        redis_route=directive.redis_route,
        redis_endpoint=directive.redis_endpoint,
        redis_route_generation=directive.redis_route_generation,
    )


# ---- helpers ----------------------------------------------------------------------------------
def _expire(response: Response, context: ApiContext, name: str) -> None:
    """Browsers ignore an attempt to clear a __Host- cookie without these attributes."""
    response.delete_cookie(
        name, path="/", secure=context.settings.secure_cookies, httponly=True, samesite="lax"
    )


async def _workflow(context: ApiContext, workflow_id: str) -> Workflow:
    workflow = await context.repo.workflow(workflow_id)
    if workflow is None:
        raise ApiError(404, "NOT_FOUND", f"no workflow {workflow_id}")
    return workflow


async def _detail(context: ApiContext, workflow: Workflow) -> WorkflowDetail:
    repo = context.repo
    hold, _ = await repo.hold(context.seed.scope)
    actors = await context.coordinator.actor_statuses(hold.generation)
    store = ObservationStore(repo.table)
    resources = []
    for kind in ResourceKind:  # the API keeps one order; resources the app lacks are left out
        if kind not in context.seed.managed:
            continue
        state = await repo.resource(workflow.id, kind)
        resources.append(
            _resource(state, await store.latest(workflow.id, kind.value), context.clock.now())
        )
    for resource in resources:
        if resource.kind is ResourceKind.OBJECT_STORAGE and context.seed.resources.object:
            resource.details.setdefault("object", {})["expectedApps"] = (
                context.seed.resources.object.expected_apps
            )
    preparation = {}
    for preparation_kind in ("INFRA", "COMPUTE"):
        saved = await store.latest(workflow.id, preparation_kind)
        preparation[preparation_kind] = {
            "data": saved.get("data") if saved else None,
            "observation": observation_state(saved, context.clock.now()),
        }
    summary = await store.latest(workflow.id, "PUBLIC")
    public_probe = summary.get("data") if summary else None
    if public_probe:
        public_probe["freshness"] = freshness(public_probe.get("lastSampleAt"), context.clock.now())
    business = []
    for env in (workflow.source_env, workflow.target_env):
        probe = await repo.latest_business_probe(workflow.id, env)
        business.append(
            {
                "env": env,
                "latest": probe.model_dump(mode="json", by_alias=True) if probe else None,
                "freshness": freshness(
                    probe.finished_at if probe else None, context.clock.now(), 30
                ),
                "collectionState": "SKIPPED_HELD"
                if hold.desired.value == "HOLD" and hold.workflow_id == workflow.id
                else "ENABLED",
            }
        )
    commands = [
        {
            "resource": command.resource.value,
            "action": command.action,
            "phase": command.phase.value,
            "operationId": command.operation_id,
            "status": command.operation_status,
            "transitionId": command.body.get("transitionId"),
            "attempt": command.attempt,
            "errorCode": (command.error or {}).get("code"),
            "observation": command.observation,
        }
        for command in await repo.commands(workflow.id)
    ]
    active = await repo.active_workflow_id()
    monitoring = not workflow.status.terminal or (
        workflow.finished_at is not None
        and context.clock.now() <= workflow.finished_at + timedelta(seconds=60)
    )
    live = monitoring and (
        active == workflow.id or (active is None and hold.workflow_id == workflow.id)
    )
    hold_view = HoldView(
        is_live=live,
        redis_route=hold.redis_route,
        redis_endpoint=hold.redis_endpoint,
        redis_route_generation=hold.redis_route_generation,
        desired=hold.desired if live else "UNKNOWN",
        generation=hold.generation if live else 0,
        held_since=hold.held_since if live else None,
        requested_at=hold.requested_at if live else None,
        released_at=hold.released_at if live else None,
        observed_at=context.clock.now() if live else None,
        actors=[
            ActorView(
                actor_id=a.actor_id,
                state=a.state,
                in_flight=a.in_flight,
                reported=a.reported,
                expected=a.expected,
                observed_at=a.observed_at,
                active_web_sockets=a.active_web_sockets,
            )
            for a in actors
        ]
        if live
        else [],
    )
    if not live:
        app = await store.latest(workflow.id, "APP")
        if app:
            saved_app = app["data"]
            hold_view = HoldView(
                is_live=False,
                redis_route=saved_app.get("redisRoute", "DIRECT"),
                redis_route_generation=saved_app.get("redisRouteGeneration", 0),
                desired=saved_app["desired"],
                generation=saved_app["generation"],
                held_since=saved_app.get("heldSince"),
                requested_at=saved_app.get("requestedAt"),
                released_at=saved_app.get("releasedAt"),
                observed_at=app["lastSuccessAt"],
                actors=[ActorView.model_validate(a) for a in saved_app["actors"]],
            )
    return WorkflowDetail(
        preparation=preparation,
        public_probe=public_probe,
        business_probes=business,
        commands=commands,
        workflow=workflow,
        allowed_commands=allowed_commands(workflow),
        stages=[StageView.of(stage) for stage in await repo.stages(workflow.id)],
        resources=resources,
        traffic=await repo.traffic_records(workflow.id),
        hold=hold_view,
    )


def _resource(
    state: ResourceState, snapshot: dict[str, Any] | None = None, now: datetime | None = None
) -> ResourceView:
    # The observer owns separate keys and never overwrites engine state.
    if snapshot and (snapshot.get("data") or {}).get("migrationId") not in (
        None,
        state.migration_id,
    ):
        snapshot = None
    status: dict[str, Any] = (snapshot or {}).get("data") or state.last_status or {}
    if now:
        status = present(status, now)
    observed = status.get("observed") or {}
    return ResourceView(
        kind=state.kind,
        migration_id=state.migration_id,
        lifecycle=status.get("lifecycle"),
        health=status.get("health"),
        route=observed.get("route", state.last_route),
        phase=status.get("phase"),
        target_writes_possible=bool(
            observed.get("targetWritesPossible", state.target_writes_possible)
        ),
        readiness=status.get("readiness"),
        observed_at=observed.get("observedAt"),
        progress=status.get("progress"),
        checks=status.get("checks") or [],
        rollback=status.get("rollback"),
        business_write_access=observed.get("businessWriteAccess"),
        details=status.get("details") or {},
        replication=status.get("replication"),
        routing=status.get("routing"),
        active_operation_id=status.get("activeOperationId"),
        transition_id=status.get("transitionId"),
        observation=observation_state(snapshot, now) if now else {},
    )


@router.get("/v1/workflows/{workflow_id}/observations")
async def observations(
    workflow_id: str,
    context: Context,
    session: CurrentSession,
    since: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    if workflow_id.startswith(ONPREM_PREFIX):
        await context.onprem_plans.get(workflow_id, session.user.id)
        return {"observations": [], "nextCursor": None}
    await _workflow(context, workflow_id)
    now = context.clock.now()
    since = since or now - timedelta(minutes=5)
    if since.tzinfo is None or since < now - timedelta(hours=24) or since > now:
        raise ApiError(
            422, "INVALID_RANGE", "since must be timezone-aware and within the last 24 hours"
        )
    if cursor and (
        len(cursor) > 160 or not cursor.startswith("OBS#") or len(cursor.split("#")) != 3
    ):
        raise ApiError(422, "INVALID_CURSOR", "invalid observation cursor")
    if cursor:
        cursor_at = timestamp(cursor.split("#")[1])
        if cursor_at is None or not since <= cursor_at <= now:
            raise ApiError(422, "INVALID_CURSOR", "cursor is outside the requested time range")
    rows, after = await ObservationStore(context.repo.table).history(
        workflow_id, since=since, until=now, after=cursor, limit=limit
    )
    return {"observations": rows, "nextCursor": after}


@router.post(
    "/internal/workflows/{workflow_id}/preparation", dependencies=[Depends(observation_caller)]
)
async def preparation_report(
    workflow_id: str, body: PreparationReport, context: Context
) -> dict[str, str]:
    raise ApiError(410, "USE_OTLP", "publish preparation snapshots through the OTEL Collector")
