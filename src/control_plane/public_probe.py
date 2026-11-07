"""Sample the public ingress. This measures probe requests, never all user traffic."""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any

import httpx

from control_plane.clock import Clock, stamp
from control_plane.observation_projection import timestamp
from control_plane.observations import ObservationStore, watched_workflows
from control_plane.store.repository import Repository
from control_plane.telemetry import get_telemetry

log = logging.getLogger(__name__)


async def sample(
    http: httpx.AsyncClient, url: str, mapping: dict[str, str], clock: Clock
) -> dict[str, Any]:
    started, timer = clock.now(), time.perf_counter()
    result: dict[str, Any] = {
        "at": stamp(started),
        "ok": False,
        "timeout": False,
        "statusCode": None,
        "servedBy": None,
        "environment": None,
        "locationEligible": False,
        "reason": "NETWORK_ERROR",
    }
    try:
        response = await http.get(
            url,
            params={"_migration_probe": secrets.token_hex(8)},
            headers={"Cache-Control": "no-cache, no-store"},
            timeout=2,
            follow_redirects=False,
        )
        result["statusCode"] = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        result["ok"] = (
            response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True
        )
        no_cache = (
            "no-store" in response.headers.get("cache-control", "").lower()
            and response.headers.get("cf-cache-status", "").upper()
            not in {"HIT", "STALE", "UPDATING", "REVALIDATED"}
            and response.headers.get("age", "0") == "0"
        )
        served = response.headers.get("x-served-by", "")
        # Unregistered headers are counted as unknown, not reflected to the console.
        result["servedBy"] = served if served in mapping else None
        result["environment"] = mapping.get(served) if result["ok"] and no_cache else None
        result["locationEligible"] = bool(result["environment"])
        result["reason"] = (
            "HTTP_OR_BODY_MISMATCH"
            if not result["ok"]
            else "CACHE_UNVERIFIED"
            if not no_cache
            else "LOCATION_UNKNOWN"
            if not result["environment"]
            else "OK"
        )
    except httpx.TimeoutException:
        result.update(timeout=True, reason="TIMEOUT")
    except httpx.HTTPError:
        pass
    result["latencyMs"] = round((time.perf_counter() - timer) * 1000)
    result["finishedAt"] = stamp(clock.now())
    return result


def summarize(samples: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    locations: dict[str, int] = {}
    for item in samples:
        if item.get("locationEligible"):
            env = item["environment"]
            locations[env] = locations.get(env, 0) + 1
    known = sum(locations.values())
    latencies = sorted(item["latencyMs"] for item in samples if item["ok"])
    gaps = []
    previous = start
    intervals: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None
    for item in samples:
        at = timestamp(item["at"]) or start
        gaps.append(max(0.0, (at - previous).total_seconds()))
        previous = at
        if not item["ok"]:
            if failure is None:
                failure = {
                    "firstFailureAt": item["at"],
                    "lastFailureAt": item["at"],
                    "recoveredAt": None,
                }
                intervals.append(failure)
            failure["lastFailureAt"] = item["at"]
        elif failure:
            failure["recoveredAt"] = item["at"]
            failure = None
    gaps.append(max(0.0, (end - previous).total_seconds()))
    expected = max(0, math.floor((end - start).total_seconds()))
    return {
        "scope": "SYNTHETIC_PUBLIC_READ",
        "windowStart": stamp(start),
        "windowEnd": stamp(end),
        "total": len(samples),
        "failed": sum(not item["ok"] for item in samples),
        "timeouts": sum(item["timeout"] for item in samples),
        "unknownLocation": sum(item["ok"] and not item["locationEligible"] for item in samples),
        "locations": locations,
        "knownLocationSamples": known,
        "observedPercent": {key: round(count * 100 / known, 1) for key, count in locations.items()}
        if known
        else None,
        "enoughSamples": known >= 30,
        "p95Ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None,
        "missingSamples": max(0, expected - len(samples)),
        "maxSampleGapSeconds": round(max(gaps), 2),
        "failureIntervals": intervals,
        "lastSampleAt": samples[-1]["at"] if samples else None,
    }


class PublicProbeLoop:
    def __init__(
        self,
        repo: Repository,
        http: httpx.AsyncClient,
        clock: Clock,
        url: str | None,
        mapping: dict[str, str],
    ) -> None:
        self.repo, self.http, self.clock, self.url, self.mapping = repo, http, clock, url, mapping
        self.store = ObservationStore(repo.table)
        self.owner = secrets.token_hex(12)
        self.samples: dict[str, deque[dict[str, Any]]] = {}

    async def run(self) -> None:
        if not self.url:
            return
        while True:
            started = time.monotonic()
            try:
                await self.tick()
            except Exception:
                log.warning("public probe collection failed", exc_info=True)
            # One request at a time; timeouts create visible sampling gaps.
            await asyncio.sleep(max(0.0, 1 - (time.monotonic() - started)))

    async def tick(self) -> None:
        if not self.url:
            return
        for workflow in await watched_workflows(self.repo, self.clock.now()):
            if not await self.store.own(
                workflow.id, self.owner, self.clock.now(), channel="public"
            ):
                continue
            with get_telemetry("control-plane").operation("PUBLIC_PROBE", workflow=workflow.id):
                result = await sample(self.http, self.url, self.mapping, self.clock)
            now = self.clock.now()
            start = max(workflow.created_at, now - timedelta(seconds=60))
            samples = self.samples.setdefault(workflow.id, deque(maxlen=120))
            samples.append(result)
            recent = [s for s in samples if (timestamp(s["at"]) or now) >= start]
            summary = summarize(recent, start, now)
            summary["workflowId"] = workflow.id
            summary["observed"] = {"observedAt": result["at"]}
            get_telemetry("control-plane").snapshot("PUBLIC", summary)
        active = {w.id for w in await watched_workflows(self.repo, self.clock.now())}
        self.samples = {key: value for key, value in self.samples.items() if key in active}
