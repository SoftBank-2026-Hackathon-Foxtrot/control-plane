from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import httpx
import pytest
from botocore.stub import ANY, Stubber

from control_plane.ai.allocation import allocation, target_baseline
from control_plane.ai.context import DeploymentScope
from control_plane.ai.native_metrics import NativeMetricsReader
from control_plane.ai.schemas import AnalysisRequest, TimeRange

START = datetime(2026, 11, 7, 8, tzinfo=UTC)
ECS = "arn:aws:ecs:ap-northeast-2:123456789012:service/owned-cluster/owned-app"
TASK = "arn:aws:ecs:ap-northeast-2:123456789012:task-definition/owned-app:3"
RUN = "projects/owned-project/locations/asia-northeast3/services/owned-app"
REVISION = "owned-app-00001"


def scope(cloud: str = "aws") -> DeploymentScope:
    return DeploymentScope(
        "goods",
        "env-" + "1" * 12,
        "owned-app",
        "owner-hash",
        cloud,
        "READY",
        {
            "appliedAt": (START - timedelta(hours=2)).isoformat(),
            "app": {"cpu": 1, "memory_mib": 2048, "worker_count": 2},
            "resources": {"compute": {"id": ECS if cloud == "aws" else RUN}},
        },
    )


def query() -> AnalysisRequest:
    return AnalysisRequest(
        deployment_id="env-" + "1" * 12,
        time_range=TimeRange(start=START, end=START + timedelta(hours=1)),
        focus=["RESOURCES"],
    )


def aws_clients(reader: NativeMetricsReader, *, drift: bool = False) -> tuple[Stubber, Stubber]:
    session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing")
    ecs = session.client("ecs", region_name="ap-northeast-2")
    cw = session.client("cloudwatch", region_name="ap-northeast-2")
    reader._clients.update({("ecs", "ap-northeast-2"): ecs, ("cloudwatch", "ap-northeast-2"): cw})
    ecs_stub, cw_stub = Stubber(ecs), Stubber(cw)
    ecs_stub.add_response(
        "describe_services",
        {
            "services": [
                {
                    "serviceArn": ECS,
                    "taskDefinition": TASK,
                    "pendingCount": 0,
                    "deployments": [{}],
                }
            ]
        },
        {"cluster": "owned-cluster", "services": [ECS]},
    )
    ecs_stub.add_response(
        "describe_task_definition",
        {
            "taskDefinition": {
                "cpu": "2048" if drift else "1024",
                "memory": "2048",
                "containerDefinitions": [
                    {"name": name} for name in ("api", "worker-1", "worker-2")
                ],
            }
        },
        {"taskDefinition": TASK},
    )
    return ecs_stub, cw_stub


def cloudwatch_parameters() -> dict[str, Any]:
    return {
        "MetricDataQueries": [
            {
                "Id": identifier,
                "AccountId": "123456789012",
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/ECS",
                        "MetricName": metric,
                        "Dimensions": [
                            {"Name": "ClusterName", "Value": "owned-cluster"},
                            {"Name": "ServiceName", "Value": "owned-app"},
                        ],
                    },
                    "Period": 60,
                    "Stat": "Maximum",
                },
            }
            for identifier, metric in (("cpu", "CPUUtilization"), ("mem", "MemoryUtilization"))
        ],
        "StartTime": ANY,
        "EndTime": ANY,
        "ScanBy": "TimestampAscending",
    }


@pytest.mark.parametrize("mode", ["complete", "sparse", "denied", "partial", "drift"])
async def test_ecs_exact_account_service_units_and_incomplete_data(mode: str) -> None:
    async with httpx.AsyncClient() as http:
        reader = NativeMetricsReader(http)
        ecs_stub, cw_stub = aws_clients(reader, drift=mode == "drift")
        if mode == "denied":
            cw_stub.add_client_error(
                "get_metric_data",
                "AccessDenied",
                "PRIVATE_PROVIDER_ERROR",
                expected_params=cloudwatch_parameters(),
            )
        elif mode != "drift":
            at = [START + timedelta(minutes=i) for i in range(60 if mode != "sparse" else 2)]
            cw_stub.add_response(
                "get_metric_data",
                {
                    "MetricDataResults": [
                        {
                            "Id": identifier,
                            "StatusCode": "PartialData" if mode == "partial" else "Complete",
                            "Timestamps": at,
                            "Values": [value] * len(at),
                        }
                        for identifier, value in (("cpu", 50.0), ("mem", 25.0))
                    ]
                },
                cloudwatch_parameters(),
            )
        with ecs_stub, cw_stub:
            result = await reader.collect(scope(), query())
            ecs_stub.assert_no_pending_responses()
            cw_stub.assert_no_pending_responses()
    if mode in {"denied", "partial", "drift"}:
        assert result["resourceUsage"] is None
        assert all(value is None for value in result["metrics"].values())
        assert "PRIVATE_PROVIDER_ERROR" not in str(result)
    else:
        assert result["resourceUsage"]["cpuP95Cores"] == 0.5
        assert result["metrics"]["memoryPeakMiB"] == 512
        assert result["resourceUsage"]["allocation"]["scope"] == "TASK"
        assert result["resourceUsage"]["allocation"]["totalCpuCores"] == 1
        coverage = result["quality"]["cpuP95Percent"]["temporalCoveragePercent"]
        assert coverage == 100 if mode == "complete" else coverage < 80


