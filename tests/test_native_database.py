"""DB 에이전트 선택, 등록 전 차단 조건과 응답 유실 복구를 검사합니다."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from control_plane.adapters.profiles import PROFILES
from control_plane.domain.models import ResourceKind, WorkflowStatus
from control_plane.errors import ApiError
from control_plane.seed import ATOMIC_LOGICAL_PROFILE, DMS_PROFILE, MANAGED_LOGICAL_PROFILE, Seed
from tests.conftest import DB_STAGE, SEED, World
from tests.fakes import Fault


@pytest.mark.parametrize(
    "change",
    [
        {"replicationRef": "dms:old"},
        {"profile": "unknown-profile"},
        {"policy": {"rollbackMode": "unknown", "maxQuiesceSeconds": 60}},
        {"policy": {"rollbackMode": "BEFORE_TARGET_WRITE", "maxQuiesceSeconds": 1.8}},
        {"policy": {"rollbackMode": "BEFORE_TARGET_WRITE", "maxQuiesceSeconds": 301}},
        {"profile": DMS_PROFILE},
        {"profile": DMS_PROFILE, "replicationRef": "dms:goods"},
    ],
)
def test_invalid_database_settings_stop_before_any_command(change: dict[str, Any]) -> None:
    raw = deepcopy(SEED)
    raw["resources"]["database"].update(change)
    with pytest.raises(ValidationError):
        Seed.model_validate(raw)


def test_native_profile_rejects_a_leftover_dms_job() -> None:
    raw = deepcopy(SEED)
    raw["resources"]["database"]["catalog"]["dmsJob"] = {"job": "old"}
    with pytest.raises(ValidationError, match=r"rejects replicationRef and catalog\.dmsJob"):
        Seed.model_validate(raw)


async def test_default_workflow_uses_native_replication_without_dms(world: World) -> None:
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    plan = world.db.only.plan
    assert plan["profile"] == ATOMIC_LOGICAL_PROFILE
    assert "replicationRef" not in plan
    assert world.db.executed["CUTOVER"] == 1
    assert world.db.executed["RESUME"] == 1


@pytest.mark.parametrize("offered", [DMS_PROFILE, MANAGED_LOGICAL_PROFILE])
async def test_a_legacy_only_agent_is_rejected_before_creating_a_workflow(
    world: World, monkeypatch: pytest.MonkeyPatch, offered: str
) -> None:
    monkeypatch.setattr(
        world.db,
        "capabilities",
        lambda: {"profiles": [{"kind": "DATABASE", "profile": offered}]},
    )
    with pytest.raises(ApiError, match=ATOMIC_LOGICAL_PROFILE):
        await world.start()
    assert not world.db.migrations
    assert not world.db.bindings


async def test_native_unknown_result_stops_at_the_retry_limit(world: World) -> None:
    budget = PROFILES[ResourceKind.DATABASE].unknown_budget(
        world.seed.timings, world.seed.resources.database
    )
    assert budget.attempts == world.seed.timings.unknown_retry_limit
    assert budget.seconds is None
    world.db.faults["CUTOVER"] = Fault("UNKNOWN", times=50)
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.HALTED
    assert final.halt is not None and final.halt.code == "COMMAND_ATTEMPTS_EXHAUSTED"
    assert world.db.executed["RESUME"] == 0


@pytest.mark.parametrize("profile", [DMS_PROFILE, MANAGED_LOGICAL_PROFILE])
async def test_explicit_legacy_profiles_keep_their_original_flow(
    world: World, profile: str
) -> None:
    raw = deepcopy(SEED)
    database = raw["resources"]["database"]
    database["profile"] = profile
    if profile == DMS_PROFILE:
        database["replicationRef"] = "dms:goods"
        database["catalog"]["dmsJob"] = {"job": "projects/p/locations/l/migrationJobs/j"}
    selected = Seed.model_validate(raw).resources.database
    world.seed.resources.database = selected
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert world.db.only.plan["profile"] == profile
    assert ("replicationRef" in world.db.only.plan) == (profile == DMS_PROFILE)


def reverse_world() -> World:
    raw = deepcopy(SEED)
    raw["resources"]["database"]["policy"]["rollbackMode"] = "REVERSE_REQUIRED"
    world = World(seed=raw)
    world.db.reverse_supported = True
    return world


@pytest.mark.parametrize("profile", [DMS_PROFILE, MANAGED_LOGICAL_PROFILE])
def test_reverse_replication_rejects_legacy_profiles(profile: str) -> None:
    raw = deepcopy(SEED)
    database = raw["resources"]["database"]
    database["profile"] = profile
    database["policy"]["rollbackMode"] = "REVERSE_REQUIRED"
    if profile == DMS_PROFILE:
        database["replicationRef"] = "dms:goods"
        database["catalog"]["dmsJob"] = {"job": "old"}
    with pytest.raises(ValidationError, match="needs the self-managed or atomic v2 profile"):
        Seed.model_validate(raw)


@pytest.mark.parametrize("missing", ["rollbackModes", "actions", "selected_profile"])
async def test_reverse_requires_explicit_capabilities_on_the_selected_profile(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    world = reverse_world()
    capabilities = world.db.capabilities()
    selected = capabilities["profiles"][0]
    if missing == "selected_profile":
        # Another profile advertising it does not make the selected profile support it.
        capabilities["profiles"][1]["rollbackModes"] = ["REVERSE_REQUIRED"]
        selected["rollbackModes"] = ["BEFORE_TARGET_WRITE"]
    else:
        selected.pop(missing)
    monkeypatch.setattr(world.db, "capabilities", lambda: capabilities)
    with pytest.raises(ApiError) as refused:
        await world.start()
    assert refused.value.code == "UNSUPPORTED_ROLLBACK_MODE"
    assert await world.repo.active_workflow_id() is None
    assert not world.db.migrations and not world.db.bindings


@pytest.mark.parametrize("fault", [None, "LOST_RESPONSE", "UNKNOWN_APPLIED", "FAILED"])
async def test_reverse_database_rollback_after_target_writes_survives_restarts(
    fault: str | None,
) -> None:
    world = reverse_world()
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE + 1)
    assert world.db.only.target_writes
    assert world.db.only.plan["policy"]["rollbackMode"] == "REVERSE_REQUIRED"
    await world.command(workflow.id, "HALT")
    if fault:
        world.db.faults["ROLLBACK"] = Fault(fault)
    world.restart_engine_every_tick = True
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert stopped.halt is not None
    if fault == "FAILED":
        assert stopped.halt.code == "PRECONDITION_FAILED"
        assert (await world.services.coordinator.hold()).desired.value == "HOLD"
        return
    assert stopped.halt.code == "ROLLBACK_COMPLETED", stopped.halt
    assert world.db.only.lifecycle == "RESTORED"
    assert world.db.executed["ROLLBACK"] == 1
    assert (await world.services.coordinator.hold()).desired.value == "RUN"
    rollback = next(body for body in world.db.received if body["action"] == "ROLLBACK")
    assert rollback["quiesceRef"] in world.gate.gates
    assert "mode" not in rollback
    assert world.db.received[-1]["expectedLocation"] == "SOURCE"


@pytest.mark.parametrize("decision", ["FAIL", "UNKNOWN", "MISSING"])
async def test_reverse_rollback_requires_positive_readiness_and_keeps_workload_held(
    monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    world = reverse_world()
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE + 1)
    await world.command(workflow.id, "HALT")
    original = world.db.view

    def view(migration: Any) -> dict[str, Any]:
        result = original(migration)
        if decision == "MISSING":
            result["readiness"].pop("rollback")
        else:
            result["readiness"]["rollback"] = {
                "result": decision,
                "reasons": ["REVERSE_REPLICATION_NOT_READY"],
            }
        return result

    monkeypatch.setattr(world.db, "view", view)
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert stopped.halt is not None
    expected = "ROLLBACK_NOT_SAFE" if decision == "FAIL" else "ROLLBACK_NOT_READY"
    assert stopped.halt.code == expected, stopped.halt
    assert (await world.services.coordinator.hold()).desired.value == "HOLD"
    assert world.db.executed["ROLLBACK"] == 0
    assert not any(body["action"] == "ROLLBACK" for body in world.db.received)


async def test_reverse_readiness_can_recover_while_hold_evidence_is_renewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = reverse_world()
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE + 1)
    await world.command(workflow.id, "HALT")
    ready = False
    original = world.db.view

    def view(migration: Any) -> dict[str, Any]:
        result = original(migration)
        if not ready:
            result["readiness"]["rollback"] = {"result": "UNKNOWN", "reasons": ["CATCHING_UP"]}
        return result

    monkeypatch.setattr(world.db, "view", view)
    world.restart_engine_every_tick = True
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    # Exceeds the DB gate's 30-second freshness limit; waiting must renew the evidence.
    for _ in range(45):
        await world.tick()
    assert (await world.workflow(workflow.id)).status is WorkflowStatus.ROLLING_BACK
    assert (await world.services.coordinator.hold()).desired.value == "HOLD"
    assert world.db.executed["ROLLBACK"] == 0
    assert any(
        (world.clock.now() - evidence["at"]).total_seconds() < 10
        for evidence in world.gate.gates.values()
    )
    ready = True
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert stopped.halt is not None and stopped.halt.code == "ROLLBACK_COMPLETED"
    assert world.db.executed["ROLLBACK"] == 1
    assert (await world.services.coordinator.hold()).desired.value == "RUN"
