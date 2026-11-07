"""Validate the managed-environment contract and its executable input boundary."""

import copy
import importlib.util
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from control_plane.deployment.paths import iac_root

ROOT = iac_root()
SPEC = yaml.safe_load((ROOT / "contracts/provisioning.openapi.yaml").read_text(encoding="utf-8"))


def load_tool(name):
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__import__(f"control_plane.deployment.tools.{name}", fromlist=["__file__"]).__file__),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INPUTS = load_tool("render_inputs")


def validator(name):
    return Draft202012Validator(
        {"$ref": "#/components/schemas/" + name, "components": SPEC["components"]},
        format_checker=FormatChecker(),
    )


def fixture(name):
    return json.loads((ROOT / f"contracts/examples/{name}.json").read_text(encoding="utf-8"))


def baseline(cloud):
    return json.loads(
        (ROOT / f"environments/{cloud}/deployment.example.tfvars.json").read_text(encoding="utf-8")
    )


class ContractTests(unittest.TestCase):
    def test_openapi_is_valid_and_examples_conform(self):
        validate(SPEC)
        for item in fixture("index"):
            with self.subTest(file=item["file"]):
                value = json.loads((ROOT / "contracts" / item["file"]).read_text(encoding="utf-8"))
                validator(item["schema"]).validate(value)

    def test_target_ui_schema_matches_reviewed_catalog(self):
        for cloud in ("aws", "gcp"):
            with self.subTest(cloud=cloud):
                context = fixture(f"{cloud}-context-response")
                schema = context["sizing_schema"]
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(context["target_sizing"])
                self.assertEqual(schema["properties"], INPUTS.POLICY["clouds"][cloud])

    def test_requests_cannot_select_code_identity_or_network(self):
        for cloud in ("aws", "gcp"):
            for key in (
                "terraform_code",
                "root",
                "state",
                "cloud_credentials",
                "source_id",
                "target_id",
                "network",
                "iam",
                "redis_auth_token",
                "ami_id",
                "image",
            ):
                for location in ("top", "overrides"):
                    with self.subTest(cloud=cloud, key=key, location=location):
                        request = fixture(f"{cloud}-plan-request")
                        container = request if location == "top" else request["overrides"]
                        container[key] = "untrusted"
                        self.assertFalse(validator("PlanRequest").is_valid(request))

    def test_target_cloud_validation_catches_other_cloud_fields(self):
        # The shared wire schema accepts either cloud. Actual target validation is mandatory.
        for target, other in (("aws", "gcp"), ("gcp", "aws")):
            request = fixture(f"{other}-plan-request")
            self.assertTrue(validator("PlanRequest").is_valid(request))
            with self.assertRaises(ValueError):
                INPUTS.merge_overrides(target, baseline(target), request["overrides"])

    def test_partial_and_empty_overrides_preserve_managed_settings(self):
        for cloud in ("aws", "gcp"):
            original = baseline(cloud)
            before = copy.deepcopy(original)
            self.assertTrue(validator("PlanRequest").is_valid({"overrides": {}}))
            self.assertEqual(INPUTS.merge_overrides(cloud, original, {}), original)
            result = INPUTS.merge_overrides(cloud, original, {"compute_disk_gib": 40})
            expected = copy.deepcopy(original)
            expected["sizing"]["compute_disk_gib"] = 40
            self.assertEqual(result, expected)
            self.assertEqual(original, before)

    def test_invalid_values_and_merged_capacity_are_rejected(self):
        for cloud, overrides in (
            ("aws", {"compute_instance_type": "p5.48xlarge"}),
            ("gcp", {"cache_memory_gib": 999}),
            ("aws", {"db_multi_az": "false"}),
            ("gcp", {"compute_disk_gib": None}),
        ):
            self.assertFalse(validator("PlanRequest").is_valid({"overrides": overrides}))
            with self.assertRaises(ValueError):
                INPUTS.merge_overrides(cloud, baseline(cloud), overrides)
        for cloud in ("aws", "gcp"):
            with self.assertRaisesRegex(ValueError, "110%"):
                INPUTS.merge_overrides(cloud, baseline(cloud), {"db_storage_gib": 200})

    def test_handoff_never_claims_app_readiness(self):
        for cloud in ("aws", "gcp"):
            value = fixture(f"{cloud}-handoff-response")
            value["readiness"] = "APPLICATION_READY"
            self.assertFalse(validator("Handoff").is_valid(value))

    def test_execution_uses_plan_id_and_idempotency_without_new_inputs(self):
        operation = SPEC["paths"]["/plans/{plan_id}/apply"]["post"]
        self.assertNotIn("requestBody", operation)
        for path in ("/migrations/{migration_id}/plans", "/plans/{plan_id}/apply"):
            params = SPEC["paths"][path]["post"]["parameters"]
            self.assertIn({"$ref": "#/components/parameters/IdempotencyKey"}, params)
        self.assertEqual(SPEC["security"], [{"platformBearer": []}])

    def test_generated_files_are_current_and_no_old_examples_remain(self):
        artifacts = load_tool("build_contract").make_artifacts()
        for path, content in artifacts.items():
            with self.subTest(path=path):
                self.assertEqual((ROOT / path).read_text(encoding="utf-8"), content)
        actual = {
            p.relative_to(ROOT).as_posix() for p in (ROOT / "contracts/examples").glob("*.json")
        }
        expected = {p for p in artifacts if p.startswith("contracts/examples/")}
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
