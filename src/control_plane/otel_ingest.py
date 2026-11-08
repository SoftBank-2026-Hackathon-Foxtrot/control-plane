"""Collector-only OTLP/HTTP receiver for projected migration snapshot logs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)

from control_plane.api.dependencies import Context, observation_caller
from control_plane.errors import ApiError
from control_plane.observation_projection import timestamp
from control_plane.observations import ObservationStore
from control_plane.telemetry_contract import snapshot_projection

router = APIRouter()
MODULES = {
    "DATABASE": "database",
    "REDIS": "redis",
    "OBJECT_STORAGE": "object",
    "INFRA": "iac",
    "COMPUTE": "iac",
    "APP": "control-plane",
    "PUBLIC": "control-plane",
    "TRAFFIC": "traffic",
}


def attributes(items: Any) -> dict[str, str]:
    return {
        item.key: item.value.string_value
        for item in items
        if item.value.WhichOneof("value") == "string_value"
    }


@router.post("/internal/otlp/v1/logs", dependencies=[Depends(observation_caller)])
async def ingest(request: Request, context: Context) -> Response:
    if request.headers.get("content-type", "").split(";")[0] != "application/x-protobuf":
        raise ApiError(415, "OTLP_PROTOBUF_REQUIRED", "use OTLP HTTP/protobuf")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise ApiError(415, "OTLP_ENCODING", "Collector exporter must use compression: none")
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 1048576:
            raise ApiError(413, "OTLP_TOO_LARGE", "maximum request is 1 MiB")
    batch = ExportLogsServiceRequest()
    try:
        batch.ParseFromString(bytes(body))
    except DecodeError as exc:
        raise ApiError(400, "OTLP_INVALID", "invalid protobuf") from exc
    count = sum(len(s.log_records) for r in batch.resource_logs for s in r.scope_logs)
    if count > 512:
        raise ApiError(413, "OTLP_TOO_MANY_RECORDS", "maximum batch is 512")
    rejected = 0
    store = ObservationStore(context.repo.table)
    now = context.clock.now()
    for resource in batch.resource_logs:
        attrs = attributes(resource.resource.attributes)
        for scope in resource.scope_logs:
            for record in scope.log_records:
                try:
                    labels = attributes(record.attributes)
                    if labels.get("foxtrot.event") != "snapshot":
                        continue
                    if record.body.WhichOneof("value") != "string_value":
                        raise ValueError
                    if len(record.body.string_value.encode()) > 32768:
                        raise ValueError
                    envelope = json.loads(record.body.string_value)
                    kind = envelope["resource"]
                    if (
                        envelope["schemaVersion"] != 1
                        or attrs.get("foxtrot.contract.version") != "1"
                        or MODULES.get(kind) != attrs.get("foxtrot.module")
                    ):
                        raise ValueError
                    data = snapshot_projection(kind, envelope["data"])
                    wid = data["workflowId"]
                    if (
                        not isinstance(wid, str)
                        or not 0 < len(wid) <= 128
                        or labels.get("workflow.id") != wid
                    ):
                        raise ValueError
                    workflow = await context.repo.workflow(wid)
                    if workflow is None:
                        raise ValueError
                    if kind in {"DATABASE", "REDIS", "OBJECT_STORAGE"}:
                        states = await context.repo.resources(wid)
                        if not any(
                            s.kind.value == kind
                            and s.migration_id == data.get("migrationId")
                            and s.migration_id
                            for s in states
                        ):
                            raise ValueError
                    at = datetime.fromtimestamp(record.time_unix_nano / 1e9, UTC)
                    source = timestamp((data.get("observed") or {}).get("observedAt"))
                    if not now - timedelta(hours=24) <= at <= now + timedelta(seconds=5):
                        raise ValueError
                    error = envelope.get("collectionError")
                    if error is not None:
                        error = "MODULE_OBSERVATION_UNAVAILABLE"
                    elif source is None or source > now + timedelta(seconds=5):
                        raise ValueError
                    await store.save(
                        wid,
                        kind,
                        None if error else data,
                        at,
                        error=error,
                        expires_at=int((workflow.finished_at + timedelta(days=7)).timestamp())
                        if workflow.finished_at
                        else None,
                        source="OTEL",
                        trace_id=record.trace_id.hex() if len(record.trace_id) == 16 else None,
                        span_id=record.span_id.hex() if len(record.span_id) == 8 else None,
                    )
                except (ValueError, KeyError, TypeError, OverflowError, AttributeError):
                    rejected += 1
                except Exception as exc:
                    raise ApiError(503, "OTLP_STORAGE_UNAVAILABLE", "retry this batch") from exc
    result = ExportLogsServiceResponse()
    if rejected:
        result.partial_success.rejected_log_records = rejected
        result.partial_success.error_message = "invalid or unregistered migration snapshots"
    return Response(result.SerializeToString(), media_type="application/x-protobuf")
