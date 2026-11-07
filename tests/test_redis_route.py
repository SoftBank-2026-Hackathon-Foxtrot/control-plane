"""The application stays on its fixed Redis proxy throughout the workflow."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from control_plane.domain.models import ActorState, RedisRoute, WorkflowStatus
from control_plane.seed import Seed
from tests.conftest import SEED, World


async def test_completion_keeps_the_fixed_proxy(world: World) -> None:
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED
    hold = await world.services.coordinator.hold()
    assert hold.redis_route is RedisRoute.PROXY and hold.redis_endpoint is None
    assert "REDIS_PROXY_LEFT" not in await world.event_types(workflow.id)


async def test_abort_keeps_the_fixed_proxy(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 2)
    await world.command(workflow.id, "HALT")
    await world.command(workflow.id, "ABORT")
    await world.until(workflow.id, WorkflowStatus.ABORTED)
    hold = await world.services.coordinator.hold()
    assert hold.redis_route is RedisRoute.PROXY and hold.redis_endpoint is None
    assert "REDIS_PROXY_LEFT" not in await world.event_types(workflow.id)


def test_obsolete_direct_when_idle_is_rejected() -> None:
    seed = copy.deepcopy(SEED)
    seed["resources"]["redis"]["directWhenIdle"] = True
    with pytest.raises(ValidationError, match="always use proxy"):
        Seed.model_validate(seed)


async def test_heartbeat_does_not_repeat_a_persisted_direct_order(world: World) -> None:
    coordinator = world.services.coordinator
    hold = await coordinator.hold()
    await world.repo.save_hold(
        hold.model_copy(update={"redis_route": RedisRoute.DIRECT, "redis_endpoint": "aws"}),
        expected_version=0,
    )
    directive = await coordinator.heartbeat(
        actor_id="goods-aws-api",
        instance_id="i-1",
        generation=0,
        state=ActorState.RUNNING,
        in_flight=0,
    )
    assert directive.redis_route is RedisRoute.PROXY and directive.redis_endpoint is None
    assert directive.redis_route_generation == 1
    persisted = await coordinator.hold()
    assert persisted.redis_route is RedisRoute.PROXY and persisted.redis_route_generation == 1
    assert directive.poll_after_ms == world.seed.timings.idle_poll_ms
    with pytest.raises(ValueError, match="fixed Redis proxy"):
        await coordinator.request_redis_route("wf", RedisRoute.DIRECT, "aws")
