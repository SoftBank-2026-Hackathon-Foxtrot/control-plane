"""Provisioner: request rules, job lifecycle and HTTP surface, with a fake Terraform."""

import datetime as dt
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from control_plane.deployment.paths import iac_root
from control_plane.deployment.provisioner import spec
from control_plane.deployment.provisioner.server import ProvisionerServer
from control_plane.deployment.provisioner.service import Conflict, Service
from control_plane.deployment.provisioner.store import Store
from control_plane.deployment.provisioner.terraform import TerraformError


class ReadyChecker:
    def check(self, handoff, app):
        return {
            "status": "READY",
            "observedAt": "2026-01-01T00:00:00+00:00",
            "lastSuccessAt": "2026-01-01T00:00:00+00:00",
            "httpStatus": 200,
        }


ROOT = iac_root()
CATALOG = json.loads((ROOT / "provisioner/catalog.json").read_text())
CONFIG = json.loads((ROOT / "provisioner/config.example.json").read_text())
TODAY = dt.date(2026, 11, 6)
TOKEN = "test-token-0123456789abcdef"


class FakeTerraform:
    def __init__(self, root):
        self.root, self.calls, self.fail = Path(root), [], None

    def log_path(self, env_id):
        return self.root / f"{env_id}.log"

    def plan(self, env_id, cloud, tfvars, *, destroy=False):
        self.calls.append(("plan", env_id, destroy))
        if self.fail == "plan":
            raise TerraformError("terraform plan failed: boom")
        return {
            "plan": "destroy.tfplan" if destroy else "reviewed.tfplan",
            "changes": {"create": 30},
        }

    def apply(self, env_id, cloud, plan):
        self.calls.append(("apply", env_id, plan))
        if plan == "destroy.tfplan":
            return None
        return {"cloud": cloud, "app": {"url": "http://192.0.2.10"}}


def body(**over):
    b = {"org": {"id": "chiikawa", "name": "치이카와"}, "cloud": "aws"}
    b.update(over)
    return b


