from __future__ import annotations

import pytest

from control_plane.applications import ApplicationInput, ApplicationService, bind_configuration
from control_plane.errors import ApiError
from control_plane.runtime import RuntimeRecord, RuntimeService, RuntimeTarget
from tests.conftest import World
from tests.test_applications import IMAGE


class Driver:
    def __init__(self, world: World) -> None:
        self.world = world
        self.starts = 0
        self.lost = False
        self.exists = False

    async def previous(self, record: RuntimeRecord) -> str:
        return "old-definition"

    async def start(self, record: RuntimeRecord) -> str:
        saved = await self.world.repo.table.get("RUNTIME", record.id)
        assert saved and "APPLYING" in saved.body
        self.starts += 1
        self.exists = True
        if self.lost:
            raise ConnectionError("reply lost")
        return "new-definition"

    async def ready(self, record: RuntimeRecord) -> bool:
        return self.exists

    async def discover(self, record: RuntimeRecord) -> str | None:
        return "new-definition" if self.exists else None

    async def restore(self, record: RuntimeRecord) -> None:
        self.exists = False

    async def restored(self, record: RuntimeRecord) -> bool:
        return not self.exists


async def setup(world: World) -> tuple[RuntimeService, Driver, str, int]:
    apps = ApplicationService(world.repo, world.clock)
    app = await apps.create("alice", ApplicationInput(name="goods", image={"imageRef": IMAGE}))
    app = await bind_configuration(apps, "alice", app.id, app.version, world.seed)
    driver = Driver(world)
    return RuntimeService(world.repo, world.clock, {"ecs": driver}), driver, app.id, app.version


async def test_runtime_is_durable_owned_and_restorable_before_migration(world: World) -> None:
    service, driver, app_id, version = await setup(world)
    record = await service.prepare("alice", app_id, "gcp", version, RuntimeTarget(provider="ecs"))
    with pytest.raises(ApiError, match="no runtime operation"):
        await service.get("other", record.id)
    with pytest.raises(ApiError, match="migration has not finished"):
        await world.start()
    await service.tick()
    await service.tick()
    record = await service.get("alice", record.id)
    assert record.status == "READY"
    assert await world.repo.active_workflow_id() is None
    await service.request_restore("alice", record.id, record.version)
    await service.tick()
    await service.tick()
    assert (await service.get("alice", record.id)).status == "RESTORED"
    assert driver.starts == 1


async def test_lost_runtime_reply_stops_until_explicit_reconcile(world: World) -> None:
    service, driver, app_id, version = await setup(world)
    driver.lost = True
    record = await service.prepare("alice", app_id, "gcp", version, RuntimeTarget(provider="ecs"))
    await service.tick()
    unknown = await service.get("alice", record.id)
    assert unknown.status == "UNKNOWN"
    for _ in range(4):
        await service.tick()
    assert (await service.get("alice", record.id)).version == unknown.version
    await service.reconcile("alice", record.id, unknown.version)
    await service.tick()
    assert (await service.get("alice", record.id)).status == "READY"
    assert driver.starts == 1


async def test_runtime_cannot_overlap_an_existing_migration(world: World) -> None:
    service, _, app_id, version = await setup(world)
    await world.start()
    with pytest.raises(ApiError, match="completion"):
        await service.prepare("alice", app_id, "gcp", version, RuntimeTarget(provider="ecs"))


async def test_ecs_clones_approved_template_and_updates_only_approved_service(world: World) -> None:
    from control_plane.runtime_drivers import EcsDriver

    class Ecs:
        def __init__(self) -> None:
            self.registered: dict = {}
            self.updated: dict = {}

        def describe_task_definition(self, **kwargs: object) -> dict:
            return {
                "taskDefinition": {
                    "family": "approved",
                    "taskDefinitionArn": "read-only-metadata",
                    "containerDefinitions": [
                        {
                            "name": "app",
                            "image": "previous",
                            "portMappings": [{"containerPort": 8000}],
                            "environment": [{"name": "WORKLOAD_SCOPE", "value": "scope:goods"}],
                        }
                    ],
                }
            }

        def register_task_definition(self, **kwargs: object) -> dict:
            self.registered = kwargs
            return {"taskDefinition": {"taskDefinitionArn": "arn:task:new"}}

        def update_service(self, **kwargs: object) -> dict:
            self.updated = kwargs
            return {}

    service, _, app_id, version = await setup(world)
    record = await service.prepare(
        "alice",
        app_id,
        "gcp",
        version,
        RuntimeTarget(
            provider="ecs",
            cluster="approved-cluster",
            service="approved-service",
            task_definition="approved-template",
        ),
    )
    client = Ecs()
    ref = await EcsDriver("ap-northeast-2", client).start(record)
    assert ref == "arn:task:new"
    assert "taskDefinitionArn" not in client.registered
    container = client.registered["containerDefinitions"][0]
    assert container["image"] == IMAGE
    assert container["environment"][0]["value"] == "scope:goods"
    assert client.updated == {
        "cluster": "approved-cluster",
        "service": "approved-service",
        "taskDefinition": ref,
    }
