"""Loopback-only AI playground. No deployment worker, migrations, probes, or cloud SDK calls."""

from __future__ import annotations

from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from control_plane.adapters.client import AdapterClient
from control_plane.ai.api import router as ai_router
from control_plane.api.dependencies import CurrentSession
from control_plane.auth import AuthService
from control_plane.clock import SystemClock
from control_plane.config import Settings
from control_plane.dbgate import UnavailableDbGate
from control_plane.main import assemble, create_api
from control_plane.probes import HttpAppProber
from control_plane.seed import load_seed
from control_plane.store.repository import Repository
from control_plane.store.table import MemoryTable
from control_plane.traffic import TrafficService


def create_app() -> FastAPI:
    settings = Settings(
        environment="local", host="127.0.0.1", auth_disabled=True, dynamodb_endpoint="memory"
    )
    path = Path(settings.seed_path)
    has_seed = path.exists()
    bundled = Path(__file__).with_name("seed.example.yaml")
    example = (
        bundled
        if bundled.exists()
        else Path(__file__).resolve().parents[3] / "config/seed.example.yaml"
    )
    seed = load_seed(path if has_seed else example)
    clock, repo, http = SystemClock(), Repository(MemoryTable()), httpx.AsyncClient(timeout=3)
    platform = assemble(
        settings=settings,
        seed=seed,
        repo=repo,
        clock=clock,
        adapters={kind: AdapterClient(http, "unused-local-token") for kind in seed.managed},
        gate=UnavailableDbGate(),
        traffic=TrafficService(http, token="unused-local-token"),
        prober=HttpAppProber(http, seed, clock),
        auth=AuthService(
            repo,
            clock,
            http,
            client_id="",
            client_secret="",
            callback_url="",
            allowed_github_ids=[],
            session_hours=8,
        ),
        workload_token="unused-local-token",
    )
    platform.clients.append(http)
    app = create_api(platform, run_loops=False)
    # The playground exposes only AI and API docs, so unrelated control-plane actions cannot run.
    app.router.routes[:] = [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith(("/docs", "/openapi.json", "/health"))
    ]
    # Re-add only the AI router after removing the platform's included routers.
    app.include_router(ai_router)
    assets = Path(__file__).with_name("playground")

    @app.get("/", include_in_schema=False)
    async def playground() -> FileResponse:
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/playground/context", include_in_schema=False)
    async def playground_context(session: CurrentSession) -> dict[str, object]:
        assert platform.ai_service is not None
        scopes = await platform.ai_service.infra.deployments(str(session.user.id))
        return {
            "applicationId": seed.application,
            "environments": sorted({scope.cloud for scope in scopes}),
            "configurationSource": "USER_DEPLOYMENTS",
            "storage": "MEMORY",
        }

    app.mount("/playground", StaticFiles(directory=assets), name="ai-playground")
    return app


def run() -> None:
    uvicorn.run("control_plane.ai.local:create_app", factory=True, host="127.0.0.1", port=8011)
