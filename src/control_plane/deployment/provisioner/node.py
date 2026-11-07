"""Platform-owned Compose deployments over pinned SSH, separate from data-service Compose."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import node_config
from .readiness import AppChecker, now
from .spec import require
from .terraform import TerraformError


class NodeEffectUnconfirmed(TerraformError):
    """The remote mutation may have completed; inspection must precede another action."""


class DeploymentExecutor:
    def __init__(self, terraform, nodes):
        self.terraform, self.nodes = terraform, nodes
        self.preparations = None

    def __getattr__(self, name):
        return getattr(self.terraform, name)

    def resolve(self, spec, *, require_ready=True):
        return node_config.resolve(self.nodes, spec, self.preparations, require_ready=require_ready)

    def remote(self, node, payload):
        address = urlsplit(node["dockerHost"])
        host, port = address.hostname, address.port or 22
        script = Path(__file__).with_name("node_remote.py").read_text()
        with tempfile.TemporaryDirectory(prefix="foxtrot-ssh-") as folder:
            key, known = Path(folder) / "identity", Path(folder) / "known_hosts"
            key.write_text(node["sshKey"])
            key.chmod(0o600)
            known.write_text(f"[{host}]:{port} {node['sshHostKey']}\n{host} {node['sshHostKey']}\n")
            known.chmod(0o600)
            command = [
                "ssh",
                "-T",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                "-i",
                str(key),
                "-p",
                str(port),
                f"{address.username}@{host}",
                shlex.join(["python3", "-c", script]),
            ]
            failure = (
                NodeEffectUnconfirmed
                if payload["action"] in {"apply", "destroy"}
                else TerraformError
            )
            try:
                result = subprocess.run(
                    command, input=json.dumps(payload), capture_output=True, text=True, timeout=420
                )
                if result.returncode:
                    raise failure("SSH operation failed; inspect the registered node")
                reply = json.loads(result.stdout)
                if reply.get("error"):
                    raise failure("Node check failed: " + str(reply["error"]))
                return reply
            except (subprocess.TimeoutExpired, OSError, ValueError) as error:
                raise failure(
                    "Node operation outcome unavailable; inspect before retrying"
                ) from error

    def compose(self, env_id, spec, node):
        app = spec["app"]
        defaults = {
            **node.get("environment", {}),
            **node.get("secrets", {}),
            "SERVED_BY": node["expectedServedBy"],
        }
        reserved = (
            set(defaults)
            | set(node.get("apiEnvironment", {}))
            | set(node.get("workerEnvironment", {}))
        )
        for field in ("env", "api_env", "worker_env"):
            require(
                not reserved.intersection(app.get(field, {})),
                "app variables cannot override registered node settings",
            )
        services = {}
        for index in range(app.get("worker_count", 2) + 1):
            name = "api" if index == 0 else f"worker-{index}"
            role = "api" if index == 0 else "worker"
            values = {
                **defaults,
                **app.get("env", {}),
                **app.get(role + "_env", {}),
                **node.get(role + "Environment", {}),
            }
            require(
                all(isinstance(v, str) and "\x00" not in v for v in values.values()),
                "node environment must contain strings",
            )
            service = {
                "image": app["image"],
                "entrypoint": app["entrypoint"],
                "command": ["api" if index == 0 else f"w{index}"],
                "hostname": f"{env_id}-{name}",
                "environment": values,
                "restart": "unless-stopped",
                "networks": ["runtime"],
                "labels": {"foxtrot.deployment": env_id},
                "cpus": app.get("cpu", 1),
                "mem_limit": str(app.get("memory_mib", 512)) + "m",
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            }
            if node.get("dnsServers"):
                service["dns"] = node["dnsServers"]
            if index == 0:
                service["ports"] = [f"{node['bindAddress']}:{app['port']}:{app['port']}"]
                url = f"http://127.0.0.1:{app['port']}{app.get('health_path', '/readyz')}"
                code = (
                    "import urllib.request as u; r=u.urlopen("
                    + repr(url)
                    + ",timeout=3); assert r.status==200"
                )
                service["healthcheck"] = {
                    "test": ["CMD", "python", "-c", code],
                    "interval": "5s",
                    "timeout": "4s",
                    "start_period": "60s",
                    "retries": 12,
                }
            services[name] = service

        def literal(value):
            # Compose interpolates dollar expressions even in JSON. Preserve credentials.
            if isinstance(value, str):
                return value.replace("$", "$$")
            if isinstance(value, list):
                return [literal(item) for item in value]
            if isinstance(value, dict):
                return {key: literal(item) for key, item in value.items()}
            return value

        return literal(
            {
                "name": "foxtrot-" + env_id,
                "services": services,
                "networks": {"runtime": {"external": True, "name": node["network"]}},
            }
        )

    def payload(self, env_id, spec, node, action):
        compose = self.compose(env_id, spec, node)
        return {
            "action": action,
            "environmentId": env_id,
            "runtimeRoot": node["runtimeRoot"],
            "network": node["network"],
            "bindAddress": node["bindAddress"],
            "port": spec["app"]["port"],
            "compose": compose,
            "digest": hashlib.sha256(json.dumps(compose, sort_keys=True).encode()).hexdigest(),
            "networkChecks": node.get("networkChecks", []),
        }

    def plan(self, env_id, cloud, tfvars, *, destroy=False):
        if cloud != "onprem":
            return self.terraform.plan(env_id, cloud, tfvars, destroy=destroy)
        node = self.resolve(tfvars, require_ready=not destroy)
        if not destroy:
            self.remote(node, self.payload(env_id, tfvars, node, "preflight"))
        directory = self.terraform.work_root / env_id
        directory.mkdir(parents=True, exist_ok=True)
        name = "destroy.tfplan" if destroy else "reviewed.tfplan"
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {"spec": tfvars, "destroy": destroy, "nodeDigest": node_config.fingerprint(node)},
                stream,
            )
        return {
            "plan": name,
            "changes": {
                "delete" if destroy else "create": 1 + tfvars["app"].get("worker_count", 2)
            },
            "scope": "APPLICATION_CONTAINERS",
            "dataRetention": "RETAIN",
        }

    def handoff(self, env_id, spec, node):
        address = urlsplit(node.get("appUrl", "http://" + node["bindAddress"]))
        require(
            address.scheme in {"http", "https"}
            and address.hostname
            and not address.username
            and not address.password
            and not address.query
            and not address.fragment,
            "registered appUrl must be an HTTP URL",
        )
        if address.port is None and address.scheme == "http":
            address = address._replace(netloc=f"{address.hostname}:{spec['app']['port']}")
        return {
            "cloud": "onprem",
            "compute": {
                "runtime": "ON_PREM",
                "node_id": spec["node_id"],
                "id": env_id,
                "private_ip": node["bindAddress"],
                "containers": [
                    "api",
                    *[f"worker-{n + 1}" for n in range(spec["app"].get("worker_count", 2))],
                ],
            },
            "app": {"url": urlunsplit(address).rstrip("/"), "image": spec["app"]["image"]},
            "preparationEnvironmentId": spec.get("preparation_id"),
            "resources": node.get("preparationHandoff"),
            "migrationReadiness": {
                "status": "NOT_EVALUATED",
                "pendingChecks": [
                    "adapter-registration",
                    "schema-and-writer-fence",
                    "workload-hold",
                    "replication-connectivity",
                    "traffic-origin",
                ],
            },
        }

    def apply(self, env_id, cloud, plan):
        if cloud != "onprem":
            return self.terraform.apply(env_id, cloud, plan)
        saved = json.loads((self.terraform.work_root / env_id / plan).read_text())
        spec, node = saved["spec"], self.resolve(saved["spec"], require_ready=not saved["destroy"])
        if saved["nodeDigest"] != node_config.fingerprint(node):
            raise TerraformError("Registered node changed after planning; create a fresh plan")
        action = "destroy" if saved["destroy"] else "apply"
        self.remote(node, self.payload(env_id, spec, node, action))
        return None if saved["destroy"] else self.handoff(env_id, spec, node)

    def reconcile(self, env_id, cloud, spec):
        require(cloud == "onprem", "only on-prem deployments support this reconciliation")
        node = self.resolve(spec, require_ready=False)
        result = self.remote(node, self.payload(env_id, spec, node, "inspect"))
        if result.get("released"):
            return None
        return self.handoff(env_id, spec, node)

    def check(self, handoff):
        env_id = handoff["compute"]["id"]
        saved = json.loads((self.terraform.work_root / env_id / "reviewed.tfplan").read_text())
        spec, node = saved["spec"], self.resolve(saved["spec"])
        end = time.monotonic() + node.get("readinessDeadlineSeconds", 180)
        while True:
            result = self.remote(node, self.payload(env_id, spec, node, "inspect"))
            if result.get("ready"):
                result = AppChecker({"deadlineSeconds": 10, "intervalSeconds": 1}).check(
                    {"compute": {"private_ip": node["bindAddress"]}},
                    spec["app"],
                    expected_identity=node["expectedServedBy"],
                )
                result["scope"] = "CONTAINERS_AND_PLATFORM_HTTP"
                return result
            if time.monotonic() >= end:
                return {
                    "status": "NOT_READY",
                    "observedAt": now(),
                    "reason": "App containers or registered network checks are not ready",
                    "details": result,
                }
            time.sleep(2)
