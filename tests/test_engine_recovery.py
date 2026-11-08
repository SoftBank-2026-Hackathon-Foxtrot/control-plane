"""Rollback, abort and manual reconcile: the ways out of a stopped workflow."""

from __future__ import annotations

import copy

import pytest

from control_plane.domain.models import ResourceKind, WorkflowStatus
from control_plane.errors import ApiError
from tests.conftest import DB_STAGE, REDIS_STAGE, SEED, TRAFFIC_STAGE, World
from tests.fakes import Fault


async def cutover_with_reverse_replication() -> tuple[World, str]:
    seed = copy.deepcopy(SEED)
    seed["resources"]["database"]["policy"]["rollbackMode"] = "REVERSE_REQUIRED"
    world = World(seed=seed)
    world.db.reverse_supported = True
    workflow = await world.start()
    await world.until(workflow.id, WorkflowStatus.WAITING_APPROVAL)
    await world.approve(workflow.id)
    await world.until(workflow.id, WorkflowStatus.CUTOVER_COMPLETE)
    return world, workflow.id


async def test_full_rollback_restores_data_before_app_traffic_and_survives_restarts() -> None:
    world, workflow_id = await cutover_with_reverse_replication()
    world.restart_engine_every_tick = True
    await world.command(workflow_id, "ROLLBACK")
    final = await world.until(workflow_id, WorkflowStatus.ROLLED_BACK, WorkflowStatus.HALTED)
    assert final.status is WorkflowStatus.ROLLED_BACK, final.halt
    assert final.rolled_back == [
        ResourceKind.DATABASE,
        ResourceKind.REDIS,
        ResourceKind.OBJECT_STORAGE,
    ]
    assert world.traffic.current == 0
    assert await world.repo.active_workflow_id() is None
    assert all(adapter.only.lifecycle == "COMPLETED" for adapter in world.adapters.values())
    for adapter in world.adapters.values():
        assert adapter.executed["ROLLBACK"] == 1
        assert adapter.executed["FINALIZE"] == 1
    events = (await world.repo.events(workflow_id, limit=5000))[0]
    restored = [e.resource for e in events if e.type == "RESOURCE_ROLLED_BACK"]
    assert restored == final.rolled_back
    rollback_traffic = next(i for i, e in enumerate(events) if e.message.startswith("target 0%"))
    assert all(
        i < rollback_traffic for i, e in enumerate(events) if e.type == "RESOURCE_ROLLED_BACK"
    )


async def test_full_rollback_failure_stops_before_the_next_resource_and_can_retry() -> None:
    world, workflow_id = await cutover_with_reverse_replication()
    world.db.faults["ROLLBACK"] = Fault("FAILED")
    await world.command(workflow_id, "ROLLBACK")
    stopped = await world.until(workflow_id, WorkflowStatus.HALTED)
    assert stopped.rollback_all
    assert "ROLLBACK" not in [body["action"] for body in world.redis.received]
    assert world.traffic.current == 100
    for _ in range(100):
        if not (await world.workflow(workflow_id)).reconcile_requested:
            break
        await world.tick()
    await world.command(workflow_id, "ROLLBACK")
    final = await world.until(workflow_id, WorkflowStatus.ROLLED_BACK, WorkflowStatus.HALTED)
    assert final.status is WorkflowStatus.ROLLED_BACK, final.halt


async def test_full_rollback_refuses_a_database_without_reverse_replication(world: World) -> None:
    workflow = await world.start()
    await world.until(workflow.id, WorkflowStatus.WAITING_APPROVAL)
    await world.approve(workflow.id)
    await world.until(workflow.id, WorkflowStatus.CUTOVER_COMPLETE)
    await world.command(workflow.id, "ROLLBACK")
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert stopped.halt is not None and stopped.halt.code == "ROLLBACK_UNAVAILABLE"
    assert not stopped.rolled_back
    assert world.traffic.current == 100
    assert "ROLLBACK" not in [body["action"] for body in world.redis.received]


async def stop_after_object_moved(world: World) -> str:
    workflow = await world.start()
    await world.at_stage(workflow.id, 3)
    await world.command(workflow.id, "HALT")
    return workflow.id


