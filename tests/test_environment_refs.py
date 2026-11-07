"""Each environment names its own resources; a workflow takes its refs from its direction."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from control_plane.domain.models import ResourceKind, WorkflowStatus
from control_plane.seed import Seed
from tests.conftest import SEED, World

AWS_REFS = {"database": "db:goods-rds", "redis": "resource:aws", "object": "resource:aws-receipts"}
GCP_REFS = {
    "database": "db:goods-cloudsql",
    "redis": "resource:gcp",
    "object": "resource:gcp-receipts",
}


def environment_seed() -> dict[str, Any]:
    seed = copy.deepcopy(SEED)
    seed["environments"]["aws"]["resources"] = dict(AWS_REFS)
    seed["environments"]["gcp"]["resources"] = dict(GCP_REFS)
    for resource in seed["resources"].values():
        resource.pop("sourceRef")
        resource.pop("targetRef")
    return seed


def planned_refs(world: World) -> dict[str, tuple[str, str]]:
    keys = {
        ResourceKind.DATABASE: "database",
        ResourceKind.REDIS: "redis",
        ResourceKind.OBJECT_STORAGE: "object",
    }
    return {
        keys[kind]: (adapter.only.plan["sourceRef"], adapter.only.plan["targetRef"])
        for kind, adapter in world.adapters.items()
    }


@pytest.mark.parametrize(
    ("source", "target", "source_refs", "target_refs"),
    [("aws", "gcp", AWS_REFS, GCP_REFS), ("gcp", "aws", GCP_REFS, AWS_REFS)],
)
async def test_the_workflow_direction_picks_the_refs(
    source: str, target: str, source_refs: dict[str, str], target_refs: dict[str, str]
) -> None:
    world = World(seed=environment_seed())
    workflow = await world.workflows.create(source_env=source, target_env=target, actor="tester")
    await world.at_stage(workflow.id, 1)

    assert (await world.workflow(workflow.id)).halt is None
    assert planned_refs(world) == {
        key: (source_refs[key], target_refs[key]) for key in ("database", "redis", "object")
    }
    binding = next(iter(world.db.bindings.values()))
    assert binding["databaseRef"] == source_refs["database"]


def test_environment_refs_win_over_fixed_refs() -> None:
    raw = copy.deepcopy(SEED)
    raw["environments"]["aws"]["resources"] = {"redis": "resource:aws"}
    raw["environments"]["gcp"]["resources"] = {"redis": "resource:gcp"}
    seed = Seed.model_validate(raw)
    back = seed.resource_seed(ResourceKind.REDIS, "gcp", "aws")
    assert (back.source_ref, back.target_ref) == ("resource:gcp", "resource:aws")
    # A resource no environment names keeps its fixed refs.
    database = seed.resource_seed(ResourceKind.DATABASE, "gcp", "aws")
    assert (database.source_ref, database.target_ref) == ("db:goods-rds", "db:goods-cloudsql")


def test_a_resource_without_refs_needs_them_in_every_environment() -> None:
    raw = environment_seed()
    del raw["environments"]["gcp"]["resources"]["redis"]
    with pytest.raises(ValidationError, match=r"resources\.redis"):
        Seed.model_validate(raw)


def test_environment_database_refs_must_be_in_the_catalog() -> None:
    raw = environment_seed()
    raw["environments"]["gcp"]["resources"]["database"] = "db:somewhere-else"
    with pytest.raises(ValidationError, match=r"catalog\.databases"):
        Seed.model_validate(raw)


def with_onprem(raw: dict[str, Any], refs: dict[str, str]) -> dict[str, Any]:
    raw["environments"]["onprem"] = {
        "appUrl": "http://onprem-app:80",
        "trafficPool": "onprem",
        "actors": {"onprem-api": 2, "onprem-worker": 1},
        "resources": refs,
    }
    return raw


@pytest.mark.parametrize("source,target", [("aws", "onprem"), ("onprem", "aws")])
async def test_incomplete_onprem_refs_stop_before_any_adapter(source: str, target: str) -> None:
    # Redis or DB can be missing even when object (the first adapter) is configured.
    raw = with_onprem(copy.deepcopy(SEED), {"object": "resource:onprem-receipts"})
    raw["environments"]["aws"]["resources"] = AWS_REFS
    world = World(seed=raw)
    workflow = await world.workflows.create(source_env=source, target_env=target, actor="tester")
    await world.approve(workflow.id)
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert stopped.halt and stopped.halt.code == "ONPREM_TARGET_UNCONFIRMED"
    assert not any(adapter.migrations for adapter in world.adapters.values())
    assert not world.db.bindings and world.gate.catalog_published == 0


@pytest.mark.parametrize("source,target", [("aws", "onprem"), ("onprem", "aws")])
async def test_onprem_adapter_plans_use_only_the_selected_environments(
    source: str, target: str
) -> None:
    refs = {
        "object": "resource:onprem-receipts",
        "redis": "resource:onprem",
        "database": "db:personal-vm",
    }
    raw = with_onprem(environment_seed(), refs)
    raw["resources"]["database"]["catalog"]["databases"][refs["database"]] = {}
    world = World(seed=raw)
    workflow = await world.workflows.create(source_env=source, target_env=target, actor="tester")
    await world.at_stage(workflow.id, 1)
    a, b = (AWS_REFS, refs) if source == "aws" else (refs, AWS_REFS)
    assert planned_refs(world) == {k: (a[k], b[k]) for k in a}


@pytest.mark.parametrize("source,target", [("aws", "gcp"), ("gcp", "aws")])
async def test_cloud_pair_keeps_working_with_an_unconfigured_demo_node(
    source: str, target: str
) -> None:
    world = World(seed=with_onprem(environment_seed(), {}))
    workflow = await world.workflows.create(source_env=source, target_env=target, actor="tester")
    await world.at_stage(workflow.id, 1)
    a, b = (AWS_REFS, GCP_REFS) if source == "aws" else (GCP_REFS, AWS_REFS)
    assert planned_refs(world) == {k: (a[k], b[k]) for k in a}


async def test_onprem_refs_cannot_change_after_preflight_or_restart() -> None:
    raw = with_onprem(environment_seed(), {"object": "resource:onprem-receipts"})
    raw["resources"] = {"object": raw["resources"]["object"]}
    world = World(seed=raw)
    workflow = await world.workflows.create(source_env="aws", target_env="onprem", actor="tester")
    world.prober.ready = False
    await world.approve(workflow.id)
    await world.tick()
    stage = (await world.repo.stages(workflow.id))[0]
    assert stage.data["onpremResourceRefs"]["OBJECT_STORAGE"] == [
        "resource:aws-receipts",
        "resource:onprem-receipts",
    ]
    world.restart_engine_every_tick = True
    world.prober.ready = True
    world.seed.environments["onprem"].resources["object"] = "resource:gcp-receipts"
    stopped = await world.until(workflow.id, WorkflowStatus.HALTED)
    assert stopped.halt and stopped.halt.code == "ONPREM_TARGET_UNCONFIRMED"
    assert not world.object.migrations


@pytest.mark.parametrize("source,target", [("aws", "gcp"), ("gcp", "aws")])
async def test_cloud_workflow_finishes_with_an_onprem_plan_target(source: str, target: str) -> None:
    raw = with_onprem(environment_seed(), {})
    raw["environments"]["onprem"]["actors"] = {}
    world = World(seed=raw)
    wf = await world.workflows.create(source_env=source, target_env=target, actor="tester")
    assert (await world.run_to_end(wf.id)).status is WorkflowStatus.COMPLETED
    assert await world.repo.latest_business_probe(wf.id, "onprem") is None
    assert await world.repo.latency_buckets(wf.id, "onprem", since=wf.created_at) == []


@pytest.mark.parametrize("source,target", [("aws", "onprem"), ("onprem", "aws")])
async def test_onprem_plan_without_actors_stops_before_adapter_setup(
    source: str, target: str
) -> None:
    raw = with_onprem(environment_seed(), {"object": "resource:onprem-receipts"})
    raw["environments"]["onprem"]["actors"] = {}
    world = World(seed=raw)
    wf = await world.workflows.create(source_env=source, target_env=target, actor="tester")
    assert wf.status is WorkflowStatus.PLANNED
    await world.approve(wf.id)
    stopped = await world.until(wf.id, WorkflowStatus.HALTED)
    assert stopped.halt and stopped.halt.code == "ONPREM_ACTORS_REQUIRED"
    assert not world.db.bindings and not any(a.migrations for a in world.adapters.values())


def test_only_onprem_can_be_registered_without_running_actors() -> None:
    raw = with_onprem(environment_seed(), {})
    raw["environments"]["aws"]["actors"] = {}
    with pytest.raises(ValidationError, match="aws must register workload actors"):
        Seed.model_validate(raw)
