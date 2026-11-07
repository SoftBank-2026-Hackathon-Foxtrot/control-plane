"""Operator-owned on-prem catalog. API callers select references, never SSH options."""
from __future__ import annotations

import copy
import ipaddress
import json
import re
from pathlib import Path

from .spec import RequestError, require

REF = re.compile(r"^[a-z][a-z0-9-]*:[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,180}$")
ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}$")
SECRETS = {
    "pg_admin", "pg_schema", "pg_writer", "pg_server_cert", "pg_server_key", "pg_ca",
    "valkey_admin", "valkey_app", "object_admin_key", "object_admin_secret",
    "object_app_key", "object_app_secret",
}
ROLES = {"app", "database", "cache", "storage"}
PRIVATE = tuple(ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


def fields(obj, required, optional=(), label="object"):
    require(type(obj) is dict and set(required) <= obj.keys()
            and not (obj.keys() - set(required) - set(optional)), f"{label}: invalid fields")


def private_address(value):
    try:
        address = ipaddress.ip_address(value)
        require(address.version == 4 and any(address in net for net in PRIVATE), "registered private IPv4 required")
    except (ValueError, TypeError):
        raise RequestError("registered private IPv4 required") from None
    return str(address)


def positive(value, maximum, label):
    require(type(value) is int and 1 <= value <= maximum, f"{label}: invalid positive integer")


def reference(value, label):
    require(type(value) is str and REF.fullmatch(value), f"{label}: invalid reference")
    return value


def request(body):
    fields(body, {"org", "target", "driver", "profileRef"}, {"appBindingRef", "workflowId", "mode"}, "request")
    require(type(body["org"]) is str and re.fullmatch(r"[a-z][a-z0-9-]{1,30}[a-z0-9]", body["org"]), "invalid org")
    fields(body["target"], {"kind", "siteRef", "inventoryRef"}, label="target")
    require(body["driver"] == "ansible" and body["target"]["kind"] == "onprem",
            "v2 currently accepts onprem / ansible; use v1 for AWS/GCP")
    for key in ("siteRef", "inventoryRef"):
        reference(body["target"][key], key)
    reference(body["profileRef"], "profileRef")
    if body.get("appBindingRef") is not None:
        reference(body["appBindingRef"], "appBindingRef")
    workflow = body.get("workflowId")
    require(workflow is None or (type(workflow) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workflow)),
            "invalid workflowId")
    require(body.get("mode", "plan") in ("plan", "apply"), "mode must be plan or apply")
    return {**copy.deepcopy(body), "mode": body.get("mode", "plan"), "workflowId": workflow}


