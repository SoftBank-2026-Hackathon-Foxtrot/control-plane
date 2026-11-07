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
from control_plane.deployment.api import call, org_id
from control_plane.errors import ApiError
from control_plane.telemetry_query import read_backend

router = APIRouter(prefix="/v1/applications/{application_id}/system-metrics")
Category = Literal["cpu", "memory", "disk", "network"]
Window = Annotated[Literal[15, 60, 360], BeforeValidator(int)]


def shared_records(context: Context) -> list[dict[str, Any]]:
    return [
        {
            "id": "shared:" + row.environment_id,
            "org_id": row.owner_id,
            "cloud": row.environment,
            "deployment_id": row.deployment_id,
            "status": "REPORTING",
            "access": "shared",
        }
        for row in context.seed.system_deployments
    ]


def labels_for(application: str, record: dict[str, Any]) -> str:
    return (
        f"application_id={json.dumps(application)},"
        f"foxtrot_owner_id={json.dumps(record['org_id'])},"
        f"deployment_id={json.dumps(record['deployment_id'])},"
        f"deployment_environment_name={json.dumps(record['cloud'])},"
        'service_name="foxtrot-app-runtime"'
    )


@router.get("/deployments")
async def live_deployments(
    application_id: str, context: Context, request: Request, session: CurrentSession
) -> dict[str, Any]:
    if application_id != context.seed.application:
        raise ApiError(404, "NOT_FOUND", "unknown application")
    owner = org_id(str(session.user.id), application_id)
    engine = getattr(request.app.state, "deployment_service", None)
    records = shared_records(context)
    if engine is not None:
        records += [
            {**row, "access": "personal"}
            for row in await call(engine.store.environments)
            if row["org_id"] == owner and row["status"] not in {"DESTROYING", "DESTROYED"}
        ]
    if not records:
        return {"state": "OK" if engine is not None else "NOT_CONFIGURED", "environments": []}
    # Every selector includes the full registered identity. Sharing one deployment
    # does not grant access to other deployments under the same org or app name.
    selectors = [
        "(timestamp(foxtrot_runtime_collection{"
        + labels_for(application_id, row)
        + "}) > time() - 30)"
        for row in records
    ]
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as http:
        state, payload = await read_backend(
            http,
            context.settings.prometheus_url,
            "/api/v1/query",
            {
                "query": "max by (foxtrot_owner_id, deployment_id, deployment_environment_name) ("
                + " or ".join(selectors)
                + ")"
            },
        )
    if state != "OK":
        return {"state": state, "environments": []}
    try:
        if payload["status"] != "success" or payload["data"]["resultType"] != "vector":
            raise ValueError("invalid backend result")
        live = set()
        for row in payload["data"]["result"]:
            observed = float(row["value"][1])
            if (
                not math.isfinite(observed)
                or not 0 <= context.clock.now().timestamp() - observed < 30
            ):
                continue
            metric = row["metric"]
            live.add(
                (
                    metric["foxtrot_owner_id"],
                    metric["deployment_id"],
                    metric["deployment_environment_name"],
                )
            )
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return {"state": "UNAVAILABLE", "environments": []}
    return {
        "state": "OK",
        "environments": [
            {
                "environmentId": row["id"],
                "deploymentId": row["deployment_id"],
                "cloud": row["cloud"],
                "status": row["status"],
                "access": row["access"],
            }
            for row in records
            if (row["org_id"], row["deployment_id"], row["cloud"]) in live
        ],
    }


async def scope(
    context: Context, request: Request, session: Any, application_id: str, deployment: str
) -> str:
    if application_id != context.seed.application:
        raise ApiError(404, "NOT_FOUND", "unknown application")
    selected = next((row for row in shared_records(context) if row["id"] == deployment), None)
    if selected is None:
        engine = getattr(request.app.state, "deployment_service", None)
        if engine is None or deployment.startswith("shared:"):
            raise ApiError(404, "NOT_FOUND", "deployment not found")
        selected = await call(engine.store.environment, deployment)
        owner = org_id(str(session.user.id), application_id)
        if selected is None or selected["org_id"] != owner:
            raise ApiError(404, "NOT_FOUND", "deployment not found")
    return labels_for(application_id, selected)


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
            ordered = sorted(
                payload["data"]["result"], key=lambda row: float(row["value"][1]), reverse=True
            )
            for row in ordered[:64]:
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
