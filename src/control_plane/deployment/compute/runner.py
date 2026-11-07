"""Run the COMPUTE preparation steps on a registered on-prem node, using local Docker."""

import argparse
import hashlib
import http.client
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def docker(*args, timeout=30):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Docker errors can include part of the deployment configuration.
        raise RuntimeError(f"Docker {args[0]} failed. Inspect the on-prem node before retrying.")
    return result.stdout.strip()


def load_binding(path):
    binding = json.loads(path.read_text(encoding="utf-8-sig"))
    required = {
        "targetRef",
        "imageId",
        "configVersion",
        "envFile",
        "command",
        "port",
        "readinessPath",
    }
    optional = {"sourceRef", "workloadScopeRef", "bindingRef", "dockerNetwork"}
    if not isinstance(binding, dict) or not required <= binding.keys() <= required | optional:
        raise ValueError("Use the registered target binding fields from binding.example.json")
    if "dockerNetwork" in binding and (
        not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", binding["dockerNetwork"])
        or binding["dockerNetwork"] in {"host", "none"}
    ):
        raise ValueError("Use a private bridge network")
    if not re.fullmatch(r"resource:[a-z0-9][a-z0-9-]{0,62}", binding["targetRef"]):
        raise ValueError("Invalid targetRef")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", binding["imageId"]):
        raise ValueError("Expected a full Docker image ID from the source")
    if not binding["configVersion"] or not isinstance(binding["configVersion"], str):
        raise ValueError("Pin an immutable configuration version")
    if not isinstance(binding["command"], list) or not all(
        isinstance(v, str) and v for v in binding["command"]
    ):
        raise ValueError("command must be an argument list")
    if type(binding["port"]) is not int or not 1 <= binding["port"] <= 65535:
        raise ValueError("Invalid app port")
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]*", binding["readinessPath"]):
        raise ValueError("Invalid readiness path")
    env_file = (path.parent / binding["envFile"]).resolve(strict=True)
    if not env_file.is_file():
        raise ValueError("Missing registered environment file")
    return binding, env_file


def observe_web(container, binding):
    mapping = docker("port", container, f"{binding['port']}/tcp")
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", mapping)
    if not match:
        raise RuntimeError("Expected a node-local probe port")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        conn = http.client.HTTPConnection("127.0.0.1", int(match[1]), timeout=2)
        try:
            conn.request("GET", binding["readinessPath"])
            if conn.getresponse().status == 200:
                return int(match[1])
        except (OSError, http.client.HTTPException):
            pass
        finally:
            conn.close()
        time.sleep(1)
    raise RuntimeError("Web readiness timed out. Container retained for inspection.")


def execute(action, path, target_ref, *, expected_revision=None, observe_only=False):
    if action not in ("PREPARE", "START_SYNC"):
        raise ValueError("Unsupported COMPUTE action")
    binding, env_file = load_binding(path)
    if binding["targetRef"] != target_ref:
        raise ValueError("Plan targetRef differs from the registered VM binding")
    revision = binding_revision(binding)
    if expected_revision and expected_revision != revision:
        raise ValueError("Registered configuration changed after plan creation")
    image = binding["imageId"]
    if docker("image", "inspect", "--format", "{{.Id}}", image) != image:
        raise ValueError("Source image is missing on the target")
    result = {"targetRef": target_ref, "imageId": image, "configVersion": binding["configVersion"]}
    if action == "PREPARE":
        return {**result, "check": "local-image-and-config-present"}

    # configVersion covers secret versions. Keep the contents of envFile off the wire.
    name = "foxtrot-stage-" + target_ref.removeprefix("resource:")
    existing = docker("ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$")
    if existing:
        owner = docker(
            "inspect", "--format", '{{index .Config.Labels "foxtrot.stage.revision"}}', existing
        )
        if owner != revision or docker("inspect", "--format", "{{.Image}}", existing) != image:
            raise RuntimeError("Target already has another container. Reconcile the deployment.")
        container = existing
    else:
        if observe_only:
            raise RuntimeError("Expected staged container has not been observed")
        network = ["--network", binding["dockerNetwork"]] if "dockerNetwork" in binding else []
        container = docker(
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            f"foxtrot.stage.revision={revision}",
            "--env-file",
            str(env_file),
            "--publish",
            f"127.0.0.1::{binding['port']}",
            *network,
            image,
            *binding["command"],
        )
    state = docker("inspect", "--format", "{{.State.Status}}", container)
    if state == "created" and not observe_only:
        docker("start", container)
    elif state != "running":
        raise RuntimeError(f"Container is {state}. Inspect it before retrying.")
    port = observe_web(container, binding)
    return {
        **result,
        "containerId": container,
        "probePort": port,
        "check": "target-web-probe-passed",
    }


def binding_revision(binding):
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["PREPARE", "START_SYNC"])
    parser.add_argument("--target-ref", required=True)
    parser.add_argument(
        "--binding", required=True, type=Path, help="Operator-owned local binding file"
    )
    args = parser.parse_args()
    try:
        print(json.dumps(execute(args.action, args.binding, args.target_ref)))
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(
            str(exc)
            if not isinstance(exc, subprocess.TimeoutExpired)
            else "Docker timed out. Reconcile the target.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
