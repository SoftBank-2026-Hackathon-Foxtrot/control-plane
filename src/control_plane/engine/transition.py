"""What moving a resource and moving it back have in common.

A cutover and a rollback both stop the workload if the resource needs that, send commands that
carry evidence of the stop, then let the workload run again. Both go through here, so a rule fixed
for one is fixed for the other.
"""

from __future__ import annotations

from typing import Any

from control_plane.adapters.profiles import PROFILES
from control_plane.domain.models import ActorState, HoldDesired, ResourceKind
from control_plane.engine.commands import Command, CommandRunner, expected_after
from control_plane.engine.context import Outcome, Passed, StageContext, Stop, Wait
from control_plane.engine.evidence import keep_evidence_alive, quiesce_reference


def transition_id(ctx: StageContext, kind: ResourceKind) -> str:
    """One per plan: the forward move and its way back are the same attempt."""
    return f"tx-{ctx.workflow.id}-{PROFILES[kind].resource_id}"


async def hold_workload(ctx: StageContext, kind: ResourceKind) -> Outcome:
    """Passed once everything that uses this resource has really stopped."""
    if not PROFILES[kind].requires_workload_hold:
        return Passed()
    coordinator = ctx.services.coordinator
    hold = await coordinator.request_hold(ctx.workflow.id)
    reached, waiting = await coordinator.all_reached(hold.generation, ActorState.HELD)
    if reached:
        ctx.reset_timer("hold")
        return Passed()
    if ctx.elapsed("hold") > ctx.seed.timings.hold_timeout:
        # Nothing has changed outside the workload, so stopping it was the only effect.
        await coordinator.release()
        ctx.reset_timer("hold")
        return Stop("HOLD_TIMEOUT", "the workload did not stop in time", waiting, resource=kind)
    return Wait(f"waiting for {', '.join(waiting)}")


async def release_workload(ctx: StageContext, kind: ResourceKind) -> Outcome:
    """Passed once every process has reported that it runs again."""
    if not PROFILES[kind].requires_workload_hold:
        return Passed()
    coordinator = ctx.services.coordinator
    hold = await coordinator.release()
    reached, waiting = await coordinator.all_reached(hold.generation, ActorState.RUNNING)
    if reached:
        ctx.reset_timer("release")
        return Passed()
    if ctx.elapsed("release") > ctx.seed.timings.resume_workload_timeout:
        return Stop("WORKLOAD_NOT_RESUMED", "not every process resumed", waiting, resource=kind)
    return Wait(f"waiting for {', '.join(waiting)}")


async def held_command(
    runner: CommandRunner,
    ctx: StageContext,
    kind: ResourceKind,
    action: str,
    *,
    slot: str,
    location: str | None = None,
    fields: dict[str, Any] | None = None,
) -> Outcome:
    """A command that must carry fresh evidence that the workload stands still."""
    needs_hold = PROFILES[kind].needs_held_evidence(action, ctx.seed.resources.of(kind))
    generation = 0
    if needs_hold:
        hold = await ctx.services.coordinator.hold()
        if hold.desired is not HoldDesired.HOLD:
            return Stop(
                "WORKLOAD_NOT_HELD", "the workload is not held", ["no hold in force"], resource=kind
            )
        generation = hold.generation
        await keep_evidence_alive(ctx, generation)
    tx = transition_id(ctx, kind)

    async def extra() -> dict[str, Any]:
        reference = await quiesce_reference(ctx, kind, tx, generation, action)
        body: dict[str, Any] = {"transitionId": tx, "quiesceRef": reference, **(fields or {})}
        if location:
            body["expectedLocation"] = location
        return body

    return await runner.run(
        ctx,
        Command(kind, action, slot=slot, extra=extra, expected=expected_after(action, location)),
    )
