from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from control_plane.deployment.api import org_id
from control_plane.deployment.provisioner.service import Service
from control_plane.deployment.provisioner.store import Store
from control_plane.domain.models import ResourceKind, WorkflowStatus
from control_plane.engine.engine import Engine
from control_plane.traffic import TrafficError
from tests.deployment.test_provisioner import CATALOG, CONFIG, FakeTerraform, ReadyChecker
from tests.test_api import Console
from tests.test_api import console as console
from tests.test_units import TrafficApi


class TargetTerraform(FakeTerraform):
    changed_address = False

    def apply(self, env_id: str, cloud: str, plan: str) -> dict[str, Any]:
        super().apply(env_id, cloud, plan)
        return {
            "app": {
                "url": "http://new-target:8000" if self.changed_address else "http://gcp-app:8000"
            },
            "database": {"id": "target-db", "host": "target.db", "port": 5432, "name": "appdb"},
            "cache": {"host": "target.redis", "port": 6379},
            "storage": {"name": "target-bucket"},
        }


def drain(engine: Service) -> None:
    while engine.run_next():
        pass


@pytest.fixture
def target(console: Console, tmp_path: Path) -> Service:
    engine = Service(
        Store(tmp_path / "sizing.db"),
        TargetTerraform(tmp_path),
        copy.deepcopy(CATALOG),
        copy.deepcopy(CONFIG),
        checker=ReadyChecker(),
    )
    engine.create({"org": {"id": org_id("github:4242", "goods"), "name": "goods"}, "cloud": "gcp"})
    drain(engine)
    console.app.state.deployment_service = engine
    previous = console.world.platform.engine
    console.world.platform.engine = Engine(
        replace(previous.services, deployment=engine), owner=previous.owner
    )
    return engine


async def selection(console: Console) -> dict[str, Any]:
    response = await console.http.get("/v1/migration-sizing?targetEnv=gcp")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["supported"], value
    return {
        "environmentId": value["deployment"]["environmentId"],
        "expectedUpdatedAt": value["deployment"]["updatedAt"],
        "sizing": {},
        "app": {"cpu": 2, "memoryMiB": 4096},
    }


async def plan(console: Console, selected: dict[str, Any]) -> str:
    response = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": "aws", "targetEnv": "gcp", "targetConfiguration": selected},
        headers=console.change_headers(),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["workflow"]["id"])


async def test_sizing_admission_checks_target_values_and_freezes_the_plan(
    console: Console, target: Service
) -> None:
    await console.login()
    chosen = await selection(console)
    # Fixed worker identities also bound the Cloud Run CPU choices.
    available = (await console.http.get("/v1/migration-sizing?targetEnv=gcp")).json()
    assert available["appOptions"]["cpu"] == [1, 2]
    # Matching the app URL cannot prove which database/cache the migration will use.
    assert available["supported"] and available["parameters"] == {}
    for change in (
        {"environmentId": "env-000000000000"},
        {"expectedUpdatedAt": "old"},
        {"app": {"cpu": 4}},
        {"app": {"workerCount": 8}},
        {"sizing": {"db_storage_gib": 1}},
        {"sizing": {"compute_machine_type": "e2-small"}},
        {"sizing": {"db_tier": "db-custom-2-7680"}},
    ):
        response = await console.http.post(
            "/v1/workflows",
            json={
                "sourceEnv": "aws",
                "targetEnv": "gcp",
                "targetConfiguration": {**chosen, **change},
            },
            headers=console.change_headers(),
        )
        assert response.status_code in {409, 422}, response.text
    database = console.world.seed.resources.database
    assert database is not None and database.catalog is not None
    database.catalog.databases[str(database.target_ref)] = {
        "host": "different.db",
        "port": 5432,
        "name": "appdb",
    }
    assert not (await console.http.get("/v1/migration-sizing?targetEnv=gcp")).json()["parameters"]
    database.catalog.databases[str(database.target_ref)]["host"] = "target.db"
    verified = (await console.http.get("/v1/migration-sizing?targetEnv=gcp")).json()
    assert "db_tier" in verified["parameters"]
    assert "cache_memory_gib" not in verified["parameters"]
    database.catalog.databases[str(database.source_ref)] = {
        "host": "target.db",
        "port": 5432,
        "name": "another-database",
    }
    assert not (await console.http.get("/v1/migration-sizing?targetEnv=gcp")).json()["parameters"]
    database.catalog.databases[str(database.source_ref)] = {}
    endpoint = {
        "host": "source.db",
        "port": 5432,
        "database": "appdb",
        "secretRef": "env:source",
        "writerRole": "app",
    }
    wrong = await console.http.post(
        "/v1/workflows",
        json={
            "sourceEnv": "aws",
            "targetEnv": "gcp",
            "targetConfiguration": chosen,
            "resources": {
                "DATABASE": {"source": endpoint, "target": {**endpoint, "host": "elsewhere.db"}}
            },
        },
        headers=console.change_headers(),
    )
    assert wrong.status_code == 422 and wrong.json()["code"] == "MIGRATION_TARGET_MISMATCH"
    workflow_id = await plan(console, chosen)
    saved = await console.world.workflow(workflow_id)
    assert saved.target_configuration is not None
    assert saved.target_configuration.app.memory_mib == 4096
    before = target.get(chosen["environmentId"])["sizing"]
    response = await console.http.post(
        f"/v1/provisioning/environments/{chosen['environmentId']}/update",
        json={"app": {"cpu": 1}},
        headers=console.change_headers(),
    )
    assert response.status_code == 409 and response.json()["code"] == "WORKFLOW_ACTIVE"
    assert target.get(chosen["environmentId"])["sizing"] == before


