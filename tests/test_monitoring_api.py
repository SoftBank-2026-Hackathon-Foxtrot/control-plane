from datetime import timedelta

import pytest

from control_plane import monitoring_api as api
from control_plane.clock import stamp
from control_plane.observations import ObservationStore
from tests.test_system_metrics import client_for


async def setup(world):
    workflow = await world.start()
    return workflow, {
        "from": stamp(world.clock.now() - timedelta(minutes=5)),
        "to": stamp(world.clock.now() + timedelta(seconds=1)),
    }


async def test_auth_workflow_and_range(world):
    workflow, params = await setup(world)
    path = f"/v1/workflows/{workflow.id}/telemetry/series"
    async with await client_for(world, False) as client:
        assert (await client.get(path, params=params | {"comp": "app"})).status_code == 401
    async with await client_for(world) as client:
        assert (
            await client.get(path.replace(workflow.id, "missing"), params=params | {"comp": "app"})
        ).status_code == 404
        assert (await client.get(path, params=params | {"comp": "secret"})).status_code == 422
        assert (
            await client.get(path, params=params | {"comp": "app", "from": "2020-01-01T00:00:00Z"})
        ).status_code == 422
        body = (await client.get(path, params=params | {"comp": "app"})).json()
        assert body["state"] == "NOT_CONFIGURED"
        assert all(all(value is None for value in row["shared"]) for row in body["metrics"])


@pytest.mark.parametrize("comp", ["app", "db", "redis", "obj"])
async def test_real_metrics_scopes_and_gaps(world, monkeypatch, comp):
    workflow, params = await setup(world)
    queries = []

    async def backend(http, url, path, args):
        queries.append(args["query"])
        return "OK", {
            "status": "success",
            "data": {
                "result": [
                    {"values": [[args["start"], "0"], [args["start"] + args["step"], "NaN"]]}
                ]
            },
        }

    monkeypatch.setattr(api, "read_backend", backend)
    async with await client_for(world) as client:
        body = (
            await client.get(
                f"/v1/workflows/{workflow.id}/telemetry/series", params=params | {"comp": comp}
            )
        ).json()
    assert body["state"] == "OK"
    assert len(body["metrics"]) == 4
    for query in queries:
        assert f'workflow_id="{workflow.id}"' in query
        assert "timestamp(" in query
    for row in body["metrics"]:
        side = "shared" if row["scope"] == "workflow" else "source"
        assert row[side][:2] == [0, None]
        if side == "shared":
            assert all(value is None for value in row["source"] + row["target"])


async def test_tempo_scoping_and_base64_ids(world, monkeypatch):
    workflow, params = await setup(world)
    trace = "a" * 32
    begin = int((world.clock.now() - timedelta(seconds=1)).timestamp() * 1e9)
    payload = {
        "trace": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "foxtrot-redis"}},
                            {"key": "foxtrot.module", "value": {"stringValue": "redis"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "spanId": "AQEBAQEBAQE=",
                                    "name": "migration.copy",
                                    "startTimeUnixNano": str(begin),
                                    "endTimeUnixNano": str(begin + 1_000_000),
                                    "attributes": [
                                        {
                                            "key": "workflow.id",
                                            "value": {"stringValue": workflow.id},
                                        }
                                    ],
                                },
                                {
                                    "spanId": "AgICAgICAgI=",
                                    "name": "private other workflow",
                                    "startTimeUnixNano": str(begin),
                                    "endTimeUnixNano": str(begin + 1_000_000),
                                    "attributes": [
                                        {"key": "workflow.id", "value": {"stringValue": "other"}}
                                    ],
                                },
                            ]
                        }
                    ],
                }
            ]
        }
    }

    async def backend(http, url, path, args):
        assert http.headers["Accept"] == "application/json"
        if path == "/api/search":
            assert workflow.id in args["q"]
            return "OK", {"traces": [{"traceID": trace}]}
        return "OK", payload

    monkeypatch.setattr(api, "read_backend", backend)
    async with await client_for(world) as client:
        body = (await client.get(f"/v1/workflows/{workflow.id}/traces", params=params)).json()
        assert len(body["traces"]) == 1 and body["traces"][0]["side"] == "shared"
        detail = await client.get(f"/v1/workflows/{workflow.id}/traces/{trace}")
        assert "private other" not in detail.text
        assert detail.json()["spans"][0]["spanId"] == "01" * 8
        assert len(detail.json()["spans"]) == 1
        payload["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"].pop(0)
        assert (await client.get(f"/v1/workflows/{workflow.id}/traces/{trace}")).status_code == 404


