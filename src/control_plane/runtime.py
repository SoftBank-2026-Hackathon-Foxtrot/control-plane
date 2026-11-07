"""Durable preparation of fixed images. Ambiguous mutations stop at UNKNOWN."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field

from control_plane.applications import ApplicationService, ImageSpec
from control_plane.clock import Clock
from control_plane.domain.models import Model
from control_plane.errors import ApiError
from control_plane.seed import Seed
from control_plane.store.repository import Repository
from control_plane.store.table import VersionConflict

log = logging.getLogger(__name__)


class RuntimeTarget(Model):
    provider: Literal["docker", "ecs"]
    host_port: int = Field(default=0, ge=0, le=65535)
    cluster: str = ""
    service: str = ""
    task_definition: str = ""
    container: str = "app"
    region: str = "ap-northeast-2"


class RuntimeRecord(Model):
    id: str
    owner_id: str
    application_id: str
    application_version: int
    environment_id: str
    image: ImageSpec
    target: RuntimeTarget
    app_url: str
    status: str = "PENDING"
    version: int = 0
    previous_ref: str | None = None
    result_ref: str | None = None
    error: str | None = None
    action: str = "prepare"
    action_started_at: datetime | None = None
    created_at: datetime


class RuntimeDriver(Protocol):
    async def previous(self, record: RuntimeRecord) -> str | None: ...
    async def start(self, record: RuntimeRecord) -> str: ...
    async def ready(self, record: RuntimeRecord) -> bool: ...
    async def restore(self, record: RuntimeRecord) -> None: ...

    async def discover(self, record: RuntimeRecord) -> str | None: ...

    async def restored(self, record: RuntimeRecord) -> bool: ...


class RuntimeService:
    def __init__(self, repo: Repository, clock: Clock, drivers: dict[str, RuntimeDriver]) -> None:
        self.repo, self.clock, self.drivers = repo, clock, drivers
        self.owner = f"runtime-{secrets.token_hex(6)}"

    def driver(self, target: RuntimeTarget) -> RuntimeDriver:
        key = f"{target.provider}:{target.region}"
        driver = self.drivers.get(key) or self.drivers.get(target.provider)
        if driver is None:
            raise ApiError(422, "RUNTIME_UNSUPPORTED", "this runtime is not enabled")
        return driver

    async def get(self, owner: str, operation_id: str) -> RuntimeRecord:
        item = await self.repo.table.get("RUNTIME", operation_id)
        if not item:
            raise ApiError(404, "NOT_FOUND", "no runtime operation")
        record = RuntimeRecord.model_validate_json(item.body)
        if record.owner_id != owner:
            raise ApiError(404, "NOT_FOUND", "no runtime operation")
        return record.model_copy(update={"version": item.version})

    async def save(self, record: RuntimeRecord) -> RuntimeRecord:
        version = await self.repo.table.put(
            "RUNTIME",
            record.id,
            record.model_dump_json(by_alias=True),
            expected_version=record.version,
        )
        return record.model_copy(update={"version": version})

    async def prepare(
        self,
        owner: str,
        app_id: str,
        env: str,
        version: int,
        target: RuntimeTarget,
    ) -> RuntimeRecord:
        apps = ApplicationService(self.repo, self.clock)
        app = await apps.get(owner, app_id)
        apps.expect(app, version)
        await self._no_migration()
        if not app.configuration or env not in app.configuration.environments:
            raise ApiError(422, "ENVIRONMENT_NOT_CONNECTED", "connect the approved environment")
        self.driver(target)
        if target.provider == "docker" and app.image.registry_credential_ref:
            raise ApiError(
                422, "REGISTRY_AUTH_UNSUPPORTED", "local Docker uses its credential store"
            )
        record = RuntimeRecord(
            id=f"run-{secrets.token_hex(8)}",
            owner_id=owner,
            application_id=app_id,
            application_version=app.version,
            environment_id=env,
            image=app.image,
            target=target,
            app_url=app.configuration.environments[env].app_url,
            created_at=self.clock.now(),
        )
        # One mutation process-wide, including runtime preparation and migration creation.
        try:
            await self.repo.table.write_many(
                [
                    ("WORKFLOWS", "ACTIVE", json.dumps({"id": record.id}), 0),
                    ("RUNTIME", "ACTIVE", json.dumps({"id": record.id}), 0),
                    ("RUNTIME", record.id, record.model_dump_json(by_alias=True), 0),
                ],
                checks=[(f"APPS#{owner}", app_id, app.version)],
            )
        except VersionConflict as error:
            raise ApiError(409, "RUNTIME_BUSY", "another operation owns the change slot") from error
        saved = record.model_copy(update={"version": 1})
        await apps.audit(app, "RUNTIME_REQUESTED", {"operationId": record.id, "environment": env})
        return saved

    async def _no_migration(self) -> None:
        if await self.repo.active_workflow_id():
            raise ApiError(409, "WORKFLOW_ACTIVE", "runtime changes wait for migration completion")

    async def request_restore(self, owner: str, operation: str, version: int) -> RuntimeRecord:
        await self._no_migration()
        record = await self.get(owner, operation)
        if record.version != version or record.status not in {"READY", "FAILED"}:
            raise ApiError(
                409, "INVALID_STATE", "restore requires the latest READY or FAILED operation"
            )
        for workflow_id in await self.repo.workflow_ids():
            workflow = await self.repo.workflow(workflow_id)
            if workflow and workflow.application == record.application_id:
                resources = await self.repo.resources(workflow_id)
                if any(r.target_writes_possible for r in resources):
                    raise ApiError(
                        409, "DATA_RECOVERY_REQUIRED", "data recovery must precede runtime restore"
                    )
        updated = record.model_copy(
            update={
                "status": "RESTORE_PENDING",
                "action": "restore",
                "action_started_at": self.clock.now(),
            }
        )
        try:
            await self.repo.table.write_many(
                [
                    ("WORKFLOWS", "ACTIVE", json.dumps({"id": record.id}), 0),
                    ("RUNTIME", "ACTIVE", json.dumps({"id": record.id}), 0),
                    ("RUNTIME", record.id, updated.model_dump_json(by_alias=True), record.version),
                ]
            )
        except VersionConflict as error:
            raise ApiError(409, "RUNTIME_BUSY", "another operation is active") from error
        return updated.model_copy(update={"version": record.version + 1})

    async def release(self, record: RuntimeRecord) -> None:
        await self.repo.table.delete("RUNTIME", "ACTIVE")
        await self.repo.release_active(record.id)

    async def reconcile(self, owner: str, operation: str, version: int) -> RuntimeRecord:
        record = await self.get(owner, operation)
        if record.version != version or record.status != "UNKNOWN":
            raise ApiError(409, "INVALID_STATE", "reconcile requires the latest UNKNOWN operation")
        driver = self.driver(record.target)
        if record.action == "restore" and await driver.restored(record):
            record.status, record.error = "RESTORED", None
            record = await self.save(record)
            await self.release(record)
            return record
        found = await driver.discover(record)
        if found and record.action == "prepare":
            record.result_ref, record.status, record.error = found, "VERIFYING", None
        else:
            return record
        record = await self.save(record)
        return record

    async def image_available(
        self,
        owner: str,
        app_id: str,
        image: ImageSpec,
        environment_id: str | None = None,
        configuration: Seed | None = None,
    ) -> bool:
        page = await self.repo.table.query("RUNTIME", "run-")
        for item in page.items:
            record = RuntimeRecord.model_validate_json(item.body)
            if (
                record.owner_id == owner
                and record.application_id == app_id
                and record.image == image
                and record.status == "READY"
                and (environment_id is None or record.environment_id == environment_id)
            ):
                try:
                    if await self.driver(record.target).ready(record):
                        return True
                except Exception:
                    log.info("runtime image could not be confirmed: %s", record.id)
        # Existing GCP/on-prem apps may report their pinned image through authenticated heartbeats.
        # This is managed-process attestation, not a cloud-provider container inspection.
        if configuration and environment_id in configuration.environments:
            assert environment_id is not None
            hold, _ = await self.repo.hold(configuration.scope)
            acks = await self.repo.acks(configuration.scope)
            for actor, expected in configuration.environments[environment_id].actors.items():
                matching = [
                    ack
                    for ack in acks
                    if ack.actor_id == actor
                    and ack.generation == hold.generation
                    and (self.clock.now() - ack.observed_at).total_seconds()
                    <= configuration.timings.instance_ttl
                ]
                if len(matching) < expected or any(
                    ack.image_ref != image.image_ref for ack in matching
                ):
                    return False
            return True
        return False

    async def tick(self) -> None:
        active = await self.repo.table.get("RUNTIME", "ACTIVE")
        if not active:
            return
        operation = json.loads(active.body)["id"]
        item = await self.repo.table.get("RUNTIME", operation)
        if not item:
            # Keep the lock when no durable record can explain its ownership.
            return
        record = RuntimeRecord.model_validate_json(item.body).model_copy(
            update={"version": item.version}
        )
        if not await self.repo.acquire_lease(
            operation, self.owner, now=self.clock.now(), ttl_seconds=180
        ):
            return
        if record.status == "UNKNOWN":
            return
        driver = self.driver(record.target)
        try:
            if record.status == "PENDING":
                record.previous_ref = await driver.previous(record)
                record.status = "APPLYING"
                record = await self.save(record)
                record.result_ref = await driver.start(record)
                record.status = "VERIFYING"
                await self.save(record)
                return
            if record.status == "RESTORE_PENDING":
                record.status = "RESTORING"
                record = await self.save(record)
                await driver.restore(record)
                record.status = "RESTORE_VERIFYING"
            elif record.status == "RESTORE_VERIFYING":
                if await driver.restored(record):
                    record.status = "RESTORED"
                elif (
                    record.action_started_at
                    and (self.clock.now() - record.action_started_at).total_seconds() > 300
                ):
                    record.status, record.error = "UNKNOWN", "RESTORE_TIMEOUT"
            elif record.status in {"APPLYING", "RESTORING"}:
                # A process died after recording intent. Only observation is safe.
                record.status = "UNKNOWN"
                record.error = "RECONCILE_REQUIRED"
            elif record.status == "VERIFYING":
                if record.result_ref and await driver.ready(record):
                    record.status, record.error = "READY", None
                elif (self.clock.now() - record.created_at).total_seconds() > 300:
                    record.status = "FAILED" if record.status == "VERIFYING" else "UNKNOWN"
                    record.error = "RUNTIME_NOT_READY"
            record = await self.save(record)
            if record.status in {"READY", "FAILED", "RESTORED"}:
                await self.release(record)
        except VersionConflict:
            return
        except Exception as error:
            log.error("runtime %s stopped: %s", record.id, type(error).__name__)
            record.status, record.error = "UNKNOWN", type(error).__name__
            await self.save(record)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("runtime loop failed")
            await asyncio.sleep(1)
