import os
import unittest
from unittest.mock import patch


class OnPremRuntimeTest(unittest.TestCase):
    def test_proxy_only_app_does_not_require_db_administrator_credentials(self):
        from control_plane.deployment.runtime import onprem_entrypoint

        # A Redis-only or fixed-proxy app must launch without APP_DB_PASSWORD/DB_HOST.
        source = onprem_entrypoint(8000, 1)[-1]
        for role in ("api", "w1"):
            with (
                patch.dict(os.environ, {"REDIS_URL": "redis://proxy:6379"}, clear=True),
                patch("sys.argv", ["-c", role]),
                patch("os.execvp") as launch,
            ):
                exec(compile(source, "managed_start.py", "exec"), {})
                self.assertEqual(launch.call_count, 1)
