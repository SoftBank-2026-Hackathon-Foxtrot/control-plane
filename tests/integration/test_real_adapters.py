"""The engine against the teammates' actual code. See README, "실제 어댑터 검증".

The rest of the suite proves the engine against fakes written from reading those adapters. These
tests prove that reading was right. They are skipped unless the environment says where to find:

    OBJECT_MIGRATION_REPO   a checkout of object-migration (needs Go; runs with ENGINE=fake)
    STATEMIG_URL            a running Redis adapter stack (demo/docker-compose.yml)
    db_operator installed   `uv pip install -e <db-migration-adaptor>`
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import httpx
import pytest
from moto import mock_aws

from control_plane.adapters.client import AdapterClient
from control_plane.clock import stamp
from control_plane.dbgate import SdkDbGate
from control_plane.domain.models import (
    HoldDesired,
    ResourceKind,
    Workflow,
    WorkflowStatus,
)
from control_plane.seed import load_seed
from tests.conftest import World

pytestmark = pytest.mark.integration

OBJECT_REPO = os.environ.get("OBJECT_MIGRATION_REPO")
STATEMIG_URL = os.environ.get("STATEMIG_URL")
STATEMIG_TOKEN = os.environ.get("STATEMIG_TOKEN", "demo-admin-token")
TOKEN = "integration-token"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def object_server() -> Iterator[str]:
    if not OBJECT_REPO:
        pytest.skip("OBJECT_MIGRATION_REPO is not set")
    with tempfile.TemporaryDirectory() as directory:
        binary = Path(directory) / "object-migration.exe"
        subprocess.run(
            ["go", "build", "-o", str(binary), "./cmd/object-migration"],
            cwd=OBJECT_REPO,
            check=True,
        )
        resources = Path(directory) / "resources.json"
        resources.write_text(
            json.dumps(
                {
                    "aws-receipts": {"kind": "s3", "bucket": "a", "region": "r", "ref": "default"},
                    "gcp-receipts": {"kind": "gcs", "bucket": "b", "ref": "default"},
                }
            )
        )
        port = free_port()
        process = subprocess.Popen(
            [str(binary)],
            env={
                **os.environ,
                "ADDR": f"127.0.0.1:{port}",
                "API_TOKEN": TOKEN,
                "ENGINE": "fake",
                "FAKE_STEP_DELAY": "50ms",
                "POLL_INTERVAL": "50ms",
                "APP_TTL": "5s",
                "CONFIRM_TIMEOUT": "20s",
                "OBJECT_RESOURCES": str(resources),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        try:
            yield url
        finally:
            process.terminate()
            process.wait(timeout=10)


class RealObjectApps:
    """Application processes, following the real route as `demo/app/objroute.py` does."""

    def __init__(self, url: str, apps: int) -> None:
        self.http = httpx.AsyncClient(base_url=url, timeout=5)
        self.names = [f"app-{index}" for index in range(apps)]
        self.applied: dict[str, int] = dict.fromkeys(self.names, 0)

    async def follow(self, migration_id: str | None) -> None:
        if migration_id is None:
            return
        await asyncio.gather(*(self._poll(migration_id, name) for name in self.names))

    async def _poll(self, migration_id: str, name: str) -> None:
        response = await self.http.get(
            f"/v1/migrations/{migration_id}/routing",
            params={"app": name, "applied": self.applied[name]},
        )
        if response.status_code == 200:
            self.applied[name] = int(response.json()["generation"])


async def drive(
    world: World, workflow: Workflow, *, apps: RealObjectApps | None, sleep: float = 0.05
) -> Workflow:
    """Runs to completion or to a stop. Fake time moves one second per tick of real time."""
    for _ in range(6000):
        current = await world.workflow(workflow.id)
        if current.status.terminal or current.status is WorkflowStatus.HALTED:
            return current
        if current.status in {WorkflowStatus.PLANNED, WorkflowStatus.WAITING_APPROVAL}:
            await world.approve(workflow.id)
        if apps:
            hold = await world.services.coordinator.hold()
            await apps.follow(hold.object_migration_id)
        await world.tick()
        await asyncio.sleep(sleep)
    raise AssertionError(f"not finished: {current.status} at stage {current.current_stage}")


@pytest.fixture
async def object_world(object_server: str) -> AsyncIterator[tuple[World, RealObjectApps]]:
    world = World()
    # drive() advances fake time 20x faster than the real adapter. Leave enough fake
    # seconds for its real 30-second stability window; production limits are unchanged.
    world.seed.timings.advance_timeout = 1200
    http = httpx.AsyncClient(base_url=object_server, timeout=10)
    world.services.adapters[ResourceKind.OBJECT_STORAGE] = AdapterClient(http, TOKEN)
    apps = RealObjectApps(object_server, world.seed.resources.object.expected_apps or 0)
    # The workflow service reads the same mapping when it checks capabilities.
    yield world, apps
    await http.aclose()
    await apps.http.aclose()


async def test_real_object_adapter_goes_through_every_stage(
    object_world: tuple[World, RealObjectApps],
) -> None:
    world, apps = object_world
    workflow = await world.start()
    final = await drive(world, workflow, apps=apps)

    events, _ = await world.repo.events(workflow.id, limit=5000)
    assert final.status is WorkflowStatus.COMPLETED, (final.halt, [e.message for e in events[-8:]])
    resource = await world.repo.resource(workflow.id, ResourceKind.OBJECT_STORAGE)
    assert resource.last_status is not None
    assert resource.last_status["lifecycle"] == "COMPLETED"
    commands = await world.repo.commands(workflow.id)
    sent = [c.action for c in commands if c.resource is ResourceKind.OBJECT_STORAGE]
    assert sent == ["PREPARE", "START_SYNC", "QUIESCE", "CUTOVER", "RESUME", "FINALIZE"]


async def test_real_object_adapter_refuses_nothing_on_rollback(
    object_world: tuple[World, RealObjectApps],
) -> None:
    world, apps = object_world
    workflow = await world.start()
    for _ in range(3000):
        current = await world.workflow(workflow.id)
        if current.current_stage >= 3:
            break
        if current.status in {WorkflowStatus.PLANNED, WorkflowStatus.WAITING_APPROVAL}:
            await world.approve(workflow.id)
        hold = await world.services.coordinator.hold()
        await apps.follow(hold.object_migration_id)
        await world.tick()
        await asyncio.sleep(0.05)
    await world.command(workflow.id, "HALT")
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    for _ in range(2000):
        current = await world.workflow(workflow.id)
        if current.status is WorkflowStatus.HALTED:
            break
        hold = await world.services.coordinator.hold()
        await apps.follow(hold.object_migration_id)
        await world.tick()
        await asyncio.sleep(0.05)
    assert current.halt is not None and current.halt.code == "ROLLBACK_COMPLETED", current.halt
    resource = await world.repo.resource(workflow.id, ResourceKind.OBJECT_STORAGE)
    assert resource.last_status is not None
    assert resource.last_status["lifecycle"] == "RESTORED"


# ---- Redis ---------------------------------------------------------------------------------------
@pytest.fixture
async def redis_world() -> AsyncIterator[World]:
    if not STATEMIG_URL:
        pytest.skip("STATEMIG_URL is not set")
    world = World()
    http = httpx.AsyncClient(base_url=STATEMIG_URL, timeout=30)
    world.services.adapters[ResourceKind.REDIS] = AdapterClient(http, STATEMIG_TOKEN)
    yield world
    await http.aclose()


async def test_real_redis_adapter_goes_through_every_stage(redis_world: World) -> None:
    world = redis_world
    workflow = await world.start()
    final = await drive(world, workflow, apps=None, sleep=0.2)

    events, _ = await world.repo.events(workflow.id, limit=5000)
    assert final.status is WorkflowStatus.COMPLETED, (final.halt, [e.message for e in events[-8:]])
    commands = await world.repo.commands(workflow.id)
    sent = [c.action for c in commands if c.resource is ResourceKind.REDIS]
    assert sent == ["PREPARE", "START_SYNC", "QUIESCE", "CUTOVER", "RESUME", "FINALIZE"]


# ---- the DB adapter's evidence -------------------------------------------------------------------
@pytest.fixture
def db_operator_store() -> Iterator[Any]:
    store_module = pytest.importorskip("db_operator.store")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-2")
        client.create_table(
            TableName="db-state",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "namespace", "AttributeType": "S"},
                {"AttributeName": "record", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "namespace", "KeyType": "HASH"},
                {"AttributeName": "record", "KeyType": "RANGE"},
            ],
        )
        resource = boto3.resource("dynamodb", region_name="ap-northeast-2")
        yield store_module.Store(
            "db-state", namespace="test", region="ap-northeast-2", client=resource.meta.client
        )


async def test_evidence_from_the_coordinator_passes_the_real_verification(
    world: World, db_operator_store: Any
) -> None:
    """What `verify_gate` in the DB adapter accepts is what the coordinator must produce."""
    control = pytest.importorskip("db_operator.control")
    participants_valid = control.participants_valid

    coordinator = world.services.coordinator
    hold = await coordinator.request_hold("wf")
    world.workload.generation = hold.generation
    world.workload.desired = HoldDesired.HOLD
    await world.workload.beat()

    evidence = await coordinator.participants(hold.generation)
    # The real check reads the wall clock, so the reports must look fresh to it.
    now = stamp(datetime.now(UTC))
    for entry in evidence:
        entry["observedAt"] = now
    assert participants_valid(evidence, sorted(world.seed.all_actors()), hold.generation, "HELD")
    assert not participants_valid(
        evidence, sorted(world.seed.all_actors()), hold.generation + 1, "HELD"
    )
    assert not participants_valid(evidence[:-1], sorted(world.seed.all_actors()), 1, "HELD")
    assert hold.desired is HoldDesired.HOLD


async def test_native_catalog_uses_the_real_sdk_without_registering_a_dms_job(
    db_operator_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = load_seed(Path(__file__).parents[2] / "config" / "seed.example.yaml")
    database = seed.resources.database
    catalog = database.catalog
    assert catalog is not None
    db_operator_store.initialize()
    gate = SdkDbGate("db-state", namespace="test", region="ap-northeast-2")

    def unexpected_dms(*_: Any, **__: Any) -> None:
        pytest.fail("native registration must not publish a DMS job")

    monkeypatch.setattr(gate.catalog, "dms_job", unexpected_dms)
    await gate.publish_catalog(
        databases=catalog.databases,
        tables=catalog.tables,
        sequences=catalog.sequences,
        scope=seed.scope,
        participants=sorted(seed.all_actors()),
        source_ref=database.source_ref,
        target_ref=database.target_ref,
        dms_ref=None,
        dms_job=None,
    )
    rows = db_operator_store.transact(lambda tx: tx.list("CATALOG_DB"))
    assert len(rows) == 2
    scopes = db_operator_store.transact(lambda tx: tx.list("CATALOG_SCOPE"))
    assert scopes[0]["participants"] == sorted(seed.all_actors())
    assert not db_operator_store.transact(lambda tx: tx.list("CATALOG_DMS"))


# ---- the traffic service -------------------------------------------------------------------------
TRAFFIC_REPO = os.environ.get("TRAFFIC_REPO")
TRAFFIC_TOKEN = "traffic-token"


class FakeLoadBalancer:
    """Cloudflare's side only: one load balancer and its two pools, as the service expects them."""

    def __init__(self) -> None:
        self.lb: dict[str, Any] = {
            "enabled": True,
            "proxied": True,
            "steering_policy": "random",
            "session_affinity": "none",
            "default_pools": ["pool-a", "pool-b"],
            "random_steering": {"default_weight": 0, "pool_weights": {"pool-a": 1, "pool-b": 0}},
        }
        self.patches: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/load_balancers/test-lb"):
            if request.method == "PATCH":
                body = json.loads(request.content)
                self.patches.append(body)
                self.lb.update(body)
            return httpx.Response(200, json={"success": True, "result": self.lb})
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"success": True, "result": {"pop_health": {}}})
        raise AssertionError(f"unexpected Cloudflare call {request.method} {request.url}")

    @property
    def pool_weights(self) -> dict[str, float]:
        return dict(self.lb["random_steering"]["pool_weights"])


