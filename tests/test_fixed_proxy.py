"""Direct endpoint requests keep the app binding stable across distinct workflows."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import httpx
import pytest

from control_plane.domain.models import EndpointPair, ResourceKind, WorkflowStatus
from control_plane.errors import ApiError
from control_plane.main import create_api
from tests.conftest import SEED, World
from tests.fakes import Fault


def fixed_seed() -> dict[str, Any]:
    seed = copy.deepcopy(SEED)
    for name, port in (("database", 5432), ("redis", 6379)):
        resource = seed["resources"][name]
        resource.pop("sourceRef")
        resource.pop("targetRef")
        resource.pop("bindingRef", None)
        resource["source"] = {
            "host": f"old-{name}",
            "port": port,
            "database": "appdb" if name == "database" else "0",
            "secretRef": f"arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:{name}-source",
        }
        resource["target"] = {
            **resource["source"],
            "host": f"new-{name}",
            "secretRef": f"arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:{name}-target",
        }
        if name == "database":
            resource.pop("catalog")
            resource["schema"] = {"tables": ["public.orders"]}
            resource["source"]["writerRole"] = resource["target"]["writerRole"] = "app_writer"
    return seed


async def initialized() -> World:
    world = World(seed=fixed_seed())
    await world.workflows.bindings.initialize()
    ready = await world.workflows.bindings.initialize()
    assert all(binding["status"] == "ACTIVE" for binding in ready.values())
    return world


async def test_registration_required_and_fixed_proxy_returned() -> None:
    world = World(seed=fixed_seed())
    with pytest.raises(ApiError) as missing:
        await world.start()
    assert missing.value.code == "BINDING_REQUIRED"
    assert await world.repo.active_workflow_id() is None
    # Change a copy: the module's shared SETTINGS fixture stays intact.
    settings = world.platform.context.settings
    world.platform.context = replace(
        world.platform.context, settings=settings.model_copy(update={"auth_disabled": True})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_api(world.platform, run_loops=False)),
        base_url="http://platform",
    ) as http:
        response = await http.post("/v1/applications/goods/bindings", json={})
        assert response.status_code == 200
        response = await http.get("/v1/applications/goods/bindings")
        assert response.status_code == 200
        assert response.json()["DATABASE"]["proxy"]["host"] == "database-goods.internal"
        assert "token" not in response.text.lower()
    assert world.gate.catalog_published == 0


async def test_two_workflows_reuse_proxy_and_preserve_previous_records() -> None:
    world = await initialized()
    proxies = {
        kind: item["proxy"] for kind, item in (await world.workflows.bindings.list()).items()
    }
    first = await world.start()
    final = await world.run_to_end(first.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    snapshot = (await world.workflow(first.id)).model_dump()
    old_commands = [item.model_dump() for item in await world.repo.commands(first.id)]
    old_resources = [item.model_dump() for item in await world.repo.resources(first.id)]
    resources = {}
    for kind in (ResourceKind.DATABASE, ResourceKind.REDIS):
        seed = world.seed.resources.of(kind)
        assert seed.source and seed.target
        resources[kind] = EndpointPair(source=seed.target, target=seed.source)
    second = await world.workflows.create(
        source_env="gcp",
        target_env="aws",
        actor="tester",
        resources=resources,
    )
    final = await world.run_to_end(second.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    assert second.id != first.id
    for kind in (ResourceKind.DATABASE, ResourceKind.REDIS):
        old = await world.repo.resource(first.id, kind)
        new = await world.repo.resource(second.id, kind)
        assert old.migration_id != new.migration_id
        assert old.binding_ref == new.binding_ref
        assert new.plan and "sourceRef" not in new.plan
        if kind is ResourceKind.REDIS:
            binding = await world.repo.application_binding("goods", kind)
            assert binding and new.binding_ref != binding["bindingId"]
            assert new.binding_ref == binding["bindingRef"]
    assert (await world.workflow(first.id)).model_dump() == snapshot
    assert [item.model_dump() for item in await world.repo.commands(first.id)] == old_commands
    assert [item.model_dump() for item in await world.repo.resources(first.id)] == old_resources
    assert {
        kind: item["proxy"] for kind, item in (await world.workflows.bindings.list()).items()
    } == proxies


async def test_old_source_and_unknown_result_block_new_workflow() -> None:
    world = await initialized()
    workflow = await world.start()
    assert (await world.run_to_end(workflow.id)).status is WorkflowStatus.COMPLETED
    with pytest.raises(ApiError) as wrong:
        await world.start()
    assert wrong.value.code == "SOURCE_MISMATCH"
    world.db.only.health = "UNKNOWN"
    with pytest.raises(ApiError) as unknown:
        await world.start()
    assert unknown.value.code == "PREVIOUS_MIGRATION_UNFINISHED"
    assert await world.repo.active_workflow_id() is None


async def test_existing_workflow_blocks_overlapping_requests() -> None:
    world = await initialized()
    first = await world.start()
    with pytest.raises(ApiError) as unfinished:
        await world.start()
    assert unfinished.value.code == "WORKFLOW_ACTIVE"
    assert await world.repo.active_workflow_id() == first.id


async def test_queued_binding_does_not_start_a_workflow() -> None:
    world = World(seed=fixed_seed())
    world.db.binding_polls = -10
    registered = await world.workflows.bindings.initialize()
    assert registered[ResourceKind.DATABASE]["status"] == "REGISTERING"
    with pytest.raises(ApiError) as pending:
        await world.start()
    assert pending.value.code == "BINDING_NOT_ACTIVE"
    assert await world.repo.active_workflow_id() is None


async def test_source_only_registration_accepts_target_when_workflow_is_created() -> None:
    seed = fixed_seed()
    pairs: dict[ResourceKind, EndpointPair] = {}
    for kind, name in ((ResourceKind.DATABASE, "database"), (ResourceKind.REDIS, "redis")):
        resource = seed["resources"][name]
        pairs[kind] = EndpointPair.model_validate(
            {
                "source": resource["source"],
                "target": resource.pop("target"),
            }
        )
    world = World(seed=seed)
    await world.workflows.bindings.initialize()
    await world.workflows.bindings.initialize()
    with pytest.raises(ApiError) as missing:
        await world.start()
    assert missing.value.code == "ENDPOINTS_REQUIRED"
    assert await world.repo.active_workflow_id() is None
    workflow = await world.workflows.create(
        source_env="aws",
        target_env="gcp",
        actor="tester",
        resources=pairs,
    )
    assert (await world.run_to_end(workflow.id)).status is WorkflowStatus.COMPLETED


async def test_partial_finalize_keeps_the_workflow_and_proxy() -> None:
    world = await initialized()
    world.db.faults["FINALIZE"] = Fault("FAILED")
    workflow = await world.start()
    assert (await world.run_to_end(workflow.id)).status is WorkflowStatus.HALTED
    assert await world.repo.active_workflow_id() == workflow.id
    assert all(item["proxy"] for item in (await world.workflows.bindings.list()).values())
    with pytest.raises(ApiError, match="another migration"):
        await world.start()


async def test_managed_reverse_is_rejected_even_if_advertised() -> None:
    world = await initialized()
    world.db.reverse_supported = True
    world.seed.resources.database.policy.rollback_mode = "REVERSE_REQUIRED"
    database = world.seed.resources.database
    assert database.source and database.target
    source = database.source.model_copy(update={"provider": "AWS_RDS"})
    target = database.target.model_copy(update={"provider": "GCP_CLOUDSQL"})
    with pytest.raises(ApiError) as unsupported:
        await world.workflows.create(
            source_env="aws",
            target_env="gcp",
            actor="tester",
            resources={ResourceKind.DATABASE: EndpointPair(source=source, target=target)},
        )
    assert unsupported.value.code == "UNSUPPORTED_ROLLBACK_MODE"


async def test_proxy_address_change_blocks_a_new_workflow() -> None:
    world = await initialized()
    next(iter(world.db.bindings.values()))["proxy"]["host"] = "replacement.internal"
    with pytest.raises(ApiError) as changed:
        await world.start()
    assert changed.value.code == "PROXY_CHANGED"
