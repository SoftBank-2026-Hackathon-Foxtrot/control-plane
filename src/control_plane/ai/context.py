from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from control_plane.ai.allocation import allocation
from control_plane.api.dependencies import ApiContext
from control_plane.deployment.api import APP_TEMPLATE, org_id
from control_plane.deployment.paths import iac_root
from control_plane.deployment.tools.render_inputs import POLICY
from control_plane.errors import ApiError

DATA_SCOPE = "USER_DEPLOYED_APPLICATIONS_V2"


@dataclass(frozen=True)
class DeploymentScope:
    application: str
    environment_id: str
    deployment_id: str
    owner_id: str
    cloud: str
    status: str
    snapshot: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {
            "environmentId": self.environment_id,
            "deploymentId": self.deployment_id,
            "applicationId": self.application,
            "environment": self.cloud,
            "status": self.status,
            "appliedAt": self.snapshot.get("appliedAt"),
        }


class InfrastructureContext:
    """Only successful, owner-scoped application deployments. No platform seed or workflows."""

    def __init__(self, context: ApiContext, deployment: Any = None) -> None:
        self.context, self.deployment = context, deployment

    def application(self, application: str | None) -> str:
        if application and application != self.context.seed.application:
            raise ApiError(404, "NOT_FOUND", "application not found")
        return self.context.seed.application

    def environments(self, selected: list[str]) -> list[str]:
        if set(selected) - {"aws", "gcp", "onprem"}:
            raise ApiError(404, "NOT_FOUND", "environment not found")
        return sorted(set(selected))

    def _scope(self, record: dict[str, Any] | None, user: str) -> DeploymentScope | None:
        app = self.context.seed.application
        if (
            not record
            or record["org_id"] != org_id(user, app)
            or record["template"] != APP_TEMPLATE
        ):
            return None
        if record["status"] == "DESTROYED" or not record.get("applied_snapshot"):
            return None
        return DeploymentScope(
            app,
            record["id"],
            record["deployment_id"],
            record["org_id"],
            record["cloud"],
            record["status"],
            record["applied_snapshot"],
        )

    async def owned(self, user: str, identifier: str, *, ready: bool = False) -> DeploymentScope:
        record = (
            await asyncio.to_thread(self.deployment.store.environment, identifier)
            if self.deployment
            else None
        )
        scope = self._scope(record, user)
        if scope is None:
            raise ApiError(404, "NOT_FOUND", "deployed application not found")
        if ready and scope.status not in {"READY", "INFRA_READY", "APP_NOT_READY"}:
            raise ApiError(
                409,
                "DEPLOYMENT_STATE_UNCONFIRMED",
                "deployment changes must settle before analysis",
            )
        return scope

    async def deployments(self, user: str) -> list[DeploymentScope]:
        if not self.deployment:
            return []
        records = await asyncio.to_thread(
            self.deployment.store.environments_for_owner,
            org_id(user, self.context.seed.application),
            APP_TEMPLATE,
        )
        return [scope for record in records if (scope := self._scope(record, user)) is not None]

    @staticmethod
    def source(scope: DeploymentScope) -> dict[str, Any]:
        snapshot = scope.snapshot
        allowed = POLICY["clouds"].get(scope.cloud, {})
        sizing = {
            key: value for key, value in (snapshot.get("sizing") or {}).items() if key in allowed
        }
        app = {
            key: value
            for key, value in (snapshot.get("app") or {}).items()
            if key in {"cpu", "memory_mib", "worker_count", "api_workers", "port"}
        }
        resources = {}
        for kind in ("compute", "database", "cache", "storage", "network"):
            resource = (snapshot.get("resources") or {}).get(kind) or {}
            resources[kind] = {
                key: resource[key]
                for key in ("id", "host", "port", "name", "tls_required")
                if key in resource
            }
        return {
            "id": f"deployment-{scope.environment_id}",
            "type": "USER_APPLICATION_DEPLOYMENT",
            "observedAt": snapshot.get("appliedAt"),
            "data": {
                **scope.public(),
                "sizing": sizing,
                "app": app,
                "allocation": allocation(scope.cloud, app, bool(snapshot.get("hasTunnelContainer")))
                if scope.cloud in {"aws", "gcp"} and {"cpu", "memory_mib"} <= app.keys()
                else None,
                "resources": resources,
                "hasTunnelContainer": bool(snapshot.get("hasTunnelContainer")),
                "notice": "Last successful application apply; not a live cloud inventory.",
            },
        }

    async def collect(
        self, user: str, environments: list[str], deployment_id: str | None = None
    ) -> list[dict[str, Any]]:
        selected = self.environments(environments)
        scopes = (
            [await self.owned(user, deployment_id)]
            if deployment_id
            else await self.deployments(user)
        )
        scopes = [scope for scope in scopes if not selected or scope.cloud in selected]
        return [
            {
                "id": "my-application-inventory",
                "type": "USER_APPLICATION_INVENTORY",
                "observedAt": self.context.clock.now().isoformat(),
                "data": {
                    "applicationId": self.context.seed.application,
                    "deploymentCount": len(scopes),
                    "dataScope": DATA_SCOPE,
                    "notice": "Only this user's applied application deployments are available.",
                },
            },
            *(self.source(scope) for scope in scopes),
        ]

    def catalog(self, cloud: str) -> dict[str, Any]:
        if cloud not in POLICY["clouds"]:
            raise ApiError(422, "SIZING_NOT_SUPPORTED", "target sizing catalog is not configured")
        # Public reviewed sizing policy, never the platform's actual provisioner configuration.
        shipped = json.loads((iac_root() / "provisioner/config.example.json").read_text())
        return {
            "parameters": POLICY["clouds"][cloud],
            "baseline": shipped["clouds"][cloud]["baseline"]["sizing"],
            "baselineSource": "TARGET_CATALOG_BASELINE",
            "appAllocations": {
                "cpu": [0.25, 0.5, 1, 2, 4] if cloud == "aws" else [1, 2, 4],
                "memory_mib": "512 MiB units; Fargate/Cloud Run combination validator applies",
                "default": {"cpu": 1, "memory_mib": 2048, "worker_count": 2},
                "constraints": (
                    "Fargate CPU .25:512-2048; .5:1024-4096; 1:2048-8192; "
                    "2:4096-16384; 4:8192-30720 MiB. Except .25, use 1024 MiB units."
                    if cloud == "aws"
                    else "memory<=cpu*4096 MiB; (workers+1)*cpu plus tunnel<=8; "
                    "combined memory<=32768 MiB."
                ),
            },
        }
