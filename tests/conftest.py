from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from control_plane.auth import AuthService
from control_plane.config import Settings
from control_plane.domain.models import ResourceKind, StageKind, Workflow, WorkflowStatus
from control_plane.domain.stages import PLAN
from control_plane.engine.engine import Engine
from control_plane.main import Platform, assemble
from control_plane.seed import Seed
from control_plane.store.repository import Repository
from control_plane.store.table import MemoryTable, Table
from control_plane.workflows import CommandInput
from tests.fakes import (
    FakeAdapter,
    FakeClock,
    FakeDbGate,
    FakeProber,
    FakeTraffic,
    SimulatedWorkload,
)

SEED: dict[str, Any] = {
    "application": "goods",
    "scope": "scope:goods",
    "environments": {
        "aws": {
            "appUrl": "http://aws-app:8000",
            "trafficPool": "pool-aws",
            "actors": {"goods-aws-api": 4, "goods-aws-worker": 2},
        },
        "gcp": {
            "appUrl": "http://gcp-app:8000",
            "trafficPool": "pool-gcp",
            "actors": {"goods-gcp-api": 4, "goods-gcp-worker": 2},
        },
    },
    "resources": {
        "database": {
            "sourceRef": "db:goods-rds",
            "targetRef": "db:goods-cloudsql",
            "replicationRef": "dms:goods",
            "policy": {"rollbackMode": "BEFORE_TARGET_WRITE", "maxQuiesceSeconds": 300},
            "catalog": {
                "databases": {"db:goods-rds": {}, "db:goods-cloudsql": {}},
                "tables": ["public.orders"],
                "dmsJob": {"job": "projects/p/locations/l/migrationJobs/j"},
                "alias": "appdb",
                "appRef": "app:goods",
            },
        },
        "redis": {
            "sourceRef": "resource:aws",
            "targetRef": "resource:gcp",
            "bindingRef": "binding:redis-proxy",
            "policy": {"rollbackMode": "REVERSE_REQUIRED", "maxQuiesceSeconds": 1.8},
        },
        "object": {
            "sourceRef": "resource:aws-receipts",
            "targetRef": "resource:gcp-receipts",
            "bindingRef": "binding:receipts",
            "expectedApps": 12,
            "policy": {"rollbackMode": "REVERSE_REQUIRED", "maxQuiesceSeconds": 30},
        },
    },
    "adapters": {
        "database": {"url": "http://db", "tokenSecret": "env:DB"},
        "redis": {"url": "http://redis", "tokenSecret": "env:REDIS"},
        "object": {"url": "http://object", "tokenSecret": "env:OBJECT"},
    },
    "probe": {"groupbuyId": 1},
}


def stage_index(kind: StageKind) -> int:
    return next(index for index, stage in enumerate(PLAN) if stage.kind is kind)


CANARY_STAGE = stage_index(StageKind.CANARY)
REDIS_STAGE = stage_index(StageKind.REDIS_CUTOVER)
"""Also where the one approval for the Redis and DB cutovers is asked."""
DB_STAGE = stage_index(StageKind.DATABASE_CUTOVER)
TRAFFIC_STAGE = stage_index(StageKind.TRAFFIC)
FINALIZE_STAGE = stage_index(StageKind.FINALIZE)

SETTINGS = Settings(
    environment="local",
    public_url="https://platform.test",
    allowed_origins=["https://platform.test"],
    _env_file=None,  # type: ignore[call-arg]
)


