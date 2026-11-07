#!/usr/bin/env python3
"""Generate private, single-PC configuration; no deployment or system package changes.

Local validation: python tools/setup_onprem.py --local
SSH/Tailscale: add --address, --ssh-user, --host-key (verified out of band), --uid, --gid.
Never puts credentials in stdout or overwrites existing configuration.
"""
from __future__ import annotations
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from control_plane.deployment.paths import iac_root

ROOT = iac_root()
from control_plane.deployment.provisioner.inventory import Inventory, request

IMAGES = {"postgres": "postgres:17-bookworm", "valkey": "valkey/valkey:8.1", "seaweedfs": "chrislusf/seaweedfs:4.48"}


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
    parser.add_argument("--host-key", help="verified ssh-ed25519 public host key; never blind ssh-keyscan")
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    parser.add_argument("--runtime-root")
    args = parser.parse_args()
    if not args.local and (not args.address or not args.host_key):
        parser.error("SSH mode requires --address and --host-key")
    address = "127.0.0.1" if args.local else str(ipaddress.IPv4Address(args.address))
    folder = args.output.resolve()
    if folder.exists():
        parser.error("output already exists; retain its credentials and choose a new output path")
    pinned = {}
    for service, image in IMAGES.items():
        # Pull explicitly before running this command. Digests are resolved from local Docker metadata.
        records = json.loads(command(["docker", "image", "inspect", image]))
        pinned[service] = records[0]["RepoDigests"][0]
    folder.mkdir(parents=True, mode=0o700)
    secret_dir = folder / "secrets"
    secret_dir.mkdir(mode=0o700)
    command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "foxtrot-onprem-controller", "-f", str(secret_dir / "ssh_key")])
    for name in ("pg_admin", "pg_schema", "pg_writer", "valkey_admin", "valkey_app", "object_admin_key",
                 "object_admin_secret", "object_app_key", "object_app_secret"):
        private(secret_dir / name, secrets.token_hex(24))
    # Self-signed CA/server certificate for this PoC endpoint. The controller verifies its IP SAN.
    command(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes", "-days", "90",
             "-keyout", str(secret_dir / "pg_server_key"), "-out", str(secret_dir / "pg_server_cert"),
             "-subj", "/CN=foxtrot-onprem", "-addext", f"subjectAltName=IP:{address}"])
    private(secret_dir / "pg_ca", (secret_dir / "pg_server_cert").read_text())
    for file in secret_dir.iterdir():
        file.chmod(0o600)
    public_key = " ".join((secret_dir / "ssh_key.pub").read_text().split()[:2])
    runtime = args.runtime_root or f"/home/{args.ssh_user}/.local/share/foxtrot-onprem"
    host = {"id": "onprem-pc", "address": address, "serviceAddress": address,
            "sshUser": args.ssh_user, "sshPort": 22,
            "sshHostKey": args.host_key or public_key,  # Unused in explicitly enabled local mode.
            "sshKeyRef": "secret:ssh", "roles": ["app", "database", "cache", "storage"],
            "runtimeRoot": runtime, "uid": args.uid, "gid": args.gid,
            "connection": "local" if args.local else "ssh"}
    names = [p.name for p in secret_dir.iterdir() if p.name not in ("ssh_key", "ssh_key.pub")]
    catalog = {
        "inventories": {"inventory:pc": {"orgs": ["foxtrot"], "siteRef": "site:personal-pc", "hosts": [host]}},
        "profiles": {"profile:single-pc": {"orgs": ["foxtrot"], "images": pinned,
                     "minCpu": 2, "minMemoryMiB": 4096, "minDiskGiB": 20, "databaseName": "appdb",
                     "networkChecks": [], "secrets": {n: f"secret:{n}" for n in names}}},
        "bindings": {}, "credentials": {**{f"secret:{n}": {"file": n} for n in names}, "secret:ssh": {"file": "ssh_key"}}}
    body = {"org": "foxtrot", "target": {"kind": "onprem", "siteRef": "site:personal-pc", "inventoryRef": "inventory:pc"},
            "driver": "ansible", "profileRef": "profile:single-pc", "mode": "plan"}
    config = {"workRoot": str(folder / "work"), "clouds": {}, "onprem": {"catalogFile": str(folder / "catalog.json"),
              "secretRoot": str(secret_dir), "allowLocal": args.local}}
    for filename, content in (("catalog.json", catalog), ("request.json", body), ("config.json", config)):
        private(folder / filename, json.dumps(content, indent=2) + "\n")
    Inventory(folder / "catalog.json", secret_dir, allow_local=args.local).resolve(request(body))
    print(f"Prepared {folder}; SSH public key: {secret_dir / 'ssh_key.pub'}")
    print("LOCAL_ONLY: loopback validation, no cloud reachability" if args.local else "SSH target registered; verify Tailscale ACLs before applying")


if __name__ == "__main__":
    main()
