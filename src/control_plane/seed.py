"""The managed application, its environments and its resources, from one YAML file.

Registering applications through a screen is out of scope; what the platform manages is decided
at deployment. Every value here is checked at startup, so a bad file stops the process instead of
failing halfway through a migration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal, Self

import yaml
from pydantic import Field, model_validator

from control_plane.domain.models import Endpoint, Model, ResourceKind
from control_plane.domain.stages import CUTOVER_ORDER

MANAGED_LOGICAL_PROFILE: Final = "postgres-managed-logical-pgbouncer-v1"
ATOMIC_LOGICAL_PROFILE: Final = "postgres-managed-logical-pgbouncer-v2"
SELF_LOGICAL_PROFILE: Final = "postgres-logical-pgbouncer-v1"
DMS_PROFILE: Final = "gcp-dms-postgres-pgbouncer-v1"


ResourceKey = Literal["database", "redis", "object"]
RESOURCE_KEYS: Final[dict[ResourceKind, ResourceKey]] = {
    ResourceKind.DATABASE: "database",
    ResourceKind.REDIS: "redis",
    ResourceKind.OBJECT_STORAGE: "object",
}


class OnpremTargetUnconfirmed(ValueError):
    """The selected direction has no unambiguous data target."""


class Environment(Model):
    app_url: str
    traffic_pool: str
    actors: dict[str, int]
    """Actor id, as registered with the DB adapter, to the number of processes it runs."""
    resources: dict[ResourceKey, str] = Field(default_factory=dict)
    """This environment's resource refs (adapter keys). A workflow takes its source refs from its
    source environment and its target refs from its target environment, so either direction runs
    from the same seed."""


class Policy(Model):
    rollback_mode: str
    max_quiesce_seconds: float = Field(gt=0)


class DatabaseCatalog(Model):
    """What the DB adapter's catalog needs. Refs are the keys of `databases`."""

    databases: dict[str, dict[str, Any]]
    tables: list[str] = Field(min_length=1)
    sequences: list[str] = Field(default_factory=list)
    dms_job: dict[str, Any] | None = None
    alias: str
    app_ref: str


class AdapterEndpoint(Model):
    url: str
    token_secret: str
    verify: bool | str = True


class DatabaseSchema(Model):
    tables: list[str] = Field(min_length=1)
    sequences: list[str] = Field(default_factory=list)


class ResourceSeed(Model):
    source_ref: str | None = None
    target_ref: str | None = None
    source: Endpoint | None = None
    target: Endpoint | None = None
    adapter: AdapterEndpoint | None = None
    policy: Policy
    binding_ref: str | None = None
    replication_ref: str | None = None
    expected_apps: int | None = Field(default=None, ge=1)
    catalog: DatabaseCatalog | None = None
    direct_when_idle: bool = False
    """Retained only to reject obsolete temporary-proxy configuration explicitly."""

    @model_validator(mode="after")
    def endpoint_inputs(self) -> Self:
        if self.direct_when_idle:
            raise ValueError("directWhenIdle is no longer supported: applications always use proxy")
        if self.source is not None or self.target is not None:
            if self.source is None:
                raise ValueError("source is required before configuring a target")
            if self.source_ref is not None or self.target_ref is not None:
                raise ValueError("use endpoint objects or legacy refs, not both")
        elif (self.source_ref is None) != (self.target_ref is None):
            raise ValueError("sourceRef and targetRef go together")
        # With neither, the refs come from environments.*.resources (checked by Seed).
        return self

    @property
    def refs_from_environments(self) -> bool:
        return self.source is None and self.source_ref is None


class DatabaseSeed(ResourceSeed):
    gate_namespace: str | None = None
    schema_config: DatabaseSchema | None = Field(default=None, alias="schema")
    profile: Literal[
        "postgres-logical-pgbouncer-v1",
        "postgres-managed-logical-pgbouncer-v2",
        "postgres-managed-logical-pgbouncer-v1",
        "gcp-dms-postgres-pgbouncer-v1",
    ] = ATOMIC_LOGICAL_PROFILE

    @model_validator(mode="after")
    def replication(self) -> Self:
        if self.source is None and self.catalog is None:
            raise ValueError("database needs catalog")
        if self.source is not None:
            if self.schema_config is None:
                raise ValueError("direct database input needs schema.tables")
            if not self.source.writer_role or (self.target and not self.target.writer_role):
                raise ValueError("database endpoints need writerRole")
            if self.profile == DMS_PROFILE:
                raise ValueError("direct endpoint input uses the logical replication profiles")
            if (
                "profile" not in self.model_fields_set
                and self.source.provider in {None, "SELF_MANAGED"}
                and (self.target is None or self.target.provider in {None, "SELF_MANAGED"})
            ):
                self.profile = SELF_LOGICAL_PROFILE
        if self.profile == DMS_PROFILE:
            if not self.replication_ref or not self.catalog or not self.catalog.dms_job:
                raise ValueError("DMS profile needs replicationRef and catalog.dmsJob")
        elif self.replication_ref is not None or (
            self.catalog and self.catalog.dms_job is not None
        ):
            raise ValueError("managed logical profile rejects replicationRef and catalog.dmsJob")
        if self.policy.rollback_mode not in {"BEFORE_TARGET_WRITE", "REVERSE_REQUIRED"}:
            raise ValueError("unsupported database rollbackMode")
        if self.policy.rollback_mode == "REVERSE_REQUIRED" and self.profile not in {
            ATOMIC_LOGICAL_PROFILE,
            SELF_LOGICAL_PROFILE,
        }:
            raise ValueError(
                "database REVERSE_REQUIRED needs the self-managed or atomic v2 profile"
            )
        budget = self.policy.max_quiesce_seconds
        if not 1 <= budget <= 300 or not budget.is_integer():
            raise ValueError("database maxQuiesceSeconds must be an integer from 1 to 300")
        return self


