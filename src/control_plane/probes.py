"""Checks the application through the paths it really uses.

An adapter can only say its own connection works. Whether users can still order is checked here,
per environment, and that evidence decides whether a migration moves on.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from control_plane.clock import Clock
from control_plane.domain.models import (
    BusinessProbe,
    HoldDesired,
    LatencyBucket,
    StageKind,
    WorkflowStatus,
)
from control_plane.domain.stages import PLAN
from control_plane.seed import Seed
from control_plane.store.repository import Repository
from control_plane.telemetry import get_telemetry

log = logging.getLogger(__name__)

PROBED_WHILE = frozenset(
    {
        WorkflowStatus.RUNNING,
        WorkflowStatus.ROLLING_BACK,
        WorkflowStatus.HALTED,
        WorkflowStatus.WAITING_APPROVAL,
        WorkflowStatus.CUTOVER_COMPLETE,
    }
)
"""Whenever a person may be deciding. The DB adapter also needs this evidence to stay fresh."""
LIGHT_STAGES = frozenset({StageKind.CANARY, StageKind.TRAFFIC})


class ProbeFailed(Exception):
    pass


class AppProber(Protocol):
    async def is_ready(self, app_url: str) -> bool: ...

    async def light(self, app_url: str) -> tuple[bool, int]:
        """Whether one ordinary request succeeded, and how many milliseconds it took."""
        ...

    async def business(self, env: str, app_url: str) -> BusinessProbe: ...


class HttpAppProber:
    """Log in, join a dedicated group buy, wait for the worker, read the receipt, clean up.

    That single flow crosses the database, the Redis queue, the worker and object storage. It uses
    its own account and group buy, so it never touches real orders.
    """

    def __init__(self, http: httpx.AsyncClient, seed: Seed, clock: Clock) -> None:
        self.http = http
        self.seed = seed
        self.clock = clock
        self._sessions: dict[str, str] = {}

    async def is_ready(self, app_url: str) -> bool:
        try:
            response = await self.http.get(f"{app_url}/readyz", timeout=5)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def light(self, app_url: str) -> tuple[bool, int]:
        started = time.perf_counter()
        try:
            session = await self._light_session(app_url)
            response = await self.http.get(
                f"{app_url}{self.seed.probe.light_path}",
                headers={"X-Session": session},
                timeout=5,
            )
        except (httpx.HTTPError, ProbeFailed):
            return False, _elapsed_ms(started)
        if response.status_code == 401:
            self._sessions.pop(app_url, None)
        return response.status_code == 200, _elapsed_ms(started)

    async def _light_session(self, app_url: str) -> str:
        if app_url not in self._sessions:
            self._sessions[app_url] = await self._login(app_url)
        return self._sessions[app_url]

    async def business(self, env: str, app_url: str) -> BusinessProbe:
        started_at = self.clock.now()
        steps: dict[str, int] = {}
        cleaned = False
        reason = ""
        probe_id = f"evidence:{env}:{secrets.token_hex(6)}"
        try:
            async with asyncio.timeout(self.seed.timings.business_probe_timeout):
                cleaned = await self._flow(app_url, steps)
        except TimeoutError:
            reason = f"timed out after {sorted(steps)}"
        except (ProbeFailed, httpx.HTTPError, ValueError, KeyError) as error:
            reason = f"{type(error).__name__}: {error}"
        return BusinessProbe(
            probe_id=probe_id,
            env=env,
            passed=not reason,
            reason=reason,
            steps=steps,
            cleaned=cleaned,
            started_at=started_at,
            finished_at=self.clock.now(),
        )

    async def _flow(self, app_url: str, steps: dict[str, int]) -> bool:
        probe = self.seed.probe

        async def timed(name: str, method: str, path: str, **kwargs: object) -> httpx.Response:
            started = time.perf_counter()
            response = await self.http.request(method, f"{app_url}{path}", **kwargs)  # type: ignore[arg-type]
            steps[name] = _elapsed_ms(started)
            if response.status_code >= 400:
                raise ProbeFailed(f"{name}: {response.status_code}")
            return response

        login = await timed("login", "POST", f"/api/login/{probe.user}")
        headers = {"X-Session": str(login.json()["sid"])}
        order = await timed(
            "join",
            "POST",
            f"/api/groupbuys/{probe.groupbuy_id}/join?qty=1",
            headers={**headers, "Idempotency-Key": secrets.token_hex(16)},
        )
        request_id = str(order.json()["request_id"])
        while True:
            requests = await timed("requests", "GET", "/api/me/requests", headers=headers)
            mine = next((r for r in requests.json() if r["id"] == request_id), None)
            if mine and mine["status"] == "CONFIRMED" and mine.get("receipt"):
                break
            if mine and mine["status"] not in {"QUEUED", "CONFIRMED"}:
                raise ProbeFailed(f"order ended as {mine['status']}")
            await asyncio.sleep(0.5)
        await timed("receipt", "GET", f"/api/me/receipts/{request_id}", headers=headers)
        cleaned = False
        if probe.cleanup_path:
            await timed(
                "cleanup",
                "POST",
                probe.cleanup_path,
                headers=headers,
                json={"requestId": request_id},
            )
            cleaned = True
        await timed("logout", "POST", "/api/logout", headers=headers)
        return cleaned

    async def _login(self, app_url: str) -> str:
        response = await self.http.post(
            f"{app_url}/api/login/{self.seed.probe.user}-light", timeout=5
        )
        if response.status_code >= 400:
            raise ProbeFailed(f"login: {response.status_code}")
        return str(response.json()["sid"])


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


@dataclass
class _Accumulator:
    started_at: datetime
    total: int = 0
    failed: int = 0
    latencies_ms: list[int] = field(default_factory=list)


class ProbeLoop:
    """Runs the probes while a workflow needs them and stores what they found.

    Separate from the engine: a slow probe must never delay a cutover step, and the engine decides
    only from stored results, so a restart loses nothing it relied on.
    """

    def __init__(self, repo: Repository, seed: Seed, prober: AppProber, clock: Clock) -> None:
        self.repo = repo
        self.seed = seed
        self.prober = prober
        self.clock = clock
        self._buckets: dict[str, _Accumulator] = {}

    async def _active(self) -> tuple[str, StageKind] | None:
        workflow_id = await self.repo.active_workflow_id()
        if workflow_id is None:
            return None
        workflow = await self.repo.workflow(workflow_id)
        if workflow is None or workflow.status not in PROBED_WHILE:
            return None
        return workflow.id, PLAN[workflow.current_stage].kind

    async def business_once(self) -> None:
        active = await self._active()
        if active is None or active[1] in {StageKind.PREPARE, StageKind.SYNC}:
            return
        # Held requests wait at the gate, so a probe would only measure the hold.
        hold, _ = await self.repo.hold(self.seed.scope)
        if hold.desired is HoldDesired.HOLD:
            return
        with get_telemetry("control-plane").operation("BUSINESS_PROBE", workflow=active[0]):
            probes = await asyncio.gather(
                *(
                    self.prober.business(env, definition.app_url)
                    for env, definition in self.seed.environments.items()
                )
            )
        for probe in probes:
            await self.repo.add_business_probe(active[0], probe)
            get_telemetry("control-plane").measure(
                "foxtrot.probe.business_passed",
                int(probe.passed),
                {"workflow.id": active[0], "environment": probe.env},
            )
            if not probe.passed:
                log.warning("business probe failed on %s: %s", probe.env, probe.reason)

    async def light_once(self) -> None:
        active = await self._active()
        if active is None or active[1] not in LIGHT_STAGES:
            return
        await asyncio.gather(
            *(
                self._light_env(active[0], env, definition.app_url)
                for env, definition in self.seed.environments.items()
            )
        )

    async def _light_env(self, workflow_id: str, env: str, app_url: str) -> None:
        passed, latency = await self.prober.light(app_url)
        now = self.clock.now()
        bucket = self._buckets.get(env)
        if bucket is None:
            bucket = self._buckets[env] = _Accumulator(started_at=now)
        bucket.total += 1
        bucket.failed += 0 if passed else 1
        if passed:
            bucket.latencies_ms.append(latency)
        window = timedelta(seconds=self.seed.timings.light_bucket_seconds)
        if now - bucket.started_at >= window or not passed:
            # A failure is written at once: the gate must see it before the bucket would fill.
            del self._buckets[env]
            await self.repo.add_latency_bucket(
                workflow_id,
                LatencyBucket(
                    env=env,
                    started_at=bucket.started_at,
                    finished_at=now,
                    total=bucket.total,
                    failed=bucket.failed,
                    latencies_ms=bucket.latencies_ms,
                ),
            )

    async def run(self) -> None:
        timings = self.seed.timings
        await asyncio.gather(
            self._repeat(self.business_once, timings.business_probe_interval),
            self._repeat(self.light_once, 1 / timings.light_probe_rate_per_second),
        )

    @staticmethod
    async def _repeat(step: object, interval: float) -> None:
        while True:
            try:
                await step()  # type: ignore[operator]
            except Exception:
                log.exception("probe loop step failed")
            await asyncio.sleep(interval)


def p95(latencies: list[int]) -> float:
    if not latencies:
        return 0.0
    ordered = sorted(latencies)
    return float(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))])
