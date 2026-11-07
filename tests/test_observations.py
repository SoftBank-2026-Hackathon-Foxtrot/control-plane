from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from control_plane.adapters.client import MigrationView
from control_plane.api.routes import _detail
from control_plane.clock import stamp
from control_plane.domain.models import ActorState, ResourceKind, ResourceState, WorkflowStatus
from control_plane.observation_projection import freshness, present, status_projection
from control_plane.observations import ObservationStore, made_progress, observation_state
from control_plane.public_probe import sample, summarize
from tests.conftest import World


async def test_old_and_failed_observations_preserve_last_success(world: World) -> None:
    store = ObservationStore(world.table)
    now = world.clock.now()
    data = {
        "migrationId": "m",
        "observed": {"observedAt": stamp(now)},
        "progress": {"completed": 10},
    }
    await store.save("w", "DATABASE", data, now)
    await store.save("w", "DATABASE", {"migrationId": "old"}, now - timedelta(seconds=1))
    await store.save("w", "DATABASE", None, now + timedelta(seconds=5), error="ADAPTER_UNREACHABLE")
    latest = await store.latest("w", "DATABASE")
    assert latest and latest["data"] == data
    assert latest["lastSuccessAt"] == stamp(now)
    assert observation_state(latest, now + timedelta(seconds=5))["freshness"] == "UNAVAILABLE"
    await store.save(
        "w",
        "DATABASE",
        {**data, "observed": {"observedAt": stamp(now - timedelta(seconds=1))}},
        now + timedelta(seconds=6),
    )
    assert (await store.latest("w", "DATABASE"))["data"] == data
    rows, _ = await store.history(
        "w", since=now + timedelta(seconds=4), until=now + timedelta(seconds=6)
    )
    assert len(rows) == 2


@pytest.mark.parametrize(
    "status", [WorkflowStatus.WAITING_APPROVAL, WorkflowStatus.HALTED, WorkflowStatus.ROLLING_BACK]
)
async def test_collection_is_independent_of_engine_and_preserves_resource_state(
    world: World, status: WorkflowStatus
) -> None:
    workflow = await world.start()
    workflow = await world.workflow(workflow.id)
    workflow.status = status
    await world.repo.save_workflow(workflow)
    state = ResourceState(
        kind=ResourceKind.DATABASE, migration_id="db", target_writes_possible=True
    )
    await world.repo.save_resource(workflow.id, state)
    raw = {
        "workflowId": workflow.id,
        "migrationId": "db",
        "lifecycle": "SYNCING",
        "health": "OK",
        "observed": {"observedAt": stamp(world.clock.now()), "route": "SOURCE"},
        "details": {"database": {"sourceWriterSessions": 2, "dsn": "secret"}},
        "password": "secret",
    }
    client = world.platform.observation_loop.adapters[ResourceKind.DATABASE]
    client.migration = AsyncMock(return_value=MigrationView(raw))
    await world.platform.observation_loop.tick()
    assert await world.repo.resource(workflow.id, ResourceKind.DATABASE) == state
    detail = await _detail(world.platform.context, workflow)
    observed = next(row for row in detail.resources if row.kind is ResourceKind.DATABASE)
    assert observed.details["database"]["sourceWriterSessions"] == 2
    assert "secret" not in observed.model_dump_json()
    assert observed.observation["freshness"] == "FRESH"
    world.clock.advance(16)
    assert (await _detail(world.platform.context, workflow)).resources[0].observation[
        "freshness"
    ] == "STALE"


async def test_terminal_tail_survives_active_slot_release(world: World) -> None:
    workflow = await world.start()
    workflow = await world.workflow(workflow.id)
    workflow.status, workflow.finished_at = WorkflowStatus.COMPLETED, world.clock.now()
    await world.repo.save_workflow(workflow)
    await world.repo.release_active(workflow.id)
    await world.repo.save_resource(
        workflow.id, ResourceState(kind=ResourceKind.REDIS, migration_id="r")
    )
    client = world.platform.observation_loop.adapters[ResourceKind.REDIS]
    client.migration = AsyncMock(
        return_value=MigrationView({"workflowId": workflow.id, "migrationId": "r"})
    )
    await world.platform.observation_loop.tick()
    assert client.migration.await_count == 1
    world.clock.advance(61)
    await world.platform.observation_loop.tick()
    assert client.migration.await_count == 1


def test_expired_checks_are_history_and_counters_do_not_fake_progress(world: World) -> None:
    now = world.clock.now()
    raw = {
        "migrationId": "r",
        "transitionId": "new",
        "topologyRevision": 1,
        "checks": [
            {
                "decision": {"result": "PASS"},
                "observedAt": stamp(now),
                "validUntil": stamp(now + timedelta(seconds=30)),
                "transitionId": "old",
                "topologyRevision": 1,
            }
        ],
    }
    shown = present(raw, now)
    assert shown["checks"][0]["decision"]["result"] == "PASS"
    assert shown["checks"][0]["currentResult"] == "UNKNOWN"
    assert freshness(stamp(now + timedelta(seconds=1)), now) == "UNKNOWN"
    assert not made_progress(
        {"migrationId": "r", "revision": 1}, {"migrationId": "r", "revision": 2}
    )
    old = {
        "migrationId": "r",
        "details": {"redis": {"sync": {"epoch": "1", "direction": "FORWARD", "copied": 5}}},
    }
    reset = {
        "migrationId": "r",
        "details": {"redis": {"sync": {"epoch": "2", "direction": "FORWARD", "copied": 6}}},
    }
    assert not made_progress(old, reset)
    assert status_projection({"details": {"compute": {"env": {"TOKEN": "secret"}}}}) == {
        "details": {"compute": {}}
    }


@pytest.mark.parametrize(
    "status, body, cache, ok, eligible",
    [
        (200, {"ok": True}, "DYNAMIC", True, True),
        (404, {"ok": True}, "DYNAMIC", False, False),
        (200, {"ok": False}, "DYNAMIC", False, False),
        (200, {"ok": True}, "HIT", True, False),
    ],
)
async def test_public_probe_validates_body_and_cache(
    world: World, status, body, cache, ok, eligible
) -> None:
    async def respond(request):
        assert request.url.params.get("_migration_probe")
        return httpx.Response(
            status,
            json=body,
            headers={
                "X-Served-By": "aws-web",
                "Cache-Control": "no-store",
                "CF-Cache-Status": cache,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await sample(
            http, "https://app.test/api/migration-probe", {"aws-web": "aws"}, world.clock
        )
    assert result["ok"] is ok
    assert result["locationEligible"] is eligible
    report = summarize([result], world.clock.now() - timedelta(seconds=60), world.clock.now())
    assert report["missingSamples"] == 59
    assert report["observedPercent"] == ({"aws": 100.0} if eligible else None)


async def test_missing_socket_reports_are_unknown_not_zero(world: World) -> None:
    coordinator = world.services.coordinator
    for index in range(4):
        await coordinator.heartbeat(
            actor_id="goods-aws-api",
            instance_id=str(index),
            generation=0,
            state=ActorState.RUNNING,
            in_flight=0,
            active_web_sockets=None if index == 3 else 2,
        )
    actor = (await coordinator.actor_statuses(0))[0]
    assert actor.active_web_sockets is None
    await coordinator.heartbeat(
        actor_id="goods-aws-api",
        instance_id="3",
        generation=0,
        state=ActorState.RUNNING,
        in_flight=0,
        active_web_sockets=1,
    )
    assert (await coordinator.actor_statuses(0))[0].active_web_sockets == 7
