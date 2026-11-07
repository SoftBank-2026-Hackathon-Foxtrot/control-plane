"""Each environment names its own resources; a workflow takes its refs from its direction."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from control_plane.domain.models import ResourceKind
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
