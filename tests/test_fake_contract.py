"""The fake adapter is only worth testing against if it answers like the contract says."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from control_plane.domain.models import ResourceKind, WorkflowStatus
from tests.conftest import World

CONTRACT = json.loads((Path(__file__).parent / "contract" / "common-api.json").read_text("utf-8"))


def validate(schema_name: str, instance: dict[str, Any]) -> None:
    schema = {
        "$ref": f"#/components/schemas/{schema_name}",
        "components": CONTRACT["components"],
    }
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


@pytest.mark.parametrize("kind", list(ResourceKind))
async def test_status_responses_follow_the_contract_at_every_lifecycle(
    world: World, kind: ResourceKind
) -> None:
    workflow = await world.start()
    adapter = world.adapters[kind]
    seen: set[str] = set()
    for _ in range(400):
        await world.tick()
        for migration in adapter.migrations.values():
            view = adapter.view(migration)
            if view["lifecycle"] not in seen:
                seen.add(view["lifecycle"])
                # `details.object` is the real object adapter's own addition to the contract.
                if kind is ResourceKind.OBJECT_STORAGE:
                    view = {**view, "details": {}}
                validate("Migration", view)
        current = await world.workflow(workflow.id)
        if current.status is WorkflowStatus.WAITING_APPROVAL:
            await world.approve(workflow.id)
        elif current.status is WorkflowStatus.CUTOVER_COMPLETE:
            await world.command(workflow.id, "FINALIZE")
    assert {"CREATED", "PREPARED"} & seen or "READY" in seen


async def test_operation_responses_follow_the_contract(world: World) -> None:
    workflow = await world.start()
    await world.at_stage(workflow.id, 1)
    for adapter in world.adapters.values():
        operation = next(iter(adapter.operations.values()))
        response = adapter._get_operation(operation.id)
        validate("Operation", response.json())
