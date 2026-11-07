from __future__ import annotations

import pytest
from pydantic import ValidationError

from control_plane.applications import ApplicationInput, ApplicationService
from control_plane.errors import ApiError
from tests.conftest import World

IMAGE = "registry.example.com/foxtrot/goods@sha256:" + "a" * 64


async def test_image_crud_owner_version_and_archive(world: World) -> None:
    service = ApplicationService(world.repo, world.clock)
    body = ApplicationInput(name="goods", image={"imageRef": IMAGE})
    app = await service.create("alice", body)
    assert app.version == 1
    assert await service.list("bob") == []
    with pytest.raises(ApiError, match="no application"):
        await service.get("bob", app.id)
    with pytest.raises(ApiError, match="latest"):
        await service.update("alice", app.id, body, 4)
    updated = await service.update("alice", app.id, body, 1)
    assert updated.version == 2
    await service.archive("alice", app.id, 2)
    assert await service.list("alice") == []


def test_tags_and_embedded_registry_credentials_are_refused() -> None:
    for image in ("goods:latest", "user:password@registry/app@sha256:" + "a" * 64):
        with pytest.raises(ValidationError):
            ApplicationInput(name="bad", image={"imageRef": image})


async def test_resource_catalog_is_server_owned_and_requires_ownership(world: World) -> None:
    from control_plane.applications import bind_configuration
    from control_plane.catalog import catalog_for
    from tests.conftest import SETTINGS

    with pytest.raises(ApiError, match="no resource catalog"):
        catalog_for(SETTINGS, world.seed, "other")
    service = ApplicationService(world.repo, world.clock)
    app = await service.create(
        "github:4242", ApplicationInput(name="goods", image={"imageRef": IMAGE})
    )
    bound = await bind_configuration(service, app.owner_id, app.id, 1, world.seed)
    assert bound.configuration is not None
    assert bound.configuration.application == app.id
    assert world.seed.application == "goods"
    assert bound.configuration.resources == world.seed.resources


async def test_registered_snapshot_runs_on_existing_engine_after_restart(world: World) -> None:
    from control_plane.applications import bind_configuration
    from control_plane.domain.models import WorkflowStatus
    from control_plane.verification import verify_application
    from control_plane.workflows import WorkflowService

    service = ApplicationService(world.repo, world.clock)
    app = await service.create("alice", ApplicationInput(name="goods", image={"imageRef": IMAGE}))
    app = await bind_configuration(service, "alice", app.id, app.version, world.seed)
    await world.workload.beat()
    report = await verify_application(service, app, world.services.adapters, world.prober, True)
    assert report.passed
    app = await service.get("alice", app.id)
    assert app.configuration is not None
    workflow = await WorkflowService(
        world.repo,
        app.configuration,
        world.clock,
        world.services.adapters,
    ).create(
        source_env="aws",
        target_env="gcp",
        actor="alice",
        owner_id="alice",
        application_snapshot=app.model_dump(mode="json", by_alias=True),
    )
    with pytest.raises(ApiError, match="active migration"):
        await service.update(
            "alice", app.id, ApplicationInput(name="changed", image=app.image), app.version
        )
    world.restart_engine_every_tick = True
    finished = await world.run_to_end(workflow.id)
    assert finished.status == WorkflowStatus.COMPLETED, finished.halt
    assert finished.application_snapshot["image"]["imageRef"] == IMAGE
    assert finished.owner_id == "alice"


async def test_policy_cannot_reverse_resource_refs_and_caps_pause(world: World) -> None:
    from control_plane.applications import MigrationPolicy, bind_configuration
    from control_plane.governance import enforce, set_policy

    service = ApplicationService(world.repo, world.clock)
    app = await service.create("alice", ApplicationInput(name="goods", image={"imageRef": IMAGE}))
    app = await bind_configuration(service, "alice", app.id, app.version, world.seed)
    with pytest.raises(ApiError, match="direction"):
        enforce(app, "gcp", "aws")
    app = await set_policy(service, app, MigrationPolicy(max_quiesce_seconds=60), app.version)
    bounded = enforce(app, "aws", "gcp")
    assert bounded.resources.database.policy.max_quiesce_seconds == 60
    assert bounded.resources.redis.policy.max_quiesce_seconds == 1.8
    page = await world.table.query(f"APP_AUDIT#alice#{app.id}")
    assert len(page.items) >= 3


async def test_target_failure_halts_before_any_adapter_mutation(world: World) -> None:
    from dataclasses import replace

    from control_plane.applications import bind_configuration
    from control_plane.domain.models import WorkflowStatus
    from control_plane.engine.engine import Engine
    from control_plane.workflows import WorkflowService

    service = ApplicationService(world.repo, world.clock)
    app = await service.create("alice", ApplicationInput(name="goods", image={"imageRef": IMAGE}))
    app = await bind_configuration(service, "alice", app.id, app.version, world.seed)
    assert app.configuration is not None
    workflow = await WorkflowService(
        world.repo,
        app.configuration,
        world.clock,
        world.services.adapters,
    ).create(
        source_env="aws",
        target_env="gcp",
        actor="alice",
        owner_id="alice",
        application_snapshot=app.model_dump(mode="json", by_alias=True),
    )
    await world.approve(workflow.id)

    async def failed(*args: object) -> list[str]:
        return ["TARGET_IMAGE_NOT_CONFIRMED"]

    await Engine(replace(world.services, readiness=failed)).tick()
    stopped = await world.workflow(workflow.id)
    assert stopped.status == WorkflowStatus.HALTED
    assert stopped.halt.code == "TARGET_NOT_READY"
    assert await world.repo.commands(workflow.id) == []
