import base64
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.deployment.paths import iac_root

ROOT = iac_root()


class SqlText(str):
    def format(self, *values):
        return str(self).format(*values)


class ManagedRuntimeTest(unittest.TestCase):
    def test_container_restart_preserves_existing_database_login_fence(self):
        source = (
            Path(
                __import__("control_plane.deployment.paths", fromlist=["__file__"]).__file__
            ).parent
            / "runtime/bootstrap_db.py"
        ).read_text()
        for exists in (True, False):
            admin = Mock()
            admin.execute.return_value.fetchone.return_value = (1,) if exists else None
            psycopg = SimpleNamespace(
                connect=Mock(return_value=admin),
                OperationalError=RuntimeError,
                Error=RuntimeError,
                sql=SimpleNamespace(SQL=SqlText, Identifier=str, Literal=repr),
            )
            env = dict(
                DB_HOST="private-db",
                DB_PORT="5432",
                DB_NAME="app",
                DB_ADMIN_USER="admin",
                DB_ADMIN_PASSWORD="test",
                APP_DB_USER="app",
                APP_DB_PASSWORD="test",
            )
            with (
                patch.dict(os.environ, env, clear=True),
                patch.dict("sys.modules", {"psycopg": psycopg}),
            ):
                exec(compile(source, "bootstrap_db.py", "exec"), {})
            statements = [str(c.args[0]) for c in admin.execute.call_args_list]
            if exists:
                self.assertTrue(any(s.startswith("ALTER ROLE app PASSWORD") for s in statements))
                self.assertFalse(any(" LOGIN" in s for s in statements))
            else:
                self.assertTrue(
                    any(s.startswith("CREATE ROLE app LOGIN PASSWORD") for s in statements)
                )

    def test_managed_start_passes_secret_values_only_to_child_environment(self):
        source = (
            Path(
                __import__("control_plane.deployment.paths", fromlist=["__file__"]).__file__
            ).parent
            / "runtime/start.py.tmpl"
        ).read_text()
        source = (
            source.replace("${bootstrap}", base64.b64encode(b"pass").decode())
            .replace("${port}", "8000")
            .replace("${api_workers}", "4")
        )
        for mode, expected in [("api", "uvicorn"), ("w1", "python")]:
            env = {
                "APP_DB_PASSWORD": "a@b",
                "DATABASE_URL": "postgresql://app:__APP_DB_PASSWORD__@db/app",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("sys.argv", ["-c", mode]),
                patch("os.execvp") as launch,
            ):
                exec(compile(source, "managed_start.py", "exec"), {})
                self.assertEqual(launch.call_args.args[0], expected)
                self.assertEqual(os.environ["DATABASE_URL"], "postgresql://app:a%40b@db/app")
                self.assertNotIn("a@b", " ".join(launch.call_args.args[1]))
                if mode == "w1":
                    self.assertIn("--reconciler", launch.call_args.args[1])
