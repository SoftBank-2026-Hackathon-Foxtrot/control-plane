"""Business evidence the stages decide from: quiesce references and probe results."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from control_plane.adapters.client import AdapterError, AdapterUnreachable
from control_plane.adapters.profiles import PROFILES, Verdict
from control_plane.dbgate import DbGateError
from control_plane.domain.models import ActorState, HoldDesired, ResourceKind, Workflow
from control_plane.engine.context import Services, StageContext

log = logging.getLogger(__name__)


class WorkloadNotHeld(Exception):
    """Evidence was needed, but the workload is no longer verifiably stopped."""


async def quiesce_reference(
    ctx: StageContext, kind: ResourceKind, transition_id: str, generation: int, action: str
) -> str:
    """A reference the adapter will accept for this resource, right now."""
    if not PROFILES[kind].needs_held_evidence(action, ctx.seed.resources.of(kind)):
        # This adapter stops its own writes. The platform's record of the attempt is the reference.
        return f"quiesce:{kind.value.lower()}:{transition_id}:{generation}"
    coordinator = ctx.services.coordinator
    reached, waiting = await coordinator.all_reached(generation, ActorState.HELD)
    if not reached:
        raise WorkloadNotHeld(", ".join(waiting))
    hold = await coordinator.mark_held()
    assert hold.held_since is not None
    resource = await ctx.resource(kind)
    assert resource.migration_id is not None
    reference = await ctx.services.gate.issue_held(
        migration_id=resource.migration_id,
        transition_id=transition_id,
        generation=generation,
        participants=await coordinator.participants(generation),
        held_since=hold.held_since,
    )
    ctx.stage.data["quiesce_ref"] = reference
    ctx.stage.data["evidence_at"] = ctx.now.isoformat()
    return reference


async def keep_evidence_alive(ctx: StageContext, generation: int) -> None:
    """Evidence expires after 30 seconds. Renew it while a long command runs."""
    reference = ctx.stage.data.get("quiesce_ref")
    written = ctx.stage.data.get("evidence_at")
    if not reference or not written:
        return
    interval = ctx.seed.timings.evidence_renew_interval
    if (ctx.now - datetime.fromisoformat(written)).total_seconds() < interval:
        return
    coordinator = ctx.services.coordinator
    reached, waiting = await coordinator.all_reached(generation, ActorState.HELD)
    if not reached:
        raise WorkloadNotHeld(", ".join(waiting))
    try:
        await ctx.services.gate.renew(reference, await coordinator.participants(generation))
    except DbGateError:
        # A topology change retired this reference. The next command gets a new one and its
        # verification decides; a failed renewal alone proves nothing.
        ctx.stage.data.pop("quiesce_ref", None)
        return
    ctx.stage.data["evidence_at"] = ctx.now.isoformat()


async def target_probe_failing(ctx: StageContext) -> bool:
    """Positive evidence only. A missing or old result says nothing about the target."""
    probe = await ctx.services.repo.latest_business_probe(ctx.workflow.id, ctx.workflow.target_env)
    if probe is None or probe.passed:
        return False
    age = (ctx.now - probe.finished_at).total_seconds()
    return age <= ctx.seed.timings.business_probe_max_age


async def business_probes(ctx: StageContext, since: datetime) -> tuple[Verdict, dict[str, Any]]:
    """Whether every environment has passed a recent probe that started after `since`."""
    repo = ctx.services.repo
    max_age = ctx.seed.timings.business_probe_max_age
    evidence: dict[str, Any] = {}
    for env in (ctx.workflow.source_env, ctx.workflow.target_env):
        probe = await repo.latest_business_probe(ctx.workflow.id, env)
        if probe is None or probe.started_at < since:
            return Verdict(False, f"no probe of {env} yet"), evidence
        if (ctx.now - probe.finished_at).total_seconds() > max_age:
            return Verdict(False, f"latest probe of {env} is too old"), evidence
        if not probe.passed:
            return Verdict(False, f"{env}: {probe.reason}"), {"failed": probe.probe_id}
        evidence[env] = probe.probe_id
    return Verdict(True), evidence


class RunningEvidence:
    """Tells the DB adapter, for as long as the database is on its target, that the workload runs.

    The adapter needs this evidence under 30 seconds old, and a 30 second unbroken window of it, at
    `readiness.advance` and again at FINALIZE, which comes stages later. So this is not part of any
    stage: it continues through later stages, approvals and halts. If the workload stops running,
    nothing is written, the evidence ages out and the adapter refuses to finalize, as it should.
    """

    def __init__(self, services: Services) -> None:
        self.services = services
        self._written_at: datetime | None = None

    async def tick(self, workflow: Workflow) -> None:
        services = self.services
        now = services.clock.now()
        interval = services.seed.timings.evidence_renew_interval
        if self._written_at and (now - self._written_at).total_seconds() < interval:
            return
        resource = await services.repo.resource(workflow.id, ResourceKind.DATABASE)
        status = resource.last_status or {}
        if resource.migration_id is None or status.get("lifecycle") != "ACTIVE":
            return
        hold = await services.coordinator.hold()
        if hold.desired is not HoldDesired.RUN or hold.released_at is None:
            return
        reached, _ = await services.coordinator.all_reached(hold.generation, ActorState.RUNNING)
        if not reached:
            return
        probes = [
            await services.repo.latest_business_probe(workflow.id, env)
            for env in (workflow.source_env, workflow.target_env)
        ]
        max_age = services.seed.timings.business_probe_max_age
        fresh = [
            probe
            for probe in probes
            if probe
            and probe.started_at >= hold.released_at
            and (now - probe.finished_at).total_seconds() <= max_age
        ]
        if len(fresh) != len(probes):
            return
        try:
            await services.gate.record_running(
                migration_id=resource.migration_id,
                generation=hold.generation,
                participants=await services.coordinator.participants(hold.generation),
                probe_result="PASS" if all(probe.passed for probe in fresh) else "FAIL",
                probe_ref=",".join(probe.probe_id for probe in fresh),
            )
            # The adapter's stability window also needs it to be observed this often.
            await services.adapters[ResourceKind.DATABASE].migration(resource.migration_id)
        except (DbGateError, AdapterError, AdapterUnreachable) as error:
            log.warning("could not record that the workload runs: %s", error)
            return
        self._written_at = now