async def test_object_rollback_returns_to_source_and_is_verified(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    final = await world.until(workflow_id, WorkflowStatus.HALTED)

    assert final.halt is not None, final
    assert final.halt.code == "ROLLBACK_COMPLETED"
    assert world.object.only.lifecycle == "RESTORED"
    assert final.rolled_back == [ResourceKind.OBJECT_STORAGE]
    actions = [body["action"] for body in world.object.received]
    assert actions[-2:] == ["ROLLBACK", "RESUME"]
    assert world.object.received[-1]["expectedLocation"] == "SOURCE"


async def test_after_a_rollback_the_same_plan_cannot_go_on(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    await world.until(workflow_id, WorkflowStatus.HALTED)
    with pytest.raises(ApiError) as error:
        await world.command(workflow_id, "RESUME")
    assert error.value.code == "NEW_PLAN_REQUIRED"


async def test_redis_rollback_carries_the_mode_and_the_object_one_does_not(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, TRAFFIC_STAGE)
    await world.command(workflow.id, "HALT")
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.REDIS)
    final = await world.until(workflow.id, WorkflowStatus.HALTED)

    assert final.halt is not None and final.halt.code == "ROLLBACK_COMPLETED", final.halt
    rollback = next(b for b in world.redis.received if b["action"] == "ROLLBACK")
    assert rollback["mode"] == "normal"
    assert world.redis.only.lifecycle == "RESTORED"


async def test_redis_going_back_before_the_database_moves_leaves_nothing_on_the_target(
    world: World,
) -> None:
    """Why Redis moves first: if it has to go back, the DB was never touched."""
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)
    await world.command(workflow.id, "HALT")
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.REDIS)
    final = await world.until(workflow.id, WorkflowStatus.HALTED)

    assert final.halt is not None and final.halt.code == "ROLLBACK_COMPLETED", final.halt
    assert world.redis.only.lifecycle == "RESTORED"
    assert world.db.only.lifecycle == "READY"
    assert "QUIESCE" not in [body["action"] for body in world.db.received]
    assert (await world.services.coordinator.hold()).desired.value == "RUN"


async def test_database_cannot_return_after_target_writes(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE + 1)  # the database has moved
    await world.command(workflow.id, "HALT")
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    final = await world.until(workflow.id, WorkflowStatus.HALTED)

    assert final.halt is not None and final.halt.code == "ROLLBACK_UNAVAILABLE"
    assert "ROLLBACK" not in [body["action"] for body in world.db.received]
    assert world.db.only.lifecycle == "ACTIVE"


async def test_database_rollback_before_target_writes_stops_the_workload_again(
    world: World,
) -> None:
    world.db.faults["CUTOVER"] = Fault("FAILED", code="TARGET_NOT_READY")
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)
    halted = await world.run_to_end(workflow.id)
    assert halted.halt is not None and halted.halt.code == "TARGET_NOT_READY"
    assert world.db.only.lifecycle == "QUIESCED"
    assert (await world.services.coordinator.hold()).desired.value == "HOLD"
    for _ in range(10):
        await world.tick()

    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    final = await world.until(workflow.id, WorkflowStatus.HALTED)

    assert final.halt is not None and final.halt.code == "ROLLBACK_COMPLETED", final.halt
    assert world.db.only.lifecycle == "RESTORED"
    assert (await world.services.coordinator.hold()).desired.value == "RUN"
    resume = world.db.received[-1]
    assert (resume["action"], resume["expectedLocation"]) == ("RESUME", "SOURCE")


async def test_failure_mode_needs_a_failing_target_and_a_person(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, TRAFFIC_STAGE)
    await world.command(workflow.id, "HALT")

    with pytest.raises(ApiError) as refused:
        await world.command(
            workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.REDIS, mode="failure"
        )
    assert refused.value.code == "CONFIRMATION_REQUIRED"

    # Confirmed by a person, but the application still works: the target is not dead.
    await world.command(
        workflow.id,
        "ROLLBACK_RESOURCE",
        resource=ResourceKind.REDIS,
        mode="failure",
        confirm_target_failure=True,
    )
    final = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert final.halt is not None and final.halt.code == "TARGET_FAILURE_NOT_CONFIRMED"
    assert "ROLLBACK" not in [body["action"] for body in world.redis.received]

    # An old or missing result is not evidence: the probes must actually be failing.
    world.prober.business_ok["gcp"] = False
    for _ in range(int(world.seed.timings.business_probe_interval) + 1):
        await world.tick()
    await world.command(
        workflow.id,
        "ROLLBACK_RESOURCE",
        resource=ResourceKind.REDIS,
        mode="failure",
        confirm_target_failure=True,
    )
    for _ in range(20):
        await world.tick()
        if "ROLLBACK" in [body["action"] for body in world.redis.received]:
            break
    rollback = next(b for b in world.redis.received if b["action"] == "ROLLBACK")
    assert rollback["mode"] == "failure"


