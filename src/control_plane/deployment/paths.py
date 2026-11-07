"""Terraform assets shipped with the platform backend."""

import os
from pathlib import Path


def iac_root() -> Path:
    bundled = Path(__file__).with_name("infrastructure")
    source = Path(__file__).resolve().parents[3] / "infrastructure"
    return Path(
        os.environ.get("FOXTROT_INFRASTRUCTURE_ROOT", bundled if bundled.is_dir() else source)
    ).resolve()
