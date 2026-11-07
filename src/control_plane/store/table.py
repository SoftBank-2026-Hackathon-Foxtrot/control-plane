"""One table of versioned JSON documents, in DynamoDB or in memory.

The interface is deliberately small: a get, a prefix query and a compare-and-swap write. Every
guarantee the engine needs (one owner, one active workflow, no lost update) is built from the
compare-and-swap, so the in-memory table used in tests enforces the same rules as DynamoDB.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class VersionConflict(Exception):
    """The stored version is not the expected one."""


@dataclass(frozen=True)
class Item:
    pk: str
    sk: str
    version: int
    body: str


@dataclass(frozen=True)
class Page:
    items: list[Item]
    next_sk: str | None


class Table(Protocol):
    async def write_many(self, writes: list[tuple[str, str, str, int]]) -> None: ...

    async def get(self, pk: str, sk: str) -> Item | None: ...

    async def put(self, pk: str, sk: str, body: str, *, expected_version: int | None) -> int:
        """Writes and returns the new version. `expected_version=0` means it must not exist;
        `None` overwrites whatever is there."""
        ...

    async def query(
        self, pk: str, prefix: str = "", *, after: str | None = None, limit: int | None = None
    ) -> Page: ...

    async def delete(self, pk: str, sk: str) -> None: ...


class MemoryTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Item] = {}
        self._lock = threading.Lock()

    async def write_many(self, writes: list[tuple[str, str, str, int]]) -> None:
        with self._lock:
            for pk, sk, _, expected in writes:
                item = self._items.get((pk, sk))
                if (item.version if item else 0) != expected:
                    raise VersionConflict(f"{pk}/{sk}")
            for pk, sk, body, expected in writes:
                self._items[(pk, sk)] = Item(pk, sk, expected + 1, body)

    async def get(self, pk: str, sk: str) -> Item | None:
        with self._lock:
            return self._items.get((pk, sk))

    async def put(self, pk: str, sk: str, body: str, *, expected_version: int | None) -> int:
        with self._lock:
            current = self._items.get((pk, sk))
            stored = current.version if current else 0
            if expected_version is not None and stored != expected_version:
                raise VersionConflict(f"{pk}/{sk}: {stored} != {expected_version}")
            version = stored + 1
            self._items[(pk, sk)] = Item(pk, sk, version, body)
            return version

    async def query(
        self, pk: str, prefix: str = "", *, after: str | None = None, limit: int | None = None
    ) -> Page:
        with self._lock:
            matching = sorted(
                (item for (p, s), item in self._items.items() if p == pk and s.startswith(prefix)),
                key=lambda item: item.sk,
            )
        if after is not None:
            matching = [item for item in matching if item.sk > after]
        if limit is not None and len(matching) > limit:
            return Page(matching[:limit], matching[limit - 1].sk)
        return Page(matching, None)

    async def delete(self, pk: str, sk: str) -> None:
        with self._lock:
            self._items.pop((pk, sk), None)


class DynamoTable:
    """`pk`/`sk` string keys, a numeric `version` and a JSON `body`.

    Reads are strongly consistent: the engine decides from what it reads, and a session must stop
    working the moment it is revoked.
    """

    def __init__(
        self,
        name: str,
        *,
        region: str | None = None,
        endpoint: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.name = name
        self.client = client or boto3.client(
            "dynamodb",
            region_name=region,
            endpoint_url=endpoint,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )

    @staticmethod
    def _key(pk: str, sk: str) -> dict[str, dict[str, str]]:
        return {"pk": {"S": pk}, "sk": {"S": sk}}

    @staticmethod
    def _item(raw: dict[str, Any]) -> Item:
        return Item(raw["pk"]["S"], raw["sk"]["S"], int(raw["version"]["N"]), raw["body"]["S"])

    async def write_many(self, writes: list[tuple[str, str, str, int]]) -> None:
        operations = []
        for pk, sk, body, expected in writes:
            put: dict[str, Any] = {
                "TableName": self.name,
                "Item": {
                    **self._key(pk, sk),
                    "body": {"S": body},
                    "version": {"N": str(expected + 1)},
                },
                "ConditionExpression": "attribute_not_exists(pk)"
                if expected == 0
                else "version = :v",
            }
            if expected:
                put["ExpressionAttributeValues"] = {":v": {"N": str(expected)}}
            operations.append({"Put": put})
        try:
            await asyncio.to_thread(self.client.transact_write_items, TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as error:
            reasons = error.response.get("CancellationReasons", [])
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                raise VersionConflict("transaction condition failed") from error
            raise

    async def get(self, pk: str, sk: str) -> Item | None:
        response = await asyncio.to_thread(
            self.client.get_item,
            TableName=self.name,
            Key=self._key(pk, sk),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return self._item(raw) if raw else None

    async def put(self, pk: str, sk: str, body: str, *, expected_version: int | None) -> int:
        if expected_version is None:
            # Overwrites must still move the version forward: readers compare on it.
            response = await asyncio.to_thread(
                self.client.update_item,
                TableName=self.name,
                Key=self._key(pk, sk),
                UpdateExpression="SET body = :body ADD version :one",
                ExpressionAttributeValues={":body": {"S": body}, ":one": {"N": "1"}},
                ReturnValues="UPDATED_NEW",
            )
            return int(response["Attributes"]["version"]["N"])
        condition: dict[str, Any] = (
            {"ConditionExpression": "attribute_not_exists(pk)"}
            if expected_version == 0
            else {
                "ConditionExpression": "version = :expected",
                "ExpressionAttributeValues": {":expected": {"N": str(expected_version)}},
            }
        )
        version = expected_version + 1
        try:
            await asyncio.to_thread(
                self.client.put_item,
                TableName=self.name,
                Item={
                    **self._key(pk, sk),
                    "version": {"N": str(version)},
                    "body": {"S": body},
                },
                **condition,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise VersionConflict(f"{pk}/{sk}") from error
            raise
        return version

    async def query(
        self, pk: str, prefix: str = "", *, after: str | None = None, limit: int | None = None
    ) -> Page:
        values: dict[str, Any] = {":pk": {"S": pk}}
        condition = "pk = :pk"
        # `after` wins: a cursor is always inside the prefix and resumes past the last row read.
        if prefix:
            condition += " AND begins_with(sk, :prefix)"
            values[":prefix"] = {"S": prefix}
        arguments: dict[str, Any] = {
            "TableName": self.name,
            "KeyConditionExpression": condition,
            "ExpressionAttributeValues": values,
            "ConsistentRead": True,
        }
        if after is not None:
            arguments["ExclusiveStartKey"] = self._key(pk, after)
        items: list[Item] = []
        while True:
            if limit is not None:
                arguments["Limit"] = limit - len(items)
            response = await asyncio.to_thread(self.client.query, **arguments)
            items.extend(self._item(raw) for raw in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return Page(items, None)
            if limit is not None and len(items) >= limit:
                return Page(items, last["sk"]["S"])
            arguments["ExclusiveStartKey"] = last

    async def delete(self, pk: str, sk: str) -> None:
        await asyncio.to_thread(self.client.delete_item, TableName=self.name, Key=self._key(pk, sk))
