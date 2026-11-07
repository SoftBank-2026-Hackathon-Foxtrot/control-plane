"""Who is calling, and whether this change may go through."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, Request

from control_plane.auth import LOCAL_USER, AuthService
from control_plane.clock import Clock
from control_plane.config import Settings
from control_plane.domain.models import Session
from control_plane.errors import ApiError
from control_plane.seed import Seed
from control_plane.store.repository import Repository
from control_plane.workflows import WorkflowService
from control_plane.workload.coordinator import WorkloadCoordinator


@dataclass(frozen=True)
class ApiContext:
    settings: Settings
    seed: Seed
    repo: Repository
    clock: Clock
    auth: AuthService
    workflows: WorkflowService
    coordinator: WorkloadCoordinator
    workload_token: str
    observation_token: str = ""

    @property
    def session_cookie(self) -> str:
        # The __Host- prefix makes browsers refuse anything but a Secure, host-only cookie.
        return "__Host-foxtrot_session" if self.settings.secure_cookies else "foxtrot_session"

    @property
    def login_cookie(self) -> str:
        return "__Host-foxtrot_login" if self.settings.secure_cookies else "foxtrot_login"


def api_context(request: Request) -> ApiContext:
    context: ApiContext = request.app.state.context
    return context


Context = Annotated[ApiContext, Depends(api_context)]


async def current_session(request: Request, context: Context) -> Session:
    if context.settings.auth_disabled:
        now = context.clock.now()
        return Session(
            user=LOCAL_USER,
            csrf_token="local",
            expires_at=now + timedelta(hours=1),
            created_at=now,
        )
    session = await context.auth.session(request.cookies.get(context.session_cookie))
    if session is None:
        raise ApiError(401, "UNAUTHENTICATED", "log in first")
    return session


CurrentSession = Annotated[Session, Depends(current_session)]


async def checked_change(
    request: Request,
    session: CurrentSession,
    context: Context,
    x_csrf_token: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
) -> Session:
    """Changes need the session's token and an allowed origin. SameSite alone is not relied on."""
    if context.settings.auth_disabled:
        return session
    if origin not in context.settings.allowed_origins:
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "this origin may not change anything")
    if not x_csrf_token or not hmac.compare_digest(
        x_csrf_token.encode(), session.csrf_token.encode()
    ):
        raise ApiError(403, "CSRF_REJECTED", "the CSRF token is missing or wrong")
    return session


CheckedSession = Annotated[Session, Depends(checked_change)]


async def workload_caller(
    context: Context, authorization: Annotated[str | None, Header()] = None
) -> None:
    expected = f"Bearer {context.workload_token}"
    if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise ApiError(401, "UNAUTHENTICATED", "a workload token is required")


async def observation_caller(
    context: Context, authorization: Annotated[str | None, Header()] = None
) -> None:
    if (
        not context.observation_token
        or not authorization
        or not hmac.compare_digest(
            authorization.encode(), f"Bearer {context.observation_token}".encode()
        )
    ):
        raise ApiError(401, "UNAUTHENTICATED", "a Collector ingest token is required")
