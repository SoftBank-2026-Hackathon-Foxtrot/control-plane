"""Builds the process: real clients from settings, the API, and the two background loops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from control_plane.adapters.client import AdapterClient
from control_plane.api.dependencies import ApiContext
from control_plane.api.routes import router
from control_plane.auth import AuthService
from control_plane.clock import Clock, SystemClock
from control_plane.config import Settings, resolve_secret
from control_plane.dbgate import DbGate, SdkDbGate, UnavailableDbGate
from control_plane.domain.models import ResourceKind
from control_plane.engine.context import Services
from control_plane.engine.engine import Engine
from control_plane.errors import ApiError
from control_plane.probes import AppProber, HttpAppProber, ProbeLoop
from control_plane.seed import Seed, load_seed
from control_plane.store.repository import Repository
from control_plane.store.table import DynamoTable, MemoryTable, Table
from control_plane.traffic import TrafficController, TrafficService
from control_plane.workflows import WorkflowService
from control_plane.workload.coordinator import WorkloadCoordinator

log = logging.getLogger(__name__)


@dataclass
class Platform:
    """Everything one process needs. Tests build it with fakes at the edges."""

    context: ApiContext
    engine: Engine
    probe_loop: ProbeLoop
    clients: list[httpx.AsyncClient] = field(default_factory=list)


def assemble(
    *,
    settings: Settings,
    seed: Seed,
    repo: Repository,
    clock: Clock,
    adapters: dict[ResourceKind, AdapterClient],
    gate: DbGate,
    traffic: TrafficController,
    prober: AppProber,
    auth: AuthService,
    workload_token: str,
) -> Platform:
    coordinator = WorkloadCoordinator(repo, seed, clock)
    services = Services(
        repo=repo,
        clock=clock,
        seed=seed,
        adapters=adapters,
        coordinator=coordinator,
        gate=gate,
        traffic=traffic,
        prober=prober,
    )
    return Platform(
        context=ApiContext(
            settings=settings,
            seed=seed,
            repo=repo,
            clock=clock,
            auth=auth,
            workflows=WorkflowService(repo, seed, clock, adapters),
            coordinator=coordinator,
            workload_token=workload_token,
        ),
        engine=Engine(services),
        probe_loop=ProbeLoop(repo, seed, prober, clock),
    )


def build_platform(settings: Settings) -> Platform:
    seed = load_seed(settings.seed_path)
    _check_production(settings, seed)
    clock = SystemClock()

    def secret(reference: str) -> str:
        return resolve_secret(reference, region=settings.region)

    clients: list[httpx.AsyncClient] = []

    def client(**kwargs: object) -> httpx.AsyncClient:
        made = httpx.AsyncClient(timeout=10, **kwargs)  # type: ignore[arg-type]
        clients.append(made)
        return made

    table: Table = (
        MemoryTable()
        if settings.dynamodb_endpoint == "memory"
        else DynamoTable(
            settings.table_name,
            region=settings.region,
            endpoint=settings.dynamodb_endpoint,
        )
    )
    adapters = {
        kind: AdapterClient(
            client(base_url=endpoint.url, verify=endpoint.verify),
            secret(endpoint.token_secret),
        )
        for kind in ResourceKind
        for endpoint in [seed.adapters.of(kind)]
    }
    gate: DbGate = UnavailableDbGate()
    if settings.db_gate_table:
        gate = SdkDbGate(
            settings.db_gate_table,
            namespace=settings.db_gate_namespace,
            region=settings.region,
            endpoint=None if settings.dynamodb_endpoint == "memory" else settings.dynamodb_endpoint,
        )
    traffic = TrafficService(
        client(base_url=settings.traffic_url),
        token=secret(settings.traffic_token_secret),
    )
    repo = Repository(table)
    auth = AuthService(
        repo,
        clock,
        client(),
        client_id=settings.github_client_id,
        client_secret="" if settings.auth_disabled else secret(settings.github_client_secret_ref),
        callback_url=f"{settings.public_url}/api/auth/github/callback",
        allowed_github_ids=settings.github_allowed_ids,
        session_hours=settings.session_hours,
    )
    platform = assemble(
        settings=settings,
        seed=seed,
        repo=repo,
        clock=clock,
        adapters=adapters,
        gate=gate,
        traffic=traffic,
        prober=HttpAppProber(client(), seed, clock),
        auth=auth,
        workload_token=secret(settings.workload_token_secret),
    )
    platform.clients = clients
    return platform


def _check_production(settings: Settings, seed: Seed) -> None:
    if settings.environment != "production":
        return
    for kind in ResourceKind:
        if seed.adapters.of(kind).verify is False:
            raise ValueError(f"{kind.value}: TLS verification cannot be off in production")


def create_api(platform: Platform, *, run_loops: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks = (
            [
                asyncio.create_task(platform.engine.run()),
                asyncio.create_task(platform.probe_loop.run()),
            ]
            if run_loops
            else []
        )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(client.aclose() for client in platform.clients))

    # API documents are for development; production answers only to the console.
    docs = platform.context.settings.environment == "local"
    app = FastAPI(
        title="Foxtrot control plane",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.context = platform.context
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def api_error(_: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            {"code": error.code, "message": error.message, "blockers": error.blockers},
            status_code=error.status,
        )

    return app


def create_app() -> FastAPI:
    return create_api(build_platform(Settings()))


def run() -> None:
    settings = Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        "control_plane.main:create_app", factory=True, host=settings.host, port=settings.port
    )


if __name__ == "__main__":
    run()
