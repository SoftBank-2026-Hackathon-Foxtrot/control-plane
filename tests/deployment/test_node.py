"""On-prem transport, preparation ownership, secrets and interruption recovery."""

import copy
import errno
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from control_plane.deployment.provisioner import node_remote, spec
from control_plane.deployment.provisioner.node import DeploymentExecutor, NodeEffectUnconfirmed
from control_plane.deployment.provisioner.node_bundle import install
from control_plane.deployment.provisioner.node_config import resolve
from control_plane.deployment.provisioner.service import Conflict, Service
from control_plane.deployment.provisioner.store import Store
from control_plane.deployment.provisioner.terraform import TerraformError
from tests.deployment.test_provisioner import CATALOG, TODAY, FakeTerraform, ReadyChecker


@pytest.fixture
def node_config(tmp_path):
    key = tmp_path / "ssh_key"
    key.write_text("test-private-key")
    key.chmod(0o600)
    return {
        "workRoot": str(tmp_path),
        "clouds": {
            "onprem": {
                "nodes": {
                    "pc": {
                        "orgs": ["testorg"],
                        "dockerHost": "ssh://deploy@100.64.1.2:2222",
                        "sshKeyFile": str(key),
                        "sshHostKey": "ssh-ed25519 AAAA",
                        "runtimeRoot": "/home/deploy/apps",
                        "network": "registered-data_default",
                        "bindAddress": "100.64.1.2",
                        "appUrl": "http://100.64.1.2",
                        "expectedServedBy": "onprem",
                        "imagePrefixes": ["ghcr.io/foxtrot/"],
                        "environment": {"REDIS_URL": "redis://platform-proxy:6379"},
                        "apiEnvironment": {"WORKLOAD_ACTOR": "goods-onprem-api"},
                        "workerEnvironment": {"WORKLOAD_ACTOR": "goods-onprem-worker"},
                        "networkChecks": [
                            {"name": "platform", "host": "control-plane.internal", "port": 8000}
                        ],
                        "readinessDeadlineSeconds": 0,
                    }
                }
            }
        },
    }


def request(**extra):
    return {
        "org": {"id": "testorg"},
        "cloud": "onprem",
        "target": {"nodeId": "pc"},
        "app": {"image": "ghcr.io/foxtrot/app:v1", "port": 18080, "workerCount": 1},
        **extra,
    }


def rendered(config, **extra):
    return spec.render(
        spec.parse(request(**extra), CATALOG, config), CATALOG, config, "abcd", TODAY
    )


class FakeExecutor(DeploymentExecutor):
    def __init__(self, terraform, nodes):
        super().__init__(terraform, nodes)
        self.calls = []
        self.fail_apply = False

    def remote(self, node, payload):
        self.calls.append(copy.deepcopy(payload))
        if payload["action"] == "apply" and self.fail_apply:
            raise NodeEffectUnconfirmed("connection lost")
        return {"ready": True, "complete": True}


def executor(tmp_path, config):
    tf = FakeTerraform(tmp_path)
    tf.work_root = tmp_path
    return FakeExecutor(tf, config["clouds"]["onprem"]["nodes"])


def test_render_checks_owner_capacity_and_image_before_queueing(node_config):
    assert rendered(node_config)["org_id"] == "testorg"
    assert (
        rendered(node_config, app={"image": "ghcr.io/foxtrot/app:v1", "workerCount": 0})["app"][
            "worker_count"
        ]
        == 0
    )
    for extra in (
        {"org": {"id": "other"}},
        {"target": {"nodeId": "pc", "dockerHost": "ssh://evil"}},
        {"app": {"image": "ghcr.io/foxtrot-evil/app:v1"}},
        {"app": {"image": "ghcr.io/foxtrot/app:v1", "memoryMiB": -1}},
    ):
        with pytest.raises(spec.RequestError):
            rendered(node_config, **extra)


