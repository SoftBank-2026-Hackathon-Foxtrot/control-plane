"""Adapt the app console's container metrics to the existing AI resource contract."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import httpx

from control_plane.ai.allocation import allocation
from control_plane.ai.context import DeploymentScope
from control_plane.ai.native_metrics import empty, timestamp
from control_plane.ai.schemas import AnalysisRequest
from control_plane.config import Settings
from control_plane.telemetry_query import read_backend

SOURCE = "PROMETHEUS_APP_RUNTIME"
STEP = 60


def labels(scope: DeploymentScope) -> str:
    # Match system-metrics' full identity, including deployment.id (not foxtrot.deployment.id).
    return (
        f"application_id={json.dumps(scope.application)},"
        f"foxtrot_owner_id={json.dumps(scope.owner_id)},"
        f"deployment_id={json.dumps(scope.deployment_id)},"
        f"deployment_environment_name={json.dumps(scope.cloud)}"
    )


class RuntimeMetricsReader:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings, self.http = settings, http

    async def collect(self, scope: DeploymentScope, request: AnalysisRequest) -> dict[str, Any]:
        result = empty("NO_DATA", SOURCE)
        try:
            allocated = allocation(
                scope.cloud, scope.snapshot["app"], bool(scope.snapshot.get("hasTunnelContainer"))
            )
            applied = timestamp(scope.snapshot["appliedAt"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return empty("ALLOCATION_UNCONFIRMED", SOURCE)
        start, end = request.time_range.start.timestamp(), request.time_range.end.timestamp()
        first = math.ceil(max(start, applied) / STEP) * STEP
        last = math.ceil(end / STEP) * STEP - STEP
        if first > last:
            return result
        selector = (
            labels(scope) + ',service_name="foxtrot-app-runtime",measurement_scope="container"'
        )

        def query(name: str) -> str:
            metric = f"foxtrot_runtime_{name}{{{selector}}}"
            value = f"rate({metric}[2m])" if name == "cpu_seconds_total" else metric
            return (
                "max by (service_instance_id, app_role) ("
                f"{value} and (timestamp({metric}) > time() - 30))"
            )

        names = ("cpu_seconds_total", "memory_bytes", "cpu_limit", "memory_limit")
        responses = await asyncio.gather(
            *[
                read_backend(
                    self.http,
                    self.settings.prometheus_url,
                    "/api/v1/query_range",
                    {"query": query(name), "start": first, "end": last, "step": STEP},
                )
                for name in names
            ]
        )
        series: dict[str, dict[int, dict[tuple[str, str], float]]] = {}
        try:
            for name, (state, payload) in zip(names, responses, strict=True):
                if state != "OK":
                    return empty(state, SOURCE)
                series[name] = self._points(payload, first, last)
        except (KeyError, ValueError, TypeError, IndexError, AttributeError, OverflowError):
            return empty("UNAVAILABLE", SOURCE)
        workers = len(allocated["applicationContainers"]) - 1
        cpu_points, memory_points, container_cpu, container_memory = [], [], [], []
        allocation_confirmed = True
        for at in range(first, last + 1, STEP):
            cpu = series["cpu_seconds_total"].get(at, {})
            memory = series["memory_bytes"].get(at, {})
            # Require both usage signals for each exporter; a missing worker is not idle capacity.
            instances = cpu.keys() & memory.keys()
            roles = {
                role: [key for key in instances if key[1] == role] for role in ("api", "worker")
            }
            if not roles["api"] or len(roles["worker"]) < workers:
                continue
            used = [*roles["api"], *(roles["worker"] if workers else [])]
            for name, expected in (
                ("cpu_limit", allocated["cpuCores"]),
                ("memory_limit", allocated["memoryMiB"] * 1024**2),
            ):
                for key in used:
                    limit = series[name].get(at, {}).get(key)
                    # Some serverless kernels omit quotas. Percentages then use the applied spec.
                    if limit is not None and (
                        limit <= 0
                        or limit > expected * 1.01
                        or (
                            scope.cloud == "gcp" and not math.isclose(limit, expected, rel_tol=0.01)
                        )
                    ):
                        allocation_confirmed = False
            # The busiest API plus N copies of the busiest worker bounds one workload.
            # Replicas and restarted exporters are never summed as extra app containers.
            cpu_points.append(self._bound(cpu, roles, workers))
            memory_points.append(self._bound(memory, roles, workers) / 1024**2)
            container_cpu.append(max(cpu[key] for key in used))
            container_memory.append(max(memory[key] for key in used) / 1024**2)
        if not cpu_points:
            return result
        cpu_cores = self._p95(cpu_points)
        memory_mib = max(memory_points)
        cpu_bound, memory_bound = cpu_cores, memory_mib
        # Runtime exporters cover app containers. Preserve a bound for an unmeasured tunnel.
        if allocated["hasTunnelContainer"]:
            if scope.cloud == "aws":
                cpu_bound = max(cpu_bound, allocated["cpuCores"])
                memory_bound = max(memory_bound, allocated["memoryMiB"])
            else:
                cpu_bound += 1
                memory_bound += 256
        coverage = min(100.0, 100 * len(cpu_points) / max(1, math.ceil((end - start) / STEP)))
        result["metrics"].update(
            cpuP95Percent=100 * min(cpu_cores / allocated["totalCpuCores"], 1),
            memoryPeakMiB=memory_mib,
        )
        for key in result["quality"]:
            result["quality"][key].update(
                state="OK" if allocation_confirmed else "ALLOCATION_UNCONFIRMED",
                samples=len(cpu_points),
                temporalCoveragePercent=coverage,
                aggregation="P95_ROLE_BOUND" if key == "cpuP95Percent" else "MAX_ROLE_BOUND",
                sampleMeaning="one-minute evaluations of fresh container metrics",
            )
        if allocation_confirmed:
            result["resourceUsage"] = {
                "source": SOURCE,
                "allocation": allocated,
                "allocationSource": "LAST_SUCCESSFUL_APPLY_CHECKED_AGAINST_AVAILABLE_LIMITS",
                "usageScope": "CONSERVATIVE_WORKLOAD_BOUND",
                "cpuP95Cores": cpu_bound,
                "memoryPeakMiB": memory_bound,
                "containerCpuUpperBoundCores": self._p95(container_cpu),
                "containerMemoryPeakMiB": max(container_memory),
                "crossCloudMethod": "CONSERVATIVE_WHOLE_WORKLOAD_BOUND",
                "notice": "Busiest API plus configured worker count times busiest worker; "
                "replicas are not added together. Percentages use the last applied allocation. "
                "Unmeasured tunnel capacity is reserved conservatively. "
                "P95 uses minute evaluations, not instantaneous or request samples.",
            }
        return result

    @staticmethod
    def _p95(values: list[float]) -> float:
        return sorted(values)[math.ceil(len(values) * 0.95) - 1]

    @staticmethod
    def _bound(
        values: dict[tuple[str, str], float], roles: dict[str, list[tuple[str, str]]], workers: int
    ) -> float:
        return max(values[key] for key in roles["api"]) + workers * max(
            (values[key] for key in roles["worker"]), default=0
        )

    @staticmethod
    def _points(
        payload: dict[str, Any], first: int, last: int
    ) -> dict[int, dict[tuple[str, str], float]]:
        if payload["status"] != "success" or payload["data"]["resultType"] != "matrix":
            raise ValueError("invalid Prometheus matrix")
        rows = payload["data"]["result"]
        if len(rows) > 256:
            raise ValueError("too many container series")
        points: dict[int, dict[tuple[str, str], float]] = {}
        for row in rows:
            instance, role = row["metric"]["service_instance_id"], row["metric"]["app_role"]
            if not isinstance(instance, str) or not 0 < len(instance) <= 128:
                raise ValueError("invalid instance")
            if role not in {"api", "worker"}:
                continue
            for stamp, raw in row["values"]:
                at, value = float(stamp), float(raw)
                if (
                    not math.isfinite(at)
                    or not math.isfinite(value)
                    or not first <= at <= last
                    or (at - first) % STEP != 0
                    or value < 0
                ):
                    continue
                bucket = points.setdefault(int(at), {})
                key = instance, role
                bucket[key] = max(bucket.get(key, 0), value)
        return points
