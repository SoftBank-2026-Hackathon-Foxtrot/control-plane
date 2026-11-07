"""Stand-ins for everything outside the platform, faithful to how the real ones behave.

`FakeAdapter` serves the common API over an httpx transport, so the real client runs against it.
It reproduces the behaviour that makes these adapters hard to drive, including the parts that differ
between them. `tests/test_fake_contract.py` validates its responses against the team's contract.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator

from control_plane.adapters.client import AdapterClient
from control_plane.clock import stamp
from control_plane.dbgate import DbGateError
from control_plane.domain.models import (
    ActorState,
    BusinessProbe,
    HoldDesired,
    RedisRoute,
    ResourceKind,
)
from control_plane.seed import Seed
from control_plane.traffic import AppliedWeights, TrafficError
from control_plane.workload.coordinator import WorkloadCoordinator

CONTRACT = json.loads((Path(__file__).parent / "contract" / "common-api.json").read_text("utf-8"))


def contract_errors(schema_name: str, instance: dict[str, Any]) -> list[str]:
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": CONTRACT["components"]}
    validator = Draft202012Validator(schema)
    return [f"{list(error.path)}: {error.message}" for error in validator.iter_errors(instance)]


HELD_ACTIONS = {"QUIESCE", "CUTOVER", "RESUME", "ROLLBACK"}
ALLOWED = {
    "CREATED": ["PREPARE", "CANCEL"],
    "PREPARED": ["START_SYNC", "CANCEL"],
    "SYNCING": ["CANCEL"],
    "READY": ["QUIESCE", "CANCEL"],
    "QUIESCED": ["CUTOVER", "ROLLBACK"],
    "SWITCHED": ["RESUME", "ROLLBACK"],
    "ACTIVE": ["ROLLBACK", "FINALIZE"],
    "ROLLED_BACK_HELD": ["RESUME"],
    "RESTORED": ["FINALIZE"],
    "CANCELLED": [],
    "COMPLETED": [],
}
AFTER = {
    "PREPARE": "PREPARED",
    "START_SYNC": "SYNCING",
    "QUIESCE": "QUIESCED",
    "CUTOVER": "SWITCHED",
    "ROLLBACK": "ROLLED_BACK_HELD",
    "CANCEL": "CANCELLED",
    "FINALIZE": "COMPLETED",
}


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float = 1) -> None:
        self.moment += timedelta(seconds=seconds)


@dataclass
class Fault:
    """What the next command with this action does instead of simply working."""

    kind: str
    """FAILED, UNKNOWN, UNKNOWN_APPLIED, LOST_RESPONSE, REFUSE or STUCK."""
    code: str = "PRECONDITION_FAILED"
    status: int = 422
    times: int = 1


@dataclass
class Migration:
    id: str
    plan: dict[str, Any]
    revision: int = 1
    lifecycle: str = "CREATED"
    health: str = "OK"
    active_operation_id: str | None = None
    transition_id: str | None = None
    transition_state: str = "NONE"
    target_writes: bool = False
    unknown_operation: str | None = None
    drain: str = "NOT_STARTED"
    rolled_back: bool = False
    gets_until_ready: int = 2
    running_since: datetime | None = None
    running_last: datetime | None = None
    resumed_at: datetime | None = None


@dataclass
class Operation:
    id: str
    migration_id: str
    action: str
    status: str = "QUEUED"
    error: dict[str, Any] | None = None
    polls: int = 0
    body: dict[str, Any] = field(default_factory=dict)
    fault: Fault | None = None


class FakeAdapter:
    """One migration server. Its `kind` decides which of the real adapters it behaves like."""

    def __init__(self, kind: ResourceKind, clock: FakeClock, gate: FakeDbGate | None = None):
        self.kind = kind
        self.clock = clock
        self.gate = gate
        self.migrations: dict[str, Migration] = {}
        self.operations: dict[str, Operation] = {}
        self.idempotency: dict[str, tuple[str, int, dict[str, Any]]] = {}
        self.executed: Counter[str] = Counter()
        self.received: list[dict[str, Any]] = []
        self.faults: dict[str, Fault] = {}
        self.apps_live = 12
        self.unreachable = False
        self.bindings: dict[str, dict[str, Any]] = {}
        self.route_override: str | None = None
        self.reverse_supported = False
        self.binding_polls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> AdapterClient:
        http = httpx.AsyncClient(transport=self.transport(), base_url="http://adapter")
        return AdapterClient(http, "token")

    @property
    def only(self) -> Migration:
        assert len(self.migrations) == 1
        return next(iter(self.migrations.values()))

    # ---- routing -----------------------------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("adapter is down", request=request)
        assert request.headers["authorization"] == "Bearer token"
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == "/v1/capabilities":
            return _json(200, self.capabilities())
        if request.method == "POST" and path == "/v1/migrations":
            return self._idempotent(request, body, self._create)
        if request.method == "POST" and path == "/v1/bindings":
            return self._idempotent(request, body, self._register_binding)
        if request.method == "GET" and path.startswith("/v1/bindings/"):
            # It takes a moment: the real one registers in the background.
            binding = self.bindings[path.rsplit("/", 1)[1]]
            self.binding_polls += 1
            if self.binding_polls >= 2:
                binding["status"] = "ACTIVE"
            return _json(200, binding)
        if request.method == "GET" and path.startswith("/v1/operations/"):
            return self._get_operation(path.rsplit("/", 1)[1])
        if path.endswith("/actions"):
            migration = self.migrations[path.split("/")[3]]
            return self._action(request, migration, body)
        if request.method == "GET" and path.startswith("/v1/migrations/"):
            return _json(200, self.view(self.migrations[path.rsplit("/", 1)[1]]))
        raise AssertionError(f"unexpected {request.method} {path}")

    def _idempotent(
        self, request: httpx.Request, body: dict[str, Any], create: Any
    ) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        if not key:
            return _error(400, "INVALID_REQUEST", "Idempotency-Key is required")
        digest = json.dumps(body, sort_keys=True)
        if key in self.idempotency:
            saved_digest, status, response = self.idempotency[key]
            if saved_digest != digest:
                return _error(409, "IDEMPOTENCY_CONFLICT", "another body")
            return _json(status, response)
        status, response = create(body)
        self.idempotency[key] = (digest, status, response)
        return _json(status, response)

    def capabilities(self) -> dict[str, Any]:
        profile = {
            ResourceKind.DATABASE: "postgres-managed-logical-pgbouncer-v2",
            ResourceKind.REDIS: "redis-proxy-sync-v1",
            ResourceKind.OBJECT_STORAGE: "object-copy-route-v1",
        }[self.kind]
        profiles = [{"kind": self.kind.value, "profile": profile}]
        if self.kind is ResourceKind.DATABASE:
            profiles.append({"kind": self.kind.value, "profile": "postgres-logical-pgbouncer-v1"})
            profiles.append({"kind": self.kind.value, "profile": "gcp-dms-postgres-pgbouncer-v1"})
            profiles.append(
                {"kind": self.kind.value, "profile": "postgres-managed-logical-pgbouncer-v1"}
            )
        for entry in profiles:
            entry["actions"] = list(AFTER)
            modes = ["REVERSE_REQUIRED"]
            if self.kind is ResourceKind.DATABASE:
                modes = ["BEFORE_TARGET_WRITE"]
                if entry["profile"] == "postgres-logical-pgbouncer-v1" or (
                    entry["profile"] == "postgres-managed-logical-pgbouncer-v2"
                    and self.reverse_supported
                ):
                    modes.append("REVERSE_REQUIRED")
            entry["rollbackModes"] = modes
        return {"apiVersion": "v1", "profiles": profiles}

    # ---- plans -------------------------------------------------------------------------------
    def _create(self, plan: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        direct = "source" in plan
        if direct:
            plan = dict(plan)
            binding = next(
                binding
                for binding in self.bindings.values()
                if binding.get("bindingRef", binding["bindingId"]) == plan["bindingRef"]
            )
            assert binding["source"] == plan["source"]
            plan["sourceRef"] = f"endpoint:{plan['source']['host']}"
            plan["targetRef"] = f"endpoint:{plan['target']['host']}"
        if not direct and self.kind is not ResourceKind.REDIS:
            # Redis alone accepts a fractional maxQuiesceSeconds. The others hold to the contract.
            problems = contract_errors("CreateMigration", plan)
            assert not problems, problems
        required = {"workflowId", "resourceId", "kind", "profile", "sourceRef", "targetRef"}
        required |= {"bindingRef", "workloadScopeRef", "policy"}
        missing = required - set(plan)
        assert not missing, f"plan lacks {missing}"
        assert plan["kind"] == self.kind.value
        if self.kind is ResourceKind.DATABASE:
            assert plan["bindingRef"] in self.bindings
            assert plan["workloadScopeRef"] == self.bindings[plan["bindingRef"]]["workloadScopeRef"]
            if plan["profile"] == "gcp-dms-postgres-pgbouncer-v1":
                assert plan["replicationRef"]
            else:
                assert "replicationRef" not in plan
        else:
            assert "replicationRef" not in plan
        migration = Migration(id=f"mig-{uuid4().hex[:8]}", plan=plan)
        self.migrations[migration.id] = migration
        return 201, self.view(migration)

    def _register_binding(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        binding_id = f"bind-{uuid4().hex[:6]}"
        if "source" in body:
            self.bindings[binding_id] = {
                "bindingId": binding_id,
                "bindingRef": f"binding:{binding_id}"
                if self.kind is ResourceKind.REDIS
                else binding_id,
                "status": "REGISTERING",
                "kind": self.kind.value,
                "resourceId": body["resourceId"],
                "source": body["source"],
                "proxy": {
                    "host": f"{self.kind.value.lower()}-goods.internal",
                    "port": 6432 if self.kind is ResourceKind.DATABASE else 6379,
                    "database": body["source"]["database"],
                    "protocol": "postgresql" if self.kind is ResourceKind.DATABASE else "redis",
                },
                "workloadScopeRef": f"workload:{binding_id}",
            }
            return 202, {"bindingId": binding_id}
        self.bindings[binding_id] = {
            "bindingId": binding_id,
            "status": "REGISTERING",
            "databaseRef": body["databaseRef"],
            "workloadScopeRef": f"workload:{binding_id}",
        }
        return 202, {
            "operationId": f"op-{binding_id}",
            "bindingId": binding_id,
            "action": "REGISTER",
            "status": "QUEUED",
            "statusUrl": f"/v1/bindings/{binding_id}",
        }

    # ---- commands ----------------------------------------------------------------------------
    def allowed(self, migration: Migration) -> list[str]:
        """Copied from what each real adapter computes."""
        match self.kind:
            case ResourceKind.DATABASE:
                return self._allowed_database(migration)
            case ResourceKind.REDIS:
                return self._allowed_redis(migration)
            case _:
                return self._allowed_object(migration)

    def _allowed_database(self, migration: Migration) -> list[str]:
        if migration.active_operation_id:
            return []
        if migration.health != "OK":
            return ["RECONCILE"]
        actions = list(ALLOWED[migration.lifecycle])
        reverse = self.reverse_supported and migration.plan["policy"]["rollbackMode"] == (
            "REVERSE_REQUIRED"
        )
        if migration.lifecycle == "READY":
            actions = ["QUIESCE"]
            actions.append("CANCEL" if migration.transition_state == "NONE" else "ROLLBACK")
        if migration.lifecycle == "ACTIVE":
            actions = ["ROLLBACK", "FINALIZE"] if reverse else ["FINALIZE"]
        if (
            migration.plan["profile"] == "postgres-managed-logical-pgbouncer-v2"
            and migration.lifecycle == "SWITCHED"
            and migration.target_writes
            and not reverse
        ):
            actions = ["RESUME"]
        if migration.lifecycle == "CANCELLED":
            actions = ["FINALIZE"]
        return [*actions, "RECONCILE"]

    def _allowed_redis(self, migration: Migration) -> list[str]:
        if migration.unknown_operation:
            return ["RECONCILE"]
        if migration.active_operation_id:
            return []
        actions = list(ALLOWED[migration.lifecycle])
        if migration.lifecycle == "ACTIVE":
            actions = ["QUIESCE", "ROLLBACK", "FINALIZE"]
        if migration.lifecycle == "QUIESCED":
            actions = ["ROLLBACK"] if migration.target_writes else ["CUTOVER", "CANCEL"]
        return [*actions, "RECONCILE"]

    def _allowed_object(self, migration: Migration) -> list[str]:
        # An unknown outcome leaves the operation active, and only RECONCILE may run.
        if migration.unknown_operation or migration.active_operation_id:
            return ["RECONCILE"]
        return [*ALLOWED[migration.lifecycle], "RECONCILE"]

    def _action(
        self, request: httpx.Request, migration: Migration, body: dict[str, Any]
    ) -> httpx.Response:
        self.received.append(body)
        key = request.headers.get("idempotency-key")
        if not key:
            return _error(400, "INVALID_REQUEST", "Idempotency-Key is required")
        digest = json.dumps(body, sort_keys=True)
        # Replay comes before the revision check, as the contract requires.
        if key in self.idempotency:
            saved_digest, status, response = self.idempotency[key]
            if saved_digest != digest:
                return _error(409, "IDEMPOTENCY_CONFLICT", "another body")
            return _json(status, response)
        action = body.get("action", "")
        if self.kind is ResourceKind.OBJECT_STORAGE:
            known = {"action", "expectedRevision", "transitionId", "quiesceRef"}
            known |= {"expectedLocation", "deadlineMs"}
            if set(body) - known:
                return _error(400, "INVALID_REQUEST", f"unknown fields {set(body) - known}")
        if self.kind is not ResourceKind.REDIS and "mode" in body:
            return _error(400, "INVALID_REQUEST", "mode is not a field here")
        fault = self.faults.get(action)
        if fault and fault.kind == "REFUSE":
            self._spend(action)
            response = {"code": fault.code, "message": "refused", "blockers": ["why"]}
            if self.kind is ResourceKind.OBJECT_STORAGE:
                # This adapter remembers refusals under the key: only a new key can succeed.
                self.idempotency[key] = (digest, fault.status, response)
            return _json(fault.status, response)
        reconciling_object = action == "RECONCILE" and self.kind is ResourceKind.OBJECT_STORAGE
        if migration.active_operation_id and not reconciling_object:
            return _error(409, "RESOURCE_BUSY", f"{migration.active_operation_id} is running")
        if body.get("expectedRevision") != migration.revision:
            return _error(409, "REVISION_CONFLICT", f"revision is {migration.revision}")
        if action != "RECONCILE" and migration.unknown_operation:
            return _error(409, "UNKNOWN_STATE", "RECONCILE first")
        if action != "RECONCILE" and action not in self.allowed(migration):
            return _error(409, "INVALID_STATE", f"{action} not allowed in {migration.lifecycle}")
        if action in HELD_ACTIONS:
            if not body.get("transitionId") or not body.get("quiesceRef"):
                return _error(400, "INVALID_REQUEST", f"{action} needs transitionId and quiesceRef")
            if migration.transition_id and migration.transition_id != body["transitionId"]:
                return _error(409, "INVALID_STATE", "another transition")
            atomic_check = migration.plan[
                "profile"
            ] == "postgres-managed-logical-pgbouncer-v2" and action in {"QUIESCE", "RESUME"}
            if self.kind is ResourceKind.DATABASE and self.gate and not atomic_check:
                problem = self.gate.verify(body["quiesceRef"], migration.id)
                if problem:
                    return _error(409, problem, "the quiesce evidence does not hold")
        elif body.get("transitionId") and self.kind is ResourceKind.OBJECT_STORAGE:
            return _error(400, "INVALID_REQUEST", "transitionId is not a field of this action")
        if action == "FINALIZE" and self.kind is ResourceKind.DATABASE:
            refusal = self._finalize_refusal(migration)
            if refusal:
                return _error(422, *refusal)
        if action == "RESUME" and body.get("expectedLocation") != (
            "TARGET" if migration.lifecycle == "SWITCHED" else "SOURCE"
        ):
            return _error(409, "INVALID_STATE", "wrong expectedLocation")

        operation = Operation(
            id=f"op-{uuid4().hex[:8]}",
            migration_id=migration.id,
            action=action,
            body=body,
            fault=self._spend(action),
        )
        self.operations[operation.id] = operation
        migration.active_operation_id = operation.id
        if action in HELD_ACTIONS and not migration.transition_id:
            migration.transition_id = body["transitionId"]
            migration.transition_state = "OPEN"
        accepted = {
            "operationId": operation.id,
            "migrationId": migration.id,
            "action": action,
            "status": "QUEUED",
            "statusUrl": f"/v1/operations/{operation.id}",
        }
        self.idempotency[key] = (digest, 202, accepted)
        if operation.fault and operation.fault.kind == "LOST_RESPONSE":
            raise httpx.ReadTimeout("response lost", request=request)
        return _json(202, accepted)

    def _finalize_refusal(self, migration: Migration) -> tuple[str, str] | None:
        """`check_finalize`: fresh and stable RUNNING evidence (no waiting period)."""
        if migration.lifecycle != "ACTIVE":
            return None
        if not self._running_is_stable(migration):
            return "PRECONDITION_FAILED", "fresh business and stability evidence is needed"
        return None

    def _running_is_stable(self, migration: Migration) -> bool:
        now = self.clock.now()
        if migration.running_since is None or migration.running_last is None:
            return False
        fresh = (now - migration.running_last).total_seconds() < 30
        return fresh and (now - migration.running_since).total_seconds() >= 30

    def record_running(self, migration_id: str, *, passed: bool) -> None:
        migration = self.migrations[migration_id]
        now = self.clock.now()
        last = migration.running_last
        if not passed or last is None or (now - last).total_seconds() > 10:
            migration.running_since = now if passed else None
        migration.running_last = now if passed else None

    def _spend(self, action: str) -> Fault | None:
        fault = self.faults.get(action)
        if fault:
            fault.times -= 1
            if fault.times <= 0:
                del self.faults[action]
        return fault

    def _get_operation(self, operation_id: str) -> httpx.Response:
        operation = self.operations.get(operation_id)
        if operation is None:
            return _error(404, "NOT_FOUND", operation_id)
        operation.polls += 1
        migration = self.migrations[operation.migration_id]
        if operation.status in {"QUEUED", "RUNNING"}:
            operation.status = "RUNNING"
            if operation.polls >= 2 and not (operation.fault and operation.fault.kind == "STUCK"):
                self._finish(migration, operation)
        return _json(
            200,
            {
                "operationId": operation.id,
                "migrationId": operation.migration_id,
                "action": operation.action,
                "status": operation.status,
                "error": operation.error,
                "confirmedChanges": [],
            },
        )

    def _finish(self, migration: Migration, operation: Operation) -> None:
        fault = operation.fault
        if fault and fault.kind == "FAILED":
            operation.status = "FAILED"
            operation.error = {
                "code": fault.code,
                "message": "failed",
                "operationId": operation.id,
                "blockers": ["why"],
            }
        elif fault and fault.kind == "UNKNOWN":
            operation.status = "UNKNOWN"
            operation.error = {"code": "UNKNOWN_STATE", "message": "lost it", "blockers": []}
            migration.unknown_operation = operation.id
            migration.health = "UNKNOWN"
        elif fault and fault.kind == "UNKNOWN_APPLIED":
            self._effect(migration, operation)
            operation.status = "UNKNOWN"
            operation.error = {"code": "UNKNOWN_STATE", "message": "lost it", "blockers": []}
            migration.unknown_operation = operation.id
            migration.health = "UNKNOWN"
        else:
            self._effect(migration, operation)
            operation.status = "SUCCEEDED"
        if operation.status == "UNKNOWN" and self.kind is ResourceKind.OBJECT_STORAGE:
            return  # it stays active until a RECONCILE settles it
        if self.kind is not ResourceKind.REDIS or operation.status != "SUCCEEDED":
            self._release(migration)
        else:
            # Redis reports success before it frees the resource and moves the revision.
            self._pending_release = migration

    _pending_release: Migration | None = None

    def _release(self, migration: Migration) -> None:
        migration.active_operation_id = None
        migration.revision += 1

    def _effect(self, migration: Migration, operation: Operation) -> None:
        action = operation.action
        self.executed[action] += 1
        if action == "RECONCILE":
            self._settle_unknown(migration)
            return
        if action == "RESUME":
            location = operation.body["expectedLocation"]
            migration.resumed_at = self.clock.now()
            migration.lifecycle = "ACTIVE" if location == "TARGET" else "RESTORED"
            if location == "TARGET":
                migration.target_writes = True
                migration.drain = "SUCCEEDED"
            return
        if action == "QUIESCE" and migration.lifecycle == "ACTIVE":
            migration.lifecycle = "QUIESCED"
            return
        if action == "CUTOVER" and (
            self.kind is ResourceKind.REDIS
            or migration.plan["profile"] == "postgres-managed-logical-pgbouncer-v2"
        ):
            migration.target_writes = True
        if action == "ROLLBACK":
            migration.rolled_back = True
        migration.lifecycle = AFTER[action]
        if action == "FINALIZE" and "source" in migration.plan:
            binding = next(
                binding
                for binding in self.bindings.values()
                if binding.get("bindingRef", binding["bindingId"]) == migration.plan["bindingRef"]
            )
            binding["source"] = migration.plan["source" if migration.rolled_back else "target"]

    def _settle_unknown(self, migration: Migration) -> None:
        pending = self.operations.get(migration.unknown_operation or "")
        migration.unknown_operation = None
        migration.health = "OK"
        if self.kind is ResourceKind.OBJECT_STORAGE and pending:
            # Its worker finishes the command itself, and the unknown result turns into a real one.
            if not (pending.fault and pending.fault.kind == "UNKNOWN_APPLIED"):
                self._effect(migration, pending)
            pending.status = "SUCCEEDED"

    def route(self, migration: Migration) -> str:
        if self.route_override:
            return self.route_override
        lifecycle = migration.lifecycle
        if lifecycle in {"SWITCHED", "ACTIVE"}:
            return "MIXED" if self.kind is ResourceKind.OBJECT_STORAGE else "TARGET"
        if lifecycle == "COMPLETED" and not migration.rolled_back:
            return "TARGET"
        return "SOURCE"

    # ---- status ------------------------------------------------------------------------------
    def view(self, migration: Migration) -> dict[str, Any]:
        if migration.lifecycle == "SYNCING":
            if migration.gets_until_ready <= 0:
                migration.lifecycle = "READY"
                migration.revision += 1
            migration.gets_until_ready -= 1
        if self._pending_release is migration:
            self._pending_release = None
            self._release(migration)
            active = migration.active_operation_id
        else:
            active = migration.active_operation_id
        if self.kind is ResourceKind.DATABASE and migration.lifecycle in {"READY", "ACTIVE"}:
            migration.revision += 1  # the real one moves it on reads, too
        lifecycle = migration.lifecycle
        now = stamp(self.clock.now())
        route = self.route(migration)
        advance = self._advance(migration)
        rollback_ok = lifecycle in {"QUIESCED", "SWITCHED", "ACTIVE"} and (
            self.kind is not ResourceKind.REDIS
            or migration.drain == "SUCCEEDED"
            or lifecycle == "QUIESCED"
        )
        view: dict[str, Any] = {
            "migrationId": migration.id,
            "workflowId": migration.plan["workflowId"],
            "resourceId": migration.plan["resourceId"],
            "kind": self.kind.value,
            "revision": migration.revision,
            "lifecycle": lifecycle,
            "health": migration.health,
            "phase": f"fake.{lifecycle.lower()}",
            "activeOperationId": active,
            "transitionId": migration.transition_id,
            "transitionState": migration.transition_state,
            "topologyRevision": 1,
            "allowedActions": self.allowed(migration),
            "observed": {
                "observedAt": now,
                "route": route,
                "businessWriteAccess": "SOURCE_ONLY",
                "targetWritesPossible": migration.target_writes,
            },
            "readiness": {
                "cutover": _decide(lifecycle in {"READY", "QUIESCED"}),
                "resume": _decide(lifecycle in {"SWITCHED", "ROLLED_BACK_HELD"}),
                "rollback": _decide(rollback_ok, "NOT_NOW"),
                "advance": advance,
            },
            "rollback": {"method": "DIRECT", "reasons": []},
            "progress": {"completed": None, "total": None, "unit": "keys"},
            "checks": self._checks(migration, now, route),
        }
        if self.kind is ResourceKind.REDIS:
            view["details"] = {
                "redis": {
                    "dataRoute": route,
                    "producerRoute": route,
                    "consumerRoute": route,
                    "sourceConsumerAccess": "BLOCKED",
                    "drainStatus": migration.drain,
                }
            }
        if self.kind is ResourceKind.OBJECT_STORAGE:
            view["details"] = {"object": {"appsLive": self.apps_live, "readiness": {}}}
        return view

    def _advance(self, migration: Migration) -> dict[str, Any]:
        if self.kind is ResourceKind.REDIS:
            # Never PASS: it cannot see the orchestrator's evidence.
            return {"result": "UNKNOWN", "reasons": ["ORCHESTRATOR_EVIDENCE_REQUIRED"]}
        if self.kind is ResourceKind.OBJECT_STORAGE:
            # FAIL through ACTIVE, because the route reads as MIXED.
            return {"result": "FAIL", "reasons": ["ROUTE_MISMATCH"]}
        if migration.lifecycle == "ACTIVE" and self._running_is_stable(migration):
            return {"result": "PASS", "reasons": []}
        return {"result": "FAIL", "reasons": ["RESOURCE_NOT_ACTIVE"]}

    def _checks(self, migration: Migration, now: str, route: str) -> list[dict[str, Any]]:
        active = migration.lifecycle == "ACTIVE"
        orchestrator = {"result": "UNKNOWN", "reasons": ["ORCHESTRATOR_EVIDENCE_REQUIRED"]}
        decisions = {
            "RESOURCE_HEALTH": _decide(migration.health == "OK"),
            "ROUTE_MATCH": _decide(active and route == "TARGET", "ROUTE_MISMATCH"),
            "WORKLOAD_RUNNING": orchestrator,
            "BUSINESS_PROBE": orchestrator,
            "FOLLOWUP_COMPLETE": _decide(active),
            "STABILITY_WINDOW": _decide(active),
        }
        return [
            {
                "code": code,
                "stage": "ADVANCE",
                "required": True,
                "decision": decision,
                "evidenceRef": f"evidence:{code}",
                "observedAt": now,
                "validUntil": now,
                "transitionId": migration.transition_id,
                "topologyRevision": 1,
            }
            for code, decision in decisions.items()
        ]


def _decide(ok: bool, reason: str = "NOT_YET") -> dict[str, Any]:
    return {"result": "PASS", "reasons": []} if ok else {"result": "FAIL", "reasons": [reason]}


def _json(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


def _error(status: int, code: str, message: str) -> httpx.Response:
    return _json(status, {"code": code, "message": message, "operationId": None, "blockers": []})


class FakeDbGate:
    """Applies the checks `db_operator.control.verify_gate` applies to the evidence."""

    def __init__(self, seed: Seed, clock: FakeClock) -> None:
        self.seed = seed
        self.clock = clock
        self.adapter: FakeAdapter | None = None
        self.gates: dict[str, dict[str, Any]] = {}
        self.catalog_published = 0
        self.running: list[dict[str, Any]] = []
        self.refuse_next = False

    async def publish_catalog(self, **kwargs: Any) -> None:
        assert set(kwargs["participants"]) == set(self.seed.all_actors())
        self.catalog_published += 1

    def _check(self, participants: list[dict[str, object]], generation: int, state: str) -> None:
        expected = set(self.seed.all_actors())
        if {str(p["id"]) for p in participants} != expected:
            raise DbGateError("QUIESCE_EVIDENCE_EXPIRED: participants")
        for participant in participants:
            # The adapter trusts this record, so what it says is what is checked.
            observed = datetime.fromisoformat(str(participant["observedAt"]).replace("Z", "+00:00"))
            if (self.clock.now() - observed).total_seconds() >= 30:
                raise DbGateError(f"QUIESCE_EVIDENCE_EXPIRED: {participant}")
            if participant["generation"] != generation or participant["state"] != state:
                raise DbGateError(f"QUIESCE_EVIDENCE_INVALID: {participant}")
            if state == "HELD" and participant["inFlight"] != 0:
                raise DbGateError(f"QUIESCE_EVIDENCE_INVALID: {participant}")

    async def issue_held(
        self,
        *,
        migration_id: str,
        transition_id: str,
        generation: int,
        participants: list[dict[str, object]],
        held_since: datetime,
    ) -> str:
        if self.refuse_next:
            self.refuse_next = False
            raise DbGateError("STALE_GENERATION: refused")
        self._check(participants, generation, "HELD")
        assert held_since <= self.clock.now()
        reference = f"quiesce:{uuid4().hex}"
        self.gates[reference] = {"migration": migration_id, "at": self.clock.now()}
        return reference

    async def renew(self, reference: str, participants: list[dict[str, object]]) -> None:
        assert reference in self.gates
        self.gates[reference]["at"] = self.clock.now()

    async def record_running(
        self,
        *,
        migration_id: str,
        generation: int,
        participants: list[dict[str, object]],
        probe_result: str,
        probe_ref: str | None,
    ) -> None:
        self._check(participants, generation, "RUNNING")
        self.running.append({"result": probe_result, "ref": probe_ref})
        if self.adapter:
            self.adapter.record_running(migration_id, passed=probe_result == "PASS")

    def verify(self, reference: str, migration_id: str) -> str | None:
        gate = self.gates.get(reference)
        if gate is None or gate["migration"] != migration_id:
            return "QUIESCE_EVIDENCE_INVALID"
        if (self.clock.now() - gate["at"]).total_seconds() >= 30:
            return "QUIESCE_EVIDENCE_EXPIRED"
        return None


class FakeTraffic:
    def __init__(self) -> None:
        self.history: list[int] = []
        self.fail = False

    async def require_source_only(self, *, source_pool: str, target_pool: str) -> None:
        if self.fail or self.current != 0:
            raise TrafficError("source-only traffic is not confirmed")

    async def set_split(
        self,
        *,
        source_pool: str,
        target_pool: str,
        target_percent: int,
        migration_id: str | None = None,
        transition_id: str | None = None,
    ) -> AppliedWeights:
        if self.fail:
            raise TrafficError("cloudflare is down")
        self.history.append(target_percent)
        return AppliedWeights(
            {source_pool: 100 - target_percent, target_pool: target_percent},
            {source_pool: 100 - target_percent, target_pool: target_percent},
        )

    @property
    def current(self) -> int:
        return self.history[-1] if self.history else 0


class FakeProber:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.business_ok = {"aws": True, "gcp": True}
        self.light_ok = {"aws": True, "gcp": True}
        self.latency = {"aws": 100, "gcp": 110}
        self.ready = True

    def _env(self, app_url: str) -> str:
        return "aws" if "aws" in app_url else "gcp"

    async def is_ready(self, app_url: str) -> bool:
        return self.ready

    async def light(self, app_url: str) -> tuple[bool, int]:
        env = self._env(app_url)
        return self.light_ok[env], self.latency[env]

    async def business(self, env: str, app_url: str) -> BusinessProbe:
        now = self.clock.now()
        passed = self.business_ok[env]
        return BusinessProbe(
            probe_id=f"evidence:{env}:{uuid4().hex[:6]}",
            env=env,
            passed=passed,
            reason="" if passed else "join failed",
            started_at=now,
            finished_at=now,
        )


class SimulatedWorkload:
    """Every process of the application, doing what the heartbeat tells it."""

    def __init__(self, seed: Seed, coordinator: WorkloadCoordinator) -> None:
        self.coordinator = coordinator
        self.processes = [
            (actor, f"{actor}-{index}")
            for actor, count in seed.all_actors().items()
            for index in range(count)
        ]
        self.generation = 0
        self.desired = HoldDesired.RUN
        self.in_flight = 0
        self.stuck: set[str] = set()
        self.silent = False
        # Redis route each process follows (design 3-8), as the application's router does: the
        # order of a heartbeat is applied by the next one.
        self.route: dict[str, tuple[RedisRoute, str | None, int]] = {
            instance: (RedisRoute.DIRECT, None, 0) for _, instance in self.processes
        }
        self.route_stuck: set[str] = set()
        """Actors that keep a connection open on the route they leave."""
        self.endpoint_used: dict[str, str | None] = {}

    async def beat(self) -> None:
        if self.silent:
            return
        for actor, instance in self.processes:
            if actor in self.stuck:
                state, in_flight = ActorState.HOLDING, 1
            elif self.desired is HoldDesired.HOLD:
                state, in_flight = ActorState.HELD, 0
            else:
                state, in_flight = ActorState.RUNNING, self.in_flight
            route, _, route_generation = self.route[instance]
            directive = await self.coordinator.heartbeat(
                actor_id=actor,
                instance_id=instance,
                generation=self.generation,
                state=state,
                in_flight=in_flight,
                redis_route=route,
                redis_route_generation=route_generation,
                stale_connections=1 if actor in self.route_stuck and route_generation else 0,
            )
            self.desired = directive.desired
            self.generation = directive.generation
            self.route[instance] = (
                directive.redis_route,
                directive.redis_endpoint,
                directive.redis_route_generation,
            )
            self.endpoint_used[instance] = directive.redis_endpoint

    def routes(self) -> set[RedisRoute]:
        return {route for route, _, _ in self.route.values()}
