"""Application configuration endpoints; the owner always comes from the session."""

from fastapi import APIRouter, Header

from control_plane.api.dependencies import CheckedSession, Context, CurrentSession
from control_plane.api.idempotency import once
from control_plane.applications import (
    ApplicationInput,
    ApplicationService,
    ConfigurationInput,
    MigrationPolicy,
    bind_configuration,
)
from control_plane.catalog import catalog_for
from control_plane.errors import ApiError

router = APIRouter(tags=["Applications"])


def service(context: Context) -> ApplicationService:
    return ApplicationService(context.repo, context.clock)


@router.post("/v1/applications", status_code=201)
async def register(
    body: ApplicationInput,
    session: CheckedSession,
    context: Context,
    idempotency_key: str | None = Header(default=None),
) -> object:
    _check_secrets(context, body)
    return await once(
        context.repo,
        user_id=session.user.id,
        path="/v1/applications",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=201,
        execute=lambda: service(context).create(session.user.id, body),
    )


@router.get("/v1/applications/{app_id}")
async def detail(app_id: str, session: CurrentSession, context: Context) -> object:
    return await service(context).get(session.user.id, app_id)


@router.patch("/v1/applications/{app_id}")
async def update(
    app_id: str,
    body: ApplicationInput,
    session: CheckedSession,
    context: Context,
    expected_version: int = Header(ge=1),
    idempotency_key: str | None = Header(default=None),
) -> object:
    _check_secrets(context, body)
    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/applications/{app_id}",
        key=idempotency_key,
        body={**body.model_dump(), "version": expected_version},
        success_status=200,
        execute=lambda: service(context).update(session.user.id, app_id, body, expected_version),
    )


@router.delete("/v1/applications/{app_id}")
async def unregister(
    app_id: str,
    session: CheckedSession,
    context: Context,
    expected_version: int = Header(ge=1),
    idempotency_key: str | None = Header(default=None),
) -> object:
    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/applications/{app_id}/archive",
        key=idempotency_key,
        body={"version": expected_version},
        success_status=200,
        execute=lambda: service(context).archive(session.user.id, app_id, expected_version),
    )


def _check_secrets(context: Context, body: ApplicationInput) -> None:
    references = list(body.image.secret_refs.values())
    if body.image.registry_credential_ref:
        references.append(body.image.registry_credential_ref)
    if any(ref not in context.settings.allowed_secret_refs for ref in references):
        raise ApiError(422, "SECRET_NOT_ALLOWED", "select an approved Secret reference")


@router.get("/v1/catalog")
async def catalog(session: CurrentSession, context: Context) -> object:
    seed = catalog_for(context.settings, context.seed, session.user.id)
    return {
        "environments": list(seed.environments),
        "resources": {
            kind: {"sourceRef": resource.source_ref, "targetRef": resource.target_ref}
            for kind, resource in (
                ("database", seed.resources.database),
                ("redis", seed.resources.redis),
                ("object", seed.resources.object),
            )
        },
    }


@router.put("/v1/applications/{app_id}/resources")
async def connect_resources(
    app_id: str,
    body: ConfigurationInput,
    session: CheckedSession,
    context: Context,
    idempotency_key: str | None = Header(default=None),
) -> object:
    seed = catalog_for(context.settings, context.seed, session.user.id)
    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/applications/{app_id}/resources",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=200,
        execute=lambda: bind_configuration(
            service(context),
            session.user.id,
            app_id,
            body.expected_version,
            seed,
        ),
    )


@router.get("/v1/applications/{app_id}/resources")
async def resources(app_id: str, session: CurrentSession, context: Context) -> object:
    app = await service(context).get(session.user.id, app_id)
    if app.configuration is None:
        return {"connected": False}
    return {"connected": True, "resources": app.configuration.resources}


@router.get("/v1/applications/{app_id}/environments/{environment_id}")
async def environment(
    app_id: str,
    environment_id: str,
    session: CurrentSession,
    context: Context,
) -> object:
    app = await service(context).get(session.user.id, app_id)
    if app.configuration is None or environment_id not in app.configuration.environments:
        raise ApiError(404, "NOT_FOUND", "no connected environment")
    return app.configuration.environments[environment_id]


