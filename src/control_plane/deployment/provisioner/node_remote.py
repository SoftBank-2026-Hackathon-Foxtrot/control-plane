"""Fixed, stdlib-only SSH payload. JSON arrives on stdin; never print Docker stderr or env."""

import errno
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path


class NodeFailure(Exception):
    pass


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise NodeFailure("DOCKER_OPERATION_FAILED")
    return result.stdout.strip()


def write_private(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def records(project):
    ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project).split()
    return json.loads(docker("inspect", *ids)) if ids else []


def checked_records(project, environment):
    rows = records(project)
    if any(
        row["Config"].get("Labels", {}).get("foxtrot.deployment") != environment for row in rows
    ):
        raise NodeFailure("UNOWNED_CONTAINERS")
    return rows


def check_address(address, port):
    with socket.socket() as probe:
        try:
            probe.bind((address, port))
            return
        except OSError as error:
            if error.errno != errno.EACCES or port >= 1024:
                raise NodeFailure("APP_ADDRESS_UNAVAILABLE") from error
        # Docker publishes privileged ports; the SSH account need not be root.
        # Check that the address belongs to this host and no listener already uses it.
        try:
            probe.bind((address, 0))
            probe.settimeout(3)
            if probe.connect_ex((address, port)) != errno.ECONNREFUSED:
                raise NodeFailure("APP_ADDRESS_UNAVAILABLE")
        except OSError as error:
            raise NodeFailure("APP_ADDRESS_UNAVAILABLE") from error


def inspect(rows, services):
    actual = {row["Config"]["Labels"].get("com.docker.compose.service"): row for row in rows}
    complete = set(actual) == set(services) and len(rows) == len(actual)
    running = complete and all(row["State"].get("Running") for row in rows)
    matching = complete and all(
        actual[name]["Config"]["Image"] == spec["image"] for name, spec in services.items()
    )
    healthy = (
        running
        and matching
        and any(name.startswith("worker-") for name in actual)
        and actual["api"]["State"].get("Health", {}).get("Status") == "healthy"
    )
    return {
        "complete": complete,
        "ready": bool(healthy),
        "services": {
            name: {
                "running": row["State"].get("Running", False),
                "health": row["State"].get("Health", {}).get("Status", "not-configured"),
            }
            for name, row in actual.items()
        },
    }


def handle(payload):
    action, env_id = payload["action"], payload["environmentId"]
    if action not in {"preflight", "apply", "inspect", "destroy"} or not re.fullmatch(
        r"env-[a-f0-9]{12}", env_id
    ):
        raise NodeFailure("INVALID_OPERATION")
    root = Path(payload["runtimeRoot"])
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not str(root).startswith(("/home/", "/srv/"))
    ):
        raise NodeFailure("INVALID_RUNTIME_ROOT")
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise NodeFailure("INVALID_RUNTIME_ROOT")
    folder = root / env_id
    owner = folder / "owner.json"
    config = folder / "compose.json"
    if any(path.is_symlink() for path in (folder, owner, config)):
        raise NodeFailure("INVALID_OWNERSHIP_PATH")
    saved = json.loads(owner.read_text()) if owner.exists() else None
    if saved and saved.get("environmentId") != env_id:
        raise NodeFailure("OWNER_MISMATCH")
    if not saved and folder.exists() and list(folder.iterdir()):
        raise NodeFailure("UNOWNED_RUNTIME_DIRECTORY")
    project = "foxtrot-" + env_id
    rows = checked_records(project, env_id)
    if rows and not saved:
        raise NodeFailure("UNOWNED_CONTAINERS")
    if action == "inspect" and not rows and (not saved or saved.get("released")):
        return {"released": True, "ready": False, "complete": False}
    if action == "destroy":
        if saved:
            write_private(owner, {**saved, "destroying": True})
            if not config.exists():
                if rows:
                    raise NodeFailure("MISSING_COMPOSE_CONFIGURATION")
            else:
                docker("compose", "-p", project, "-f", str(config), "down", "--remove-orphans")
            if checked_records(project, env_id):
                raise NodeFailure("CONTAINERS_REMAIN")
            config.unlink(missing_ok=True)
            write_private(owner, {**saved, "released": True})
        return {"released": True}
    compose = payload["compose"]
    if saved and (saved.get("digest") != payload["digest"] or saved.get("released")):
        raise NodeFailure("REGISTERED_CONFIGURATION_CHANGED")
    docker("compose", "version", "--short")
    docker("network", "inspect", payload["network"])
    if action == "inspect" and saved and not rows and not saved.get("destroying"):
        return {"ownedEmpty": True, "ready": False, "complete": False}
    if action == "preflight":
        if not rows:
            check_address(payload["bindAddress"], payload["port"])
        for check in payload.get("networkChecks", []):
            try:
                with socket.create_connection((check["host"], check["port"]), timeout=3):
                    pass
            except OSError as error:
                raise NodeFailure("NETWORK_CHECK_FAILED:" + check["name"]) from error
        return {"ready": True, "scope": "HOST_TO_REGISTERED_ENDPOINTS"}
    if action == "apply":
        if payload.get("retryOwnedEmpty"):
            if not saved or saved.get("destroying") or rows:
                raise NodeFailure("RETRY_REQUIRES_EMPTY_OWNED_PROJECT")
            check_address(payload["bindAddress"], payload["port"])
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        if saved is None:
            fd = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"environmentId": env_id, "digest": payload["digest"]}, stream)
        write_private(config, compose)
        docker("compose", "-p", project, "-f", str(config), "pull")
        docker("compose", "-p", project, "-f", str(config), "up", "-d", "--no-build")
        rows = checked_records(project, env_id)
    result = inspect(rows, compose["services"])
    if result["ready"] and payload.get("networkChecks"):
        api = next(
            row
            for row in rows
            if row["Config"]["Labels"].get("com.docker.compose.service") == "api"
        )
        probe = (
            "import socket,sys; "
            "c=socket.create_connection((sys.argv[1],int(sys.argv[2])),3); c.close()"
        )
        for check in payload["networkChecks"]:
            try:
                docker("exec", api["Id"], "python", "-c", probe, check["host"], str(check["port"]))
            except NodeFailure:
                result.update(ready=False, networkFailure=check["name"])
                break
        result["networkScope"] = "APP_CONTAINER_TO_REGISTERED_ENDPOINTS"
    return result


if __name__ == "__main__":
    try:
        result = handle(json.load(sys.stdin))
    except (NodeFailure, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        # Do not echo command arguments, environment values or remote stderr.
        result = {
            "error": str(error) if isinstance(error, NodeFailure) else "REMOTE_OPERATION_FAILED"
        }
    print(json.dumps(result))
