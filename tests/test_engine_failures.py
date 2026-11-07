"""What the design promises when things go wrong. Each test names one promise."""

from __future__ import annotations

import pytest

from control_plane.domain.models import ResourceKind, StageKind, StageStatus, WorkflowStatus
from control_plane.store.table import VersionConflict
from tests.conftest import CANARY_STAGE, DB_STAGE, FINALIZE_STAGE, REDIS_STAGE, TRAFFIC_STAGE, World
from tests.fakes import Fault


@pytest.mark.parametrize("kind", list(ResourceKind))
@pytest.mark.parametrize("action", ["PREPARE", "START_SYNC", "QUIESCE", "CUTOVER", "RESUME"])
async def test_a_lost_response_does_not_run_a_command_twice(
    world: World, kind: ResourceKind, action: str
) -> None:
    world.adapters[kind].faults[action] = Fault("LOST_RESPONSE")
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert world.adapters[kind].executed[action] == 1


async def test_a_restart_between_any_two_ticks_changes_nothing(world: World) -> None:
    world.restart_engine_every_tick = True
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    for adapter in world.adapters.values():
        assert all(count == 1 for count in adapter.executed.values()), adapter.executed


async def test_a_new_owner_waits_for_the_old_lease_to_expire(world: World) -> None:
    workflow = await world.start()
    await world.approve(workflow.id)
    await world.tick()
    before = len(world.object.received)
    from control_plane.engine.engine import Engine

    other = Engine(world.services, owner="other")
    await other.tick()
    assert len(world.object.received) == before, "the lease is still held"
    world.clock.advance(world.seed.timings.lease_ttl + 1)
    await other.tick()
    assert len(world.object.received) >= before


async def test_a_failed_command_stops_and_asks_the_adapter_to_compare(world: World) -> None:
    world.object.faults["CUTOVER"] = Fault("FAILED", code="FREEZE_REFUSED")
    workflow = await world.start()
    halted = await world.run_to_end(workflow.id)

    assert halted.status is WorkflowStatus.HALTED
    assert halted.halt is not None and halted.halt.code == "FREEZE_REFUSED"
    assert halted.current_stage == 2
    assert "CUTOVER" not in [k for k, n in world.object.executed.items() if n], "it never applied"
    for _ in range(10):
        await world.tick()
    assert world.object.executed["RECONCILE"] >= 1, "FAILED does not mean nothing changed"
    stage = await world.repo.stage(workflow.id, 2)
    assert stage is not None and stage.status is StageStatus.HALTED


async def test_going_on_after_a_failure_needs_a_person_and_a_new_key(world: World) -> None:
    world.object.faults["CUTOVER"] = Fault("FAILED")
    workflow = await world.start()
    await world.run_to_end(workflow.id)
    await world.until(workflow.id, WorkflowStatus.HALTED)
    for _ in range(10):
        await world.tick()
    assert sum(b["action"] == "CUTOVER" for b in world.object.received) == 1, "no automatic retry"

    await world.command(workflow.id, "RESUME")
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    cutovers = [b for b in world.object.received if b["action"] == "CUTOVER"]
    assert len(cutovers) == 2
    assert world.object.executed["CUTOVER"] == 1


async def test_an_unknown_result_blocks_everything_but_reconcile(world: World) -> None:
    world.redis.faults["CUTOVER"] = Fault("UNKNOWN")
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)

    assert final.status is WorkflowStatus.COMPLETED, final.halt
    actions = [body["action"] for body in world.redis.received]
    first = actions.index("CUTOVER")
    assert actions[first + 1] == "RECONCILE", "no change may follow an unknown result directly"
    assert world.redis.executed["CUTOVER"] == 1, "it had not applied, so it ran once after"
    assert "COMMAND_UNKNOWN" in await world.event_types(workflow.id)


async def test_an_unknown_result_that_did_apply_is_not_applied_again(world: World) -> None:
    world.redis.faults["CUTOVER"] = Fault("UNKNOWN_APPLIED")
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)

    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert world.redis.executed["CUTOVER"] == 1
    assert sum(b["action"] == "CUTOVER" for b in world.redis.received) == 1
    assert "COMMAND_CONFIRMED" in await world.event_types(workflow.id)


async def test_database_tolerates_repeated_unknowns_while_dms_works(world: World) -> None:
    seed = world.seed.resources.database
    seed.profile = "gcp-dms-postgres-pgbouncer-v1"
    seed.replication_ref = "dms:goods"
    assert seed.catalog is not None
    seed.catalog.dms_job = {"job": "projects/p/locations/l/migrationJobs/j"}
    world.db.faults["CUTOVER"] = Fault("UNKNOWN", times=5)
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert world.db.executed["CUTOVER"] == 1


