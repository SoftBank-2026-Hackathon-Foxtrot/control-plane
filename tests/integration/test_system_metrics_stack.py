"""Opt-in real Collector/Prometheus checks. No cloud operations.

Use the central local stack on OTLP 14318 and Prometheus 19090.
RUN_SYSTEM_METRICS_STACK=1 enables identity isolation checks.
SYSTEM_METRICS_RUNTIME=1 also checks a running demo container carrying the
owner/deployment/cloud identity from deployment_records()['env-own-onprem'].
"""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from control_plane.main import create_api
from tests.conftest import World
from tests.test_system_metrics import deployment_records

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SYSTEM_METRICS_STACK") != "1", reason="local OTEL stack required"
)


def client_and_records():
    world = World()
    world.clock.now = lambda: datetime.now(UTC)
    ctx = world.platform.context
    world.platform.context = replace(
        ctx,
        settings=ctx.settings.model_copy(
            update={
                "auth_disabled": True,
                "host": "127.0.0.1",
                "prometheus_url": os.getenv(
                    "SYSTEM_METRICS_PROMETHEUS_URL", "http://127.0.0.1:19090"
                ),
            }
        ),
    )
    app = create_api(world.platform, run_loops=False)
    records = deployment_records()
    records["env-same-cloud"] = {
        **records["env-own-aws"],
        "id": "env-same-cloud",
        "deployment_id": "same-cloud-other-deployment",
    }
    app.state.deployment_service = SimpleNamespace(store=SimpleNamespace(environment=records.get))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test"), records


async def test_same_app_and_instance_do_not_mix_deployments_or_owners():
    client, records = client_and_records()
    owner = records["env-own-aws"]["org_id"]
    providers = []
    try:
        for deployment, identity, size in [
            ("deploy-one", owner, 100),
            ("same-cloud-other-deployment", owner, 900),
            ("deploy-one", "other-owner", 8000),
        ]:
            provider = MeterProvider(
                resource=Resource.create(
                    {
                        "service.name": "foxtrot-app-runtime",
                        "service.instance.id": "isolation-instance",
                        "application.id": "goods",
                        "deployment.environment.name": "aws",
                        "foxtrot.owner.id": identity,
                        "deployment.id": deployment,
                    }
                ),
                metric_readers=[
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(
                            endpoint="http://127.0.0.1:14318/v1/metrics",
                            headers={"Authorization": "Bearer local-module-test"},
                            timeout=2,
                        ),
                        export_interval_millis=60000,
                    )
                ],
            )
            providers.append(provider)
            provider.get_meter("isolation-test").create_observable_gauge(
                "foxtrot.runtime.memory_bytes",
                callbacks=[lambda _, value=size: [Observation(value)]],
            )
            provider.force_flush()
        async with client:
            for _attempt in range(30):
                response = await client.get(
                    "/v1/applications/goods/system-metrics",
                    params={
                        "deployment": "env-own-aws",
                        "instance": "isolation-instance",
                        "category": "memory",
                        "minutes": 15,
                    },
                )
                assert response.status_code == 200, response.text
                metric = next(
                    row for row in response.json()["series"] if row["id"] == "memory_bytes"
                )
                samples = [p[1] for p in metric["points"] if p[1] is not None]
                if samples:
                    break
                await asyncio.sleep(0.5)
            assert samples and samples[-1] == 100, metric
            response = await client.get(
                "/v1/applications/goods/system-metrics",
                params={
                    "deployment": "env-same-cloud",
                    "instance": "isolation-instance",
                    "category": "memory",
                    "minutes": 15,
                },
            )
            metric = next(row for row in response.json()["series"] if row["id"] == "memory_bytes")
            assert [p[1] for p in metric["points"] if p[1] is not None][-1] == 900
            assert (
                await client.get(
                    "/v1/applications/goods/system-metrics/instances",
                    params={"deployment": "env-other"},
                )
            ).status_code == 404
    finally:
        for provider in providers:
            provider.shutdown()


@pytest.mark.skipif(
    os.getenv("SYSTEM_METRICS_RUNTIME") != "1", reason="running demo container required"
)
async def test_real_onprem_container_all_categories():
    client, _ = client_and_records()
    async with client:
        root = "/v1/applications/goods/system-metrics"
        body = (
            await client.get(root + "/instances", params={"deployment": "env-own-onprem"})
        ).json()
        assert body["state"] == "OK" and body["instances"], body
        instance = next(row for row in body["instances"] if not row["stale"])
        assert instance["networkScope"] == "container"
        for category in ("cpu", "memory", "disk", "network"):
            response = await client.get(
                root,
                params={
                    "deployment": "env-own-onprem",
                    "instance": instance["id"],
                    "category": category,
                    "minutes": 15,
                },
            )
            assert response.status_code == 200, response.text
            for metric in response.json()["series"]:
                assert metric["state"] == "OK", metric
                values = [p[1] for p in metric["points"] if p[1] is not None]
                assert values and all(value >= 0 for value in values), metric
