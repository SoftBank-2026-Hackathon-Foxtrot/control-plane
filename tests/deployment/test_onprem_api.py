"""Session-authenticated preparation -> deployment, without SSH or Docker side effects."""

import json
from types import SimpleNamespace

import pytest

from control_plane.deployment.api import org_id
from control_plane.deployment.provisioner.onprem import OnPremService
from control_plane.deployment.provisioner.onprem_store import OnPremStore
from control_plane.deployment.provisioner.service import Service
from control_plane.deployment.provisioner.store import Store
from control_plane.seed import Environment
from tests.conftest import SETTINGS
from tests.deployment.test_node import executor
from tests.deployment.test_node import node_config as node_config  # pytest fixture
from tests.deployment.test_onprem import FakeDriver, fixture
from tests.deployment.test_provisioner import CATALOG, ReadyChecker
from tests.test_api import Console


@pytest.fixture
async def registered_pc(world, tmp_path, node_config):
    console = Console(world, SETTINGS)
    await console.login()
    owner = org_id("github:4242", "goods")
    node_config["clouds"]["onprem"]["images"] = {"chiikawa-groupbuy": "ghcr.io/foxtrot/app:v1"}
    catalog, raw, body = fixture(tmp_path)
    for group in ("inventories", "profiles", "bindings"):
        for entry in raw[group].values():
            entry["orgs"] = [owner]
    catalog.path.write_text(json.dumps(raw))
    nodes = node_config["clouds"]["onprem"]["nodes"]
    nodes["pc"]["orgs"] = [owner]
    nodes["pc"]["preparation"] = {key: body[key] for key in ("target", "profileRef")}
    runner = executor(tmp_path, node_config)
    engine = Service(Store(tmp_path / "api.db"), runner, CATALOG, node_config, ReadyChecker())
    prep = OnPremService(
        OnPremStore(tmp_path / "api.db"),
        catalog,
        FakeDriver(),
        SimpleNamespace(),
        app_store=engine.store,
    )
    engine.onprem = runner.preparations = prep
    console.app.state.deployment_service = engine
    yield console, engine, runner, owner
    await console.http.aclose()
    engine.store.db.close()
    prep.store.db.close()


async def test_session_preparation_deployment_and_retained_data_lifecycle(registered_pc):
    console, engine, runner, owner = registered_pc
    targets = (await console.http.get("/v1/applications/goods/deployment-targets")).json()
    assert targets == {
        "orgId": owner,
        "onprem": [{"nodeId": "pc", "name": "pc", "requiresPreparation": True}],
    }
    headers = console.change_headers()
    prepared = await console.http.post(
        "/v1/applications/goods/onprem-preparations",
        json={"nodeId": "pc", "mode": "apply"},
        headers=headers,
    )
    assert prepared.status_code == 202, prepared.text
    repeated = await console.http.post(
        "/v1/applications/goods/onprem-preparations",
        json={"nodeId": "pc", "mode": "apply"},
        headers=headers,
    )
    assert repeated.json() == prepared.json()
    prep_id = prepared.json()["environmentId"]
    prep_path = f"/v1/provisioning/onprem/environments/{prep_id}"
    body = {
        "cloud": "onprem",
        "target": {"nodeId": "pc", "preparationId": prep_id},
        "app": {"workerCount": 0},
    }
    early = await console.http.post(
        "/v1/applications/goods/deployments", json=body, headers=console.change_headers()
    )
    assert early.status_code == 422
    assert runner.calls == []
    engine.run_next()
    assert (await console.http.get(prep_path)).json()["status"] == "INFRA_READY"
    assert (await console.http.get(prepared.headers["Location"])).json()["status"] == "SUCCEEDED"
    deployed = await console.http.post(
        "/v1/applications/goods/deployments", json=body, headers=console.change_headers()
    )
    assert deployed.status_code == 202, deployed.text
    saved = engine.store.environment(deployed.json()["environmentId"])
    assert saved["tfvars"]["app"]["image"] == "ghcr.io/foxtrot/app:v1"
    assert (
        saved["tfvars"]["app"]["port"] == CATALOG["templates"]["chiikawa-groupbuy"]["app"]["port"]
    )
    engine.run_next()
    engine.run_next()
    app_path = f"/v1/provisioning/environments/{deployed.json()['environmentId']}"
    env = (await console.http.get(app_path)).json()
    assert env["status"] == "READY"
    assert env["handoff"]["resources"]["database"]["provider"] == "SELF_MANAGED"
    assert env["handoff"]["resources"]["storage"]["kind"] == "seaweedfs"
    assert env["handoff"]["preparationEnvironmentId"] == prep_id
    assert env["handoff"]["migrationReadiness"]["status"] == "NOT_EVALUATED"
    assert "private-value" not in json.dumps(env)
    revision = engine.onprem.get(prep_id)["revision"]
    blocked = await console.http.post(
        prep_path + "/plan",
        json={"expectedRevision": revision, "intent": "cleanup"},
        headers=console.change_headers(),
    )
    assert blocked.status_code == 409 and blocked.json()["code"] == "PREPARATION_IN_USE"
    removed = await console.http.post(
        app_path + "/destroy", json={}, headers=console.change_headers()
    )
    assert removed.status_code == 202
    engine.run_next()
    assert (await console.http.get(app_path)).json()["status"] == "DESTROYED"
    assert engine.onprem.get(prep_id)["status"] == "INFRA_READY"  # data retained
    cleanup = await console.http.post(
        prep_path + "/plan",
        json={"expectedRevision": revision, "intent": "cleanup"},
        headers=console.change_headers(),
    )
    assert cleanup.status_code == 202, cleanup.text


