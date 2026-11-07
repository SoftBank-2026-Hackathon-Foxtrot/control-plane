"""Personal applications: immutable images and versioned migration configuration."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Literal

from pydantic import Field

from control_plane.clock import Clock
from control_plane.domain.models import Model
from control_plane.errors import ApiError
from control_plane.seed import Seed
from control_plane.store.repository import Repository
from control_plane.store.table import VersionConflict


class ImageSpec(Model):
    image_ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}$")
    port: int = Field(default=8000, ge=1, le=65535)
    cpu: int = Field(default=256, ge=128, le=16384)
    memory: int = Field(default=512, ge=128, le=131072)
    architecture: Literal["X86_64", "ARM64"] = "X86_64"
    registry_credential_ref: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)


class MigrationPolicy(Model):
    allowed_directions: list[str] = Field(default_factory=list)
    max_quiesce_seconds: int = Field(default=300, ge=1, le=300)
    require_target_ready: Literal[True] = True
    allow_target_write_rollback: Literal[False] = False


class ApplicationInput(Model):
    name: str = Field(min_length=1, max_length=80)
    image: ImageSpec


class Application(Model):
    id: str
    owner_id: str
    name: str
    image: ImageSpec
    version: int = 0
    status: Literal["REGISTERED", "VERIFIED", "ARCHIVED"] = "REGISTERED"
    configuration: Seed | None = None
    policy: MigrationPolicy = Field(default_factory=MigrationPolicy)
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ApplicationService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo, self.clock = repo, clock

    async def get(self, owner: str, app_id: str) -> Application:
        item = await self.repo.table.get(f"APPS#{owner}", app_id)
        if item is None:
            raise ApiError(404, "NOT_FOUND", "no application")
        app = Application.model_validate_json(item.body)
        if app.status == "ARCHIVED":
            raise ApiError(404, "NOT_FOUND", "no application")
        return app.model_copy(update={"version": item.version})

    async def list(self, owner: str) -> list[Application]:
        page = await self.repo.table.query(f"APPS#{owner}", "app-")
        return [
            Application.model_validate_json(item.body).model_copy(update={"version": item.version})
            for item in page.items
            if Application.model_validate_json(item.body).status != "ARCHIVED"
        ]

    async def create(self, owner: str, body: ApplicationInput) -> Application:
        now = self.clock.now()
        app = Application(
            id=f"app-{secrets.token_hex(8)}",
            owner_id=owner,
            **body.model_dump(),
            created_at=now,
            updated_at=now,
        )
        return await self.save(app)

    async def idle(self, app: Application) -> None:
        active = await self.repo.active_workflow_id()
        workflow = await self.repo.workflow(active) if active else None
        if workflow and workflow.application == app.id and not workflow.status.terminal:
            raise ApiError(409, "APPLICATION_BUSY", "finish or recover the active migration first")

    async def save(self, app: Application) -> Application:
        try:
            version = await self.repo.table.put(
                f"APPS#{app.owner_id}",
                app.id,
                app.model_dump_json(by_alias=True),
                expected_version=app.version,
            )
        except VersionConflict as error:
            raise ApiError(
                409, "VERSION_CONFLICT", "read the latest application version"
            ) from error
        await self.audit(app, "APPLICATION_SAVED", {"version": version, "status": app.status})
        return app.model_copy(update={"version": version})

    async def audit(self, app: Application, event: str, details: dict[str, object]) -> None:
        at = self.clock.now().isoformat()
        await self.repo.table.put(
            f"APP_AUDIT#{app.owner_id}#{app.id}",
            f"{at}#{secrets.token_hex(6)}",
            json.dumps({"type": event, "at": at, "actor": app.owner_id, **details}),
            expected_version=0,
        )

    async def update(
        self, owner: str, app_id: str, body: ApplicationInput, expected_version: int
    ) -> Application:
        app = await self.get(owner, app_id)
        await self.idle(app)
        self.expect(app, expected_version)
        return await self.save(
            app.model_copy(
                update={
                    **body.model_dump(),
                    "image": body.image,
                    "status": "REGISTERED",
                    "verified_at": None,
                    "updated_at": self.clock.now(),
                }
            )
        )

    @staticmethod
    def expect(app: Application, version: int) -> None:
        if app.version != version:
            raise ApiError(409, "VERSION_CONFLICT", "read the latest application version")

    async def archive(self, owner: str, app_id: str, version: int) -> Application:
        app = await self.get(owner, app_id)
        await self.idle(app)
        self.expect(app, version)
        return await self.save(
            app.model_copy(
                update={
                    "status": "ARCHIVED",
                    "updated_at": self.clock.now(),
                }
            )
        )


class ConfigurationInput(Model):
    expected_version: int = Field(ge=1)


class Verification(Model):
    application_id: str
    version: int
    passed: bool
    checks: dict[str, bool]
    blockers: list[str]


async def bind_configuration(
    service: ApplicationService,
    owner: str,
    app_id: str,
    version: int,
    catalog: Seed,
) -> Application:
    app = await service.get(owner, app_id)
    await service.idle(app)
    service.expect(app, version)
    # Adapter credentials and database catalogs come from server-owned configuration, never JSON.
    configuration = catalog.model_copy(update={"application": app.id}, deep=True)
    scope_ref = json.dumps({"owner": owner, "id": app_id})
    current = await service.repo.table.get("APP_SCOPES", catalog.scope)
    if current and current.body != scope_ref:
        raise ApiError(409, "SCOPE_IN_USE", "this workload scope is registered to another app")
    if current is None:
        try:
            await service.repo.table.put("APP_SCOPES", catalog.scope, scope_ref, expected_version=0)
        except VersionConflict as error:
            raise ApiError(409, "SCOPE_IN_USE", "this workload scope was claimed") from error

    return await service.save(
        app.model_copy(
            update={
                "configuration": configuration,
                "status": "REGISTERED",
                "verified_at": None,
                "updated_at": service.clock.now(),
            }
        )
    )