async def test_prepare_resizes_once_across_restart_and_waits_for_app_ready(
    console: Console, target: Service
) -> None:
    await console.login()
    chosen = await selection(console)
    workflow_id = await plan(console, chosen)
    await console.world.approve(workflow_id)
    await console.world.tick()
    assert not console.world.db.migrations and not console.world.db.bindings
    stage = await console.world.repo.stage(workflow_id, 0)
    assert stage is not None and stage.data["target_sizing_job_id"]
    job_count = target.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    # Simulate losing only the DynamoDB save after the atomic SQLite update admission.
    stage.data.pop("target_sizing_job_id")
    await console.world.repo.save_stage(workflow_id, stage)
    previous = console.world.platform.engine
    console.world.platform.engine = Engine(previous.services, owner=previous.owner)
    await console.world.tick()
    assert target.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == job_count
    assert not console.world.db.bindings
    drain(target)
    await console.world.tick()
    stage = await console.world.repo.stage(workflow_id, 0)
    assert stage is not None and stage.data["target_sizing_ready"]
    # Object storage is the first existing PREPARE handler in this seed.
    assert console.world.object.migrations
    assert target.get(chosen["environmentId"])["appliedSnapshot"]["app"]["cpu"] == 2


@pytest.mark.parametrize("failure", ["plan", "address", "traffic", "binding"])
async def test_failed_or_serving_target_never_starts_cdc(
    console: Console, target: Service, failure: str
) -> None:
    await console.login()
    chosen = await selection(console)
    workflow_id = await plan(console, chosen)
    if failure == "plan":
        target.tf.fail = "plan"
    elif failure == "address":
        target.tf.changed_address = True
    elif failure == "traffic":
        console.world.traffic.history.append(100)
    else:
        identifier = "binding:changed"
        console.world.db.bindings[identifier] = {
            "status": "ACTIVE",
            "databaseRef": "db:goods-cloudsql",
        }
        resource = await console.world.repo.resource(workflow_id, ResourceKind.DATABASE)
        await console.world.repo.save_resource(
            workflow_id, resource.model_copy(update={"binding_ref": identifier})
        )
    await console.world.approve(workflow_id)
    await console.world.tick()
    drain(target)
    await console.world.tick()
    workflow = await console.world.workflow(workflow_id)
    assert workflow.status is WorkflowStatus.HALTED
    assert workflow.halt is not None
    assert workflow.halt.code in {
        "TARGET_SIZING_FAILED",
        "TARGET_ADDRESS_CHANGED",
        "TARGET_TRAFFIC_ACTIVE",
        "SOURCE_BINDING_MISMATCH",
    }
    assert not console.world.db.migrations
    assert set(console.world.db.bindings) == (
        {"binding:changed"} if failure == "binding" else set()
    )
    assert not console.world.redis.migrations


async def test_target_traffic_guard_reads_only_and_refuses_unknown_or_canary() -> None:
    api = TrafficApi()
    reader = api.client()
    await reader.require_source_only(source_pool="aws", target_pool="gcp")
    api.weights = {"aws": 95, "gcp": 5}
    with pytest.raises(TrafficError):
        await reader.require_source_only(source_pool="aws", target_pool="gcp")
    api.weights = {"aws": 100, "gcp": 0}
    api.unknown = ["op-unknown"]
    with pytest.raises(TrafficError):
        await reader.require_source_only(source_pool="aws", target_pool="gcp")
    assert not api.posted
