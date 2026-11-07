import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

from control_plane.deployment.paths import iac_root

ROOT = iac_root()
spec = importlib.util.spec_from_file_location(
    "render_inputs",
    Path(
        __import__("control_plane.deployment.tools.render_inputs", fromlist=["__file__"]).__file__
    ),
)
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


class InputsTest(unittest.TestCase):
    def test_managed_runtime_allocations_and_reserved_environment(self):
        for cloud in ("aws", "gcp"):
            base = self.base(cloud)
            base["app"].update(entrypoint=["python", "-m", "app.main"], cpu=1, memory_mib=2048)
            inputs.validate_config(cloud, base)
            for changes in (
                {"cpu": True},
                {"cpu": 3},
                {"memory_mib": 100},
                {"env": {"APP_DB_PASSWORD": "unsafe"}},
            ):
                bad = copy.deepcopy(base)
                bad["app"].update(changes)
                with self.assertRaises(ValueError):
                    inputs.validate_config(cloud, bad)

    def test_legacy_baseline_without_app_still_accepts_sizing_changes(self):
        for cloud in ("aws", "gcp"):
            baseline = self.base(cloud)
            baseline.pop("app")
            if cloud == "aws":
                baseline["postgres_version"] = "16"
            rendered = inputs.merge_overrides(cloud, baseline, {"compute_disk_gib": 40})
            self.assertNotIn("app", rendered)
            self.assertEqual(rendered["sizing"]["compute_disk_gib"], 40)
            baseline["app"] = {}
            with self.assertRaises(ValueError):
                inputs.validate_config(cloud, baseline)

    def base(self, cloud="aws"):
        return inputs.load_json(ROOT / f"environments/{cloud}/deployment.example.tfvars.json")

    def proposal(self, params, cloud="aws"):
        return {
            "cloud": cloud,
            "parameters": params,
            "rationale": {k: "Observed workload requirement" for k in params},
        }

    def test_management_baseline_is_preserved_and_not_ai_editable(self):
        for cloud in ("aws", "gcp"):
            base = self.base(cloud)
            base.update(proxy_cidrs=["100.64.0.10/32"], enable_cloudflare_tunnel=True)
            if cloud == "aws":
                base["migration_source_cidrs"] = ["10.20.10.5/32"]
            inputs.validate_config(cloud, base)
            result = inputs.merge_overrides(cloud, base, {"compute_disk_gib": 40})
            self.assertEqual(result["proxy_cidrs"], base["proxy_cidrs"])
            for key in ("proxy_cidrs", "migration_source_cidrs", "enable_cloudflare_tunnel"):
                with self.assertRaises(ValueError):
                    inputs.merge_overrides(cloud, base, {key: True})
            for cidr in ("0.0.0.0/0", "8.8.8.8/32", "10.0.0.0/8", "::/0"):
                bad = copy.deepcopy(base)
                bad["proxy_cidrs"] = [cidr]
                with self.assertRaises(ValueError):
                    inputs.validate_config(cloud, bad)

    def test_examples_and_partial_merge_preserve_human_settings(self):
        for cloud in ("aws", "gcp"):
            with self.subTest(cloud=cloud):
                base = self.base(cloud)
                original = copy.deepcopy(base)
                out = inputs.merge_recommendation(
                    cloud, base, self.proposal({"compute_disk_gib": 40}, cloud)
                )
                self.assertEqual(base, original)
                self.assertEqual(out["sizing"]["compute_disk_gib"], 40)
                self.assertEqual(
                    {k: v for k, v in out.items() if k != "sizing"},
                    {k: v for k, v in base.items() if k != "sizing"},
                )

    def test_ai_cannot_override_security_identity_or_metadata(self):
        for key, value in {
            "admin_cidrs": ["0.0.0.0/0"],
            "deletion_protection": False,
            "iam_policy": "admin",
            "metadata": {},
            "region": "x",
            "user_data": "run",
        }.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                inputs.merge_recommendation("aws", self.base(), self.proposal({key: value}))

    def test_unknown_top_level_and_cross_cloud_rejected(self):
        p = self.proposal({"compute_disk_gib": 40})
        p["network"] = {}
        with self.assertRaises(ValueError):
            inputs.merge_recommendation("aws", self.base(), p)
        with self.assertRaises(ValueError):
            inputs.merge_recommendation(
                "aws", self.base(), self.proposal({"compute_disk_gib": 40}, "gcp")
            )

    def test_invalid_types_sizes_and_catalog_values_rejected(self):
        for params in [
            {"compute_disk_gib": True},
            {"compute_disk_gib": "40"},
            {"compute_disk_gib": 40.5},
            {"compute_disk_gib": 100000},
            {"compute_instance_type": "p5.48xlarge"},
            {"cache_node_count": 0},
            {"db_multi_az": "false"},
            {"db_storage_gib": 200},
        ]:
            with self.subTest(params=params), self.assertRaises(ValueError):
                inputs.merge_recommendation("aws", self.base(), self.proposal(params))

    def test_unexplained_recommendation_rejected(self):
        p = self.proposal({"compute_disk_gib": 40})
        p["rationale"] = {}
        with self.assertRaises(ValueError):
            inputs.merge_recommendation("aws", self.base(), p)

    def test_missing_baseline_sizing_cannot_reset_to_defaults(self):
        base = self.base()
        del base["sizing"]["db_multi_az"]
        with self.assertRaises(ValueError):
            inputs.merge_recommendation("aws", base, self.proposal({"compute_disk_gib": 40}))

    def test_cidr_overlap_and_outside_vpc_rejected(self):
        for cidr in ("10.10.0.0/24", "10.99.0.0/24"):
            base = self.base()
            base["network"]["private_subnet_cidrs"][0] = cidr
            with self.assertRaises(ValueError):
                inputs.validate_config("aws", base)
        base = self.base("gcp")
        base["network"]["service_range_address"] = "10.20.0.0"
        with self.assertRaises(ValueError):
            inputs.validate_config("gcp", base)

    def test_open_ssh_and_database_public_port_rejected(self):
        base = self.base()
        base["admin_cidrs"] = ["0.0.0.0/0"]
        with self.assertRaises(ValueError):
            inputs.validate_config("aws", base)
        base = self.base()
        base["web_ports"] = [5432]
        with self.assertRaises(ValueError):
            inputs.validate_config("aws", base)

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "bad.json"
            for text in ('{"cloud":"aws","cloud":"gcp"}', '{"value":NaN}'):
                file.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    inputs.load_json(file)

    def test_secret_cannot_be_added_to_baseline(self):
        base = self.base()
        base["redis_auth_token"] = "do-not-store-secrets"
        with self.assertRaises(ValueError):
            inputs.validate_config("aws", base)


if __name__ == "__main__":
    unittest.main()