async def test_redis_gives_up_on_unknowns_after_its_budget(world: World) -> None:
    world.redis.faults["CUTOVER"] = Fault("UNKNOWN", times=50)
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.HALTED
    assert final.halt is not None
    assert final.halt.code in {"RESULT_UNKNOWN", "COMMAND_ATTEMPTS_EXHAUSTED"}


async def test_a_stuck_command_stops_and_resuming_polls_the_same_operation(world: World) -> None:
    world.redis.faults["QUIESCE"] = Fault("STUCK")
    workflow = await world.start()
    halted = await world.run_to_end(workflow.id)
    assert halted.halt is not None and halted.halt.code == "COMMAND_TIMEOUT"

    await world.command(workflow.id, "RESUME")
    for _ in range(10):
        await world.tick()
    assert sum(b["action"] == "QUIESCE" for b in world.redis.received) == 1, "never a second one"


async def test_a_refusal_remembered_under_a_key_does_not_trap_the_retry(world: World) -> None:
    world.object.faults["QUIESCE"] = Fault("REFUSE", code="RESOURCE_BUSY", status=409)
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert world.object.executed["QUIESCE"] == 1


async def test_unreachable_adapter_waits_then_stops(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 2)
    world.object.unreachable = True
    final = await world.until(workflow.id, WorkflowStatus.HALTED, limit=1000)
    assert final.halt is not None and final.halt.code == "ADAPTER_UNREACHABLE"
    world.object.unreachable = False
    await world.command(workflow.id, "RESUME")
    assert (await world.run_to_end(workflow.id)).status is WorkflowStatus.COMPLETED


async def test_observed_routes_must_match_the_plan_before_a_cutover(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 1)
    world.redis.route_override = "TARGET"
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.HALTED
    assert final.halt is not None and final.halt.code == "ROUTE_MISMATCH"
    assert "QUIESCE" not in [body["action"] for body in world.object.received]


async def test_apps_must_follow_the_object_route_before_its_cutover(world: World) -> None:
    world.object.apps_live = 0
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.halt is not None and final.halt.code == "NOT_READY_FOR_CUTOVER"
    assert "QUIESCE" not in [body["action"] for body in world.object.received]

    world.object.apps_live = 12
    await world.command(workflow.id, "RESUME")
    assert (await world.run_to_end(workflow.id)).status is WorkflowStatus.COMPLETED


async def test_a_workload_that_will_not_stop_blocks_the_cutover_and_is_released(
    world: World,
) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)
    world.workload.stuck.add("goods-gcp-worker")
    final = await world.run_to_end(workflow.id)

    assert final.halt is not None and final.halt.code == "HOLD_TIMEOUT"
    assert any("goods-gcp-worker" in blocker for blocker in final.halt.blockers)
    assert "CUTOVER" not in [body["action"] for body in world.db.received]
    hold = await world.services.coordinator.hold()
    assert hold.desired.value == "RUN", "the workload must not stay stopped for nothing"

    world.workload.stuck.clear()
    await world.command(workflow.id, "RESUME")
    assert (await world.run_to_end(workflow.id)).status is WorkflowStatus.COMPLETED


async def test_a_workload_that_stops_reporting_mid_cutover_blocks_it(world: World) -> None:
    world.db.faults["CUTOVER"] = Fault("STUCK")
    workflow = await world.start()
    await world.run_until_approval(workflow.id, stage=REDIS_STAGE)
    await world.approve(workflow.id)
    for _ in range(600):  # Redis moves first
        await world.tick()
        if "CUTOVER" in [body["action"] for body in world.db.received]:
            break
    world.workload.silent = True
    final = await world.until(workflow.id, WorkflowStatus.HALTED, limit=400)
    assert final.halt is not None and final.halt.code == "WORKLOAD_NOT_HELD"


async def test_canary_failure_returns_traffic_by_itself(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 3)
    await world.tick()
    assert world.traffic.current == 5
    world.prober.light_ok["gcp"] = False
    final = await world.until(workflow.id, WorkflowStatus.HALTED)

    assert world.traffic.current == 0, "the revert does not wait for a person"
    assert final.halt is not None and final.halt.code == "TRAFFIC_GATE_FAILED"
    assert "TRAFFIC_REVERTED" in await world.event_types(workflow.id)

    world.prober.light_ok["gcp"] = True
    await world.command(workflow.id, "RESUME")
    await world.at_stage(workflow.id, CANARY_STAGE + 1)
    assert world.traffic.history == [5, 0, 5]