@pytest.mark.parametrize(
    "mode", ["complete", "missing-worker", "drift", "denied", "foreign-series"]
)
async def test_cloud_run_revision_all_containers_and_units(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(NativeMetricsReader, "_google_token", lambda self: "test-token")
    requests = []

    def backend(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.url.host == "run.googleapis.com":
            assert request.url.path == "/v2/" + RUN
            return httpx.Response(
                200,
                json={
                    "name": RUN,
                    "latestReadyRevision": REVISION,
                    "latestCreatedRevision": REVISION,
                    "trafficStatuses": [{"revision": REVISION, "percent": 100}],
                    "template": {
                        "containers": [
                            {
                                "name": name,
                                "resources": {
                                    "limits": {
                                        "cpu": "2" if mode == "drift" else "1",
                                        "memory": "2Gi",
                                    }
                                },
                                "env": [{"name": "PRIVATE_ENV", "value": "never-model"}],
                            }
                            for name in ("api", "worker-1", "worker-2")
                        ]
                    },
                },
            )
        assert request.url.host == "monitoring.googleapis.com"
        assert request.url.path == "/v3/projects/owned-project/timeSeries"
        params = request.url.params
        for field, value in (
            ("project_id", "owned-project"),
            ("service_name", "owned-app"),
            ("location", "asia-northeast3"),
            ("revision_name", REVISION),
        ):
            assert f'resource.labels.{field}="{value}"' in params["filter"]
        if mode == "denied":
            return httpx.Response(403, json={"error": "PRIVATE_PROVIDER_ERROR"})
        cpu = "cpu/utilizations" in params["filter"]
        assert params["aggregation.perSeriesAligner"] == (
            "ALIGN_PERCENTILE_95" if cpu else "ALIGN_PERCENTILE_99"
        )
        series = []
        names = [""] if cpu else ["api", "worker-1", "worker-2"]
        if mode == "missing-worker" and not cpu:
            names.pop()
        for i, name in enumerate(names):
            series.append(
                {
                    "resource": {
                        "type": "cloud_run_revision",
                        "labels": {
                            "project_id": "foreign-project"
                            if mode == "foreign-series"
                            else "owned-project",
                            "location": "asia-northeast3",
                            "service_name": "owned-app",
                            "revision_name": REVISION,
                        },
                    },
                    "metric": {
                        "type": "run.googleapis.com/container/"
                        + ("cpu/utilizations" if cpu else "memory/usage"),
                        "labels": {"container_name": name} if name else {},
                    },
                    "points": [
                        {
                            "interval": {"endTime": (START + timedelta(minutes=m)).isoformat()},
                            "value": {"doubleValue": 0.5 if cpu else (i + 1) * 100 * 1048576},
                        }
                        for m in range(60)
                    ],
                }
            )
        return httpx.Response(200, json={"timeSeries": series})

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as http:
        result = await NativeMetricsReader(http).collect(scope("gcp"), query())
    assert "never-model" not in str(result) and "PRIVATE_PROVIDER_ERROR" not in str(result)
    if mode == "complete":
        assert result["resourceUsage"]["allocation"]["scope"] == "CONTAINER"
        assert result["resourceUsage"]["allocation"]["totalCpuCores"] == 3
        assert result["resourceUsage"]["cpuP95Cores"] == 1.5
        assert result["metrics"]["memoryPeakMiB"] == 600
        assert result["quality"]["memoryPeakMiB"]["temporalCoveragePercent"] == 100
    else:
        assert result["resourceUsage"] is None
        if mode == "drift":
            assert len(requests) == 1


async def test_missing_identity_does_not_initialize_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no credentials or global resource discovery")

    monkeypatch.setattr(boto3, "Session", forbidden)
    monkeypatch.setattr(NativeMetricsReader, "_google_token", forbidden)
    for cloud in ("aws", "gcp"):
        selected = scope(cloud)
        selected.snapshot["resources"] = {}
        async with httpx.AsyncClient() as http:
            result = await NativeMetricsReader(http).collect(selected, query())
        assert result["resourceUsage"] is None


def test_cross_cloud_baseline_does_not_copy_different_allocation_units() -> None:
    current = {"environment": "aws", "app": {"cpu": 0.25, "memory_mib": 512, "worker_count": 2}}
    default = {"cpu": 1, "memory_mib": 2048, "worker_count": 2}
    assert target_baseline("gcp", current, None, default) == default
    copied = copy.deepcopy(current)
    copied["environment"] = "gcp"
    assert target_baseline("aws", copied, None, default) == default
    assert allocation("aws", default)["totalCpuCores"] == 1
    assert allocation("gcp", default)["totalCpuCores"] == 3
