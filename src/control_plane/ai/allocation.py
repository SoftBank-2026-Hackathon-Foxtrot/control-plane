from __future__ import annotations

import math
from typing import Any


def allocation(cloud: str, app: dict[str, Any], tunnel: bool = False) -> dict[str, Any]:
    cpu, memory = float(app["cpu"]), int(app["memory_mib"])
    workers = int(app.get("worker_count", 2))
    if not math.isfinite(cpu) or cpu <= 0 or memory <= 0 or not 0 <= workers <= 10:
        raise ValueError("invalid applied application allocation")
    count = workers + 1
    return {
        "scope": "TASK" if cloud == "aws" else "CONTAINER",
        "cpuCores": cpu,
        "memoryMiB": memory,
        "applicationContainers": ["api", *(f"worker-{i + 1}" for i in range(workers))],
        "hasTunnelContainer": tunnel,
        "totalCpuCores": cpu if cloud == "aws" else cpu * count + int(tunnel),
        "totalMemoryMiB": memory if cloud == "aws" else memory * count + 256 * int(tunnel),
    }


def target_baseline(
    cloud: str, current: dict[str, Any], target: dict[str, Any] | None, default: dict[str, Any]
) -> dict[str, Any]:
    # Never copy a task allocation into a per-container field (or vice versa).
    topology = (target or current).get("app") or {}
    base = {**default, **{k: v for k, v in topology.items() if k not in {"cpu", "memory_mib"}}}
    if target or current["environment"] == cloud:
        base.update({k: topology[k] for k in ("cpu", "memory_mib") if k in topology})
    return base
