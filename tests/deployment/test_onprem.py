import copy
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from control_plane.deployment.paths import iac_root
from control_plane.deployment.provisioner.drivers.ansible import AnsibleDriver
from control_plane.deployment.provisioner.drivers.base import DriverError
from control_plane.deployment.provisioner.inventory import SECRETS, Inventory, request
from control_plane.deployment.provisioner.onprem import OnPremService
from control_plane.deployment.provisioner.onprem_bundle import write_bundle
from control_plane.deployment.provisioner.onprem_store import OnPremStore
from control_plane.deployment.provisioner.service import Conflict
from control_plane.deployment.provisioner.spec import RequestError

ROOT = iac_root()


def fixture(root):
    material = root / "secrets"
    material.mkdir()
    refs = {n: f"secret:{n}" for n in SECRETS}
    credentials = {ref: {"file": name} for name, ref in refs.items()}
    credentials["secret:ssh"] = {"file": "ssh"}
    for entry in credentials.values():
        file = material / entry["file"]
        file.write_text("private-value-" + entry["file"])
        file.chmod(0o600)
    host = {
        "id": "pc-one",
        "address": "100.64.1.2",
        "serviceAddress": "100.64.1.2",
        "sshUser": "deploy",
        "sshPort": 22,
        "sshHostKey": "ssh-ed25519 AAAA",
        "sshKeyRef": "secret:ssh",
        "roles": ["app", "database", "cache", "storage"],
        "runtimeRoot": "/home/deploy/.local/share/foxtrot",
        "uid": 1000,
        "gid": 1000,
    }
    catalog = {
        "inventories": {
            "inventory:pc": {"orgs": ["testorg"], "siteRef": "site:pc", "hosts": [host]}
        },
        "profiles": {
            "profile:pc": {
                "orgs": ["testorg"],
                "images": {
                    n: f"{n}@sha256:" + "1" * 64 for n in ("postgres", "valkey", "seaweedfs")
                },
                "minCpu": 2,
                "minMemoryMiB": 4096,
                "minDiskGiB": 20,
                "databaseName": "appdb",
                "secrets": refs,
                "networkChecks": [],
            }
        },
        "credentials": credentials,
        "bindings": {
            "binding:app": {
                "orgs": ["testorg"],
                "hostId": "pc-one",
                "port": 18080,
                "healthPath": "/readyz",
                "expectedServedBy": "onprem-app",
            }
        },
    }
    path = root / "catalog.json"
    path.write_text(json.dumps(catalog))
    body = {
        "org": "testorg",
        "driver": "ansible",
        "target": {"kind": "onprem", "siteRef": "site:pc", "inventoryRef": "inventory:pc"},
        "profileRef": "profile:pc",
        "mode": "plan",
    }
    return Inventory(path, material), catalog, body


class FakeDriver:
    def __init__(self):
        self.apply_calls = 0
        self.fail = False
        self.ready = True

    def plan(self, env, spec, context):
        return {
            "seal": {"configuration": "sealed", "code": "version"},
            "public": {"intent": context["intent"], "changes": None},
        }

    def apply(self, env, plan, context):
        self.apply_calls += 1
        context["progress"]({"phase": "prepare", "hostId": "pc-one"})
        if self.fail:
            raise DriverError("REMOTE_FAILED", "secret MUST NOT LEAK", uncertain=True)
        return self.inspect(env, context)

    def inspect(self, env, context):
        return {
            "ready": self.ready,
            "released": not self.ready,
            "observedAt": "2026-11-07T00:00:00+00:00",
            "networkChecks": [],
        }

    def cleanup(self, env, plan, context):
        self.ready = False
        return self.inspect(env, context)


class OnPremTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog, self.raw, self.body = fixture(self.root)
        self.store = OnPremStore(self.root / "journal.db")
        self.driver = FakeDriver()
        self.app_result = {"status": "NOT_READY", "observedAt": "2026-11-07T00:00:00+00:00"}
        self.service = OnPremService(
            self.store,
            self.catalog,
            self.driver,
            SimpleNamespace(check_onprem=lambda *a: copy.deepcopy(self.app_result)),
        )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def create(self, **extra):
        code, result = self.service.create({**self.body, **extra}, "create")
        self.assertEqual(code, 202)
        self.id = result["environmentId"]
        return result

    def action(self, action, **body):
        rev = self.service.get(self.id)["revision"]
        return self.service.command(
            self.id, action, {"expectedRevision": rev, **body}, f"{action}-{rev}"
        )

    def test_reference_validation_and_secret_permissions(self):
        resolved = self.catalog.resolve(request(self.body))
        self.assertEqual(len(self.catalog.secrets(resolved)), 13)
        (self.root / "secrets/pg_admin").chmod(0o644)
        with self.assertRaises(RequestError):
            self.catalog.secrets(resolved)
        self.raw["inventories"]["inventory:pc"]["hosts"][0]["address"] = "8.8.8.8"
        self.catalog.path.write_text(json.dumps(self.raw))
        with self.assertRaises(RequestError):
            self.catalog.resolve(request(self.body))

    def test_local_is_operator_opt_in_and_loopback_only(self):
        host = self.raw["inventories"]["inventory:pc"]["hosts"][0]
        host.update(connection="local", address="127.0.0.1", serviceAddress="127.0.0.1")
        self.catalog.path.write_text(json.dumps(self.raw))
        with self.assertRaises(RequestError):
            self.catalog.resolve(request(self.body))
        self.catalog.allow_local = True
        self.catalog.resolve(request(self.body))
        host["serviceAddress"] = "0.0.0.0"
        self.catalog.path.write_text(json.dumps(self.raw))
        with self.assertRaises(RequestError):
            self.catalog.resolve(request(self.body))

    def test_unknown_inputs_and_cross_org_are_rejected(self):
        with self.assertRaises(RequestError):
            request({**self.body, "playbook": "/etc/passwd"})
        with self.assertRaises(RequestError):
            self.catalog.resolve(request({**self.body, "org": "otherorg"}))

    def test_idempotency_and_host_reservation(self):
        first = self.create()
        self.assertEqual(self.service.create(self.body, "create")[1], first)
        with self.assertRaises(Conflict):
            self.service.create({**self.body, "mode": "apply"}, "create")
        with self.assertRaises(Conflict):
            self.service.create(self.body, "another")
        self.assertEqual(len(self.store.all("environments")), 1)

    def test_concurrent_create_has_one_winner(self):
        results = []

        def create(key):
            try:
                results.append(self.service.create(self.body, key)[0])
            except Conflict:
                results.append(409)

        threads = [threading.Thread(target=create, args=(f"key-{n}",)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [202, 409])

    def test_reviewed_plan_and_revision_are_required(self):
        self.create()
        self.service.run_next()
        env = self.service.get(self.id)
        self.assertEqual(env["status"], "PLANNED")
        with self.assertRaises(Conflict):
            self.action("apply", planId="stale")
        with self.assertRaises(Conflict):
            self.service.command(self.id, "apply", {"expectedRevision": 0}, "old")
        self.action("apply", planId=env["plan"]["planId"])
        self.service.run_next()
        env = self.service.get(self.id)
        self.assertEqual(env["status"], "INFRA_READY")
        self.assertEqual(env["readiness"]["application"]["status"], "NOT_CONFIGURED")
        self.assertEqual(env["handoff"]["revision"], env["revision"])
        self.assertEqual(env["handoff"]["database"]["provider"], "SELF_MANAGED")
        self.assertNotIn("private-value", json.dumps(env))

    def test_expired_plan_does_not_apply(self):
        self.create()
        self.service.run_next()
        env = self.service.get(self.id)
        self.service.clock = lambda: datetime.now(UTC) + timedelta(hours=2)
        with self.assertRaises(Conflict):
            self.action("apply", planId=env["plan"]["planId"])
        self.assertEqual(self.driver.apply_calls, 0)

    def test_partial_failure_requires_reconcile_and_redacts_error(self):
        self.driver.fail = True
        self.create(mode="apply")
        self.service.run_next()
        env = self.service.get(self.id)
        self.assertEqual(env["status"], "NEEDS_RECONCILIATION")
        self.assertNotIn("MUST NOT LEAK", json.dumps(env))
        with self.assertRaises(Conflict):
            self.action("plan")
        self.action("reconcile")
        self.service.run_next()
        self.assertEqual(self.service.get(self.id)["status"], "INFRA_READY")
        self.assertEqual(self.driver.apply_calls, 1)

    def test_restart_never_replays_running_mutation(self):
        created = self.create(mode="apply")
        with self.store.transaction():
            job = self.store.get("jobs", created["jobId"])
            job["status"] = "RUNNING"
            self.store.put("jobs", job)
        self.service.recover()
        self.assertEqual(self.service.get(self.id)["status"], "NEEDS_RECONCILIATION")
        self.assertFalse(self.service.run_next())
        self.assertEqual(self.driver.apply_calls, 0)

    def test_ready_requires_app_and_infrastructure(self):
        self.create(mode="apply", appBindingRef="binding:app")
        self.service.run_next()
        self.assertEqual(self.service.get(self.id)["status"], "INFRA_READY")
        self.service.run_next()
        self.assertEqual(self.service.get(self.id)["status"], "APP_NOT_READY")
        self.app_result["status"] = "READY"
        self.action("check-app")
        self.service.run_next()
        env = self.service.get(self.id)
        self.assertEqual(env["status"], "READY")
        self.assertEqual(env["handoff"]["application"]["deploymentStatus"], "VERIFIED")

    def test_workflow_prevents_cleanup_and_reconfiguration(self):
        self.create(mode="apply", workflowId="wf-1")
        self.service.run_next()
        with self.assertRaises(Conflict):
            self.action("plan", intent="cleanup")
        with self.assertRaises(Conflict):
            self.action("plan", intent="prepare")

    def test_cleanup_retains_data_reservation(self):
        self.create(mode="apply")
        self.service.run_next()
        self.action("plan", intent="cleanup")
        self.service.run_next()
        env = self.service.get(self.id)
        self.action("apply", planId=env["plan"]["planId"])
        self.service.run_next()
        self.assertEqual(self.service.get(self.id)["status"], "RELEASED")
        with self.assertRaises(Conflict):
            self.service.create(self.body, "new-env")

    def test_real_driver_pins_material_and_erases_runner_artifacts(self):
        self.create()
        env = self.store.get("environments", self.id)
        captured = []

        def runner(**kwargs):
            captured.append(kwargs)
            self.assertTrue(Path(kwargs["private_data_dir"]).exists())
            host = kwargs["inventory"]["all"]["children"]["foxtrot"]["hosts"]["pc-one"]
            self.assertFalse(host["ansible_become"])
            self.assertNotIn("StrictHostKeyChecking=no", host["ansible_ssh_common_args"])
            self.assertFalse((Path(host["foxtrot_bundle_dir"]) / "ssh.key").exists())
            kwargs["event_handler"](
                {
                    "event": "runner_on_ok",
                    "event_data": {
                        "host": "pc-one",
                        "task": "Publish preparation evidence",
                        "res": {"msg": {"ready": True, "configurationMatches": True}},
                    },
                }
            )
            return SimpleNamespace(rc=0, status="successful")

        driver = AnsibleDriver(ROOT, self.root / "runner", self.catalog, runner=runner)
        planned = driver.plan(env, env["resolved"], {"intent": "prepare"})
        self.assertIsNone(planned["public"]["changes"])
        self.assertFalse(Path(captured[0]["private_data_dir"]).exists())
        (self.root / "secrets/pg_admin").write_text("rotated-secret")
        with self.assertRaisesRegex(DriverError, "changed"):
            driver.apply(env, planned["public"], {"seal": planned["seal"], "intent": "prepare"})
        self.assertEqual(len(captured), 1)

    def test_bundle_ports_nonroot_credentials_and_no_table_bootstrap(self):
        resolved = self.catalog.resolve(request(self.body))
        env = {"id": "env-123456789012", "resolved": resolved}
        folder = self.root / "bundle"
        write_bundle(
            folder, ROOT, env, resolved["inventory"]["hosts"][0], self.catalog.secrets(resolved)
        )
        bundle = json.loads((folder / "compose.json").read_text())
        for service in bundle["services"].values():
            self.assertEqual(service["user"], "1000:1000")
            self.assertTrue(all(p.startswith("100.64.1.2:") for p in service["ports"]))
        self.assertNotIn("CREATE TABLE", (folder / "scripts/postgres-roles.sh").read_text())
        pg_mounts = bundle["services"]["postgres"]["volumes"]
        self.assertFalse(any("valkey_" in p or "s3.json" in p for p in pg_mounts))
        self.assertTrue((folder / "secrets/pg_admin").stat().st_mode & 0o777 == 0o600)

    def test_http_requires_auth_idempotency_and_preserves_v2_responses(self):
        import urllib.error
        import urllib.request

        from control_plane.deployment.provisioner.server import ProvisionerServer

        server = ProvisionerServer(
            ("127.0.0.1", 0),
            SimpleNamespace(onprem=self.service, wake=threading.Event()),
            "test-token",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def call(method, path, body=None, token="test-token", key=None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}" + path,
                data=json.dumps(body).encode() if body is not None else None,
                method=method,
            )
            if token:
                req.add_header("Authorization", "Bearer " + token)
            if key:
                req.add_header("Idempotency-Key", key)
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.load(error)

        try:
            self.assertEqual(call("GET", "/v2/environments", token=None)[0], 401)
            self.assertEqual(call("POST", "/v2/environments", self.body)[0], 400)
            code, result = call("POST", "/v2/environments", self.body, key="http-create")
            self.assertEqual(code, 202)
            self.service.run_next()
            self.assertEqual(
                call("GET", "/v2/environments/" + result["environmentId"])[1]["status"], "PLANNED"
            )
            self.assertEqual(call("GET", "/v2/jobs/" + result["jobId"])[1]["status"], "SUCCEEDED")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_known_incomplete_reconcile_allows_explicit_repair_only(self):
        self.driver.fail = True
        self.create(mode="apply")
        self.service.run_next()
        self.driver.inspect = lambda *a: {
            "ready": False,
            "observedAt": "2026-11-07T00:00:00+00:00",
            "hosts": {"pc-one": {"configurationMatches": True}},
        }
        self.action("reconcile")
        self.service.run_next()
        self.assertEqual(self.service.get(self.id)["status"], "FAILED")
        self.assertEqual(self.driver.apply_calls, 1)
        self.action("plan")
        self.service.run_next()
        self.assertEqual(self.service.get(self.id)["status"], "PLANNED")

    def test_openapi_valid_and_created_request_matches(self):
        import yaml
        from jsonschema import validate
        from openapi_spec_validator import validate_spec

        document = yaml.safe_load((ROOT / "contracts/onprem.openapi.yaml").read_text())
        validate_spec(document)
        validate(self.body, document["components"]["schemas"]["Create"])

    def test_onprem_probe_rejects_wrong_identity(self):
        from control_plane.deployment.provisioner.readiness import AppChecker

        checker = AppChecker({"deadlineSeconds": 0})
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.getheader.return_value = "old-cloud"
        checker.opener = SimpleNamespace(open=lambda *a, **k: response)
        handoff = {"compute": {"privateAddress": "100.64.1.2"}}
        binding = {
            "hostId": "pc-one",
            "port": 18080,
            "healthPath": "/readyz",
            "expectedServedBy": "onprem-app",
        }
        self.assertEqual(checker.check_onprem(handoff, binding)["status"], "NOT_READY")
        response.getheader.return_value = "onprem-app"
        self.assertEqual(checker.check_onprem(handoff, binding)["status"], "READY")


if __name__ == "__main__":
    unittest.main()
