"""Stops and resumes the workload, and knows whether it really stopped.

Processes poll the platform and report back. The channel depends on neither the database nor Redis,
because those are what is being moved, and needs no inbound route to the application VMs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from control_plane.clock import Clock, stamp
from control_plane.domain.models import ActorAck, ActorState, HoldDesired, HoldState, RedisRoute
from control_plane.seed import Seed
from control_plane.store.repository import Repository
from control_plane.store.table import VersionConflict


@dataclass(frozen=True)
class Directive:
    desired: HoldDesired
    generation: int
    object_migration_id: str | None
    poll_after_ms: int
    redis_route: RedisRoute = RedisRoute.PROXY
    redis_endpoint: str | None = None
    redis_route_generation: int = 0


@dataclass(frozen=True)
class ActorStatus:
    """One registered actor, aggregated from its processes."""

    actor_id: str
    generation: int
    state: str
    in_flight: int
    observed_at: datetime | None
    reported: int
    expected: int
    active_web_sockets: int | None = None

    def satisfied(self, state: ActorState) -> bool:
        return self.state == state.value and self.reported >= self.expected


class WorkloadCoordinator:
    def __init__(self, repo: Repository, seed: Seed, clock: Clock) -> None:
        self.repo = repo
        self.seed = seed
        self.clock = clock
        self.scope = seed.scope

    # ---- what the processes call -------------------------------------------------------------
    async def heartbeat(
        self,
        *,
        actor_id: str,
        instance_id: str,
        generation: int,
        state: ActorState,
        in_flight: int,
        active_web_sockets: int | None = None,
        redis_route: RedisRoute | None = None,
        redis_route_generation: int = 0,
        stale_connections: int = 0,
    ) -> Directive:
        await self.repo.save_ack(
            ActorAck(
                scope=self.scope,
                actor_id=actor_id,
                instance_id=instance_id,
                generation=generation,
                state=state,
                in_flight=in_flight,
                active_web_sockets=active_web_sockets,
                observed_at=self.clock.now(),
                redis_route=redis_route,
                redis_route_generation=redis_route_generation,
                stale_connections=stale_connections,
            )
        )
        hold = await self.hold()
        return Directive(
            desired=hold.desired,
            generation=hold.generation,
            object_migration_id=hold.object_migration_id,
            poll_after_ms=await self._poll_interval(hold),
            redis_route=RedisRoute.PROXY,
            redis_endpoint=None,
            redis_route_generation=hold.redis_route_generation,
        )

    async def _poll_interval(self, hold: HoldState) -> int:
        """Fast while stopping and until processes have reported that they run again."""
        timings = self.seed.timings
        settling = hold.released_at is not None and (
            (self.clock.now() - hold.released_at).total_seconds() < timings.resume_workload_timeout
        )
        if hold.desired is HoldDesired.HOLD or settling:
            return timings.fast_poll_ms
        return timings.idle_poll_ms

    # ---- what the engine calls ----------------------------------------------------------------
    async def publish_object_migration(self, workflow_id: str, migration_id: str) -> None:
        await self._update(workflow_id=workflow_id, object_migration_id=migration_id)

    async def request_hold(self, workflow_id: str) -> HoldState:
        """Starts a new generation, or returns the hold already in force."""
        for _ in range(5):
            hold, version = await self.repo.hold(self.scope)
            if hold.desired is HoldDesired.HOLD:
                return hold
            updated = hold.model_copy(
                update={
                    "desired": HoldDesired.HOLD,
                    "generation": hold.generation + 1,
                    "workflow_id": workflow_id,
                    "requested_at": self.clock.now(),
                    "held_since": None,
                    "released_at": None,
                }
            )
            try:
                await self.repo.save_hold(updated, expected_version=version)
            except VersionConflict:
                continue
            await self._forget_gone_processes()
            return updated
        raise VersionConflict(f"hold of {self.scope}")

    async def _forget_gone_processes(self) -> None:
        """Only at the start of a hold, and only long after the TTL, so it cannot race a report."""
        limit = timedelta(seconds=self.seed.timings.instance_ttl * 4)
        for ack in await self.repo.acks(self.scope):
            if self.clock.now() - ack.observed_at > limit:
                await self.repo.delete_ack(ack)

    async def release(self) -> HoldState:
        """Business work may resume. The generation stays, so acknowledgements can be matched."""
        for _ in range(5):
            hold, version = await self.repo.hold(self.scope)
            if hold.desired is HoldDesired.RUN:
                return hold
            updated = hold.model_copy(
                update={"desired": HoldDesired.RUN, "released_at": self.clock.now()}
            )
            try:
                await self.repo.save_hold(updated, expected_version=version)
            except VersionConflict:
                continue
            return updated
        raise VersionConflict(f"hold of {self.scope}")

    async def hold(self) -> HoldState:
        for _ in range(5):
            hold, version = await self.repo.hold(self.scope)
            if hold.redis_route is RedisRoute.PROXY and hold.redis_endpoint is None:
                return hold
            # Retire a persisted temporary-proxy order without changing business HOLD ownership.
            updated = hold.model_copy(
                update={
                    "redis_route": RedisRoute.PROXY,
                    "redis_endpoint": None,
                    "redis_route_generation": hold.redis_route_generation + 1,
                }
            )
            try:
                await self.repo.save_hold(updated, expected_version=version)
            except VersionConflict:
                continue
            return updated
        raise VersionConflict(f"hold of {self.scope}")

    async def mark_held(self) -> HoldState:
        """Records when the whole workload first stood still. Later calls keep the first time."""
        for _ in range(5):
            hold, version = await self.repo.hold(self.scope)
            if hold.held_since is not None:
                return hold
            updated = hold.model_copy(update={"held_since": self.clock.now()})
            try:
                await self.repo.save_hold(updated, expected_version=version)
            except VersionConflict:
                continue
            return updated
        raise VersionConflict(f"hold of {self.scope}")

    async def request_redis_route(
        self, workflow_id: str, route: RedisRoute, endpoint: str | None
    ) -> HoldState:
        """Only the permanent proxy can be advertised to application processes."""
        if route is not RedisRoute.PROXY or endpoint is not None:
            raise ValueError("applications always use the fixed Redis proxy")
        for _ in range(5):
            hold, version = await self.repo.hold(self.scope)
            if hold.redis_route is route and hold.redis_endpoint == endpoint:
                return hold
            updated = hold.model_copy(
                update={
                    "workflow_id": workflow_id,
                    "redis_route": route,
                    "redis_endpoint": endpoint,
                    "redis_route_generation": hold.redis_route_generation + 1,
                }
            )
            try:
                await self.repo.save_hold(updated, expected_version=version)
            except VersionConflict:
                continue
            return updated
        raise VersionConflict(f"hold of {self.scope}")

    async def route_followed(self, generation: int, route: RedisRoute) -> tuple[bool, list[str]]:
        """Whether every process of every actor follows the route order `generation` and has
        closed its connections on the other route, and which actors do not yet."""
        now = self.clock.now()
        ttl = timedelta(seconds=self.seed.timings.instance_ttl)
        live = [ack for ack in await self.repo.acks(self.scope) if now - ack.observed_at <= ttl]
        waiting = []
        for actor_id, expected in self.seed.all_actors().items():
            mine = [ack for ack in live if ack.actor_id == actor_id]
            done = [
                ack
                for ack in mine
                if ack.redis_route is route
                and ack.redis_route_generation == generation
                and ack.stale_connections == 0
            ]
            # Every live process must be done: one more than expected is still a direct writer.
            if len(done) < expected or len(done) < len(mine):
                stale = sum(ack.stale_connections for ack in mine)
                waiting.append(
                    f"{actor_id} ({len(done)}/{max(expected, len(mine))} on {route.value}"
                    f", {stale} old connections)"
                )
        return not waiting, waiting

    async def actor_statuses(self, generation: int) -> list[ActorStatus]:
        now = self.clock.now()
        ttl = timedelta(seconds=self.seed.timings.instance_ttl)
        live = [ack for ack in await self.repo.acks(self.scope) if now - ack.observed_at <= ttl]
        statuses = []
        for actor_id, expected in self.seed.all_actors().items():
            mine = [
                ack for ack in live if ack.actor_id == actor_id and ack.generation == generation
            ]
            states = {ack.state for ack in mine}
            statuses.append(
                ActorStatus(
                    actor_id=actor_id,
                    generation=generation,
                    state=states.pop().value if len(states) == 1 else ActorState.HOLDING.value,
                    in_flight=sum(ack.in_flight for ack in mine),
                    # The oldest report vouches for the whole actor.
                    observed_at=min((ack.observed_at for ack in mine), default=None),
                    reported=len(mine),
                    expected=expected,
                    active_web_sockets=(
                        sum(ack.active_web_sockets or 0 for ack in mine)
                        if len(mine) >= expected
                        and all(ack.active_web_sockets is not None for ack in mine)
                        else None
                    ),
                )
            )
        return statuses

    async def all_reached(self, generation: int, state: ActorState) -> tuple[bool, list[str]]:
        """Whether every process of every actor reports `state`, and which actors do not."""
        waiting = []
        for status in await self.actor_statuses(generation):
            held_clean = state is not ActorState.HELD or status.in_flight == 0
            if not (status.satisfied(state) and held_clean):
                waiting.append(
                    f"{status.actor_id} ({status.reported}/{status.expected} {status.state}"
                    f", {status.in_flight} in flight)"
                )
        return not waiting, waiting

    async def participants(self, generation: int) -> list[dict[str, object]]:
        """The evidence as the DB adapter verifies it: one entry per registered actor, in the state
        it really reports. The adapter trusts this record, so it must never claim more than that."""
        statuses = await self.actor_statuses(generation)
        return [
            {
                "id": status.actor_id,
                "generation": status.generation,
                "state": status.state,
                "inFlight": status.in_flight,
                "observedAt": stamp(status.observed_at),
            }
            for status in statuses
            if status.observed_at is not None
        ]

    async def _update(self, *, workflow_id: str, **fields: object) -> None:
        for _ in range(5):
            hold, version = await self.repo.hold(self.scope)
            updated = hold.model_copy(update={"workflow_id": workflow_id, **fields})
            if updated == hold:
                return
            try:
                await self.repo.save_hold(updated, expected_version=version)
            except VersionConflict:
                continue
            return
        raise VersionConflict(f"hold of {self.scope}")
