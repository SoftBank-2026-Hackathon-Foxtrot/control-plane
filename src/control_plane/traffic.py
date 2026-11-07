"""Who gets how much traffic. The engine asks for percentages; this file knows the traffic service.

The team's traffic service (github.com/SoftBank-2026-Hackathon-Foxtrot/traffic) is the only writer
of the Cloudflare load balancer. One change is one SET_WEIGHTS operation: read the status for its
revision, submit with an Idempotency-Key, then poll the operation until it ends.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class TrafficError(Exception):
    """The weight change failed or could not be confirmed."""

    def __init__(self, message: str, operation: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.operation = operation or {}


@dataclass(frozen=True)
class AppliedWeights:
    weights: dict[str, float]
    """What was requested, in percent per environment."""
    effective_percent: dict[str, float]
    operation_id: str | None = None
    operation_status: str = "NO_CHANGE"
    started_at: str | None = None
    finished_at: str | None = None
    """What the load balancer reported back after the change."""


class TrafficController(Protocol):
    async def require_source_only(self, *, source_pool: str, target_pool: str) -> None: ...

    async def set_split(
        self,
        *,
        source_pool: str,
        target_pool: str,
        target_percent: int,
        migration_id: str | None = None,
        transition_id: str | None = None,
    ) -> AppliedWeights: ...


RUNNING = frozenset({"QUEUED", "RUNNING"})


class TrafficService:
    """Calls the traffic service. `source_pool` and `target_pool` are its environment names."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        token: str,
        poll_timeout: float = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}"}
        self.poll_timeout = poll_timeout
        self.sleep = sleep

    async def require_source_only(self, *, source_pool: str, target_pool: str) -> None:
        """A target taking public traffic must not be resized by PREPARE."""
        status, _ = await self._call("GET", "/v1/traffic/status")
        if status.get("unknownOperationIds"):
            raise TrafficError("traffic has an unknown operation")
        current = status.get("weights")
        if not isinstance(current, dict) or not {source_pool, target_pool} <= current.keys():
            raise TrafficError("the source and target traffic pools cannot be confirmed")
        try:
            source_only = all(
                math.isfinite(float(value))
                and abs(float(value) - (100 if pool == source_pool else 0)) < 0.000001
                for pool, value in current.items()
            )
        except (TypeError, ValueError):
            source_only = False
        if not source_only:
            raise TrafficError("the target may serve traffic; refusing its sizing change")

    async def set_split(
        self,
        *,
        source_pool: str,
        target_pool: str,
        target_percent: int,
        migration_id: str | None = None,
        transition_id: str | None = None,
    ) -> AppliedWeights:
        if not 0 <= target_percent <= 100:
            raise ValueError("target_percent is a percentage")
        status, _ = await self._call("GET", "/v1/traffic/status")
        unknown = status.get("unknownOperationIds") or []
        if unknown:
            # Only a person who knows the previous writer has stopped may reconcile these.
            raise TrafficError(f"UNKNOWN_STATE: reconcile {', '.join(unknown)} first")
        current = {name: float(value) for name, value in status["weights"].items()}
        missing = sorted({source_pool, target_pool} - current.keys())
        if missing:
            raise TrafficError(
                f"the traffic service has no environment {missing}: trafficPool must be one of "
                f"{sorted(current)}"
            )
        # The service needs every pool it manages, summing to 100.
        weights = dict.fromkeys(current, 0.0)
        weights[source_pool] = float(100 - target_percent)
        weights[target_pool] = float(target_percent)
        if _same(current, weights):
            # Already applied: a change whose answer was lost, or a repeat after a restart.
            return AppliedWeights(weights=weights, effective_percent=_rounded(current))

        revision = int(status["revision"])
        correlation = f"{migration_id}-{transition_id}-" if migration_id else ""
        accepted, headers = await self._call(
            "POST",
            "/v1/traffic/actions",
            json={
                "action": "SET_WEIGHTS",
                "expectedRevision": revision,
                "weights": weights,
                **({"migrationId": migration_id} if migration_id else {}),
                **({"transitionId": transition_id} if transition_id else {}),
            },
            # The same change at the same revision is the same request, so a retry after a lost
            # answer finds the first operation instead of starting another.
            key=f"control-plane-{correlation}{target_pool}-{target_percent}-r{revision}",
        )
        operation = await self._wait(headers.get("location") or accepted["statusUrl"])
        result = operation.get("result") or {}
        reported = {name: float(value) for name, value in (result.get("weights") or {}).items()}
        if not _same(reported, weights):
            raise TrafficError(f"weights read back as {reported}, expected {weights}", operation)
        return AppliedWeights(
            weights=weights,
            effective_percent=_rounded(reported),
            operation_id=operation.get("operationId"),
            operation_status=operation["status"],
            started_at=operation.get("startedAt"),
            finished_at=operation.get("finishedAt"),
        )

    async def _wait(self, url: str) -> dict[str, Any]:
        waited = 0.0
        while True:
            operation, headers = await self._call("GET", url)
            state = operation.get("status")
            if state == "SUCCEEDED":
                return operation
            if state not in RUNNING:
                # FAILED changed nothing. UNKNOWN may have, and blocks every change until a person
                # reconciles it, so neither is retried here.
                raise TrafficError(
                    f"{state} {operation.get('operationId')}: {operation.get('error') or ''}",
                    operation,
                )
            if waited >= self.poll_timeout:
                raise TrafficError(f"{operation.get('operationId')} still {state}", operation)
            delay = float(headers.get("retry-after") or 1)
            await self.sleep(delay)
            waited += delay

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        headers = dict(self.headers)
        if key:
            headers["Idempotency-Key"] = key
        try:
            response = await self.http.request(method, path, json=json, headers=headers)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TrafficError(f"{method} {path}: {error!r}") from error
        if response.status_code >= 400:
            raise TrafficError(
                f"{method} {path}: {response.status_code} {body.get('code')} {body.get('message')}"
            )
        return dict(body), response.headers


def _same(left: dict[str, float], right: dict[str, float]) -> bool:
    return left.keys() == right.keys() and all(
        math.isclose(left[name], right[name], abs_tol=1e-6) for name in right
    )


def _rounded(weights: dict[str, float]) -> dict[str, float]:
    return {name: round(value, 1) for name, value in weights.items()}