class SpecTest(unittest.TestCase):
    def render(self, b):
        return spec.render(spec.parse(b, CATALOG, CONFIG), CATALOG, CONFIG, "a1b2", TODAY)

    def test_each_cloud_renders_valid_inputs(self):
        aws = self.render(body())
        self.assertEqual(aws["deployment_id"], "chiikawa-aws-a1b2")
        self.assertEqual(
            aws["app"]["image"], CONFIG["clouds"]["aws"]["images"]["chiikawa-groupbuy"]
        )
        self.assertEqual(aws["app"]["worker_count"], 2)
        self.assertEqual(aws["metadata"]["owner"], "chiikawa")
        self.assertEqual(aws["metadata"]["expires_on"], "2026-12-06")
        self.assertFalse(aws["deletion_protection"])
        gcp = self.render(
            body(cloud="gcp", access={"dbAuthorizedNetworks": {"db-agent": "203.0.113.7/32"}})
        )
        self.assertEqual(gcp["project_id"], "replace-with-project-id")
        self.assertEqual(gcp["db_authorized_networks"], {"db-agent": "203.0.113.7/32"})
        self.assertEqual(gcp["bucket_name"], "foxtrot-chiikawa-gcp-a1b2")

    def test_deployment_id_fits_terraform_rule(self):
        dep = spec.deployment_id("a-very-long-organisation-name", "gcp", "ffff")
        self.assertLessEqual(len(dep), 20)
        self.assertRegex(dep, r"^[a-z][a-z0-9-]{2,19}$")

    def test_unreviewed_choices_are_rejected(self):
        bad = [
            body(org={"id": "Bad Org"}),
            body(cloud="azure"),
            body(app={"template": "unknown"}),
            body(target={"accountId": "999999999999"}),
            body(cloud="gcp", target={"projectId": "someone-else"}),
            body(app={"image": "docker.io/library/nginx:latest"}),
            body(sizing={"compute_instance_type": "p5.48xlarge"}),
            body(access={"dbAuthorizedNetworks": {}}),  # GCP-only field on AWS
            body(app={"env": {"lower": "x"}}),
            body(extra=True),
        ]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(spec.RequestError):
                self.render(b)

    def test_platform_app_env_is_the_default(self):
        config = json.loads(json.dumps(CONFIG))
        config["appEnv"] = {
            "REDIS_URL": "redis://redis-proxy.internal:6379",
            "OBJECT_MIGRATION_SERVER": "http://object-migration.internal:8080",
        }
        tf = spec.render(
            spec.parse(body(app={"env": {"REDIS_URL": "redis://mine:6379"}}), CATALOG, config),
            CATALOG,
            config,
            "a1b2",
            TODAY,
        )
        self.assertEqual(tf["app"]["env"]["REDIS_URL"], "redis://mine:6379")
        self.assertEqual(
            tf["app"]["env"]["OBJECT_MIGRATION_SERVER"], "http://object-migration.internal:8080"
        )

    def test_tailscale_comes_from_the_server_config(self):
        config = json.loads(json.dumps(CONFIG))
        config["clouds"]["gcp"]["tailscale"] = {
            "authKeySecret": "projects/p/secrets/foxtrot-tailscale-authkey-gcp"
        }
        tf = spec.render(
            spec.parse(body(cloud="gcp"), CATALOG, config), CATALOG, config, "a1b2", TODAY
        )
        self.assertEqual(
            tf["tailscale"],
            {
                "auth_key_secret": "projects/p/secrets/foxtrot-tailscale-authkey-gcp",
                "tag": "tag:gcp",
            },
        )
        self.assertNotIn("tailscale", self.render(body()))

    def test_app_settings_pass_through(self):
        tf = self.render(
            body(
                app={
                    "workerCount": 3,
                    "seedDemoData": False,
                    "env": {"CONTROL_PLANE_URL": "http://cp.internal:8000"},
                },
                sizing={"compute_instance_type": "m6i.large"},
            )
        )
        self.assertEqual(tf["app"]["worker_count"], 3)
        self.assertFalse(tf["app"]["seed_demo_data"])
        self.assertEqual(tf["app"]["env"]["CONTROL_PLANE_URL"], "http://cp.internal:8000")
        self.assertEqual(tf["sizing"]["compute_instance_type"], "m6i.large")


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tf = FakeTerraform(self.tmp.name)
        self.service = Service(
            Store(Path(self.tmp.name) / "p.db"), self.tf, CATALOG, CONFIG, checker=ReadyChecker()
        )
        self.service.today = lambda: TODAY

    def tearDown(self):
        self.tmp.cleanup()

    def drain(self):
        while self.service.run_next():
            pass

    def test_create_apply_destroy(self):
        code, created = self.service.create(body())
        self.assertEqual(code, 202)
        self.drain()
        env = self.service.get(created["environmentId"])
        self.assertEqual(env["status"], "READY")
        self.assertEqual(env["appUrl"], "http://192.0.2.10")
        self.assertEqual(self.service.job(created["jobId"])["status"], "SUCCEEDED")
        with self.assertRaises(Conflict) as ctx:
            self.service.create(body())
        self.assertEqual(ctx.exception.code, "ENVIRONMENT_EXISTS")
        self.service.destroy(created["environmentId"])
        self.drain()
        self.assertEqual(self.service.get(created["environmentId"])["status"], "DESTROYED")
        self.assertEqual(self.service.create(body())[0], 202)  # the org can build again

    def test_plan_mode_waits_for_apply(self):
        _, created = self.service.create(body(mode="plan"))
        self.drain()
        env = self.service.get(created["environmentId"])
        self.assertEqual((env["status"], env["plan"]["changes"]), ("PLANNED", {"create": 30}))
        self.assertNotIn("apply", [c[0] for c in self.tf.calls])
        self.service.apply(created["environmentId"])
        self.drain()
        self.assertEqual(self.service.get(created["environmentId"])["status"], "READY")

    def test_update_keeps_resources_and_merges_settings(self):
        _, created = self.service.create(body(app={"env": {"A": "1"}}))
        self.drain()
        env_id = created["environmentId"]
        before = self.service.store.environment(env_id)
        self.assertEqual(
            self.service.update(
                env_id, {"app": {"tunnelSecret": "arn:aws:secretsmanager:x", "port": 8000}}
            )[0],
            202,
        )
        self.drain()
        after = self.service.store.environment(env_id)
        self.assertEqual(after["status"], "READY")
        self.assertEqual(after["deployment_id"], before["deployment_id"])
        self.assertEqual(after["tfvars"]["bucket_name"], before["tfvars"]["bucket_name"])
        self.assertEqual(after["tfvars"]["app"]["tunnel_secret"], "arn:aws:secretsmanager:x")
        self.assertEqual(after["tfvars"]["app"]["env"]["A"], "1")
        self.assertIn("OTEL_RESOURCE_ATTRIBUTES", after["tfvars"]["app"]["env"])
        self.assertNotIn("OTEL_RESOURCE_ATTRIBUTES", after["request"]["app"]["env"])
        self.assertEqual(after["tfvars"]["app"]["port"], 8000)
        self.assertTrue(after["tfvars"]["enable_cloudflare_tunnel"])
        with self.assertRaises(spec.RequestError):
            self.service.update(env_id, {"cloud": "gcp"})

    def test_idempotency_key(self):
        first = self.service.create(body(), "key-1")
        self.assertEqual(self.service.create(body(), "key-1"), first)
        with self.assertRaises(Conflict) as ctx:
            self.service.create(body(cloud="gcp"), "key-1")
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_failure_and_interrupted_job(self):
        self.tf.fail = "plan"
        _, created = self.service.create(body())
        self.drain()
        env = self.service.get(created["environmentId"])
        self.assertEqual(env["status"], "FAILED")
        self.assertIn("boom", env["error"])
        self.tf.fail = None
        _, again = self.service.create(body(cloud="gcp"))
        self.service.store.update_job(again["jobId"], status="RUNNING")
        self.service.recover()
        self.assertEqual(self.service.get(again["environmentId"])["status"], "NEEDS_RECONCILIATION")
        with self.assertRaises(Conflict):
            self.service.destroy(again["environmentId"])


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(
            Store(Path(self.tmp.name) / "p.db"),
            FakeTerraform(self.tmp.name),
            CATALOG,
            CONFIG,
            checker=ReadyChecker(),
        )
        self.server = ProvisionerServer(("127.0.0.1", 0), self.service, TOKEN)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, payload=None, token=TOKEN):
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_routes(self):
        self.assertEqual(self.call("GET", "/health", token=None), (200, {"status": "ok"}))
        self.assertEqual(self.call("GET", "/v1/environments", token=None)[0], 401)
        self.assertEqual(self.call("POST", "/v1/environments", body(cloud="azure"))[0], 400)
        code, created = self.call("POST", "/v1/environments", body())
        self.assertEqual(code, 202)
        while self.service.run_next():
            pass
        code, env = self.call("GET", f"/v1/environments/{created['environmentId']}")
        self.assertEqual((code, env["status"]), (200, "READY"))
        self.assertEqual(self.call("GET", f"/v1/jobs/{created['jobId']}")[1]["status"], "SUCCEEDED")
        self.assertEqual(
            self.call("POST", "/v1/environments", body())[1]["code"], "ENVIRONMENT_EXISTS"
        )
        self.assertEqual(
            self.call("DELETE", f"/v1/environments/{created['environmentId']}")[0], 202
        )
        self.assertEqual(self.call("GET", "/v1/environments/env-000000000000")[0], 404)


