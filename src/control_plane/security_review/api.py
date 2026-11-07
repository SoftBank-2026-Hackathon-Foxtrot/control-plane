"""Opt-in review endpoints. No workflow mutation, gate, approval or traffic commands."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response

from control_plane.api.dependencies import CheckedSession, Context, CurrentSession
from control_plane.errors import ApiError

from .models import Change, ReviewConfig
from .provider import CloudProvider, FixtureProvider
from .service import ReviewService

router = APIRouter()


def service(request: Request, context: Context, actor: str) -> ReviewService:
    existing: ReviewService | None = getattr(request.app.state, "security_review", None)
    if existing is None:
        path = os.environ.get("CONTROL_PLANE_SECURITY_REVIEW_CONFIG")
        inline = os.environ.get("CONTROL_PLANE_SECURITY_REVIEW_CONFIG_JSON")
        if not path and not inline:
            raise ApiError(
                503, "SECURITY_NOT_CONFIGURED", "보안 비교 대상이 아직 연결되지 않았습니다."
            )
        try:
            config = ReviewConfig.model_validate_json(inline or Path(str(path)).read_text())
            if (
                config.application != context.seed.application
                or "gcp" not in context.seed.environments
            ):
                raise ValueError("unregistered application or environment")
            if config.mode == "fixture" and context.settings.environment != "local":
                raise ValueError("fixture is local only")
        except (ValueError, OSError) as error:
            raise ApiError(
                503, "SECURITY_NOT_CONFIGURED", "보안 비교 설정을 확인하세요."
            ) from error

        async def probe() -> bool:
            if config.mode == "fixture":
                return True
            url = context.seed.environments["gcp"].app_url.rstrip("/") + "/readyz"
            try:
                async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                    return (await client.get(url)).status_code == 200
            except httpx.HTTPError:
                return False

        existing = ReviewService(
            config,
            FixtureProvider(config)
            if config.mode == "fixture"
            else CloudProvider(config, context.settings.region),
            context.repo.table,
            context.clock,
            probe,
        )
        request.app.state.security_review = existing
    if actor not in existing.config.allowed_user_ids:
        raise ApiError(404, "NOT_FOUND", "보안 비교 대상이 없습니다.")
    return existing


@router.get("/v1/security-review")
async def get_review(
    request: Request, response: Response, context: Context, session: CurrentSession
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    review = service(request, context, str(session.user.id))
    return review.view(await review.load())


@router.post("/v1/security-review/{action}")
async def change_review(
    action: str,
    body: Change,
    request: Request,
    response: Response,
    context: Context,
    session: CheckedSession,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    review = service(request, context, str(session.user.id))
    # The running task survives an HTTP disconnect; reload reads its persisted state.
    task = asyncio.create_task(review.change(action, body, session.user.login))
    tasks: set[asyncio.Task[dict[str, Any]]] = getattr(request.app.state, "security_tasks", set())
    request.app.state.security_tasks = tasks
    tasks.add(task)

    def finished(done: asyncio.Task[dict[str, Any]]) -> None:
        tasks.discard(done)
        if not done.cancelled():
            done.exception()  # Consume failures even if the HTTP caller disconnected.

    task.add_done_callback(finished)
    return await asyncio.shield(task)
