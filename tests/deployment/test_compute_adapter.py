import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.deployment.compute.adapter import Adapter, Problem
from control_plane.deployment.paths import iac_root

ROOT = iac_root()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        self.binding = path / "binding.json"
        self.binding.write_bytes((ROOT / "compute/binding.example.json").read_bytes())
        (path / "web.env").touch()
        self.plan = json.loads((ROOT / "compute/plan.example.json").read_text())
        self.db = path / "compute.db"
        self.handoff = json.loads(
            (ROOT / "contracts/examples/gcp-handoff-response.json").read_text()
        )
        self.adapter = Adapter(self.db, self.binding, self.handoff)

    def create(self):
        return self.adapter.post(None, "create", self.plan)

    def test_unsupported_plan_and_unregistered_target(self):
        for overrides in ({"targetRef": "resource:unregistered"}, {"profile": "full-cutover"}):
            with self.assertRaises(Problem) as error:
                self.adapter.post(None, "create", {**self.plan, **overrides})
            self.assertEqual(error.exception.status, 422)

    @patch("control_plane.deployment.compute.adapter.runner.execute", return_value={"check": "ok"})
    def test_retries_return_original_result_after_revision_changes(self, execute):
        created = self.create()
        item = created[2]["migrationId"]
        body = {"action": "PREPARE", "expectedRevision": 1}
        accepted = self.adapter.post(item, "prepare", body)
        self.assertTrue(self.adapter.run_next())
        operation = self.adapter.get("operations", accepted[2]["operationId"])
        self.assertIsNotNone(operation["requestedAt"])
        self.assertIsNotNone(operation["startedAt"])
        self.assertIsNotNone(operation["finishedAt"])
        self.assertLessEqual(operation["startedAt"], operation["finishedAt"])
        self.assertEqual(created, self.create())
        self.assertEqual(accepted, self.adapter.post(item, "prepare", body))
        self.assertFalse(self.adapter.run_next())
        execute.assert_called_once()
        with self.assertRaises(Problem) as error:
            self.adapter.post(item, "prepare", {**body, "action": "START_SYNC"})
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(Problem) as error:
            self.adapter.post(item, "different-key", body)
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")

    @patch("control_plane.deployment.compute.adapter.runner.execute")
    def test_interrupted_start_is_observed_instead_of_reissued(self, execute):
        execute.return_value = {"check": "ok"}
        item = self.create()[2]["migrationId"]

        def act(name):
            rev = self.adapter.get("migrations", item)["revision"]
            return self.adapter.post(item, name, {"action": name, "expectedRevision": rev})[2][
                "operationId"
            ]

        act("PREPARE")
        self.adapter.run_next()
        pending = act("START_SYNC")
        with self.adapter.transaction() as db:
            db.execute("UPDATE operations SET status='RUNNING' WHERE id=?", (pending,))
        self.adapter = Adapter(self.db, self.binding, self.handoff)
        self.assertEqual(self.adapter.get("operations", pending)["status"], "UNKNOWN")
        self.assertEqual(self.adapter.get("migrations", item)["allowedActions"], ["RECONCILE"])
        self.assertFalse(self.adapter.run_next())
        act("RECONCILE")
        self.adapter.run_next()
        self.assertEqual(execute.call_args.args[0], "START_SYNC")
        self.assertTrue(execute.call_args.kwargs["observe_only"])
        state = self.adapter.get("migrations", item)
        self.assertEqual(state["lifecycle"], "SYNCING")
        self.assertEqual(state["readiness"]["advance"]["result"], "UNKNOWN")
