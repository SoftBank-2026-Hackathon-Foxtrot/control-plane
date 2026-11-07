import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)

from control_plane.clock import stamp
from control_plane.domain.models import ResourceKind, ResourceState
from control_plane.main import create_api
from control_plane.observations import ObservationStore
from tests.conftest import World


def batch(data, at, kind="DATABASE", module="database", error=None):
    message = ExportLogsServiceRequest()
    resource = message.resource_logs.add()
    for key, value in {"foxtrot.module": module, "foxtrot.contract.version": "1"}.items():
        attribute = resource.resource.attributes.add(key=key)
        attribute.value.string_value = value
    record = resource.scope_logs.add().log_records.add(time_unix_nano=int(at.timestamp() * 1e9))
    for key, value in {"foxtrot.event": "snapshot", "workflow.id": data["workflowId"]}.items():
        record.attributes.add(key=key).value.string_value = value
    record.body.string_value = json.dumps(
        {"schemaVersion": 1, "resource": kind, "data": data, "collectionError": error}
    )
    return message.SerializeToString()


async def deliver(
    world, data, *, kind="DATABASE", module="database", at=None, error=None, token="collector-token"
):
    world.platform.context = replace(world.platform.context, observation_token="collector-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_api(world.platform, run_loops=False)),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/internal/otlp/v1/logs",
            content=batch(data, at or world.clock.now(), kind, module, error),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-protobuf"},
        )


async def test_otlp_auth_identity_allowlist_and_delayed_delivery(world: World):
    workflow = await world.start()
    await world.repo.save_resource(
        workflow.id, ResourceState(kind=ResourceKind.DATABASE, migration_id="db")
    )
    data = {
        "workflowId": workflow.id,
        "migrationId": "db",
        "observed": {"observedAt": stamp(world.clock.now())},
        "progress": {"completed": 5},
        "dsn": "secret",
    }
    assert (await deliver(world, data, token="workload-token")).status_code == 401
    result = await deliver(world, data, module="redis")
    assert (
        ExportLogsServiceResponse.FromString(result.content).partial_success.rejected_log_records
        == 1
    )
    result = await deliver(world, data | {"migrationId": "wrong"})
    assert (
        ExportLogsServiceResponse.FromString(result.content).partial_success.rejected_log_records
        == 1
    )
    assert (await deliver(world, data)).status_code == 200
    store = ObservationStore(world.table)
    saved = await store.latest(workflow.id, "DATABASE")
    assert saved["source"] == "OTEL" and "secret" not in str(saved)
    world.clock.advance(5)
    await deliver(world, data | {"progress": {"completed": 10}})
    await deliver(world, data, at=world.clock.now() - timedelta(seconds=4))
    assert (await store.latest(workflow.id, "DATABASE"))["data"]["progress"]["completed"] == 10
    world.clock.advance(1)
    await deliver(world, data, error="password=secret")
    saved = await store.latest(workflow.id, "DATABASE")
    assert saved["data"]["progress"]["completed"] == 10
    assert saved["collectionError"] == "MODULE_OBSERVATION_UNAVAILABLE"


@pytest.mark.parametrize(
    "kind,module",
    [
        ("INFRA", "iac"),
        ("COMPUTE", "iac"),
        ("APP", "control-plane"),
        ("PUBLIC", "control-plane"),
        ("TRAFFIC", "traffic"),
    ],
)
async def test_all_module_contracts_and_future_rejection(world: World, kind, module):
    workflow = await world.start()
    data = {
        "workflowId": workflow.id,
        "observedAt": stamp(world.clock.now()),
        "observed": {"observedAt": stamp(world.clock.now())},
    }
    reply = await deliver(world, data, kind=kind, module=module)
    assert not ExportLogsServiceResponse.FromString(
        reply.content
    ).partial_success.rejected_log_records
    bad = await deliver(
        world, data, kind=kind, module=module, at=world.clock.now() + timedelta(minutes=1)
    )
    assert (
        ExportLogsServiceResponse.FromString(bad.content).partial_success.rejected_log_records == 1
    )
