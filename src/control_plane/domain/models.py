"""What the platform persists. Every model serializes in the console's camelCase."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ResourceKind(StrEnum):
    DATABASE = "DATABASE"
    REDIS = "REDIS"
    OBJECT_STORAGE = "OBJECT_STORAGE"


class Endpoint(Model):
    """A connection description. Credentials stay in the agent's secret store."""

    host: str = Field(min_length=1, pattern=r"^[^\s/@]+$")
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(min_length=1)
    secret_ref: str = Field(min_length=1)
    provider: Literal["SELF_MANAGED", "AWS_RDS", "GCP_CLOUDSQL"] | None = None
    provider_instance: str | None = None
    writer_role: str | None = None
    options: dict[Literal["tls"], bool] | None = None

    def wire(self, kind: ResourceKind) -> dict[str, Any]:
        body = self.model_dump(by_alias=True, exclude_none=True)
        body["port"] = self.port or (5432 if kind is ResourceKind.DATABASE else 6379)
        return body

    def identity(self, kind: ResourceKind) -> tuple[object, ...]:
        body = self.wire(kind)
        identity: tuple[object, ...] = (
            self.host.lower(),
            body["port"],
            self.database,
        )
        if kind is ResourceKind.DATABASE:
            identity += (self.provider or "SELF_MANAGED", self.provider_instance)
        return identity


class EndpointPair(Model):
    source: Endpoint
    target: Endpoint


class WorkflowStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    HALTED = "HALTED"
    ROLLING_BACK = "ROLLING_BACK"
    ABORTING = "ABORTING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"

    @property
    def terminal(self) -> bool:
        return self in {WorkflowStatus.COMPLETED, WorkflowStatus.ABORTED}


class StageKind(StrEnum):
    PREPARE = "PREPARE"
    SYNC = "SYNC"
    OBJECT_CUTOVER = "OBJECT_CUTOVER"
    CANARY = "CANARY"
    DATABASE_CUTOVER = "DATABASE_CUTOVER"
    REDIS_CUTOVER = "REDIS_CUTOVER"
    TRAFFIC = "TRAFFIC"
    FINALIZE = "FINALIZE"


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    HALTED = "HALTED"


class Halt(Model):
    code: str
    message: str
    blockers: list[str] = Field(default_factory=list)
    resource: ResourceKind | None = None
    at: datetime


class RollbackRequest(Model):
    resource: ResourceKind
    mode: str = "normal"
    requested_by: str
    target_failure_confirmed: bool = False


class Workflow(Model):
    id: str
    application: str
    source_env: str
    target_env: str
    status: WorkflowStatus = WorkflowStatus.PLANNED
    current_stage: int = 0
    approved_through: int = -1
    version: int = 0
    retry_epoch: int = 0
    """Moves each time a person asks to go on, roll back or abort. Failures of an earlier epoch
    have been reviewed and may be retried, and time spent waiting for that person does not count."""
    created_by: str
    created_at: datetime
    finished_at: datetime | None = None
    halt: Halt | None = None
    rollback: RollbackRequest | None = None
    reconcile_requested: bool = False
    reconcile_count: int = 0
    rolled_back: list[ResourceKind] = Field(default_factory=list)


class StageState(Model):
    index: int
    kind: StageKind
    status: StageStatus = StageStatus.PENDING
    step: str = ""
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ResourceState(Model):
    kind: ResourceKind
    migration_id: str | None = None
    transition_id: str | None = None
    binding_ref: str | None = None
    workload_scope_ref: str | None = None
    target_writes_possible: bool = False
    last_status: dict[str, Any] | None = None
    last_status_at: datetime | None = None
    last_route: str | None = None
    plan: dict[str, Any] | None = None
    """The immutable request for this workflow, including direct endpoint inputs."""


