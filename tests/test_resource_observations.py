from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from control_plane.adapters.client import AdapterUnreachable, MigrationView
from control_plane.api.resource_observations import project_resource
from control_plane.api.schemas import ResourceView
from control_plane.clock import stamp
from control_plane.domain.models import ResourceKind, ResourceState, WorkflowStatus
from tests.conftest import SETTINGS, World
from tests.fakes import FakeClock
from tests.test_api import Console


def redis_status(clock: FakeClock) -> dict[str, Any]:
    at = stamp(clock.now())
    return {
        "migrationId": "redis-live",
        "lifecycle": "ACTIVE",
        "health": "OK",
        "phase": "redis.CUTOVER",
        "transitionId": "transition-1",
        "topologyRevision": 1,
        "activeOperationId": None,
        "observed": {
            "observedAt": at,
            "route": "TARGET",
            "businessWriteAccess": "TARGET_ONLY",
            "targetWritesPossible": True,
        },
        "details": {
            "redis": {
                "producerRoute": "TARGET",
                "consumerRoute": "SOURCE",
                "drainStatus": "RUNNING",
                "proxy": {
                    "paused": False,
                    "inflight_writes": 2,
                    "open_tx": 0,
                    "pubsub_sessions": 3,
                    "observedAt": at,
                },
                "sync": {
                    "running": True,
                    "halted": False,
                    "direction": "REVERSE",
                    "copied": 350,
                    "backlog": 7,
                    "lag_ms": 12,
                    "observedAt": at,
                    "error": None,
                },
            }
        },
        "progress": {"completed": None, "total": None, "unit": "keys"},
        "readiness": {"advance": {"result": "UNKNOWN", "reasons": ["BUSINESS_PROBE"]}},
        "rollback": {"method": "REVERSE_SYNC", "reasons": []},
        "checks": [
            {
                "code": "RESOURCE_HEALTH",
                "stage": "ADVANCE",
                "decision": {"result": "PASS", "reasons": []},
                "observedAt": at,
                "validUntil": stamp(clock.now() + timedelta(seconds=10)),
                "transitionId": "transition-1",
                "topologyRevision": 1,
            }
        ],
    }


def state_for(raw: dict[str, Any], clock: FakeClock) -> ResourceState:
    return ResourceState(
        kind=ResourceKind.REDIS,
        migration_id="redis-live",
        last_status=raw,
        last_status_at=clock.now(),
        last_route="TARGET",
    )


def test_reports_proxy_worker_values_without_fabricating_copy_totals() -> None:
    clock = FakeClock()
    raw = redis_status(clock)
    original = deepcopy(raw)
    result = project_resource(state_for(raw, clock), now=clock.now())
    assert result.details["redis"]["proxy"]["paused"] is False
    assert result.details["redis"]["proxy"]["open_tx"] == 0
    assert result.details["redis"]["sync"]["backlog"] == 7
    assert result.details["redis"]["sync"]["freshness"] == "FRESH"
    assert result.streams == [
        {
            "channel": "REDIS_SYNC",
            "direction": "REVERSE",
            "state": "RUNNING",
            "freshness": "FRESH",
            "observedAt": raw["observed"]["observedAt"],
            "copied": 350,
            "backlog": 7,
            "lagMs": 12,
        }
    ]
    assert result.replication == {"reverse": {"state": "RUNNING", "backlog": 7}}
    assert result.progress == {"completed": None, "total": None, "unit": "keys"}
    assert result.checks[0]["currentResult"] == "PASS"
    assert result.business_write_access == "TARGET_ONLY"
    assert raw == original


def test_unreported_details_are_empty_and_expired_checks_are_not_current_passes() -> None:
    clock = FakeClock()
    raw = redis_status(clock)
    raw["details"] = {"redis": {"sync": None, "proxy": None}}
    raw["checks"][0]["validUntil"] = stamp(clock.now() - timedelta(seconds=1))
    result = project_resource(state_for(raw, clock), now=clock.now())
    assert result.details == {"redis": {}}
    assert result.replication is None and result.streams == []
    assert result.checks[0]["decision"]["result"] == "PASS"
    assert result.checks[0]["currentResult"] == "UNKNOWN"


def test_public_projection_drops_private_adapter_fields_and_old_transition_checks() -> None:
    clock = FakeClock()
    raw = redis_status(clock)
    raw["details"]["redis"]["proxy"]["password"] = "must-not-leak"
    raw["details"]["redis"]["sync"]["client"] = {"dsn": "must-not-leak"}
    raw["details"]["database"] = {"dsn": "must-not-leak", "sourceWriterSessions": 0}
    raw["checks"][0]["transitionId"] = "previous-transition"
    result = project_resource(state_for(raw, clock), now=clock.now())
    assert "must-not-leak" not in result.model_dump_json()
    assert result.details["database"]["sourceWriterSessions"] == 0
    assert result.checks[0]["currentResult"] == "UNKNOWN"


