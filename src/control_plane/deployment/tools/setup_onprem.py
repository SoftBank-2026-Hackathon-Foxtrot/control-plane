#!/usr/bin/env python3
"""Generate private, single-PC configuration; no deployment or system package changes.

Local validation: python -m control_plane.deployment.tools.setup_onprem --local
SSH/Tailscale: add --address, --ssh-user, --host-key (verified out of band), --uid, --gid.
Never puts credentials in stdout or overwrites existing configuration.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import ipaddress
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

from control_plane.deployment.paths import iac_root
from control_plane.deployment.provisioner.inventory import (
    ID,
    IMAGE,
    Inventory,
    private_address,
    request,
)

ROOT = iac_root()

IMAGES = {
    "postgres": "postgres:17-bookworm",
    "valkey": "valkey/valkey:8.1",
    "seaweedfs": "chrislusf/seaweedfs:4.48",
}


def command(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed; check installed tools and access")
    return result.stdout.strip()


def private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".onprem-local")
    parser.add_argument("--address")
    parser.add_argument("--ssh-user", default=getpass.getuser())
    parser.add_argument(
        "--host-key", help="verified ssh-ed25519 public host key; never blind ssh-keyscan"
    )
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    parser.add_argument("--runtime-root")
    parser.add_argument(
        "--org",
        default="foxtrot",
        help="orgId returned by the application's deployment-targets API",
    )
    parser.add_argument("--node-id", default="personal-pc")
    parser.add_argument(
        "--app-config",
        type=Path,
        help="operator node JSON: imagePrefixes, environment, role settings and checks",
    )
    parser.add_argument(
        "--app-image",
        help=(
            "server-owned chiikawa-groupbuy image; "
            "required with --app-config unless already configured"
        ),
    )
    parser.add_argument(
        "--app-secrets",
        type=Path,
        help="private JSON mapping app environment names to secret values",
    )
    parser.add_argument(
        "--images-file",
        type=Path,
        help="pinned postgres/valkey/seaweedfs image map; avoids controller Docker dependency",
    )
    parser.add_argument(
        "--platform-config", type=Path, help="merge with the existing platform configuration"
    )
    args = parser.parse_args()
    if not args.local and (not args.address or not args.host_key):
        parser.error("SSH mode requires --address and --host-key")
    address = "127.0.0.1" if args.local else str(ipaddress.IPv4Address(args.address))
    folder = args.output.resolve()
    if folder.exists():
        parser.error("output already exists; retain its credentials and choose a new output path")
    if args.app_config and args.local:
        parser.error(
            "platform app deployment requires SSH/Tailscale; "
            "--local is infrastructure validation only"
        )
    if not args.local:
        private_address(address)
    if not ID.fullmatch(args.node_id):
        parser.error("invalid --node-id")
    if args.app_secrets and not args.app_config:
        parser.error("--app-secrets requires --app-config")
    if args.app_image and not args.app_config:
        parser.error("--app-image requires --app-config")
    if args.app_secrets and (
        args.app_secrets.is_symlink() or args.app_secrets.stat().st_mode & 0o077
    ):
        parser.error("--app-secrets must be a private regular file (0600)")
    values = json.loads(args.app_secrets.read_text()) if args.app_secrets else {}
    if type(values) is not dict or any(
        not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
        or not isinstance(value, str)
        or not value
        or "\0" in value
        for key, value in values.items()
    ):
        parser.error("invalid application secrets")
    node = json.loads(args.app_config.read_text()) if args.app_config else {}
    if args.app_config and (type(node) is not dict or not node.get("imagePrefixes")):
        parser.error("--app-config requires an imagePrefixes allowlist")
    config = (
        json.loads(args.platform_config.read_text())
        if args.platform_config
        else {"workRoot": str(folder / "work"), "clouds": {}}
    )
    app_image = args.app_image or config.get("clouds", {}).get("onprem", {}).get("images", {}).get(
        "chiikawa-groupbuy"
    )
    if args.app_config:
        prefixes = node["imagePrefixes"]
        if (
            not isinstance(app_image, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,511}", app_image)
            or not isinstance(prefixes, list)
            or not prefixes
            or any(not isinstance(p, str) or not p.endswith(("/", ":", "@")) for p in prefixes)
            or not any(app_image.startswith(p) for p in prefixes)
        ):
            parser.error("configure --app-image within the node's imagePrefixes allowlist")
    pinned = json.loads(args.images_file.read_text()) if args.images_file else {}
    for service, image in ({} if args.images_file else IMAGES).items():
        # Pull first. Resolve image digests from local Docker metadata.
        records = json.loads(command(["docker", "image", "inspect", image]))
        pinned[service] = records[0]["RepoDigests"][0]
    if (
        type(pinned) is not dict
        or set(pinned) != set(IMAGES)
        or any(type(value) is not str or not IMAGE.fullmatch(value) for value in pinned.values())
    ):
        parser.error(
            "--images-file requires three postgres/valkey/seaweedfs sha256 image references"
        )
    folder.mkdir(parents=True, mode=0o700)
    secret_dir = folder / "secrets"
    secret_dir.mkdir(mode=0o700)
    command(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "foxtrot-onprem-controller",
            "-f",
            str(secret_dir / "ssh_key"),
        ]
    )
    for name in (
        "pg_admin",
        "pg_schema",
        "pg_writer",
        "valkey_admin",
        "valkey_app",
        "object_admin_key",
        "object_admin_secret",
        "object_app_key",
        "object_app_secret",
    ):
        private(secret_dir / name, secrets.token_hex(24))
    # Self-signed CA/server certificate for this PoC endpoint. The controller verifies its IP SAN.
    command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-sha256",
            "-nodes",
            "-days",
            "90",
            "-keyout",
            str(secret_dir / "pg_server_key"),
            "-out",
            str(secret_dir / "pg_server_cert"),
            "-subj",
            "/CN=foxtrot-onprem",
            "-addext",
            f"subjectAltName=IP:{address}",
        ]
    )
    private(secret_dir / "pg_ca", (secret_dir / "pg_server_cert").read_text())
    for file in secret_dir.iterdir():
        file.chmod(0o600)
    public_key = " ".join((secret_dir / "ssh_key.pub").read_text().split()[:2])
    runtime = args.runtime_root or f"/home/{args.ssh_user}/.local/share/foxtrot-onprem"
    host = {
        "id": "onprem-pc",
        "address": address,
        "serviceAddress": address,
        "sshUser": args.ssh_user,
        "sshPort": 22,
        "sshHostKey": args.host_key or public_key,  # Unused in explicitly enabled local mode.
        "sshKeyRef": "secret:ssh",
        "roles": ["app", "database", "cache", "storage"],
        "runtimeRoot": runtime,
        "uid": args.uid,
        "gid": args.gid,
        "connection": "local" if args.local else "ssh",
    }
    names = [p.name for p in secret_dir.iterdir() if p.name not in ("ssh_key", "ssh_key.pub")]
    catalog = {
        "inventories": {
            "inventory:pc": {"orgs": [args.org], "siteRef": "site:personal-pc", "hosts": [host]}
        },
        "profiles": {
            "profile:single-pc": {
                "orgs": [args.org],
                "images": pinned,
                "minCpu": 2,
                "minMemoryMiB": 4096,
                "minDiskGiB": 20,
                "databaseName": "appdb",
                "networkChecks": [],
                "secrets": {n: f"secret:{n}" for n in names},
            }
        },
        "bindings": {},
        "credentials": {
            **{f"secret:{n}": {"file": n} for n in names},
            "secret:ssh": {"file": "ssh_key"},
        },
    }
    body = {
        "org": args.org,
        "target": {"kind": "onprem", "siteRef": "site:personal-pc", "inventoryRef": "inventory:pc"},
        "driver": "ansible",
        "profileRef": "profile:single-pc",
        "mode": "plan",
    }
    config["onprem"] = {
        "catalogFile": str(folder / "catalog.json"),
        "secretRoot": str(secret_dir),
        "allowLocal": args.local,
    }
    if args.app_config:
        node["orgs"] = [args.org]
        node["preparation"] = {k: body[k] for k in ("target", "profileRef")}
        node.setdefault("expectedServedBy", "onprem")
        node.setdefault("imagePrefixes", [])
        node["secretFiles"] = {}
        for key, value in values.items():
            name = "app_" + key
            private(secret_dir / name, value)
            node["secretFiles"][key] = str(secret_dir / name)
        config.setdefault("clouds", {}).setdefault("onprem", {}).setdefault("nodes", {})[
            args.node_id
        ] = node
        config["clouds"]["onprem"].setdefault("images", {})["chiikawa-groupbuy"] = app_image
        bundled = copy.deepcopy(node)
        bundled["secretFiles"] = {key: Path(path).name for key, path in node["secretFiles"].items()}
        private(
            folder / "platform-bundle.json",
            json.dumps(
                {
                    "schemaVersion": 1,
                    "nodeId": args.node_id,
                    "node": bundled,
                    "images": {"chiikawa-groupbuy": app_image},
                    "catalog": catalog,
                    "credentials": {
                        p.name: p.read_text()
                        for p in secret_dir.iterdir()
                        if p.name != "ssh_key.pub"
                    },
                },
                indent=2,
            )
            + "\n",
        )
        if (folder / "platform-bundle.json").stat().st_size > 65536:
            parser.error("platform bundle exceeds the 64 KiB secret limit")
    for filename, content in (
        ("catalog.json", catalog),
        ("request.json", body),
        ("config.json", config),
    ):
        private(folder / filename, json.dumps(content, indent=2) + "\n")
    Inventory(folder / "catalog.json", secret_dir, allow_local=args.local).resolve(request(body))
    print(f"Prepared {folder}; SSH public key: {secret_dir / 'ssh_key.pub'}")
    print(
        "LOCAL_ONLY: loopback validation, no cloud reachability"
        if args.local
        else "SSH target registered; verify Tailscale ACLs before applying"
    )


if __name__ == "__main__":
    main()
