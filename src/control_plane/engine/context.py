"""What a stage handler works with, and what it can answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from control_plane.adapters.client import AdapterClient, MigrationView
from control_plane.clock import Clock
from control_plane.dbgate import DbGate
from control_plane.domain.models import (
    ResourceKind,
    ResourceState,
    StageState,
    Workflow,
    WorkflowEvent,
)
from control_plane.seed import Environment, Seed
from control_plane.store.repository import Repository
from control_plane.traffic import TrafficController
from control_plane.workload.coordinator import WorkloadCoordinator

SNAPSHOT_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class Wait:
    """Nothing is wrong; ask again on the next tick."""

    detail: str = ""


@dataclass(frozen=True)
class Passed:
    """This piece of work is finished."""


@dataclass(frozen=True)
class Stop:
    """A person must decide. Nothing moves on until then."""

    code: str
    message: str
    blockers: list[str] = field(default_factory=list)
    resource: ResourceKind | None = None
    reconcile: bool = False
    """Ask the adapters to compare their records with reality before the person decides."""


Outcome = Wait | Passed | Stop


@dataclass(frozen=True)
class Services:
    repo: Repository
    clock: Clock
    seed: Seed
    adapters: dict[ResourceKind, AdapterClient]
    coordinator: WorkloadCoordinator
    gate: DbGate
    traffic: TrafficController
    prober: Any
    deployment: Any = None


class StageContext:
    """One tick of one stage. Views are cached for the tick, so decisions agree with each other."""

    def __init__(self, services: Services, workflow: Workflow, stage: StageState) -> None:
        self.services = services
        self.workflow = workflow
        self.stage = stage
        self._views: dict[ResourceKind, MigrationView] = {}

    @property
    def seed(self) -> Seed:
        return self.services.seed

    @property
    def now(self) -> datetime:
        return self.services.clock.now()

    @property
    def source_env(self) -> Environment:
        return self.seed.environments[self.workflow.source_env]

    @property
    def target_env(self) -> Environment:
        return self.seed.environments[self.workflow.target_env]

    def client(self, kind: ResourceKind) -> AdapterClient:
        return self.services.adapters[kind]

    async def resource(self, kind: ResourceKind) -> ResourceState:
        return await self.services.repo.resource(self.workflow.id, kind)

    async def view(self, kind: ResourceKind, *, fresh: bool = False) -> MigrationView:
        if fresh or kind not in self._views:
            resource = await self.resource(kind)
            if resource.migration_id is None:
                raise LookupError(f"no plan is registered for {kind.value}")
            view = await self.client(kind).migration(resource.migration_id)
            self._views[kind] = view
            await self._remember(resource, view)
        return self._views[kind]

    async def _remember(self, resource: ResourceState, view: MigrationView) -> None:
        """Keeps what the console shows, without a table write on every tick."""
        changed = resource.last_route != view.route or (
            resource.last_status is None
            or resource.last_status.get("lifecycle") != view.lifecycle
            or resource.target_writes_possible != view.target_writes_possible
        )
        stale = (
            resource.last_status_at is None
            or (self.now - resource.last_status_at).total_seconds() >= SNAPSHOT_INTERVAL_SECONDS
        )
        if changed or stale:
            await self.services.repo.save_resource(
                self.workflow.id,
                resource.model_copy(
                    update={
                        "last_status": view.raw,
                        "last_status_at": self.now,
                        "last_route": view.route,
                        # One-way: once the target may have taken writes, it may have.
                        "target_writes_possible": resource.target_writes_possible
                        or view.target_writes_possible,
                        "transition_id": view.raw.get("transitionId") or resource.transition_id,
                    }
                ),
            )

    async def emit(
        self,
        type_: str,
        message: str = "",
        *,
        resource: ResourceKind | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        await self.services.repo.add_event(
            self.workflow.id,
            WorkflowEvent(
                at=self.now,
                type=type_,
                stage=self.stage.index,
                resource=resource,
                message=message,
                evidence=evidence or {},
            ),
        )

    # ---- timers: how long has this been true? They survive restarts with the stage. -----------
    def elapsed(self, name: str) -> float:
        """Seconds since `name` was first asked about, starting the timer if needed."""
        timers = self.stage.data.setdefault("timers", {})
        if name not in timers:
            timers[name] = self.now.isoformat()
        return (self.now - datetime.fromisoformat(timers[name])).total_seconds()

    def reset_timer(self, name: str) -> None:
        self.stage.data.setdefault("timers", {}).pop(name, None)

    def clear_timers(self) -> None:
        self.stage.data["timers"] = {}