@pytest.fixture
async def traffic_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[tuple[Any, FakeLoadBalancer]]:
    """The traffic team's server code: its routes, SQLite and Cloudflare client, in this process."""
    if not TRAFFIC_REPO:
        pytest.skip("TRAFFIC_REPO is not set")
    monkeypatch.syspath_prepend(TRAFFIC_REPO)
    from server import main as traffic_main  # type: ignore[import-not-found]

    from control_plane.traffic import TrafficService

    for name, value in {
        "TRAFFIC_SERVICE_TOKEN": TRAFFIC_TOKEN,
        "CF_API_TOKEN": "cf",
        "CF_ACCOUNT_ID": "test-account",
        "CF_ZONE_ID": "test-zone",
        "CF_LB_ID": "test-lb",
        "CF_POOL_IDS": '{"aws":"pool-a","gcp":"pool-b"}',
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(traffic_main, "DB_PATH", tmp_path / "traffic.db")
    cloudflare = FakeLoadBalancer()
    real_client = httpx.AsyncClient
    # Only the traffic service's own Cloudflare client is redirected, not httpx for everyone.
    from server import cloudflare as cloudflare_module  # type: ignore[import-not-found]

    monkeypatch.setattr(
        cloudflare_module,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: real_client(
                transport=httpx.MockTransport(cloudflare), **kwargs
            )
        ),
    )

    async def no_wait(_: float) -> None:
        return None

    async with traffic_main.lifespan(traffic_main.app):
        http = real_client(
            transport=httpx.ASGITransport(app=traffic_main.app), base_url="http://traffic"
        )
        yield TrafficService(http, token=TRAFFIC_TOKEN, sleep=no_wait), cloudflare
        await http.aclose()


