"""Materialize an operator-owned secret bundle in the platform container."""

import copy
import json
import os
import re
from pathlib import Path

from .spec import require


def install(config, raw):
    require(len(raw.encode()) <= 65536, "on-prem bundle exceeds the secret size limit")
    try:
        bundle = json.loads(raw)
    except ValueError:
        raise ValueError("invalid on-prem bundle JSON") from None
    require(
        type(bundle) is dict
        and {"schemaVersion", "nodeId", "node", "catalog", "credentials"} <= set(bundle)
        and not (
            set(bundle) - {"schemaVersion", "nodeId", "node", "catalog", "credentials", "images"}
        )
        and bundle["schemaVersion"] == 1,
        "invalid on-prem bundle",
    )
    require(
        type(bundle["nodeId"]) is str and re.fullmatch(r"[a-z][a-z0-9-]{1,62}", bundle["nodeId"]),
        "invalid bundled node ID",
    )
    require(
        type(bundle["node"]) is dict and type(bundle["catalog"]) is dict,
        "bundled node and catalog must be objects",
    )
    images = bundle.get("images", {})
    require(
        type(images) is dict and not (set(images) - {"chiikawa-groupbuy"}),
        "invalid bundled application images",
    )
    for value in images.values():
        require(
            type(value) is str
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,511}", value)
            and any(
                isinstance(prefix, str)
                and prefix.endswith(("/", ":", "@"))
                and value.startswith(prefix)
                for prefix in bundle["node"].get("imagePrefixes", [])
            ),
            "bundled application image must be allowed on this node",
        )
    root = Path(config["workRoot"]) / "onprem-registration"
    require(
        not any(p.is_symlink() for p in (root, *root.parents)),
        "invalid on-prem registration directory",
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_root = root / "secrets"
    secret_root.mkdir(mode=0o700, exist_ok=True)
    require(not secret_root.is_symlink(), "invalid credential directory")
    require(type(bundle["credentials"]) is dict, "bundle credentials must be a map")
    for name, value in bundle["credentials"].items():
        require(
            type(name) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", name),
            "invalid bundled credential name",
        )
        require(type(value) is str and value and "\0" not in value, "invalid bundled credential")
    node = copy.deepcopy(bundle["node"])
    for key, name in node.get("secretFiles", {}).items():
        require(name in bundle["credentials"], "missing app credential")
        node["secretFiles"][key] = str(secret_root / name)

    def write(path, value):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)

    for name, value in bundle["credentials"].items():
        write(secret_root / name, value)
    write(root / "catalog.json", json.dumps(bundle["catalog"]))
    result = copy.deepcopy(config)
    result["onprem"] = {
        "catalogFile": str(root / "catalog.json"),
        "secretRoot": str(secret_root),
        "allowLocal": False,
    }
    result.setdefault("clouds", {}).setdefault("onprem", {}).setdefault("nodes", {})[
        bundle["nodeId"]
    ] = node
    # Older bundles keep the operator's existing image map; new bundles carry the fixed image.
    result["clouds"]["onprem"].setdefault("images", {}).update(images)
    return result
