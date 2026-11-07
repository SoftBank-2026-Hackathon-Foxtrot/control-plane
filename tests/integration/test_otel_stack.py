"""Opt-in real Collector -> CP/Prometheus/Tempo contract test; no cloud operations.

Start IaC/observability/compose.yaml with project foxtrot-otel-check, ports
14318/19090/13200, CP logs endpoint http://host.docker.internal:18800/internal/otlp/v1/logs,
and the two test tokens below. RUN_OTEL_STACK=1 uv run pytest tests/integration/test_otel_stack.py
"""

import asyncio
import os
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn

from control_plane.domain.models import ResourceKind, ResourceState
from control_plane.main import create_api
from control_plane.telemetry import Telemetry
from tests.conftest import World

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_OTEL_STACK") != "1", reason="local OTEL stack required"
)


async def test_real_sdk_collector_and_three_backends(monkeypatch):
    world = World()
    world.clock.now = lambda: datetime.now(UTC)
    workflow = await world.start()
    await world.repo.save_resource(
        workflow.id, ResourceState(kind=ResourceKind.DATABASE, migration_id="db-otel")
    )
    context = world.platform.context
    world.platform.context = replace(
        context,
        observation_token="local-collector-test",
        settings=context.settings.model_copy(
            update={
                "auth_disabled": True,
                "host": "127.0.0.1",
                "prometheus_url": "http://127.0.0.1:19090",
                "tempo_url": "http://127.0.0.1:13200",
            }
        ),
    )
    app = create_api(world.platform, run_loops=False)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=18800, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:14318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20local-module-test")
    telemetry = Telemetry("database")
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        with telemetry.operation("COPY", workflow=workflow.id, migration="db-otel"):
            assert telemetry.snapshot(
                "DATABASE",
                {
                    "workflowId": workflow.id,
                    "migrationId": "db-otel",
                    "observed": {"observedAt": datetime.now(UTC).isoformat()},
                    "progress": {"completed": 7, "total": 10},
                    "password": "must-not-leak",
                },
            )
        for provider in (telemetry.logs, telemetry.metrics, telemetry.traces):
            await asyncio.to_thread(provider.force_flush)
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18800", timeout=5) as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                detail = (await client.get(f"/v1/workflows/{workflow.id}")).json()
                result = (await client.get(f"/v1/workflows/{workflow.id}/telemetry")).json()
                resource = next(r for r in detail["resources"] if r["kind"] == "DATABASE")
                if resource["progress"] and result["metrics"] and result["traces"]:
                    break
                await asyncio.sleep(1)
            assert resource["progress"]["completed"] == 7
            assert resource["observation"]["source"] == "OTEL"
            assert "must-not-leak" not in str(detail)
            assert result["metricsState"] == result["tracesState"] == "OK"
            assert any(
                m["name"] == "foxtrot_migration_completed" and m["value"] == 7
                for m in result["metrics"]
            )
            assert result["traces"], result
            # After the exporter stops, a retained snapshot becomes stale rather than zero.
            world.clock.now = lambda: (
                datetime.now(UTC) + __import__("datetime").timedelta(seconds=30)
            )
            stale = (await client.get(f"/v1/workflows/{workflow.id}")).json()["resources"][0]
            assert stale["observation"]["freshness"] == "STALE"
            assert stale["progress"]["completed"] == 7
    finally:
        await asyncio.to_thread(telemetry.close)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
