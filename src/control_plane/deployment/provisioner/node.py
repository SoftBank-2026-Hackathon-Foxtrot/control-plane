"""Deploy application containers to a server-registered on-prem Docker node."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .terraform import TerraformError


class NodeEffectUnconfirmed(TerraformError):
    """A Docker mutation may have reached the registered node."""


class DeploymentExecutor:
    def __init__(self, terraform, nodes):
        self.terraform, self.nodes = terraform, nodes

    def __getattr__(self, name):
        return getattr(self.terraform, name)

    def docker(self, node, *args):
        host = node["dockerHost"]
        if not host.startswith("ssh://"):
            raise TerraformError("on-prem nodes require a registered SSH Docker endpoint")
        try:
            result = subprocess.run(
                ["docker", *args],
                env={**os.environ, "DOCKER_HOST": host},
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            raise NodeEffectUnconfirmed(
                "node operation outcome is unknown; reconcile before retrying"
            ) from error
        if result.returncode:
            error_type = NodeEffectUnconfirmed if args[0] in {"run", "rm"} else TerraformError
            raise error_type(f"on-prem Docker {args[0]} failed; inspect the node")
        return result.stdout.strip()

    def plan(self, env_id, cloud, tfvars, *, destroy=False):
        if cloud != "onprem":
            return self.terraform.plan(env_id, cloud, tfvars, destroy=destroy)
        directory = self.terraform.work_root / env_id
        directory.mkdir(parents=True, exist_ok=True)
        name = "destroy.tfplan" if destroy else "reviewed.tfplan"
        (directory / name).write_text(json.dumps({"spec": tfvars, "destroy": destroy}))
        return {
            "plan": name,
            "changes": {
                "delete" if destroy else "create": 1 + tfvars["app"].get("worker_count", 2)
            },
        }

    def apply(self, env_id, cloud, plan):
        if cloud != "onprem":
            return self.terraform.apply(env_id, cloud, plan)
        directory = self.terraform.work_root / env_id
        saved = json.loads((directory / plan).read_text())
        spec = saved["spec"]
        node = self.nodes[spec["node_id"]]
        names = [f"{spec['deployment_id']}-api"] + [
            f"{spec['deployment_id']}-worker-{n + 1}"
            for n in range(spec["app"].get("worker_count", 2))
        ]
        if saved["destroy"]:
            for name in names:
                found = self.docker(
                    node, "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"
                )
                if found == name:
                    self.docker(node, "rm", "-f", name)
            return None
        self.docker(node, "pull", spec["app"]["image"])
        self.docker(node, "network", "inspect", node["network"])
        reserved = set(node.get("environment", {})) | set(node.get("secretEnv", {}))
        for field in ("env", "api_env", "worker_env"):
            if reserved.intersection(spec["app"].get(field, {})):
                raise TerraformError("application variables cannot override registered node settings")
        base = {**node.get("environment", {}), **spec["app"].get("env", {})}
        for key, reference in node.get("secretEnv", {}).items():
            if not os.environ.get(reference):
                raise TerraformError(
                    f"node secret environment variable {reference} is not configured"
                )
            base[key] = os.environ[reference]
        if spec["app"].get("secret_env"):
            raise TerraformError("on-prem secrets must be registered on the node configuration")
        created = []
        try:
            for index, name in enumerate(names):
                existing = self.docker(
                    node, "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"
                )
                if existing:
                    raise TerraformError(
                        "container already exists; reconcile the deployment before retrying"
                    )
                values = {**base, **spec["app"].get("api_env" if index == 0 else "worker_env", {})}
                if any("\n" in str(value) or "\r" in str(value) for value in values.values()):
                    raise TerraformError("multiline node environment values are not supported")
                fd, path = tempfile.mkstemp(prefix="foxtrot-node-")
                try:
                    with os.fdopen(fd, "w") as env_file:
                        env_file.write("\n".join(f"{key}={value}" for key, value in values.items()))
                    app = spec["app"]
                    command = [
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--restart",
                        "unless-stopped",
                        "--network",
                        node["network"],
                        "--label",
                        f"foxtrot.deployment={env_id}",
                        "--env-file",
                        path,
                    ]
                    if index == 0:
                        command += [
                            "-p",
                            f"{node.get('bindAddress', '127.0.0.1')}:{app['port']}:{app['port']}",
                        ]
                        url = f"http://127.0.0.1:{app['port']}{app.get('health_path', '/readyz')}"
                        health = "import urllib.request as u; u.urlopen(" + repr(url) + ", timeout=2)"
                        url = f"http://127.0.0.1:{app['port']}{app.get('health_path', '/readyz')}"
                        health = "import urllib.request as u; u.urlopen(" + repr(url) + ", timeout=2)"
                        command += [
                            "--health-cmd",
                            shlex.join(["python", "-c", health]),
                            "--health-interval",
                            "5s",
                            "--health-start-period",
                            "60s",
                            "--health-retries",
                            "12",
                        ]
                    command += [
                        "--entrypoint",
                        app["entrypoint"][0],
                        app["image"],
                        *app["entrypoint"][1:],
                        "api" if index == 0 else f"w{index}",
                    ]
                    self.docker(node, *command)
                    created.append(name)
                finally:
                    Path(path).unlink(missing_ok=True)
            address = urlsplit(node["appUrl"])
            if address.scheme not in {"http", "https"} or not address.hostname:
                raise TerraformError("registered node appUrl must be an HTTP URL")
            if address.port is None:
                host = address.hostname
                if ":" in host:
                    host = f"[{host}]"
                address = address._replace(netloc=f"{host}:{spec['app']['port']}")
            handoff = {
                "cloud": "onprem",
                "compute": {
                    "runtime": "ON_PREM",
                    "node_id": spec["node_id"],
                    "id": env_id,
                    "containers": names,
                },
                "app": {
                    "url": urlunsplit(address).rstrip("/"),
                    "image": spec["app"]["image"],
                },
            }
            ready = self.check(handoff)
            if ready["status"] != "READY":
                raise TerraformError("on-prem application did not become healthy")
            return handoff
        except Exception as error:
            if created:
                raise NodeEffectUnconfirmed(
                    "partial node deployment requires reconciliation"
                ) from error
            raise

    def check(self, handoff):
        from .readiness import now

        info = handoff["compute"]
        node = self.nodes[info["node_id"]]
        end = time.monotonic() + 180
        while True:
            rows = json.loads(self.docker(node, "inspect", *info["containers"]))
            states = [row["State"] for row in rows]
            ready = (
                len(states) == len(info["containers"])
                and all(state.get("Running") for state in states)
                and states[0].get("Health", {}).get("Status") == "healthy"
            )
            if ready:
                return {
                    "status": "READY",
                    "observedAt": now(),
                    "lastSuccessAt": now(),
                    "method": "DOCKER_HEALTHCHECK",
                }
            if (
                any(state.get("Status") in {"exited", "dead"} for state in states)
                or time.monotonic() >= end
            ):
                return {
                    "status": "UNKNOWN",
                    "observedAt": now(),
                    "reason": "node app is not healthy",
                }
            time.sleep(2)