class Inventory:
    def __init__(self, catalog_file, secret_root, *, allow_local=False):
        self.path = Path(catalog_file)
        self.secret_root = Path(secret_root).resolve()
        self.allow_local = allow_local

    def resolve(self, req):
        try:
            catalog = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raise RequestError("onprem catalog is unavailable or invalid") from None
        org, target = req["org"], req["target"]
        selected = {}
        for kind, ref in (("inventories", target["inventoryRef"]), ("profiles", req["profileRef"]),
                          ("bindings", req.get("appBindingRef"))):
            item = catalog.get(kind, {}).get(ref) if ref else None
            require(ref is None or (type(item) is dict and org in item.get("orgs", [])),
                    f"{kind}: reference is not registered for this org")
            selected[kind] = copy.deepcopy(item)
        inv, profile, binding = selected.values()
        fields(inv, {"orgs", "siteRef", "hosts"}, label="inventory")
        require(inv["siteRef"] == target["siteRef"], "inventory belongs to a different site")
        require(type(inv["hosts"]) is list and 1 <= len(inv["hosts"]) <= 16, "inventory needs 1-16 hosts")
        ids, addresses, roles = set(), set(), {}
        for host in inv["hosts"]:
            fields(host, {"id", "address", "serviceAddress", "sshUser", "sshPort", "sshHostKey",
                          "sshKeyRef", "roles", "runtimeRoot", "uid", "gid"}, {"connection"}, label="host")
            require(type(host["id"]) is str and ID.fullmatch(host["id"]) and host["id"] not in ids, "duplicate/invalid host id")
            ids.add(host["id"])
            connection = host.setdefault("connection", "ssh")
            require(connection in ("ssh", "local"), "invalid connection")
            require(connection != "local" or self.allow_local, "local execution is disabled")
            for name in ("address", "serviceAddress"):
                if connection == "local":
                    require(host[name] == "127.0.0.1", "local validation must use loopback")
                else:
                    host[name] = private_address(host[name])
            require(type(host["runtimeRoot"]) is str and re.fullmatch(r"/[A-Za-z0-9_./ -]+", host["runtimeRoot"])
                    and ".." not in Path(host["runtimeRoot"]).parts
                    and host["runtimeRoot"].startswith(("/home/", "/srv/")), "use a dedicated user-owned runtime directory")
            for name in ("uid", "gid"):
                positive(host[name], 2147483647, name)
            require(host["address"] not in addresses, "duplicate management address")
            addresses.add(host["address"])
            require(type(host["sshUser"]) is str and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", host["sshUser"]), "invalid SSH user")
            positive(host["sshPort"], 65535, "SSH port")
            require(type(host["sshHostKey"]) is str and re.fullmatch(
                r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", host["sshHostKey"]), "verified ed25519 host key required")
            reference(host["sshKeyRef"], "sshKeyRef")
            require(type(host["roles"]) is list and host["roles"]
                    and all(type(role) is str for role in host["roles"])
                    and len(host["roles"]) == len(set(host["roles"])), "host roles must be unique strings")
            for role in host["roles"]:
                require(role in ROLES and role not in roles, "each supported role must have exactly one host")
                roles[role] = host["id"]
        require(set(roles) == ROLES, "app, database, cache and storage hosts must be assigned")
        fields(profile, {"orgs", "images", "minCpu", "minMemoryMiB", "minDiskGiB", "databaseName",
                         "secrets", "networkChecks"}, label="profile")
        require(type(profile["images"]) is dict and set(profile["images"]) == {"postgres", "valkey", "seaweedfs"},
                "three pinned service images required")
        for image in profile["images"].values():
            require(type(image) is str and IMAGE.fullmatch(image), "service images must use sha256 digests")
        for key, maximum in (("minCpu", 4096), ("minMemoryMiB", 16777216), ("minDiskGiB", 1048576)):
            positive(profile[key], maximum, key)
        require(type(profile["databaseName"]) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", profile["databaseName"]),
                "invalid database name")
        require(type(profile["secrets"]) is dict and set(profile["secrets"]) == SECRETS, "profile credential references required")
        for value in profile["secrets"].values():
            reference(value, "credential")
        checks = profile["networkChecks"]
        require(type(checks) is list and len(checks) <= 64, "invalid network checks")
        for check in checks:
            fields(check, {"fromHost", "host", "port"}, label="network check")
            require(check["fromHost"] in ids, "network check host is not in inventory")
            check["host"] = private_address(check["host"])
            positive(check["port"], 65535, "network check port")
        if binding is not None:
            fields(binding, {"orgs", "hostId", "port", "healthPath", "expectedServedBy"}, label="binding")
            require(binding["hostId"] == roles["app"], "binding must refer to the app host")
            positive(binding["port"], 65535, "app port")
            require(type(binding["healthPath"]) is str and re.fullmatch(r"/[A-Za-z0-9/_.-]*", binding["healthPath"]),
                    "invalid health path")
            require(type(binding["expectedServedBy"]) is str and ID.fullmatch(binding["expectedServedBy"]),
                    "expected app identity required")
        refs = set(profile["secrets"].values()) | {h["sshKeyRef"] for h in inv["hosts"]}
        credentials = {}
        for ref in refs:
            entry = catalog.get("credentials", {}).get(ref)
            fields(entry, {"file"}, label="credential entry")
            require(type(entry["file"]) is str, "credential file must be a relative path")
            path = self.secret_root / entry["file"]
            require(not Path(entry["file"]).is_absolute() and ".." not in Path(entry["file"]).parts,
                    "credential path must stay within secretRoot")
            require(path.resolve().is_relative_to(self.secret_root) and not path.is_symlink(), "invalid credential path")
            credentials[ref] = entry["file"]
        return {"inventory": inv, "profile": profile, "binding": binding, "credentials": credentials}

    def secrets(self, resolved):
        result = {}
        for ref, relative in resolved["credentials"].items():
            path = self.secret_root / relative
            try:
                require(path.resolve().is_relative_to(self.secret_root) and not path.is_symlink(), "invalid credential path")
                info = path.stat()
                require(path.is_file() and not info.st_mode & 0o077 and info.st_size <= 65536,
                        "credential files must be private regular files (0600 or stricter)")
                value = path.read_bytes()
                require(value and b"\0" not in value, "empty or invalid credential file")
            except OSError:
                raise RequestError("a required credential file is unavailable") from None
            result[ref] = value
        return result
