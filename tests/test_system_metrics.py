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
                        "metric": {
                            "foxtrot_owner_id": records["env-own-aws"]["org_id"],
                            "deployment_id": dep,
                            "deployment_environment_name": env,
                        },
                        "value": [0, world.clock.now().timestamp() - 1],
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
        response = await client.get(
            "/v1/applications/goods/system-metrics/deployments",
            params={"include_inactive": True},
        )
        assert {
            row["environmentId"]: row["collection"] for row in response.json()["environments"]
        } == {
            "env-own-aws": "REPORTING",
            "env-own-onprem": "NOT_REPORTING",
        }

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


def register_shared(world):
    from control_plane.seed import Seed

    ctx = world.platform.context
    seed = ctx.seed.model_dump(by_alias=True)
    seed["systemDeployments"] = [
        {
            "environmentId": "env-123456789abc",
            "deploymentId": "goods-shared-aws",
            "ownerId": "shared-org",
            "environment": "aws",
        },
        {
            "environmentId": "env-abcdef123456",
            "deploymentId": "goods-shared-gcp",
            "ownerId": "shared-org",
            "environment": "gcp",
        },
    ]
    world.platform.context = replace(ctx, seed=Seed.model_validate(seed))


async def test_shared_metrics_work_without_embedded_provisioner_and_reject_other_targets(
    world, monkeypatch
):
    register_shared(world)
    seen = []

    async def backend(http, url, path, params):
        seen.append(params["query"])
        return "OK", {"status": "success", "data": {"resultType": "matrix", "result": []}}

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        client._transport.app.state.deployment_service = None
        root = "/v1/applications/goods/system-metrics"
        response = await client.get(
            root, params={"deployment": "shared:env-123456789abc", "instance": "api-1"}
        )
        assert response.status_code == 200
        assert seen and all(
            'foxtrot_owner_id="shared-org"' in query
            and 'deployment_id="goods-shared-aws"' in query
            and 'deployment_environment_name="aws"' in query
            and 'service_instance_id="api-1"' in query
            for query in seen
        )
        for deployment in ["env-123456789abc", "shared:env-000000000000", "env-other"]:
            assert (
                await client.get(root, params={"deployment": deployment, "instance": "api-1"})
            ).status_code == 404
        assert (
            await client.get(
                root.replace("goods", "other"),
                params={"deployment": "shared:env-123456789abc", "instance": "api-1"},
            )
        ).status_code == 404
    async with await client_for(world, False) as client:
        assert (
            await client.get(
                root, params={"deployment": "shared:env-123456789abc", "instance": "api-1"}
            )
        ).status_code == 401


async def test_shared_discovery_validates_full_identity_and_keeps_personal_access(
    world, monkeypatch
):
    register_shared(world)
    own = deployment_records()["env-own-aws"]
    own["status"] = "READY"
    queries = []

    async def backend(http, url, path, params):
        queries.append(params["query"])
        identities = [
            ("shared-org", "goods-shared-aws", "aws", 1),
            ("shared-org", "unregistered", "aws", 1),
            ("wrong-owner", "goods-shared-aws", "aws", 1),
            ("shared-org", "goods-shared-gcp", "gcp", 31),
            (own["org_id"], own["deployment_id"], "aws", 1),
        ]
        return "OK", {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "foxtrot_owner_id": owner,
                            "deployment_id": dep,
                            "deployment_environment_name": cloud,
                        },
                        "value": [0, world.clock.now().timestamp() - age],
                    }
                    for owner, dep, cloud, age in identities
                ],
            },
        }

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        client._transport.app.state.deployment_service.store.environments = lambda: [own]
        response = await client.get("/v1/applications/goods/system-metrics/deployments")
        body = response.json()
        assert body["state"] == "OK"
        assert [(row["environmentId"], row["access"]) for row in body["environments"]] == [
            ("shared:env-123456789abc", "shared"),
            ("env-own-aws", "personal"),
        ]
        assert 'deployment_id="unregistered"' not in queries[0]
        assert 'deployment_id="goods-shared-aws"' in queries[0]
        client._transport.app.state.deployment_service = None
        response = await client.get("/v1/applications/goods/system-metrics/deployments")
        assert [row["access"] for row in response.json()["environments"]] == ["shared"]


@pytest.mark.parametrize(
    "change", ["duplicate-id", "duplicate-identity", "unknown-environment", "empty-owner"]
)
def test_shared_registry_rejects_ambiguous_or_unregistered_configuration(world, change):
    from pydantic import ValidationError

    from control_plane.seed import Seed

    register_shared(world)
    body = world.platform.context.seed.model_dump(by_alias=True)
    first, second = body["systemDeployments"]
    if change == "duplicate-id":
        second["environmentId"] = first["environmentId"]
    elif change == "duplicate-identity":
        second.update({key: first[key] for key in ["ownerId", "deploymentId", "environment"]})
    elif change == "unknown-environment":
        second["environment"] = "unknown"
    else:
        first["ownerId"] = ""
    with pytest.raises(ValidationError):
        Seed.model_validate(body)


async def test_registered_deployments_remain_visible_without_samples(world, monkeypatch):
    register_shared(world)

    async def backend(*args):
        return "OK", {"status": "success", "data": {"resultType": "vector", "result": []}}

    monkeypatch.setattr(metrics, "read_backend", backend)
    async with await client_for(world) as client:
        client._transport.app.state.deployment_service = None
        root = "/v1/applications/goods/system-metrics/deployments"
        assert (await client.get(root)).json()["environments"] == []
        body = (await client.get(root, params={"include_inactive": True})).json()
        assert body["state"] == "OK"
        assert [row["environmentId"] for row in body["environments"]] == [
            "shared:env-123456789abc",
            "shared:env-abcdef123456",
        ]
        assert all(row["collection"] == "NOT_REPORTING" for row in body["environments"])

        async def unavailable(*args):
            return "UNAVAILABLE", {}

        monkeypatch.setattr(metrics, "read_backend", unavailable)
        body = (await client.get(root, params={"include_inactive": True})).json()
        assert body["state"] == "UNAVAILABLE"
        assert len(body["environments"]) == 2
        assert all(row["collection"] == "UNKNOWN" for row in body["environments"])