class World:
    """A whole platform with fakes at its edges, driven one second at a time."""

    def __init__(self, table: Table | None = None) -> None:
        self.seed = Seed.model_validate(SEED)
        self.clock = FakeClock()
        self.table = table or MemoryTable()
        self.gate = FakeDbGate(self.seed, self.clock)
        self.db = FakeAdapter(ResourceKind.DATABASE, self.clock, self.gate)
        self.gate.adapter = self.db
        self.redis = FakeAdapter(ResourceKind.REDIS, self.clock)
        self.object = FakeAdapter(ResourceKind.OBJECT_STORAGE, self.clock)
        self.adapters = {
            ResourceKind.DATABASE: self.db,
            ResourceKind.REDIS: self.redis,
            ResourceKind.OBJECT_STORAGE: self.object,
        }
        self.traffic = FakeTraffic()
        self.prober = FakeProber(self.clock)
        self.platform = self._assemble()
        self.services = self.platform.engine.services
        self.repo = self.services.repo
        self.workflows = self.platform.context.workflows
        self.workload = SimulatedWorkload(self.seed, self.services.coordinator)
        self.seconds = 0
        self.restart_engine_every_tick = False
        self.tick_hook: Callable[[], None] | None = None

    def _assemble(self) -> Platform:
        repo = Repository(self.table)
        return assemble(
            settings=SETTINGS,
            seed=self.seed,
            repo=repo,
            clock=self.clock,
            adapters={kind: adapter.client() for kind, adapter in self.adapters.items()},
            gate=self.gate,
            traffic=self.traffic,
            prober=self.prober,
            auth=AuthService(
                repo,
                self.clock,
                httpx.AsyncClient(),
                client_id="",
                client_secret="",
                callback_url="",
                allowed_github_ids=[],
                session_hours=8,
            ),
            workload_token="workload-token",
        )

    async def start(self) -> Workflow:
        return await self.workflows.create(source_env="aws", target_env="gcp", actor="tester")

    async def workflow(self, workflow_id: str) -> Workflow:
        workflow = await self.repo.workflow(workflow_id)
        assert workflow is not None
        return workflow

    async def command(self, workflow_id: str, command: str, **fields: Any) -> Workflow:
        workflow = await self.workflow(workflow_id)
        return await self.workflows.apply(
            workflow_id,
            CommandInput(command=command, expected_version=workflow.version, **fields),
            actor="tester",
        )

    async def approve(self, workflow_id: str) -> Workflow:
        return await self.command(workflow_id, "APPROVE")

    async def tick(self) -> None:
        self.seconds += 1
        self.clock.advance(1)
        await self.workload.beat()
        await self.platform.probe_loop.light_once()
        if self.seconds % self.seed.timings.business_probe_interval == 0:
            await self.platform.probe_loop.business_once()
        engine = self.platform.engine
        if self.restart_engine_every_tick:
            # A new process that knows only what is in the table.
            engine = Engine(self.services, owner=engine.owner)
        await engine.tick()
        if self.tick_hook:
            self.tick_hook()

    async def until(
        self,
        workflow_id: str,
        *statuses: WorkflowStatus,
        limit: int = 2000,
        stage: int | None = None,
    ) -> Workflow:
        """Ticks until the workflow reaches a status, and fails with its state if it does not."""
        for _ in range(limit):
            workflow = await self.workflow(workflow_id)
            at_stage = stage is None or workflow.current_stage == stage
            if workflow.status in statuses and at_stage:
                return workflow
            await self.tick()
        stages = await self.repo.stages(workflow_id)
        current = stages[workflow.current_stage]
        raise AssertionError(
            f"still {workflow.status} at {current.kind}: {current.detail} / {workflow.halt}"
        )

    async def at_stage(self, workflow_id: str, index: int) -> Workflow:
        """Runs until `index` is the current stage, approving everything before it."""
        workflow = await self.workflow(workflow_id)
        for _ in range(3000):
            workflow = await self.workflow(workflow_id)
            if workflow.current_stage >= index:
                return workflow
            if workflow.status in {WorkflowStatus.PLANNED, WorkflowStatus.WAITING_APPROVAL}:
                await self.approve(workflow_id)
            elif workflow.status is not WorkflowStatus.RUNNING:
                raise AssertionError(f"stopped early: {workflow.status} {workflow.halt}")
            await self.tick()
        raise AssertionError(f"never reached stage {index}: {PLAN[workflow.current_stage]}")

    async def run_until_approval(self, workflow_id: str, *, stage: int) -> Workflow:
        """Approves everything before `stage`, and stops where `stage` asks for approval."""
        for _ in range(3000):
            workflow = await self.workflow(workflow_id)
            if workflow.status is WorkflowStatus.WAITING_APPROVAL and (
                workflow.current_stage == stage
            ):
                return workflow
            if workflow.status in {WorkflowStatus.PLANNED, WorkflowStatus.WAITING_APPROVAL}:
                await self.approve(workflow_id)
            await self.tick()
        raise AssertionError(f"never asked for approval at stage {stage}")

    async def run_to_end(self, workflow_id: str, *, limit: int = 4000) -> Workflow:
        for _ in range(limit):
            workflow = await self.workflow(workflow_id)
            if workflow.status.terminal or workflow.status is WorkflowStatus.HALTED:
                return workflow
            if workflow.status in {WorkflowStatus.PLANNED, WorkflowStatus.WAITING_APPROVAL}:
                await self.approve(workflow_id)
            await self.tick()
        raise AssertionError(f"not finished: {workflow.status} at {workflow.current_stage}")

    async def event_types(self, workflow_id: str) -> list[str]:
        events, _ = await self.repo.events(workflow_id, limit=5000)
        return [event.type for event in events]


@pytest.fixture
def world() -> World:
    return World()
