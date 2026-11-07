"""The HTTP surface. Validates, delegates and shapes the answer; decisions live elsewhere.

The browser calls `/api/...`; the gateway, or Vite in development, removes the prefix.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse

from control_plane.api.dependencies import (
    ApiContext,
    CheckedSession,
    Context,
    CurrentSession,
    workload_caller,
)
from control_plane.api.idempotency import once
from control_plane.api.schemas import (
    ActorView,
    ApplicationView,
    CreateWorkflow,
    DirectiveView,
    EnvironmentView,
    EventPage,
    Heartbeat,
    HoldView,
    Me,
    ResourceView,
    StageView,
    WorkflowCommand,
    WorkflowDetail,
    Workspace,
    allowed_commands,
)
from control_plane.applications import Application, ApplicationService
from control_plane.domain.models import ResourceKind, ResourceState, Session, Workflow
from control_plane.engine.context import Services
from control_plane.errors import ApiError
from control_plane.governance import enforce
from control_plane.seed import Seed
from control_plane.workflows import CommandInput, WorkflowService
from control_plane.workload.coordinator import WorkloadCoordinator

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
async def applications(
    session: CurrentSession, context: Context
) -> list[Application | ApplicationView]:
    registered = await ApplicationService(context.repo, context.clock).list(session.user.id)
    if not _owns_seed(context, session):
        return [*registered]
    seed = context.seed
    return [
        ApplicationView(
            id=seed.application,
            scope=seed.scope,
            environments=[
                EnvironmentView(name=name, traffic_pool=env.traffic_pool, actors=env.actors)
                for name, env in seed.environments.items()
            ],
            resources=list(ResourceKind),
        ),
        *registered,
    ]


# ---- workflows --------------------------------------------------------------------------------
@router.get("/v1/workflows", response_model_by_alias=True)
async def list_workflows(session: CurrentSession, context: Context) -> list[Workflow]:
    found = [await context.repo.workflow(i) for i in await context.repo.workflow_ids()]
    return sorted(
        (workflow for workflow in found if workflow and workflow.owner_id == session.user.id),
        key=lambda workflow: workflow.created_at,
        reverse=True,
    )


@router.post("/v1/workflows", status_code=201)
async def create_workflow(
    body: CreateWorkflow,
    session: CheckedSession,
    context: Context,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Response:
    if body.application_id is None and not _owns_seed(context, session):
        raise ApiError(404, "NOT_FOUND", "no application")

    async def execute() -> WorkflowDetail:
        selected = context.workflows
        snapshot = None
        if body.application_id:
            app = await ApplicationService(context.repo, context.clock).get(
                session.user.id, body.application_id
            )
            if app.status != "VERIFIED" or app.configuration is None:
                raise ApiError(409, "APP_NOT_VERIFIED", "verify the application first")
            try:
                configuration = enforce(app, body.source_env, body.target_env)
            except ApiError as error:
                await ApplicationService(context.repo, context.clock).audit(
                    app,
                    "POLICY_DENIED",
                    {"reasonCode": error.code},
                )
                raise
            app = app.model_copy(update={"configuration": configuration})
            services = context.services.configured(configuration)
            selected = WorkflowService(
                context.repo, configuration, context.clock, services.adapters
            )
            snapshot = app.model_dump(mode="json", by_alias=True)
        created = await selected.create(
            source_env=body.source_env,
            target_env=body.target_env,
            actor=session.user.login,
            owner_id=session.user.id,
            application_snapshot=snapshot,
        )
        return await _detail(context, created)

    return await once(
        context.repo,
        user_id=session.user.id,
        path="/v1/workflows",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=201,
        execute=execute,
    )


@router.get("/v1/workflows/{workflow_id}", response_model_by_alias=True)
async def get_workflow(
    workflow_id: str, session: CurrentSession, context: Context
) -> WorkflowDetail:
    return await _detail(context, await _workflow(context, workflow_id, session))


@router.get("/v1/workflows/{workflow_id}/events", response_model_by_alias=True)
async def workflow_events(
    workflow_id: str,
    session: CurrentSession,
    context: Context,
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> EventPage:
    await _workflow(context, workflow_id, session)
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
    workflow = await _workflow(context, workflow_id, session)

    async def execute() -> WorkflowDetail:
        services = _services(context, workflow)
        selected = WorkflowService(context.repo, services.seed, context.clock, services.adapters)
        updated = await selected.apply(
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


@router.api_route("/v1/provisioning/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def provisioning(_: CurrentSession, path: str) -> None:
    raise ApiError(501, "NOT_IMPLEMENTED", f"provisioning is reserved, not built: {path}")


# ---- called by the application's own processes -------------------------------------------------
@router.post(
    "/internal/v1/workloads/{scope}/heartbeat",
    dependencies=[Depends(workload_caller)],
    response_model_by_alias=True,
)
async def heartbeat(scope: str, body: Heartbeat, context: Context) -> DirectiveView:
    seed = context.seed
    if scope != seed.scope:
        item = await context.repo.table.get("APP_SCOPES", scope)
        if item is None:
            raise ApiError(404, "NOT_FOUND", f"no workload {scope}")
        import json

        ref = json.loads(item.body)
        app = await ApplicationService(context.repo, context.clock).get(ref["owner"], ref["id"])
        assert app.configuration is not None
        seed = app.configuration
    if body.actor_id not in seed.all_actors():
        raise ApiError(422, "UNKNOWN_ACTOR", f"{body.actor_id} is not registered")
    directive = await WorkloadCoordinator(context.repo, seed, context.clock).heartbeat(
        actor_id=body.actor_id,
        instance_id=body.instance_id,
        generation=body.generation,
        state=body.state,
        in_flight=body.in_flight,
        image_ref=body.image_ref,
    )
    return DirectiveView(
        desired=directive.desired,
        generation=directive.generation,
        object_migration_id=directive.object_migration_id,
        poll_after_ms=directive.poll_after_ms,
    )


# ---- helpers ----------------------------------------------------------------------------------
def _expire(response: Response, context: ApiContext, name: str) -> None:
    """Browsers ignore an attempt to clear a __Host- cookie without these attributes."""
    response.delete_cookie(
        name, path="/", secure=context.settings.secure_cookies, httponly=True, samesite="lax"
    )


def _owns_seed(context: ApiContext, session: Session) -> bool:
    if not context.settings.legacy_seed_enabled:
        return False
    owner = "local:developer" if context.settings.auth_disabled else context.settings.seed_owner_id
    return bool(owner) and owner == session.user.id


async def _workflow(context: ApiContext, workflow_id: str, session: Session) -> Workflow:
    workflow = await context.repo.workflow(workflow_id)
    if workflow is None or workflow.owner_id != session.user.id:
        raise ApiError(404, "NOT_FOUND", f"no workflow {workflow_id}")
    return workflow


async def _detail(context: ApiContext, workflow: Workflow) -> WorkflowDetail:
    repo = context.repo
    services = _services(context, workflow)
    hold, _ = await repo.hold(services.seed.scope)
    actors = await services.coordinator.actor_statuses(hold.generation)
    return WorkflowDetail(
        workflow=workflow,
        allowed_commands=allowed_commands(workflow),
        stages=[StageView.of(stage) for stage in await repo.stages(workflow.id)],
        resources=[_resource(state) for state in await repo.resources(workflow.id)],
        traffic=await repo.traffic_records(workflow.id),
        hold=HoldView(
            desired=hold.desired,
            generation=hold.generation,
            held_since=hold.held_since,
            actors=[
                ActorView(
                    actor_id=actor.actor_id,
                    state=actor.state,
                    in_flight=actor.in_flight,
                    reported=actor.reported,
                    expected=actor.expected,
                    observed_at=actor.observed_at,
                )
                for actor in actors
            ],
        ),
    )


def _resource(state: ResourceState) -> ResourceView:
    status: dict[str, Any] = state.last_status or {}
    return ResourceView(
        kind=state.kind,
        migration_id=state.migration_id,
        lifecycle=status.get("lifecycle"),
        health=status.get("health"),
        route=state.last_route,
        phase=status.get("phase"),
        target_writes_possible=state.target_writes_possible,
        readiness=status.get("readiness"),
        observed_at=state.last_status_at,
    )


def _services(context: ApiContext, workflow: Workflow) -> Services:
    if workflow.application_snapshot:
        return context.services.configured(
            Seed.model_validate(
                workflow.application_snapshot["configuration"],
            )
        )
    return context.services
