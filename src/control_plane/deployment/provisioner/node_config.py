"""Resolve operator-registered Docker nodes and their prepared data services."""

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .inventory import private_address
from .spec import require


def private_file(path):
    require(isinstance(path, str) and path, "node credential file is unavailable")
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "node credential file is unavailable")
    require(path.stat().st_mode & 0o077 == 0, "node credential file must be private (0600)")
    require(path.stat().st_size <= 65536, "node credential file is too large")
    return path.read_text()


def registered(nodes, node_id, org):
    require(type(node_id) is str and node_id in nodes, "target.nodeId must name a registered node")
    node = copy.deepcopy(nodes[node_id])
    require(org in node.get("orgs", []), "on-prem node is not registered for this application")
    return node


def resolve(nodes, spec, preparations=None, *, require_ready=True):
    node = registered(nodes, spec["node_id"], spec["org_id"])
    target_secrets = {}
    prep_id = spec.get("preparation_id")
    if node.get("preparation"):
        require(preparations is not None and prep_id, "prepare this node before deploying the app")
        env = preparations._get(prep_id)
        require(
            env["request"]["org"] == spec["org_id"], "preparation belongs to another application"
        )
        expected = node["preparation"]
        require(
            all(env["request"].get(k) == v for k, v in expected.items()),
            "preparation does not match the registered node",
        )
        require(
            not require_ready
            or (
                env["status"] in {"INFRA_READY", "READY", "APP_NOT_READY"}
                and env["readiness"]["infrastructure"]["status"] == "READY"
            ),
            "on-prem data services are not ready",
        )
        preparations._busy(prep_id)
        require(
            preparations.catalog.resolve(env["request"]) == env["resolved"],
            "prepared inventory changed; reconcile before deployment",
        )
        host = next(h for h in env["resolved"]["inventory"]["hosts"] if "app" in h["roles"])
        require(
            host.get("connection", "ssh") == "ssh",
            "LOCAL_ONLY preparation cannot be deployed remotely",
        )
        material = preparations.catalog.secrets(env["resolved"])
        node.update(
            dockerHost=f"ssh://{host['sshUser']}@{host['address']}:{host['sshPort']}",
            sshHostKey=host["sshHostKey"],
            sshKey=material[host["sshKeyRef"]].decode(),
            runtimeRoot=host["runtimeRoot"] + "/applications",
            network=prep_id + "_default",
            bindAddress=host["serviceAddress"],
            preparationHandoff=env["handoff"],
        )
        node.setdefault("appUrl", "http://" + host["serviceAddress"])
        # Inject the destination app identity, never its administrator credentials.
        refs = env["resolved"]["profile"]["secrets"]
        target_secrets = {
            "STORAGE_SEAWEEDFS_ACCESS_KEY_ID": material[refs["object_app_key"]].decode().strip(),
            "STORAGE_SEAWEEDFS_SECRET_ACCESS_KEY": material[refs["object_app_secret"]]
            .decode()
            .strip(),
            "STORAGE_SEAWEEDFS_ENDPOINT": env["handoff"]["storage"]["url"],
        }
    else:
        require(prep_id is None, "this node does not use an infrastructure preparation")
        node["sshKey"] = private_file(node.get("sshKeyFile", ""))
    address = urlsplit(node.get("dockerHost", ""))
    require(
        address.scheme == "ssh"
        and address.username
        and not address.password
        and not address.query
        and not address.fragment
        and address.path in ("", "/"),
        "node requires an SSH endpoint",
    )
    require(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", address.username), "invalid SSH user")
    private_address(address.hostname)
    require(1 <= (address.port or 22) <= 65535, "invalid SSH port")
    require(
        re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", node.get("sshHostKey", "")),
        "node requires a verified SSH host key",
    )
    private_address(node.get("bindAddress"))
    root = node.get("runtimeRoot", "")
    require(
        type(root) is str
        and root.startswith(("/home/", "/srv/"))
        and ".." not in Path(root).parts
        and re.fullmatch(r"/[A-Za-z0-9_./ -]+", root),
        "invalid node runtimeRoot",
    )
    require(
        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", node.get("network", "")),
        "node requires a registered Docker network",
    )
    require(
        type(node.get("imagePrefixes")) is list and node["imagePrefixes"],
        "node image allowlist is required",
    )
    for prefix in node["imagePrefixes"]:
        require(
            type(prefix) is str and prefix.endswith(("/", ":", "@")),
            "imagePrefixes must end at a registry path, tag or digest boundary",
        )
    secrets = {}
    for key, reference in node.get("secretEnv", {}).items():
        require(bool(os.environ.get(reference)), f"node secret {reference} is not configured")
        secrets[key] = os.environ[reference]
    for key, path in node.get("secretFiles", {}).items():
        secrets[key] = private_file(path).rstrip("\r\n")
    require(
        not set(secrets).intersection(target_secrets),
        "destination storage credentials come from the preparation",
    )
    node["secrets"] = {**secrets, **target_secrets}
    require(
        type(node.get("expectedServedBy")) is str and node["expectedServedBy"],
        "node expectedServedBy is required for app identity checks",
    )
    require(
        type(node.get("dnsServers", [])) is list and len(node.get("dnsServers", [])) <= 3,
        "dnsServers must contain up to three private resolver addresses",
    )
    for address in node.get("dnsServers", []):
        private_address(address)
    deadline = node.get("readinessDeadlineSeconds", 180)
    require(type(deadline) in (int, float) and 0 <= deadline <= 900, "invalid readiness deadline")
    for check in node.get("networkChecks", []):
        require(
            type(check) is dict and set(check) == {"name", "host", "port"}, "invalid network check"
        )
        require(
            type(check["name"]) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", check["name"]),
            "invalid check name",
        )
        require(
            type(check["host"]) is str and re.fullmatch(r"[a-zA-Z0-9_.-]+", check["host"]),
            "invalid check host",
        )
        require(type(check["port"]) is int and 1 <= check["port"] <= 65535, "invalid check port")
    return node


def fingerprint(node):
    return hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()