async def test_the_real_traffic_service_moves_the_load_balancer(
    traffic_service: tuple[Any, FakeLoadBalancer],
) -> None:
    service, cloudflare = traffic_service
    applied = await service.set_split(source_pool="aws", target_pool="gcp", target_percent=5)
    assert applied.effective_percent == {"aws": 95.0, "gcp": 5.0}
    assert cloudflare.pool_weights == {"pool-a": 0.95, "pool-b": 0.05}

    await service.set_split(source_pool="aws", target_pool="gcp", target_percent=5)
    assert len(cloudflare.patches) == 1, "a repeat finds the weights in place and sends nothing"

    await service.set_split(source_pool="aws", target_pool="gcp", target_percent=0)
    assert cloudflare.pool_weights == {"pool-a": 1.0, "pool-b": 0.0}


async def test_a_whole_migration_moves_traffic_through_the_real_service(
    traffic_service: tuple[Any, FakeLoadBalancer],
) -> None:
    service, cloudflare = traffic_service
    world = World(traffic=service)
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)

    assert final.status is WorkflowStatus.COMPLETED, final.halt
    shares = [
        round(patch["random_steering"]["pool_weights"]["pool-b"] * 100)
        for patch in cloudflare.patches
    ]
    assert shares == [5, 50, 100]
    assert cloudflare.pool_weights == {"pool-a": 0.0, "pool-b": 1.0}
