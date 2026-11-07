"""An explicit public allowlist: never forward adapter payloads, plans or credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def fields(names: str) -> dict[str, Any]:
    return dict.fromkeys(names.split(), True)


DECISION = fields("result reasons")
VERIFICATION = fields(
    "method scope result evidenceKind observedAt transitionId operationId checkedCount "
    "mismatchCount committedThroughLsn durableOriginLsn barrierId valid comparedBuckets "
    "mismatchedBuckets matched missing different extra"
)
CHECK = {
    **fields("code stage required evidenceRef observedAt validUntil transitionId topologyRevision"),
    "decision": DECISION,
}
READINESS = {key: DECISION for key in ("cutover", "resume", "rollback", "advance")}
FLOW = fields("state backlog lagSeconds")
OBJECT_DECISION = {
    **DECISION,
    "checks": [{**fields("name ok"), "verification": VERIFICATION}],
}
DETAILS = {
    "database": {
        **fields(
            "observedAt observationAvailable proxyPaused sourceWriterSessions "
            "targetWriterSessions sourcePreparedTransactions targetPreparedTransactions "
            "targetReachable accessEvidence"
        ),
        "verification": VERIFICATION,
    },
    "redis": {
        **fields("dataRoute producerRoute consumerRoute sourceConsumerAccess drainStatus"),
        "sync": {
            **fields(
                "running halted backlog lag_ms changed_keys not_ready error observedAt "
                "direction copied oldestDirtyAgeMs epoch"
            ),
            "verification": VERIFICATION,
        },
        "proxy": fields(
            "gen paused inflight_writes open_tx last_pause_ms pubsub_sessions "
            "pubsub_moved pubsub_cut pubsub_dropped observedAt"
        ),
    },
    "object": {
        **fields(
            "appsLive lastSuccessfulSyncAt lastVerificationAt lastProgressAt "
            "initialCopyObservedAt forwardObservedAt reverseObservedAt"
        ),
        "apps": fields("generation live acked waiting recovering oldestReportAt observedAt"),
        "readiness": {key: OBJECT_DECISION for key in ("cutover", "rollback", "advance")},
    },
    "compute": {
        **fields("targetRef imageId configVersion containerId probePort"),
        "check": True,
    },
}
STATUS = {
    **fields(
        "migrationId workflowId resourceId kind revision lifecycle health phase "
        "activeOperationId transitionId topologyRevision lastSuccessfulSyncAt "
        "lastVerificationAt lastProgressAt"
    ),
    "observed": fields("observedAt route businessWriteAccess targetWritesPossible"),
    "progress": fields("completed total unit bytes totalBytes"),
    "readiness": READINESS,
    "rollback": fields("method reasons"),
    "checks": [CHECK],
    "details": DETAILS,
    "replication": {
        "forward": FLOW,
        "reverse": FLOW,
        "initialCopy": fields("completed total unit bytes totalBytes"),
    },
    "routing": fields("read write generation"),
}
OPERATION = fields(
    "operationId migrationId action status requestedAt startedAt finishedAt phase confirmedChanges"
)
PREPARATION = {
    **fields(
        "migrationId workflowId stage status observedAt requestedAt startedAt finishedAt "
        "jobId targetRef vmId cloud imageId configVersion containerId"
    ),
    "changes": fields("create update delete replace"),
    "check": True,
    "operations": [OPERATION],
}


def project(value: Any, spec: Any) -> Any:
    if value is None:
        return None
    if spec is True:
        if isinstance(value, str):
            return value[:512]
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, list):
            return [
                project(item, True) for item in value[:100] if not isinstance(item, (dict, list))
            ]
        return None
    if isinstance(spec, list):
        return [project(item, spec[0]) for item in value[:100]] if isinstance(value, list) else []
    if not isinstance(value, dict):
        return {}
    return {key: project(value[key], child) for key, child in spec.items() if key in value}


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
