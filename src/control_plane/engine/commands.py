"""Runs one adapter command so that it has at most one effect, whatever fails around it.

A command moves through recorded phases, and each tick does the next thing its phase calls for:

    INTENT -> SENT -> SETTLING -> DONE
       \\        \\-> FAILED
        \\-> UNCERTAIN -> (RECONCILE) -> DONE, or a new attempt
        \\-> SUPERSEDED -> a new attempt

The record is written before the POST. After a crash the same key and body are sent again, and the
adapter answers with the original operation instead of starting a second one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from control_plane.adapters.client import AdapterError, AdapterUnreachable, OperationView
from control_plane.adapters.profiles import PROFILES
from control_plane.domain.models import CommandPhase, CommandRecord, ResourceKind
from control_plane.domain.stages import EXPECTED_LIFECYCLE_AFTER
from control_plane.engine.context import Outcome, Passed, StageContext, Stop, Wait

RECONCILE = "RECONCILE"
MAX_ATTEMPTS = 8

RETRY_WITH_NEW_KEY = frozenset(
    {"RESOURCE_BUSY", "QUIESCE_EVIDENCE_EXPIRED", "QUIESCE_EVIDENCE_INVALID"}
)
"""Refused before anything happened, for a reason that passes. The object adapter remembers
refusals under the key, so the same key could get the same refusal for ever."""


@dataclass(frozen=True)
class Command:
    resource: ResourceKind
    action: str
    slot: str | None = None
    extra: Callable[[], Awaitable[dict[str, Any]]] | None = None
    """Called right before the first send. Evidence must be as fresh as possible."""
    expected: frozenset[str] = frozenset()
    """The lifecycles that prove it took effect, if its own answer is lost."""
    not_allowed_timeout: float | None = None
    """How long to wait for the adapter to allow it."""
    wait_codes: frozenset[str] = frozenset()
    """Refusals that only mean "not yet", such as a retention period: ask again later."""
    wait_interval: float = 30

    @property
    def name(self) -> str:
        return self.slot or self.action


class CommandRunner:
    async def run(self, ctx: StageContext, command: Command) -> Outcome:
        """Moves the command forward by one tick."""
        record = await ctx.services.repo.latest_command(
            ctx.workflow.id, command.resource, command.name
        )
        if record is None:
            return await self._begin(ctx, command, attempt=1, previous=None)
        match record.phase:
            case CommandPhase.DONE:
                return Passed()
            case CommandPhase.SUPERSEDED:
                return await self._begin(ctx, command, attempt=record.attempt + 1, previous=record)
            case CommandPhase.FAILED:
                if record.retry_epoch >= ctx.workflow.retry_epoch:
                    return self._failure(command, record)
                # A person reviewed the failure and chose to go on.
                return await self._begin(ctx, command, attempt=record.attempt + 1, previous=record)
            case CommandPhase.INTENT:
                return await self._send(ctx, command, record)
            case CommandPhase.SENT:
                return await self._poll(ctx, command, record)
            case CommandPhase.SETTLING:
                return await self._settle(ctx, command, record)
            case CommandPhase.UNCERTAIN:
                return await self._reconcile(ctx, command, record)

    async def _begin(
        self, ctx: StageContext, command: Command, *, attempt: int, previous: CommandRecord | None
    ) -> Outcome:
        tries = self._tries(ctx, command, previous)
        if tries > MAX_ATTEMPTS:
            return Stop(
                "COMMAND_ATTEMPTS_EXHAUSTED",
                f"{command.name} did not go through after {MAX_ATTEMPTS} attempts",
                resource=command.resource,
            )
        if self._still_waiting(ctx, command, previous):
            return Wait(f"{command.action}: not allowed yet")
        view = await ctx.view(command.resource, fresh=True)
        if previous is not None and view.lifecycle in command.expected:
            previous.phase = CommandPhase.DONE
            await self._save(ctx, previous)
            return Passed()
        # The object adapter keeps an operation active while its outcome is unknown, and takes
        # RECONCILE anyway. What it allows decides, not the field.
        if view.active_operation_id and command.action not in view.allowed_actions:
            if ctx.elapsed(f"busy:{command.name}") > ctx.seed.timings.command_timeout:
                return Stop(
                    "RESOURCE_BUSY",
                    f"{view.active_operation_id} did not finish",
                    resource=command.resource,
                )
            return Wait(f"{view.active_operation_id} is running")
        ctx.reset_timer(f"busy:{command.name}")
        timer = f"blocked:{command.name}"
        if command.action != RECONCILE and command.action not in view.allowed_actions:
            limit = command.not_allowed_timeout or ctx.seed.timings.not_allowed_timeout
            if ctx.elapsed(timer) > limit:
                return Stop(
                    "ACTION_NOT_ALLOWED",
                    f"{command.action} is not allowed in {view.lifecycle}",
                    blockers=view.allowed_actions,
                    resource=command.resource,
                )
            return Wait(f"waiting for {command.action}: lifecycle is {view.lifecycle}")
        ctx.reset_timer(timer)

        extra = await command.extra() if command.extra else {}
        if command.extra:
            # Writing evidence can move the adapter's revision.
            view = await ctx.view(command.resource, fresh=True)
        key = ":".join(
            (ctx.workflow.id, view.migration_id, command.name, str(view.revision), str(attempt))
        )
        now = ctx.now
        record = CommandRecord(
            workflow_id=ctx.workflow.id,
            resource=command.resource,
            slot=command.name,
            action=command.action,
            attempt=attempt,
            idempotency_key=key,
            body={"action": command.action, "expectedRevision": view.revision, **extra},
            retry_epoch=ctx.workflow.retry_epoch,
            tries=tries,
            created_at=now,
            updated_at=now,
        )
        await ctx.services.repo.save_command(record, new=True)
        await ctx.emit(
            "COMMAND_STARTED",
            f"{command.action} (attempt {attempt})",
            resource=command.resource,
            evidence={"idempotencyKey": key, "revision": view.revision},
        )
        return await self._send(ctx, command, record)

    @staticmethod
    def _tries(ctx: StageContext, command: Command, previous: CommandRecord | None) -> int:
        if previous is None or previous.retry_epoch < ctx.workflow.retry_epoch:
            return 1
        waiting = previous.error and previous.error.get("code") in command.wait_codes
        return previous.tries if waiting else previous.tries + 1

    @staticmethod
    def _still_waiting(ctx: StageContext, command: Command, previous: CommandRecord | None) -> bool:
        if previous is None or not previous.error:
            return False
        if previous.error.get("code") not in command.wait_codes:
            return False
        return (ctx.now - previous.updated_at).total_seconds() < command.wait_interval

    async def _send(self, ctx: StageContext, command: Command, record: CommandRecord) -> Outcome:
        record.send_attempts += 1
        await self._save(ctx, record)
        resource = await ctx.resource(command.resource)
        assert resource.migration_id is not None
        try:
            record.operation_id = await ctx.client(command.resource).act(
                resource.migration_id, record.idempotency_key, record.body
            )
        except AdapterUnreachable as error:
            if self._expired(ctx, record):
                # It may have been received, so the command cannot simply be dropped.
                record.phase = CommandPhase.UNCERTAIN
                record.error = {"code": "UNREACHABLE", "message": str(error)}
                record.reconcile_started_at = ctx.now
                await self._save(ctx, record)
            return Wait(f"{command.action}: adapter unreachable")
        except AdapterError as error:
            return await self._refused(ctx, command, record, error)
        record.phase = CommandPhase.SENT
        await self._save(ctx, record)
        return Wait(f"{command.action} accepted")

    async def _refused(
        self, ctx: StageContext, command: Command, record: CommandRecord, error: AdapterError
    ) -> Outcome:
        record.error = {"code": error.code, "message": error.message, "blockers": error.blockers}
        if command.action == RECONCILE:
            # Whatever a reconcile answers, the caller reads the status again next. The error
            # stays on the record, for the caller that must report it. A moved revision only
            # means this one was not sent in time, so that one is retried.
            moved = error.code == "REVISION_CONFLICT"
            record.phase = CommandPhase.SUPERSEDED if moved else CommandPhase.DONE
        elif error.code in RETRY_WITH_NEW_KEY or error.code in command.wait_codes:
            record.phase = CommandPhase.SUPERSEDED
        elif error.code == "REVISION_CONFLICT" and record.send_attempts == 1:
            # The revision moved between the read and this first send. Nothing was accepted.
            record.phase = CommandPhase.SUPERSEDED
        elif error.code in {"REVISION_CONFLICT", "UNKNOWN_STATE"}:
            # On a repeat, a conflict can mean an earlier send was accepted before a crash.
            record.phase = CommandPhase.UNCERTAIN
            record.reconcile_started_at = ctx.now
        else:
            record.phase = CommandPhase.FAILED
        await self._save(ctx, record)
        if record.phase is CommandPhase.FAILED:
            return self._failure(command, record)
        if record.phase is CommandPhase.DONE:
            return Passed()
        return Wait(f"{command.action}: {error.code}")

    async def _poll(self, ctx: StageContext, command: Command, record: CommandRecord) -> Outcome:
        assert record.operation_id is not None
        if record.retry_epoch < ctx.workflow.retry_epoch:
            # Polling the same operation under a new epoch: its time limit starts again.
            record.retry_epoch = ctx.workflow.retry_epoch
            record.clock_started_at = ctx.now
            await self._save(ctx, record)
        try:
            operation = await ctx.client(command.resource).operation(record.operation_id)
        except AdapterUnreachable:
            return self._timed_wait(ctx, command, record, "adapter unreachable")
        except AdapterError as error:
            if error.status == 404:
                # The adapter lost it. Only its current state can say what happened.
                record.phase = CommandPhase.UNCERTAIN
                record.reconcile_started_at = ctx.now
                await self._save(ctx, record)
                return Wait(f"{command.action}: operation not found")
            # A read can lose a race with the adapter's own write. Nothing changed; read again.
            return self._timed_wait(ctx, command, record, error.code)
        return await self._observed(ctx, command, record, operation)

    async def _observed(
        self, ctx: StageContext, command: Command, record: CommandRecord, operation: OperationView
    ) -> Outcome:
        if record.operation_status != operation.status:
            record.operation_status = operation.status
            await self._save(ctx, record)
        match operation.status:
            case "SUCCEEDED":
                record.phase = CommandPhase.SETTLING
                await self._save(ctx, record)
                return await self._settle(ctx, command, record)
            case "FAILED":
                record.error = operation.error or {"code": "FAILED", "message": "no detail"}
                record.phase = (
                    CommandPhase.DONE if command.action == RECONCILE else CommandPhase.FAILED
                )
                await self._save(ctx, record)
                return (
                    Passed()
                    if record.phase is CommandPhase.DONE
                    else self._failure(command, record)
                )
            case "UNKNOWN":
                record.error = operation.error
                if command.action == RECONCILE:
                    record.phase = CommandPhase.DONE
                    await self._save(ctx, record)
                    return Passed()
                record.phase = CommandPhase.UNCERTAIN
                record.reconcile_started_at = ctx.now
                await self._save(ctx, record)
                await ctx.emit(
                    "COMMAND_UNKNOWN",
                    f"{command.action}: result unknown, reconciling",
                    resource=command.resource,
                    evidence={"operationId": record.operation_id, "error": operation.error},
                )
                return Wait(f"{command.action}: result unknown")
            case _:
                return self._timed_wait(ctx, command, record, f"operation {operation.status}")

    async def _settle(self, ctx: StageContext, command: Command, record: CommandRecord) -> Outcome:
        # An operation can report success just before the adapter frees the resource and moves
        # its revision. A command sent in that gap would be refused.
        view = await ctx.view(command.resource, fresh=True)
        if view.active_operation_id == record.operation_id:
            return self._timed_wait(ctx, command, record, "adapter is finishing")
        record.phase = CommandPhase.DONE
        await self._save(ctx, record)
        await ctx.emit(
            "COMMAND_SUCCEEDED",
            command.action,
            resource=command.resource,
            evidence={"operationId": record.operation_id, "lifecycle": view.lifecycle},
        )
        return Passed()

    async def _reconcile(
        self, ctx: StageContext, command: Command, record: CommandRecord
    ) -> Outcome:
        if record.retry_epoch < ctx.workflow.retry_epoch:
            # A person looked at it and asked to go on: the budget starts again.
            record.retry_epoch = ctx.workflow.retry_epoch
            record.reconcile_attempts = 0
            record.reconcile_started_at = ctx.now
            await self._save(ctx, record)
        budget = PROFILES[command.resource].unknown_budget(ctx.seed.timings)
        started = record.reconcile_started_at or record.created_at
        if (budget.attempts is not None and record.reconcile_attempts >= budget.attempts) or (
            budget.seconds is not None and (ctx.now - started).total_seconds() > budget.seconds
        ):
            return Stop(
                "RESULT_UNKNOWN",
                f"could not establish whether {command.action} took effect",
                blockers=[record.operation_id or record.idempotency_key],
                resource=command.resource,
            )
        nested = await self.run(
            ctx,
            Command(
                resource=command.resource,
                action=RECONCILE,
                slot=f"{command.name}~{record.attempt}.{record.reconcile_attempts}",
            ),
        )
        if not isinstance(nested, Passed):
            return nested
        return await self._after_reconcile(ctx, command, record)

    async def _after_reconcile(
        self, ctx: StageContext, command: Command, record: CommandRecord
    ) -> Outcome:
        if record.operation_id:
            try:
                operation = await ctx.client(command.resource).operation(record.operation_id)
            except (AdapterError, AdapterUnreachable):
                operation = None
            # An unknown result is not final: some adapters settle it during the reconcile.
            if operation and operation.status == "SUCCEEDED":
                record.phase = CommandPhase.SETTLING
                await self._save(ctx, record)
                return Wait(f"{command.action}: confirmed by reconcile")
        view = await ctx.view(command.resource, fresh=True)
        if view.lifecycle in command.expected:
            record.phase = CommandPhase.DONE
            await self._save(ctx, record)
            await ctx.emit(
                "COMMAND_CONFIRMED",
                f"{command.action} took effect ({view.lifecycle})",
                resource=command.resource,
            )
            return Passed()
        if (
            view.health == "OK"
            and not view.active_operation_id
            and command.action in view.allowed_actions
        ):
            # The adapter has compared records with reality and allows the command again.
            record.phase = CommandPhase.SUPERSEDED
            await self._save(ctx, record)
            return Wait(f"{command.action}: will retry after reconcile")
        record.reconcile_attempts += 1
        await self._save(ctx, record)
        return Wait(f"{command.action}: still unresolved ({view.lifecycle}, {view.health})")

    @staticmethod
    def _failure(command: Command, record: CommandRecord) -> Stop:
        error = record.error or {}
        return Stop(
            str(error.get("code") or "COMMAND_FAILED"),
            f"{command.action}: {error.get('message') or 'failed'}",
            blockers=list(error.get("blockers") or []),
            resource=command.resource,
            # FAILED does not mean nothing changed. Compare before anyone decides.
            reconcile=True,
        )

    def _timed_wait(
        self, ctx: StageContext, command: Command, record: CommandRecord, detail: str
    ) -> Outcome:
        if self._expired(ctx, record):
            # The record stays as it is: going on later polls the same operation, not a new one.
            return Stop(
                "COMMAND_TIMEOUT",
                f"{command.action} did not finish: {detail}",
                blockers=[record.operation_id or record.idempotency_key],
                resource=command.resource,
            )
        return Wait(f"{command.action}: {detail}")

    @staticmethod
    def _expired(ctx: StageContext, record: CommandRecord) -> bool:
        since = record.clock_started_at or record.created_at
        return (ctx.now - since).total_seconds() > ctx.seed.timings.command_timeout

    @staticmethod
    async def _save(ctx: StageContext, record: CommandRecord) -> None:
        record.updated_at = ctx.now
        await ctx.services.repo.save_command(record)


def expected_after(action: str, location: str | None = None) -> frozenset[str]:
    return EXPECTED_LIFECYCLE_AFTER.get(f"{action}:{location}" if location else action, frozenset())
