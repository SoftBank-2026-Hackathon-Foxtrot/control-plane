import json
import unittest
from datetime import UTC, datetime

from control_plane.deployment.tools.report_observations import (
    compute_report,
    infra_report,
    plan_counts,
    provisioner_reports,
)


class ObservationReportTests(unittest.TestCase):
    def test_failure_before_plan_and_interrupted_job_can_be_reported(self):
        for status in ("FAILED", "NEEDS_RECONCILIATION"):
            job = {
                "id": "j",
                "migration_id": "w",
                "type": "PLAN",
                "status": status,
                "observed_at": "2026-01-01T00:00:00+00:00",
            }
            report = infra_report("w", job)
            self.assertEqual(report["data"]["status"], status)
            self.assertIsNone(report["data"]["changes"])

    def test_provisioner_keeps_infra_success_and_app_failure_separate(self):
        at = "2026-01-01T00:00:00+00:00"
        environment = {
            "workflowId": "w",
            "environmentId": "e",
            "cloud": "gcp",
            "status": "APP_NOT_READY",
            "readiness": {
                "infrastructure": {"status": "READY", "observedAt": at, "jobId": "apply-job"},
                "application": {"status": "NOT_READY", "observedAt": at, "httpStatus": 503},
            },
            "handoff": {"compute": {"id": "vm"}, "database": {"password": "secret"}},
            "plan": {"changes": {"delete+create": 1}},
        }
        job = {
            "jobId": "check-job",
            "environmentId": "e",
            "kind": "CHECK_APP",
            "status": "FAILED",
            "logTail": "secret",
        }
        reports = provisioner_reports("w", environment, job)
        self.assertEqual([report["resource"] for report in reports], ["INFRA"])
        infra = reports[0]
        self.assertEqual(infra["data"]["status"], "SUCCEEDED")
        self.assertIn("APP=NOT_READY", infra["data"]["check"][1])
        self.assertEqual(infra["data"]["jobId"], "apply-job")
        self.assertEqual(infra["data"]["changes"]["replace"], 1)
        self.assertIn(at, infra["data"]["check"][1])
        self.assertNotIn("secret", str(reports))
        with self.assertRaises(ValueError):
            provisioner_reports("different", environment, job)

    def test_replacements_are_not_double_counted(self):
        plan = {
            "format_version": "1.2",
            "resource_changes": [
                {"change": {"actions": actions}}
                for actions in (["delete", "create"], ["create"], ["no-op"], ["update"])
            ],
        }
        self.assertEqual(plan_counts(plan), {"create": 1, "update": 1, "delete": 0, "replace": 1})

    def test_examples_do_not_prove_completed_infrastructure(self):
        example = json.loads(
            (
                __import__("control_plane.deployment.paths", fromlist=["iac_root"]).iac_root()
                / "contracts/examples/plan-job-response.json"
            ).read_text()
        )
        with self.assertRaises(KeyError):
            infra_report(example["migration_id"], example, {})

    def test_compute_report_does_not_expose_environment_or_refresh_evidence(self):
        at = "2025-01-01T00:00:00+00:00"
        state = {
            "workflowId": "w",
            "migrationId": "m",
            "kind": "COMPUTE",
            "lifecycle": "SYNCING",
            "health": "OK",
            "observed": {"observedAt": at},
            "details": {"compute": {"imageId": "sha256:x", "env": {"TOKEN": "secret"}}},
        }
        report = compute_report("w", state)
        self.assertEqual(report["data"]["observedAt"], at)
        self.assertNotIn("secret", str(report))
        with self.assertRaises(ValueError):
            compute_report("different", state)

    def test_success_requires_finished_at(self):
        job = {
            "id": "job",
            "migration_id": "w",
            "type": "APPLY",
            "status": "SUCCEEDED",
            "observed_at": datetime.now(UTC).isoformat(),
        }
        with self.assertRaises(ValueError):
            infra_report("w", job, {"format_version": "1.2", "resource_changes": []})