def test_compose_uses_proxy_env_and_role_identities_and_keeps_dollar_secrets(
    tmp_path, node_config, monkeypatch
):
    monkeypatch.setenv("NODE_SECRET", "password${not-an-environment-variable}")
    node_config["clouds"]["onprem"]["nodes"]["pc"]["secretEnv"] = {"WORKLOAD_TOKEN": "NODE_SECRET"}
    runner = executor(tmp_path, node_config)
    rendered_spec = rendered(node_config)
    node = runner.resolve(rendered_spec)
    document = runner.compose("env-123456789abc", rendered_spec, node)
    api, worker = document["services"].values()
    assert api["environment"]["WORKLOAD_ACTOR"] == "goods-onprem-api"
    assert worker["environment"]["WORKLOAD_ACTOR"] == "goods-onprem-worker"
    assert api["hostname"] != worker["hostname"]
    assert api["environment"]["WORKLOAD_TOKEN"] == "password$${not-an-environment-variable}"
    assert api["environment"]["REDIS_URL"] == "redis://platform-proxy:6379"
    assert api["ports"] == ["100.64.1.2:18080:18080"]
    assert "ports" not in worker
    assert "volumes" not in document
    assert document["networks"]["runtime"]["external"] is True
    rendered_spec["app"]["env"] = {"REDIS_URL": "redis://bypass"}
    with pytest.raises(spec.RequestError, match="override"):
        runner.compose("env-123456789abc", rendered_spec, node)


def test_plan_excludes_resolved_secrets_and_refuses_changed_registration(
    tmp_path, node_config, monkeypatch
):
    monkeypatch.setenv("NODE_SECRET", "unique-secret-value")
    node_config["clouds"]["onprem"]["nodes"]["pc"]["secretEnv"] = {"WORKLOAD_TOKEN": "NODE_SECRET"}
    runner = executor(tmp_path, node_config)
    plan = runner.plan("env-123456789abc", "onprem", rendered(node_config))
    saved = tmp_path / "env-123456789abc" / plan["plan"]
    assert "unique-secret-value" not in saved.read_text()
    assert saved.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("NODE_SECRET", "rotated")
    with pytest.raises(TerraformError, match="changed"):
        runner.apply("env-123456789abc", "onprem", plan["plan"])
    assert [call["action"] for call in runner.calls] == ["preflight"]


def test_ssh_pins_host_and_passes_secrets_only_on_stdin(tmp_path, node_config):
    runner = executor(tmp_path, node_config)
    node = runner.resolve(rendered(node_config))
    payload = {"action": "apply", "secret": "only-on-stdin"}
    with patch(
        "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout='{"ready":true}')
    ) as run:
        DeploymentExecutor.remote(runner, node, payload)
        kwargs = run.call_args.kwargs
        command = run.call_args.args[0]
        assert "only-on-stdin" not in " ".join(command)
        assert json.loads(kwargs["input"])["secret"] == "only-on-stdin"
        assert "StrictHostKeyChecking=yes" in command and "IdentityAgent=none" in command
        assert "deploy@100.64.1.2" in command and "2222" in command
    with (
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 420)),
        pytest.raises(NodeEffectUnconfirmed),
    ):
        DeploymentExecutor.remote(runner, node, payload)


def test_interruption_requires_read_only_reconcile_and_preserves_node_reservation(
    tmp_path, node_config
):
    runner = executor(tmp_path, node_config)
    service = Service(
        Store(tmp_path / "jobs.db"), runner, CATALOG, node_config, checker=ReadyChecker()
    )
    _, accepted = service.create(request(), "first")
    runner.fail_apply = True
    service.run_next()
    env_id = accepted["environmentId"]
    assert service.get(env_id)["status"] == "NEEDS_RECONCILIATION"
    with pytest.raises(Conflict):
        service.create(request(), "second")
    service.reconcile(env_id)
    runner.calls.clear()
    service.run_next()
    service.run_next()
    assert [call["action"] for call in runner.calls] == ["inspect"]
    assert service.get(env_id)["status"] == "READY"
    assert service.get(env_id)["handoff"]["migrationReadiness"]["status"] == "NOT_EVALUATED"
    service.destroy(env_id)
    service.run_next()
    assert runner.calls[-1]["action"] == "destroy"
    assert service.get(env_id)["status"] == "DESTROYED"


def test_http_identity_is_required_after_container_health(tmp_path, node_config):
    runner = executor(tmp_path, node_config)
    runner.plan("env-123456789abc", "onprem", rendered(node_config))
    handoff = runner.apply("env-123456789abc", "onprem", "reviewed.tfplan")
    with patch(
        "control_plane.deployment.provisioner.node.AppChecker.check",
        return_value={"status": "NOT_READY"},
    ) as probe:
        assert runner.check(handoff)["status"] == "NOT_READY"
        assert probe.call_args.kwargs["expected_identity"] == "onprem"


