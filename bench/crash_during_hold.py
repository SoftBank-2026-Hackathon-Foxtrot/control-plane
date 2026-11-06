"""Kills the engine at a random moment while the application stands still, and starts a new one.

The cancellation can land in the middle of a tick: between writing a command's intent and sending
it, or between the adapter's answer and the write that records it. The new engine has a new owner
and knows only what is in the table, as a replacement ECS task would.

Checks, for each run:
- the DB cutover still gets through its hold (phase VERIFY, every process running again)
- the DB adapter carried out QUIESCE, CUTOVER and RESUME once each, no more
- how long the application stood still, including the wait for the dead engine's lease

    uv run python -m bench.crash_during_hold --runs 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time

from bench.hold_window import DB_STAGE, Process, SwitchableClock, _probe_loop, _with_clock
from control_plane.domain.models import WorkflowStatus
from control_plane.engine.engine import Engine


async def one_run(rng: random.Random, restart_delay: float) -> dict[str, object]:
    clock = SwitchableClock()
    world = _with_clock(clock)
    workflow = await world.start()
    await world.at_stage(workflow.id, DB_STAGE)
    for _ in range(50):
        current = await world.workflow(workflow.id)
        if current.status is WorkflowStatus.WAITING_APPROVAL:
            await world.approve(workflow.id)
            break
        await world.tick()

    clock.go_live()
    stop = asyncio.Event()
    processes = [Process(world, actor, instance) for actor, instance in world.workload.processes]
    tasks = [asyncio.create_task(p.loop(stop)) for p in processes]
    tasks.append(asyncio.create_task(_probe_loop(world, stop)))
    first = asyncio.create_task(world.platform.engine.run())

    # Kill somewhere inside the stop: the gates close, then up to ~1.4s of commands follow.
    while not any(p.closed_at for p in processes):  # noqa: ASYNC110 - a 1ms poll is fine here
        await asyncio.sleep(0.001)
    kill_after = rng.uniform(0, 1.4)
    await asyncio.sleep(kill_after)
    stage = await world.repo.stage(workflow.id, DB_STAGE)
    killed_in = str(stage.data.get("phase")) if stage else "?"
    first.cancel()
    await asyncio.sleep(restart_delay)
    second = asyncio.create_task(Engine(world.services).run())

    started = time.monotonic()
    while time.monotonic() - started < 180:
        stage = await world.repo.stage(workflow.id, DB_STAGE)
        current = await world.workflow(workflow.id)
        if current.status is WorkflowStatus.HALTED:
            break
        if (
            stage
            and stage.data.get("phase") == "VERIFY"
            and all(p.pauses and p.closed_at is None for p in processes)
        ):
            break
        await asyncio.sleep(0.02)
    stop.set()
    second.cancel()
    await asyncio.gather(*tasks)

    executed = {action: world.db.executed[action] for action in ("QUIESCE", "CUTOVER", "RESUME")}
    pauses = [pause for p in processes for pause in p.pauses]
    current = await world.workflow(workflow.id)
    return {
        "killed_after": round(kill_after, 3),
        "killed_in": killed_in,
        "status": current.status.value,
        "halt": current.halt.code if current.halt else None,
        "executed": executed,
        "once_each": all(count == 1 for count in executed.values()),
        "pause_max": round(max(pauses), 3) if pauses else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--restart-delay", type=float, default=0.0)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    results = []
    for index in range(args.runs):
        result = asyncio.run(one_run(rng, args.restart_delay))
        results.append(result)
        print(json.dumps({"run": index + 1, **result}, ensure_ascii=False))
    pauses = [r["pause_max"] for r in results if isinstance(r["pause_max"], float)]
    print(
        json.dumps(
            {
                "runs": args.runs,
                "restart_delay": args.restart_delay,
                "recovered": sum(1 for r in results if r["status"] == "RUNNING"),
                "once_each": sum(1 for r in results if r["once_each"]),
                "pause_max_median": round(statistics.median(pauses), 3) if pauses else None,
                "pause_max_range": [min(pauses), max(pauses)] if pauses else None,
            }
        )
    )


if __name__ == "__main__":
    main()
