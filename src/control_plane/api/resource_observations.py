"""Read-only console projection of the common API; never used to authorize a transition."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from typing import Any

from control_plane.adapters.client import AdapterError, AdapterUnreachable
from control_plane.api.dependencies import ApiContext
from control_plane.api.schemas import ResourceView
from control_plane.domain.models import ResourceState, Workflow, WorkflowStatus

MAX_AGE_SECONDS = 15
READ_TIMEOUT_SECONDS = 2


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _present(value: Any) -> Any:
    """Optional nested objects may be null on the wire; omission means unreported in the UI."""
    if isinstance(value, dict):
        return {key: _present(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_present(item) for item in value]
    return value


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _freshness(value: Any, now: datetime) -> str:
    moment = _timestamp(value)
    if moment is None or (moment - now).total_seconds() > 5:
        return "UNKNOWN"
    return "FRESH" if (now - moment).total_seconds() <= MAX_AGE_SECONDS else "STALE"


def project_resource(
    state: ResourceState,
    *,
    now: datetime,
    raw: dict[str, Any] | None = None,
    collected_at: datetime | None = None,
    collection_error: str | None = None,
    historical: bool = False,
) -> ResourceView:
    status = deepcopy(state.last_status or {}) if raw is None else deepcopy(raw)
    observed = _mapping(status.get("observed"))
    collected_at = collected_at or state.last_status_at
    observed_at = observed.get("observedAt")
    freshness = _freshness(observed_at, now)
    if collection_error or historical:
        freshness = "STALE" if status else "UNAVAILABLE"
    details = _present(_mapping(status.get("details")))
    redis = _mapping(details.get("redis"))
    for name in ("sync", "proxy"):
        part = _mapping(redis.get(name))
        if not part:
            continue
        reported_freshness = part.get("freshness")
        part["freshness"] = (
            reported_freshness
            if reported_freshness in {"UNKNOWN", "STALE", "UNAVAILABLE"}
            else _freshness(part.get("observedAt"), now)
        )
        if collection_error or historical:
            part["freshness"] = "STALE"
        if part.get("error"):
            part["freshness"] = "UNAVAILABLE"
    replication = _present(_mapping(status.get("replication")))
    streams = deepcopy(status.get("streams") or [])
    sync = _mapping(redis.get("sync"))
    direction = sync.get("direction")
    if direction in {"FORWARD", "REVERSE"}:
        running, halted = sync.get("running"), sync.get("halted")
        stream_state = (
            "PAUSED"
            if halted is True
            else "RUNNING"
            if running is True and halted is False
            else "STOPPED"
            if running is False
            else "UNKNOWN"
        )
        if sync.get("error"):
            stream_state = "ERROR"
        if not streams:
            streams = [
                {
                    "channel": "REDIS_SYNC",
                    "direction": direction,
                    "state": stream_state,
                    "freshness": sync.get("freshness", "UNKNOWN"),
                    "observedAt": sync.get("observedAt"),
                    "copied": sync.get("copied"),
                    "backlog": sync.get("backlog"),
                    "lagMs": sync.get("lag_ms"),
                }
            ]
        flow_state = stream_state if sync.get("freshness") == "FRESH" else "UNKNOWN"
        replication.setdefault(
            direction.lower(), {"state": flow_state, "backlog": sync.get("backlog")}
        )
    for stream in streams:
        if collection_error or historical:
            stream["freshness"] = "STALE"
        elif stream.get("freshness") not in {"UNKNOWN", "STALE", "UNAVAILABLE"}:
            stream["freshness"] = _freshness(stream.get("observedAt"), now)
    checks = []
    for reported in status.get("checks") or []:
        check = deepcopy(reported)
        result = _mapping(check.get("decision")).get("result", "UNKNOWN")
        expires = _timestamp(check.get("validUntil"))
        check["currentResult"] = (
            result if freshness == "FRESH" and expires is not None and expires > now else "UNKNOWN"
        )
        checks.append(check)
    return ResourceView(
        kind=state.kind,
        migration_id=state.migration_id,
        lifecycle=status.get("lifecycle"),
        health=status.get("health"),
        route=observed.get("route", state.last_route),
        phase=status.get("phase"),
        target_writes_possible=state.target_writes_possible
        or bool(observed.get("targetWritesPossible")),
        readiness=status.get("readiness"),
        observed_at=observed_at,
        business_write_access=observed.get("businessWriteAccess"),
        transition_id=status.get("transitionId", state.transition_id),
        active_operation_id=status.get("activeOperationId"),
        details=details,
        progress=status.get("progress"),
        replication=replication or None,
        streams=streams,
        checks=checks,
        rollback=status.get("rollback"),
        observation={
            "source": "COMMON_API",
            "freshness": freshness,
            "collectedAt": collected_at,
            "lastSuccessAt": collected_at,
            "collectionError": collection_error,
        },
    )


async def read_resource(
    context: ApiContext, workflow: Workflow, state: ResourceState
) -> ResourceView:
    now = context.clock.now()
    if workflow.status in {WorkflowStatus.COMPLETED, WorkflowStatus.ABORTED}:
        return project_resource(state, now=now, historical=True)
    if state.migration_id is None:
        return project_resource(state, now=now)
    try:
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            view = await context.workflows.adapters[state.kind].migration(state.migration_id)
        return project_resource(state, now=now, raw=view.raw, collected_at=now)
    except (AdapterError, AdapterUnreachable, TimeoutError):
        # Do not expose URLs, tokens or response bodies through connection error strings.
        return project_resource(state, now=now, collection_error="MODULE_OBSERVATION_UNAVAILABLE")
