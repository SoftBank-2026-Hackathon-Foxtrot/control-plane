"""Local API preview; no workflow workers. Run only on 127.0.0.1."""

from __future__ import annotations

import os

from fastapi import FastAPI

from control_plane.config import Settings
from control_plane.main import build_platform, create_api


def create_demo() -> FastAPI:
    if os.environ.get("CONTROL_PLANE_ENVIRONMENT") != "local":
        raise ValueError("this entry point requires explicit environment=local")
    return create_api(build_platform(Settings()), run_loops=False)
