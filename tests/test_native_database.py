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
from tests.conftest import SEED, World
from tests.fakes import Fault


@pytest.mark.parametrize(
    "change",
    [
        {"replicationRef": "dms:old"},
        {"profile": "unknown-profile"},
        {"policy": {"rollbackMode": "REVERSE_REQUIRED", "maxQuiesceSeconds": 60}},
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
