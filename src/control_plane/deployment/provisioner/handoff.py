"""Public v2 endpoint references, never resolved credential contents."""


def onprem(environment, inspection):
    selected = environment["resolved"]
    profile, inventory = selected["profile"], selected["inventory"]
    hosts = {role: host for host in inventory["hosts"] for role in host["roles"]}
    env_id = environment["id"]
    prefix = env_id.removeprefix("env-")
    return {
        "schemaVersion": 2, "environmentId": env_id, "workflowId": environment.get("workflowId"),
        "revision": environment["revision"], "target": environment["request"]["target"], "driver": "ansible",
        "compute": {"targetRef": f"resource:{prefix}-web", "hostId": hosts["app"]["id"],
                    "privateAddress": hosts["app"]["serviceAddress"], "runtime": "docker", "architecture": "amd64"},
        "database": {"targetRef": f"db:{prefix}-postgres", "provider": "SELF_MANAGED", "engine": "postgresql",
                     "majorVersion": 17, "endpoint": {"host": hosts["database"]["serviceAddress"],
                                                     "port": 5432, "database": profile["databaseName"]},
                     "tls": {"mode": "verify-full", "caRef": profile["secrets"]["pg_ca"]},
                     "adminCredentialRef": profile["secrets"]["pg_admin"],
                     "schemaCredentialRef": profile["secrets"]["pg_schema"],
                     "writerCredentialRef": profile["secrets"]["pg_writer"],
                     "schemaUser": "app_schema", "writerUser": "app_writer"},
        "cache": {"targetRef": f"resource:{prefix}-valkey", "engine": "valkey",
                  "host": hosts["cache"]["serviceAddress"], "port": 6379,
                  "transport": "private-network", "appUser": "app", "adminUser": "default",
                  "appCredentialRef": profile["secrets"]["valkey_app"],
                  "adminCredentialRef": profile["secrets"]["valkey_admin"]},
        "storage": {"targetRef": f"resource:{prefix}-receipts", "kind": "seaweedfs",
                    "url": f"http://{hosts['storage']['serviceAddress']}:8333",
                    "filer": f"{hosts['storage']['serviceAddress']}:18888",
                    "bucket": "receipts", "region": "us-east-1",
                    "adminCredentialRefs": {k: profile["secrets"][f"object_admin_{k}"] for k in ("key", "secret")},
                    "appCredentialRefs": {k: profile["secrets"][f"object_app_{k}"] for k in ("key", "secret")}},
        "application": {"bindingRef": environment["request"].get("appBindingRef"),
                        "initialWorkloadMode": "HOLD", "deploymentStatus": "NOT_DEPLOYED"},
        "ownership": {"host": "EXISTING", "services": "MANAGED", "dataRetention": "RETAIN"},
        "network": {"scope": "LOCAL_ONLY" if any(h.get("connection") == "local" for h in inventory["hosts"])
                    else "REGISTERED_CONNECTIONS", "checks": inspection.get("networkChecks", [])},
        "pendingChecks": ["app-owned-schema-bootstrap", "target-writer-fence", "compute-staging",
                          "cloud-to-self-managed-db-profile", "adapter-registration", "migration-readiness",
                          "cloudflare-origin-registration"],
    }