def test_bundle_materializes_private_files_without_changing_cloud_config(tmp_path):
    bundle = {
        "schemaVersion": 1,
        "nodeId": "pc",
        "node": {"secretFiles": {"TOKEN": "app_TOKEN"}},
        "catalog": {"example": True},
        "credentials": {"app_TOKEN": "value"},
    }
    original = {
        "workRoot": str(tmp_path),
        "clouds": {
            "aws": {"existing": True},
            "onprem": {"images": {"chiikawa-groupbuy": "registry/app:existing"}},
        },
    }
    config = install(original, json.dumps(bundle))
    assert "onprem" not in original
    assert config["clouds"]["aws"] == {"existing": True}
    assert config["clouds"]["onprem"]["images"] == original["clouds"]["onprem"]["images"]
    file = Path(config["clouds"]["onprem"]["nodes"]["pc"]["secretFiles"]["TOKEN"])
    assert file.read_text() == "value" and file.stat().st_mode & 0o077 == 0
    bundle["credentials"] = {"../escape": "value"}
    with pytest.raises(spec.RequestError):
        install(original, json.dumps(bundle))


def test_platform_defaults_reach_app_without_overriding_node_configuration(tmp_path, node_config):
    node_config["appEnv"] = {
        "REDIS_URL": "redis://default:6379",
        "OBJECT_MIGRATION_SERVER": "http://object-migration.internal:8080",
    }
    runner = executor(tmp_path, node_config)
    spec = rendered(node_config)
    compose = runner.compose("env-123456789abc", spec, runner.resolve(spec))
    for service in compose["services"].values():
        assert service["environment"]["REDIS_URL"] == "redis://platform-proxy:6379"
        assert (
            service["environment"]["OBJECT_MIGRATION_SERVER"]
            == "http://object-migration.internal:8080"
        )


def test_inspection_rejects_missing_workers_and_wrong_images():
    api = {
        "Config": {"Image": "app:v1", "Labels": {"com.docker.compose.service": "api"}},
        "State": {"Running": True, "Health": {"Status": "healthy"}},
    }
    assert not node_remote.inspect(
        [api], {"api": {"image": "app:v1"}, "worker-1": {"image": "app:v1"}}
    )["ready"]
    assert not node_remote.inspect([api], {"api": {"image": "app:v2"}})["ready"]
    assert node_remote.inspect([api], {"api": {"image": "app:v1"}})["ready"]


def test_shared_docker_project_cannot_delete_another_owners_container():
    row = {"Config": {"Labels": {"foxtrot.deployment": "another-env"}}}
    with (
        patch.object(node_remote, "records", return_value=[row]),
        pytest.raises(node_remote.NodeFailure, match="UNOWNED"),
    ):
        node_remote.checked_records("foxtrot-env-123456789abc", "env-123456789abc")


@pytest.mark.parametrize("connection_result", [errno.ECONNREFUSED, 0, errno.ETIMEDOUT])
def test_port_80_preflight_with_unprivileged_ssh_account(connection_result):
    with patch.object(node_remote.socket, "socket") as socket:
        probe = socket.return_value.__enter__.return_value
        probe.bind.side_effect = [PermissionError(errno.EACCES, "permission denied"), None]
        probe.connect_ex.return_value = connection_result
        if connection_result == errno.ECONNREFUSED:
            node_remote.check_address("100.64.1.2", 80)
        else:
            with pytest.raises(node_remote.NodeFailure, match="APP_ADDRESS_UNAVAILABLE"):
                node_remote.check_address("100.64.1.2", 80)
        assert [call.args[0] for call in probe.bind.call_args_list] == [
            ("100.64.1.2", 80),
            ("100.64.1.2", 0),
        ]


def test_port_preflight_rejects_occupied_or_missing_address():
    for code in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
        with patch.object(node_remote.socket, "socket") as socket:
            probe = socket.return_value.__enter__.return_value
            probe.bind.side_effect = OSError(code, "unavailable")
            with pytest.raises(node_remote.NodeFailure, match="APP_ADDRESS_UNAVAILABLE"):
                node_remote.check_address("100.64.1.2", 80)
            probe.connect_ex.assert_not_called()


