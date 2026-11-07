"""The loop that drives the active workflow.

All it holds in memory is its own identity. Each tick reads the state from the table, does one
thing and writes the result, so stopping the process between any two ticks loses nothing.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import replace

from control_plane.adapters.client import AdapterError, AdapterUnreachable
from control_plane.dbgate import DbGateError
from control_plane.domain.models import (
    Halt,
    HoldDesired,
    StageState,
    StageStatus,
    Workflow,
    WorkflowEvent,
    WorkflowStatus,
)
from control_plane.domain.stages import PLAN, requires_approval, skipped
from control_plane.engine.commands import CommandRunner
from control_plane.engine.context import Outcome, Passed, Services, StageContext, Stop, Wait
from control_plane.engine.evidence import RunningEvidence
from control_plane.engine.recovery import Abort, ManualReconcile, Rollback
from control_plane.engine.stages import build_handlers
from control_plane.store.table import VersionConflict
from control_plane.telemetry import get_telemetry

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, services: Services, *, owner: str | None = None) -> None:
        self.services = services
        self.owner = owner or f"engine-{secrets.token_hex(4)}"
        runner = CommandRunner()
        seed = services.seed
        self.handlers = build_handlers(runner, seed.canary_weights, seed.traffic_weights)
        self.rollback = Rollback(runner)
        self.abort = Abort(runner)
        self.reconcile = ManualReconcile(runner)
        self.running_evidence = RunningEvidence(services)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                # The loop must outlive any one bad tick: a stopped loop looks like a healthy,
                # idle platform while a migration sits half done.
                log.exception("engine tick failed")
            await asyncio.sleep(await self.interval())

    async def interval(self) -> float:
        """Seconds until the next tick: short while the application stands still.

        Every step of a cutover waits for the next tick, and a command takes three. At one second
        a tick, the platform alone kept the application stopped for about 12 seconds. Ticking
        faster only while the hold is in force shortens that without changing what a tick does.
        """
        timings = self.services.seed.timings
        try:
            hold = await self.services.coordinator.hold()
        except Exception:
            log.exception("could not read the hold")
            return timings.tick_interval
        if hold.desired is HoldDesired.HOLD:
            return timings.held_tick_interval
        return timings.tick_interval

    async def tick(self) -> None:
        repo = self.services.repo
        workflow_id = await repo.active_workflow_id()
        if workflow_id is None:
            return
        workflow = await repo.workflow(workflow_id)
        if workflow is None:
            return
        await self.running_evidence.tick(workflow)
        if not self._needs_work(workflow):
            return
        timings = self.services.seed.timings
        owned = await repo.acquire_lease(
            workflow.id,
            self.owner,
            now=self.services.clock.now(),
            ttl_seconds=timings.lease_ttl,
        )
        if not owned:
            return
        try:
            with get_telemetry("control-plane").operation("WORKFLOW_STEP", workflow=workflow.id):
                await self._drive(workflow)
        except VersionConflict:
            # A person's command won the race. Decide again from what is there now.
            log.info("workflow %s changed during a tick", workflow.id)

    @staticmethod
    def _needs_work(workflow: Workflow) -> bool:
        return workflow.status in {
            WorkflowStatus.RUNNING,
            WorkflowStatus.ROLLING_BACK,
            WorkflowStatus.ABORTING,
        } or (workflow.status is WorkflowStatus.HALTED and workflow.reconcile_requested)

    async def _drive(self, workflow: Workflow) -> None:
        stage = await self._current_stage(workflow)
        match workflow.status:
            case WorkflowStatus.RUNNING:
                await self._drive_stage(workflow, stage)
            case WorkflowStatus.ROLLING_BACK:
                await self._drive_rollback(workflow, stage)
            case WorkflowStatus.ABORTING:
                await self._drive_abort(workflow, stage)
            case WorkflowStatus.HALTED:
                await self._drive_reconcile(workflow, stage)

    async def _current_stage(self, workflow: Workflow) -> StageState:
        stage = await self.services.repo.stage(workflow.id, workflow.current_stage)
        if stage is None:
            definition = PLAN[workflow.current_stage]
            stage = StageState(index=workflow.current_stage, kind=definition.kind)
        return stage

    # ---- forward ------------------------------------------------------------------------------
    async def _drive_stage(self, workflow: Workflow, stage: StageState) -> None:
        if stage.status in (StageStatus.PASSED, StageStatus.SKIPPED):
            # The previous tick saved the stage but lost the race to save the workflow.
            await self._next_stage(workflow)
            return
        managed = frozenset(self.services.seed.managed)
        if skipped(stage.kind, managed):
            # The application has no such resource. Nothing to ask, nothing to move.
            stage.status = StageStatus.SKIPPED
            stage.finished_at = self.services.clock.now()
            stage.detail = "not managed by this application"
            await self.services.repo.save_stage(workflow.id, stage)
            await self._event(workflow, "STAGE_SKIPPED", stage.kind.value, stage.index)
            await self._next_stage(workflow)
            return
        if requires_approval(stage.index, managed) and workflow.approved_through < stage.index:
            await self._wait_for_approval(workflow)
            return
        if stage.status is not StageStatus.RUNNING:
            stage.status = StageStatus.RUNNING
            stage.started_at = stage.started_at or self.services.clock.now()
            await self._event(workflow, "STAGE_STARTED", stage.kind.value, stage.index)
        outcome = await self._attempt(workflow, stage, self.handlers[stage.kind].advance)
        await self._apply(workflow, stage, outcome)

    async def _attempt(
        self,
        workflow: Workflow,
        stage: StageState,
        handler: Callable[[StageContext], Awaitable[Outcome]],
    ) -> Outcome:
        ctx = StageContext(self.services, workflow, stage)
        if stage.data.get("epoch_seen") != workflow.retry_epoch:
            # Time spent waiting for a person must not count against the next attempt.
            stage.data["epoch_seen"] = workflow.retry_epoch
            ctx.clear_timers()
        try:
            outcome = await handler(ctx)
        except AdapterUnreachable as error:
            if ctx.elapsed("unreachable") > self.services.seed.timings.command_timeout:
                return Stop("ADAPTER_UNREACHABLE", str(error))
            return Wait(f"adapter unreachable: {error}")
        except AdapterError as error:
            return Stop(error.code, error.message, error.blockers, reconcile=True)
        except DbGateError as error:
            return Stop("DB_GATE_REFUSED", str(error))
        except LookupError as error:
            return Stop("INCONSISTENT_STATE", str(error))
        except VersionConflict:
            raise
        except Exception as error:
            log.exception("stage %s failed", stage.kind)
            return Stop("INTERNAL_ERROR", f"{type(error).__name__}: {error}")
        ctx.reset_timer("unreachable")
        return outcome

    async def _apply(self, workflow: Workflow, stage: StageState, outcome: Outcome) -> None:
        repo = self.services.repo
        match outcome:
            case Wait(detail):
                stage.detail = detail
                stage.step = str(stage.data.get("phase", ""))
                await repo.save_stage(workflow.id, stage)
            case Passed():
                stage.status = StageStatus.PASSED
                stage.finished_at = self.services.clock.now()
                stage.detail = ""
                await repo.save_stage(workflow.id, stage)
                await self._event(workflow, "STAGE_PASSED", stage.kind.value, stage.index)
                await self._next_stage(workflow)
            case Stop():
                await self._halt(workflow, stage, outcome)

    async def _next_stage(self, workflow: Workflow) -> None:
        repo = self.services.repo
        following = workflow.current_stage + 1
        if following >= len(PLAN):
            finished = workflow.model_copy(
                update={
                    "status": WorkflowStatus.COMPLETED,
                    "finished_at": self.services.clock.now(),
                }
            )
            await repo.save_workflow(finished)
            await repo.release_active(workflow.id)
            await self._event(workflow, "WORKFLOW_COMPLETED", "", workflow.current_stage)
            return
        await repo.save_workflow(workflow.model_copy(update={"current_stage": following}))

    async def _wait_for_approval(self, workflow: Workflow) -> None:
        await self.services.repo.save_workflow(
            workflow.model_copy(update={"status": WorkflowStatus.WAITING_APPROVAL})
        )
        await self._event(
            workflow,
            "APPROVAL_REQUIRED",
            PLAN[workflow.current_stage].kind.value,
            workflow.current_stage,
        )

    async def _halt(self, workflow: Workflow, stage: StageState, stop: Stop) -> None:
        repo = self.services.repo
        stage.status = StageStatus.HALTED
        stage.detail = stop.message
        await repo.save_stage(workflow.id, stage)
        # A person deciding what to do must see at a glance that users are waiting on them.
        hold = await self.services.coordinator.hold()
        if hold.desired is HoldDesired.HOLD:
            stop = replace(stop, blockers=[*stop.blockers, "WORKLOAD_HELD"])
        await repo.save_workflow(
            workflow.model_copy(
                update={
                    "status": WorkflowStatus.HALTED,
                    "halt": self._halt_record(stop),
                    "rollback": None,
                    "reconcile_requested": stop.reconcile or workflow.reconcile_requested,
                    "reconcile_count": workflow.reconcile_count + (1 if stop.reconcile else 0),
                }
            )
        )
        await repo.add_event(
            workflow.id,
            WorkflowEvent(
                at=self.services.clock.now(),
                type="WORKFLOW_HALTED",
                stage=stage.index,
                resource=stop.resource,
                message=f"{stop.code}: {stop.message}",
                evidence={"blockers": stop.blockers},
            ),
        )

    def _halt_record(self, stop: Stop) -> Halt:
        return Halt(
            code=stop.code,
            message=stop.message,
            blockers=stop.blockers,
            resource=stop.resource,
            at=self.services.clock.now(),
        )

    # ---- ways out -----------------------------------------------------------------------------
    async def _drive_rollback(self, workflow: Workflow, stage: StageState) -> None:
        outcome = await self._attempt(workflow, stage, self.rollback.advance)
        assert workflow.rollback is not None
        match outcome:
            case Wait(detail):
                stage.detail = detail
                await self.services.repo.save_stage(workflow.id, stage)
            case Stop():
                await self._halt(workflow, stage, outcome)
            case Passed():
                resource = workflow.rollback.resource
                await self.services.repo.save_stage(workflow.id, stage)
                await self.services.repo.save_workflow(
                    workflow.model_copy(
                        update={
                            "status": WorkflowStatus.HALTED,
                            "rollback": None,
                            "rolled_back": [*workflow.rolled_back, resource],
                            "halt": self._halt_record(
                                Stop(
                                    "ROLLBACK_COMPLETED",
                                    f"{resource.value} is back on its source",
                                    resource=resource,
                                )
                            ),
                        }
                    )
                )
                await self._event(workflow, "ROLLBACK_COMPLETED", resource.value, stage.index)

    async def _drive_abort(self, workflow: Workflow, stage: StageState) -> None:
        outcome = await self._attempt(workflow, stage, self.abort.advance)
        match outcome:
            case Wait(detail):
                stage.detail = detail
                await self.services.repo.save_stage(workflow.id, stage)
            case Stop():
                await self._halt(workflow, stage, outcome)
            case Passed():
                await self.services.repo.save_stage(workflow.id, stage)
                await self.services.repo.save_workflow(
                    workflow.model_copy(
                        update={
                            "status": WorkflowStatus.ABORTED,
                            "finished_at": self.services.clock.now(),
                        }
                    )
                )
                await self.services.repo.release_active(workflow.id)
                await self._event(workflow, "WORKFLOW_ABORTED", "", stage.index)

    async def _drive_reconcile(self, workflow: Workflow, stage: StageState) -> None:
        outcome = await self._attempt(workflow, stage, self.reconcile.advance)
        match outcome:
            case Wait(detail):
                stage.detail = detail
                await self.services.repo.save_stage(workflow.id, stage)
            case Stop():
                # The flag clears so a person is not locked out, but they are told it failed.
                await self.services.repo.save_stage(workflow.id, stage)
                await self.services.repo.save_workflow(
                    workflow.model_copy(
                        update={
                            "reconcile_requested": False,
                            "halt": self._halt_record(outcome),
                        }
                    )
                )
                await self._event(workflow, "RECONCILE_FAILED", outcome.message, stage.index)
            case Passed():
                await self._finish_reconcile(workflow, stage)

    async def _finish_reconcile(self, workflow: Workflow, stage: StageState) -> None:
        await self.services.repo.save_stage(workflow.id, stage)
        await self.services.repo.save_workflow(
            workflow.model_copy(update={"reconcile_requested": False})
        )
        await self._event(workflow, "RECONCILE_FINISHED", "", stage.index)

    async def _event(self, workflow: Workflow, type_: str, message: str, stage: int) -> None:
        await self.services.repo.add_event(
            workflow.id,
            WorkflowEvent(at=self.services.clock.now(), type=type_, stage=stage, message=message),
        )
