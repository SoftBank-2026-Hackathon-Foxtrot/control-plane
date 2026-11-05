"""The managed application, its environments and its resources, from one YAML file.

Registering applications through a screen is out of scope; what the platform manages is decided
at deployment. Every value here is checked at startup, so a bad file stops the process instead of
failing halfway through a migration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import Field, model_validator

from control_plane.domain.models import Model, ResourceKind


class Environment(Model):
    app_url: str
    traffic_pool: str
    actors: dict[str, int] = Field(min_length=1)
    """Actor id, as registered with the DB adapter, to the number of processes it runs."""


class Policy(Model):
    rollback_mode: str
    max_quiesce_seconds: float = Field(gt=0)


class DatabaseCatalog(Model):
    """What the DB adapter's catalog needs. Refs are the keys of `databases`."""

    databases: dict[str, dict[str, Any]]
    tables: list[str] = Field(min_length=1)
    sequences: list[str] = Field(default_factory=list)
    dms_job: dict[str, Any]
    alias: str
    app_ref: str


class ResourceSeed(Model):
    source_ref: str
    target_ref: str
    policy: Policy
    binding_ref: str | None = None
    replication_ref: str | None = None
    expected_apps: int | None = Field(default=None, ge=1)
    catalog: DatabaseCatalog | None = None


class Resources(Model):
    database: ResourceSeed
    redis: ResourceSeed
    object: ResourceSeed

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.database.catalog is None or self.database.replication_ref is None:
            raise ValueError("database needs catalog and replicationRef")
        if self.redis.binding_ref is None or self.object.binding_ref is None:
            raise ValueError("redis and object need bindingRef")
        if self.object.expected_apps is None:
            raise ValueError("object needs expectedApps: with none reporting, no guard applies")
        return self

    def of(self, kind: ResourceKind) -> ResourceSeed:
        return {
            ResourceKind.DATABASE: self.database,
            ResourceKind.REDIS: self.redis,
            ResourceKind.OBJECT_STORAGE: self.object,
        }[kind]


class AdapterEndpoint(Model):
    url: str
    token_secret: str
    verify: bool | str = True
    """TLS verification: true, false for plain local setups, or a CA bundle path."""


class Adapters(Model):
    database: AdapterEndpoint
    redis: AdapterEndpoint
    object: AdapterEndpoint

    def of(self, kind: ResourceKind) -> AdapterEndpoint:
        return {
            ResourceKind.DATABASE: self.database,
            ResourceKind.REDIS: self.redis,
            ResourceKind.OBJECT_STORAGE: self.object,
        }[kind]


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
    business_probe_interval: float = 15
    business_probe_timeout: float = 25
    business_probe_max_age: float = 30
    light_probe_rate_per_second: float = 2
    light_bucket_seconds: float = 5
    observation_window: float = 60
    min_light_samples: int = 30
    p95_factor: float = 1.5
    p95_margin_ms: float = 50
    instance_ttl: float = 15
    unknown_retry_limit: int = 3
    dms_unknown_deadline: float = 600
    lease_ttl: float = 15
    fast_poll_ms: int = 500
    idle_poll_ms: int = 5000


class Seed(Model):
    application: str
    scope: str
    environments: dict[str, Environment] = Field(min_length=2)
    resources: Resources
    adapters: Adapters
    probe: ProbeSeed
    canary_weights: list[int] = Field(default_factory=lambda: [5])
    traffic_weights: list[int] = Field(default_factory=lambda: [25, 50, 100])
    timings: Timings = Field(default_factory=Timings)

    @model_validator(mode="after")
    def unique_actors(self) -> Self:
        actors = [actor for env in self.environments.values() for actor in env.actors]
        if len(actors) != len(set(actors)):
            raise ValueError("actor ids must be unique across environments")
        for weight in (*self.canary_weights, *self.traffic_weights):
            if not 0 <= weight <= 100:
                raise ValueError("weights are percentages of the target")
        if not self.traffic_weights or self.traffic_weights[-1] != 100:
            raise ValueError("trafficWeights must end at 100")
        return self

    def all_actors(self) -> dict[str, int]:
        return {a: n for env in self.environments.values() for a, n in env.actors.items()}


def load_seed(path: str | Path) -> Seed:
    with open(path, encoding="utf-8") as handle:
        return Seed.model_validate(yaml.safe_load(handle))
