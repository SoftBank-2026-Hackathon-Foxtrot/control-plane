"""The fixed order of a migration, and which routes each stage leaves in place."""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.domain.models import ResourceKind, StageKind


@dataclass(frozen=True)
class StageDefinition:
    kind: StageKind
    requires_approval: bool = False


PLAN: tuple[StageDefinition, ...] = (
    StageDefinition(StageKind.PREPARE, requires_approval=True),
    StageDefinition(StageKind.SYNC),
    StageDefinition(StageKind.OBJECT_CUTOVER),
    StageDefinition(StageKind.CANARY),
    # One approval starts Redis and then the DB. Target writes to the DB cannot be undone with the
    # DMS profile, so a person decides before either moves. Redis goes first: it can still go back,
    # and if it fails the DB is untouched and everything returns to the source. Asking again
    # between the two would leave the stores split across clouds while a person is away.
    StageDefinition(StageKind.REDIS_CUTOVER, requires_approval=True),
    StageDefinition(StageKind.DATABASE_CUTOVER),
    StageDefinition(StageKind.TRAFFIC),
    # FINALIZE removes the way back.
    StageDefinition(StageKind.FINALIZE, requires_approval=True),
)

CUTOVER_RESOURCE: dict[StageKind, ResourceKind] = {
    StageKind.OBJECT_CUTOVER: ResourceKind.OBJECT_STORAGE,
    StageKind.DATABASE_CUTOVER: ResourceKind.DATABASE,
    StageKind.REDIS_CUTOVER: ResourceKind.REDIS,
}

CUTOVER_ORDER: tuple[ResourceKind, ...] = (
    ResourceKind.OBJECT_STORAGE,
    ResourceKind.REDIS,
    ResourceKind.DATABASE,
)
"""Every resource kind, in the order the plan moves them. A seed may manage only some of them."""


def skipped(kind: StageKind, managed: frozenset[ResourceKind]) -> bool:
    """A cutover stage for a resource the application does not have is skipped."""
    resource = CUTOVER_RESOURCE.get(kind)
    return resource is not None and resource not in managed


def requires_approval(index: int, managed: frozenset[ResourceKind]) -> bool:
    """Whether the stage at `index` waits for a person.

    An approval asked at a skipped stage moves to the next stage that runs, so leaving a resource
    out never removes a decision: without Redis, the approval before the store cutovers is asked
    at the DB cutover instead.
    """
    if PLAN[index].requires_approval:
        return True
    earlier = index - 1
    while earlier >= 0 and skipped(PLAN[earlier].kind, managed):
        if PLAN[earlier].requires_approval:
            return True
        earlier -= 1
    return False


SOURCE = frozenset({"SOURCE"})
# The object adapter keeps reading both buckets until FINALIZE and reports MIXED.
OBJECT_MOVED = frozenset({"TARGET", "MIXED"})
TARGET = frozenset({"TARGET"})

ALLOWED_ROUTES_BEFORE: dict[StageKind, dict[ResourceKind, frozenset[str]]] = {
    StageKind.OBJECT_CUTOVER: {
        ResourceKind.OBJECT_STORAGE: SOURCE,
        ResourceKind.DATABASE: SOURCE,
        ResourceKind.REDIS: SOURCE,
    },
    StageKind.REDIS_CUTOVER: {
        ResourceKind.OBJECT_STORAGE: OBJECT_MOVED,
        ResourceKind.DATABASE: SOURCE,
        ResourceKind.REDIS: SOURCE,
    },
    StageKind.DATABASE_CUTOVER: {
        ResourceKind.OBJECT_STORAGE: OBJECT_MOVED,
        ResourceKind.DATABASE: SOURCE,
        ResourceKind.REDIS: TARGET,
    },
    StageKind.TRAFFIC: {
        ResourceKind.OBJECT_STORAGE: OBJECT_MOVED,
        ResourceKind.DATABASE: TARGET,
        ResourceKind.REDIS: TARGET,
    },
}
"""What must be observed before a stage may change anything. A mismatch stops the workflow."""

EXPECTED_LIFECYCLE_AFTER: dict[str, frozenset[str]] = {
    "PREPARE": frozenset({"PREPARED", "SYNCING", "READY"}),
    "START_SYNC": frozenset({"SYNCING", "READY"}),
    "QUIESCE": frozenset({"QUIESCED"}),
    "CUTOVER": frozenset({"SWITCHED"}),
    "RESUME:TARGET": frozenset({"ACTIVE"}),
    "RESUME:SOURCE": frozenset({"RESTORED"}),
    "ROLLBACK": frozenset({"ROLLED_BACK_HELD"}),
    "CANCEL": frozenset({"CANCELLED"}),
    "FINALIZE": frozenset({"COMPLETED"}),
}
"""How a reconcile tells that a command whose result was lost has in fact taken effect."""
