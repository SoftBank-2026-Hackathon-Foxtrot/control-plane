"""Fixed application proxy bindings, separate from immutable workflow requests."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from control_plane.adapters.client import AdapterClient, AdapterError, AdapterUnreachable
from control_plane.adapters.profiles import PROFILES
from control_plane.domain.models import (
    Endpoint,
    EndpointPair,
    ResourceKind,
    ResourceState,
    Workflow,
)
from control_plane.errors import ApiError
from control_plane.seed import Seed
from control_plane.store.repository import Repository

FIXED_RESOURCES = (ResourceKind.DATABASE, ResourceKind.REDIS)


class BindingService:
    def __init__(
        self, repo: Repository, seed: Seed, adapters: dict[ResourceKind, AdapterClient]
    ) -> None:
        self.repo = repo
        self.seed = seed
        self.adapters = adapters

    def _fixed(self) -> list[ResourceKind]:
        """The fixed-proxy resources this application has."""
        return [kind for kind in FIXED_RESOURCES if kind in self.seed.managed]

    async def initialize(
        self, sources: dict[ResourceKind, Endpoint] | None = None
    ) -> dict[ResourceKind, dict[str, Any]]:
        sources = sources or {}
        if any(kind not in FIXED_RESOURCES for kind in sources):
            raise ApiError(
                422, "UNSUPPORTED_RESOURCE", "only database and Redis have fixed proxies"
            )
        if any(kind not in self.seed.managed for kind in sources):
            raise ApiError(
                422, "RESOURCE_NOT_MANAGED", "this application does not have that resource"
            )
        result = {}
        for kind in self._fixed():
            stored = await self.repo.application_binding(self.seed.application, kind)
            source = sources.get(kind) or self.seed.resources.of(kind).source
            if stored is None:
                if source is None:
                    continue  # Unchanged legacy resource registration remains in PREPARE.
                self._validate_profile(kind, source)
                if await self.repo.active_workflow_id():
                    raise ApiError(409, "WORKFLOW_ACTIVE", "cannot register during a migration")
                self._validate_endpoint(kind, source)
                body: dict[str, Any] = {
                    "resourceId": PROFILES[kind].resource_id,
                    "kind": kind.value,
                    "profile": PROFILES[kind].selected_profile(self.seed.resources.of(kind)),
                    "source": source.wire(kind),
                }
                if kind is ResourceKind.DATABASE:
                    database = self.seed.resources.database
                    assert database is not None  # _fixed() only yields managed resources
                    schema = database.schema_config
                    catalog = database.catalog
                    if schema is None and catalog is None:
                        raise ApiError(422, "SCHEMA_REQUIRED", "database schema.tables is required")
                    body["schema"] = (
                        schema.model_dump()
                        if schema
                        else {
                            "tables": catalog.tables,
                            "sequences": catalog.sequences,
                        }
                        if catalog
                        else {}
                    )
                    body["workload"] = {"participants": sorted(self.seed.all_actors())}
                try:
                    binding_id = await self.adapters[kind].register_binding(
                        f"binding:{self.seed.application}:{kind.value}", body
                    )
                except (AdapterError, AdapterUnreachable) as error:
                    raise self._error(kind, error) from error
                stored = {"bindingId": binding_id}
                # Save the accepted binding before polling; an interrupted registration is reused.
                await self.repo.save_application_binding(self.seed.application, kind, stored)
            current = await self._read(kind, stored)
            if kind in sources:
                self._match_source(kind, sources[kind], current)
            result[kind] = current
        return result

    async def list(self) -> dict[ResourceKind, dict[str, Any]]:
        result = {}
        for kind in self._fixed():
            stored = await self.repo.application_binding(self.seed.application, kind)
            if stored:
                result[kind] = await self._read(kind, stored)
        return result

    async def plan(
        self, workflow: Workflow, kind: ResourceKind, endpoints: EndpointPair | None
    ) -> ResourceState | None:
        if kind not in self.seed.managed:
            if endpoints is not None:
                raise ApiError(
                    422, "RESOURCE_NOT_MANAGED", f"{kind.value} is not managed by this application"
                )
            return None
        stored = await self.repo.application_binding(self.seed.application, kind)
        seed = self.seed.resources.of(kind)
        if endpoints is None and seed.source is None and stored is None:
            return None
        if stored is None:
            raise ApiError(
                409, "BINDING_REQUIRED", f"initialize {kind.value} proxy before migrating"
            )
        current = await self._read(kind, stored)
        if current.get("status") != "ACTIVE":
            raise ApiError(409, "BINDING_NOT_ACTIVE", f"{kind.value} proxy is not ready")
        previous_id = current.get("lastMigrationId") or stored.get("lastMigrationId")
        if previous_id:
            try:
                previous = await self.adapters[kind].migration(str(previous_id))
            except (AdapterError, AdapterUnreachable) as error:
                raise self._error(kind, error) from error
            if (
                previous.lifecycle not in {"COMPLETED", "CANCELLED"}
                or previous.health != "OK"
                or previous.active_operation_id
                or previous.route not in {"SOURCE", "TARGET"}
            ):
                raise ApiError(
                    409, "PREVIOUS_MIGRATION_UNFINISHED", f"{kind.value} needs reconciliation"
                )
        if endpoints is None:
            if seed.source is None or seed.target is None:
                raise ApiError(
                    422, "ENDPOINTS_REQUIRED", f"source and target required for {kind.value}"
                )
            endpoints = EndpointPair(source=seed.source, target=seed.target)
        self._validate_endpoint(kind, endpoints.source)
        self._validate_endpoint(kind, endpoints.target)
        if (
            kind is ResourceKind.DATABASE
            and seed.policy.rollback_mode == "REVERSE_REQUIRED"
            and any(
                endpoint.provider in {"AWS_RDS", "GCP_CLOUDSQL"}
                for endpoint in (endpoints.source, endpoints.target)
            )
        ):
            raise ApiError(
                422, "UNSUPPORTED_ROLLBACK_MODE", "managed database reverse CDC is unsupported"
            )
        self._validate_profile(kind, endpoints.source, endpoints.target)
        self._match_source(kind, endpoints.source, current)
        if endpoints.source.identity(kind) == endpoints.target.identity(kind):
            raise ApiError(422, "SAME_RESOURCE", f"{kind.value} source and target must differ")
        binding_ref = str(current.get("bindingRef") or current["bindingId"])
        scope = str(current.get("workloadScopeRef") or self.seed.scope)
        resource = ResourceState(kind=kind, binding_ref=binding_ref, workload_scope_ref=scope)
        body = PROFILES[kind].plan_body(workflow, seed, resource, self.seed.scope)
        body.pop("sourceRef", None)
        body.pop("targetRef", None)
        body.update(source=endpoints.source.wire(kind), target=endpoints.target.wire(kind))
        return resource.model_copy(update={"plan": body})

    async def _read(self, kind: ResourceKind, stored: dict[str, Any]) -> dict[str, Any]:
        try:
            current = await self.adapters[kind].binding(str(stored["bindingId"]))
        except (AdapterError, AdapterUnreachable) as error:
            raise self._error(kind, error) from error
        if current.get("bindingId") != stored["bindingId"]:
            raise ApiError(
                502, "INVALID_BINDING_RESPONSE", f"{kind.value} binding identity changed"
            )
        if stored.get("proxy") and stored["proxy"] != current.get("proxy"):
            raise ApiError(409, "PROXY_CHANGED", f"{kind.value} fixed proxy address changed")
        # Persist only the contract's public fields, never tokens or resolved credentials.
        safe = {
            key: current[key]
            for key in (
                "bindingId",
                "bindingRef",
                "resourceId",
                "kind",
                "profile",
                "status",
                "source",
                "proxy",
                "workloadScopeRef",
            )
            if key in current
        }
        if safe.get("source"):
            try:
                safe["source"] = Endpoint.model_validate(safe["source"]).wire(kind)
            except ValidationError:
                raise ApiError(
                    502, "INVALID_BINDING_RESPONSE", "invalid source description"
                ) from None
        if safe.get("proxy"):
            safe["proxy"] = {
                key: safe["proxy"][key]
                for key in (
                    "host",
                    "port",
                    "database",
                    "protocol",
                )
                if key in safe["proxy"]
            }
        if current.get("status") == "ACTIVE" and (not safe.get("source") or not safe.get("proxy")):
            raise ApiError(502, "INVALID_BINDING_RESPONSE", "active binding needs source and proxy")
        previous_id = current.get("lastMigrationId") or stored.get("lastMigrationId")
        if previous_id:
            safe["lastMigrationId"] = previous_id
        await self.repo.save_application_binding(self.seed.application, kind, safe)
        return safe

    @staticmethod
    def _match_source(kind: ResourceKind, expected: Endpoint, current: dict[str, Any]) -> None:
        source = current.get("source")
        if not source:
            raise ApiError(409, "SOURCE_UNKNOWN", f"{kind.value} proxy did not report its source")
        actual = Endpoint.model_validate(source)
        if actual.identity(kind) != expected.identity(kind):
            raise ApiError(
                409, "SOURCE_MISMATCH", f"{kind.value} source differs from the proxy route"
            )

    @staticmethod
    def _validate_endpoint(kind: ResourceKind, endpoint: Endpoint) -> None:
        if kind is ResourceKind.DATABASE and not endpoint.writer_role:
            raise ApiError(422, "WRITER_ROLE_REQUIRED", "database endpoints need writerRole")
        if kind is ResourceKind.REDIS and (
            endpoint.provider or endpoint.provider_instance or endpoint.writer_role
        ):
            raise ApiError(
                422, "INVALID_ENDPOINT", "database fields are not Redis endpoint options"
            )
        if kind is ResourceKind.REDIS and endpoint.database != "0":
            raise ApiError(422, "INVALID_ENDPOINT", "the Redis engine requires database '0'")

    def _validate_profile(
        self, kind: ResourceKind, source: Endpoint, target: Endpoint | None = None
    ) -> None:
        problem = PROFILES[kind].direct_input_problem(self.seed.resources.of(kind), source, target)
        if problem is not None:
            code, message = problem
            raise ApiError(422, code, message)

    @staticmethod
    def _error(kind: ResourceKind, error: AdapterError | AdapterUnreachable) -> ApiError:
        if isinstance(error, AdapterError):
            return ApiError(error.status, error.code, f"{kind.value} binding request refused")
        return ApiError(503, "ADAPTER_UNAVAILABLE", f"{kind.value} binding request unavailable")
