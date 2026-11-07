"""Migration limits are checked against a saved application, not browser claims."""

from __future__ import annotations

from control_plane.applications import Application, ApplicationService, MigrationPolicy
from control_plane.errors import ApiError
from control_plane.seed import Seed


def direction(seed: Seed) -> str:
    names = list(seed.environments)
    source = seed.source_environment or names[0]
    target = seed.target_environment or names[1]
    return f"{source}->{target}"


def enforce(app: Application, source: str, target: str) -> Seed:
    if app.configuration is None:
        raise ApiError(409, "RESOURCES_NOT_CONNECTED", "connect approved resources first")
    requested = f"{source}->{target}"
    # Resource refs describe one direction. Swapping environment names cannot reverse those refs.
    allowed = app.policy.allowed_directions or [direction(app.configuration)]
    if requested != direction(app.configuration) or requested not in allowed:
        raise ApiError(
            422, "DIRECTION_DENIED", "this resource catalog does not allow that direction"
        )
    selected = app.configuration.model_copy(deep=True)
    for resource in (
        selected.resources.database,
        selected.resources.redis,
        selected.resources.object,
    ):
        resource.policy.max_quiesce_seconds = min(
            resource.policy.max_quiesce_seconds,
            app.policy.max_quiesce_seconds,
        )
    return selected


async def set_policy(
    service: ApplicationService,
    app: Application,
    policy: MigrationPolicy,
    version: int,
) -> Application:
    await service.idle(app)
    service.expect(app, version)
    if app.configuration is None:
        raise ApiError(409, "RESOURCES_NOT_CONNECTED", "connect approved resources first")
    if any(item != direction(app.configuration) for item in policy.allowed_directions):
        raise ApiError(
            422, "DIRECTION_DENIED", "reverse migration needs a separate approved catalog"
        )
    return await service.save(
        app.model_copy(
            update={
                "policy": policy,
                "status": "REGISTERED",
                "verified_at": None,
                "updated_at": service.clock.now(),
            }
        )
    )
