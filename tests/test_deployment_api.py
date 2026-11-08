"""The fixed application shares deployment reads and sizing updates; request checks still apply."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from control_plane.deployment.api import APP_TEMPLATE, org_id
from control_plane.deployment.provisioner.node_config import resolve
from control_plane.deployment.provisioner.service import Service
from control_plane.deployment.provisioner.store import Store
from tests.deployment.test_node import node_config as node_config  # pytest fixture
from tests.deployment.test_provisioner import CATALOG, CONFIG, FakeTerraform, ReadyChecker
from tests.test_api import Console
from tests.test_api import console as console

DEPLOY = "/v1/applications/goods/deployments"


@pytest.fixture
async def deployment_console(
    console: Console, tmp_path: Path, node_config: dict
) -> AsyncIterator[Console]:
    engine = Service(
        Store(tmp_path / "provisioner.db"),
        FakeTerraform(tmp_path),
        copy.deepcopy(CATALOG),
        copy.deepcopy(CONFIG),
        checker=ReadyChecker(),
    )
    node = node_config["clouds"]["onprem"]["nodes"]["pc"]
    node["orgs"] = [org_id("github:4242", "goods")]
    node["imagePrefixes"] = ["registry.internal/apps/"]
    engine.config["clouds"]["onprem"]["nodes"] = {"lab": node}
    engine.tf.resolve = lambda spec: resolve(engine.config["clouds"]["onprem"]["nodes"], spec)
    console.app.state.deployment_service = engine
    await console.login()
    yield console
    engine.store.db.close()


def drain(engine: Service) -> None:
    while engine.run_next():
        pass


async def test_registration_removed_and_old_registration_not_listed(
    deployment_console: Console,
) -> None:
    console = deployment_console
    await console.world.table.put(
        "USER#github:4242",
        "APPLICATION#another-app",
        json.dumps({"id": "another-app", "image": "unrelated-image"}),
        expected_version=0,
    )
    apps = await console.http.get("/v1/applications")
    assert apps.status_code == 200
    assert [app["id"] for app in apps.json()] == ["goods"]
    response = await console.http.post(
        "/v1/applications",
        json={"id": "new-app", "name": "New", "image": "unrelated-image"},
        headers=console.change_headers(),
    )
    assert response.status_code == 405
    assert await console.world.table.get("USER#github:4242", "APPLICATION#new-app") is None


@pytest.mark.parametrize("action", ["reconcile", "destroy"])
@pytest.mark.parametrize("cloud", ["aws", "gcp"])
async def test_onprem_recovery_does_not_mutate_cloud_deployments(deployment_console, action, cloud):
    console = deployment_console
    engine = console.app.state.deployment_service
    created = await console.http.post(
        DEPLOY, json={"cloud": cloud}, headers=console.change_headers()
    )
    drain(engine)
    env_id = created.json()["environmentId"]
    before = engine.store.environment(env_id)
    calls = list(engine.tf.calls)
    response = await console.http.post(
        f"/v1/provisioning/environments/{env_id}/{action}",
        json={},
        headers=console.change_headers(),
    )
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")
    assert engine.tf.calls == calls
    assert engine.store.environment(env_id) == before


async def test_failed_onprem_registration_leaves_cloud_api_available(deployment_console):
    console = deployment_console
    engine = console.app.state.deployment_service
    engine.onprem_error = "ONPREM_CONFIGURATION_UNAVAILABLE"
    engine.config["clouds"].pop("onprem")
    response = await console.http.get("/v1/applications/goods/deployment-targets")
    assert response.status_code == 503
    assert response.json()["code"] == "ONPREM_CONFIGURATION_UNAVAILABLE"
    response = await console.http.post(
        DEPLOY, json={"cloud": "onprem"}, headers=console.change_headers()
    )
    assert response.status_code == 503
    for cloud in ("aws", "gcp"):
        response = await console.http.post(
            DEPLOY, json={"cloud": cloud}, headers=console.change_headers()
        )
        assert response.status_code == 202, response.text
        drain(engine)
        result = await console.http.get(response.headers["Location"])
        assert result.status_code == 200 and result.json()["status"] == "SUCCEEDED"


@pytest.mark.parametrize(("source", "target"), [("aws", "gcp"), ("gcp", "aws")])
async def test_cloud_workflow_keeps_its_wire_format_and_deployment_permissions(
    deployment_console: Console, source: str, target: str
) -> None:
    console = deployment_console
    created = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": source, "targetEnv": target},
        headers=console.change_headers(),
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    assert workflow["createdBy"] == "hyunsu"
    assert "createdById" not in workflow
    row = await console.world.table.get("WORKFLOW#" + workflow["id"], "META")
    assert row is not None and "createdById" not in json.loads(row.body)
    # The earlier branch persisted this field. It must not grant cloud deployment access.
    previous = json.loads(row.body) | {"createdById": "github:4242"}
    await console.world.table.put(
        row.pk, row.sk, json.dumps(previous), expected_version=row.version
    )
    read = await console.http.get("/v1/workflows/" + workflow["id"])
    assert read.status_code == 200 and "createdById" not in read.json()["workflow"]
    for cloud in (target, "onprem"):
        denied = await console.http.post(
            DEPLOY,
            json={"cloud": cloud, "workflowId": workflow["id"]},
            headers=console.change_headers(),
        )
        assert (denied.status_code, denied.json()["code"]) == (404, "NOT_FOUND")


@pytest.mark.parametrize("cloud", ["aws", "gcp", "onprem"])
async def test_api_deploys_fixed_image_without_registration(
    deployment_console: Console, cloud: str
) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    body = {"cloud": cloud}
    if cloud == "onprem":
        body["target"] = {"nodeId": "lab"}
    headers = console.change_headers()
    response = await console.http.post(DEPLOY, json=body, headers=headers)
    assert response.status_code == 202, response.text
    created = response.json()
    again = await console.http.post(DEPLOY, json=body, headers=headers)
    assert again.status_code == 202
    assert again.json() == created

    env = engine.store.environment(created["environmentId"])
    assert env["tfvars"]["app"]["image"] == CONFIG["clouds"][cloud]["images"][APP_TEMPLATE]
    assert env["tfvars"]["app"]["port"] == CATALOG["templates"][APP_TEMPLATE]["app"]["port"]
    assert env["org_id"] == org_id("github:4242", "goods")
    assert await console.world.table.get("USER#github:4242", "APPLICATION#goods") is None

    drain(engine)
    detail = await console.http.get(f"/v1/provisioning/environments/{created['environmentId']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "READY"
    job = await console.http.get(response.headers["Location"])
    assert job.status_code == 200
    assert job.json()["status"] == "SUCCEEDED"
    assert "logTail" not in job.json()
    assert [call[0] for call in engine.tf.calls] == ["plan", "apply"]
    listing = await console.http.get(DEPLOY)
    assert [item["environmentId"] for item in listing.json()["environments"]] == [
        created["environmentId"]
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("image", "other-image"), ("template", "other"), ("port", 9000), ("healthPath", "/other")],
)
@pytest.mark.parametrize("action", ["create", "update"])
async def test_fixed_application_fields_cannot_be_overridden(
    deployment_console: Console, field: str, value: object, action: str
) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    if action == "create":
        url = DEPLOY
        body = {"cloud": "aws", "app": {field: value}}
    else:
        created = await console.http.post(
            DEPLOY, json={"cloud": "aws"}, headers=console.change_headers()
        )
        drain(engine)
        url = f"/v1/provisioning/environments/{created.json()['environmentId']}/update"
        body = {"app": {field: value}}
    calls = list(engine.tf.calls)
    environments = engine.store.environments()
    response = await console.http.post(url, json=body, headers=console.change_headers())
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DEPLOYMENT"
    assert field in response.json()["message"]
    assert engine.store.environments() == environments
    assert engine.tf.calls == calls


async def test_sizing_update_keeps_fixed_runtime_and_environment(
    deployment_console: Console,
) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    created = await console.http.post(
        DEPLOY, json={"cloud": "aws"}, headers=console.change_headers()
    )
    env_id = created.json()["environmentId"]
    drain(engine)
    before = engine.store.environment(env_id)
    response = await console.http.post(
        f"/v1/provisioning/environments/{env_id}/update",
        json={"sizing": {"compute_disk_gib": 40}, "app": {"cpu": 2, "memoryMiB": 4096}},
        headers=console.change_headers(),
    )
    assert response.status_code == 202, response.text
    drain(engine)
    after = engine.store.environment(env_id)
    assert after["status"] == "READY"
    assert after["deployment_id"] == before["deployment_id"]
    assert after["tfvars"]["sizing"]["compute_disk_gib"] == 40
    assert after["tfvars"]["app"]["cpu"] == 2
    for field in ("image", "port", "entrypoint"):
        assert after["tfvars"]["app"][field] == before["tfvars"]["app"][field]


async def test_all_application_deployments_are_visible_and_sizing_updates_are_shared(
    deployment_console: Console,
) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    # These use the previous owner IDs without any application registration records.
    jobs = []
    for user, app in [("github:4242", "goods"), ("github:4242", "other"), ("github:123", "goods")]:
        _, created = engine.create({"org": {"id": org_id(user, app)}, "cloud": "aws"})
        jobs.append(created)
    drain(engine)
    for url in (DEPLOY, "/v1/provisioning/environments"):
        response = await console.http.get(url)
        assert response.status_code == 200
        assert {env["environmentId"] for env in response.json()["environments"]} == {
            job["environmentId"] for job in jobs
        }
    for job in jobs[1:]:
        environment = f"/v1/provisioning/environments/{job['environmentId']}"
        assert (await console.http.get(environment)).status_code == 200
        assert (await console.http.get(f"/v1/provisioning/jobs/{job['jobId']}")).status_code == 200
        response = await console.http.post(
            f"{environment}/update",
            json={"app": {"cpu": 2, "memoryMiB": 4096}},
            headers=console.change_headers(),
        )
        assert response.status_code == 202, response.text
        drain(engine)
        changed = engine.store.environment(job["environmentId"])
        assert changed["tfvars"]["app"]["cpu"] == 2
        assert changed["tfvars"]["app"]["memory_mib"] == 4096
    response = await console.http.get("/v1/applications/other/deployments")
    assert response.status_code == 404
    response = await console.http.post(
        "/v1/applications/other/deployments",
        json={"cloud": "aws"},
        headers=console.change_headers(),
    )
    assert response.status_code == 404


async def test_deployment_keeps_session_csrf_and_idempotency_checks(
    deployment_console: Console,
) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    body = {"cloud": "aws"}
    headers = console.change_headers()
    response = await console.http.post(
        DEPLOY, json=body, headers={**headers, "Origin": "https://other.test"}
    )
    assert response.status_code == 403
    response = await console.http.post(
        DEPLOY, json=body, headers={**headers, "X-CSRF-Token": "bad"}
    )
    assert response.status_code == 403
    response = await console.http.post(
        DEPLOY,
        json=body,
        headers={key: value for key, value in headers.items() if key != "Idempotency-Key"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert engine.store.environments() == []
    console.http.cookies.clear()
    assert (await console.http.post(DEPLOY, json=body, headers=headers)).status_code == 401
    assert (await console.http.get(DEPLOY)).status_code == 401


@pytest.mark.parametrize("body", [None, [], {"app": None}, {"app": []}])
async def test_update_rejects_invalid_bodies_without_mutation(
    deployment_console: Console, body: object
) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    created = await console.http.post(
        DEPLOY, json={"cloud": "aws"}, headers=console.change_headers()
    )
    drain(engine)
    environments = engine.store.environments()
    response = await console.http.post(
        f"/v1/provisioning/environments/{created.json()['environmentId']}/update",
        content=json.dumps(body),
        headers={**console.change_headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert engine.store.environments() == environments


async def test_onprem_requires_a_server_configured_image(deployment_console: Console) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    engine.config["clouds"]["onprem"].pop("images")
    response = await console.http.post(
        DEPLOY,
        json={"cloud": "onprem", "target": {"nodeId": "lab"}},
        headers=console.change_headers(),
    )
    assert response.status_code == 503
    assert response.json()["code"] == "DEPLOYMENT_NOT_CONFIGURED"
    assert engine.store.environments() == []


async def test_shared_sizing_update_keeps_request_checks(deployment_console: Console) -> None:
    console = deployment_console
    engine = console.app.state.deployment_service
    _, created = engine.create({"org": {"id": org_id("stranger", "goods")}, "cloud": "aws"})
    drain(engine)
    url = f"/v1/provisioning/environments/{created['environmentId']}/update"
    before = engine.store.environments()
    calls = list(engine.tf.calls)
    headers = console.change_headers()
    body = {"app": {"cpu": 2, "memoryMiB": 4096}}
    for invalid in (
        {"Origin": "https://other.test"},
        {"X-CSRF-Token": "bad"},
    ):
        response = await console.http.post(url, json=body, headers={**headers, **invalid})
        assert response.status_code == 403
    for action in ("apply", "check-app"):
        response = await console.http.post(
            url.removesuffix("update") + action, json={}, headers=console.change_headers()
        )
        assert response.status_code == 404
    console.http.cookies.clear()
    assert (await console.http.post(url, json=body, headers=headers)).status_code == 401
    assert engine.store.environments() == before
    assert engine.tf.calls == calls
