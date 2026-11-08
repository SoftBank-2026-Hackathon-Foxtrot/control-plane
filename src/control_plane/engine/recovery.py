"""The ways out of a stopped workflow. Each runs only because a person asked for it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from control_plane.adapters.profiles import PROFILES
from control_plane.domain.models import HoldDesired, ResourceKind
from control_plane.engine.commands import Command, CommandRunner, expected_after
from control_plane.engine.context import Outcome, Passed, StageContext, Stop, Wait
from control_plane.engine.evidence import (
    WorkloadNotHeld,
    business_probes,
    keep_evidence_alive,
    quiesce_reference,
    target_probe_failing,
)
from control_plane.engine.traffic_observation import set_traffic
from control_plane.engine.transition import (
    held_command,
    hold_workload,
    release_workload,
    transition_id,
)
from control_plane.traffic import TrafficError

Step = Callable[[StageContext, ResourceKind, dict[str, Any]], Awaitable[Outcome]]

CANCELLABLE_EARLY = frozenset({"CREATED", "PREPARED", "SYNCING", "READY", "QUIESCED", "FAILED"})
"""Lifecycles in which nothing has switched, so CANCEL can end the plan if the adapter allows it."""
LEFT_AS_THEY_ARE = frozenset({"ACTIVE", "RESTORED", "CANCELLED", "COMPLETED"})
"""A plan here runs on one side, alone. Aborting the workflow does not need to touch it."""


class Rollback:
    """Returns one resource to its source, then proves the application still works."""

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner
        self.steps: dict[str, Step] = {
            "CHECK": self._check,
            "HOLD": self._hold,
            "QUIESCE": self._quiesce,
            "READY": self._ready,
            "ROLLBACK": self._rollback,
            "RESUME": self._resume,
            "RELEASE": self._release,
            "VERIFY": self._verify,
        }

    async def advance(self, ctx: StageContext) -> Outcome:
        assert ctx.workflow.rollback is not None
        kind = ctx.workflow.rollback.resource
        data = ctx.stage.data.setdefault("rollback", {})
        try:
            outcome = await self.steps[data.setdefault("phase", "CHECK")](ctx, kind, data)
        except WorkloadNotHeld as error:
            outcome = Stop(
                "WORKLOAD_NOT_HELD", "the workload is not stopped", [str(error)], resource=kind
            )
        if isinstance(outcome, Passed | Stop):
            # A later rollback of this workflow decides again from the real state.
            ctx.stage.data.pop("rollback", None)
        return outcome

    async def finish_all(self, ctx: StageContext) -> Outcome:
        """Restore app traffic only after every data resource proves it is back on the source."""
        data = ctx.stage.data.setdefault("full_rollback", {})
        for kind in ctx.seed.managed:
            view = await ctx.view(kind, fresh=True)
            if (
                view.route != "SOURCE"
                or view.health != "OK"
                or (view.active_operation_id and not data.get("cleanup_started"))
            ):
                return Stop(
                    "ROLLBACK_ROUTE_UNCONFIRMED",
                    f"{kind.value} has not confirmed a healthy source route",
                    resource=kind,
                )
        if not data.get("traffic_restored_at"):
            if not await ctx.services.prober.is_ready(ctx.source_env.app_url):
                return Stop("SOURCE_APP_NOT_READY", "source application does not answer /readyz")
            try:
                await set_traffic(ctx, 0, "ROLLBACK")
            except TrafficError as error:
                return Stop("TRAFFIC_REVERT_FAILED", str(error))
            data["traffic_restored_at"] = ctx.now.isoformat()
            await ctx.emit("TRAFFIC_CHANGED", "target 0%: all resources restored")
            return Wait("source traffic restored; verifying application")
        probes, _ = await business_probes(ctx, datetime.fromisoformat(data["traffic_restored_at"]))
        if probes.ok:
            data["cleanup_started"] = True
            for kind in ctx.seed.managed:
                outcome = await self.runner.run(
                    ctx,
                    Command(
                        kind,
                        "FINALIZE",
                        slot="ROLLBACK_FINALIZE",
                        expected=expected_after("FINALIZE"),
                        not_allowed_timeout=ctx.seed.timings.finalize_timeout,
                    ),
                )
                if not isinstance(outcome, Passed):
                    return outcome
            for kind in ctx.seed.managed:
                view = await ctx.view(kind, fresh=True)
                if view.route != "SOURCE" or view.health != "OK":
                    return Stop(
                        "ROLLBACK_ROUTE_UNCONFIRMED",
                        "source route changed during cleanup",
                        resource=kind,
                    )
            return Passed()
        if ctx.elapsed("full_rollback_verify") > ctx.seed.timings.advance_timeout:
            return Stop("ROLLBACK_NOT_VERIFIED", probes.reason)
        return Wait(probes.reason)

    async def _check(self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]) -> Outcome:
        assert ctx.workflow.rollback is not None
        if (await ctx.resource(kind)).migration_id is None:
            return Stop("NOTHING_TO_ROLL_BACK", f"{kind.value} has no plan", resource=kind)
        view = await ctx.view(kind, fresh=True)
        if view.raw.get("transitionState") == "NONE":
            # It never moved. QUIESCE may be on offer, but it would pause a healthy resource.
            return Stop(
                "NOTHING_TO_ROLL_BACK",
                f"{kind.value} has not started to move: abort instead",
                resource=kind,
            )
        if view.lifecycle == "RESTORED":
            data["phase"] = "RELEASE"
            return Wait("already restored")
        if view.lifecycle == "ROLLED_BACK_HELD":
            data["phase"], data["next"] = "HOLD", "RESUME"
            return Wait("resuming on the source")
        if "ROLLBACK" not in view.allowed_actions and "QUIESCE" not in view.allowed_actions:
            return Stop(
                "ROLLBACK_UNAVAILABLE",
                f"{kind.value} cannot return to its source from {view.lifecycle}",
                list(view.readiness("rollback").reasons),
                resource=kind,
            )
        if ctx.workflow.rollback.mode == "failure":
            # This mode accepts losing what the reverse sync had not copied, and it does not check
            # that the target is really gone. A live target must never take this path.
            if not (
                ctx.workflow.rollback.target_failure_confirmed and await target_probe_failing(ctx)
            ):
                return Stop(
                    "TARGET_FAILURE_NOT_CONFIRMED",
                    "failure mode needs a person's confirmation and failing business probes",
                    resource=kind,
                )
        elif (
            not PROFILES[kind].rollback_needs_ready_after_hold(ctx.seed.resources.of(kind))
            and "ROLLBACK" in view.allowed_actions
            and view.readiness("rollback").result == "FAIL"
        ):
            return Stop(
                "ROLLBACK_NOT_SAFE",
                f"{kind.value} is not ready to return",
                list(view.readiness("rollback").reasons),
                resource=kind,
            )
        data["phase"] = "HOLD"
        data["next"] = "ROLLBACK" if "ROLLBACK" in view.allowed_actions else "QUIESCE"
        return Wait("starting rollback")

    async def _hold(self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]) -> Outcome:
        outcome = await hold_workload(ctx, kind)
        if isinstance(outcome, Passed):
            data["phase"] = (
                "READY"
                if data["next"] == "ROLLBACK"
                and PROFILES[kind].rollback_needs_ready_after_hold(ctx.seed.resources.of(kind))
                else data["next"]
            )
            return Wait("workload held")
        return outcome

    async def _quiesce(
        self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]
    ) -> Outcome:
        outcome = await held_command(self.runner, ctx, kind, "QUIESCE", slot="ROLLBACK_QUIESCE")
        if not isinstance(outcome, Passed):
            return outcome
        data["phase"] = (
            "READY"
            if PROFILES[kind].rollback_needs_ready_after_hold(ctx.seed.resources.of(kind))
            else "ROLLBACK"
        )
        return Wait("QUIESCE done")

    async def _ready(self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]) -> Outcome:
        if PROFILES[kind].rollback_needs_ready_after_hold(ctx.seed.resources.of(kind)):
            hold = await ctx.services.coordinator.hold()
            if hold.desired is not HoldDesired.HOLD:
                raise WorkloadNotHeld("no hold in force")
            await keep_evidence_alive(ctx, hold.generation)
            if not data.get("ready_evidence") or not ctx.stage.data.get("quiesce_ref"):
                await quiesce_reference(
                    ctx, kind, transition_id(ctx, kind), hold.generation, "ROLLBACK"
                )
                data["ready_evidence"] = True
                return Wait("rollback hold evidence published")
            view = await ctx.view(kind, fresh=True)
            decision = view.readiness("rollback")
            if decision.result == "FAIL":
                return Stop(
                    "ROLLBACK_NOT_SAFE",
                    "the adapter refused reverse replication rollback",
                    list(decision.reasons),
                    resource=kind,
                )
            if (
                not decision.passed
                or view.health != "OK"
                or view.active_operation_id
                or "ROLLBACK" not in view.allowed_actions
            ):
                if ctx.elapsed("rollback_ready") > ctx.seed.timings.command_timeout:
                    return Stop(
                        "ROLLBACK_NOT_READY",
                        "the adapter did not confirm rollback readiness; workload remains held",
                        list(decision.reasons),
                        resource=kind,
                    )
                return Wait("waiting for reverse replication rollback readiness")
        ctx.reset_timer("rollback_ready")
        data["phase"] = "ROLLBACK"
        return Wait("rollback ready")

    async def _rollback(
        self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]
    ) -> Outcome:
        assert ctx.workflow.rollback is not None
        outcome = await held_command(
            self.runner,
            ctx,
            kind,
            "ROLLBACK",
            slot="ROLLBACK",
            fields=PROFILES[kind].rollback_extra(ctx.workflow.rollback.mode),
        )
        if not isinstance(outcome, Passed):
            return outcome
        data["phase"] = "RESUME"
        return Wait("ROLLBACK done")

    async def _resume(self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]) -> Outcome:
        outcome = await held_command(
            self.runner, ctx, kind, "RESUME", slot="RESUME_SOURCE", location="SOURCE"
        )
        if not isinstance(outcome, Passed):
            return outcome
        data["phase"] = "RELEASE"
        return Wait("RESUME SOURCE done")

    async def _release(
        self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]
    ) -> Outcome:
        outcome = await release_workload(ctx, kind)
        if not isinstance(outcome, Passed):
            return outcome
        data["resumed_at"] = ctx.now.isoformat()
        data["phase"] = "VERIFY"
        return Wait("verifying")

    async def _verify(self, ctx: StageContext, kind: ResourceKind, data: dict[str, Any]) -> Outcome:
        probes, _ = await business_probes(ctx, datetime.fromisoformat(str(data["resumed_at"])))
        if probes.ok:
            return Passed()
        if ctx.elapsed("rollback_verify") > ctx.seed.timings.advance_timeout:
            return Stop(
                "ROLLBACK_NOT_VERIFIED",
                f"{kind.value} is on its source, but the application does not pass its probes",
                [probes.reason],
                resource=kind,
            )
        return Wait(probes.reason)


class Abort:
    """Gives up, but only from a state that is safe to walk away from.

    The workload stays stopped until every resource is settled: a paused proxy with applications
    running against it would be an outage reported as a clean abort.
    """

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def advance(self, ctx: StageContext) -> Outcome:
        unsafe = []
        for kind in ctx.seed.managed:
            if (await ctx.resource(kind)).migration_id is None:
                continue
            record = await ctx.services.repo.latest_command(ctx.workflow.id, kind, "CANCEL")
            view = await ctx.view(kind, fresh=True)
            if record is not None and view.lifecycle == "CANCELLED":
                outcome = await self._clean_up_cancelled(ctx, kind, view.allowed_actions)
                if not isinstance(outcome, Passed):
                    return outcome
                continue
            if view.lifecycle in LEFT_AS_THEY_ARE:
                continue
            cancellable = view.lifecycle in CANCELLABLE_EARLY and ("CANCEL" in view.allowed_actions)
            if record is None and not cancellable:
                unsafe.append(f"{kind.value}: {view.lifecycle}, {view.health}")
                continue
            outcome = await self.runner.run(
                ctx, Command(kind, "CANCEL", expected=expected_after("CANCEL"))
            )
            if not isinstance(outcome, Passed):
                return outcome
            view = await ctx.view(kind, fresh=True)
            outcome = await self._clean_up_cancelled(ctx, kind, view.allowed_actions)
            if not isinstance(outcome, Passed):
                return outcome
        if unsafe:
            return Stop(
                "ABORT_NOT_SAFE",
                "these resources must be reconciled or rolled back first",
                unsafe,
            )
        coordinator = ctx.services.coordinator
        if (await coordinator.hold()).desired is HoldDesired.HOLD:
            await coordinator.release()
        if not ctx.stage.data.get("abort_traffic_restored"):
            restored = await self._restore_traffic(ctx)
            if not isinstance(restored, Passed):
                return restored
            # Restore traffic once; the fixed data proxies stay in place.
            ctx.stage.data["abort_traffic_restored"] = True
        return Passed()

    async def _clean_up_cancelled(
        self, ctx: StageContext, kind: ResourceKind, allowed: list[str] | tuple[str, ...]
    ) -> Outcome:
        """Removes what this workflow's CANCEL left behind, so the next migration can start.

        Only for a plan this workflow cancelled itself. A failed cleanup does not hold the abort
        back: the data stays on its source either way, and the event says what is left.
        """
        if not PROFILES[kind].finalize_after_cancel:
            return Passed()
        sent = await ctx.services.repo.latest_command(ctx.workflow.id, kind, "ABORT_FINALIZE")
        if sent is None and "FINALIZE" not in allowed:
            await ctx.emit(
                "CANCEL_CLEANUP_SKIPPED", "the adapter does not allow FINALIZE", resource=kind
            )
            return Passed()
        outcome = await self.runner.run(
            ctx,
            Command(kind, "FINALIZE", slot="ABORT_FINALIZE", expected=expected_after("FINALIZE")),
        )
        if isinstance(outcome, Stop):
            await ctx.emit(
                "CANCEL_CLEANUP_FAILED",
                f"replication objects may remain: {outcome.code}",
                resource=kind,
            )
            return Passed()
        return outcome

    async def _restore_traffic(self, ctx: StageContext) -> Outcome:
        """Users go back to the source if the data is still there. Otherwise weights stay."""
        records = await ctx.services.repo.traffic_records(ctx.workflow.id)
        if not records or records[-1].requested.get(ctx.workflow.target_env) == 0:
            return Passed()
        for kind in (ResourceKind.DATABASE, ResourceKind.REDIS):
            if kind not in ctx.seed.managed:
                continue
            if (await ctx.resource(kind)).last_route != "SOURCE":
                await ctx.emit("TRAFFIC_LEFT_IN_PLACE", f"{kind.value} is not on its source")
                return Passed()
        try:
            await set_traffic(ctx, 0, "ABORT")
        except TrafficError as error:
            return Stop("TRAFFIC_REVERT_FAILED", str(error))
        await ctx.emit("TRAFFIC_CHANGED", "target 0%: workflow aborted")
        return Passed()


class ManualReconcile:
    """Asks every adapter to compare its records with reality. Changes no route or permission."""

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def advance(self, ctx: StageContext) -> Outcome:
        refused = []
        slot = f"MANUAL_RECONCILE_{ctx.workflow.reconcile_count}"
        for kind in ctx.seed.managed:
            if (await ctx.resource(kind)).migration_id is None:
                continue
            outcome = await self.runner.run(ctx, Command(kind, "RECONCILE", slot=slot))
            if not isinstance(outcome, Passed):
                return outcome
            record = await ctx.services.repo.latest_command(ctx.workflow.id, kind, slot)
            if record and record.error:
                refused.append(f"{kind.value}: {record.error.get('code')}")
            await ctx.view(kind, fresh=True)
        if refused:
            return Stop("RECONCILE_INCOMPLETE", "not every adapter could compare", refused)
        return Passed()
