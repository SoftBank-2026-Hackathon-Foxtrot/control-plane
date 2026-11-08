"""Bounded monitoring views over emitted migration metrics, Tempo and snapshot history."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Query

from control_plane.api.dependencies import Context, CurrentSession
from control_plane.clock import stamp
from control_plane.domain.models import Workflow
from control_plane.errors import ApiError
from control_plane.observations import ObservationStore, observation_state
from control_plane.otel_ingest import MODULES
from control_plane.telemetry_query import read_backend

router = APIRouter()
Component = Literal["app", "db", "redis", "obj"]
TraceID = Annotated[str, Query(pattern=r"^[a-fA-F0-9]{32}$")]
COMPONENTS = {"app": "APP", "db": "DATABASE", "redis": "REDIS", "obj": "OBJECT_STORAGE"}
MODULE_COMPONENT = {
    "app": "app",
    "control-plane": "app",
    "database": "db",
    "redis": "redis",
    "object": "obj",
}


@router.get("/v1/telemetry/workflows", response_model_by_alias=True)
async def live_workflows(context: Context, _: CurrentSession) -> list[Workflow]:
    """Live selector, separate from the full workflow history and explicit detail links."""
    store = ObservationStore(context.repo.table)
    result = []
    now = context.clock.now()
    for workflow_id in await context.repo.workflow_ids():
        workflow = await context.repo.workflow(workflow_id)
        if not workflow or workflow.status.terminal or workflow.status == "PLANNED":
            continue
        for resource in MODULES:
            snapshot = await store.latest(workflow_id, resource)
            if observation_state(snapshot, now)["freshness"] == "FRESH":
                result.append(workflow)
                break
    return sorted(result, key=lambda workflow: workflow.created_at, reverse=True)


async def workflow_for(context: Context, workflow_id: str) -> Workflow:
    workflow = await context.repo.workflow(workflow_id)
    if workflow is None:
        raise ApiError(404, "NOT_FOUND", "unknown workflow")
    return workflow


def window(start: datetime, end: datetime, context: Context) -> tuple[float, float]:
    if start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 21600:
        raise ApiError(422, "INVALID_RANGE", "use a time range up to 6 hours with time zones")
    return start.timestamp(), min(end.timestamp(), context.clock.now().timestamp())


def gauge(workflow: str, resource: str, metric: str, extra: str = "") -> str:
    selector = (
        f"{metric}{{workflow_id={json.dumps(workflow)},"
        f"resource_kind={json.dumps(resource)}{extra}}}"
    )
    return f"({selector} and (timestamp({selector}) > time() - 20))"


def metric_queries(context: Context, workflow: Workflow, comp: Component) -> list[dict[str, Any]]:
    resource = COMPONENTS[comp]

    def shared(key: str, unit: str, metric: str, extra: str = "") -> dict[str, Any]:
        return {
            "key": key,
            "unit": unit,
            "scope": "workflow",
            "queries": {"shared": f"sum({gauge(workflow.id, resource, metric, extra)})"},
        }

    def pair(key: str, source: str, target: str) -> dict[str, Any]:
        return {
            "key": key,
            "unit": "",
            "scope": "environment",
            "queries": {
                "source": f"sum({gauge(workflow.id, resource, source)})",
                "target": f"sum({gauge(workflow.id, resource, target)})",
            },
        }

    if comp == "app":
        rows = []
        for key in ("inflight", "activewebsockets"):
            queries = {}
            for side, environment in (
                ("source", workflow.source_env),
                ("target", workflow.target_env),
            ):
                env = context.seed.environments.get(environment)
                actors = list(env.actors) if env else []
                actor_pattern = "|".join(re.escape(actor) for actor in actors) or "a^"
                selector = gauge(
                    workflow.id,
                    resource,
                    "foxtrot_app_" + key,
                    ",actor_id=~" + json.dumps(actor_pattern),
                )
                queries[side] = f"sum({selector})"
            rows.append({"key": key, "unit": "", "scope": "environment", "queries": queries})
        for key, metric, unit in (
            ("probe_failed", "foxtrot_probe_failed", ""),
            ("probe_p95", "foxtrot_probe_p95ms", "ms"),
        ):
            rows.append(
                {
                    "key": key,
                    "unit": unit,
                    "scope": "workflow",
                    "queries": {"shared": f"sum({gauge(workflow.id, 'PUBLIC', metric)})"},
                }
            )
        return rows
    if comp == "db":
        return [
            pair(
                "writers",
                "foxtrot_database_sourcewritersessions",
                "foxtrot_database_targetwritersessions",
            ),
            pair(
                "prepared",
                "foxtrot_database_sourcepreparedtransactions",
                "foxtrot_database_targetpreparedtransactions",
            ),
            shared("forward_lag", "s", "foxtrot_replication_lagseconds", ',direction="forward"'),
            shared("reverse_lag", "s", "foxtrot_replication_lagseconds", ',direction="reverse"'),
        ]
    if comp == "redis":
        return [
            shared("backlog", "", "foxtrot_redis_backlog"),
            shared("copied", "", "foxtrot_redis_copied"),
            shared("dirty_age", "ms", "foxtrot_redis_oldestdirtyagems"),
            shared("writes", "", "foxtrot_redis_proxy_inflight_writes"),
        ]
    return [
        shared("completed", "", "foxtrot_migration_completed"),
        shared("total", "", "foxtrot_migration_total"),
        shared("forward_backlog", "", "foxtrot_replication_backlog", ',direction="forward"'),
        shared("reverse_backlog", "", "foxtrot_replication_backlog", ',direction="reverse"'),
    ]


@router.get("/v1/workflows/{workflow_id}/telemetry/series")
async def series(
    workflow_id: str,
    context: Context,
    _: CurrentSession,
    comp: Component,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    step: Literal["10s", "30s", "60s"] = "60s",
) -> dict[str, Any]:
    workflow = await workflow_for(context, workflow_id)
    since, until = window(start, end, context)
    seconds = max(int(step[:-1]), math.ceil(max(0, until - since) / 360))
    count = max(0, int((until - since) // seconds) + 1)
    definitions = metric_queries(context, workflow, comp)
    states = []
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as http:

        async def read(query: str) -> tuple[str, list[float | None]]:
            values: list[float | None] = [None] * count
            if not count:
                return "OK", values
            state, data = await read_backend(
                http,
                context.settings.prometheus_url,
                "/api/v1/query_range",
                {
                    "query": query,
                    "start": since,
                    "end": until,
                    "step": seconds,
                },
            )
            if state != "OK":
                return state, values
            try:
                result = data["data"]["result"]
                if data["status"] != "success" or not isinstance(result, list) or len(result) > 1:
                    raise ValueError
                for at, raw in result[0]["values"] if result else []:
                    index = round((float(at) - since) / seconds)
                    value = float(raw)
                    if 0 <= index < count and math.isfinite(value) and value >= 0:
                        values[index] = value
                return "OK", values
            except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                return "UNAVAILABLE", [None] * count

        metrics = []
        for definition in definitions:
            sides = list(definition["queries"])
            results = await asyncio.gather(*(read(definition["queries"][side]) for side in sides))
            metric = {k: definition[k] for k in ("key", "unit", "scope")}
            metric.update(source=[None] * count, target=[None] * count, shared=[None] * count)
            for side, (state, values) in zip(sides, results, strict=True):
                states.append(state)
                metric[side] = values
            metrics.append(metric)
    state = (
        "UNAVAILABLE"
        if "UNAVAILABLE" in states
        else "NOT_CONFIGURED"
        if "NOT_CONFIGURED" in states
        else "OK"
    )
    return {"state": state, "comp": comp, "from": stamp(start), "step": seconds, "metrics": metrics}


@router.get("/v1/telemetry/collection")
async def collection(context: Context, _: CurrentSession, workflow: str) -> dict[str, Any]:
    selected = await workflow_for(context, workflow)
    store = ObservationStore(context.repo.table)
    at = selected.finished_at or context.clock.now()
    modules = []
    for resource in MODULES:
        snapshot = await store.latest(workflow, resource)
        status = observation_state(snapshot, at)
        modules.append(
            {
                "id": resource,
                "name": resource,
                "state": status["freshness"],
                "lastCollectedAt": (snapshot or {}).get("collectedAt"),
            }
        )
    return {"modules": modules}


def attributes(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        item["key"]: next(iter(item.get("value", {}).values()), None)
        for item in items
        if "key" in item
    }


def identifier(value: str, size: int) -> str:
    if re.fullmatch(r"[a-fA-F0-9]{" + str(size * 2) + "}", value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded.hex() if len(decoded) == size else ""
    except ValueError:
        return ""


def spans_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    # Tempo 2.9 returns OTLP JSON; IDs are protobuf bytes (base64), timestamps are strings.
    trace = data.get("trace", data)
    rows = []
    for resource in trace.get("resourceSpans", trace.get("batches", [])):
        resource_attrs = attributes(resource.get("resource", {}).get("attributes", []))
        for scope in resource.get("scopeSpans", resource.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                rows.append(
                    {
                        **span,
                        "resourceAttributes": resource_attrs,
                        "spanAttributes": attributes(span.get("attributes", [])),
                    }
                )
                if len(rows) > 2000:
                    raise ValueError("trace too large")
    return rows


def scoped_spans(data: dict[str, Any], workflow: str) -> list[dict[str, Any]]:
    rows = spans_from(data)
    # Only expose explicitly scoped spans. Another workflow sharing a trace is not authorized.
    return [row for row in rows if row["spanAttributes"].get("workflow.id") == workflow]


def trace_side(rows: list[dict[str, Any]], workflow: Workflow) -> str:
    envs = {
        row["spanAttributes"].get("environment")
        or row["resourceAttributes"].get("deployment.environment.name")
        for row in rows
    }
    if envs == {workflow.source_env}:
        return "source"
    if envs == {workflow.target_env}:
        return "target"
    return "shared"


def component(row: dict[str, Any]) -> str | None:
    return MODULE_COMPONENT.get(row["resourceAttributes"].get("foxtrot.module"))


def error_span(row: dict[str, Any]) -> bool:
    return row.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")


async def trace_data(
    http: httpx.AsyncClient, context: Context, trace_id: str
) -> tuple[str, dict[str, Any]]:
    return await read_backend(http, context.settings.tempo_url, f"/api/v2/traces/{trace_id}", {})


@router.get("/v1/workflows/{workflow_id}/traces")
async def traces(
    workflow_id: str,
    context: Context,
    _: CurrentSession,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    comp: Component | None = None,
) -> dict[str, Any]:
    workflow = await workflow_for(context, workflow_id)
    since, until = window(start, end, context)
    if since > until:
        return {"state": "OK", "traces": []}
    async with httpx.AsyncClient(
        timeout=3, follow_redirects=False, headers={"Accept": "application/json"}
    ) as http:
        state, data = await read_backend(
            http,
            context.settings.tempo_url,
            "/api/search",
            {
                "q": "{ span.workflow.id = " + json.dumps(workflow_id) + " }",
                "start": int(since),
                "end": int(until),
                "limit": 20,
            },
        )
        if state != "OK":
            return {"state": state, "traces": []}
        try:
            ids = [
                row["traceID"].lower()
                for row in data["traces"][:20]
                if re.fullmatch(r"[a-fA-F0-9]{32}", row["traceID"])
            ]
            semaphore = asyncio.Semaphore(4)

            async def load(trace_id: str) -> tuple[str, dict[str, Any]]:
                async with semaphore:
                    return await trace_data(http, context, trace_id)

            details = await asyncio.gather(*(load(trace_id) for trace_id in ids))
            result = []
            for trace_id, (item_state, payload) in zip(ids, details, strict=True):
                if item_state != "OK":
                    state = "UNAVAILABLE"
                    continue
                spans = scoped_spans(payload, workflow_id)
                if not spans:
                    continue
                comps = sorted({c for row in spans if (c := component(row))})
                if comp and comp not in comps:
                    continue
                first = min(spans, key=lambda row: int(row["startTimeUnixNano"]))
                begin = int(first["startTimeUnixNano"])
                finish = max(int(row["endTimeUnixNano"]) for row in spans)
                if not since <= begin / 1e9 <= until:
                    continue
                status_codes = [
                    str(row["spanAttributes"]["http.response.status_code"])
                    for row in spans
                    if "http.response.status_code" in row["spanAttributes"]
                ]
                result.append(
                    {
                        "traceId": trace_id,
                        "at": stamp(datetime.fromtimestamp(begin / 1e9, UTC)),
                        "name": str(first.get("name", ""))[:256],
                        "service": str(first["resourceAttributes"].get("service.name", ""))[:128],
                        "side": trace_side(spans, workflow),
                        "durationMs": max(0, (finish - begin) / 1e6),
                        "status": "error" if any(error_span(row) for row in spans) else "ok",
                        "statusCode": status_codes[0] if status_codes else None,
                        "components": comps,
                    }
                )
            return {
                "state": state,
                "traces": sorted(result, key=lambda row: row["at"], reverse=True),
                "limit": 20,
            }
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return {"state": "UNAVAILABLE", "traces": []}


@router.get("/v1/workflows/{workflow_id}/traces/{trace_id}")
async def trace_detail(
    workflow_id: str, trace_id: str, context: Context, _: CurrentSession
) -> dict[str, Any]:
    await workflow_for(context, workflow_id)
    if not re.fullmatch(r"[a-fA-F0-9]{32}", trace_id):
        raise ApiError(422, "INVALID_TRACE", "invalid trace ID")
    async with httpx.AsyncClient(
        timeout=3, follow_redirects=False, headers={"Accept": "application/json"}
    ) as http:
        state, data = await trace_data(http, context, trace_id.lower())
    if state != "OK":
        return {"state": state, "traceId": trace_id, "spans": []}
    try:
        spans = scoped_spans(data, workflow_id)
        if not spans:
            raise ApiError(404, "NOT_FOUND", "trace does not belong to this workflow")
        begin = min(int(row["startTimeUnixNano"]) for row in spans)
        result = [
            {
                "spanId": identifier(row["spanId"], 8),
                "parentSpanId": identifier(row.get("parentSpanId", ""), 8) or None,
                "name": str(row.get("name", ""))[:256],
                "startMs": (int(row["startTimeUnixNano"]) - begin) / 1e6,
                "durationMs": max(
                    0, (int(row["endTimeUnixNano"]) - int(row["startTimeUnixNano"])) / 1e6
                ),
                "status": "error" if error_span(row) else "ok",
            }
            for row in spans
        ]
        return {
            "state": "OK",
            "traceId": trace_id.lower(),
            "spans": sorted(result, key=lambda row: row["startMs"]),
        }
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return {"state": "UNAVAILABLE", "traceId": trace_id, "spans": []}


@router.get("/v1/workflows/{workflow_id}/logs")
async def logs(
    workflow_id: str,
    context: Context,
    _: CurrentSession,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    traceId: TraceID | None = None,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> dict[str, Any]:
    await workflow_for(context, workflow_id)
    window(start, end, context)
    if cursor and not cursor.startswith("OBS#"):
        raise ApiError(422, "INVALID_CURSOR", "invalid observation cursor")
    rows, next_cursor = await ObservationStore(context.repo.table).history(
        workflow_id, since=start, until=end, after=cursor, limit=200
    )
    result = []
    for row in rows:
        if traceId and row.get("traceId") != traceId.lower():
            continue
        resource = row["resource"]
        result.append(
            {
                "id": row["collectedAt"] + ":" + resource,
                "at": row["collectedAt"],
                "level": "ERROR" if row.get("collectionError") else "INFO",
                "side": "shared",
                "service": "foxtrot-" + MODULES.get(resource, "unknown"),
                "message": resource
                + (
                    " observation unavailable"
                    if row.get("collectionError")
                    else " observation collected"
                ),
                "traceId": row.get("traceId"),
                "spanId": row.get("spanId"),
            }
        )
    return {"state": "OK", "logs": result, "nextCursor": next_cursor}


@router.get("/v1/workflows/{workflow_id}/monitoring-scope")
async def monitoring_scope(workflow_id: str, context: Context, _: CurrentSession) -> dict[str, Any]:
    workflow = await workflow_for(context, workflow_id)
    resources = await context.repo.resources(workflow_id)

    def endpoint(plan: dict[str, Any], side: str) -> dict[str, Any]:
        direct = plan.get(side)
        # Explicit public endpoint fields only; plans may contain credential references.
        if isinstance(direct, dict):
            return {
                key: direct[key]
                for key in ("provider", "host", "port", "database", "resourceRef")
                if isinstance(direct.get(key), (str, int))
            }
        ref = plan.get(side + "Ref")
        return {"resourceRef": ref} if isinstance(ref, str) else {}

    return {
        "workflowId": workflow.id,
        "applicationId": workflow.application,
        "workloadScope": context.seed.scope,
        "sourceEnvironment": workflow.source_env,
        "targetEnvironment": workflow.target_env,
        "actors": {
            side: list(context.seed.environments[env].actors)
            for side, env in (("source", workflow.source_env), ("target", workflow.target_env))
            if env in context.seed.environments
        },
        "resources": [
            {
                "kind": row.kind.value,
                "migrationId": row.migration_id,
                "bindingRef": row.binding_ref,
                "source": endpoint(row.plan or {}, "source"),
                "target": endpoint(row.plan or {}, "target"),
            }
            for row in resources
        ],
    }
