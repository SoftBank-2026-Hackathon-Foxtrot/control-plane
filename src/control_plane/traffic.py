"""Who gets how much traffic. The engine asks for percentages; this file knows Cloudflare."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import httpx


class TrafficError(Exception):
    """The weight change failed or could not be confirmed."""


@dataclass(frozen=True)
class AppliedWeights:
    weights: dict[str, float]
    """What was sent to the load balancer."""
    effective_percent: dict[str, float]
    """The split those weights produce. It can differ from the request, see `ratio_weights`."""


class TrafficController(Protocol):
    async def set_split(
        self, *, source_pool: str, target_pool: str, target_percent: int
    ) -> AppliedWeights: ...


def ratio_weights(target_percent: int, *, step: float) -> tuple[float, float]:
    """The (source, target) weights in multiples of `step`, in 0..1, nearest to the request.

    Cloudflare divides each weight by their sum, so 75:25 is 0.3:0.1. Its schema documents
    `default_weight` in steps of 0.1. If `pool_weights` follows the same rule, the smallest
    non-zero share is 1.0:0.1, about 9%, so 95:5 cannot be expressed exactly. The result is
    recorded as `effective_percent`, so the console shows what actually applies.
    """
    if not 0 <= target_percent <= 100:
        raise ValueError("target_percent is a percentage")
    if target_percent == 0:
        return 1.0, 0.0
    if target_percent == 100:
        return 0.0, 1.0
    unit = Decimal(str(step))
    count = int(Decimal(1) / unit)
    best: tuple[int, int] | None = None
    best_error = float("inf")
    for source in range(1, count + 1):
        for target in range(1, count + 1):
            error = abs(target / (source + target) * 100 - target_percent)
            # Among equal splits, prefer the largest weights: they read best in the dashboard.
            if error < best_error - 1e-9 or (
                abs(error - best_error) <= 1e-9 and best and source + target > sum(best)
            ):
                best, best_error = (source, target), error
    assert best is not None
    return float(best[0] * unit), float(best[1] * unit)


class CloudflareTraffic:
    """Changes `random_steering` on one load balancer, then reads it back."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        token: str,
        zone_id: str,
        load_balancer_id: str,
        weight_step: float = 0.1,
    ) -> None:
        self.http = http
        self.path = f"/zones/{zone_id}/load_balancers/{load_balancer_id}"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.weight_step = weight_step

    async def set_split(
        self, *, source_pool: str, target_pool: str, target_percent: int
    ) -> AppliedWeights:
        source, target = ratio_weights(target_percent, step=self.weight_step)
        weights = {source_pool: source, target_pool: target}
        # A PATCH overwrites the properties it names, so this sends the whole object. Pools that
        # are not part of this move get no share.
        await self._call(
            "PATCH",
            json={
                "steering_policy": "random",
                "random_steering": {"default_weight": 0, "pool_weights": weights},
            },
        )
        applied = (await self._call("GET")).get("random_steering") or {}
        confirmed = applied.get("pool_weights") or {}
        if any(float(confirmed.get(pool, -1)) != weight for pool, weight in weights.items()):
            raise TrafficError(f"weights read back as {confirmed}, expected {weights}")
        total = source + target
        return AppliedWeights(
            weights=weights,
            effective_percent={
                source_pool: round(source / total * 100, 1),
                target_pool: round(target / total * 100, 1),
            },
        )

    async def _call(self, method: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self.http.request(method, self.path, json=json, headers=self.headers)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TrafficError(f"{method} load balancer: {error!r}") from error
        if response.status_code >= 400 or not body.get("success"):
            raise TrafficError(
                f"{method} load balancer: {response.status_code} {body.get('errors')}"
            )
        return dict(body["result"])
