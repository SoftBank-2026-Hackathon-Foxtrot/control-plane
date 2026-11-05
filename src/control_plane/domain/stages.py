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
    # Target writes cannot be undone with the DMS profile, so a person starts this stage.
    StageDefinition(StageKind.DATABASE_CUTOVER, requires_approval=True),
    StageDefinition(StageKind.REDIS_CUTOVER),
    StageDefinition(StageKind.TRAFFIC),
    # FINALIZE removes the way back.
    StageDefinition(StageKind.FINALIZE, requires_approval=True),
)

CUTOVER_RESOURCE: dict[StageKind, ResourceKind] = {
    StageKind.OBJECT_CUTOVER: ResourceKind.OBJECT_STORAGE,
    StageKind.DATABASE_CUTOVER: ResourceKind.DATABASE,
    StageKind.REDIS_CUTOVER: ResourceKind.REDIS,
}

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
    StageKind.DATABASE_CUTOVER: {
        ResourceKind.OBJECT_STORAGE: OBJECT_MOVED,
        ResourceKind.DATABASE: SOURCE,
        ResourceKind.REDIS: SOURCE,
    },
    StageKind.REDIS_CUTOVER: {
        ResourceKind.OBJECT_STORAGE: OBJECT_MOVED,
        ResourceKind.DATABASE: TARGET,
        ResourceKind.REDIS: SOURCE,
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