class Resources(Model):
    """The resources this application has. Leave one out and the plan skips its cutover."""

    database: DatabaseSeed | None = None
    redis: ResourceSeed | None = None
    object: ResourceSeed | None = None

    @model_validator(mode="after")
    def complete(self) -> Self:
        if not self.managed():
            raise ValueError("resources needs at least one of database, redis and object")
        if self.redis is not None and self.redis.source is None and self.redis.binding_ref is None:
            raise ValueError("redis needs bindingRef")
        if self.object is not None:
            if self.object.binding_ref is None:
                raise ValueError("object needs bindingRef")
            if self.object.expected_apps is None:
                raise ValueError("object needs expectedApps: with none reporting, no guard applies")
        for resource in (self.database, self.object):
            if resource is not None and resource.direct_when_idle:
                raise ValueError("directWhenIdle applies to redis only")
        return self

    def _by_kind(self) -> dict[ResourceKind, ResourceSeed | None]:
        return {
            ResourceKind.DATABASE: self.database,
            ResourceKind.REDIS: self.redis,
            ResourceKind.OBJECT_STORAGE: self.object,
        }

    def managed(self) -> tuple[ResourceKind, ...]:
        """The resources present, in cutover order."""
        present = self._by_kind()
        return tuple(kind for kind in CUTOVER_ORDER if present[kind] is not None)

    def of(self, kind: ResourceKind) -> ResourceSeed:
        resource = self._by_kind()[kind]
        if resource is None:
            raise KeyError(f"{kind.value} is not managed by this application")
        return resource


class Adapters(Model):
    """One adapter per managed resource. An adapter for a resource left out is not called."""

    database: AdapterEndpoint | None = None
    redis: AdapterEndpoint | None = None
    object: AdapterEndpoint | None = None

    def of(self, kind: ResourceKind) -> AdapterEndpoint:
        endpoint = {
            ResourceKind.DATABASE: self.database,
            ResourceKind.REDIS: self.redis,
            ResourceKind.OBJECT_STORAGE: self.object,
        }[kind]
        if endpoint is None:
            raise KeyError(f"no adapter for {kind.value}")
        return endpoint


class ProbeSeed(Model):
    user: str = "probe-platform"
    groupbuy_id: int
    cleanup_path: str | None = None
    light_path: str = "/api/groupbuys"


class Timings(Model):
    """Seconds. The fixed defaults come from the design; a deployment may tune them."""

    command_timeout: float = 300
    not_allowed_timeout: float = 120
    sync_timeout: float = 3600
    apps_following_timeout: float = 120
    hold_timeout: float = 60
    resume_workload_timeout: float = 60
    advance_timeout: float = 300
    finalize_timeout: float = 7200
    stability_window: float = 30
    stability_max_gap: float = 10
    evidence_renew_interval: float = 8
    business_probe_interval: float = 1
    business_probe_timeout: float = 25
    business_probe_max_age: float = 30
    light_probe_rate_per_second: float = 5
    light_bucket_seconds: float = 1
    observation_window: float = 3
    min_light_samples: int = 3
    p95_factor: float = 1.5
    p95_margin_ms: float = 50
    instance_ttl: float = 15
    unknown_retry_limit: int = 3
    dms_unknown_deadline: float = 600
    lease_ttl: float = 15
    tick_interval: float = 1.0
    held_tick_interval: float = 0.1
    fast_poll_ms: int = 200
    idle_poll_ms: int = 5000
    redis_route_timeout: float = 120
    """How long every process may take to move its Redis connection."""
    redis_route_stable_window: float = 10
    """No direct connection may be reported for this long before the proxy entry counts."""


class SystemDeployment(Model):
    """Explicit read access to one shared app deployment; never a wildcard owner grant."""

    environment_id: str = Field(pattern=r"^env-[a-f0-9]{12}$")
    deployment_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    owner_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    environment: str = Field(min_length=1, max_length=64)