@router.post("/v1/applications/{app_id}/verify")
async def verify(
    app_id: str,
    body: ConfigurationInput,
    session: CheckedSession,
    context: Context,
    idempotency_key: str | None = Header(default=None),
) -> object:
    from control_plane.verification import verify_application

    async def execute() -> object:
        app = await service(context).get(session.user.id, app_id)
        await service(context).idle(app)
        service(context).expect(app, body.expected_version)
        # Runtime observation is connected by the runtime adapter, never inferred from a digest.
        available = await context.runtime.image_available(session.user.id, app.id, app.image)
        return await verify_application(
            service(context),
            app,
            context.services.configured(app.configuration).adapters if app.configuration else {},
            context.prober,
            available,
        )

    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/applications/{app_id}/verify",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=200,
        execute=execute,
    )


@router.get("/v1/applications/{app_id}/policy")
async def policy(app_id: str, session: CurrentSession, context: Context) -> object:
    return (await service(context).get(session.user.id, app_id)).policy


class PolicyInput(ConfigurationInput):
    policy: MigrationPolicy


@router.put("/v1/applications/{app_id}/policy")
async def change_policy(
    app_id: str,
    body: PolicyInput,
    session: CheckedSession,
    context: Context,
    idempotency_key: str | None = Header(default=None),
) -> object:
    from control_plane.governance import set_policy

    async def execute() -> object:
        app = await service(context).get(session.user.id, app_id)
        saved = await set_policy(service(context), app, body.policy, body.expected_version)
        await service(context).audit(saved, "POLICY_CHANGED", {"version": saved.version})
        return saved

    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/applications/{app_id}/policy",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=200,
        execute=execute,
    )


@router.get("/v1/applications/{app_id}/events")
async def audit_events(
    app_id: str,
    session: CurrentSession,
    context: Context,
    after: str | None = None,
    limit: int = 100,
) -> object:
    import json

    from control_plane.errors import ApiError

    await service(context).get(session.user.id, app_id)
    if not 1 <= limit <= 500:
        raise ApiError(422, "INVALID_LIMIT", "limit must be 1 to 500")
    page = await context.repo.table.query(
        f"APP_AUDIT#{session.user.id}#{app_id}",
        after=after,
        limit=limit,
    )
    return {"events": [json.loads(item.body) for item in page.items], "nextCursor": page.next_sk}


@router.post("/v1/applications/{app_id}/environments/{environment_id}/prepare", status_code=202)
async def prepare_runtime(
    app_id: str,
    environment_id: str,
    body: ConfigurationInput,
    session: CheckedSession,
    context: Context,
    idempotency_key: str | None = Header(default=None),
) -> object:
    target = context.settings.runtime_targets.get(f"{session.user.id}/{environment_id}")
    if target is None:
        raise ApiError(
            422, "RUNTIME_NOT_ASSIGNED", "no runtime assigned to this personal workspace"
        )
    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/applications/{app_id}/environments/{environment_id}/prepare",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=202,
        execute=lambda: context.runtime.prepare(
            session.user.id,
            app_id,
            environment_id,
            body.expected_version,
            target,
        ),
    )


@router.get("/v1/runtime-operations/{operation_id}")
async def runtime_status(operation_id: str, session: CurrentSession, context: Context) -> object:
    return await context.runtime.get(session.user.id, operation_id)


@router.post("/v1/runtime-operations/{operation_id}/{action}", status_code=202)
async def runtime_action(
    operation_id: str,
    action: str,
    body: ConfigurationInput,
    session: CheckedSession,
    context: Context,
    idempotency_key: str | None = Header(default=None),
) -> object:
    if action not in {"restore", "reconcile"}:
        raise ApiError(404, "NOT_FOUND", "no runtime action")
    method = context.runtime.request_restore if action == "restore" else context.runtime.reconcile
    return await once(
        context.repo,
        user_id=session.user.id,
        path=f"/v1/runtime-operations/{operation_id}/{action}",
        key=idempotency_key,
        body=body.model_dump(),
        success_status=202,
        execute=lambda: method(session.user.id, operation_id, body.expected_version),
    )
