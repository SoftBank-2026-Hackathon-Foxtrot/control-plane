"""Stops and resumes the workload, and knows whether it really stopped.

Processes poll the platform and report back. The channel depends on neither the database nor Redis,
because those are what is being moved, and needs no inbound route to the application VMs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from control_plane.clock import Clock, stamp
from control_plane.domain.models import ActorAck, ActorState, HoldDesired, HoldState
from control_plane.seed import Seed
from control_plane.store.repository import Repository
from control_plane.store.table import VersionConflict


@dataclass(frozen=True)
class Directive:
    desired: HoldDesired
    generation: int
    object_migration_id: str | None
    poll_after_ms: int


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
        image_ref: str | None = None,
    ) -> Directive:
        await self.repo.save_ack(
            ActorAck(
                scope=self.scope,
                image_ref=image_ref,
                actor_id=actor_id,
                instance_id=instance_id,
                generation=generation,
                state=state,
                in_flight=in_flight,
                observed_at=self.clock.now(),
            )
        )
        hold, _ = await self.repo.hold(self.scope)
        return Directive(
            desired=hold.desired,
            generation=hold.generation,
            object_migration_id=hold.object_migration_id,
            poll_after_ms=self._poll_interval(hold),
        )

    def _poll_interval(self, hold: HoldState) -> int:
        """Fast while stopping, and until the processes have had time to report that they run."""
        timings = self.seed.timings
        settling = hold.released_at is not None and (
            (self.clock.now() - hold.released_at).total_seconds() < timings.resume_workload_timeout
        )
        fast = hold.desired is HoldDesired.HOLD or settling
        return timings.fast_poll_ms if fast else timings.idle_poll_ms

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
        hold, _ = await self.repo.hold(self.scope)
        return hold

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
