"""The DB adapter verifies quiesce evidence by reading its own control records.

There is no HTTP API for these records. `DbGate` is the only place that knows it, so replacing the
SDK with an API later changes one class.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol


class DbGateError(Exception):
    """A control record was refused. The message says why; it is safe to show."""


class DbGate(Protocol):
    async def publish_catalog(
        self,
        *,
        databases: dict[str, dict[str, Any]],
        tables: list[str],
        sequences: list[str],
        scope: str,
        participants: list[str],
        dms_ref: str | None,
        source_ref: str,
        target_ref: str,
        dms_job: dict[str, Any] | None,
    ) -> None: ...

    async def issue_held(
        self,
        *,
        migration_id: str,
        transition_id: str,
        generation: int,
        participants: list[dict[str, object]],
        held_since: datetime,
    ) -> str: ...

    async def renew(self, reference: str, participants: list[dict[str, object]]) -> None: ...

    async def record_running(
        self,
        *,
        migration_id: str,
        generation: int,
        participants: list[dict[str, object]],
        probe_result: str,
        probe_ref: str | None,
    ) -> None: ...


class UnavailableDbGate:
    """For a deployment without the DB adapter's package: a DB cutover stops with a clear reason."""

    async def publish_catalog(self, **_: Any) -> None:
        raise DbGateError("the db_operator package is not installed")

    async def issue_held(self, **_: Any) -> str:
        raise DbGateError("the db_operator package is not installed")

    async def renew(self, reference: str, participants: list[dict[str, object]]) -> None:
        raise DbGateError("the db_operator package is not installed")

    async def record_running(self, **_: Any) -> None:
        raise DbGateError("the db_operator package is not installed")


class SdkDbGate:
    """Uses `db_operator`'s own SDK, so the records are always in the format it verifies."""

    def __init__(self, table: str, *, namespace: str, region: str, endpoint: str | None = None):
        from db_operator.config import ResourceCatalog
        from db_operator.control import ControlPlaneRecords
        from db_operator.store import Store

        store = Store(table, namespace=namespace, region=region, endpoint=endpoint)
        self.records = ControlPlaneRecords(store)
        self.catalog = ResourceCatalog(store)

    async def publish_catalog(
        self,
        *,
        databases: dict[str, dict[str, Any]],
        tables: list[str],
        sequences: list[str],
        scope: str,
        participants: list[str],
        dms_ref: str | None,
        source_ref: str,
        target_ref: str,
        dms_job: dict[str, Any] | None,
    ) -> None:
        from db_operator.config import Database

        if (dms_ref is None) != (dms_job is None):
            raise DbGateError("DMS registration needs both dms_ref and dms_job")

        def publish() -> None:
            for reference, definition in databases.items():
                self.catalog.database(
                    reference,
                    Database.model_validate(definition),
                    tables=tables,
                    sequences=sequences,
                )
            self.catalog.workload(scope, participants)
            if dms_ref is not None:
                assert dms_job is not None
                self.catalog.dms_job(
                    dms_ref, source_ref=source_ref, target_ref=target_ref, binding=dms_job
                )

        await self._guarded(publish)

    async def issue_held(
        self,
        *,
        migration_id: str,
        transition_id: str,
        generation: int,
        participants: list[dict[str, object]],
        held_since: datetime,
    ) -> str:
        return str(
            await self._guarded(
                lambda: self.records.held(
                    migration_id=migration_id,
                    transition_id=transition_id,
                    generation=generation,
                    participants=participants,
                    held_since=held_since,
                )
            )
        )

    async def renew(self, reference: str, participants: list[dict[str, object]]) -> None:
        await self._guarded(lambda: self.records.renew(reference, participants))

    async def record_running(
        self,
        *,
        migration_id: str,
        generation: int,
        participants: list[dict[str, object]],
        probe_result: str,
        probe_ref: str | None,
    ) -> None:
        await self._guarded(
            lambda: self.records.running(
                migration_id,
                generation=generation,
                participants=participants,
                probe_result=probe_result,
                probe_ref=probe_ref,
            )
        )

    @staticmethod
    async def _guarded(call: Any) -> Any:
        from db_operator.models import OperatorError

        try:
            return await asyncio.to_thread(call)
        except OperatorError as error:
            raise DbGateError(f"{error.code}: {error}") from error
