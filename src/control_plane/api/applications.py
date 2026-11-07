"""Application configuration endpoints; the owner always comes from the session."""

from fastapi import APIRouter, Header

from control_plane.api.dependencies import CheckedSession, Context, CurrentSession
from control_plane.api.idempotency import once
from control_plane.applications import ApplicationInput, ApplicationService

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
