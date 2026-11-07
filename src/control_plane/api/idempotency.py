"""Console POSTs can be retried by a browser or a person. The same key has one effect."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from control_plane.errors import ApiError
from control_plane.store.repository import Repository


async def once(
    repo: Repository,
    *,
    user_id: str,
    path: str,
    key: str | None,
    body: dict[str, Any],
    success_status: int,
    execute: Callable[[], Awaitable[Any]],
) -> JSONResponse:
    if not key:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "the Idempotency-Key header is required")
    storage_key = f"{user_id}:{path}:{key}"
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    claimed = await repo.claim_idempotency(storage_key, {"digest": digest, "state": "RUNNING"})
    if not claimed:
        existing = await repo.idempotent_response(storage_key)
        assert existing is not None
        if existing["digest"] != digest:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "this key was used with another body")
        if existing["state"] == "RUNNING":
            raise ApiError(409, "REQUEST_IN_PROGRESS", "the first request is still running")
        return JSONResponse(existing["response"], status_code=existing["status"])
    try:
        result = jsonable_encoder(await execute(), by_alias=True)
    except ApiError:
        # A refused request changed nothing and its answer can go stale (a version conflict, say),
        # so the key is freed rather than made to replay it.
        await repo.forget_idempotency(storage_key)
        raise
    await repo.complete_idempotency(
        storage_key,
        {"digest": digest, "state": "DONE", "status": success_status, "response": result},
    )
    return JSONResponse(result, status_code=success_status)