if __name__ == "__main__":
    unittest.main()


def test_app_exporter_settings_are_cloud_scoped_secret_references(tmp_path):
    import copy

    import pytest

    config = copy.deepcopy(CONFIG)
    config["appTelemetry"] = {
        "endpoint": "http://collector.internal:4318",
        "applicationId": "goods",
        "headersSecrets": {
            "aws": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:otel",
            "gcp": "projects/replace-with-project-id/secrets/otel",
        },
    }
    for cloud in ("aws", "gcp"):
        req = spec.parse(
            {"org": {"id": "testorg"}, "cloud": cloud, "app": {"template": "chiikawa-groupbuy"}},
            CATALOG,
            config,
        )
        app = spec.render(req, CATALOG, config, "abcd", TODAY)["app"]
        assert app["env"]["OTEL_SYSTEM_METRICS"] == "container"
        assert app["env"]["OTEL_APPLICATION_ID"] == "goods"
        assert app["env"]["OTEL_OWNER_ID"] == "testorg"
        assert app["env"]["OTEL_DEPLOYMENT_ID"].endswith(cloud + "-abcd")
        assert f"deployment.environment.name={cloud}" in app["env"]["OTEL_RESOURCE_ATTRIBUTES"]
        attributes = dict(
            item.split("=", 1) for item in app["env"]["OTEL_RESOURCE_ATTRIBUTES"].split(",")
        )
        assert attributes["foxtrot.owner.id"] == "testorg"
        assert attributes["deployment.id"] == app["env"]["OTEL_DEPLOYMENT_ID"]
        assert (
            app["secret_env"]["OTEL_EXPORTER_OTLP_HEADERS"]
            == config["appTelemetry"]["headersSecrets"][cloud]
        )
        engine = Service(
            Store(tmp_path / f"{cloud}.db"),
            FakeTerraform(tmp_path),
            CATALOG,
            config,
            ReadyChecker(),
        )
        _, created = engine.create(
            {"org": {"id": "testorg", "name": "different-app-name"}, "cloud": cloud}
        )
        while engine.run_next():
            pass
        saved = engine.store.environment(created["environmentId"])
        for rendered in (
            saved["tfvars"],
            engine.validate_update(saved["id"], {"app": {"cpu": 1, "memoryMiB": 2048}})[1],
        ):
            env = rendered["app"]["env"]
            attrs = dict(item.split("=", 1) for item in env["OTEL_RESOURCE_ATTRIBUTES"].split(","))
            assert attrs["application.id"] == env["OTEL_APPLICATION_ID"] == "goods"
            assert attrs["deployment.id"] == env["OTEL_DEPLOYMENT_ID"] == saved["deployment_id"]
            assert attrs["foxtrot.owner.id"] == env["OTEL_OWNER_ID"] == "testorg"
        engine.store.db.close()
        req["app"]["apiEnv"] = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://other"}
        with pytest.raises(spec.RequestError, match="OTEL"):
            spec.render(req, CATALOG, config, "abcd", TODAY)


def test_telemetry_uses_loaded_seed_application_instead_of_example_name():
    from control_plane.deployment.provisioner.app_telemetry import configure_app

    app = {}
    configure_app(
        app,
        {
            "applicationId": "chiikawa",
            "appTelemetry": {
                "applicationId": "goods",
                "endpoint": "http://collector:4318",
                "headersSecrets": {"aws": "secret"},
            },
        },
        "aws",
        owner="owner",
        deployment="deployment",
    )
    assert app["env"]["OTEL_APPLICATION_ID"] == "chiikawa"