async def test_existing_observer_pipeline_is_returned_without_direct_repolling(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.api import routes

    workflow = await world.start()
    original = await routes._detail(world.platform.context, workflow)
    monkeypatch.setattr(routes, "_detail", AsyncMock(return_value=original))
    monkeypatch.setitem(
        ResourceView.model_fields, "observation", ResourceView.model_fields["observed_at"]
    )
    direct_reader = AsyncMock()
    monkeypatch.setattr(routes, "read_resource", direct_reader)
    result = await routes.get_workflow(workflow.id, None, world.platform.context)
    assert result is original
    direct_reader.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "stale",
        "worker_error",
        "no_timestamp",
        "paused",
        "no_direction",
        "unknown",
        "no_halted_flag",
    ],
)
def test_uncertain_worker_reports_do_not_become_live_streams(change: str) -> None:
    clock = FakeClock()
    raw = redis_status(clock)
    sync = raw["details"]["redis"]["sync"]
    if change == "stale":
        sync["observedAt"] = stamp(clock.now() - timedelta(seconds=60))
    elif change == "worker_error":
        sync["error"] = "SYNC_WORKER_UNREACHABLE"
    elif change == "no_timestamp":
        del sync["observedAt"]
    elif change == "paused":
        sync["halted"] = True
    elif change == "unknown":
        sync["freshness"] = "UNKNOWN"
    elif change == "no_halted_flag":
        del sync["halted"]
    else:
        del sync["direction"]
    result = project_resource(state_for(raw, clock), now=clock.now())
    assert not any(
        stream["state"] == "RUNNING" and stream["freshness"] == "FRESH" and stream.get("observedAt")
        for stream in result.streams
    )


def test_passes_database_and_object_observations_without_changing_their_shape() -> None:
    clock = FakeClock()
    raw = {
        "observed": {"observedAt": stamp(clock.now()), "route": "SOURCE"},
        "details": {
            "database": {
                "proxyPaused": True,
                "sourceWriterSessions": 0,
                "verification": {"mismatchCount": 0},
            },
            "object": {
                "apps": {"live": 12, "acked": 10, "recovering": False},
                "lastSuccessfulSyncAt": stamp(clock.now()),
            },
        },
        "replication": {"forward": {"state": "RUNNING", "backlog": 4}, "initialCopy": None},
    }
    result = project_resource(state_for(raw, clock), now=clock.now())
    assert result.details == raw["details"]
    assert result.replication == {"forward": {"state": "RUNNING", "backlog": 4}}


async def test_detail_refreshes_same_phase_telemetry_without_mutating_engine_state(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "observation" in ResourceView.model_fields:
        pytest.skip("the team observer pipeline owns resource reads on this branch")
    workflow = await world.start()
    raw = redis_status(world.clock)
    state = state_for(raw, world.clock)
    cached = deepcopy(state.last_status)
    await world.repo.save_resource(workflow.id, state)
    reader = AsyncMock(return_value=MigrationView(raw))
    monkeypatch.setattr(world.workflows.adapters[ResourceKind.REDIS], "migration", reader)
    console = Console(world, SETTINGS)
    try:
        await console.login()
        url = f"/v1/workflows/{workflow.id}"
        first = (await console.http.get(url)).json()["resources"][0]
        assert first["details"]["redis"]["proxy"]["paused"] is False
        raw["details"]["redis"]["proxy"]["paused"] = True
        raw["details"]["redis"]["sync"]["backlog"] = 2
        second = (await console.http.get(url)).json()["resources"][0]
        assert second["phase"] == first["phase"]
        assert second["details"]["redis"]["proxy"]["paused"] is True
        assert second["details"]["redis"]["sync"]["backlog"] == 2
        assert second["observation"]["freshness"] == "FRESH"
        stored = await world.repo.resource(workflow.id, ResourceKind.REDIS)
        assert stored.last_status == cached
        assert (await world.workflow(workflow.id)).version == workflow.version
        assert world.redis.received == []
    finally:
        await console.http.aclose()


@pytest.mark.parametrize("failure", ["unreachable", "timeout", "completed"])
async def test_failed_or_historical_reads_preserve_cached_values_without_live_motion(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if "observation" in ResourceView.model_fields:
        pytest.skip("the team observer pipeline owns resource reads on this branch")
    workflow = await world.start()
    raw = redis_status(world.clock)
    await world.repo.save_resource(workflow.id, state_for(raw, world.clock))
    if failure == "completed":
        await world.repo.save_workflow(
            workflow.model_copy(update={"status": WorkflowStatus.COMPLETED})
        )
    reader = AsyncMock(side_effect=AdapterUnreachable("https://secret-token@private-host"))
    if failure == "timeout":

        async def wait_forever(_: str) -> None:
            await asyncio.Event().wait()

        reader = AsyncMock(side_effect=wait_forever)
        monkeypatch.setattr("control_plane.api.resource_observations.READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(world.workflows.adapters[ResourceKind.REDIS], "migration", reader)
    console = Console(world, SETTINGS)
    try:
        await console.login()
        response = await console.http.get(f"/v1/workflows/{workflow.id}")
        assert response.status_code == 200
        resource = response.json()["resources"][0]
        assert resource["details"]["redis"]["sync"]["copied"] == 350
        assert resource["observation"]["freshness"] == "STALE"
        assert resource["streams"][0]["freshness"] == "STALE"
        assert "secret-token" not in response.text
        if failure == "completed":
            reader.assert_not_awaited()
        else:
            assert resource["observation"]["collectionError"] == "MODULE_OBSERVATION_UNAVAILABLE"
    finally:
        await console.http.aclose()
