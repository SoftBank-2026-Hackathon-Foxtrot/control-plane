"""Typed access to the table. Key layout lives here and nowhere else.

| pk                | sk                             | content                           |
| ----------------- | ------------------------------ | --------------------------------- |
| WORKFLOWS         | ACTIVE                         | the one workflow allowed to run   |
| WORKFLOWS         | ID#{id}                        | index of all workflows            |
| WORKFLOW#{id}     | META                           | Workflow                          |
| WORKFLOW#{id}     | LEASE                          | the engine that owns it now       |
| WORKFLOW#{id}     | STAGE#{n}                      | StageState                        |
| WORKFLOW#{id}     | RESOURCE#{kind}                | ResourceState                     |
| WORKFLOW#{id}     | COMMAND#{kind}#{slot}#{nnn}    | CommandRecord                     |
| WORKFLOW#{id}     | EVENT#{cursor}                 | WorkflowEvent                     |
| WORKFLOW#{id}     | TRAFFIC#{operationId}          | TrafficRecord                     |
| WORKFLOW#{id}     | PROBE#{env}#{cursor}           | BusinessProbe                     |
| WORKFLOW#{id}     | LIGHT#{env}#{cursor}           | LatencyBucket                     |
| SCOPE#{scope}     | HOLD                           | HoldState                         |
| SCOPE#{scope}     | ACK#{actor}#{instance}         | ActorAck                          |
| SESSION#{hash}    | META                           | Session                           |
| OAUTH#{state}     | META                           | OAuthRequest                      |
| IDEMPOTENCY#{key} | META                           | stored console response           |
"""

from __future__ import annotations

import itertools
import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from control_plane.domain.models import (
    ActorAck,
    BusinessProbe,
    CommandRecord,
    HoldState,
    LatencyBucket,
    OAuthRequest,
    ResourceKind,
    ResourceState,
    Session,
    StageState,
    TrafficRecord,
    Workflow,
    WorkflowEvent,
)
from control_plane.store.table import Item, Table, VersionConflict

WORKFLOWS = "WORKFLOWS"
ACTIVE = "ACTIVE"
META = "META"
HOLD = "HOLD"


def _dump(model: BaseModel) -> str:
    return model.model_dump_json(by_alias=True)


def _dump_workflow(workflow: Workflow) -> str:
    if workflow.target_env == "onprem" and workflow.created_by_id is not None:
        body = workflow.model_dump(mode="json", by_alias=True)
        body["createdById"] = workflow.created_by_id
        return json.dumps(body)
    return _dump(workflow)


_sequence = itertools.count()


def _cursor(moment: datetime) -> str:
    """Sorts by time, then by write order within this process. The timeline must not reorder
    two events of one millisecond, and the random tail keeps two processes' writes apart."""
    return f"{int(moment.timestamp() * 1000):015d}-{next(_sequence):010d}-{secrets.token_hex(2)}"


class ActiveWorkflowExists(Exception):
    def __init__(self, workflow_id: str) -> None:
        super().__init__(workflow_id)
        self.workflow_id = workflow_id


