"""An application without some resource: its cutover is skipped and nothing calls its adapter."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from control_plane.api.routes import _detail
from control_plane.domain.models import ResourceKind, StageKind, StageStatus, WorkflowStatus
from control_plane.domain.stages import PLAN, requires_approval
from control_plane.errors import ApiError
from control_plane.seed import Seed
from tests.conftest import FINALIZE_STAGE, SEED, World


def without(*names: str, adapters: bool = True) -> dict[str, Any]:
    seed = copy.deepcopy(SEED)
    for name in names:
        del seed["resources"][name]
        if adapters:
            del seed["adapters"][name]
    return seed


def actions(world: World, kind: ResourceKind) -> list[str]:
    return [body["action"] for body in world.adapters[kind].received]


def test_a_seed_may_leave_resources_out() -> None:
    seed = Seed.model_validate(without("database"))
    assert seed.managed == (ResourceKind.OBJECT_STORAGE, ResourceKind.REDIS)
    with pytest.raises(KeyError):
        seed.resources.of(ResourceKind.DATABASE)
    assert Seed.model_validate(without("redis", "object")).managed == (ResourceKind.DATABASE,)


def test_a_seed_needs_a_resource_and_an_adapter_for_each() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        Seed.model_validate(without("database", "redis", "object"))
    no_adapter = without("database")
    del no_adapter["adapters"]["redis"]
    with pytest.raises(ValidationError, match="REDIS is managed but has no adapter"):
        Seed.model_validate(no_adapter)
    own = without("database")
    own["resources"]["redis"]["adapter"] = own["adapters"].pop("redis")
    assert Seed.model_validate(own).adapter(ResourceKind.REDIS).url == "http://redis"
    # An adapter for a resource that is left out is accepted and never called.
    assert Seed.model_validate(without("database", adapters=False)).managed


def test_one_cutover_approval_always_precedes_the_first_route_or_traffic_change() -> None:
    every = frozenset(ResourceKind)
    for managed in (every, every - {ResourceKind.REDIS}, frozenset({ResourceKind.DATABASE})):
        executed = [
            index
            for index, stage in enumerate(PLAN)
            if stage.kind is not StageKind.OBJECT_CUTOVER or ResourceKind.OBJECT_STORAGE in managed
        ]
        gates = [index for index in executed if requires_approval(index, managed)]
        assert gates == [2 if ResourceKind.OBJECT_STORAGE in managed else 3]
        assert not requires_approval(FINALIZE_STAGE, managed)


async def test_a_migration_without_a_database_completes() -> None:
    world = World(seed=without("database"))
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)

    assert final.status is WorkflowStatus.COMPLETED, final.halt
    stages = {stage.kind: stage for stage in await world.repo.stages(workflow.id)}
    assert stages[StageKind.DATABASE_CUTOVER].status is StageStatus.SKIPPED
    assert all(
        stage.status is StageStatus.PASSED
        for kind, stage in stages.items()
        if kind is not StageKind.DATABASE_CUTOVER
    )
    assert world.db.received == [], "the DB adapter is never called"
    for kind in (ResourceKind.OBJECT_STORAGE, ResourceKind.REDIS):
        assert actions(world, kind) == [
            "PREPARE",
            "START_SYNC",
            "QUIESCE",
            "CUTOVER",
            "RESUME",
            "FINALIZE",
        ]
    assert world.traffic.history == [5, 50, 100]
    assert "STAGE_SKIPPED" in await world.event_types(workflow.id)


async def test_without_redis_cutover_still_waits_before_object_storage() -> None:
    world = World(seed=without("redis"))
    workflow = await world.start()
    stopped = await world.until(workflow.id, WorkflowStatus.WAITING_APPROVAL)

    assert stopped.status is WorkflowStatus.WAITING_APPROVAL
    assert "QUIESCE" not in actions(world, ResourceKind.DATABASE)
    assert stopped.current_stage == 2
    assert "CUTOVER" not in actions(world, ResourceKind.OBJECT_STORAGE)
    assert world.redis.received == []


async def test_commands_and_views_name_only_managed_resources() -> None:
    world = World(seed=without("database"))
    workflow = await world.start()
    halted = workflow.model_copy(update={"status": WorkflowStatus.HALTED})
    await world.repo.save_workflow(halted)
    with pytest.raises(ApiError) as refused:
        await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    assert refused.value.code == "RESOURCE_NOT_MANAGED"
    assert await world.workflows.bindings.plan(workflow, ResourceKind.DATABASE, None) is None
    detail = await _detail(world.platform.context, workflow)
    assert [row.kind for row in detail.resources] == [
        ResourceKind.REDIS,
        ResourceKind.OBJECT_STORAGE,
    ]
