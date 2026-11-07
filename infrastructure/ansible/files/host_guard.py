"""Read/claim/inspect a reserved runtime directory. Preserve unrelated workloads."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def docker(*args):
    proc = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=30)
    if proc.returncode:
        raise RuntimeError("Docker inspection failed")
    return proc.stdout.strip()


def containers():
    try:
        ids = docker("ps", "-aq")
    except FileNotFoundError:
        return []
    return json.loads(docker("inspect", *ids.split())) if ids else []


def run(args):
    root = Path(args.root)
    owner = root / "owner.json"
    expected = {"environmentId": args.environment, "hostId": args.host, "configDigest": args.digest}
    if owner.is_symlink() or root.is_symlink():
        raise RuntimeError("Ownership path is not a regular directory/file")
    saved = json.loads(owner.read_text()) if owner.exists() else None
    if saved and any(saved.get(k) != v for k, v in expected.items()):
        raise RuntimeError("Host ownership or pinned configuration differs")
    if args.action == "claim":
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if saved is None:
            # Atomic across controllers even when each controller has a separate journal.
            fd = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({**expected, "released": False}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        return {"claimed": True}
    all_records = containers()
    for container in all_records:
        labels = container["Config"].get("Labels", {})
        if labels.get("com.docker.compose.project") == args.environment and labels.get("foxtrot.environment") != args.environment:
            raise RuntimeError("Compose project contains an unowned workload")
    records = [c for c in all_records if c["Config"].get("Labels", {}).get("foxtrot.environment") == args.environment]
    env_root = root / args.environment
    if args.action == "preflight":
        if saved is None and env_root.exists():
            raise RuntimeError("Unowned environment directory already exists")
        return {"preflight": True, "claimed": saved is not None}
    if args.action == "release":
        if saved is None or records:
            raise RuntimeError("Stop owned containers before releasing")
        tmp = root / "owner.tmp"
        tmp.write_text(json.dumps({**expected, "released": True}))
        tmp.chmod(0o600)
        tmp.replace(owner)
        return {"released": True}
    if args.action == "inspect":
        if saved is None:
            return {"ready": False, "released": False, "configurationMatches": False}
        if saved.get("released"):
            return {"ready": False, "released": not records, "configurationMatches": True}
        config = json.loads((env_root / "host.json").read_text())
        actual = {c["Config"].get("Labels", {}).get("com.docker.compose.service"): c for c in records}
        healthy = set(actual) == set(config["services"]) and all(
            c["State"].get("Running") is True and c["State"].get("Health", {}).get("Status") == "healthy"
            and c["Config"]["Image"] == config["images"][name] for name, c in actual.items())
        if len(records) != len(actual):
            healthy = False
        if "postgres" in actual and healthy:
            # App tables are intentionally not inspected/created by infrastructure preparation.
            db = actual["postgres"]["Id"]
            version = docker("exec", db, "psql", "-U", "postgres", "-At", "-c", "SHOW server_version_num")
            healthy = 170000 <= int(version) < 180000
            logical = docker("exec", db, "psql", "-U", "postgres", "-At", "-c",
                             "SELECT current_setting('wal_level') = 'logical' AND "
                             "current_setting('max_replication_slots')::int > 0 AND "
                             "current_setting('max_wal_senders')::int > 0")
            healthy = healthy and logical == "t"
            roles = docker("exec", db, "psql", "-U", "postgres", "-At", "-c",
                           "SELECT count(*) FROM pg_roles WHERE rolname IN ('app_schema','app_writer') "
                           "AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication")
            healthy = healthy and roles == "2"
        if "valkey" in actual and healthy:
            # Verify persistent config through the authenticated admin connection.
            result = docker("exec", actual["valkey"]["Id"], "sh", "-c",
                            'REDISCLI_AUTH=$(cat /run/foxtrot/valkey_admin) valkey-cli --raw CONFIG GET appendonly aclfile maxmemory-policy')
            healthy = all(value in result.splitlines() for value in ("yes", "/data/users.acl", "noeviction"))
        if "seaweedfs" in actual and healthy:
            healthy = subprocess.run([sys.executable, str(env_root / "scripts/bucket.py"), "--check"],
                                     capture_output=True, timeout=15).returncode == 0
        return {"ready": healthy, "released": False, "configurationMatches": True}
    raise RuntimeError("Unknown operation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preflight", "claim", "inspect", "release"])
    parser.add_argument("environment")
    parser.add_argument("host")
    parser.add_argument("digest")
    parser.add_argument("--root", default="/var/lib/foxtrot")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args)))
    except Exception:
        print("Host ownership/service verification failed", file=sys.stderr)
        sys.exit(1)