class Seed(Model):
    application: str
    scope: str
    environments: dict[str, Environment] = Field(min_length=2)
    resources: Resources
    adapters: Adapters = Field(default_factory=Adapters)
    probe: ProbeSeed
    canary_weights: list[int] = Field(default_factory=lambda: [5])
    traffic_weights: list[int] = Field(default_factory=lambda: [50, 100])
    timings: Timings = Field(default_factory=Timings)
    system_deployments: list[SystemDeployment] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def system_deployment_identity(self) -> Self:
        ids = [row.environment_id for row in self.system_deployments]
        identities = [
            (row.owner_id, row.deployment_id, row.environment) for row in self.system_deployments
        ]
        if len(ids) != len(set(ids)) or len(identities) != len(set(identities)):
            raise ValueError("systemDeployments must have unique IDs and identities")
        if any(row.environment not in self.environments for row in self.system_deployments):
            raise ValueError("systemDeployments environment must belong to this application")
        return self

    @model_validator(mode="after")
    def environment_refs(self) -> Self:
        for kind in self.resources.managed():
            resource = self.resources.of(kind)
            key = RESOURCE_KEYS[kind]
            if resource.source is not None:
                continue
            # The demo node may be registered before its data targets. Validate those at PREPARE.
            missing = [
                name
                for name, env in self.environments.items()
                if name != "onprem" and key not in env.resources
            ]
            if resource.refs_from_environments and missing:
                raise ValueError(
                    f"{key} has no sourceRef/targetRef: environments {missing} need resources.{key}"
                )
            if kind is ResourceKind.DATABASE and resource.catalog is not None:
                known = resource.catalog.databases
                unknown = [
                    env.resources[key]
                    for name, env in self.environments.items()
                    if name != "onprem" and key in env.resources and env.resources[key] not in known
                ]
                if unknown:
                    raise ValueError(f"database refs {unknown} are not in catalog.databases")
        return self

    def resource_seed(self, kind: ResourceKind, source_env: str, target_env: str) -> ResourceSeed:
        """The resource as a workflow from `source_env` to `target_env` uses it.

        Refs listed by both environments win over the resource's own sourceRef/targetRef.
        Endpoint-object input is returned as it is."""
        resource = self.resources.of(kind)
        key = RESOURCE_KEYS[kind]
        onprem = "onprem" in (source_env, target_env)
        if resource.source is not None:
            if onprem:
                raise OnpremTargetUnconfirmed(f"provide explicit {key} endpoints for this workflow")
            return resource
        source = self.environments[source_env].resources.get(key)
        target = self.environments[target_env].resources.get(key)
        if onprem:
            if not source or not target or source == target:
                raise OnpremTargetUnconfirmed(
                    f"configure distinct {key} refs in both workflow environments"
                )
            if (
                kind is ResourceKind.DATABASE
                and resource.catalog
                and (
                    source not in resource.catalog.databases
                    or target not in resource.catalog.databases
                )
            ):
                raise OnpremTargetUnconfirmed("register both database refs in the adapter catalog")
        if source is None or target is None:
            return resource
        return resource.model_copy(update={"source_ref": source, "target_ref": target})

    @model_validator(mode="after")
    def unique_actors(self) -> Self:
        for name, env in self.environments.items():
            if not env.actors and name != "onprem":
                raise ValueError(f"{name} must register workload actors")
            if name == "onprem" and any(count < 1 for count in env.actors.values()):
                raise ValueError("onprem actor counts must be positive")
        actors = [actor for env in self.environments.values() for actor in env.actors]
        if len(actors) != len(set(actors)):
            raise ValueError("actor ids must be unique across environments")
        for weight in (*self.canary_weights, *self.traffic_weights):
            if not 0 <= weight <= 100:
                raise ValueError("weights are percentages of the target")
        if not self.traffic_weights or self.traffic_weights[-1] != 100:
            raise ValueError("trafficWeights must end at 100")
        for kind in self.resources.managed():
            try:
                self.adapter(kind)
            except KeyError as error:
                raise ValueError(f"{kind.value} is managed but has no adapter") from error
        return self

    @property
    def managed(self) -> tuple[ResourceKind, ...]:
        """The resources this application has, in cutover order."""
        return self.resources.managed()

    def all_actors(self) -> dict[str, int]:
        return {a: n for env in self.environments.values() for a, n in env.actors.items()}

    def adapter(self, kind: ResourceKind) -> AdapterEndpoint:
        """The resource's own adapter, else the shared one. KeyError if the resource is absent."""
        return self.resources.of(kind).adapter or self.adapters.of(kind)


def load_seed(path: str | Path) -> Seed:
    with open(path, encoding="utf-8") as handle:
        return Seed.model_validate(yaml.safe_load(handle))
