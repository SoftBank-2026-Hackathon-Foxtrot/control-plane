from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from control_plane.ai.runtime_metrics import SOURCE, RuntimeMetricsReader
from control_plane.ai.telemetry import TelemetryReader
from control_plane.config import Settings
from tests.test_ai import FakeModel, model, post, source_id
from tests.test_ai_native_metrics import query, scope
from tests.test_api import Console, console

# Imported fixtures are reused rather than creating another authenticated console/provisioner.
__all__ = ["console", "model"]


def runtime_response(request: httpx.Request, *, mode: str = "complete") -> httpx.Response:
    expression = request.url.params["query"]
    assert 'application_id="goods"' in expression
    assert 'foxtrot_owner_id="' in expression and 'deployment_id="' in expression
    assert 'deployment_environment_name="' in expression
    assert 'service_name="foxtrot-app-runtime"' in expression
    assert 'measurement_scope="container"' in expression
    assert "timestamp(" in expression and "time() - 30" in expression
    if mode == "unavailable":
        return httpx.Response(503)
    name = next(
        name
        for name in ("cpu_seconds_total", "memory_bytes", "cpu_limit", "memory_limit")
        if "foxtrot_runtime_" + name in expression
    )
    start, end = (int(request.url.params[key]) for key in ("start", "end"))
    rows = []
    for replica in range(2):
        for index, role in enumerate(("api", "worker", "worker")):
            if mode == "missing_workers" and role == "worker":
                continue
            value = {
                "cpu_seconds_total": 0.3 if role == "api" else 0.15,
                "memory_bytes": (500 if role == "api" else 150) * 1024**2,
                "cpu_limit": 2 if mode == "drift" else 1,
                "memory_limit": 2048 * 1024**2,
            }[name]
            times = list(range(start, end + 1, 60))
            if mode == "sparse":
                times = times[:2]
            rows.append(
                {
                    "metric": {"service_instance_id": f"r{replica}-{index}", "app_role": role},
                    "values": [[at, str(value)] for at in times],
                }
            )
    if mode == "invalid" and name == "memory_bytes":
        for row in rows:
            row["values"] = [[at, "NaN"] for at in range(start, end + 1, 60)]
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "matrix", "result": rows}}
    )


@pytest.mark.parametrize("cloud", ["aws", "gcp"])
async def test_runtime_units_replicas_coverage_and_existing_resource_contract(cloud: str) -> None:
    selected = scope(cloud)
    async with httpx.AsyncClient(transport=httpx.MockTransport(runtime_response)) as http:
        result = await RuntimeMetricsReader(
            Settings(prometheus_url="http://prometheus", _env_file=None), http
        ).collect(selected, query())
    usage = result["resourceUsage"]
    assert usage["source"] == SOURCE
    assert usage["cpuP95Cores"] == pytest.approx(0.6)
    assert usage["memoryPeakMiB"] == 800
    assert usage["containerCpuUpperBoundCores"] == 0.3
    assert usage["containerMemoryPeakMiB"] == 500
    assert result["metrics"]["cpuP95Percent"] == pytest.approx(60 if cloud == "aws" else 20)
    assert result["metrics"]["memoryPeakMiB"] == 800
    assert result["quality"]["cpuP95Percent"]["temporalCoveragePercent"] == 100


@pytest.mark.parametrize("mode", ["sparse", "missing_workers", "unavailable", "invalid", "drift"])
async def test_missing_or_unconfirmed_runtime_never_becomes_idle_capacity(mode: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: runtime_response(request, mode=mode))
    ) as http:
        result = await RuntimeMetricsReader(
            Settings(prometheus_url="http://prometheus", _env_file=None), http
        ).collect(scope(), query())
    if mode == "sparse":
        assert 0 < result["quality"]["cpuP95Percent"]["temporalCoveragePercent"] < 80
    else:
        assert result["resourceUsage"] is None
    if mode != "drift" and mode != "sparse":
        assert all(value is None for value in result["metrics"].values())


async def test_default_reader_feeds_existing_update_recommendation(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    engine = console.app.state.ai_service.infra.deployment
    identifier = source_id(console)
    record = engine.store.environment(identifier)
    snapshot = dict(record["applied_snapshot"])
    snapshot["appliedAt"] = (console.world.clock.now() - timedelta(hours=2)).isoformat()
    snapshot["app"] = {**snapshot["app"], "cpu": 1, "memory_mib": 2048, "worker_count": 2}
    snapshot["hasTunnelContainer"] = False
    engine.store.update_environment(identifier, record["updated_at"], applied_snapshot=snapshot)

    def backend(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("query_range"):
            return runtime_response(request)
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as http:
        console.app.state.ai_service.telemetry = TelemetryReader(
            Settings(prometheus_url="http://prometheus", _env_file=None), http
        )
        result: dict[str, Any] = (
            await post(
                console,
                "recommendations",
                {"sourceDeploymentId": identifier, "targetEnvironment": "gcp"},
            )
        ).json()
    assert result["status"] == "COMPLETED", result
    assert model.calls[-1][1]["analysis"]["resourceUsage"]["source"] == SOURCE
    assert result["proposal"] is not None and result["iac"]["action"] == "UPDATE"
    assert source_id(console, "gcp") in result["iac"]["endpoint"]
