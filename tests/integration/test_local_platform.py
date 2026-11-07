"""HTTP flow with real DynamoDB Local and Docker; migration providers remain contract fakes."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from uuid import uuid4

import boto3
import httpx
import pytest

from control_plane.config import Settings
from control_plane.domain.models import WorkflowStatus
from control_plane.runtime import RuntimeTarget
from control_plane.runtime_drivers import DockerDriver
from control_plane.store.table import DynamoTable
from tests.conftest import SETTINGS, World
from tests.test_api import Console

pytestmark = pytest.mark.integration
ENDPOINT = os.environ.get("CONTROL_PLANE_TEST_DYNAMODB_URL")
IMAGE = "nginx@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def test_personal_image_registration_prepare_restore_and_migrate() -> None:
    if not ENDPOINT:
        pytest.skip("CONTROL_PLANE_TEST_DYNAMODB_URL is not set")
    client = boto3.client(
        "dynamodb",
        endpoint_url=ENDPOINT,
        region_name="ap-northeast-2",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    name = f"platform-test-{uuid4().hex}"
    await asyncio.to_thread(
        client.create_table,
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("pk", "sk")],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )
    driver = DockerDriver()
    runtime_ids: list[str] = []
    console = None
    try:
        inspected = json.loads(await driver.command("image", "inspect", IMAGE))[0]
        architecture = {"amd64": "X86_64", "arm64": "ARM64"}[inspected["Architecture"]]
        port = free_port()
        settings: Settings = SETTINGS.model_copy(
            update={
                "legacy_seed_enabled": False,
                "runtime_targets": {
                    "github:4242/gcp": RuntimeTarget(provider="docker", host_port=port)
                },
            }
        )
        world = World(table=DynamoTable(name, client=client))
        world.platform.context.runtime.drivers["docker"] = driver
        console = Console(world, settings)
        await console.login()
        body = {
            "name": "local-runtime-test",
            "image": {
                "imageRef": IMAGE,
                "port": 80,
                "architecture": architecture,
            },
        }
        headers = console.change_headers()
        response = await console.http.post("/v1/applications", json=body, headers=headers)
        assert response.status_code == 201, response.text
        app = response.json()
        replay = await console.http.post("/v1/applications", json=body, headers=headers)
        assert replay.json()["id"] == app["id"]
        response = await console.http.put(
            f"/v1/applications/{app['id']}/resources",
            json={"expectedVersion": app["version"]},
            headers=console.change_headers(),
        )
        assert response.status_code == 200, response.text
        app = response.json()

        async def prepare() -> dict:
            response = await console.http.post(
                f"/v1/applications/{app['id']}/environments/gcp/prepare",
                json={"expectedVersion": app["version"]},
                headers=console.change_headers(),
            )
            assert response.status_code == 202, response.text
            operation = response.json()
            runtime_ids.append(operation["id"])
            for _ in range(20):
                await world.platform.context.runtime.tick()
                operation = (
                    await console.http.get(
                        f"/v1/runtime-operations/{operation['id']}",
                    )
                ).json()
                if operation["status"] in {"READY", "FAILED", "UNKNOWN"}:
                    break
                await asyncio.sleep(0.1)
            assert operation["status"] == "READY", operation
            async with httpx.AsyncClient() as http:
                for _ in range(30):
                    try:
                        ready = await http.get(f"http://127.0.0.1:{port}/")
                        if ready.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("real container did not answer HTTP")
            return operation

        operation = await prepare()
        response = await console.http.post(
            f"/v1/runtime-operations/{operation['id']}/restore",
            json={"expectedVersion": operation["version"]},
            headers=console.change_headers(),
        )
        assert response.status_code == 202, response.text
        await world.platform.context.runtime.tick()
        await world.platform.context.runtime.tick()
        restored = (await console.http.get(f"/v1/runtime-operations/{operation['id']}")).json()
        assert restored["status"] == "RESTORED"
        await prepare()
        await world.workload.beat()
        verified = await console.http.post(
            f"/v1/applications/{app['id']}/verify",
            json={"expectedVersion": app["version"]},
            headers=console.change_headers(),
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["passed"], verified.text
        app["version"] = verified.json()["version"]
        denied = await console.http.post(
            "/v1/workflows",
            json={"applicationId": app["id"], "sourceEnv": "gcp", "targetEnv": "aws"},
            headers=console.change_headers(),
        )
        assert denied.status_code == 422
        response = await console.http.post(
            "/v1/workflows",
            json={"applicationId": app["id"], "sourceEnv": "aws", "targetEnv": "gcp"},
            headers=console.change_headers(),
        )
        assert response.status_code == 201, response.text
        workflow = response.json()["workflow"]
        blocked = await console.http.patch(
            f"/v1/applications/{app['id']}",
            json=body,
            headers=console.change_headers(**{"Expected-Version": str(app["version"])}),
        )
        assert blocked.status_code == 409
        world.restart_engine_every_tick = True
        finished = await world.run_to_end(workflow["id"])
        assert finished.status == WorkflowStatus.COMPLETED, finished.halt
        assert finished.application_snapshot["image"]["imageRef"] == IMAGE
        assert (await console.http.get(f"/v1/workflows/{workflow['id']}")).status_code == 200
        events = (await console.http.get(f"/v1/applications/{app['id']}/events")).json()["events"]
        assert any(event["type"] == "POLICY_DENIED" for event in events)
    finally:
        for operation in runtime_ids:
            found = await driver.command(
                "ps", "-aq", "--filter", f"label=foxtrot.operation={operation}"
            )
            if found:
                await driver.command("rm", "-f", *found.splitlines())
        if console:
            await console.http.aclose()
        await asyncio.to_thread(client.delete_table, TableName=name)
