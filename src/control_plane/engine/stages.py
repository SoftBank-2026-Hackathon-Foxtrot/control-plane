"""One handler per kind of stage. Each call does the next thing and returns.

A handler keeps its position in `stage.data`, saved after every call. It can be stopped between any
two calls, by a crash or a person, and continue from what is recorded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from control_plane.adapters.client import AdapterError
from control_plane.adapters.profiles import PROFILES
from control_plane.domain.models import ResourceKind, StageKind
from control_plane.domain.stages import ALLOWED_ROUTES_BEFORE, CUTOVER_RESOURCE
from control_plane.engine.commands import Command, CommandRunner, expected_after
from control_plane.engine.context import Outcome, Passed, StageContext, Stop, Wait
from control_plane.engine.evidence import WorkloadNotHeld, business_probes
from control_plane.engine.traffic_observation import set_traffic
from control_plane.engine.transition import held_command, hold_workload, release_workload
from control_plane.probes import p95
from control_plane.traffic import TrafficError


class StageHandler(Protocol):
    async def advance(self, ctx: StageContext) -> Outcome: ...


async def check_routes(ctx: StageContext) -> Outcome:
    """The cheapest protection there is: do not build on a state nobody planned."""
    allowed = ALLOWED_ROUTES_BEFORE.get(ctx.stage.kind)
    if not allowed:
        return Passed()
    wrong = []
    for kind, routes in allowed.items():
        if kind not in ctx.seed.managed:
            continue
        view = await ctx.view(kind, fresh=True)
        if view.route not in routes:
            wrong.append(f"{kind.value}: {view.route}, expected {'/'.join(sorted(routes))}")
    if wrong:
        return Stop("ROUTE_MISMATCH", "the observed routes are not the planned ones", wrong)
    return Passed()


async def _earlier_binding(ctx: StageContext) -> str | None:
    """The DB binding the newest earlier workflow of this deployment registered, if any."""
    repo = ctx.services.repo
    earlier = []
    for workflow_id in await repo.workflow_ids():
        if workflow_id == ctx.workflow.id:
            continue
        workflow = await repo.workflow(workflow_id)
        if workflow is not None:
            earlier.append(workflow)
    for workflow in sorted(earlier, key=lambda w: w.created_at, reverse=True):
        for resource in await repo.resources(workflow.id):
            if resource.kind is ResourceKind.DATABASE and resource.binding_ref:
                return resource.binding_ref
    return None


class Prepare:
    """Applications up, resources registered, one plan per resource, every PREPARE passed."""

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def advance(self, ctx: StageContext) -> Outcome:
        if ctx.workflow.target_configuration is not None:
            from control_plane.deployment.migration_sizing import prepare_target

            configured = await prepare_target(ctx)
            if not isinstance(configured, Passed):
                return configured
        for env in (ctx.source_env, ctx.target_env):
            if not await ctx.services.prober.is_ready(env.app_url):
                if ctx.elapsed("apps_ready") > ctx.seed.timings.command_timeout:
                    return Stop("APP_NOT_READY", f"{env.app_url} does not answer /readyz")
                return Wait(f"waiting for {env.app_url}")
        ctx.reset_timer("apps_ready")
        for kind in ctx.seed.managed:
            registered = await self._register(ctx, kind)
            if not isinstance(registered, Passed):
                return registered
            planned = await self._plan(ctx, kind)
            if not isinstance(planned, Passed):
                return planned
            if kind is ResourceKind.OBJECT_STORAGE:
                # Every tick, not once: a crash right after the plan must not lose this.
                resource = await ctx.resource(kind)
                assert resource.migration_id is not None
                await ctx.services.coordinator.publish_object_migration(
                    ctx.workflow.id, resource.migration_id
                )
            prepared = await self.runner.run(
                ctx, Command(kind, "PREPARE", expected=expected_after("PREPARE"))
            )
            if not isinstance(prepared, Passed):
                return prepared
        return Passed()

    async def _register(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        if kind is not ResourceKind.DATABASE:
            return Passed()
        resource = await ctx.resource(kind)
        if resource.binding_ref:
            return Passed()
        seed = ctx.seed.resources.database
        assert seed is not None  # PREPARE only registers resources the seed manages
        catalog = seed.catalog
        assert catalog is not None
        assert seed.source_ref is not None and seed.target_ref is not None
        if not ctx.stage.data.get("catalog_published"):
            await ctx.services.gate.publish_catalog(
                databases=catalog.databases,
                tables=catalog.tables,
                sequences=catalog.sequences,
                scope=ctx.seed.scope,
                participants=sorted(ctx.seed.all_actors()),
                dms_ref=seed.replication_ref,
                source_ref=seed.source_ref,
                target_ref=seed.target_ref,
                dms_job=catalog.dms_job,
            )
            ctx.stage.data["catalog_published"] = True
        client = ctx.client(kind)
        binding_id = ctx.stage.data.get("binding_id")
        if binding_id is None:
            # A later migration of the same application (the way back, or onward) keeps the binding
            # an earlier workflow registered. Registering again under the same key with the new
            # source would be refused as a different body (IDEMPOTENCY_CONFLICT).
            binding_id = await _earlier_binding(ctx)
        if binding_id is None:
            # The key belongs to the application, not the workflow.
            binding_id = await client.register_binding(
                f"binding:{ctx.seed.application}:{catalog.alias}",
                {
                    "appRef": catalog.app_ref,
                    "databaseRef": seed.source_ref,
                    "alias": catalog.alias,
                    "workloadScopeRef": ctx.seed.scope,
                },
            )
            ctx.stage.data["binding_id"] = binding_id
        binding = await client.binding(binding_id)
        if binding.get("status") != "ACTIVE":
            if ctx.elapsed("binding") > ctx.seed.timings.command_timeout:
                return Stop(
                    "BINDING_NOT_ACTIVE",
                    f"binding {binding_id} is {binding.get('status')}",
                    resource=kind,
                )
            return Wait(f"binding is {binding.get('status')}")
        if binding.get("databaseRef") != seed.source_ref:
            return Stop(
                "BINDING_ROUTE_MISMATCH",
                f"the application uses {binding.get('databaseRef')}, not {seed.source_ref}",
                resource=kind,
            )
        await ctx.services.repo.save_resource(
            ctx.workflow.id,
            resource.model_copy(
                update={
                    "binding_ref": binding_id,
                    "workload_scope_ref": binding["workloadScopeRef"],
                }
            ),
        )
        return Passed()

    async def _plan(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        resource = await ctx.resource(kind)
        if resource.migration_id:
            return Passed()
        profile = PROFILES[kind]
        body = resource.plan or profile.plan_body(
            ctx.workflow, ctx.seed.resources.of(kind), resource, ctx.seed.scope
        )
        try:
            view = await ctx.client(kind).create_migration(f"{ctx.workflow.id}:{kind.value}", body)
        except AdapterError as error:
            return Stop(error.code, error.message, error.blockers, resource=kind)
        if resource.plan is not None:
            binding = await ctx.services.repo.application_binding(ctx.workflow.application, kind)
            if binding is None:
                return Stop(
                    "BINDING_MISSING", "the fixed application binding disappeared", resource=kind
                )
            await ctx.services.repo.save_application_binding(
                ctx.workflow.application,
                kind,
                binding,
                migration_id=view.migration_id,
            )
        await ctx.services.repo.save_resource(
            ctx.workflow.id, resource.model_copy(update={"migration_id": view.migration_id})
        )
        await ctx.emit("PLAN_REGISTERED", view.migration_id, resource=kind)
        return Passed()


class Sync:
    """Copying starts everywhere. It is safe to run side by side and users do not notice it."""

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def advance(self, ctx: StageContext) -> Outcome:
        for kind in ctx.seed.managed:
            started = await self.runner.run(
                ctx, Command(kind, "START_SYNC", expected=expected_after("START_SYNC"))
            )
            if not isinstance(started, Passed):
                return started
        # Started is not copied. READY appears only in the status, and can go away again.
        waiting = [
            f"{kind.value}: {view.lifecycle}"
            for kind in ctx.seed.managed
            if (view := await ctx.view(kind, fresh=True)).lifecycle != "READY"
        ]
        if waiting:
            if ctx.elapsed("sync") > ctx.seed.timings.sync_timeout:
                return Stop("SYNC_TIMEOUT", "not every resource became READY", waiting)
            return Wait(", ".join(waiting))
        return Passed()


class Cutover:
    """Moves one resource: stop what must stop, switch, resume, and prove it still works."""

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner
        self.steps: dict[str, Callable[[StageContext, ResourceKind], Awaitable[Outcome]]] = {
            "CHECK": self._check,
            "HOLD": self._hold,
            "QUIESCE": self._quiesce,
            "CUTOVER": self._cutover,
            "RESUME": self._resume,
            "RELEASE": self._release,
            "VERIFY": self._verify,
        }

    async def advance(self, ctx: StageContext) -> Outcome:
        kind = CUTOVER_RESOURCE[ctx.stage.kind]
        try:
            return await self.steps[ctx.stage.data.setdefault("phase", "CHECK")](ctx, kind)
        except WorkloadNotHeld as error:
            return Stop(
                "WORKLOAD_NOT_HELD",
                "the workload is no longer verifiably stopped",
                [str(error)],
                resource=kind,
            )

    @staticmethod
    def _to(ctx: StageContext, phase: str) -> Wait:
        ctx.stage.data["phase"] = phase
        return Wait(phase)

    async def _check(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        routes = await check_routes(ctx)
        if isinstance(routes, Stop):
            return routes
        view = await ctx.view(kind)
        verdict = PROFILES[kind].ready_for_cutover(view, ctx.seed.resources.of(kind))
        if not verdict.ok:
            if ctx.elapsed("ready") > ctx.seed.timings.apps_following_timeout:
                return Stop("NOT_READY_FOR_CUTOVER", verdict.reason, resource=kind)
            return Wait(verdict.reason)
        check_first = PROFILES[kind].check_before_hold(ctx.seed.resources.of(kind))
        return self._to(ctx, "QUIESCE" if check_first else "HOLD")

    async def _hold(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        check_first = PROFILES[kind].check_before_hold(ctx.seed.resources.of(kind))
        outcome = await hold_workload(ctx, kind)
        if isinstance(outcome, Stop):
            ctx.stage.data["phase"] = "HOLD" if check_first else "CHECK"
        return (
            self._to(ctx, "CUTOVER" if check_first else "QUIESCE")
            if isinstance(outcome, Passed)
            else outcome
        )

    async def _quiesce(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        outcome = await held_command(self.runner, ctx, kind, "QUIESCE", slot="QUIESCE")
        check_first = PROFILES[kind].check_before_hold(ctx.seed.resources.of(kind))
        return (
            self._to(ctx, "HOLD" if check_first else "CUTOVER")
            if isinstance(outcome, Passed)
            else outcome
        )

    async def _cutover(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        outcome = await held_command(self.runner, ctx, kind, "CUTOVER", slot="CUTOVER")
        return self._to(ctx, "RESUME") if isinstance(outcome, Passed) else outcome

    async def _resume(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        outcome = await held_command(
            self.runner, ctx, kind, "RESUME", slot="RESUME_TARGET", location="TARGET"
        )
        return self._to(ctx, "RELEASE") if isinstance(outcome, Passed) else outcome

    async def _release(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        outcome = await release_workload(ctx, kind)
        if not isinstance(outcome, Passed):
            return outcome
        ctx.stage.data["resumed_at"] = ctx.now.isoformat()
        return self._to(ctx, "VERIFY")

    async def _verify(self, ctx: StageContext, kind: ResourceKind) -> Outcome:
        profile = PROFILES[kind]
        view = await ctx.view(kind, fresh=True)
        since = datetime.fromisoformat(ctx.stage.data["resumed_at"])
        probes, evidence = await business_probes(ctx, since)
        verdict = profile.advance(view)
        ok = verdict.ok and probes.ok
        # An adapter that judges advance has a stability window of its own over the evidence.
        if ok if profile.adapter_judges_advance else self._stable(ctx, ok):
            await ctx.emit("RESOURCE_MOVED", resource=kind, evidence={"probes": evidence})
            return Passed()
        if ctx.elapsed("advance") > ctx.seed.timings.advance_timeout:
            return Stop(
                "ADVANCE_CONDITIONS_NOT_MET",
                f"{kind.value} did not settle after the switch",
                [reason for reason in (verdict.reason, probes.reason) if reason],
                resource=kind,
            )
        return Wait(verdict.reason or probes.reason or "observing stability")

    @staticmethod
    def _stable(ctx: StageContext, ok: bool) -> bool:
        """The conditions must hold without a gap for the whole window, or the window restarts."""
        timings = ctx.seed.timings
        data = ctx.stage.data
        last = data.get("observed_at")
        gap = (ctx.now - datetime.fromisoformat(last)).total_seconds() if last else 0
        data["observed_at"] = ctx.now.isoformat()
        if not ok or gap > timings.stability_max_gap:
            data.pop("stable_since", None)
            return False
        since = data.setdefault("stable_since", ctx.now.isoformat())
        return (ctx.now - datetime.fromisoformat(since)).total_seconds() >= timings.stability_window


class TrafficShift:
    """Moves users step by step and keeps a step only if the target proves as good as the source."""

    def __init__(self, weights: list[int], *, previous: int) -> None:
        self.weights = weights
        self.previous = previous

    async def advance(self, ctx: StageContext) -> Outcome:
        if ctx.stage.data.get("routes_checked") is None:
            routes = await check_routes(ctx)
            if isinstance(routes, Stop):
                return routes
            ctx.stage.data["routes_checked"] = True
        index = int(ctx.stage.data.setdefault("weight_index", 0))
        if index >= len(self.weights):
            return Passed()
        window_start = ctx.stage.data.get("window_start")
        if window_start is None:
            return await self._apply(ctx, self.weights[index])
        return await self._observe(ctx, index, datetime.fromisoformat(window_start))

    def _before(self, index: int) -> int:
        return self.weights[index - 1] if index else self.previous

    async def _apply(self, ctx: StageContext, percent: int) -> Outcome:
        try:
            applied = await set_traffic(ctx, percent, "SHIFT")
        except TrafficError as error:
            if ctx.elapsed("apply") > ctx.seed.timings.command_timeout:
                return Stop("TRAFFIC_CHANGE_FAILED", str(error), [f"requested {percent}%"])
            return Wait(f"retrying weight change: {error}")
        ctx.reset_timer("apply")
        await ctx.emit(
            "TRAFFIC_CHANGED",
            f"target {percent}%",
            evidence={"effectivePercent": applied.effective_percent},
        )
        ctx.stage.data["window_start"] = ctx.now.isoformat()
        return Wait(f"observing target {percent}%")

    async def _observe(self, ctx: StageContext, index: int, since: datetime) -> Outcome:
        repo = ctx.services.repo
        timings = ctx.seed.timings
        target = await repo.latency_buckets(ctx.workflow.id, ctx.workflow.target_env, since=since)
        source = await repo.latency_buckets(ctx.workflow.id, ctx.workflow.source_env, since=since)
        failures = sum(bucket.failed for bucket in target)
        if failures:
            return await self._revert(ctx, index, f"{failures} failed requests on the target")
        probe = await repo.latest_business_probe(ctx.workflow.id, ctx.workflow.target_env)
        if probe and probe.started_at >= since and not probe.passed:
            return await self._revert(ctx, index, f"business probe failed: {probe.reason}")
        if (ctx.now - since).total_seconds() < timings.observation_window:
            return Wait(f"observing target {self.weights[index]}%")

        total = sum(bucket.total for bucket in target)
        if total < timings.min_light_samples or not source:
            return await self._revert(ctx, index, f"too few samples to judge ({total})")
        if probe is None or probe.started_at < since:
            return await self._revert(ctx, index, "no business probe in the window")
        target_p95 = p95([ms for bucket in target for ms in bucket.latencies_ms])
        source_p95 = p95([ms for bucket in source for ms in bucket.latencies_ms])
        limit = source_p95 * timings.p95_factor + timings.p95_margin_ms
        if target_p95 > limit:
            return await self._revert(
                ctx, index, f"target p95 {target_p95:.0f} ms is over {limit:.0f} ms"
            )
        await ctx.emit(
            "TRAFFIC_GATE_PASSED",
            f"target {self.weights[index]}%",
            evidence={"targetP95": target_p95, "sourceP95": source_p95, "samples": total},
        )
        ctx.stage.data["weight_index"] = index + 1
        ctx.stage.data.pop("window_start", None)
        return Wait("gate passed")

    async def _revert(self, ctx: StageContext, index: int, reason: str) -> Outcome:
        """Automatic: it changes no data path, and every second counts while users see errors."""
        before = self._before(index)
        ctx.stage.data.pop("window_start", None)
        try:
            await set_traffic(ctx, before, "COMPENSATE")
        except TrafficError as error:
            return Stop(
                "TRAFFIC_REVERT_FAILED",
                f"the gate failed ({reason}) and the weights could not be restored",
                [str(error)],
            )
        await ctx.emit("TRAFFIC_REVERTED", f"back to target {before}%: {reason}")
        return Stop("TRAFFIC_GATE_FAILED", reason, [f"reverted to target {before}%"])

    @staticmethod
    def _split(ctx: StageContext, percent: int) -> dict[str, int]:
        return {ctx.workflow.source_env: 100 - percent, ctx.workflow.target_env: percent}


class Finalize:
    """Removes temporary resources. Source data stays: deleting it is a separate decision."""

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    async def advance(self, ctx: StageContext) -> Outcome:
        for kind in ctx.seed.managed:
            outcome = await self.runner.run(
                ctx,
                Command(
                    kind,
                    "FINALIZE",
                    expected=expected_after("FINALIZE"),
                    # The adapters have no waiting period of their own: FINALIZE goes when this
                    # completion decision is made. Remaining safety checks may take a moment.
                    not_allowed_timeout=ctx.seed.timings.finalize_timeout,
                ),
            )
            if not isinstance(outcome, Passed):
                if ctx.elapsed("finalize") > ctx.seed.timings.finalize_timeout:
                    return Stop("FINALIZE_TIMEOUT", "FINALIZE was not accepted", resource=kind)
                return outcome
        wrong = [
            f"{kind.value}: {view.route}"
            for kind in ctx.seed.managed
            # COMPLETED alone does not say which side a resource ended on.
            if (view := await ctx.view(kind, fresh=True)).route != "TARGET"
        ]
        if wrong:
            return Stop("ROUTE_MISMATCH", "a finalized resource is not on the target", wrong)
        return Passed()


def build_handlers(
    runner: CommandRunner, canary: list[int], traffic: list[int]
) -> dict[StageKind, StageHandler]:
    cutover = Cutover(runner)
    return {
        StageKind.PREPARE: Prepare(runner),
        StageKind.SYNC: Sync(runner),
        StageKind.OBJECT_CUTOVER: cutover,
        StageKind.CANARY: TrafficShift(canary, previous=0),
        StageKind.DATABASE_CUTOVER: cutover,
        StageKind.REDIS_CUTOVER: cutover,
        StageKind.TRAFFIC: TrafficShift(traffic, previous=canary[-1] if canary else 0),
        StageKind.FINALIZE: Finalize(runner),
    }
