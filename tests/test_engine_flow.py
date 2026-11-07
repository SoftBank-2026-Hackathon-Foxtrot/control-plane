"""The whole migration, start to finish, and what must hold along the way."""

from __future__ import annotations

from control_plane.domain.models import (
    ActorState,
    HoldDesired,
    ResourceKind,
    StageStatus,
    WorkflowStatus,
)
from control_plane.store.repository import Repository
from tests.conftest import DB_STAGE, FINALIZE_STAGE, REDIS_STAGE, World


def actions(world: World, kind: ResourceKind) -> list[str]:
    return [body["action"] for body in world.adapters[kind].received]


async def test_a_migration_completes_in_the_planned_order(world: World) -> None:
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)

    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert await world.repo.active_workflow_id() is None
    assert all(s.status is StageStatus.PASSED for s in await world.repo.stages(workflow.id))
    for kind in ResourceKind:
        assert actions(world, kind) == [
            "PREPARE",
            "START_SYNC",
            "QUIESCE",
            "CUTOVER",
            "RESUME",
            "FINALIZE",
        ]
    assert world.traffic.history == [5, 50, 100]
    moved = [
        event
        for event in (await world.repo.events(workflow.id, limit=5000))[0]
        if event.type == "RESOURCE_MOVED"
    ]
    assert [event.resource for event in moved] == [
        ResourceKind.OBJECT_STORAGE,
        ResourceKind.REDIS,
        ResourceKind.DATABASE,
    ]


async def test_cutover_never_overlaps(world: World) -> None:
    """One resource moves at a time: the next QUIESCE waits for the previous advance."""
    workflow = await world.start()
    await world.at_stage(workflow.id, REDIS_STAGE)
    assert world.object.only.lifecycle == "ACTIVE"
    assert world.redis.only.lifecycle == "READY"
    assert world.db.only.lifecycle == "READY"
    await world.at_stage(workflow.id, DB_STAGE)
    assert world.redis.only.lifecycle == "ACTIVE"
    assert world.db.only.lifecycle == "READY"


async def test_each_stage_that_cannot_be_undone_waits_for_a_person(world: World) -> None:
    workflow = await world.start()
    assert (await world.workflow(workflow.id)).status is WorkflowStatus.PLANNED
    for _ in range(5):
        await world.tick()
    assert not world.db.received, "nothing runs before the first approval"

    await world.approve(workflow.id)
    stopped = await world.until(workflow.id, WorkflowStatus.WAITING_APPROVAL)
    assert stopped.current_stage == REDIS_STAGE, "one approval starts the Redis and DB cutovers"
    assert "QUIESCE" not in actions(world, ResourceKind.REDIS)
    assert "QUIESCE" not in actions(world, ResourceKind.DATABASE)

    await world.approve(workflow.id)
    stopped = await world.until(workflow.id, WorkflowStatus.WAITING_APPROVAL)
    assert stopped.current_stage == FINALIZE_STAGE, "FINALIZE removes the way back"
    assert "FINALIZE" not in actions(world, ResourceKind.REDIS)


async def test_database_and_redis_cutovers_stop_the_workload(world: World) -> None:
    workflow = await world.start()
    held_during: set[int] = set()

    await world.approve(workflow.id)
    for _ in range(3000):
        current = await world.workflow(workflow.id)
        if current.status.terminal:
            break
        if current.status is WorkflowStatus.WAITING_APPROVAL:
            await world.approve(workflow.id)
        await world.tick()
        hold = await world.services.coordinator.hold()
        if hold.desired is HoldDesired.HOLD:
            held_during.add((await world.workflow(workflow.id)).current_stage)

    assert held_during == {REDIS_STAGE, DB_STAGE}
    assert (await world.services.coordinator.hold()).desired is HoldDesired.RUN


async def test_database_commands_carry_fresh_evidence_of_a_stopped_workload(world: World) -> None:
    workflow = await world.start()
    await world.run_to_end(workflow.id)

    held = [b for b in world.db.received if b["action"] in {"QUIESCE", "CUTOVER", "RESUME"}]
    cutover = next(body for body in held if body["action"] == "CUTOVER")
    assert cutover["quiesceRef"] in world.gate.gates
    assert all(
        body["quiesceRef"] not in world.gate.gates for body in held if body["action"] != "CUTOVER"
    )
    assert {body["transitionId"] for body in held} == {f"tx-{workflow.id}-database"}
    assert next(b for b in held if b["action"] == "RESUME")["expectedLocation"] == "TARGET"
    assert world.gate.catalog_published == 1
    assert world.gate.running, "the adapter needs RUNNING evidence to judge advance"


async def test_the_object_plan_reaches_the_application(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 1)
    directive = await world.services.coordinator.heartbeat(
        actor_id="goods-aws-api",
        instance_id="new",
        generation=0,
        state=ActorState.RUNNING,
        in_flight=0,
    )
    assert directive.object_migration_id == world.object.only.id


async def test_stability_window_restarts_when_a_probe_fails(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 2)
    moved_at = None
    # Fail probes as soon as the object adapter is active; recovery must restart the window.
    for _ in range(400):
        await world.tick()
        if world.object.only.lifecycle == "ACTIVE":
            moved_at = world.seconds
            break
    assert moved_at is not None
    world.prober.business_ok["gcp"] = False
    for _ in range(60):
        await world.tick()
    assert (await world.workflow(workflow.id)).current_stage == 2, "a failing app must not advance"
    world.prober.business_ok["gcp"] = True
    await world.at_stage(workflow.id, 3)
    assert world.seconds - moved_at >= 60 + world.seed.timings.stability_window


async def test_state_is_written_through_one_table_only(world: World) -> None:
    """A fresh repository over the same table sees everything: nothing lives in the engine."""
    workflow = await world.start()
    await world.at_stage(workflow.id, 2)
    other = Repository(world.table)
    assert (await other.workflow(workflow.id)) == (await world.repo.workflow(workflow.id))
    assert [r.kind for r in await other.resources(workflow.id)]
