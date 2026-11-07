"""Docker for local verification; ECS updates only server-approved services/templates."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import boto3
from botocore.config import Config

from control_plane.config import resolve_secret
from control_plane.runtime import RuntimeRecord


class DockerDriver:
    async def command(self, *args: str, environment: dict[str, str] | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(environment or {})},
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 120)
        except BaseException:
            process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError("Docker operation failed; inspect the local Docker daemon")
        return stdout.decode().strip()

    async def previous(self, record: RuntimeRecord) -> str | None:
        if not record.target.host_port:
            raise ValueError("local target requires a server-approved host port")
        return None  # Local preparation creates a new container; it never replaces an existing one.

    async def start(self, record: RuntimeRecord) -> str:
        await self.command("pull", record.image.image_ref)
        raw = await self.command("image", "inspect", record.image.image_ref)
        image = json.loads(raw)[0]
        architecture = {"X86_64": "amd64", "ARM64": "arm64"}[record.image.architecture]
        if image["Architecture"] != architecture:
            raise ValueError("image architecture does not match the application")
        environment = dict(record.image.environment)
        for key, ref in record.image.secret_refs.items():
            environment[key] = resolve_secret(ref)
        environment["FOXTROT_IMAGE_REF"] = record.image.image_ref
        args = [
            "run",
            "-d",
            "--name",
            f"foxtrot-{record.id}",
            "--label",
            f"foxtrot.operation={record.id}",
            "--memory",
            f"{record.image.memory}m",
            "--cpus",
            str(record.image.cpu / 1024),
            "-p",
            f"127.0.0.1:{record.target.host_port}:{record.image.port}",
        ]
        for key in environment:
            args += ["-e", key]
        return await self.command(*args, record.image.image_ref, environment=environment)

    async def ready(self, record: RuntimeRecord) -> bool:
        if not record.result_ref:
            return False
        raw = await self.command("inspect", record.result_ref)
        container = json.loads(raw)[0]
        return bool(
            container["State"]["Running"]
            and container["Config"]["Image"] == record.image.image_ref
            and container["Config"]["Labels"].get("foxtrot.operation") == record.id
        )

    async def discover(self, record: RuntimeRecord) -> str | None:
        result = await self.command("ps", "-aq", "--filter", f"label=foxtrot.operation={record.id}")
        return result.splitlines()[0] if result else None

    async def restore(self, record: RuntimeRecord) -> None:
        if record.result_ref:
            raw = json.loads(await self.command("inspect", record.result_ref))[0]
            if raw["Config"]["Labels"].get("foxtrot.operation") != record.id:
                raise ValueError("container ownership changed")
            await self.command("rm", "-f", record.result_ref)

    async def restored(self, record: RuntimeRecord) -> bool:
        return await self.discover(record) is None


class EcsDriver:
    def __init__(self, region: str, client: Any = None) -> None:
        self.client = client or boto3.client(
            "ecs",
            region_name=region,
            config=Config(
                connect_timeout=3,
                read_timeout=10,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )

    async def call(self, name: str, **kwargs: Any) -> Any:
        return await asyncio.to_thread(getattr(self.client, name), **kwargs)

    async def service(self, record: RuntimeRecord) -> dict[str, Any]:
        result = await self.call(
            "describe_services", cluster=record.target.cluster, services=[record.target.service]
        )
        if result.get("failures") or len(result["services"]) != 1:
            raise ValueError("approved ECS service was not found")
        service: dict[str, Any] = result["services"][0]
        if service.get("deploymentController", {}).get("type", "ECS") != "ECS":
            raise ValueError("only the ECS rolling deployment controller is supported")
        if service["desiredCount"] < 1:
            raise ValueError("approved ECS service requires a positive desired count")
        return service

    async def previous(self, record: RuntimeRecord) -> str:
        return str((await self.service(record))["taskDefinition"])

    async def start(self, record: RuntimeRecord) -> str:
        described = await self.call(
            "describe_task_definition", taskDefinition=record.target.task_definition
        )
        template = described["taskDefinition"]
        allowed = {
            "family",
            "taskRoleArn",
            "executionRoleArn",
            "networkMode",
            "containerDefinitions",
            "volumes",
            "placementConstraints",
            "requiresCompatibilities",
            "cpu",
            "memory",
            "pidMode",
            "ipcMode",
            "proxyConfiguration",
            "inferenceAccelerators",
            "ephemeralStorage",
            "runtimePlatform",
        }
        request = {key: copy.deepcopy(value) for key, value in template.items() if key in allowed}
        containers = [
            c for c in request["containerDefinitions"] if c["name"] == record.target.container
        ]
        if len(containers) != 1:
            raise ValueError("approved application container was not found")
        container = containers[0]
        container["image"] = record.image.image_ref
        env = {item["name"]: item["value"] for item in container.get("environment", [])}
        env.update(record.image.environment)
        env["FOXTROT_IMAGE_REF"] = record.image.image_ref
        container["environment"] = [{"name": k, "value": v} for k, v in env.items()]
        refs = {item["name"]: item["valueFrom"] for item in container.get("secrets", [])}
        refs.update(record.image.secret_refs)
        if any(not value.startswith("arn:") for value in refs.values()):
            raise ValueError("ECS secrets must reference approved AWS ARNs")
        container["secrets"] = [{"name": k, "valueFrom": v} for k, v in refs.items()]
        if not any(
            p["containerPort"] == record.image.port for p in container.get("portMappings", [])
        ):
            raise ValueError("application port differs from the approved ECS service port")
        if record.image.registry_credential_ref:
            if not record.image.registry_credential_ref.startswith("arn:"):
                raise ValueError("ECS registry credentials require a Secrets Manager ARN")
            container["repositoryCredentials"] = {
                "credentialsParameter": record.image.registry_credential_ref,
            }
        request["cpu"], request["memory"] = str(record.image.cpu), str(record.image.memory)
        request["runtimePlatform"] = {
            "cpuArchitecture": record.image.architecture,
            "operatingSystemFamily": "LINUX",
        }
        request["tags"] = [{"key": "foxtrot-operation", "value": record.id}]
        created = await self.call("register_task_definition", **request)
        ref = str(created["taskDefinition"]["taskDefinitionArn"])
        # A lost response leaves UNKNOWN. The engine never silently issues a second deployment.
        await self.call(
            "update_service",
            cluster=record.target.cluster,
            service=record.target.service,
            taskDefinition=ref,
        )
        return ref

    async def ready(self, record: RuntimeRecord) -> bool:
        service = await self.service(record)
        return bool(
            record.result_ref
            and service["taskDefinition"] == record.result_ref
            and service["runningCount"] == service["desiredCount"]
            and service.get("pendingCount", 0) == 0
            and len(service.get("deployments", [])) == 1
            and all(d.get("rolloutState") == "COMPLETED" for d in service["deployments"])
        )

    async def discover(self, record: RuntimeRecord) -> str | None:
        service = await self.service(record)
        ref = str(service["taskDefinition"])
        described = await self.call(
            "describe_task_definition", taskDefinition=ref, include=["TAGS"]
        )
        tags = {item["key"]: item["value"] for item in described.get("tags", [])}
        return ref if tags.get("foxtrot-operation") == record.id else None

    async def restore(self, record: RuntimeRecord) -> None:
        service = await self.service(record)
        if (
            not record.result_ref
            or service["taskDefinition"] != record.result_ref
            or not record.previous_ref
        ):
            raise ValueError("ECS service changed; reconcile before restoring")
        await self.call(
            "update_service",
            cluster=record.target.cluster,
            service=record.target.service,
            taskDefinition=record.previous_ref,
        )

    async def restored(self, record: RuntimeRecord) -> bool:
        service = await self.service(record)
        return bool(
            service["taskDefinition"] == record.previous_ref
            and service["runningCount"] == service["desiredCount"]
            and not service.get("pendingCount", 0)
            and len(service.get("deployments", [])) == 1
            and all(d.get("rolloutState") == "COMPLETED" for d in service["deployments"])
        )
