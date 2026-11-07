"""Request and response bodies that are not simply a stored model."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SerializeAsAny

from control_plane.domain.models import (
    ActorState,
    Endpoint,
    EndpointPair,
    HoldDesired,
    Model,
    RedisRoute,
    ResourceKind,
    StageState,
    TargetConfiguration,
    TrafficRecord,
    User,
    Workflow,
    WorkflowEvent,
)
from control_plane.workflows import commands_for


class CreateWorkflow(Model):
    source_env: str
    target_env: str
    resources: dict[ResourceKind, EndpointPair] = Field(default_factory=dict)
    target_configuration: TargetConfiguration | None = None


class InitializeBindings(Model):
    sources: dict[ResourceKind, Endpoint] = Field(default_factory=dict)


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
    progress: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    rollback: dict[str, Any] | None = None
    business_write_access: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    replication: dict[str, Any] | None = None
    routing: dict[str, Any] | None = None
    observation: dict[str, Any] = Field(default_factory=dict)
    transition_id: str | None = None
    active_operation_id: str | None = None

    kind: ResourceKind
    migration_id: str | None
    lifecycle: str | None
    health: str | None
    route: str | None
    phase: str | None
    target_writes_possible: bool
    readiness: dict[str, Any] | None
    observed_at: Any


class ObservedResourceView(ResourceView):
    """Additive console telemetry, also safe to merge with the observer's ResourceView."""

    business_write_access: str | None = None
    transition_id: str | None = None
    active_operation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    progress: dict[str, Any] | None = None
    replication: dict[str, Any] | None = None
    streams: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    rollback: dict[str, Any] | None = None
    observation: dict[str, Any] = Field(default_factory=dict)


class ActorView(Model):
    active_web_sockets: int | None = None
    actor_id: str
    state: str
    in_flight: int
    reported: int
    expected: int
    observed_at: Any


class HoldView(Model):
    is_live: bool = True
    observed_at: Any = None
    requested_at: Any = None
    released_at: Any = None
    desired: HoldDesired | Literal["UNKNOWN"]
    generation: int
    held_since: Any
    actors: list[ActorView]
    redis_route: RedisRoute = RedisRoute.PROXY
    redis_endpoint: str | None = None
    redis_route_generation: int = 0


class WorkflowDetail(Model):
    preparation: dict[str, Any] = Field(default_factory=dict)
    public_probe: dict[str, Any] | None = None
    business_probes: list[dict[str, Any]] = Field(default_factory=list)
    commands: list[dict[str, Any]] = Field(default_factory=list)

    workflow: Workflow
    allowed_commands: list[str]
    stages: list[StageView]
    resources: list[SerializeAsAny[ResourceView]]
    traffic: list[TrafficRecord]
    hold: HoldView


class EventPage(Model):
    events: list[WorkflowEvent]
    next_cursor: str | None


class Heartbeat(Model):
    active_web_sockets: int | None = Field(default=None, ge=0)
    actor_id: str
    instance_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    state: ActorState
    in_flight: int = Field(ge=0)
    redis_route: RedisRoute | None = None
    """The route this process uses now. Absent: it does not switch Redis connections."""
    redis_route_generation: int = Field(default=0, ge=0)
    """The route order it has followed."""
    stale_connections: int = Field(default=0, ge=0)
    """Redis connections still open on the route it left."""


class DirectiveView(Model):
    desired: HoldDesired
    generation: int
    object_migration_id: str | None
    poll_after_ms: int
    redis_route: RedisRoute
    redis_endpoint: str | None
    """For DIRECT: the environment whose Redis to connect to. None keeps the process's default."""
    redis_route_generation: int


def allowed_commands(workflow: Workflow) -> list[str]:
    return list(commands_for(workflow))


class PreparationReport(Model):
    resource: Literal["INFRA", "COMPUTE"]
    data: dict[str, Any]