async def test_only_redis_has_a_failure_mode(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    with pytest.raises(ApiError) as refused:
        await world.command(
            workflow_id,
            "ROLLBACK_RESOURCE",
            resource=ResourceKind.OBJECT_STORAGE,
            mode="failure",
            confirm_target_failure=True,
        )
    assert refused.value.code == "UNSUPPORTED_MODE"


async def test_abort_before_any_cutover_cancels_everything_and_frees_the_slot(
    world: World,
) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 2)
    await world.command(workflow.id, "HALT")
    await world.command(workflow.id, "ABORT")
    final = await world.until(workflow.id, WorkflowStatus.ABORTED)

    assert final.finished_at is not None
    # The database's CANCEL leaves its subscription; FINALIZE removes it for the next migration.
    assert world.db.only.lifecycle == "COMPLETED"
    assert world.db.executed["FINALIZE"] == 1
    for kind in (ResourceKind.REDIS, ResourceKind.OBJECT_STORAGE):
        assert world.adapters[kind].only.lifecycle == "CANCELLED"
        assert world.adapters[kind].executed["FINALIZE"] == 0
    assert await world.repo.active_workflow_id() is None
    assert (await world.start()).id != workflow.id


async def test_abort_still_finishes_if_the_cancelled_database_cannot_be_cleaned_up(
    world: World,
) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 2)
    await world.command(workflow.id, "HALT")
    world.db.faults["FINALIZE"] = Fault("REFUSE", code="PRECONDITION_FAILED", times=50)
    await world.command(workflow.id, "ABORT")
    await world.until(workflow.id, WorkflowStatus.ABORTED)

    assert world.db.only.lifecycle == "CANCELLED"
    assert "CANCEL_CLEANUP_FAILED" in await world.event_types(workflow.id)
    assert await world.repo.active_workflow_id() is None


async def test_abort_refuses_while_a_resource_is_on_its_target(world: World) -> None:
    """An open plan on the target would keep running beside the next workflow."""
    workflow_id = await stop_after_object_moved(world)
    await world.command(workflow_id, "ABORT")
    stopped = await world.until(workflow_id, WorkflowStatus.HALTED)

    assert stopped.halt is not None and stopped.halt.code == "ABORT_NOT_SAFE"
    assert any("OBJECT_STORAGE" in blocker for blocker in stopped.halt.blockers)
    assert world.object.only.lifecycle == "ACTIVE"
    assert await world.repo.active_workflow_id() == workflow_id


async def test_abort_after_a_rollback_closes_every_plan(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    await world.until(workflow_id, WorkflowStatus.HALTED)
    assert world.object.only.lifecycle == "RESTORED"

    await world.command(workflow_id, "ABORT")
    await world.until(workflow_id, WorkflowStatus.ABORTED)

    # RESTORED is finalized, so no reverse copy outlives the workflow.
    assert world.object.only.lifecycle == "COMPLETED"
    assert world.object.executed["FINALIZE"] == 1
    assert world.db.only.lifecycle == "COMPLETED"
    assert await world.repo.active_workflow_id() is None


async def test_abort_refuses_to_walk_away_from_a_paused_database(world: World) -> None:
    """CUTOVER 중 프록시가 멈추면 앱 업무를 임의로 재개하지 않습니다."""
    world.db.faults["CUTOVER"] = Fault("FAILED")
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)
    halted = await world.run_to_end(workflow.id)
    assert halted.halt is not None and "WORKLOAD_HELD" in halted.halt.blockers
    for _ in range(10):
        await world.tick()
    await world.command(workflow.id, "ABORT")
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)

    assert stopped.halt is not None and stopped.halt.code == "ABORT_NOT_SAFE"
    assert any("DATABASE" in blocker for blocker in stopped.halt.blockers)
    assert (await world.services.coordinator.hold()).desired.value == "HOLD"
    assert await world.repo.active_workflow_id() == workflow.id