class Repository:
    def __init__(self, table: Table) -> None:
        self.table = table

    async def application_binding(
        self, application: str, kind: ResourceKind
    ) -> dict[str, Any] | None:
        item = await self.table.get(f"APPLICATION#{application}", f"BINDING#{kind.value}")
        return dict(json.loads(item.body)) if item else None

    async def save_application_binding(
        self,
        application: str,
        kind: ResourceKind,
        binding: dict[str, Any],
        *,
        migration_id: str | None = None,
    ) -> None:
        pk, sk = f"APPLICATION#{application}", f"BINDING#{kind.value}"
        for _ in range(5):
            item = await self.table.get(pk, sk)
            saved = dict(json.loads(item.body)) if item else {}
            previous_id = saved.get("lastMigrationId")
            saved.update({key: value for key, value in binding.items() if key != "lastMigrationId"})
            if migration_id or previous_id:
                saved["lastMigrationId"] = migration_id or previous_id
            try:
                await self.table.put(
                    pk,
                    sk,
                    json.dumps(saved),
                    expected_version=item.version if item else 0,
                )
            except VersionConflict:
                continue
            return
        raise VersionConflict(f"binding {application}/{kind.value}")

    # ---- workflows ---------------------------------------------------------------------------
    async def create_workflow(self, workflow: Workflow) -> None:
        """Takes the single active slot first, so two requests cannot both start a migration."""
        try:
            await self.table.put(
                WORKFLOWS, ACTIVE, json.dumps({"id": workflow.id}), expected_version=0
            )
        except VersionConflict:
            active = await self.active_workflow_id()
            if active is None:  # finished between the write and the read
                raise
            raise ActiveWorkflowExists(active) from None
        await self.table.put(
            f"WORKFLOW#{workflow.id}", META, _dump_workflow(workflow), expected_version=0
        )
        await self.table.put(WORKFLOWS, f"ID#{workflow.id}", "{}", expected_version=0)

    async def active_workflow_id(self) -> str | None:
        item = await self.table.get(WORKFLOWS, ACTIVE)
        return str(json.loads(item.body)["id"]) if item else None

    async def release_active(self, workflow_id: str) -> None:
        if await self.active_workflow_id() == workflow_id:
            await self.table.delete(WORKFLOWS, ACTIVE)

    async def workflow(self, workflow_id: str) -> Workflow | None:
        item = await self.table.get(f"WORKFLOW#{workflow_id}", META)
        return self._workflow(item) if item else None

    async def workflow_ids(self) -> list[str]:
        page = await self.table.query(WORKFLOWS, "ID#")
        return [item.sk.removeprefix("ID#") for item in page.items]

    @staticmethod
    def _workflow(item: Item) -> Workflow:
        # The table's counter is the authority; the body's copy is only for the response.
        return Workflow.model_validate_json(item.body).model_copy(update={"version": item.version})

    async def save_workflow(self, workflow: Workflow) -> Workflow:
        """Raises VersionConflict if anyone changed it since it was read."""
        version = await self.table.put(
            f"WORKFLOW#{workflow.id}",
            META,
            _dump_workflow(workflow),
            expected_version=workflow.version,
        )
        return workflow.model_copy(update={"version": version})

    async def acquire_lease(
        self, workflow_id: str, owner: str, *, now: datetime, ttl_seconds: float
    ) -> bool:
        """Only the owner may drive a workflow. An expired lease can be taken over, but the new
        owner still compares recorded commands with the adapters before changing anything."""
        pk = f"WORKFLOW#{workflow_id}"
        item = await self.table.get(pk, "LEASE")
        current = json.loads(item.body) if item else None
        if current and current["owner"] != owner and datetime.fromisoformat(current["until"]) > now:
            return False
        if current and current["owner"] == owner:
            remaining = (datetime.fromisoformat(current["until"]) - now).total_seconds()
            if remaining > ttl_seconds / 2:
                return True
        until = now + timedelta(seconds=ttl_seconds)
        try:
            await self.table.put(
                pk,
                "LEASE",
                json.dumps({"owner": owner, "until": until.isoformat()}),
                expected_version=item.version if item else 0,
            )
        except VersionConflict:
            return False
        return True

    # ---- stages and resources ----------------------------------------------------------------
    async def stage(self, workflow_id: str, index: int) -> StageState | None:
        item = await self.table.get(f"WORKFLOW#{workflow_id}", f"STAGE#{index}")
        return StageState.model_validate_json(item.body) if item else None

    async def stages(self, workflow_id: str) -> list[StageState]:
        page = await self.table.query(f"WORKFLOW#{workflow_id}", "STAGE#")
        return sorted(
            (StageState.model_validate_json(item.body) for item in page.items),
            key=lambda stage: stage.index,
        )

    async def save_stage(self, workflow_id: str, stage: StageState) -> None:
        await self.table.put(
            f"WORKFLOW#{workflow_id}", f"STAGE#{stage.index}", _dump(stage), expected_version=None
        )

    async def resource(self, workflow_id: str, kind: ResourceKind) -> ResourceState:
        item = await self.table.get(f"WORKFLOW#{workflow_id}", f"RESOURCE#{kind.value}")
        return ResourceState.model_validate_json(item.body) if item else ResourceState(kind=kind)

    async def resources(self, workflow_id: str) -> list[ResourceState]:
        page = await self.table.query(f"WORKFLOW#{workflow_id}", "RESOURCE#")
        return [ResourceState.model_validate_json(item.body) for item in page.items]

    async def save_resource(self, workflow_id: str, resource: ResourceState) -> None:
        await self.table.put(
            f"WORKFLOW#{workflow_id}",
            f"RESOURCE#{resource.kind.value}",
            _dump(resource),
            expected_version=None,
        )

    # ---- commands ----------------------------------------------------------------------------
    @staticmethod
    def _command_prefix(kind: ResourceKind, slot: str) -> str:
        return f"COMMAND#{kind.value}#{slot}#"

    async def latest_command(
        self, workflow_id: str, kind: ResourceKind, slot: str
    ) -> CommandRecord | None:
        page = await self.table.query(f"WORKFLOW#{workflow_id}", self._command_prefix(kind, slot))
        if not page.items:
            return None
        return CommandRecord.model_validate_json(page.items[-1].body)

    async def commands(self, workflow_id: str) -> list[CommandRecord]:
        page = await self.table.query(f"WORKFLOW#{workflow_id}", "COMMAND#")
        return sorted(
            (CommandRecord.model_validate_json(item.body) for item in page.items),
            key=lambda command: command.created_at,
        )

    async def save_command(self, command: CommandRecord, *, new: bool = False) -> None:
        sk = f"{self._command_prefix(command.resource, command.slot)}{command.attempt:03d}"
        pk = f"WORKFLOW#{command.workflow_id}"
        if new:
            await self.table.put(pk, sk, _dump(command), expected_version=0)
        else:
            await self.table.put(pk, sk, _dump(command), expected_version=None)

    # ---- events ------------------------------------------------------------------------------
    async def add_event(self, workflow_id: str, event: WorkflowEvent) -> WorkflowEvent:
        cursor = _cursor(event.at)
        stored = event.model_copy(update={"cursor": cursor})
        await self.table.put(
            f"WORKFLOW#{workflow_id}", f"EVENT#{cursor}", _dump(stored), expected_version=0
        )
        return stored

    async def events(
        self, workflow_id: str, *, after: str | None = None, limit: int = 200
    ) -> tuple[list[WorkflowEvent], str | None]:
        page = await self.table.query(
            f"WORKFLOW#{workflow_id}",
            "EVENT#",
            after=f"EVENT#{after}" if after else None,
            limit=limit,
        )
        events = [WorkflowEvent.model_validate_json(item.body) for item in page.items]
        return events, page.next_sk.removeprefix("EVENT#") if page.next_sk else None

    # ---- traffic -----------------------------------------------------------------------------
    async def save_traffic(self, workflow_id: str, record: TrafficRecord) -> None:
        await self.table.put(
            f"WORKFLOW#{workflow_id}",
            f"TRAFFIC#{record.operation_id}",
            _dump(record),
            expected_version=None,
        )

    async def traffic_records(self, workflow_id: str) -> list[TrafficRecord]:
        page = await self.table.query(f"WORKFLOW#{workflow_id}", "TRAFFIC#")
        return sorted(
            (TrafficRecord.model_validate_json(item.body) for item in page.items),
            key=lambda record: record.at,
        )

    # ---- probes ------------------------------------------------------------------------------
    async def add_business_probe(self, workflow_id: str, probe: BusinessProbe) -> None:
        await self.table.put(
            f"WORKFLOW#{workflow_id}",
            f"PROBE#{probe.env}#{_cursor(probe.finished_at)}",
            _dump(probe),
            expected_version=0,
        )

    async def latest_business_probe(self, workflow_id: str, env: str) -> BusinessProbe | None:
        page = await self.table.query(f"WORKFLOW#{workflow_id}", f"PROBE#{env}#")
        if not page.items:
            return None
        return BusinessProbe.model_validate_json(page.items[-1].body)

    async def add_latency_bucket(self, workflow_id: str, bucket: LatencyBucket) -> None:
        await self.table.put(
            f"WORKFLOW#{workflow_id}",
            f"LIGHT#{bucket.env}#{_cursor(bucket.started_at)}",
            _dump(bucket),
            expected_version=0,
        )

    async def latency_buckets(
        self, workflow_id: str, env: str, *, since: datetime
    ) -> list[LatencyBucket]:
        page = await self.table.query(f"WORKFLOW#{workflow_id}", f"LIGHT#{env}#")
        buckets = [LatencyBucket.model_validate_json(item.body) for item in page.items]
        # By end time: a bucket that failed just after `since` may have started just before it.
        return [bucket for bucket in buckets if bucket.finished_at >= since]

    # ---- holds -------------------------------------------------------------------------------
    async def hold(self, scope: str) -> tuple[HoldState, int]:
        item = await self.table.get(f"SCOPE#{scope}", HOLD)
        if item is None:
            return HoldState(scope=scope), 0
        return HoldState.model_validate_json(item.body), item.version

    async def save_hold(self, hold: HoldState, *, expected_version: int) -> None:
        await self.table.put(
            f"SCOPE#{hold.scope}", HOLD, _dump(hold), expected_version=expected_version
        )

    async def save_ack(self, ack: ActorAck) -> None:
        await self.table.put(
            f"SCOPE#{ack.scope}",
            f"ACK#{ack.actor_id}#{ack.instance_id}",
            _dump(ack),
            expected_version=None,
        )

    async def acks(self, scope: str) -> list[ActorAck]:
        page = await self.table.query(f"SCOPE#{scope}", "ACK#")
        return [ActorAck.model_validate_json(item.body) for item in page.items]

    async def delete_ack(self, ack: ActorAck) -> None:
        await self.table.delete(f"SCOPE#{ack.scope}", f"ACK#{ack.actor_id}#{ack.instance_id}")

    # ---- sessions ----------------------------------------------------------------------------
    async def save_session(self, session_hash: str, session: Session) -> None:
        await self.table.put(f"SESSION#{session_hash}", META, _dump(session), expected_version=None)

    async def session(self, session_hash: str) -> Session | None:
        item = await self.table.get(f"SESSION#{session_hash}", META)
        return Session.model_validate_json(item.body) if item else None

    async def delete_session(self, session_hash: str) -> None:
        await self.table.delete(f"SESSION#{session_hash}", META)

    async def save_oauth_request(self, state: str, request: OAuthRequest) -> None:
        await self.table.put(f"OAUTH#{state}", META, _dump(request), expected_version=0)

    async def consume_oauth_request(self, state: str) -> OAuthRequest | None:
        """Single use: only the writer that wins the swap gets the request back."""
        item = await self.table.get(f"OAUTH#{state}", META)
        if item is None or item.body == "{}":
            return None
        try:
            await self.table.put(f"OAUTH#{state}", META, "{}", expected_version=item.version)
        except VersionConflict:
            return None
        return OAuthRequest.model_validate_json(item.body)

    # ---- console idempotency -----------------------------------------------------------------
    async def idempotent_response(self, key: str) -> dict[str, Any] | None:
        item = await self.table.get(f"IDEMPOTENCY#{key}", META)
        return dict(json.loads(item.body)) if item else None

    async def claim_idempotency(self, key: str, record: dict[str, Any]) -> bool:
        try:
            await self.table.put(f"IDEMPOTENCY#{key}", META, json.dumps(record), expected_version=0)
        except VersionConflict:
            return False
        return True

    async def forget_idempotency(self, key: str) -> None:
        await self.table.delete(f"IDEMPOTENCY#{key}", META)

    async def complete_idempotency(self, key: str, record: dict[str, Any]) -> None:
        await self.table.put(f"IDEMPOTENCY#{key}", META, json.dumps(record), expected_version=None)
