"""Opt-in checks against the agents' actual input models and Redis HTTP translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from control_plane.adapters.client import AdapterClient
from control_plane.domain.models import ResourceKind
from tests.conftest import World
from tests.test_fixed_proxy import fixed_seed

pytestmark = pytest.mark.integration


async def test_binding_and_migration_payloads_use_the_real_agent_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = pytest.importorskip("db_operator.models")
    redis = pytest.importorskip("statemig.bindings")
    common = pytest.importorskip("statemig.common")
    world = World(seed=fixed_seed())
    for kind, binding_type, migration_type in (
        (ResourceKind.DATABASE, db.CreateBinding, db.DirectMigration),
        (ResourceKind.REDIS, redis.BindingReq, common.CreateReq),
    ):
        adapter = world.adapters[kind]
        register, create = adapter._register_binding, adapter._create

        def checked_register(
            body: dict[str, Any],
            model: Any = binding_type,
            delegate: Any = register,
        ) -> tuple[int, dict[str, Any]]:
            model.model_validate(body)
            return delegate(body)

        def checked_create(
            body: dict[str, Any],
            model: Any = migration_type,
            delegate: Any = create,
        ) -> tuple[int, dict[str, Any]]:
            model.model_validate(body)
            return delegate(body)

        monkeypatch.setattr(adapter, "_register_binding", checked_register)
        monkeypatch.setattr(adapter, "_create", checked_create)
    await world.workflows.bindings.initialize()
    await world.workflows.bindings.initialize()
    workflow = await world.start()
    final = await world.run_to_end(workflow.id)
    assert final.status.value == "COMPLETED", final.halt


async def test_redis_actual_http_preserves_management_id_and_migration_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings_module = pytest.importorskip("statemig.bindings")
    common = pytest.importorskip("statemig.common")
    migration = pytest.importorskip("statemig.migration")
    world = World(seed=fixed_seed())
    source, target = world.seed.resources.redis.source, world.seed.resources.redis.target
    assert source and target
    record = migration.ControlRecord(tmp_path / "control.json")

    class Admin:
        def __init__(self) -> None:
            self.state: dict[str, Any] = {
                "active": None,
                "consume": None,
                "paused": False,
                "gen": 0,
            }

        async def call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
            if path == "/bind":
                self.state.update(record.read())
            return {"proxy": dict(self.state)}

    proxy, sync = Admin(), Admin()
    endpoints: dict[str, Any] = {}
    addresses = {source.secret_ref: source.host, target.secret_ref: target.host}
    bindings = bindings_module.Bindings(
        tmp_path / "bindings.json",
        proxy,
        sync,
        record,
        endpoints,
        resolver=lambda reference: {"url": f"redis://{addresses[reference]}:6379/0"},
    )
    api = common.CommonApi(
        proxy,
        sync,
        record,
        endpoints,
        tmp_path / "migrations.json",
        lambda: None,
        lambda *args: None,
        bindings=bindings,
    )
    app = FastAPI()
    app.include_router(common.build_router(api))
    monkeypatch.setenv("STATEMIG_PROXY_PUBLIC_HOST", "redis-goods.internal")
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://redis-goods")
    client = AdapterClient(http, "token")
    world.services.adapters[ResourceKind.REDIS] = client
    try:
        await world.workflows.bindings.initialize()
        ready = await world.workflows.bindings.initialize()
        current = ready[ResourceKind.REDIS]
        assert current["bindingId"] != current["bindingRef"]
        assert current["source"]["host"] == source.host
        assert current["proxy"]["host"] == "redis-goods.internal"
        workflow = await world.start()
        resource = await world.repo.resource(workflow.id, ResourceKind.REDIS)
        assert resource.plan and resource.plan["bindingRef"] == current["bindingRef"]
        created = await client.create_migration(f"{workflow.id}:REDIS", resource.plan)
        assert created.lifecycle == "CREATED"
        reported = await client.binding(current["bindingId"])
        assert reported["lastMigrationId"] == created.migration_id
    finally:
        await http.aclose()
