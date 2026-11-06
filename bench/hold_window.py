"""Measures how long the application stands still during the DB cutover, in real time.

The adapters are the test fakes and answer at once, so what is measured is the platform's own share
of the stop: engine ticks, command bookkeeping and heartbeat polling. Adapter work adds to it.

The run reaches the DB cutover on simulated time, then switches to the wall clock and runs the real
loops: the engine loop, the probe loop, and one heartbeat loop per application process that sleeps
for the `pollAfterMs` it is given, as an application does.

    uv run python -m bench.hold_window --runs 5                       # the defaults
    uv run python -m bench.hold_window --runs 5 --hold-interval 1 --fast-poll-ms 500   # before
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib
import itertools

from control_plane.domain.models import ActorState, HoldDesired, WorkflowStatus
from tests.conftest import World
from tests.fakes import FakeClock

DB_STAGE = 4


class SwitchableClock(FakeClock):
    """Simulated time until `go_live`, then the wall clock from where simulated time stood."""

    def __init__(self) -> None:
        super().__init__()
        self.live_from: float | None = None
        self.base: datetime | None = None

    def go_live(self) -> None:
        self.base = self.moment
        self.live_from = time.monotonic()

    def now(self) -> datetime:
        if self.live_from is None or self.base is None:
            return self.moment
        return self.base + timedelta(seconds=time.monotonic() - self.live_from)

    def advance(self, seconds: float = 1) -> None:
        if self.live_from is None:
            self.moment += timedelta(seconds=seconds)


class Process:
    """One application process: closes its gate on HOLD, opens it on RUN."""

    def __init__(self, world: World, actor: str, instance: str) -> None:
        self.world = world
        self.actor = actor
        self.instance = instance
        self.generation = world.workload.generation
        self.desired = HoldDesired.RUN
        self.closed_at: float | None = None
        self.pauses: list[float] = []

    async def loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            state = ActorState.HELD if self.desired is HoldDesired.HOLD else ActorState.RUNNING
            directive = await self.world.services.coordinator.heartbeat(
                actor_id=self.actor,
                instance_id=self.instance,
                generation=self.generation,
                state=state,
                in_flight=0,
            )
            now = time.monotonic()
            if directive.desired is HoldDesired.HOLD and self.closed_at is None:
                self.closed_at = now
            if directive.desired is HoldDesired.RUN and self.closed_at is not None:
                self.pauses.append(now - self.closed_at)
                self.closed_at = None
            self.desired = directive.desired
            self.generation = directive.generation
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), directive.poll_after_ms / 1000)


async def watch_phases(
    world: World, workflow_id: str, stop: asyncio.Event
) -> list[tuple[str, float]]:
    seen: list[tuple[str, float]] = []
    while not stop.is_set():
        stage = await world.repo.stage(workflow_id, DB_STAGE)
        phase = str(stage.data.get("phase", "")) if stage else ""
        if not seen or seen[-1][0] != phase:
            seen.append((phase, time.monotonic()))
        await asyncio.sleep(0.005)
    return seen


async def one_run() -> dict[str, object]:
    clock = SwitchableClock()
    world = _with_clock(clock)

    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)
    # The DB cutover asks a person first.
    for _ in range(50):
        current = await world.workflow(workflow.id)
        if current.status is WorkflowStatus.WAITING_APPROVAL:
            await world.approve(workflow.id)
            break
        await world.tick()
    else:
        raise RuntimeError(f"the DB cutover never asked for approval: {current.status}")

    clock.go_live()
    stop = asyncio.Event()
    processes = [Process(world, actor, instance) for actor, instance in world.workload.processes]
    engine = world.platform.engine
    tasks = [asyncio.create_task(p.loop(stop)) for p in processes]
    engine_task = asyncio.create_task(engine.run())
    tasks.append(asyncio.create_task(_probe_loop(world, stop)))
    watcher = asyncio.create_task(watch_phases(world, workflow.id, stop))

    started = time.monotonic()
    while time.monotonic() - started < 120:
        stage = await world.repo.stage(workflow.id, DB_STAGE)
        if (
            stage
            and stage.data.get("phase") == "VERIFY"
            and all(p.pauses and p.closed_at is None for p in processes)
        ):
            break
        current = await world.workflow(workflow.id)
        if current.status is WorkflowStatus.HALTED:
            raise RuntimeError(f"halted: {current.halt}")
        await asyncio.sleep(0.02)
    else:
        stage = await world.repo.stage(workflow.id, DB_STAGE)
        current = await world.workflow(workflow.id)
        stop.set()
        engine_task.cancel()
        phases = await watcher
        raise RuntimeError(
            f"the DB cutover did not finish its hold in 120s: stage={current.current_stage} "
            f"status={current.status} detail={stage.detail if stage else None} "
            f"phases={[name for name, _ in phases]} "
            f"pauses={[p.pauses for p in processes[:2]]}"
        )
    stop.set()
    engine_task.cancel()
    phases = await watcher
    await asyncio.gather(*tasks)

    pauses = [pause for p in processes for pause in p.pauses]
    marks = {name: at for name, at in phases}
    spans = {}
    order = [name for name, _ in phases]
    for current_name, following in itertools.pairwise(order):
        spans[current_name] = round(marks[following] - marks[current_name], 3)
    commands = await world.repo.commands(workflow.id)
    return {
        "pause_max": round(max(pauses), 3),
        "pause_min": round(min(pauses), 3),
        "phases": spans,
        "db_commands": [c.action for c in commands if c.resource.value == "DATABASE"],
    }


def _with_clock(clock: SwitchableClock) -> World:
    """A World whose every part reads `clock`."""
    import tests.conftest as conftest

    original = conftest.FakeClock
    conftest.FakeClock = lambda: clock  # type: ignore[assignment,misc]
    try:
        return World()
    finally:
        conftest.FakeClock = original  # type: ignore[misc]


async def _probe_loop(world: World, stop: asyncio.Event) -> None:
    loop = world.platform.probe_loop
    last_business = 0.0
    while not stop.is_set():
        await loop.light_once()
        if time.monotonic() - last_business >= world.seed.timings.business_probe_interval:
            await loop.business_once()
            last_business = time.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), 0.5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--hold-interval", type=float, default=0.1)
    parser.add_argument("--fast-poll-ms", type=int, default=200)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    import tests.conftest as conftest

    timings = conftest.SEED.setdefault("timings", {})
    timings["tickInterval"] = args.interval
    timings["heldTickInterval"] = args.hold_interval
    timings["fastPollMs"] = args.fast_poll_ms

    results = []
    for index in range(args.runs):
        result = asyncio.run(one_run())
        results.append(result)
        print(json.dumps({"run": index + 1, **result}, ensure_ascii=False))
    worst = [r["pause_max"] for r in results]
    summary = {
        "label": args.label,
        "interval": args.interval,
        "hold_interval": args.hold_interval,
        "fast_poll_ms": args.fast_poll_ms,
        "runs": args.runs,
        "pause_max_median": round(statistics.median(worst), 3),
        "pause_max_range": [min(worst), max(worst)],
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
