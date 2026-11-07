"""An explicit public allowlist: never forward adapter payloads, plans or credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from control_plane.telemetry_contract import STATUS, project


def status_projection(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = project(raw, STATUS)
    return result


def timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def freshness(at: Any, now: datetime, seconds: float = 15) -> str:
    parsed = timestamp(at)
    if parsed is None or parsed > now:
        return "UNKNOWN"
    return "FRESH" if (now - parsed).total_seconds() <= seconds else "STALE"


def present(data: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Keep historical decisions; add current applicability without changing control decisions."""
    out = status_projection(data)
    for check in out.get("checks") or []:
        until = timestamp(check.get("validUntil"))
        current = (
            freshness(check.get("observedAt"), now) == "FRESH"
            and until is not None
            and until >= now
            and check.get("transitionId") == out.get("transitionId")
            and check.get("topologyRevision") == out.get("topologyRevision")
        )
        check["currentResult"] = (
            (check.get("decision") or {}).get("result", "UNKNOWN") if current else "UNKNOWN"
        )
    details = out.get("details") or {}
    redis = details.get("redis") or {}
    for key in ("sync", "proxy"):
        signal = redis.get(key)
        if signal:
            signal["freshness"] = freshness(signal.get("observedAt"), now)
            if signal.get("error"):
                signal["freshness"] = "UNAVAILABLE"
    obj = details.get("object")
    if obj:
        obj["syncFreshness"] = freshness(obj.get("lastSuccessfulSyncAt"), now)
    return out
