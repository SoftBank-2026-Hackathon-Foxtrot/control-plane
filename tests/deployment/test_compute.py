import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.deployment.compute import runner


class TargetPreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "binding.json"
        example = (
            __import__("control_plane.deployment.paths", fromlist=["iac_root"]).iac_root()
            / "compute/binding.example.json"
        )
        self.binding = json.loads(example.read_text(encoding="utf-8"))
        self.path.write_text(json.dumps(self.binding), encoding="utf-8")
        self.path.with_name("web.env").write_text("", encoding="utf-8")

    @patch.object(runner, "docker")
    def test_wrong_target_cannot_touch_docker(self, docker):
        with self.assertRaisesRegex(ValueError, "targetRef"):
            runner.execute("START_SYNC", self.path, "resource:source")
        docker.assert_not_called()

    @patch.object(runner, "docker")
    def test_preflight_only_observes_expected_image(self, docker):
        image = self.binding["imageId"]
        docker.return_value = image
        runner.execute("PREPARE", self.path, self.binding["targetRef"])
        docker.assert_called_once_with("image", "inspect", "--format", "{{.Id}}", image)

    @patch.object(runner, "docker")
    def test_existing_container_with_other_binding_is_left_alone(self, docker):
        docker.side_effect = [self.binding["imageId"], "container", "another-revision"]
        with self.assertRaisesRegex(RuntimeError, "Reconcile"):
            runner.execute("START_SYNC", self.path, self.binding["targetRef"])
        self.assertEqual(
            [call.args[0] for call in docker.call_args_list], ["image", "ps", "inspect"]
        )
