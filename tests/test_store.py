"""The in-memory table used by the other tests must keep the same promises as DynamoDB."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from control_plane.domain.models import OAuthRequest, Workflow
from control_plane.store.repository import ActiveWorkflowExists, Repository
from control_plane.store.table import DynamoTable, MemoryTable, Table, VersionConflict


@pytest.fixture(params=["memory", "dynamodb"])
def table(request: pytest.FixtureRequest) -> Iterator[Table]:
    if request.param == "memory":
        yield MemoryTable()
        return
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-2")
        client.create_table(
            TableName="test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )
        yield DynamoTable("test", client=client)


async def test_new_items_must_not_exist(table: Table) -> None:
    assert await table.put("a", "b", "one", expected_version=0) == 1
    with pytest.raises(VersionConflict):
        await table.put("a", "b", "two", expected_version=0)
    item = await table.get("a", "b")
    assert item is not None and item.body == "one"


async def test_compare_and_swap(table: Table) -> None:
    await table.put("a", "b", "one", expected_version=0)
    assert await table.put("a", "b", "two", expected_version=1) == 2
    with pytest.raises(VersionConflict):
        await table.put("a", "b", "stale", expected_version=1)
    item = await table.get("a", "b")
    assert item is not None and (item.body, item.version) == ("two", 2)


async def test_overwrite_still_moves_the_version(table: Table) -> None:
    assert await table.put("a", "b", "one", expected_version=None) == 1
    assert await table.put("a", "b", "two", expected_version=None) == 2


async def test_query_is_sorted_by_prefix_and_pages(table: Table) -> None:
    for sk in ("EVENT#3", "EVENT#1", "EVENT#2", "STAGE#1"):
        await table.put("a", sk, sk, expected_version=0)
    await table.put("other", "EVENT#9", "x", expected_version=0)

    everything = await table.query("a", "EVENT#")
    assert [item.sk for item in everything.items] == ["EVENT#1", "EVENT#2", "EVENT#3"]
    assert everything.next_sk is None

    first = await table.query("a", "EVENT#", limit=2)
    assert [item.sk for item in first.items] == ["EVENT#1", "EVENT#2"]
    assert first.next_sk == "EVENT#2"
    second = await table.query("a", "EVENT#", after=first.next_sk, limit=2)
    assert [item.sk for item in second.items] == ["EVENT#3"]
    assert second.next_sk is None


async def test_delete(table: Table) -> None:
    await table.put("a", "b", "one", expected_version=0)
    await table.delete("a", "b")
    assert await table.get("a", "b") is None
    await table.delete("a", "b")


def workflow(identifier: str, now: datetime) -> Workflow:
    return Workflow(
        id=identifier,
        application="goods",
        source_env="aws",
        target_env="gcp",
        created_by="tester",
        created_at=now,
    )


async def test_one_active_workflow(table: Table) -> None:
    repo = Repository(table)
    now = datetime(2026, 11, 7, tzinfo=UTC)
    await repo.create_workflow(workflow("wf-1", now))
    with pytest.raises(ActiveWorkflowExists) as error:
        await repo.create_workflow(workflow("wf-2", now))
    assert error.value.workflow_id == "wf-1"

    await repo.release_active("wf-1")
    await repo.create_workflow(workflow("wf-2", now))
    assert await repo.active_workflow_id() == "wf-2"
    assert sorted(await repo.workflow_ids()) == ["wf-1", "wf-2"]


async def test_saving_a_stale_workflow_is_refused(table: Table) -> None:
    repo = Repository(table)
    now = datetime(2026, 11, 7, tzinfo=UTC)
    await repo.create_workflow(workflow("wf-1", now))
    loaded = await repo.workflow("wf-1")
    assert loaded is not None
    saved = await repo.save_workflow(loaded.model_copy(update={"current_stage": 1}))
    assert saved.version == loaded.version + 1
    with pytest.raises(VersionConflict):
        await repo.save_workflow(loaded)


async def test_lease_belongs_to_one_owner_until_it_expires(table: Table) -> None:
    repo = Repository(table)
    now = datetime(2026, 11, 7, tzinfo=UTC)
    assert await repo.acquire_lease("wf", "a", now=now, ttl_seconds=15)
    assert not await repo.acquire_lease("wf", "b", now=now, ttl_seconds=15)
    assert await repo.acquire_lease("wf", "a", now=now, ttl_seconds=15)
    later = now + timedelta(seconds=16)
    assert await repo.acquire_lease("wf", "b", now=later, ttl_seconds=15)
    assert not await repo.acquire_lease("wf", "a", now=later, ttl_seconds=15)


async def test_an_oauth_request_can_be_consumed_once(table: Table) -> None:
    repo = Repository(table)
    request = OAuthRequest(
        code_verifier="v",
        browser_binding="b",
        return_to="/",
        expires_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    await repo.save_oauth_request("state", request)
    assert await repo.consume_oauth_request("state") == request
    assert await repo.consume_oauth_request("state") is None
    assert await repo.consume_oauth_request("missing") is None
