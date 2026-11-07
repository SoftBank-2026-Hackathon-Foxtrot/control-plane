"""Opt-in check against a real app-host Collector and Prometheus.

Set RUN_SYSTEM_METRICS_STACK=1 after starting the demo host agent and the
central observability compose project on Prometheus port 29090.
"""

import os
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from control_plane.main import create_api
from tests.conftest import World

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SYSTEM_METRICS_STACK") != "1", reason="local host-metrics stack required"
)


async def test_real_host_agent_prometheus_and_scoped_api():
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
                    "SYSTEM_METRICS_PROMETHEUS_URL", "http://127.0.0.1:29090"
                ),
            }
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_api(world.platform, run_loops=False)),
        base_url="http://test",
    ) as client:
        root = "/v1/applications/goods/system-metrics"
        hosts = (await client.get(root + "/hosts?environment=aws")).json()
        assert hosts["state"] == "OK" and hosts["hosts"], hosts
        host = hosts["hosts"][0]
        assert not host["stale"]
        for category in ("cpu", "memory", "disk", "network"):
            response = await client.get(
                root,
                params={
                    "environment": "aws",
                    "host": host["name"],
                    "category": category,
                    "minutes": 15,
                },
            )
            assert response.status_code == 200, response.text
            for metric in response.json()["series"]:
                assert metric["state"] == "OK", metric
                samples = [p[1] for p in metric["points"] if p[1] is not None]
                assert samples and all(v >= 0 for v in samples), metric
                if metric["unit"] == "%":
                    assert all(v <= 100 for v in samples)
        other = (await client.get(root + "/hosts?environment=gcp")).json()
        assert other["hosts"] == []
