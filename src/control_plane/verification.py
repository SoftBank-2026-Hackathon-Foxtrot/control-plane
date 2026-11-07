"""Read-only checks before a workflow may use an application."""

from __future__ import annotations

from typing import Any

from control_plane.applications import Application, ApplicationService, Verification
from control_plane.domain.models import ActorState, ResourceKind
from control_plane.errors import ApiError
from control_plane.workflows import WorkflowService
from control_plane.workload.coordinator import WorkloadCoordinator


async def verify_application(
    service: ApplicationService,
    app: Application,
    clients: dict[ResourceKind, Any],
    prober: Any,
    image_available: bool,
) -> Verification:
    checks = {"configuration": app.configuration is not None, "imageAccess": image_available}
    if app.configuration:
        try:
            await WorkflowService(
                service.repo, app.configuration, service.clock, clients
            )._check_capabilities()
            checks["adapterProfiles"] = True
        except ApiError:
            checks["adapterProfiles"] = False
        for name, env in app.configuration.environments.items():
            checks[f"environment:{name}"] = await prober.is_ready(env.app_url)
        coordinator = WorkloadCoordinator(service.repo, app.configuration, service.clock)
        hold = await coordinator.hold()
        reached, _ = await coordinator.all_reached(hold.generation, ActorState.RUNNING)
        checks["workloadActors"] = reached
    passed = all(checks.values())
    updated = await service.save(
        app.model_copy(
            update={
                "status": "VERIFIED" if passed else "REGISTERED",
                "verified_at": service.clock.now() if passed else None,
            }
        )
    )
    return Verification(
        application_id=app.id,
        version=updated.version,
        passed=passed,
        checks=checks,
        blockers=[name for name, value in checks.items() if not value],
    )