async def test_logs_pagination_and_trace_correlation(world):
    workflow, params = await setup(world)
    store = ObservationStore(world.platform.context.repo.table)
    for i in range(201):
        at = world.clock.now() - timedelta(seconds=250 - i)
        await store.save(
            workflow.id,
            "APP",
            {"observed": {}},
            at,
            source="OTEL",
            trace_id="a" * 32,
            span_id="b" * 16,
        )
    async with await client_for(world) as client:
        path = f"/v1/workflows/{workflow.id}/logs"
        first = (await client.get(path, params=params)).json()
        assert len(first["logs"]) == 200 and first["nextCursor"]
        assert first["logs"][0]["traceId"] == "a" * 32
        second = (await client.get(path, params=params | {"cursor": first["nextCursor"]})).json()
        assert len(second["logs"]) == 1
        assert second["logs"][0]["id"] not in {row["id"] for row in first["logs"]}
        assert not (await client.get(path, params=params | {"traceId": "c" * 32})).json()["logs"]


async def test_scope_reports_workflow_resources_without_credentials(world):
    from control_plane.domain.models import ResourceKind, ResourceState

    workflow = await world.start()
    await world.repo.save_resource(
        workflow.id,
        ResourceState(
            kind=ResourceKind.REDIS,
            migration_id="redis-current",
            binding_ref="binding:goods:redis",
            plan={
                "source": {
                    "host": "source.internal",
                    "port": 6379,
                    "secretRef": "never-expose",
                    "password": "never-expose",
                },
                "target": {
                    "host": "target.internal",
                    "database": "0",
                    "nested": {"token": "never-expose"},
                },
            },
        ),
    )
    path = f"/v1/workflows/{workflow.id}/monitoring-scope"
    async with await client_for(world, False) as client:
        assert (await client.get(path)).status_code == 401
    async with await client_for(world) as client:
        response = await client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["workflowId"] == workflow.id
        assert body["applicationId"] == workflow.application
        assert body["workloadScope"] == world.platform.context.seed.scope
        redis = next(row for row in body["resources"] if row["kind"] == "REDIS")
        assert redis["migrationId"] == "redis-current"
        assert redis["source"] == {"host": "source.internal", "port": 6379}
        assert redis["target"] == {"host": "target.internal", "database": "0"}
        assert "never-expose" not in response.text
        assert (await client.get(path.replace(workflow.id, "missing"))).status_code == 404


async def test_live_selector_excludes_history_and_stopped_collection(world):
    from control_plane.domain.models import WorkflowStatus

    base = await world.start()
    store = ObservationStore(world.repo.table)
    for index, (name, status, age, error) in enumerate(
        [
            ("live-one", "RUNNING", 1, None),
            ("live-two", "WAITING_APPROVAL", 2, None),
            ("halted-but-reporting", "HALTED", 1, None),
            ("stopped", "RUNNING", 60, None),
            ("error", "HALTED", 1, "OFFLINE"),
            ("planned", "PLANNED", 1, None),
            ("finished", "COMPLETED", 1, None),
            ("aborted", "ABORTED", 1, None),
        ]
    ):
        workflow = base.model_copy(
            update={
                "id": name,
                "version": 0,
                "status": WorkflowStatus(status),
                "created_at": world.clock.now() + timedelta(seconds=index),
            }
        )
        active = await world.repo.active_workflow_id()
        if active:
            await world.repo.release_active(active)
        await world.repo.create_workflow(workflow)
        await store.save(
            name,
            "APP",
            {
                "observed": {"observedAt": stamp(world.clock.now() - timedelta(seconds=age))},
                "desired": "RUN",
                "generation": 0,
                "actors": [],
            },
            world.clock.now(),
            error=error,
        )
    async with await client_for(world, False) as client:
        assert (await client.get("/v1/telemetry/workflows")).status_code == 401
    async with await client_for(world) as client:
        rows = (await client.get("/v1/telemetry/workflows")).json()
        assert [row["id"] for row in rows] == ["halted-but-reporting", "live-two", "live-one"]
        history = (await client.get("/v1/workflows")).json()
        assert {"planned", "finished", "aborted", "stopped"}.issubset(
            {row["id"] for row in history}
        )
        assert (await client.get("/v1/workflows/finished")).status_code == 200
        world.clock.advance(seconds=60)
        assert (await client.get("/v1/telemetry/workflows")).json() == []
