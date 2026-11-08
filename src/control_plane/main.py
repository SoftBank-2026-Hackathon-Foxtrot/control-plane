"""Builds the process: real clients from settings, the API, and the two background loops."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Protocol

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from control_plane.adapters.client import AdapterClient
from control_plane.ai.api import router as ai_router
from control_plane.ai.context import InfrastructureContext
from control_plane.ai.model import OpenAIModel
from control_plane.ai.service import AIService
from control_plane.ai.store import AIStore
from control_plane.ai.telemetry import TelemetryReader
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
from control_plane.monitoring_api import router as monitoring_router
from control_plane.observations import ObservationLoop
from control_plane.otel_ingest import router as otel_router
from control_plane.probes import AppProber, HttpAppProber, ProbeLoop
from control_plane.public_probe import PublicProbeLoop
from control_plane.seed import Seed, load_seed
from control_plane.store.repository import Repository
from control_plane.store.table import DynamoTable, MemoryTable, Table
from control_plane.system_metrics import router as system_metrics_router
from control_plane.telemetry import TraceMiddleware, get_telemetry
from control_plane.telemetry_query import router as telemetry_router
from control_plane.traffic import TrafficController, TrafficService
from control_plane.workflows import WorkflowService
from control_plane.workload.coordinator import WorkloadCoordinator

log = logging.getLogger(__name__)


class DeploymentService(Protocol):
    def recover(self) -> None: ...

    def run_forever(self, stop: threading.Event) -> None: ...


@dataclass
class Platform:
    """Everything one process needs. Tests build it with fakes at the edges."""

    context: ApiContext
    engine: Engine
    probe_loop: ProbeLoop
    observation_loop: ObservationLoop
    public_probe_loop: PublicProbeLoop | None = None
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    deployment_service: DeploymentService | None = None
    ai_service: AIService | None = None


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
    observation_token: str = "",
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
            observation_token=observation_token,
        ),
        engine=Engine(services),
        probe_loop=ProbeLoop(repo, seed, prober, clock),
        observation_loop=ObservationLoop(repo, adapters, clock, coordinator),
    )


def build_platform(settings: Settings) -> Platform:
    seed = load_seed(settings.seed_path)
    _check_production(settings, seed)
    clock = SystemClock()

    def secret(reference: str) -> str:
        return resolve_secret(reference, region=settings.region)

    clients: list[httpx.AsyncClient] = []

    def client(**kwargs: object) -> httpx.AsyncClient:
        async def propagate(request: httpx.Request) -> None:
            request.headers.update(get_telemetry("control-plane").headers())

        made = httpx.AsyncClient(timeout=10, event_hooks={"request": [propagate]}, **kwargs)  # type: ignore[arg-type]
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
        for kind in seed.managed
        for endpoint in [seed.adapter(kind)]
    }
    gate: DbGate = UnavailableDbGate()
    if settings.db_gate_table:
        gate = SdkDbGate(
            settings.db_gate_table,
            namespace=(seed.resources.database and seed.resources.database.gate_namespace)
            or settings.db_gate_namespace,
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
        observation_token=secret(settings.observation_token_secret)
        if settings.observation_token_secret
        else "",
    )
    unknown = set(settings.public_probe_served_by.values()) - set(seed.environments)
    if unknown:
        raise ValueError("public probe mapping contains unregistered environments")
    platform.public_probe_loop = PublicProbeLoop(
        repo, client(), clock, settings.public_probe_url, settings.public_probe_served_by
    )
    if os.environ.get("PROVISIONER_CONFIG") or os.environ.get("PROVISIONER_CONFIG_JSON"):
        from control_plane.deployment.provisioner.__main__ import build, load

        catalog, config = load(None)
        config["applicationId"] = seed.application
        platform.deployment_service = build(config, catalog)
    platform.clients = clients
    return platform


def _check_production(settings: Settings, seed: Seed) -> None:
    if settings.environment != "production":
        return
    for kind in seed.managed:
        if seed.adapter(kind).verify is False:
            raise ValueError(f"{kind.value}: TLS verification cannot be off in production")


def create_api(platform: Platform, *, run_loops: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        telemetry = get_telemetry("control-plane")
        deploy_stop = threading.Event()
        deploy_worker = None
        if run_loops and platform.deployment_service is not None:
            worker_service = platform.deployment_service
            worker_service.recover()
            deploy_worker = threading.Thread(
                target=worker_service.run_forever, args=(deploy_stop,), daemon=True
            )
            deploy_worker.start()
        tasks = (
            [
                asyncio.create_task(platform.engine.run()),
                asyncio.create_task(platform.probe_loop.run()),
                asyncio.create_task(platform.observation_loop.run()),
            ]
            if run_loops
            else []
        )
        if run_loops and platform.public_probe_loop:
            tasks.append(asyncio.create_task(platform.public_probe_loop.run()))
        try:
            yield
        finally:
            deploy_stop.set()
            if deploy_worker is not None:
                await asyncio.to_thread(deploy_worker.join, 10)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(client.aclose() for client in platform.clients))
            await asyncio.to_thread(telemetry.close)

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
    app.state.deployment_service = platform.deployment_service
    platform.engine.services = replace(
        platform.engine.services, deployment=platform.deployment_service
    )
    if platform.ai_service is None:
        context = platform.context
        settings = context.settings
        key = settings.ai_api_key.get_secret_value() if settings.ai_api_key else None
        if settings.ai_api_key_secret:
            key = resolve_secret(settings.ai_api_key_secret, region=settings.region)
        http = httpx.AsyncClient(timeout=3, follow_redirects=False)
        platform.clients.append(http)
        platform.ai_service = AIService(
            InfrastructureContext(context, platform.deployment_service),
            AIStore(context.repo.table, context.clock, settings.ai_retention_days),
            OpenAIModel(
                key, settings.ai_model, settings.ai_timeout_seconds, settings.ai_max_output_tokens
            ),
            TelemetryReader(settings, http),
        )
    app.state.ai_service = platform.ai_service
    from control_plane.deployment.api import router as deployment_router
    from control_plane.deployment.onprem_api import router as onprem_router

    app.include_router(onprem_router)
    app.include_router(deployment_router)
    app.include_router(router)
    app.include_router(otel_router)
    app.include_router(telemetry_router)
    app.include_router(system_metrics_router)
    app.include_router(monitoring_router)
    from control_plane.security_review.api import router as security_review_router
    from control_plane.security_review.rehearsal_api import router as rehearsal_router

    app.include_router(security_review_router)
    app.include_router(ai_router)
    app.include_router(rehearsal_router)
    app.add_middleware(TraceMiddleware, telemetry=get_telemetry("control-plane"))

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
