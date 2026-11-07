"""Read-only collection, separate from engine resource state and decision evidence."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from control_plane.adapters.client import AdapterClient
from control_plane.clock import Clock, stamp
from control_plane.domain.models import ResourceKind, Workflow
from control_plane.observation_projection import freshness, timestamp
from control_plane.store.repository import Repository
from control_plane.store.table import Table, VersionConflict
from control_plane.telemetry import get_telemetry
from control_plane.workload.coordinator import WorkloadCoordinator

log = logging.getLogger(__name__)


class ObservationStore:
    def __init__(self, table: Table) -> None:
        self.table = table

    async def latest(self, workflow: str, resource: str) -> dict[str, Any] | None:
        item = await self.table.get(f"WORKFLOW#{workflow}", f"OBS_LATEST#{resource}")
        return json.loads(item.body) if item else None

    async def save(
        self,
        workflow: str,
        resource: str,
        data: dict[str, Any] | None,
        at: datetime,
        *,
        error: str | None = None,
        expires_at: int | None = None,
        source: str = "OTEL",
    ) -> None:
        """CAS rejects older responses. Collection errors retain the last success."""
        pk, sk = f"WORKFLOW#{workflow}", f"OBS_LATEST#{resource}"
        for _ in range(5):
            item = await self.table.get(pk, sk)
            previous = json.loads(item.body) if item else {}
            if (timestamp(previous.get("collectedAt")) or at) > at:
                return
            prior_data = previous.get("data") or {}
            old_at = timestamp((prior_data.get("observed") or {}).get("observedAt"))
            new_at = timestamp(((data or {}).get("observed") or {}).get("observedAt"))
            if (
                data
                and data.get("migrationId") == prior_data.get("migrationId")
                and old_at
                and new_at
                and new_at < old_at
            ):
                data, error = None, "OUT_OF_ORDER_SOURCE"
            progress_at = previous.get("lastProgressAt")
            if data and made_progress(prior_data, data):
                progress_at = stamp(at)
            value = {
                "resource": resource,
                "source": source,
                "collectedAt": stamp(at),
                "collectionError": error,
                "lastSuccessAt": stamp(at) if data is not None else previous.get("lastSuccessAt"),
                "lastProgressAt": progress_at,
                "data": data if data is not None else prior_data,
            }
            body = json.dumps(value, allow_nan=False)
            try:
                await self.table.put(
                    pk,
                    sk,
                    body,
                    expected_version=item.version if item else 0,
                    expires_at=expires_at,
                )
            except VersionConflict:
                continue
            await self.table.put(
                pk,
                f"OBS#{stamp(at)}#{resource}",
                body,
                expected_version=None,
                expires_at=int((at + timedelta(hours=24)).timestamp()),
            )
            return

    async def history(
        self,
        workflow: str,
        *,
        since: datetime,
        until: datetime,
        after: str | None = None,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], str | None]:
        page = await self.table.query(
            f"WORKFLOW#{workflow}",
            "OBS#",
            start=f"OBS#{stamp(since)}",
            end=f"OBS#{stamp(until)}#\uffff",
            after=after,
            limit=limit,
        )
        return [json.loads(item.body) for item in page.items], page.next_sk

    async def own(self, workflow: str, owner: str, now: datetime, *, channel: str) -> bool:
        pk, sk = f"WORKFLOW#{workflow}", f"OBS_LEASE#{channel}"
        item = await self.table.get(pk, sk)
        prior = json.loads(item.body) if item else {}
        if prior.get("owner") != owner and (timestamp(prior.get("until")) or now) > now:
            return False
        try:
            await self.table.put(
                pk,
                sk,
                json.dumps({"owner": owner, "until": stamp(now + timedelta(seconds=30))}),
                expected_version=item.version if item else 0,
            )
        except VersionConflict:
            return False
        return True


def made_progress(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if not old or old.get("migrationId") != new.get("migrationId"):
        return False
    if any(old.get(key) != new.get(key) for key in ("phase", "lifecycle")):
        return True
    for key in ("completed", "bytes"):
        before, after = (old.get("progress") or {}).get(key), (new.get("progress") or {}).get(key)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and after > before:
            return True
    old_sync = ((old.get("details") or {}).get("redis") or {}).get("sync") or {}
    new_sync = ((new.get("details") or {}).get("redis") or {}).get("sync") or {}
    if new_sync.get("epoch") and all(
        old_sync.get(k) == new_sync.get(k) for k in ("epoch", "direction")
    ):
        for key, increasing in (("copied", True), ("backlog", False)):
            before, after = old_sync.get(key), new_sync.get(key)
            if (
                isinstance(before, (int, float))
                and isinstance(after, (int, float))
                and (after > before if increasing else after < before)
            ):
                return True
    for direction in ("forward", "reverse"):
        before = ((old.get("replication") or {}).get(direction) or {}).get("backlog")
        after = ((new.get("replication") or {}).get(direction) or {}).get("backlog")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and after < before:
            return True
    return False


def observation_state(snapshot: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if not snapshot:
        return {"freshness": "UNKNOWN", "collectionError": None}
    observed = (snapshot.get("data") or {}).get("observed") or {}
    state = freshness(observed.get("observedAt"), now)
    database = ((snapshot.get("data") or {}).get("details") or {}).get("database") or {}
    if snapshot.get("collectionError") or database.get("observationAvailable") is False:
        state = "UNAVAILABLE"
    return {key: value for key, value in snapshot.items() if key != "data"} | {"freshness": state}


async def watched_workflows(repo: Repository, now: datetime) -> list[Workflow]:
    workflows = []
    for workflow_id in await repo.workflow_ids():
        workflow = await repo.workflow(workflow_id)
        if workflow and (
            not workflow.status.terminal
            or (workflow.finished_at and now <= workflow.finished_at + timedelta(seconds=60))
        ):
            workflows.append(workflow)
    return workflows


class ObservationLoop:
    def __init__(
        self,
        repo: Repository,
        adapters: dict[ResourceKind, AdapterClient],
        clock: Clock,
        coordinator: WorkloadCoordinator | None = None,
    ) -> None:
        self.repo, self.adapters, self.clock = repo, adapters, clock
        self.store = ObservationStore(repo.table)
        self.owner = secrets.token_hex(12)
        self.coordinator = coordinator

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("observation cycle failed", exc_info=True)
            await asyncio.sleep(5)

    async def tick(self) -> None:
        for workflow in await watched_workflows(self.repo, self.clock.now()):
            if await self.store.own(workflow.id, self.owner, self.clock.now(), channel="resources"):
                await self.collect_app(workflow)

    async def collect_app(self, workflow: Workflow) -> None:
        if not self.coordinator:
            return
        hold, _ = await self.repo.hold(self.coordinator.scope)
        active = await self.repo.active_workflow_id()
        if active != workflow.id and not (active is None and hold.workflow_id == workflow.id):
            return
        actors = await self.coordinator.actor_statuses(hold.generation)
        now = self.clock.now()
        data = {
            "workflowId": workflow.id,
            "redisRoute": hold.redis_route,
            "redisRouteGeneration": hold.redis_route_generation,
            "desired": hold.desired.value,
            "generation": hold.generation,
            "heldSince": stamp(hold.held_since) if hold.held_since else None,
            "requestedAt": stamp(hold.requested_at) if hold.requested_at else None,
            "releasedAt": stamp(hold.released_at) if hold.released_at else None,
            "observed": {"observedAt": stamp(now)},
            "actors": [
                {
                    "actorId": actor.actor_id,
                    "state": actor.state,
                    "inFlight": actor.in_flight,
                    "activeWebSockets": actor.active_web_sockets,
                    "reported": actor.reported,
                    "expected": actor.expected,
                    "observedAt": stamp(actor.observed_at) if actor.observed_at else None,
                }
                for actor in actors
            ],
        }
        get_telemetry("control-plane").snapshot("APP", data)
