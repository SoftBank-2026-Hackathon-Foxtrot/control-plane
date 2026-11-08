from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from control_plane.ai.context import DATA_SCOPE
from control_plane.clock import Clock
from control_plane.errors import ApiError
from control_plane.store.table import Table, VersionConflict

Kind = Literal["CONVERSATION", "ANALYSIS", "RECOMMENDATION"]


class AIStore:
    def __init__(self, table: Table, clock: Clock, ttl_days: int) -> None:
        self.table, self.clock, self.ttl_days = table, clock, ttl_days

    async def owned(self, kind: Kind, identifier: str, user: str) -> dict[str, Any]:
        item = await self.table.get(f"AI_{kind}#{identifier}", "META")
        value = json.loads(item.body) if item else None
        if (
            not value
            or value.get("dataScope") != DATA_SCOPE
            or value["ownerUserId"] != user
            or value["expiresAt"] <= self.clock.now().timestamp()
        ):
            raise ApiError(404, "NOT_FOUND", "AI record not found")
        return dict(value)

    async def save(
        self, kind: Kind, identifier: str, user: str, application: str, result: dict[str, Any]
    ) -> None:
        now = self.clock.now()
        ttl = int(now.timestamp()) + self.ttl_days * 86400
        value = {
            "schemaVersion": 2,
            "dataScope": DATA_SCOPE,
            "ownerUserId": user,
            "applicationId": application,
            "createdAt": now.isoformat(),
            "expiresAt": ttl,
            "result": result,
        }
        body = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(body.encode()) > 300000:
            raise ApiError(413, "AI_RESULT_TOO_LARGE", "AI result exceeds storage limit")
        await self.table.put(
            f"AI_{kind}#{identifier}", "META", body, expected_version=0, expires_at=ttl
        )
        await self.table.put(
            f"USER#{user}",
            f"AI#{kind}#{now.isoformat()}#{identifier}",
            json.dumps({"id": identifier, "createdAt": now.isoformat(), "expiresAt": ttl}),
            expected_version=0,
            expires_at=ttl,
        )

    async def history(self, identifier: str, user: str) -> list[dict[str, Any]]:
        meta = await self.owned("CONVERSATION", identifier, user)
        ids = meta["result"].get("recentTurns", [])
        # References are bounded; turn contents never accumulate in the metadata item.
        turns = []
        for turn_id in ids[-10:]:
            item = await self.table.get(f"AI_CONVERSATION#{identifier}", f"TURN#{turn_id}")
            if item:
                turns.append(json.loads(item.body))
        return turns

    async def append(
        self, identifier: str, user: str, message: str, response: dict[str, Any]
    ) -> None:
        meta = await self.owned("CONVERSATION", identifier, user)
        turn_id = uuid4().hex
        await self.table.put(
            f"AI_CONVERSATION#{identifier}",
            f"TURN#{turn_id}",
            json.dumps(
                {"user": message, "assistant": response, "createdAt": self.clock.now().isoformat()},
                ensure_ascii=False,
            ),
            expected_version=0,
            expires_at=meta["expiresAt"],
        )
        for _ in range(5):
            item = await self.table.get(f"AI_CONVERSATION#{identifier}", "META")
            assert item is not None
            value = json.loads(item.body)
            value["result"]["recentTurns"] = [*value["result"].get("recentTurns", []), turn_id][
                -10:
            ]
            try:
                await self.table.put(
                    item.pk,
                    item.sk,
                    json.dumps(value, ensure_ascii=False),
                    expected_version=item.version,
                    expires_at=value["expiresAt"],
                )
                return
            except VersionConflict:
                continue
        raise ApiError(409, "CONVERSATION_BUSY", "conversation changed; try again")