def test_reconcile_completed_cleanup_releases_node_without_another_mutation(tmp_path, node_config):
    runner = executor(tmp_path, node_config)
    service = Service(
        Store(tmp_path / "jobs.db"), runner, CATALOG, node_config, checker=ReadyChecker()
    )
    _, accepted = service.create(request(), "first")
    service.run_next()
    service.run_next()
    env_id = accepted["environmentId"]
    service.store.update_environment(env_id, "now", status="NEEDS_RECONCILIATION")
    service.reconcile(env_id)
    with patch.object(runner, "remote", return_value={"released": True}) as remote:
        service.run_next()
    assert remote.call_count == 1 and remote.call_args.args[1]["action"] == "inspect"
    assert service.get(env_id)["status"] == "DESTROYED"
    assert not service.run_next()  # no app check after cleanup
    assert service.create(request(), "replacement")[0] == 202


def test_cleanup_keeps_data_network_and_can_be_inspected_after_disconnect():
    with tempfile.TemporaryDirectory(prefix=".onprem-test-", dir=Path.cwd()) as temporary:
        root = Path(temporary)
        env_id = "env-123456789abc"
        folder = root / env_id
        folder.mkdir()
        (folder / "owner.json").write_text(json.dumps({"environmentId": env_id, "digest": "old"}))
        (folder / "compose.json").write_text("{}")
        payload = {"action": "destroy", "environmentId": env_id, "runtimeRoot": str(root)}
        with (
            patch.object(node_remote, "records", return_value=[]),
            patch.object(node_remote, "docker") as docker,
        ):
            assert node_remote.handle(payload) == {"released": True}
            assert docker.call_args.args[-2:] == ("down", "--remove-orphans")
            assert "-v" not in docker.call_args.args
            docker.reset_mock()
            payload["action"] = "inspect"
            assert node_remote.handle(payload)["released"] is True
            docker.assert_not_called()
        assert not (folder / "compose.json").exists()


def test_network_failure_during_check_preserves_infrastructure_evidence(tmp_path, node_config):
    runner = executor(tmp_path, node_config)
    checker = SimpleNamespace(check=lambda *a: (_ for _ in ()).throw(TerraformError("offline")))
    service = Service(Store(tmp_path / "jobs.db"), runner, CATALOG, node_config, checker=checker)
    _, created = service.create(request(), "first")
    service.run_next()
    service.run_next()
    result = service.get(created["environmentId"])
    assert result["status"] == "INFRA_READY"
    assert result["readiness"]["infrastructure"]["status"] == "READY"
    assert result["readiness"]["application"]["status"] == "UNKNOWN"


def test_node_requires_pinned_ssh_identity_and_private_key(node_config):
    nodes = node_config["clouds"]["onprem"]["nodes"]
    value = rendered(node_config)
    nodes["pc"]["sshHostKey"] = ""
    with pytest.raises(spec.RequestError, match="host key"):
        resolve(nodes, value)
    nodes["pc"]["sshHostKey"] = "ssh-ed25519 AAAA"
    Path(nodes["pc"]["sshKeyFile"]).chmod(0o644)
    with pytest.raises(spec.RequestError, match="private"):
        resolve(nodes, value)


def test_monitoring_is_deployed_on_registered_host(tmp_path, node_config, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.internal:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20test-token")
    runner = executor(tmp_path, node_config)
    runner.telemetry["applicationId"] = "chiikawa"
    document = rendered(node_config)
    node = runner.resolve(document)
    services = runner.compose("env-123456789abc", document, node)["services"]
    assert "otel-system" not in services
    assert all(service.get("network_mode") != "host" for service in services.values())
    assert services["api"]["environment"]["OTEL_APPLICATION_ID"] == "chiikawa"
    assert services["api"]["environment"]["OTEL_SYSTEM_METRICS"] == "container"
    assert services["api"]["environment"]["OTEL_OWNER_ID"] == "testorg"
    assert services["api"]["environment"]["OTEL_DEPLOYMENT_ID"] == document["deployment_id"]
    assert services["worker-1"]["environment"]["OTEL_APP_ROLE"] == "worker"
    document["app"]["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://other"
    with pytest.raises(spec.RequestError, match="OTEL"):
        runner.compose("env-123456789abc", document, node)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS")
    with pytest.raises(spec.RequestError, match="headers"):
        runner.resolve(rendered(node_config))
