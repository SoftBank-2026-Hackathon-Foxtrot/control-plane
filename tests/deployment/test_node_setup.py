"""Generate and load a registration bundle, without Docker, SSH or cloud API calls."""

import json
import sys
from pathlib import Path

import pytest

from control_plane.deployment.provisioner.__main__ import build
from control_plane.deployment.provisioner.inventory import Inventory
from control_plane.deployment.provisioner.node_bundle import install
from control_plane.deployment.tools import setup_onprem


def test_setup_bundle_round_trip_with_existing_cloud_configuration(tmp_path, monkeypatch, capsys):
    app = tmp_path / "app.json"
    app.write_text(
        json.dumps(
            {
                "imagePrefixes": ["ghcr.io/foxtrot/"],
                "environment": {"CONTROL_PLANE_URL": "http://platform.internal:8000"},
            }
        )
    )
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"WORKLOAD_TOKEN": "test-workload-token"}))
    secrets.chmod(0o600)
    images = tmp_path / "images.json"
    images.write_text(
        json.dumps({name: name + "@sha256:" + "a" * 64 for name in setup_onprem.IMAGES})
    )
    config = tmp_path / "platform.json"
    config.write_text(
        json.dumps(
            {"workRoot": str(tmp_path / "work"), "clouds": {"aws": {"accounts": ["existing"]}}}
        )
    )
    output = tmp_path / "generated"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_onprem",
            "--address",
            "100.64.1.2",
            "--ssh-user",
            "deploy",
            "--host-key",
            "ssh-ed25519 AAAA",
            "--uid",
            "1000",
            "--gid",
            "1000",
            "--org",
            "app-1234567890",
            "--node-id",
            "home-pc",
            "--output",
            str(output),
            "--app-image",
            "ghcr.io/foxtrot/app@sha256:" + "b" * 64,
            "--app-config",
            str(app),
            "--app-secrets",
            str(secrets),
            "--images-file",
            str(images),
            "--platform-config",
            str(config),
        ],
    )
    original_command = setup_onprem.command
    calls = []

    def command(argv):
        calls.append(argv[0])
        assert argv[0] in {"ssh-keygen", "openssl"}
        return original_command(argv)

    monkeypatch.setattr(setup_onprem, "command", command)
    setup_onprem.main()
    assert calls == ["ssh-keygen", "openssl"]
    raw = (output / "platform-bundle.json").read_text()
    bundle = json.loads(raw)
    assert (output / "platform-bundle.json").stat().st_mode & 0o077 == 0
    assert "test-workload-token" not in capsys.readouterr().out
    assert bundle["node"]["orgs"] == ["app-1234567890"]
    merged = install(json.loads(config.read_text()), raw)
    assert merged["clouds"]["aws"] == {"accounts": ["existing"]}
    expected = {"chiikawa-groupbuy": "ghcr.io/foxtrot/app@sha256:" + "b" * 64}
    assert merged["clouds"]["onprem"]["images"] == expected
    assert (
        json.loads((output / "config.json").read_text())["clouds"]["onprem"]["images"] == expected
    )
    assert bundle["images"] == expected
    settings = merged["onprem"]
    inventory = Inventory(settings["catalogFile"], settings["secretRoot"])
    resolved = inventory.resolve(json.loads((output / "request.json").read_text()))
    material = inventory.secrets(resolved)
    assert material["secret:ssh"].startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    assert len(material) == 13
    node = merged["clouds"]["onprem"]["nodes"]["home-pc"]
    assert Path(node["secretFiles"]["WORKLOAD_TOKEN"]).read_text() == "test-workload-token"
    registration = Path(merged["workRoot"]) / "onprem-registration" / "bundle.json"
    registration.write_text(raw)
    registration.chmod(0o600)
    engine = build(json.loads(config.read_text()), {})
    try:
        assert engine.onprem_error is None
        assert engine.onprem is not None
        assert engine.config["clouds"]["aws"] == {"accounts": ["existing"]}
        assert engine.tf.nodes["home-pc"]["orgs"] == ["app-1234567890"]
        assert engine.onprem.catalog.resolve(json.loads((output / "request.json").read_text()))
    finally:
        engine.store.db.close()
        if engine.onprem:
            engine.onprem.store.db.close()


@pytest.mark.parametrize("image", [None, "ghcr.io/other/app:v1", "ghcr.io/foxtrot-evil/app:v1"])
def test_invalid_fixed_image_rejected_before_generating_credentials(tmp_path, monkeypatch, image):
    app = tmp_path / "app.json"
    app.write_text(json.dumps({"imagePrefixes": ["ghcr.io/foxtrot/"]}))
    output = tmp_path / "generated"
    argv = [
        "setup_onprem",
        "--address",
        "100.64.1.2",
        "--host-key",
        "ssh-ed25519 AAAA",
        "--app-config",
        str(app),
        "--output",
        str(output),
    ]
    if image is not None:
        argv.extend(["--app-image", image])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(
        setup_onprem, "command", lambda _: pytest.fail("must not run credential or Docker tools")
    )
    with pytest.raises(SystemExit) as error:
        setup_onprem.main()
    assert error.value.code == 2
    assert not output.exists()
