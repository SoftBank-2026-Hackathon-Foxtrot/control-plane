"""A sandbox decision has its own revision and never mutates a workload or a cloud plan."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response

from control_plane.api.dependencies import CheckedSession, Context, CurrentSession
from control_plane.domain.models import Model
from control_plane.errors import ApiError
from control_plane.store.table import VersionConflict

from .api import service
from .models import fingerprint
from .rehearsal import run_rehearsal

router = APIRouter(prefix="/v1/security-review/onprem/rehearsal")
SORT_KEY = "ONPREM_REHEARSAL"


class Decision(Model):
    revision: int


def binary_path() -> str | None:
    path = Path(os.environ.get("CONTROL_PLANE_SECURITY_WEED_BINARY", ""))
    return str(path) if path.is_absolute() and path.is_file() else None


def review_key(request: Request, context: Context, user_id: str) -> str:
    users = os.environ.get("CONTROL_PLANE_SECURITY_REHEARSAL_USERS")
    if users is None:
        return service(request, context, user_id).config.key
    if user_id not in {value.strip() for value in users.split(",")}:
        raise ApiError(404, "NOT_FOUND", "보안 비교 대상이 없습니다.")
    return "SECURITY_SANDBOX#" + fingerprint(context.seed.application)


async def current(context: Context, key: str) -> dict[str, Any]:
    item = await context.repo.table.get(key, SORT_KEY)
    state: dict[str, Any] = json.loads(item.body) if item else {"status": "idle", "history": []}
    state.update(revision=item.version if item else 0, available=bool(binary_path()))
    if state["status"] == "running" and state["expiresAt"] <= context.clock.now().isoformat():
        state["status"] = "error"
    return state


async def save(context: Context, key: str, state: dict[str, Any]) -> None:
    try:
        state["revision"] = await context.repo.table.put(
            key, SORT_KEY, json.dumps(state), expected_version=state["revision"]
        )
    except VersionConflict as exc:
        raise ApiError(409, "REVIEW_CHANGED", "검사 상태를 다시 조회하세요.") from exc


@router.get("")
async def status(
    request: Request, response: Response, context: Context, session: CurrentSession
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await current(context, review_key(request, context, str(session.user.id)))


@router.post("/{action}")
async def change(
    action: str,
    command: Decision,
    request: Request,
    response: Response,
    context: Context,
    session: CheckedSession,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    key = review_key(request, context, str(session.user.id))
    state = await current(context, key)
    if state["revision"] != command.revision or state["status"] == "running":
        raise ApiError(409, "REVIEW_CHANGED", "검사 상태를 다시 조회하세요.")
    now = context.clock.now()
    actor = session.user.login
    if action in {"approve", "hold"}:
        if state["status"] != "tested" or state["expiresAt"] <= now.isoformat():
            raise ApiError(409, "PLAN_STALE", "검사를 다시 실행하세요.")
        state.update(
            status="approved" if action == "approve" else "held",
            decidedBy=actor,
            decidedAt=now.isoformat(),
        )
        state["history"] = [
            dict(
                at=now.isoformat(),
                actor=actor,
                action=action,
                revision=command.revision,
                result=state["result"],
            ),
            *state["history"],
        ][:20]
        await save(context, key, state)
        return state
    if action != "run":
        raise ApiError(404, "NOT_FOUND", "Unknown rehearsal action")
    binary = binary_path()
    if not binary:
        raise ApiError(503, "SANDBOX_UNAVAILABLE", "SeaweedFS 샌드박스 실행기를 연결하세요.")
    state = dict(
        revision=command.revision,
        available=True,
        status="running",
        startedAt=now.isoformat(),
        startedBy=actor,
        expiresAt=(now + timedelta(minutes=4)).isoformat(),
        history=state["history"],
    )
    await save(context, key, state)
    try:
        report = await asyncio.to_thread(
            run_rehearsal, binary, os.environ.get("CONTROL_PLANE_SECURITY_BEDROCK_MODEL")
        )
        state.update(status="tested" if report["passed"] else "failed", result=report)
    except Exception:
        state.update(status="error")
    finished = context.clock.now()
    state.update(
        finishedAt=finished.isoformat(), expiresAt=(finished + timedelta(minutes=10)).isoformat()
    )
    state["history"] = [
        dict(
            at=finished.isoformat(), actor=actor, action=state["status"], revision=command.revision
        ),
        *state["history"],
    ][:20]
    await save(context, key, state)
    return state
