"""Read applied deployment snapshots without creating a provisioner or running jobs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

from control_plane.deployment.api import APP_TEMPLATE
from control_plane.errors import ApiError


class DeploymentInventory(Protocol):
    def environments(self) -> list[dict[str, Any]]: ...

    def environment(self, identifier: str) -> dict[str, Any] | None: ...


class SqliteDeploymentInventory:
    """Open an existing provisioner database in read-only mode for each query."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def environments(self) -> list[dict[str, Any]]:
        return self._read()

    def environment(self, identifier: str) -> dict[str, Any] | None:
        records = self._read(identifier)
        return records[0] if records else None

    def _read(self, identifier: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT id, org_id, cloud, template, deployment_id, status, applied_snapshot "
            "FROM environments WHERE template = ?"
        )
        parameters = [APP_TEMPLATE]
        if identifier is not None:
            query += " AND id = ?"
            parameters.append(identifier)
        query += " ORDER BY created_at DESC"
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.row_factory = sqlite3.Row
                records = []
                for row in connection.execute(query, parameters):
                    record = dict(row)
                    raw = record["applied_snapshot"]
                    snapshot = json.loads(raw) if raw else None
                    if snapshot is not None and not isinstance(snapshot, dict):
                        raise ValueError("snapshot must be an object")
                    record["applied_snapshot"] = snapshot
                    records.append(record)
                return records
        except (OSError, sqlite3.Error, ValueError) as error:
            raise ApiError(
                503,
                "DEPLOYMENT_INVENTORY_UNAVAILABLE",
                "application deployment inventory is unavailable",
            ) from error
