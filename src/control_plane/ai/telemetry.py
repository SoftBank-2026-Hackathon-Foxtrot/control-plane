from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any

import httpx

from control_plane.ai.context import DeploymentScope
from control_plane.ai.native_metrics import NativeMetricsReader
from control_plane.ai.runtime_metrics import RuntimeMetricsReader, labels
from control_plane.ai.schemas import AnalysisRequest
from control_plane.config import Settings
from control_plane.telemetry_query import read_backend

FOCUS = {
    "RESOURCES": ("cpuP95Percent", "memoryPeakMiB"),
    "PERFORMANCE": ("requestP95Ms", "requestRatePerSecond", "databaseP95Ms", "redisP95Ms"),
    "ERRORS": ("errorRatePercent",),
}


class TelemetryReader:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        native: NativeMetricsReader | None = None,
    ) -> None:
        self.settings, self.http = settings, http
        self.native = native or RuntimeMetricsReader(settings, http)

    async def collect(self, scope: DeploymentScope, request: AnalysisRequest) -> dict[str, Any]:
        settings = self.settings
        metric_labels = labels(scope)
        start, end = request.time_range.start.timestamp(), request.time_range.end.timestamp()
        duration = int(end - start)
        window = f"[{duration}s]"

        def histogram(metric: str, extra: str = "") -> str:
            return (
                f"histogram_quantile(0.95,sum by(le)(increase("
                f"{metric}{{{metric_labels}{extra}}}{window}))) * 1000"
            )

        counts = "http_server_request_duration_seconds_count"
        errors = f'{counts}{{{metric_labels},http_response_status_code=~"5.."}}{window}'
        total = f'{counts}{{{metric_labels},http_response_status_code=~"[1-5].."}}{window}'

        # These names form the collector contract. Missing instrumentation yields null, not zero.
        queries = {
            "requestP95Ms": histogram("http_server_request_duration_seconds_bucket"),
            "requestRatePerSecond": (
                f"sum(increase({counts}{{{metric_labels}}}{window})) / {duration}"
            ),
            "errorRatePercent": (
                f"100 * ((sum(increase({errors})) or vector(0)) / sum(increase({total})))"
            ),
            "databaseP95Ms": histogram(
                "db_client_operation_duration_seconds_bucket", ',db_system_name="postgresql"'
            ),
            "redisP95Ms": histogram(
                "db_client_operation_duration_seconds_bucket", ',db_system_name="redis"'
            ),
        }
        selected = list(dict.fromkeys(key for focus in request.focus for key in FOCUS[focus]))

        async def metric(key: str) -> tuple[str, float | None, dict[str, Any]]:
            state, data = await read_backend(
                self.http,
                settings.prometheus_url,
                "/api/v1/query",
                {"query": queries[key], "time": end},
            )
            values: list[tuple[float, float]] = []
            try:
                if state == "OK" and data.get("status") != "success":
                    state = "UNAVAILABLE"
                for row in (data.get("data") or {}).get("result", [])[:4]:
                    for point in [row.get("value")]:
                        at, value = float(point[0]), float(point[1])
                        if (
                            math.isfinite(at)
                            and math.isfinite(value)
                            and start <= at <= end
                            and value >= 0
                            and (key != "errorRatePercent" or value <= 100)
                        ):
                            values.append((at, value))
            except (KeyError, TypeError, ValueError, OverflowError):
                state, values = "UNAVAILABLE", []
            result = None
            if state == "OK" and values:
                ordered = sorted(value for _, value in values)
                result = max(ordered)
            return (
                key,
                result,
                {
                    "state": state
                    if state != "OK"
                    else ("OK" if result is not None else "NO_DATA"),
                    "samples": len(values),
                    "sampleMeaning": "time-series query points, not request count",
                    "temporalCoveragePercent": None,
                    "aggregation": "WHOLE_PERIOD",
                    "source": "PROMETHEUS_OTEL",
                },
            )

        # Resource usage is adapted from the same container series as the app console.
        supplementary = [key for key in selected if key not in FOCUS["RESOURCES"]]
        readings = await asyncio.gather(*(metric(key) for key in supplementary))
        metrics: dict[str, float | None] = {key: None for keys in FOCUS.values() for key in keys}
        quality = {}
        for key, value, evidence in readings:
            metrics[key], quality[key] = value, evidence
        native = await self.native.collect(scope, request) if "RESOURCES" in request.focus else None
        if native:
            metrics.update(native["metrics"])
            quality.update(native["quality"])
        missing = [key for key in selected if metrics[key] is None]
        partial = missing or any(
            row["state"] != "OK"
            or (row["temporalCoveragePercent"] is not None and row["temporalCoveragePercent"] < 80)
            for row in quality.values()
        )
        state, data = await read_backend(
            self.http,
            settings.tempo_url,
            "/api/search",
            {
                "q": "{ resource.application.id = "
                + json.dumps(scope.application)
                + " && resource.deployment.environment.name = "
                + json.dumps(scope.cloud)
                + " && resource.foxtrot.owner.id = "
                + json.dumps(scope.owner_id)
                + " && resource.foxtrot.deployment.id = "
                + json.dumps(scope.deployment_id)
                + ' && resource.foxtrot.workload.kind = "USER_APPLICATION"'
                + " }",
                "limit": 10,
                "start": int(start),
                "end": int(end),
            },
        )
        traces = []
        for row in data.get("traces", [])[:10] if isinstance(data.get("traces"), list) else []:
            if not isinstance(row, dict) or not re.fullmatch(
                r"[a-fA-F0-9]{16,32}", str(row.get("traceID", ""))
            ):
                continue
            duration_ms = row.get("durationMs")
            if (
                not isinstance(duration_ms, (int, float))
                or isinstance(duration_ms, bool)
                or not math.isfinite(duration_ms)
                or duration_ms < 0
            ):
                continue
            traces.append({"traceID": row["traceID"], "durationMs": duration_ms})
        return {
            "status": "INSUFFICIENT_DATA"
            if len(missing) == len(selected)
            else ("PARTIAL" if partial else "COMPLETED"),
            "metrics": metrics,
            "resourceUsage": native["resourceUsage"] if native else None,
            "dataQuality": {
                "coveragePercent": round(100 * (len(selected) - len(missing)) / len(selected), 1),
                "coverageMeaning": "available metric kinds, not temporal coverage",
                "missingMetrics": missing,
                "metrics": quality,
            },
            "traces": traces,
            "tracesState": state,
        }