class CommandPhase(StrEnum):
    INTENT = "INTENT"
    """Recorded before the POST. The adapter may or may not have received it."""
    SENT = "SENT"
    """The adapter answered 202 with an operation id."""
    SETTLING = "SETTLING"
    """The operation finished; waiting for the adapter to release the resource."""
    UNCERTAIN = "UNCERTAIN"
    """Whether it took effect is unknown. Only RECONCILE may run until this is resolved."""
    SUPERSEDED = "SUPERSEDED"
    """Never took effect; the next attempt replaces it with a fresh revision and key."""
    DONE = "DONE"
    FAILED = "FAILED"


class CommandRecord(Model):
    workflow_id: str
    resource: ResourceKind
    slot: str
    """Names this command within the workflow. RESUME runs once forward and once on rollback."""
    action: str
    attempt: int
    idempotency_key: str
    body: dict[str, Any]
    phase: CommandPhase = CommandPhase.INTENT
    send_attempts: int = 0
    operation_id: str | None = None
    operation_status: str | None = None
    observation: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    retry_epoch: int = 0
    tries: int = 1
    """Attempts within this epoch. The attempt number keeps counting across epochs."""
    clock_started_at: datetime | None = None
    """When the current epoch started waiting for this command."""
    reconcile_attempts: int = 0
    reconcile_started_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WorkflowEvent(Model):
    cursor: str = ""
    at: datetime
    type: str
    stage: int | None = None
    resource: ResourceKind | None = None
    actor: str = "engine"
    message: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class TrafficRecord(Model):
    operation_id: str
    requested: dict[str, int]
    applied: dict[str, float] | None = None
    effective_percent: dict[str, float] | None = None
    verified: bool = False
    config_verified: bool = False
    adapter_operation_id: str | None = None
    operation_status: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    at: datetime


class HoldDesired(StrEnum):
    RUN = "RUN"
    HOLD = "HOLD"


class RedisRoute(StrEnum):
    """DIRECT is accepted in old actor reports; directives always use the fixed proxy."""

    DIRECT = "DIRECT"
    PROXY = "PROXY"


class HoldState(Model):
    """What every process of a workload is told to do. Survives platform restarts."""

    scope: str
    desired: HoldDesired = HoldDesired.RUN
    generation: int = 0
    workflow_id: str | None = None
    requested_at: datetime | None = None
    held_since: datetime | None = None
    released_at: datetime | None = None
    object_migration_id: str | None = None
    redis_route: RedisRoute = RedisRoute.PROXY
    redis_endpoint: str | None = None
    """For DIRECT: the environment whose Redis to use. None leaves the application's own default."""
    redis_route_generation: int = 0
    """Moves with every route change, so a report can be matched to the order it follows."""


class ActorState(StrEnum):
    RUNNING = "RUNNING"
    HOLDING = "HOLDING"
    HELD = "HELD"


class ActorAck(Model):
    active_web_sockets: int | None = None
    scope: str
    actor_id: str
    instance_id: str
    generation: int
    state: ActorState
    in_flight: int
    observed_at: datetime
    redis_route: RedisRoute | None = None
    """None: a process that does not switch its Redis connection."""
    redis_route_generation: int = 0
    stale_connections: int = 0
    """Redis connections still open on the route it left."""


class BusinessProbe(Model):
    probe_id: str
    env: str
    passed: bool
    reason: str = ""
    steps: dict[str, int] = Field(default_factory=dict)
    cleaned: bool = False
    started_at: datetime
    finished_at: datetime


class LatencyBucket(Model):
    """Light requests to one environment over a few seconds."""

    env: str
    started_at: datetime
    finished_at: datetime
    total: int
    failed: int
    latencies_ms: list[int] = Field(default_factory=list)


class User(Model):
    id: str
    github_id: int
    login: str
    avatar_url: str = ""


class Session(Model):
    user: User
    csrf_token: str
    expires_at: datetime
    created_at: datetime


class OAuthRequest(Model):
    code_verifier: str
    browser_binding: str
    return_to: str
    expires_at: datetime
