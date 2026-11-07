"""Server-approved resources. A personal workspace cannot select another owner's catalog."""

from __future__ import annotations

from control_plane.auth import LOCAL_USER
from control_plane.config import Settings
from control_plane.errors import ApiError
from control_plane.seed import Seed, load_seed


def catalog_for(settings: Settings, seed: Seed, owner: str) -> Seed:
    path = settings.personal_catalog_paths.get(owner)
    if path:
        return load_seed(path)
    seed_owner = LOCAL_USER.id if settings.auth_disabled else settings.seed_owner_id
    if owner == seed_owner:
        return seed.model_copy(deep=True)
    raise ApiError(403, "CATALOG_NOT_ASSIGNED", "no resource catalog assigned to this workspace")
