"""Read-only, scoped host metrics for the application console."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Query
from pydantic import BeforeValidator

from control_plane.api.dependencies import Context, CurrentSession
from control_plane.errors import ApiError
from control_plane.telemetry_query import read_backend

router = APIRouter(prefix="/v1/applications/{application_id}/system-metrics")
Category = Literal["cpu", "memory", "disk", "network"]
Window = Annotated[Literal[15, 60, 360], BeforeValidator(int)]


def scope(context: Context, application_id: str, environment: str) -> str:
    if application_id != context.seed.application or environment not in context.seed.environments:
        raise ApiError(404, "NOT_FOUND", "unknown application or environment")
    return (
        f"application_id={json.dumps(application_id)},"
        f'deployment_environment_name={json.dumps(environment)},service_name="foxtrot-app-host"'
    )


def queries(labels: str, host: str, category: Category) -> dict[str, tuple[str, str]]:
    labels += f",host_name={json.dumps(host)}"

    def fresh(metric: str, extra: str = "") -> str:
        selector = f"{metric}{{{labels}{extra}}}"
        return f"({selector} and (timestamp({selector}) > time() - 30))"

    if category == "cpu":
        metric = "system_cpu_time_seconds_total{" + labels + ',state="idle"}'
        return {
            "cpu": (
                f"100 * (1 - avg(rate({metric}[2m]) and (timestamp({metric}) > time() - 30)))",
                "%",
            )
        }
    if category == "memory":
        available = f"max({fresh('system_linux_memory_available_bytes')})"
        limit = f"max({fresh('system_memory_limit_bytes')})"
        return {
            "memory": (f"100 * (1 - ({available}) / ({limit}))", "%"),
            "memory_bytes": (f"({limit}) - ({available})", "By"),
        }
    if category == "disk":
        used = "sum(" + fresh("system_filesystem_usage_bytes", ',state="used",mountpoint="/"') + ")"
        total = "sum(" + fresh("system_filesystem_usage_bytes", ',mountpoint="/"') + ")"
        result = {"disk_usage": (f"100 * ({used}) / ({total})", "%")}
        metric, directions = "system_disk_io_bytes_total", ("read", "write")
    else:
        result = {}
        metric, directions = "system_network_io_bytes_total", ("receive", "transmit")
    for direction in directions:
        selector = f'{metric}{{{labels},direction="{direction}"}}'
        result[f"{category}_{direction}"] = (
            f"sum(rate({selector}[2m]) and (timestamp({selector}) > time() - 30))",
            "By/s",
        )
    return result


def points(payload: dict[str, Any], start: int, end: int, step: int) -> list[list[float | None]]:
    data = payload.get("data", {})
    if payload.get("status") != "success" or data.get("resultType") != "matrix":
        raise ValueError("invalid Prometheus result")
    rows = data.get("result", [])
    if len(rows) > 1:
        raise ValueError("expected an aggregate")
    samples = {}
    for at, raw in rows[0].get("values", [])[:361] if rows else []:
        stamp, value = float(at), float(raw)
        if not math.isfinite(stamp):
            raise ValueError("invalid timestamp")
        samples[stamp] = value if math.isfinite(value) else None
    return [[at, samples.get(at)] for at in range(start, end + 1, step)]


@router.get("")
async def series(
    application_id: str,
    context: Context,
    _: CurrentSession,
    environment: str = Query(max_length=64),
    host: str = Query(min_length=1, max_length=128),
    category: Category = "cpu",
    minutes: Window = 60,
) -> dict[str, Any]:
    labels = scope(context, application_id, environment)
    end = int(context.clock.now().timestamp())
    start, step = end - minutes * 60, max(10, minutes // 3)
    definitions = queries(labels, host, category)
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as http:
        responses = await asyncio.gather(
            *[
                read_backend(
                    http,
                    context.settings.prometheus_url,
                    "/api/v1/query_range",
                    {"query": query, "start": start, "end": end, "step": step},
                )
                for query, _unit in definitions.values()
            ]
        )
    rows = []
    for (name, (_query, unit)), (state, payload) in zip(
        definitions.items(), responses, strict=True
    ):
        values: list[list[float | None]] = []
        if state == "OK":
            try:
                values = points(payload, start, end, step)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
                state = "UNAVAILABLE"
        rows.append({"id": name, "unit": unit, "state": state, "points": values})
    return {"from": start, "to": end, "step": step, "series": rows}


@router.get("/hosts")
async def hosts(
    application_id: str,
    context: Context,
    _: CurrentSession,
    environment: str = Query(max_length=64),
) -> dict[str, Any]:
    labels = scope(context, application_id, environment)
    selector = f"system_cpu_time_seconds_total{{{labels}}}"
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as http:
        state, payload = await read_backend(
            http,
            context.settings.prometheus_url,
            "/api/v1/query",
            {"query": f"max by (host_name) (timestamp({selector}))"},
        )
    rows = []
    try:
        if state == "OK":
            if (
                payload.get("status") != "success"
                or payload.get("data", {}).get("resultType") != "vector"
            ):
                raise ValueError("invalid backend result")
            for row in payload["data"]["result"][:64]:
                name = row["metric"].get("host_name")
                at = float(row["value"][1])
                if isinstance(name, str) and 0 < len(name) <= 128 and math.isfinite(at):
                    rows.append(
                        {
                            "name": name,
                            "lastSeen": at,
                            "stale": context.clock.now().timestamp() - at > 30,
                        }
                    )
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return {"state": "UNAVAILABLE", "hosts": []}
    return {"state": state, "hosts": sorted(rows, key=lambda r: str(r["name"]))}
