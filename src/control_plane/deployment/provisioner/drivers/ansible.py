"""Pinned Ansible preparation with redacted events and explicit uncertain outcomes."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import ssl
import struct
import tempfile
from datetime import datetime, timezone

from .base import DriverError
from ..onprem_bundle import write_bundle


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AnsibleDriver:
    name = "ansible"

    def __init__(self, repo_root, work_root, catalog, *, timeout=1800, runner=None):
        self.root, self.work, self.catalog = Path(repo_root), Path(work_root), catalog
        self.timeout, self.runner = max(30, min(int(timeout), 3600)), runner
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        keyfile = self.work / "plan-seal.key"
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        if keyfile.is_symlink() or keyfile.stat().st_mode & 0o077:
            raise ValueError("plan-seal.key must be a private regular file")
        self.key = keyfile.read_bytes()
        if len(self.key) != 32:
            raise ValueError("invalid plan sealing key")

    def _seal(self, resolved, material):
        configuration = json.dumps(resolved, sort_keys=True).encode()
        for ref, value in sorted(material.items()):
            configuration += b"\0" + ref.encode() + b"\0" + value
        files = sorted((self.root / "ansible").rglob("*"))
        files += [self.root / "provisioner/onprem_bundle.py", Path(__file__)]
        code = hashlib.sha256()
        for path in files:
            if path.is_file() and "__pycache__" not in path.parts:
                code.update(str(path.relative_to(self.root)).encode() + b"\0" + path.read_bytes())
        return {"configuration": hmac.new(self.key, configuration, hashlib.sha256).hexdigest(),
                "code": code.hexdigest()}

    def _inputs(self, environment, context, *, verify=False):
        resolved = environment["resolved"]
        material = self.catalog.secrets(resolved)
        seal = self._seal(resolved, material)
        if verify and seal != context.get("seal"):
            raise DriverError("STALE_PLAN", "code, credentials or configuration changed")
        return material, seal

    def plan(self, environment, spec, context):
        material, seal = self._inputs(environment, context)
        observed = self._run("preflight", environment, material, seal, context)
        return {"seal": seal, "public": {
            "driver": "ansible", "intent": context["intent"], "guarantee": "ADVISORY_CHECK",
            "hosts": [h["id"] for h in spec["inventory"]["hosts"]],
            "roles": ["container-runtime", "postgresql", "valkey", "seaweedfs"],
            "changes": None,
            "checks": {"hostPreflight": "PASS", "declaredNetworkChecks": len(spec["profile"]["networkChecks"])},
            "limitations": ["container creation is verified after apply, not predicted by check mode",
                            "application deployment, schema bootstrap and migration are separate steps"],
            "observedAt": observed["observedAt"],
        }}

    def apply(self, environment, plan, context):
        material, seal = self._inputs(environment, context, verify=True)
        # Re-check host identity, ownership, capacity and conflicts immediately before mutation.
        self._run("preflight", environment, material, seal, context)
        self._run("prepare", environment, material, seal, context)
        result = self._run("inspect", environment, material, seal, context)
        if not result["ready"]:
            raise DriverError("INFRA_NOT_READY", "service inspection incomplete", uncertain=True)
        self._network(environment, material)
        return result

    def inspect(self, environment, context):
        material, seal = self._inputs(environment, context)
        result = self._run("inspect", environment, material, seal, context)
        if result["ready"]:
            self._network(environment, material)
        return result

    def cleanup(self, environment, plan, context):
        material, seal = self._inputs(environment, context, verify=True)
        self._run("cleanup", environment, material, seal, context)
        result = self._run("inspect", environment, material, seal, context)
        if not result["released"]:
            raise DriverError("CLEANUP_INCOMPLETE", "inspect retained services before retrying", uncertain=True)
        return result

    def _network(self, environment, material):
        """Verify database TLS from this runner, not only inside the target container."""
        selected = environment["resolved"]
        host = next(h for h in selected["inventory"]["hosts"] if "database" in h["roles"])
        ca = material[selected["profile"]["secrets"]["pg_ca"]].decode()
        try:
            context = ssl.create_default_context(cadata=ca)
            with socket.create_connection((host["serviceAddress"], 5432), timeout=5) as sock:
                sock.sendall(struct.pack("!II", 8, 80877103))
                if sock.recv(1) != b"S":
                    raise OSError("TLS negotiation refused")
                with context.wrap_socket(sock, server_hostname=host["serviceAddress"]):
                    pass
            for role, ports in (("cache", (6379,)), ("storage", (8333, 18888))):
                address = next(h["serviceAddress"] for h in selected["inventory"]["hosts"] if role in h["roles"])
                for port in ports:
                    with socket.create_connection((address, port), timeout=5):
                        pass
        except (OSError, ValueError):
            raise DriverError("SERVICE_CONNECTIVITY_FAILED", "service endpoint/TLS check failed", uncertain=True) from None

    def _run(self, phase, environment, material, seal, context):
        try:
            if self.runner is None:
                import ansible_runner
                runner = ansible_runner.run
            else:
                runner = self.runner
        except ImportError:
            raise DriverError("RUNNER_UNAVAILABLE", "install requirements-onprem.txt") from None
        selected = environment["resolved"]
        hosts, observations = selected["inventory"]["hosts"], {}
        folder = Path(tempfile.mkdtemp(prefix="onprem-job-", dir=self.work))
        try:
            os.chmod(folder, 0o700)
            inventory_hosts = {}
            known = folder / "known_hosts"
            known.write_text("\n".join(f"[{h['address']}]:{h['sshPort']} {h['sshHostKey']}\n"
                                      f"{h['address']} {h['sshHostKey']}" for h in hosts) + "\n")
            known.chmod(0o600)
            for host in hosts:
                bundle = folder / host["id"]
                write_bundle(bundle, self.root, environment, host, material)
                key = folder / f"{host['id']}.ssh.key"
                key.write_bytes(material[host["sshKeyRef"]])
                key.chmod(0o600)
                roles = host["roles"]
                service_names = [name for role, name in (("database", "postgres"), ("cache", "valkey"),
                                                         ("storage", "seaweedfs")) if role in roles]
                inventory_hosts[host["id"]] = {
                    "ansible_host": host["address"], "ansible_port": host["sshPort"], "ansible_user": host["sshUser"],
                    "ansible_ssh_private_key_file": str(key),
                    "ansible_ssh_common_args": f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={shlex.quote(str(known))} -o IdentitiesOnly=yes",
                    "ansible_become": False, "ansible_connection": host["connection"],
                    "ansible_python_interpreter": "/usr/bin/python3",
                    "foxtrot_environment_id": environment["id"], "foxtrot_host_id": host["id"],
                    "foxtrot_config_digest": seal["configuration"],
                    "foxtrot_base": host["runtimeRoot"],
                    "foxtrot_root": f"{host['runtimeRoot']}/{environment['id']}",
                    "foxtrot_uid": host["uid"], "foxtrot_gid": host["gid"],
                    "foxtrot_service_address": host["serviceAddress"],
                    "foxtrot_bundle_dir": str(bundle), "foxtrot_services": service_names,
                    "foxtrot_expected_images": {name: selected["profile"]["images"][name] for name in service_names},
                    "foxtrot_ports": [p for role, ports in (("database", [5432]), ("cache", [6379]),
                                                           ("storage", [8333, 18888])) if role in roles for p in ports],
                    "foxtrot_min_cpu": selected["profile"]["minCpu"],
                    "foxtrot_min_memory_mib": selected["profile"]["minMemoryMiB"],
                    "foxtrot_min_disk_gib": selected["profile"]["minDiskGiB"],
                    "foxtrot_network_checks": [c for c in selected["profile"]["networkChecks"] if c["fromHost"] == host["id"]],
                }

            def event_handler(event):
                data = event.get("event_data", {})
                host_id = data.get("host")
                if host_id in inventory_hosts:
                    if context.get("progress"):
                        context["progress"]({"phase": phase, "hostId": host_id, "observedAt": stamp()})
                    msg = data.get("res", {}).get("msg")
                    if event.get("event") == "runner_on_ok" and data.get("task") == "Publish preparation evidence" and type(msg) is dict:
                        observations[host_id] = {
                            "ready": msg.get("ready") is True, "released": msg.get("released") is True,
                            "configurationMatches": msg.get("configurationMatches") is True,
                        }
                # Do not retain unfiltered event JSON containing args, secrets or facts.
                return False

            result = runner(
                private_data_dir=str(folder), project_dir=str(self.root / "ansible"),
                playbook=f"playbooks/{phase}.yml",
                inventory={"all": {"children": {"foxtrot": {"hosts": inventory_hosts}}}},
                envvars={"ANSIBLE_CONFIG": str(self.root / "ansible/ansible.cfg"),
                         "ANSIBLE_HOST_KEY_CHECKING": "True", "ANSIBLE_NOCOLOR": "1",
                         "ANSIBLE_DISPLAY_ARGS_TO_STDOUT": "False", "ANSIBLE_RETRY_FILES_ENABLED": "False"},
                settings={"job_timeout": self.timeout, "pexpect_timeout": 10},
                cmdline="--check" if phase == "preflight" else "",
                event_handler=event_handler, quiet=True,
            )
            if result.rc != 0 or result.status != "successful":
                raise DriverError("ANSIBLE_" + phase.upper() + "_FAILED", "Ansible stage did not finish",
                                  uncertain=phase != "preflight")
            complete = set(observations) == set(inventory_hosts)
            return {"ready": complete and all(o["ready"] and o["configurationMatches"] for o in observations.values()),
                    "released": complete and all(o["released"] and o["configurationMatches"] for o in observations.values()),
                    "hosts": observations, "observedAt": stamp(),
                    "networkChecks": [{"fromHost": c["fromHost"], "host": c["host"], "port": c["port"],
                                       "status": "PASS"} for c in selected["profile"]["networkChecks"]]}
        except DriverError:
            raise
        except Exception:
            raise DriverError("RUNNER_ERROR", "runner failed; inspect the registered hosts",
                              uncertain=phase != "preflight") from None
        finally:
            shutil.rmtree(folder)