async def test_abort_returns_users_to_the_source_if_the_data_is_there(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, REDIS_STAGE)
    await world.command(workflow.id, "HALT")
    assert world.traffic.current == 5
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    await world.until(workflow.id, WorkflowStatus.HALTED)
    await world.command(workflow.id, "ABORT")
    await world.until(workflow.id, WorkflowStatus.ABORTED)
    assert world.traffic.current == 0


async def test_abort_refuses_while_redis_has_moved(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)  # Redis has moved
    await world.command(workflow.id, "HALT")
    await world.command(workflow.id, "ABORT")
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert stopped.halt is not None and stopped.halt.code == "ABORT_NOT_SAFE"
    assert any("REDIS" in blocker for blocker in stopped.halt.blockers)
    assert world.traffic.current == 5


async def test_a_failed_rollback_can_be_tried_again(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    world.object.faults["ROLLBACK"] = Fault("FAILED", code="BUSY_NOW")
    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    first = await world.until(workflow_id, WorkflowStatus.HALTED)
    assert first.halt is not None and first.halt.code == "BUSY_NOW"
    for _ in range(10):
        await world.tick()

    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    second = await world.until(workflow_id, WorkflowStatus.HALTED)
    assert second.halt is not None and second.halt.code == "ROLLBACK_COMPLETED", second.halt


async def test_a_rollback_timed_out_once_gets_its_full_time_again(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    world.prober.business_ok["gcp"] = False
    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    first = await world.until(workflow_id, WorkflowStatus.HALTED)
    assert first.halt is not None and first.halt.code == "ROLLBACK_NOT_VERIFIED"

    world.prober.business_ok["gcp"] = True
    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.OBJECT_STORAGE)
    second = await world.until(workflow_id, WorkflowStatus.HALTED)
    assert second.halt is not None and second.halt.code == "ROLLBACK_COMPLETED", second.halt


async def test_rollback_that_cannot_hold_the_workload_lets_it_run(world: World) -> None:
    world.db.faults["CUTOVER"] = Fault("FAILED", code="TARGET_NOT_READY")
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)
    await world.run_to_end(workflow.id)
    for _ in range(10):
        await world.tick()
    await world.services.coordinator.release()
    for _ in range(3):
        await world.tick()
    world.workload.stuck.add("goods-aws-worker")
    await world.command(workflow.id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    final = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert final.halt is not None and final.halt.code == "HOLD_TIMEOUT"
    assert (await world.services.coordinator.hold()).desired.value == "RUN"


async def test_a_resource_that_never_moved_is_not_rolled_back(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    await world.command(workflow_id, "ROLLBACK_RESOURCE", resource=ResourceKind.DATABASE)
    final = await world.until(workflow_id, WorkflowStatus.HALTED)
    assert final.halt is not None and final.halt.code == "NOTHING_TO_ROLL_BACK"
    assert "QUIESCE" not in [body["action"] for body in world.db.received]
    assert (await world.services.coordinator.hold()).desired.value == "RUN"


async def test_a_reconcile_an_adapter_refused_is_reported_not_called_finished(
    world: World,
) -> None:
    workflow_id = await stop_after_object_moved(world)
    world.redis.faults["RECONCILE"] = Fault("REFUSE", code="FORBIDDEN", status=403)
    await world.command(workflow_id, "RECONCILE")
    for _ in range(15):
        await world.tick()
    current = await world.workflow(workflow_id)
    assert not current.reconcile_requested
    assert current.halt is not None and current.halt.code == "RECONCILE_INCOMPLETE"
    events = await world.event_types(workflow_id)
    assert "RECONCILE_FAILED" in events and "RECONCILE_FINISHED" not in events


async def test_manual_reconcile_changes_no_route(world: World) -> None:
    workflow_id = await stop_after_object_moved(world)
    route = world.object.route(world.object.only)
    await world.command(workflow_id, "RECONCILE")
    for _ in range(15):
        await world.tick()
    current = await world.workflow(workflow_id)
    assert current.status is WorkflowStatus.HALTED
    assert not current.reconcile_requested
    for adapter in world.adapters.values():
        assert adapter.executed["RECONCILE"] == 1
    assert world.object.route(world.object.only) == route
    assert "RECONCILE_FINISHED" in await world.event_types(workflow_id)
