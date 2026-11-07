"""A later migration of the same application keeps the DB binding an earlier workflow registered."""

from __future__ import annotations

from control_plane.domain.models import ResourceKind, WorkflowStatus
from tests.conftest import World

BINDING_KEY = "binding:goods:goods"


async def _binding_ref(world: World, workflow_id: str) -> str | None:
    for resource in await world.repo.resources(workflow_id):
        if resource.kind is ResourceKind.DATABASE:
            return resource.binding_ref
    return None


async def test_the_next_workflow_reuses_the_binding_instead_of_registering_again(
    world: World,
) -> None:
    first = await world.start()
    final = await world.run_to_end(first.id)
    assert final.status is WorkflowStatus.COMPLETED, final.halt
    registered = await _binding_ref(world, first.id)
    assert registered is not None and len(world.db.bindings) == 1

    # The adapter keeps the first registration under the application's key. A second one with
    # another body (the way back names the other database as source) is refused.
    key = next(k for k in world.db.idempotency if k.startswith("binding:"))
    _, status, response = world.db.idempotency[key]
    world.db.idempotency[key] = ("a different body", status, response)

    second = await world.start()
    await world.at_stage(second.id, 1)

    assert (await world.workflow(second.id)).halt is None
    assert await _binding_ref(world, second.id) == registered
    assert len(world.db.bindings) == 1
