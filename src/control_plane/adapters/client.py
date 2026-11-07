"""HTTP client for the common migration API. Knows the wire, decides nothing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


class AdapterUnreachable(Exception):
    """No usable answer: a network error or a 5xx. This never proves a command was not accepted."""


class AdapterError(Exception):
    """The adapter refused, in the common error shape."""

    def __init__(self, status: int, code: str, message: str, blockers: list[str]) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.blockers = blockers


@dataclass(frozen=True)
class Decision:
    result: str
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.result == "PASS"


class MigrationView:
    """A status response. Adapters add fields of their own, so the raw body stays available."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    @property
    def migration_id(self) -> str:
        return str(self.raw["migrationId"])

    @property
    def revision(self) -> int:
        return int(self.raw["revision"])

    @property
    def lifecycle(self) -> str:
        return str(self.raw["lifecycle"])

    @property
    def health(self) -> str:
        return str(self.raw.get("health", "UNKNOWN"))

    @property
    def active_operation_id(self) -> str | None:
        value = self.raw.get("activeOperationId")
        return str(value) if value else None

    @property
    def allowed_actions(self) -> list[str]:
        return list(self.raw.get("allowedActions") or [])

    @property
    def route(self) -> str:
        return str((self.raw.get("observed") or {}).get("route", "UNKNOWN"))

    @property
    def target_writes_possible(self) -> bool:
        return bool((self.raw.get("observed") or {}).get("targetWritesPossible"))

    @property
    def details(self) -> dict[str, Any]:
        return dict(self.raw.get("details") or {})

    def readiness(self, name: str) -> Decision:
        return _decision((self.raw.get("readiness") or {}).get(name))

    def advance_check_failures(self, *, ignoring: frozenset[str]) -> list[str]:
        """Required ADVANCE checks that did not pass, other than the ones named."""
        return [
            check["code"]
            for check in self.raw.get("checks") or []
            if check.get("stage") == "ADVANCE"
            and check.get("required", True)
            and check.get("code") not in ignoring
            and not _decision(check.get("decision")).passed
        ]


def _decision(raw: dict[str, Any] | None) -> Decision:
    if not raw:
        return Decision("UNKNOWN", ("MISSING",))
    return Decision(str(raw.get("result", "UNKNOWN")), tuple(raw.get("reasons") or ()))


@dataclass(frozen=True)
class OperationView:
    operation_id: str
    status: str
    error: dict[str, Any] | None
    raw: dict[str, Any]


class AdapterClient:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}"}
        self._reads: dict[str, asyncio.Task[dict[str, Any]]] = {}

    async def capabilities(self) -> dict[str, Any]:
        return await self._call("GET", "/v1/capabilities")

    async def create_migration(self, key: str, body: dict[str, Any]) -> MigrationView:
        return MigrationView(await self._call("POST", "/v1/migrations", body=body, key=key))

    async def migration(self, migration_id: str) -> MigrationView:
        task = self._reads.get(migration_id)
        if task is None or task.done():
            task = asyncio.create_task(self._call("GET", f"/v1/migrations/{migration_id}"))
            self._reads[migration_id] = task
        try:
            return MigrationView(await asyncio.shield(task))
        finally:
            if task.done() and self._reads.get(migration_id) is task:
                self._reads.pop(migration_id, None)

    async def act(self, migration_id: str, key: str, body: dict[str, Any]) -> str:
        """Returns the operation id. Accepted is not executed: poll `operation`."""
        accepted = await self._call(
            "POST", f"/v1/migrations/{migration_id}/actions", body=body, key=key
        )
        return str(accepted["operationId"])

    async def operation(self, operation_id: str) -> OperationView:
        raw = await self._call("GET", f"/v1/operations/{operation_id}")
        return OperationView(operation_id, str(raw["status"]), raw.get("error"), raw)

    async def register_binding(self, key: str, body: dict[str, Any]) -> str:
        accepted = await self._call("POST", "/v1/bindings", body=body, key=key)
        return str(accepted["bindingId"])

    async def binding(self, binding_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/bindings/{binding_id}")

    async def _call(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, key: str | None = None
    ) -> dict[str, Any]:
        headers = dict(self.headers)
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            response = await self.http.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise AdapterUnreachable(f"{method} {path}: {error!r}") from error
        if response.status_code >= 500:
            raise AdapterUnreachable(f"{method} {path}: {response.status_code}")
        if response.status_code >= 400:
            raise _refusal(response)
        return dict(response.json())


def _refusal(response: httpx.Response) -> AdapterError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return AdapterError(
        response.status_code,
        # Branch on the code, not on the status: the adapters disagree on some statuses.
        str(body.get("code") or f"HTTP_{response.status_code}"),
        str(body.get("message") or body.get("detail") or response.reason_phrase),
        list(body.get("blockers") or []),
    )
