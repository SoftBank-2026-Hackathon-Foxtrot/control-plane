from dataclasses import replace

import httpx
import pytest

from control_plane import system_metrics as metrics
from control_plane.main import create_api


async def client_for(world, authenticated=True):
    ctx = world.platform.context
    world.platform.context = replace(
        ctx,
        settings=ctx.settings.model_copy(
            update={"auth_disabled": authenticated, "host": "127.0.0.1"}
        ),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(create_api(world.platform, run_loops=False)),
        base_url="http://test",
    )


async def test_auth_scope_and_query_validation(world):
    root = "/v1/applications/goods/system-metrics"
    async with await client_for(world, False) as client:
        assert (await client.get(root + "/hosts?environment=aws")).status_code == 401
    async with await client_for(world) as client:
        good = {"environment": "aws", "host": "app-01", "minutes": "15"}
        assert (await client.get(root, params=good)).status_code == 200
        for params in (
            good | {"environment": "other"},
            good | {"category": "secrets"},
            good | {"minutes": "10000"},
            good | {"host": ""},
        ):
            assert (await client.get(root, params=params)).status_code in (404, 422)
        assert (await client.get(root.replace("goods", "other"), params=good)).status_code == 404


async def test_series_fixed_queries_and_gaps(world, monkeypatch):
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
                "environment": "aws",
                "host": 'app"} or secret{',
                "category": "network",
                "minutes": "15",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert [p[1] for p in body["series"][0]["points"][:4]] == [0, None, None, 1.5]
    assert body["series"][0]["points"][-1][1] is None
    for call in calls:
        assert 'application_id="goods"' in call["query"]
        assert 'deployment_environment_name="aws"' in call["query"]
        assert 'host_name="app\\"} or secret{"' in call["query"]
        assert "timestamp(" in call["query"]
        assert (call["end"] - call["start"]) / call["step"] <= 360


@pytest.mark.parametrize("payload", [{}, {"status": "error"}, {"status": "success", "data": None}])
async def test_backend_failure_is_not_zero(world, monkeypatch, payload):
    async def backend(*args):
        return "OK", payload

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        body = (
            await client.get("/v1/applications/goods/system-metrics?environment=aws&host=app")
        ).json()
    assert all(row["state"] == "UNAVAILABLE" and row["points"] == [] for row in body["series"])


async def test_host_freshness_and_unconfigured_state(world, monkeypatch):
    async with await client_for(world) as client:
        body = (
            await client.get("/v1/applications/goods/system-metrics/hosts?environment=aws")
        ).json()
        assert body == {"state": "NOT_CONFIGURED", "hosts": []}

    async def backend(*args):
        return "OK", {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"host_name": "app-01", "secret": "hidden"},
                        "value": [0, world.clock.now().timestamp() - 31],
                    },
                    {
                        "metric": {"host_name": "app-02"},
                        "value": [0, world.clock.now().timestamp() - 10],
                    },
                ],
            },
        }

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        response = await client.get("/v1/applications/goods/system-metrics/hosts?environment=aws")
    assert "hidden" not in response.text
    assert [h["stale"] for h in response.json()["hosts"]] == [True, False]
