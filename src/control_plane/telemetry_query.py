"""Small, authenticated console queries. Clients cannot provide backend URLs or query text."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import httpx
from fastapi import APIRouter

from control_plane.api.dependencies import Context, CurrentSession
from control_plane.errors import ApiError

router = APIRouter()
METRICS = (
    "foxtrot_observation_age",
    "foxtrot_observation_available",
    "foxtrot_migration_completed",
    "foxtrot_migration_total",
    "foxtrot_replication_backlog",
    "foxtrot_replication_lagseconds",
    "foxtrot_redis_backlog",
    "foxtrot_redis_lag_ms",
    "foxtrot_redis_oldestdirtyagems",
    "foxtrot_redis_proxy_inflight_writes",
    "foxtrot_redis_proxy_open_tx",
    "foxtrot_redis_proxy_last_pause_ms",
    "foxtrot_app_inflight",
    "foxtrot_app_activewebsockets",
    "foxtrot_probe_failed",
    "foxtrot_probe_p95ms",
)


async def read_backend(
    http: httpx.AsyncClient, url: str | None, path: str, params: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    if not url:
        return "NOT_CONFIGURED", {}
    try:
        # Read a bounded response even if a backend is misconfigured.
        async with http.stream("GET", url.rstrip("/") + path, params=params) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1048576:
                    return "UNAVAILABLE", {}
        data = json.loads(body)
        return ("OK", data) if isinstance(data, dict) else ("UNAVAILABLE", {})
    except (httpx.HTTPError, ValueError):
        return "UNAVAILABLE", {}


@router.get("/v1/workflows/{workflow_id}/telemetry")
async def query(workflow_id: str, context: Context, _: CurrentSession) -> dict[str, Any]:
    workflow = await context.repo.workflow(workflow_id)
    if workflow is None:
        raise ApiError(404, "NOT_FOUND", "unknown workflow")
    literal = json.dumps(workflow_id)
    promql = '{__name__=~"' + "|".join(METRICS) + '",workflow_id=' + literal + "}"
    end = workflow.finished_at or context.clock.now()
    start = max(workflow.created_at.timestamp(), end.timestamp() - 86400)
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as http:
        (metrics_state, metrics), (traces_state, traces) = await asyncio.gather(
            read_backend(
                http,
                context.settings.prometheus_url,
                "/api/v1/query",
                {"query": promql, "time": end.timestamp()},
            ),
            read_backend(
                http,
                context.settings.tempo_url,
                "/api/search",
                {
                    "q": "{ span.workflow.id = " + literal + " }",
                    "limit": 10,
                    "start": int(start),
                    "end": int(end.timestamp()),
                },
            ),
        )
    values = []
    try:
        if metrics_state == "OK" and metrics.get("status") != "success":
            metrics_state = "UNAVAILABLE"
        for row in (metrics.get("data") or {}).get("result", [])[:128]:
            labels = row["metric"]
            value = float(row["value"][1])
            at = float(row["value"][0])
            if labels.get("__name__") not in METRICS or not math.isfinite(value):
                continue
            values.append(
                {
                    "name": labels["__name__"],
                    "value": value,
                    "at": at,
                    "resource": labels.get("resource_kind"),
                    "direction": labels.get("direction"),
                    "actor": labels.get("actor_id"),
                    "service": labels.get("service_name"),
                    "freshness": "FRESH" if 0 <= end.timestamp() - at <= 20 else "STALE",
                }
            )
        trace_values = [
            {
                key: row.get(key)
                for key in (
                    "traceID",
                    "rootServiceName",
                    "rootTraceName",
                    "startTimeUnixNano",
                    "durationMs",
                )
            }
            for row in traces.get("traces", [])[:10]
        ]
    except (KeyError, TypeError, ValueError, OverflowError):
        return {
            "source": "OTEL",
            "metricsState": "UNAVAILABLE",
            "tracesState": "UNAVAILABLE",
            "metrics": [],
            "traces": [],
        }
    return {
        "source": "OTEL",
        "metricsState": metrics_state,
        "tracesState": traces_state,
        "metrics": values,
        "traces": trace_values,
    }
