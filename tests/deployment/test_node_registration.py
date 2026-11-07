"""A missing/broken optional node registration must not stop cloud deployment."""

import copy
import json

import pytest

from control_plane.deployment.provisioner import __main__ as provisioner
from tests.deployment.test_provisioner import CATALOG, CONFIG, FakeTerraform, ReadyChecker, body


@pytest.mark.parametrize(
    "problem", ["absent", "json", "permissions", "symlink", "node", "settings", "driver"]
)
def test_cloud_startup_and_deployment_survive_registration_errors(
    tmp_path, monkeypatch, caplog, problem
):
    config = copy.deepcopy(CONFIG)
    config["workRoot"] = str(tmp_path)
    config["clouds"].pop("onprem", None)
    source = tmp_path / "onprem-registration" / "bundle.json"
    secret = "secret-must-not-appear-in-logs"
    if problem in {"json", "permissions", "symlink", "node"}:
        source.parent.mkdir(mode=0o700)
        if problem == "symlink":
            target = tmp_path / "private.json"
            target.write_text(secret)
            target.chmod(0o600)
            source.symlink_to(target)
        else:
            source.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "nodeId": "pc",
                        "node": {"orgs": secret},
                        "catalog": {},
                        "credentials": {},
                    }
                )
                if problem == "node"
                else secret
            )
            source.chmod(0o644 if problem == "permissions" else 0o600)
    elif problem == "settings":
        config["onprem"] = {"catalogFile": secret}  # missing secretRoot
    elif problem == "driver":
        config["onprem"] = {"catalogFile": str(tmp_path / "catalog"), "secretRoot": str(tmp_path)}
        key = tmp_path / "ansible" / "plan-seal.key"
        key.parent.mkdir()
        key.write_text(secret)  # invalid seal; fail during optional driver startup
        key.chmod(0o600)
    original = copy.deepcopy(config)
    fake = FakeTerraform(tmp_path)
    monkeypatch.setattr(provisioner, "Terraform", lambda *a, **kw: fake)
    # The old ECS bundle environment variable is no longer a startup input.
    monkeypatch.setenv("PROVISIONER_ONPREM_BUNDLE", secret)
    engine = provisioner.build(config, CATALOG)
    try:
        assert config == original
        assert engine.config["clouds"] == original["clouds"]
        assert engine.onprem is None and engine.tf.nodes == {}
        assert bool(engine.onprem_error) == (problem != "absent")
        assert secret not in caplog.text
        assert source.exists() or problem in {"absent", "settings", "driver"}
        engine.checker = ReadyChecker()
        engine.recover()
        environments = [
            engine.create(body(cloud=cloud))[1]["environmentId"] for cloud in ("aws", "gcp")
        ]
        while engine.run_next():
            pass
        assert [engine.get(env)["status"] for env in environments] == ["READY", "READY"]
        assert len(fake.calls) == 4
    finally:
        engine.store.db.close()


def test_onprem_error_does_not_hide_invalid_cloud_configuration(tmp_path):
    # The isolation boundary must not silently rewrite or forgive base platform config.
    with pytest.raises(KeyError):
        provisioner.build({"clouds": {}}, CATALOG)
