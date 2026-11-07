"""Redis design 3-8: the application reaches Redis directly outside a migration, and the platform
moves every process onto the proxy before PREPARE and off it after FINALIZE or an abort."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from control_plane.domain.models import ActorState, RedisRoute, StageKind, WorkflowStatus
from tests.conftest import REDIS_STAGE, SEED, World, stage_index

PREPARE_STAGE = stage_index(StageKind.PREPARE)


def direct_seed() -> dict[str, Any]:
    seed = copy.deepcopy(SEED)
    seed["resources"]["redis"]["directWhenIdle"] = True
    return seed


@pytest.fixture
def direct() -> World:
    return World(seed=direct_seed())


async def test_without_the_setting_nothing_is_ordered(world: World) -> None:
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED
    hold = await world.services.coordinator.hold()
    assert hold.redis_route is RedisRoute.DIRECT and hold.redis_route_generation == 0
    assert "REDIS_PROXY_ENTERED" not in await world.event_types(workflow.id)


async def test_every_process_is_on_the_proxy_before_any_plan(direct: World) -> None:
    workflow = await direct.start()
    await direct.approve(workflow.id)
    seen_plan_before_entry = False
    for _ in range(200):
        await direct.tick()
        entered = "REDIS_PROXY_ENTERED" in await direct.event_types(workflow.id)
        if direct.redis.migrations and not entered:
            seen_plan_before_entry = True
        if entered:
            break
    assert not seen_plan_before_entry, "the Redis plan came before every process moved"
    assert direct.workload.routes() == {RedisRoute.PROXY}


async def test_a_whole_migration_enters_and_leaves_the_proxy(direct: World) -> None:
    workflow = await direct.start()
    await direct.run_until_approval(workflow.id, stage=REDIS_STAGE)
    assert direct.workload.routes() == {RedisRoute.PROXY}, "the cutovers run through the proxy"

    final = await direct.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    hold = await direct.services.coordinator.hold()
    assert hold.redis_route is RedisRoute.DIRECT and hold.redis_endpoint == "gcp"
    assert direct.workload.routes() == {RedisRoute.DIRECT}
    assert set(direct.workload.endpoint_used.values()) == {"gcp"}
    events = await direct.event_types(workflow.id)
    assert events.index("REDIS_PROXY_ENTERED") < events.index("PLAN_REGISTERED")
    assert events.index("REDIS_PROXY_LEFT") < events.index("WORKFLOW_COMPLETED")


async def test_a_process_that_keeps_a_direct_connection_stops_the_entry(direct: World) -> None:
    direct.workload.route_stuck.add("goods-aws-worker")
    workflow = await direct.start()
    await direct.approve(workflow.id)
    halted = await direct.until(workflow.id, WorkflowStatus.HALTED)

    assert halted.current_stage == PREPARE_STAGE
    assert halted.halt is not None and halted.halt.code == "REDIS_ENTRY_TIMEOUT"
    assert any("goods-aws-worker" in blocker for blocker in halted.halt.blockers)
    assert not direct.redis.migrations, "nothing was planned on a Redis still reached directly"
    hold = await direct.services.coordinator.hold()
    assert hold.redis_route is RedisRoute.DIRECT and hold.redis_endpoint == "aws"


async def test_entry_can_be_retried_once_the_process_moves(direct: World) -> None:
    direct.workload.route_stuck.add("goods-aws-worker")
    workflow = await direct.start()
    await direct.approve(workflow.id)
    await direct.until(workflow.id, WorkflowStatus.HALTED)

    direct.workload.route_stuck.clear()
    await direct.command(workflow.id, "RESUME")
    await direct.run_until_approval(workflow.id, stage=REDIS_STAGE)
    assert direct.workload.routes() == {RedisRoute.PROXY}


async def test_abort_before_a_cutover_goes_back_to_the_source_directly(direct: World) -> None:
    workflow = await direct.start()
    await direct.at_stage(workflow.id, 2)
    assert direct.workload.routes() == {RedisRoute.PROXY}
    await direct.command(workflow.id, "HALT")
    await direct.command(workflow.id, "ABORT")
    await direct.until(workflow.id, WorkflowStatus.ABORTED)

    hold = await direct.services.coordinator.hold()
    assert hold.redis_route is RedisRoute.DIRECT and hold.redis_endpoint == "aws"
    assert direct.workload.routes() == {RedisRoute.DIRECT}
    assert "REDIS_PROXY_LEFT" in await direct.event_types(workflow.id)


async def test_the_heartbeat_carries_the_route(direct: World) -> None:
    coordinator = direct.services.coordinator
    hold = await coordinator.request_redis_route("wf", RedisRoute.PROXY, None)
    directive = await coordinator.heartbeat(
        actor_id="goods-aws-api",
        instance_id="i-1",
        generation=0,
        state=ActorState.RUNNING,
        in_flight=0,
    )
    assert directive.redis_route is RedisRoute.PROXY
    assert directive.redis_route_generation == hold.redis_route_generation == 1
    again = await coordinator.request_redis_route("wf", RedisRoute.PROXY, None)
    assert again.redis_route_generation == 1, "the same order is not a new generation"
