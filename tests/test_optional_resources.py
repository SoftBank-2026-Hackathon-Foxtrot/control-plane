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
from tests.conftest import DB_STAGE, FINALIZE_STAGE, REDIS_STAGE, SEED, World


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


def test_an_approval_at_a_skipped_stage_moves_to_the_next_one() -> None:
    every = frozenset(ResourceKind)
    no_redis = every - {ResourceKind.REDIS}
    no_stores = frozenset({ResourceKind.OBJECT_STORAGE})
    assert requires_approval(REDIS_STAGE, every)
    assert not requires_approval(DB_STAGE, every)
    assert requires_approval(DB_STAGE, no_redis), "the store approval is asked at the DB instead"
    traffic = next(i for i, stage in enumerate(PLAN) if stage.kind is StageKind.TRAFFIC)
    assert requires_approval(traffic, no_stores), "and at traffic when neither store is there"
    assert requires_approval(FINALIZE_STAGE, no_stores)


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
    assert world.traffic.history == [5, 25, 50, 100]
    assert "STAGE_SKIPPED" in await world.event_types(workflow.id)


async def test_without_redis_the_store_approval_is_asked_at_the_database() -> None:
    world = World(seed=without("redis"))
    workflow = await world.start()
    stopped = await world.run_until_approval(workflow.id, stage=DB_STAGE)

    assert stopped.status is WorkflowStatus.WAITING_APPROVAL
    assert "QUIESCE" not in actions(world, ResourceKind.DATABASE)
    redis_stage = await world.repo.stage(workflow.id, REDIS_STAGE)
    assert redis_stage is not None and redis_stage.status is StageStatus.SKIPPED
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
