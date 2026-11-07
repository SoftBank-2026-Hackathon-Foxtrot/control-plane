"""Render a fixed service bundle. No user-provided playbooks, shell commands or SQL."""
import json
from pathlib import Path


def write_bundle(folder, repo, environment, host, material):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = folder / "secrets"
    credentials.mkdir(mode=0o700)
    profile = environment["resolved"]["profile"]
    values = {}
    for name, ref in profile["secrets"].items():
        value = material[ref].rstrip(b"\r\n")
        values[name] = value.decode()
        path = credentials / name
        path.write_bytes(value)
        path.chmod(0o600)
    identities = [
        {"name": "object-migration", "credentials": [{"accessKey": values["object_admin_key"], "secretKey": values["object_admin_secret"]}],
         "actions": ["Admin", "Read", "Write", "List", "Tagging"]},
        {"name": "app", "credentials": [{"accessKey": values["object_app_key"], "secretKey": values["object_app_secret"]}],
         "actions": ["Read", "Write", "List"]},
    ]
    (credentials / "s3.json").write_text(json.dumps({"identities": identities}))
    (credentials / "s3.json").chmod(0o600)
    # Do not install data-service credentials on hosts that do not run that service.
    allowed = set()
    if "database" in host["roles"]:
        allowed |= {key for key in values if key.startswith("pg_")}
    if "cache" in host["roles"]:
        allowed |= {"valkey_admin", "valkey_app"}
    if "storage" in host["roles"]:
        allowed |= {"s3.json", "object_admin_key", "object_admin_secret"}
    for path in credentials.iterdir():
        if path.name not in allowed:
            path.unlink()
    root = f"{host['runtimeRoot']}/{environment['id']}"
    address, images = host["serviceAddress"], profile["images"]
    common = {"restart": "unless-stopped", "user": f"{host['uid']}:{host['gid']}",
              "labels": {"foxtrot.environment": environment["id"]}}
    services = {}
    def mounts(names):
        return [f"{root}/secrets/{name}:/run/foxtrot/{name}:ro" for name in names]
    if "database" in host["roles"]:
        services["postgres"] = {
            **common, "image": images["postgres"], "ports": [f"{address}:5432:5432"],
            "environment": {"POSTGRES_DB": profile["databaseName"], "POSTGRES_PASSWORD_FILE": "/run/foxtrot/pg_admin"},
            "volumes": [f"{root}/data/postgres:/var/lib/postgresql/data",
                        f"{root}/scripts:/opt/foxtrot:ro"] + mounts(
                            ["pg_admin", "pg_schema", "pg_writer", "pg_server_cert", "pg_server_key", "pg_ca"]),
            "command": ["postgres", "-c", "wal_level=logical", "-c", "max_replication_slots=10",
                        "-c", "max_wal_senders=10", "-c", "ssl=on", "-c", "ssl_cert_file=/run/foxtrot/pg_server_cert",
                        "-c", "ssl_key_file=/run/foxtrot/pg_server_key", "-c", "ssl_ca_file=/run/foxtrot/pg_ca",
                        "-c", "hba_file=/opt/foxtrot/pg_hba.conf"],
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U postgres -d \"$$POSTGRES_DB\""],
                            "interval": "5s", "timeout": "3s", "retries": 30}}
    if "cache" in host["roles"]:
        services["valkey"] = {
            **common, "image": images["valkey"], "ports": [f"{address}:6379:6379"],
            "entrypoint": ["sh", "/opt/foxtrot/valkey-start.sh"],
            "volumes": [f"{root}/data/valkey:/data", f"{root}/scripts:/opt/foxtrot:ro"] + mounts(["valkey_admin", "valkey_app"]),
            "healthcheck": {"test": ["CMD-SHELL", "REDISCLI_AUTH=$$(cat /run/foxtrot/valkey_admin) valkey-cli ping | grep -q PONG"],
                            "interval": "5s", "timeout": "3s", "retries": 30}}
    if "storage" in host["roles"]:
        services["seaweedfs"] = {
            **common, "image": images["seaweedfs"],
            "command": ["server", "-dir=/data", "-ip.bind=0.0.0.0", "-master.volumeSizeLimitMB=1024",
                        "-volume.max=0", "-filer", "-s3", "-s3.config=/run/foxtrot/s3.json"],
            "ports": [f"{address}:8333:8333", f"{address}:18888:18888"],
            "volumes": [f"{root}/data/seaweedfs:/data", f"{root}/secrets/s3.json:/run/foxtrot/s3.json:ro"],
            "healthcheck": {"test": ["CMD", "wget", "-qO-", "http://localhost:9333/cluster/status"],
                            "interval": "5s", "timeout": "3s", "retries": 30}}
    # App-only hosts prepare Docker but have no compose application yet.
    (folder / "compose.json").write_text(json.dumps({"name": environment["id"], "services": services}, indent=2))
    (folder / "host.json").write_text(json.dumps({"environmentId": environment["id"], "hostId": host["id"],
                                               "serviceAddress": address, "services": list(services),
                                               "images": {key: images[key] for key in services}}))
    scripts = folder / "scripts"
    scripts.mkdir(mode=0o700)
    for source in (Path(repo) / "ansible/files").iterdir():
        if source.is_file():
            target = scripts / source.name
            target.write_bytes(source.read_bytes())
            target.chmod(0o700)