async def test_slow_target_fails_the_gate(world: World) -> None:
    world.prober.latency["gcp"] = 1000
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.current_stage == 3
    assert final.halt is not None and "p95" in final.halt.message
    assert world.traffic.current == 0


async def test_a_failed_step_later_returns_only_to_the_step_before(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, TRAFFIC_STAGE)
    await world.tick()
    assert world.traffic.current == 25
    for _ in range(int(world.seed.timings.observation_window) + 1):
        await world.tick()
    assert world.traffic.current == 50
    world.prober.light_ok["gcp"] = False
    await world.until(workflow.id, WorkflowStatus.HALTED)
    assert world.traffic.current == 25
    stages = await world.repo.stages(workflow.id)
    assert stages[TRAFFIC_STAGE].kind is StageKind.TRAFFIC


async def test_a_refused_weight_change_stops_after_retrying(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 3)
    world.traffic.fail = True
    final = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert final.halt is not None and final.halt.code == "TRAFFIC_CHANGE_FAILED"


async def test_halting_during_a_command_loses_nothing(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 2)
    await world.tick()
    await world.command(workflow.id, "HALT")
    sent = len(world.object.received)
    for _ in range(10):
        await world.tick()
    assert len(world.object.received) == sent, "halted means nothing new is sent"

    await world.command(workflow.id, "RESUME")
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert all(count == 1 for count in world.object.executed.values())


async def test_resuming_a_long_command_does_not_stop_it_at_once(world: World) -> None:
    world.redis.faults["QUIESCE"] = Fault("STUCK")
    workflow = await world.start()
    await world.run_until_approval(workflow.id, stage=REDIS_STAGE)
    await world.approve(workflow.id)
    for _ in range(5):
        await world.tick()
    await world.command(workflow.id, "HALT")
    world.clock.advance(world.seed.timings.command_timeout + 100)
    await world.command(workflow.id, "RESUME")
    for _ in range(20):
        await world.tick()
    current = await world.workflow(workflow.id)
    assert current.status is WorkflowStatus.RUNNING, current.halt


async def test_object_unknown_with_an_active_operation_is_reconciled(world: World) -> None:
    """The real adapter keeps the operation active and accepts RECONCILE. Waiting would hang."""
    world.object.faults["CUTOVER"] = Fault("UNKNOWN")
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert world.object.executed["RECONCILE"] >= 1
    assert world.object.executed["CUTOVER"] == 1


async def test_running_evidence_continues_until_finalize(world: World) -> None:
    """The DB adapter checks it again at FINALIZE, stages after the database moved."""
    workflow = await world.start()
    await world.run_until_approval(workflow.id, stage=FINALIZE_STAGE)
    written = len(world.gate.running)
    for _ in range(120):  # a person takes two minutes to approve
        await world.tick()
    assert len(world.gate.running) >= written + 10
    await world.approve(workflow.id)
    assert (await world.run_to_end(workflow.id)).status is WorkflowStatus.COMPLETED


async def test_finalize_is_refused_if_the_workload_stopped_reporting(world: World) -> None:
    workflow = await world.start()
    await world.run_until_approval(workflow.id, stage=FINALIZE_STAGE)
    world.workload.silent = True
    for _ in range(40):
        await world.tick()
    await world.approve(workflow.id)
    final = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert final.halt is not None and final.halt.code == "PRECONDITION_FAILED"


async def test_running_evidence_claims_only_what_is_reported(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE + 1)  # the database has moved
    written = len(world.gate.running)
    world.workload.processes = world.workload.processes[:-1]
    world.clock.advance(world.seed.timings.instance_ttl + 1)
    for _ in range(20):
        await world.tick()
    assert len(world.gate.running) == written, "a process is missing, so nothing may be claimed"


async def test_a_crash_right_after_the_plan_still_publishes_the_object_route(
    world: World,
) -> None:
    workflow = await world.start()
    original = world.services.coordinator.publish_object_migration
    calls = 0

    async def crash_once(workflow_id: str, migration_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VersionConflict("simulated crash before the write")
        await original(workflow_id, migration_id)

    world.services.coordinator.publish_object_migration = crash_once  # type: ignore[method-assign]
    await world.at_stage(workflow.id, 1)
    assert (await world.services.coordinator.hold()).object_migration_id == world.object.only.id


async def test_failures_right_after_a_weight_change_count(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 3)
    world.prober.light_ok["gcp"] = False  # already failing when the weight changes
    final = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert final.halt is not None and final.halt.code == "TRAFFIC_GATE_FAILED"
