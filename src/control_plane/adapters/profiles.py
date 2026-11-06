"""Where the three adapters mean different things by the same API.

The engine walks every resource through the same steps. Whatever differs between resources is a
rule in one of these classes, so a new resource is a new class and the engine does not change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from control_plane.adapters.client import MigrationView
from control_plane.domain.models import ResourceKind, ResourceState, Workflow
from control_plane.seed import (
    ATOMIC_LOGICAL_PROFILE,
    DMS_PROFILE,
    DatabaseSeed,
    ResourceSeed,
    Timings,
)

ORCHESTRATOR_EVIDENCE = frozenset({"WORKLOAD_RUNNING", "BUSINESS_PROBE"})
"""Checks the adapters report as UNKNOWN because only the platform can judge them."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class UnknownBudget:
    """How long a command with a lost result may be reconciled and retried before a person."""

    attempts: int | None = None
    seconds: float | None = None


class ResourceProfile(ABC):
    kind: ClassVar[ResourceKind]
    resource_id: ClassVar[str]
    profile: ClassVar[str]

    requires_workload_hold: ClassVar[bool] = False
    """Whether the workload must stop before this resource's QUIESCE."""
    adapter_judges_advance: ClassVar[bool] = False
    """Whether `readiness.advance` can reach PASS, given the evidence the platform records."""

    def plan_body(
        self, workflow: Workflow, seed: ResourceSeed, resource: ResourceState, scope: str
    ) -> dict[str, Any]:
        return {
            "workflowId": workflow.id,
            "resourceId": self.resource_id,
            "kind": self.kind.value,
            "profile": self.selected_profile(seed),
            "sourceRef": seed.source_ref,
            "targetRef": seed.target_ref,
            "bindingRef": resource.binding_ref or seed.binding_ref,
            "workloadScopeRef": resource.workload_scope_ref or scope,
            "policy": self._policy(seed),
        }

    def selected_profile(self, seed: ResourceSeed) -> str:
        return self.profile

    def check_before_hold(self, seed: ResourceSeed) -> bool:
        return False

    def needs_held_evidence(self, action: str, seed: ResourceSeed) -> bool:
        return self.requires_workload_hold

    def _policy(self, seed: ResourceSeed) -> dict[str, Any]:
        # The contract says integer, and the DB and object adapters refuse anything else.
        return {
            "rollbackMode": seed.policy.rollback_mode,
            "maxQuiesceSeconds": int(seed.policy.max_quiesce_seconds),
        }

    def ready_for_cutover(self, view: MigrationView, seed: ResourceSeed) -> Verdict:
        if view.lifecycle != "READY":
            return Verdict(False, f"lifecycle is {view.lifecycle}")
        return Verdict(True)

    @abstractmethod
    def advance(self, view: MigrationView) -> Verdict:
        """Whether this resource's own state allows the next stage. Probes are checked apart."""

    def rollback_extra(self, mode: str) -> dict[str, Any]:
        return {}

    def unknown_budget(self, timings: Timings, seed: ResourceSeed) -> UnknownBudget:
        return UnknownBudget(attempts=timings.unknown_retry_limit)

    def _settled(self, view: MigrationView) -> Verdict:
        if view.lifecycle != "ACTIVE":
            return Verdict(False, f"lifecycle is {view.lifecycle}")
        if view.health != "OK":
            return Verdict(False, f"health is {view.health}")
        if view.active_operation_id:
            return Verdict(False, f"{view.active_operation_id} is running")
        return Verdict(True)


class DatabaseProfile(ResourceProfile):
    kind = ResourceKind.DATABASE
    resource_id = "database"
    profile = ATOMIC_LOGICAL_PROFILE
    requires_workload_hold = True
    adapter_judges_advance = True

    def plan_body(
        self, workflow: Workflow, seed: ResourceSeed, resource: ResourceState, scope: str
    ) -> dict[str, Any]:
        body = super().plan_body(workflow, seed, resource, scope)
        if self.selected_profile(seed) == DMS_PROFILE:
            body["replicationRef"] = seed.replication_ref
        return body

    def selected_profile(self, seed: ResourceSeed) -> str:
        assert isinstance(seed, DatabaseSeed)
        return seed.profile

    def check_before_hold(self, seed: ResourceSeed) -> bool:
        return self.selected_profile(seed) == ATOMIC_LOGICAL_PROFILE

    def needs_held_evidence(self, action: str, seed: ResourceSeed) -> bool:
        if self.check_before_hold(seed):
            return action in {"CUTOVER", "ROLLBACK"}
        return True

    def advance(self, view: MigrationView) -> Verdict:
        # With the RUNNING record and the probe evidence the platform writes, the adapter can
        # judge this itself, including its own stability window.
        decision = view.readiness("advance")
        return Verdict(decision.passed, ", ".join(decision.reasons))

    def unknown_budget(self, timings: Timings, seed: ResourceSeed) -> UnknownBudget:
        # DMS calls give up after a short wait and report UNKNOWN while Google keeps working, so
        # UNKNOWN then RECONCILE is the normal path here.
        if self.selected_profile(seed) == DMS_PROFILE:
            return UnknownBudget(seconds=timings.dms_unknown_deadline)
        return super().unknown_budget(timings, seed)


class RedisProfile(ResourceProfile):
    kind = ResourceKind.REDIS
    resource_id = "redis"
    profile = "redis-proxy-sync-v1"

    def _policy(self, seed: ResourceSeed) -> dict[str, Any]:
        # Its writes stop for under two seconds, so this adapter takes fractions.
        return {**super()._policy(seed), "maxQuiesceSeconds": seed.policy.max_quiesce_seconds}

    def advance(self, view: MigrationView) -> Verdict:
        settled = self._settled(view)
        if not settled.ok:
            return settled
        drain = (view.details.get("redis") or {}).get("drainStatus")
        if drain != "SUCCEEDED":
            return Verdict(False, f"source queue drain is {drain}")
        failed = view.advance_check_failures(ignoring=ORCHESTRATOR_EVIDENCE)
        return Verdict(not failed, ", ".join(failed))

    def rollback_extra(self, mode: str) -> dict[str, Any]:
        return {"mode": mode}


class ObjectStorageProfile(ResourceProfile):
    kind = ResourceKind.OBJECT_STORAGE
    resource_id = "object"
    profile = "object-copy-route-v1"

    def ready_for_cutover(self, view: MigrationView, seed: ResourceSeed) -> Verdict:
        ready = super().ready_for_cutover(view, seed)
        if not ready.ok:
            return ready
        # CUTOVER only waits for apps that have reported. With none reporting there is no guard,
        # and an app that never followed the route would keep writing to the source.
        live = int((view.details.get("object") or {}).get("appsLive") or 0)
        expected = seed.expected_apps or 0
        if live < expected:
            return Verdict(False, f"{live} of {expected} apps follow the route")
        return Verdict(True)

    def advance(self, view: MigrationView) -> Verdict:
        settled = self._settled(view)
        if not settled.ok:
            return settled
        # Reads span both buckets until FINALIZE, so the route is MIXED by design in this window.
        failed = view.advance_check_failures(ignoring=ORCHESTRATOR_EVIDENCE | {"ROUTE_MATCH"})
        return Verdict(not failed, ", ".join(failed))


PROFILES: dict[ResourceKind, ResourceProfile] = {
    profile.kind: profile for profile in (DatabaseProfile(), RedisProfile(), ObjectStorageProfile())
}