async def test_deploy_for_requested_migration_after_preparing_the_node(registered_pc):
    console, engine, _, _ = registered_pc
    console.world.seed.environments["onprem"] = Environment(
        app_url="http://onprem-app:80", traffic_pool="onprem", actors={"onprem-api": 1}
    )
    prepared = await console.http.post(
        "/v1/applications/goods/onprem-preparations",
        json={"nodeId": "pc", "mode": "apply"},
        headers=console.change_headers(),
    )
    assert prepared.status_code == 202, prepared.text
    engine.run_next()
    prep_id = prepared.json()["environmentId"]
    assert engine.onprem.get(prep_id)["status"] == "INFRA_READY"

    requested = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": "aws", "targetEnv": "onprem"},
        headers=console.change_headers(),
    )
    assert requested.status_code == 201, requested.text
    workflow = requested.json()["workflow"]
    assert workflow["status"] == "PLANNED"
    assert workflow["createdBy"] == "hyunsu"
    assert "createdById" not in workflow
    for cloud in ("aws", "gcp"):
        rejected = await console.http.post(
            "/v1/applications/goods/deployments",
            json={"cloud": cloud, "workflowId": workflow["id"]},
            headers=console.change_headers(),
        )
        assert (rejected.status_code, rejected.json()["code"]) == (404, "NOT_FOUND")
    body = {
        "cloud": "onprem",
        "workflowId": workflow["id"],
        "target": {"nodeId": "pc", "preparationId": prep_id},
        "mode": "apply",
    }
    headers = console.change_headers()
    path = "/v1/applications/goods/deployments"
    deployed = await console.http.post(path, json=body, headers=headers)
    assert deployed.status_code == 202, deployed.text
    assert (await console.http.post(path, json=body, headers=headers)).json() == deployed.json()
    engine.run_next()
    engine.run_next()
    app_path = "/v1/provisioning/environments/" + deployed.json()["environmentId"]
    app = (await console.http.get(app_path)).json()
    assert (app["workflowId"], app["status"]) == (workflow["id"], "READY")
    assert app["handoff"]["preparationEnvironmentId"] == prep_id
    removal = await console.http.post(app_path + "/destroy", headers=console.change_headers())
    assert (removal.status_code, removal.json()["code"]) == (409, "WORKFLOW_BOUND")


