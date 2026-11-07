"""Run the actual bootstrap Python with fake cloud APIs and Tailscale commands."""

import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TEMPLATE = (
    __import__("control_plane.deployment.paths", fromlist=["iac_root"]).iac_root()
    / "modules/tailscale_bootstrap/router.sh.tftpl"
).read_text()
CONFIGURE = TEMPLATE.split("<<'PYTHON'\n", 1)[1].split("\nPYTHON\n", 1)[0]


class BootstrapTests(unittest.TestCase):
    def execute(self, cloud="aws", running=False, fail_login=False):
        config = {
            "name": "foxtrot-router",
            "cloud": cloud,
            "region": "ap-northeast-2",
            "secret_ref": "projects/test/secrets/router" if cloud == "gcp" else "arn:test",
            "advertised_cidrs": ["10.20.0.0/16"],
        }
        secret = b"test-only-not-a-real-auth-key"
        commands = []
        with tempfile.TemporaryDirectory() as folder:
            key_path = Path(folder) / "auth"
            source = CONFIGURE.replace(
                "${config}", base64.b64encode(json.dumps(config).encode()).decode()
            )
            source = source.replace(
                'Path("/run/foxtrot-tailscale-auth")', f"Path({str(key_path)!r})"
            )

            def run(args, **kwargs):
                commands.append(args)
                self.assertNotIn(secret.decode(), " ".join(args))
                if args[:2] == ["tailscale", "status"]:
                    state = "Running" if running else "NeedsLogin"
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"BackendState": state}))
                if args[:2] == ["tailscale", "up"]:
                    self.assertEqual(key_path.read_bytes(), secret)
                    self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
                    self.assertIn("--snat-subnet-routes=false", args)
                    if fail_login:
                        raise subprocess.CalledProcessError(1, args)
                return SimpleNamespace(returncode=0)

            responses = [
                io.BytesIO(b'{"access_token":"test-only-oauth-token"}'),
                io.BytesIO(
                    json.dumps({"payload": {"data": base64.b64encode(secret).decode()}}).encode()
                ),
            ]
            with (
                patch("subprocess.run", side_effect=run),
                patch("subprocess.check_output", return_value=secret) as aws,
                patch("urllib.request.urlopen", side_effect=responses) as gcp,
            ):
                if fail_login:
                    with self.assertRaises(subprocess.CalledProcessError):
                        exec(compile(source, "configure.py", "exec"), {})
                else:
                    exec(compile(source, "configure.py", "exec"), {})
                if running:
                    aws.assert_not_called()
                    gcp.assert_not_called()
                    self.assertIn("--accept-routes=true", commands[-1])
                elif cloud == "aws":
                    self.assertEqual(aws.call_args.args[0][1], "secretsmanager")
                else:
                    self.assertEqual(gcp.call_count, 2)
            self.assertFalse(key_path.exists(), "Auth key must be removed after success or failure")

    def test_aws_login_fetches_private_key_file_and_removes_it(self):
        self.execute()

    def test_gcp_login_uses_metadata_identity_and_secret_manager(self):
        self.execute(cloud="gcp")

    def test_reboot_with_live_node_needs_no_auth_key(self):
        self.execute(running=True)

    def test_login_failure_removes_key_and_propagates_for_systemd_retry(self):
        self.execute(fail_login=True)
