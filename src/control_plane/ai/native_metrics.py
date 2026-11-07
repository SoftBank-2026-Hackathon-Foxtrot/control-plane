from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
from datetime import datetime
from typing import Any

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from control_plane.ai.allocation import allocation
from control_plane.ai.context import DeploymentScope
from control_plane.ai.schemas import AnalysisRequest

RESOURCE_KEYS = ("cpuP95Percent", "memoryPeakMiB")
ECS_ID = re.compile(
    r"arn:(aws(?:-us-gov|-cn)?):ecs:([a-z0-9-]+):(\d{12}):service/([\w-]+)/([\w-]+)"
)
RUN_ID = re.compile(r"projects/([a-z0-9-]+)/locations/([a-z0-9-]+)/services/([a-z0-9-]+)")


def empty(state: str, provider: str) -> dict[str, Any]:
    return {
        "metrics": dict.fromkeys(RESOURCE_KEYS),
        "quality": {
            key: {
                "state": state,
                "source": provider,
                "samples": 0,
                "temporalCoveragePercent": 0.0,
                "aggregation": "NONE",
            }
            for key in RESOURCE_KEYS
        },
        "resourceUsage": None,
    }


def timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must have a timezone")
        return value.timestamp()
    return timestamp(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def summarize(
    scope: DeploymentScope,
    request: AnalysisRequest,
    points: dict[str, dict[int, float]],
    provider: str,
    allocated: dict[str, Any],
    *,
    memory_scale: float,
    cpu_aggregation: str,
    memory_aggregation: str,
) -> dict[str, Any]:
    result = empty("NO_DATA", provider)
    start, end = request.time_range.start.timestamp(), request.time_range.end.timestamp()
    expected = max(1, math.ceil((end - start) / 60))
    applied = timestamp(scope.snapshot["appliedAt"])
    values: dict[str, list[float]] = {}
    for key in RESOURCE_KEYS:
        valid = {
            at // 60: value
            for at, value in points[key].items()
            if max(start, applied) <= at < end
            and math.isfinite(value)
            and value >= 0
            and (key != "cpuP95Percent" or value <= 100)
        }
        values[key] = sorted(valid.values())
        if valid:
            value = (
                values[key][math.ceil(len(valid) * 0.95) - 1]
                if key == "cpuP95Percent"
                else max(valid.values()) * memory_scale
            )
            result["metrics"][key] = value
            result["quality"][key].update(
                state="OK",
                samples=len(valid),
                temporalCoveragePercent=min(100.0, 100 * len(valid) / expected),
                aggregation=cpu_aggregation if key == "cpuP95Percent" else memory_aggregation,
                sampleMeaning="one-minute cloud metric aggregates, not request samples",
            )
    cpu, memory = (result["metrics"][key] for key in RESOURCE_KEYS)
    if cpu is not None and memory is not None:
        # Cloud Run utilization is instance-wide; ECS Maximum is the busiest task.
        cpu_allocation = allocated["totalCpuCores"]
        result["resourceUsage"] = {
            "source": provider,
            "allocation": allocated,
            "usageScope": "TASK" if scope.cloud == "aws" else "INSTANCE",
            "cpuP95Cores": cpu_allocation * cpu / 100,
            "containerCpuUpperBoundCores": (
                min(cpu_allocation * cpu / 100, allocated["cpuCores"])
                if scope.cloud == "gcp"
                else cpu_allocation * cpu / 100
            ),
            "memoryPeakMiB": memory,
            "crossCloudMethod": "CONSERVATIVE_WHOLE_WORKLOAD_BOUND",
            "notice": "CPU is P95 of minute aggregates; memory is a sampled high-water estimate. "
            "Container CPU bottlenecks cannot be located from instance/task totals. "
            "Do not divide totals by the container count to size a Cloud Run container.",
        }
    return result


class NativeMetricsReader:
    """Exact owned-resource reads only. SDK credentials and clients are initialized lazily."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self._clients: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self._session: Any = None
        self._credentials: Any = None

    def _client(self, service: str, region: str) -> Any:
        with self._lock:
            key = service, region
            if key not in self._clients:
                if self._session is None:
                    self._session = boto3.Session()
                self._clients[key] = self._session.client(
                    service,
                    region_name=region,
                    config=Config(
                        connect_timeout=3,
                        read_timeout=5,
                        retries={"total_max_attempts": 2, "mode": "standard"},
                    ),
                )
            return self._clients[key]

    async def collect(self, scope: DeploymentScope, request: AnalysisRequest) -> dict[str, Any]:
        provider = {"aws": "CLOUDWATCH_ECS", "gcp": "CLOUD_MONITORING_RUN"}.get(
            scope.cloud, "UNSUPPORTED"
        )
        identity = ((scope.snapshot.get("resources") or {}).get("compute") or {}).get("id", "")
        match = (ECS_ID if scope.cloud == "aws" else RUN_ID).fullmatch(str(identity))
        if scope.cloud not in {"aws", "gcp"} or match is None:
            return empty("RESOURCE_ID_UNAVAILABLE", provider)
        try:
            allocated = allocation(
                scope.cloud, scope.snapshot["app"], bool(scope.snapshot.get("hasTunnelContainer"))
            )
            timestamp(scope.snapshot["appliedAt"])
            if scope.cloud == "aws":
                return await asyncio.to_thread(self._aws, scope, request, match, allocated)
            return await self._gcp(scope, request, match, allocated)
        except (BotoCoreError, ClientError, httpx.HTTPError, ValueError, KeyError, TypeError):
            # Provider response bodies and credentials must never reach model/API output.
            return empty("UNAVAILABLE", provider)

    def _aws(
        self,
        scope: DeploymentScope,
        request: AnalysisRequest,
        identity: re.Match[str],
        allocated: dict[str, Any],
    ) -> dict[str, Any]:
        partition, region, account, cluster, service = identity.groups()
        ecs = self._client("ecs", region)
        arn = identity.group()
        data = ecs.describe_services(cluster=cluster, services=[arn])
        services = data.get("services", [])
        if data.get("failures") or len(services) != 1 or services[0].get("serviceArn") != arn:
            return empty("RESOURCE_ID_UNAVAILABLE", "CLOUDWATCH_ECS")
        live = services[0]
        task = live.get("taskDefinition", "")
        if (
            len(live.get("deployments", [])) != 1
            or live.get("pendingCount", 0)
            or not task.startswith(f"arn:{partition}:ecs:{region}:{account}:task-definition/")
        ):
            return empty("ALLOCATION_UNCONFIRMED", "CLOUDWATCH_ECS")
        definition = ecs.describe_task_definition(taskDefinition=task)["taskDefinition"]
        containers = definition["containerDefinitions"]
        expected_names = set(allocated["applicationContainers"])
        if allocated["hasTunnelContainer"]:
            expected_names.add("cloudflared")
        if (
            float(definition["cpu"]) != allocated["cpuCores"] * 1024
            or int(definition["memory"]) != allocated["memoryMiB"]
            or {row["name"] for row in containers} != expected_names
            or any(
                row.get("cpu") or row.get("memory") or row.get("memoryReservation")
                for row in containers
            )
        ):
            return empty("ALLOCATION_UNCONFIRMED", "CLOUDWATCH_ECS")
        queries = [
            {
                "Id": name,
                "AccountId": account,
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/ECS",
                        "MetricName": metric,
                        "Dimensions": [
                            {"Name": "ClusterName", "Value": cluster},
                            {"Name": "ServiceName", "Value": service},
                        ],
                    },
                    "Period": 60,
                    "Stat": "Maximum",
                },
            }
            for name, metric in (("cpu", "CPUUtilization"), ("mem", "MemoryUtilization"))
        ]
        points: dict[str, dict[int, float]] = {key: {} for key in RESOURCE_KEYS}
        pages = (
            self._client("cloudwatch", region)
            .get_paginator("get_metric_data")
            .paginate(
                MetricDataQueries=queries,
                StartTime=request.time_range.start,
                EndTime=request.time_range.end,
                ScanBy="TimestampAscending",
                PaginationConfig={"MaxItems": 6000},
            )
        )
        for page in pages:
            for row in page.get("MetricDataResults", []):
                if row.get("Id") not in {"cpu", "mem"} or row.get("StatusCode") != "Complete":
                    return empty("UNAVAILABLE", "CLOUDWATCH_ECS")
                key = RESOURCE_KEYS[0 if row["Id"] == "cpu" else 1]
                for at, value in zip(row.get("Timestamps", []), row.get("Values", []), strict=True):
                    points[key][int(timestamp(at))] = float(value)
        return summarize(
            scope,
            request,
            points,
            "CLOUDWATCH_ECS",
            allocated,
            memory_scale=allocated["memoryMiB"] / 100,
            cpu_aggregation="P95_OF_MINUTE_TASK_MAX_PERCENT",
            memory_aggregation="MAX_OF_MINUTE_TASK_MAX_MIB",
        )

    def _google_token(self) -> str:
        import google.auth
        from google.auth.transport.requests import Request
        from google.oauth2.service_account import Credentials

        with self._lock:
            if self._credentials is None:
                scopes = ["https://www.googleapis.com/auth/cloud-platform"]
                raw = os.environ.get("GOOGLE_CREDENTIALS")
                self._credentials = (
                    Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
                        json.loads(raw), scopes=scopes
                    )
                    if raw
                    else google.auth.default(scopes=scopes)[0]
                )
            if not self._credentials.valid:
                self._credentials.refresh(lambda **kwargs: Request()(**{**kwargs, "timeout": 5}))
            return str(self._credentials.token)

    async def _gcp(
        self,
        scope: DeploymentScope,
        request: AnalysisRequest,
        identity: re.Match[str],
        allocated: dict[str, Any],
    ) -> dict[str, Any]:
        from google.auth.exceptions import GoogleAuthError

        try:
            token = await asyncio.to_thread(self._google_token)
        except GoogleAuthError:
            return empty("UNAVAILABLE", "CLOUD_MONITORING_RUN")
        headers = {"Authorization": "Bearer " + token}
        project, location, service = identity.groups()
        response = await self.http.get(
            "https://run.googleapis.com/v2/" + identity.group(), headers=headers, timeout=5
        )
        response.raise_for_status()
        live = response.json()
        revision = live.get("latestReadyRevision", "").rsplit("/", 1)[-1]
        traffic = live.get("trafficStatuses", [])
        if (
            live.get("reconciling")
            or live.get("name") != identity.group()
            or live.get("latestCreatedRevision") != live.get("latestReadyRevision")
            or not revision
            or len(traffic) != 1
            or traffic[0].get("percent") != 100
            or traffic[0].get("revision", "").rsplit("/", 1)[-1] != revision
        ):
            return empty("ALLOCATION_UNCONFIRMED", "CLOUD_MONITORING_RUN")
        # Check only limits and container identities. Never project env/secrets into AI evidence.
        expected = {
            name: (allocated["cpuCores"], allocated["memoryMiB"])
            for name in allocated["applicationContainers"]
        }
        if allocated["hasTunnelContainer"]:
            expected["cloudflared"] = (1, 256)
        actual = {}
        for row in live["template"]["containers"]:
            limits = row["resources"]["limits"]
            memory = str(limits["memory"])
            scale = 1024 if memory.endswith("Gi") else 1
            actual[row["name"]] = float(limits["cpu"]), float(memory[:-2]) * scale
        if actual != expected:
            return empty("ALLOCATION_UNCONFIRMED", "CLOUD_MONITORING_RUN")
        base_filter = (
            'resource.type="cloud_run_revision"'
            f' AND resource.labels.project_id="{project}"'
            f' AND resource.labels.location="{location}"'
            f' AND resource.labels.service_name="{service}"'
            f' AND resource.labels.revision_name="{revision}"'
        )
        points: dict[str, dict[int, float]] = {key: {} for key in RESOURCE_KEYS}
        for key, metric, aligner in (
            (RESOURCE_KEYS[0], "container/cpu/utilizations", "ALIGN_PERCENTILE_95"),
            (RESOURCE_KEYS[1], "container/memory/usage", "ALIGN_PERCENTILE_99"),
        ):
            params = {
                "filter": base_filter + f' AND metric.type="run.googleapis.com/{metric}"',
                "interval.startTime": request.time_range.start.isoformat(),
                "interval.endTime": request.time_range.end.isoformat(),
                "aggregation.alignmentPeriod": "60s",
                "aggregation.perSeriesAligner": aligner,
                "view": "FULL",
                "pageSize": "10000",
            }
            by_container: dict[str, dict[int, float]] = {}
            for _ in range(10):
                response = await self.http.get(
                    f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
                    headers=headers,
                    params=params,
                    timeout=5,
                )
                response.raise_for_status()
                data = response.json()
                for series in data.get("timeSeries", []):
                    resource = series.get("resource", {})
                    resource_labels = resource.get("labels", {})
                    if (
                        resource.get("type") != "cloud_run_revision"
                        or any(
                            resource_labels.get(key) != value
                            for key, value in {
                                "project_id": project,
                                "location": location,
                                "service_name": service,
                                "revision_name": revision,
                            }.items()
                        )
                        or series.get("metric", {}).get("type") != f"run.googleapis.com/{metric}"
                    ):
                        continue
                    name = series.get("metric", {}).get("labels", {}).get("container_name", "")
                    if key == RESOURCE_KEYS[1] and name not in expected:
                        return empty("CONTAINER_SCOPE_UNCONFIRMED", "CLOUD_MONITORING_RUN")
                    dest = by_container.setdefault(name, {})
                    for point in series.get("points", []):
                        at = int(timestamp(point["interval"]["endTime"]))
                        value = float(point["value"]["doubleValue"])
                        if math.isfinite(value) and value >= 0:
                            dest[at] = max(dest.get(at, 0), value)
                next_page = data.get("nextPageToken")
                if not next_page:
                    break
                params["pageToken"] = next_page
            else:
                return empty("UNAVAILABLE", "CLOUD_MONITORING_RUN")
            if key == RESOURCE_KEYS[0]:
                for series in by_container.values():
                    for at, value in series.items():
                        points[key][at] = max(points[key].get(at, 0), value * 100)
            elif set(by_container) == set(expected):
                # A minute with a missing worker is not evidence of zero worker memory.
                for at in set.intersection(*(set(s) for s in by_container.values())):
                    points[key][at] = sum(s[at] for s in by_container.values()) / 1048576
        result = summarize(
            scope,
            request,
            points,
            "CLOUD_MONITORING_RUN",
            allocated,
            memory_scale=1,
            cpu_aggregation="P95_OF_MINUTE_INSTANCE_P95_PERCENT",
            memory_aggregation="MAX_OF_SUM_CONTAINER_MINUTE_P99_MIB",
        )
        if result["resourceUsage"]:
            valid_memory = [
                value / 1048576
                for name, series in by_container.items()
                if name in allocated["applicationContainers"]
                for at, value in series.items()
                if max(request.time_range.start.timestamp(), timestamp(scope.snapshot["appliedAt"]))
                <= at
                < request.time_range.end.timestamp()
            ]
            result["resourceUsage"]["containerMemoryPeakMiB"] = max(valid_memory)
        return result