@pytest.mark.parametrize("owner_id", ["github:7", None])
async def test_matching_login_alone_cannot_claim_a_workflow(registered_pc, owner_id):
    console, engine, _, _ = registered_pc
    console.world.seed.environments["onprem"] = Environment(
        app_url="http://onprem-app:80", traffic_pool="onprem", actors={"onprem-api": 1}
    )
    workflow = await console.world.platform.context.workflows.create(
        source_env="aws", target_env="onprem", actor="hyunsu", actor_id=owner_id
    )
    denied = await console.http.post(
        "/v1/applications/goods/deployments",
        json={"cloud": "onprem", "workflowId": workflow.id, "target": {"nodeId": "pc"}},
        headers=console.change_headers(),
    )
    assert denied.status_code == 404
    assert engine.store.environments() == []


async def test_targets_and_preparations_enforce_ownership_csrf_and_reference_integrity(
    registered_pc,
):
    console, engine, runner, _ = registered_pc
    response = await console.http.post(
        "/v1/applications/goods/onprem-preparations",
        json={"nodeId": "pc"},
        headers={"Idempotency-Key": "no-csrf"},
    )
    assert response.status_code == 403
    assert (await console.http.get("/v1/applications/other/deployment-targets")).status_code == 404
    response = await console.http.post(
        "/v1/applications/other/onprem-preparations",
        json={"nodeId": "pc"},
        headers=console.change_headers(),
    )
    assert response.status_code == 404
    for change in ({"org": "other"}, {"target": {"inventoryRef": "evil"}}):
        response = await console.http.post(
            "/v1/applications/goods/onprem-preparations",
            json={"nodeId": "pc", **change},
            headers=console.change_headers(),
        )
        assert response.status_code == 422
    prepared = await console.http.post(
        "/v1/applications/goods/onprem-preparations",
        json={"nodeId": "pc", "mode": "apply"},
        headers=console.change_headers(),
    )
    engine.run_next()
    prep_id = prepared.json()["environmentId"]
    spec = {"node_id": "pc", "org_id": "another-owner", "preparation_id": prep_id}
    # Even a registered alias cannot borrow another application's prepared endpoints.
    runner.nodes["pc"]["orgs"].append("another-owner")
    with pytest.raises(ValueError, match="another application"):
        runner.resolve(spec)
    raw = json.loads(engine.onprem.catalog.path.read_text())
    raw["inventories"]["inventory:pc"]["hosts"][0]["address"] = "100.64.1.3"
    engine.onprem.catalog.path.write_text(json.dumps(raw))
    spec["org_id"] = runner.nodes["pc"]["orgs"][0]
    with pytest.raises(ValueError, match="changed"):
        runner.resolve(spec)


@pytest.mark.parametrize(
    "override",
    [
        {"image": "evil/image:latest"},
        {"template": "other"},
        {"port": 9000},
        {"healthPath": "/other"},
    ],
)
async def test_onprem_keeps_fixed_application_policy(registered_pc, override):
    console, engine, runner, _ = registered_pc
    response = await console.http.post(
        "/v1/applications/goods/deployments",
        json={"cloud": "onprem", "target": {"nodeId": "pc"}, "app": override},
        headers=console.change_headers(),
    )
    assert response.status_code == 422
    assert engine.store.environments() == []
    assert runner.calls == []


async def test_another_users_node_is_not_listed_or_prepared(registered_pc):
    console, engine, runner, _ = registered_pc
    runner.nodes["pc"]["orgs"] = [org_id("github:9999", "goods")]
    result = await console.http.get("/v1/applications/goods/deployment-targets")
    assert result.json()["onprem"] == []
    response = await console.http.post(
        "/v1/applications/goods/onprem-preparations",
        json={"nodeId": "pc"},
        headers=console.change_headers(),
    )
    assert response.status_code == 404
    assert engine.onprem.environments() == []
