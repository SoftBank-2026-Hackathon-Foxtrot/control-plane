"""Deployment-owned container metrics. No application-name or host-wide fallback."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Query, Request
from pydantic import BeforeValidator

from control_plane.api.dependencies import Context, CurrentSession
from control_plane.deployment.api import call, org_id, service
from control_plane.errors import ApiError
from control_plane.telemetry_query import read_backend

router = APIRouter(prefix="/v1/applications/{application_id}/system-metrics")
Category = Literal["cpu", "memory", "disk", "network"]
Window = Annotated[Literal[15, 60, 360], BeforeValidator(int)]


async def scope(
    context: Context, request: Request, session: Any, application_id: str, deployment: str
) -> str:
    if application_id != context.seed.application:
        raise ApiError(404, "NOT_FOUND", "unknown application")
    owner = org_id(str(session.user.id), application_id)
    engine = service(request)
    selected = await call(engine.store.environment, deployment)
    if selected is None or selected["org_id"] != owner:
        raise ApiError(404, "NOT_FOUND", "deployment not found")
    return (
        f"application_id={json.dumps(application_id)},"
        f"foxtrot_owner_id={json.dumps(owner)},"
        f"deployment_id={json.dumps(selected['deployment_id'])},"
        f"deployment_environment_name={json.dumps(selected['cloud'])},"
        'service_name="foxtrot-app-runtime"'
    )


def queries(labels: str, instance: str, category: Category) -> dict[str, tuple[str, str]]:
    labels += f",service_instance_id={json.dumps(instance)}"

    def fresh(metric: str) -> str:
        selector = f"{metric}{{{labels}}}"
        return f"({selector} and (timestamp({selector}) > time() - 30))"

    def value(name: str) -> str:
        return f"max({fresh('foxtrot_runtime_' + name)})"

    def rate(name: str) -> str:
        selector = f"foxtrot_runtime_{name}_total{{{labels}}}"
        return f"max(rate({selector}[2m]) and (timestamp({selector}) > time() - 30))"

    if category == "cpu":
        return {
            "cpu": (f"100 * ({rate('cpu_seconds')}) / ({value('cpu_limit')})", "%"),
            "cpu_cores": (rate("cpu_seconds"), "cores"),
        }
    if category == "memory":
        return {
            "memory": (f"100 * ({value('memory_bytes')}) / ({value('memory_limit')})", "%"),
            "memory_bytes": (value("memory_bytes"), "By"),
        }
    if category == "disk":
        return {
            "disk_read": (rate("disk_read"), "By/s"),
            "disk_write": (rate("disk_write"), "By/s"),
        }
    return {
        "network_receive": (rate("network_receive"), "By/s"),
        "network_transmit": (rate("network_transmit"), "By/s"),
    }


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
    request: Request,
    session: CurrentSession,
    deployment: str = Query(min_length=1, max_length=128),
    instance: str = Query(min_length=1, max_length=128),
    category: Category = "cpu",
    minutes: Window = 60,
) -> dict[str, Any]:
    labels = await scope(context, request, session, application_id, deployment)
    end = int(context.clock.now().timestamp())
    start, step = end - minutes * 60, max(10, minutes // 3)
    definitions = queries(labels, instance, category)
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


@router.get("/instances")
async def instances(
    application_id: str,
    context: Context,
    request: Request,
    session: CurrentSession,
    deployment: str = Query(min_length=1, max_length=128),
) -> dict[str, Any]:
    labels = await scope(context, request, session, application_id, deployment)
    selector = f"foxtrot_runtime_collection{{{labels}}}"
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as http:
        state, payload = await read_backend(
            http,
            context.settings.prometheus_url,
            "/api/v1/query",
            {
                "query": "max by (service_instance_id, app_role, network_scope) "
                f"(timestamp({selector}))"
            },
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
                name = row["metric"].get("service_instance_id")
                at = float(row["value"][1])
                if isinstance(name, str) and 0 < len(name) <= 128 and math.isfinite(at):
                    rows.append(
                        {
                            "id": name,
                            "role": str(row["metric"].get("app_role", "app"))[:128],
                            "networkScope": "app-instance"
                            if row["metric"].get("network_scope") == "app-instance"
                            else "container",
                            "lastSeen": at,
                            "stale": context.clock.now().timestamp() - at > 30,
                        }
                    )
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return {"state": "UNAVAILABLE", "instances": []}
    return {"state": state, "instances": sorted(rows, key=lambda row: str(row["id"]))}
