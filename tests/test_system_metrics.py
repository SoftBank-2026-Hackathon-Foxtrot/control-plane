from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from control_plane import system_metrics as metrics
from control_plane.auth import LOCAL_USER
from control_plane.deployment.api import org_id
from control_plane.main import create_api


def deployment_records():
    owner = org_id(str(LOCAL_USER.id), "goods")
    return {
        "env-own-aws": {
            "id": "env-own-aws",
            "org_id": owner,
            "cloud": "aws",
            "deployment_id": "deploy-one",
        },
        "env-own-onprem": {
            "id": "env-own-onprem",
            "org_id": owner,
            "cloud": "onprem",
            "deployment_id": "deploy-two",
        },
        "env-other": {
            "id": "env-other",
            "org_id": "other-owner",
            "cloud": "aws",
            "deployment_id": "other-deploy",
        },
    }


async def client_for(world, authenticated=True):
    ctx = world.platform.context
    world.platform.context = replace(
        ctx,
        settings=ctx.settings.model_copy(
            update={"auth_disabled": authenticated, "host": "127.0.0.1"}
        ),
    )
    app = create_api(world.platform, run_loops=False)
    app.state.deployment_service = SimpleNamespace(
        store=SimpleNamespace(environment=deployment_records().get)
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


async def test_auth_and_exact_deployment_required(world):
    path = "/v1/applications/goods/system-metrics"
    valid = {"deployment": "env-own-aws", "instance": "api-1"}
    async with await client_for(world, False) as client:
        assert (await client.get(path, params=valid)).status_code == 401
    async with await client_for(world) as client:
        assert (await client.get(path, params=valid)).status_code == 200
        for invalid in (
            {"environment": "aws", "host": "app-01"},
            valid | {"deployment": "env-other"},
            valid | {"deployment": "missing"},
            valid | {"category": "unknown"},
            valid | {"minutes": 10000},
            valid | {"instance": ""},
        ):
            assert (await client.get(path, params=invalid)).status_code in (404, 422)
        assert (await client.get(path.replace("goods", "other"), params=valid)).status_code == 404


async def test_queries_never_aggregate_another_owner_deployment_or_instance(world, monkeypatch):
    calls = []

    async def backend(http, url, path, params):
        calls.append(params)
        start, step = params["start"], params["step"]
        return "OK", {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"values": [[start, "0"], [start + step, "NaN"], [start + step * 3, "1.5"]]}
                ],
            },
        }

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        response = await client.get(
            "/v1/applications/goods/system-metrics",
            params={
                "deployment": "env-own-onprem",
                "instance": 'app"} or secret{',
                "category": "network",
                "minutes": 15,
            },
        )
    assert response.status_code == 200
    assert [p[1] for p in response.json()["series"][0]["points"][:4]] == [0, None, None, 1.5]
    for call in calls:
        query = call["query"]
        assert 'application_id="goods"' in query
        assert f'foxtrot_owner_id="{org_id(str(LOCAL_USER.id), "goods")}"' in query
        assert 'deployment_id="deploy-two"' in query
        assert 'deployment_environment_name="onprem"' in query
        assert 'service_instance_id="app\\"} or secret{"' in query
        assert 'service_name="foxtrot-app-runtime"' in query
        assert "system_" not in query
        assert "timestamp(" in query
        assert (call["end"] - call["start"]) / call["step"] <= 360


@pytest.mark.parametrize("payload", [{}, {"status": "error"}, {"status": "success", "data": None}])
async def test_backend_failure_is_not_zero(world, monkeypatch, payload):
    async def backend(*args):
        return "OK", payload

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        body = (
            await client.get(
                "/v1/applications/goods/system-metrics",
                params={"deployment": "env-own-aws", "instance": "api"},
            )
        ).json()
    assert all(row["state"] == "UNAVAILABLE" and row["points"] == [] for row in body["series"])


async def test_instance_freshness_and_allowlisted_fields(world, monkeypatch):
    path = "/v1/applications/goods/system-metrics/instances?deployment=env-own-aws"
    async with await client_for(world) as client:
        assert (await client.get(path)).json() == {"state": "NOT_CONFIGURED", "instances": []}

    async def backend(*args):
        return "OK", {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "service_instance_id": "api-1",
                            "app_role": "api",
                            "secret": "hidden",
                        },
                        "value": [0, world.clock.now().timestamp() - 31],
                    },
                    {
                        "metric": {"service_instance_id": "worker-1", "app_role": "worker"},
                        "value": [0, world.clock.now().timestamp() - 10],
                    },
                ],
            },
        }

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        response = await client.get(path)
    assert "hidden" not in response.text
    assert [row["stale"] for row in response.json()["instances"]] == [True, False]


def test_disk_is_container_io_only():
    result = metrics.queries('application_id="goods"', "app-1", "disk")
    assert set(result) == {"disk_read", "disk_write"}
    assert all("filesystem" not in query for query, unit in result.values())


async def test_live_deployments_match_owner_cloud_and_recent_heartbeat(world, monkeypatch):
    records = deployment_records()
    for row in records.values():
        row["status"] = "READY"
    records["env-destroyed"] = {
        **records["env-own-aws"],
        "id": "env-destroyed",
        "deployment_id": "deleted",
        "status": "DESTROYED",
    }
    queries = []

    async def backend(http, url, path, params):
        queries.append(params["query"])
        return "OK", {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"deployment_id": dep, "deployment_environment_name": env},
                        "value": [0, 1],
                    }
                    for dep, env in [
                        ("deploy-one", "aws"),
                        ("deploy-two", "aws"),
                        ("other-deploy", "aws"),
                        ("deleted", "aws"),
                    ]
                ],
            },
        }

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        client._transport.app.state.deployment_service.store.environments = lambda: list(
            records.values()
        )
        response = await client.get("/v1/applications/goods/system-metrics/deployments")
        assert response.status_code == 200
        assert [row["environmentId"] for row in response.json()["environments"]] == ["env-own-aws"]
        assert "time() - 30" in queries[0]
        assert records["env-own-aws"]["org_id"] in queries[0]
        assert 'application_id="goods"' in queries[0]

        async def unavailable(*args):
            return "UNAVAILABLE", {}

        monkeypatch.setattr(metrics, "read_backend", unavailable)
        response = await client.get("/v1/applications/goods/system-metrics/deployments")
        assert response.json() == {"state": "UNAVAILABLE", "environments": []}


async def test_fresh_instances_are_not_hidden_by_old_exporters(world, monkeypatch):
    async def backend(*args):
        now = world.clock.now().timestamp()
        rows = [
            {"metric": {"service_instance_id": f"old-{index}"}, "value": [now, now - 90]}
            for index in range(70)
        ]
        rows.append({"metric": {"service_instance_id": "live"}, "value": [now, now - 1]})
        return "OK", {"status": "success", "data": {"resultType": "vector", "result": rows}}

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        response = await client.get(
            "/v1/applications/goods/system-metrics/instances?deployment=env-own-aws"
        )
        assert any(row["id"] == "live" and not row["stale"] for row in response.json()["instances"])
