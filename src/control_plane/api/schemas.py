"""Request and response bodies that are not simply a stored model."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from control_plane.domain.models import (
    ActorState,
    HoldDesired,
    Model,
    ResourceKind,
    StageState,
    TrafficRecord,
    User,
    Workflow,
    WorkflowEvent,
)
from control_plane.workflows import ALLOWED_COMMANDS


class CreateWorkflow(Model):
    source_env: str
    target_env: str


class WorkflowCommand(Model):
    command: str
    expected_version: int
    resource_id: ResourceKind | None = None
    mode: str = "normal"
    confirm_target_failure: bool = False


class Me(Model):
    user: User
    expires_at: Any
    csrf_token: str


class Workspace(Model):
    id: str
    name: str
    role: str


class EnvironmentView(Model):
    name: str
    traffic_pool: str
    actors: dict[str, int]


class ApplicationView(Model):
    id: str
    scope: str
    environments: list[EnvironmentView]
    resources: list[ResourceKind]


class StageView(Model):
    index: int
    kind: str
    status: str
    step: str
    detail: str
    started_at: Any
    finished_at: Any

    @classmethod
    def of(cls, stage: StageState) -> StageView:
        return cls(
            index=stage.index,
            kind=stage.kind.value,
            status=stage.status.value,
            step=stage.step,
            detail=stage.detail,
            started_at=stage.started_at,
            finished_at=stage.finished_at,
        )


class ResourceView(Model):
    kind: ResourceKind
    migration_id: str | None
    lifecycle: str | None
    health: str | None
    route: str | None
    phase: str | None
    target_writes_possible: bool
    readiness: dict[str, Any] | None
    observed_at: Any


class ActorView(Model):
    actor_id: str
    state: str
    in_flight: int
    reported: int
    expected: int
    observed_at: Any


class HoldView(Model):
    desired: HoldDesired
    generation: int
    held_since: Any
    actors: list[ActorView]


class WorkflowDetail(Model):
    workflow: Workflow
    allowed_commands: list[str]
    stages: list[StageView]
    resources: list[ResourceView]
    traffic: list[TrafficRecord]
    hold: HoldView


class EventPage(Model):
    events: list[WorkflowEvent]
    next_cursor: str | None


class Heartbeat(Model):
    actor_id: str
    instance_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    state: ActorState
    in_flight: int = Field(ge=0)


class DirectiveView(Model):
    desired: HoldDesired
    generation: int
    object_migration_id: str | None
    poll_after_ms: int


def allowed_commands(workflow: Workflow) -> list[str]:
    return list(ALLOWED_COMMANDS[workflow.status])
