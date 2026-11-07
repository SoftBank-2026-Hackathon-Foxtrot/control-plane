"""Moving the application onto the Redis proxy before a migration, and off it after (design 3-8).

Outside a migration the application reaches Redis directly. The proxy is what makes a Redis cutover
atomic, so every process must go through it before the Redis adapter's PREPARE, which refuses a
source that still has a direct connection. Entering, the proxy points at the source; leaving, at the
side that stays. Both ways a mix of direct and proxied processes writes to one Redis, so the data is
never at risk here: what is checked is that every process has really moved.

Only with `directWhenIdle` set on the Redis resource. Otherwise each call passes at once.
"""

from __future__ import annotations

from datetime import datetime

from control_plane.domain.models import RedisRoute, ResourceKind
from control_plane.engine.context import Outcome, Passed, StageContext, Stop, Wait


def _enabled(ctx: StageContext) -> bool:
    return ctx.seed.resources.redis.direct_when_idle


async def enter_proxy(ctx: StageContext) -> Outcome:
    if not _enabled(ctx) or ctx.stage.data.get("redis_entered"):
        return Passed()
    coordinator = ctx.services.coordinator
    timings = ctx.seed.timings
    data = ctx.stage.data
    hold = await coordinator.request_redis_route(ctx.workflow.id, RedisRoute.PROXY, None)
    followed, waiting = await coordinator.route_followed(
        hold.redis_route_generation, RedisRoute.PROXY
    )
    if not followed:
        data.pop("redis_route_stable_since", None)
        if ctx.elapsed("redis_entry") > timings.redis_route_timeout:
            # Back to the source directly: both routes reach the same Redis, so nothing is lost.
            await coordinator.request_redis_route(
                ctx.workflow.id, RedisRoute.DIRECT, ctx.workflow.source_env
            )
            ctx.reset_timer("redis_entry")
            await ctx.emit("REDIS_PROXY_ENTRY_REVERTED", resource=ResourceKind.REDIS)
            return Stop(
                "REDIS_ENTRY_TIMEOUT",
                "not every process moved to the Redis proxy",
                waiting,
                resource=ResourceKind.REDIS,
            )
        return Wait("moving to the Redis proxy: " + ", ".join(waiting))
    since = datetime.fromisoformat(data.setdefault("redis_route_stable_since", ctx.now.isoformat()))
    if (ctx.now - since).total_seconds() < timings.redis_route_stable_window:
        return Wait("every process is on the Redis proxy, confirming")
    data["redis_entered"] = True
    ctx.reset_timer("redis_entry")
    await ctx.emit(
        "REDIS_PROXY_ENTERED",
        f"route generation {hold.redis_route_generation}",
        resource=ResourceKind.REDIS,
    )
    return Passed()


async def leave_proxy(ctx: StageContext, env: str) -> Outcome:
    """Every process back on a direct connection to the Redis of `env`."""
    if not _enabled(ctx) or ctx.stage.data.get("redis_left") == env:
        return Passed()
    coordinator = ctx.services.coordinator
    hold = await coordinator.request_redis_route(ctx.workflow.id, RedisRoute.DIRECT, env)
    followed, waiting = await coordinator.route_followed(
        hold.redis_route_generation, RedisRoute.DIRECT
    )
    if not followed:
        if ctx.elapsed("redis_exit") > ctx.seed.timings.redis_route_timeout:
            return Stop(
                "REDIS_EXIT_TIMEOUT",
                f"not every process connected directly to the Redis of {env}",
                waiting,
                resource=ResourceKind.REDIS,
            )
        return Wait(f"leaving the Redis proxy for {env}: " + ", ".join(waiting))
    ctx.stage.data["redis_left"] = env
    ctx.reset_timer("redis_exit")
    await ctx.emit("REDIS_PROXY_LEFT", f"direct to {env}", resource=ResourceKind.REDIS)
    return Passed()
