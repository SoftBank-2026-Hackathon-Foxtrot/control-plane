"""Run Terraform for one environment in its own workspace.

Each environment gets a copy of the cloud's root and the modules (so ../../modules resolves),
its own tfvars and its own state: a local file in the workspace, or a key in the S3 state
bucket when one is configured. Plans are saved and applied as reviewed, never re-planned."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

IGNORE = shutil.ignore_patterns(
    ".terraform",
    "tests",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfplan",
    "*.tfvars.json",
    "backend_override.tf",
)


class TerraformError(RuntimeError):
    """A Terraform command failed; the message is the tail of its output."""


class Terraform:
    def __init__(
        self,
        repo_root: Path,
        work_root: Path,
        *,
        binary: str = "terraform",
        state: dict | None = None,
    ):
        self.repo_root, self.work_root, self.binary, self.state = (
            repo_root,
            work_root,
            binary,
            state,
        )

    def workspace(self, env_id: str, cloud: str) -> Path:
        return self.work_root / env_id / "environments" / cloud

    def log_path(self, env_id: str) -> Path:
        return self.work_root / env_id / "terraform.log"

    def prepare(self, env_id: str, cloud: str, tfvars: dict) -> Path:
        """Copy the code (state and plans in the workspace are kept) and write this environment's inputs."""
        base = self.work_root / env_id
        shutil.copytree(
            self.repo_root / "environments" / cloud,
            base / "environments" / cloud,
            ignore=IGNORE,
            dirs_exist_ok=True,
        )
        shutil.copytree(
            self.repo_root / "modules", base / "modules", ignore=IGNORE, dirs_exist_ok=True
        )
        ws = self.workspace(env_id, cloud)
        (ws / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2, ensure_ascii=False))
        if self.state:
            (ws / "backend_override.tf").write_text(
                'terraform {\n  backend "s3" {\n'
                f'    bucket       = "{self.state["bucket"]}"\n'
                f'    key          = "{self.state.get("prefix", "provisioner")}/{env_id}.tfstate"\n'
                f'    region       = "{self.state["region"]}"\n'
                "    encrypt      = true\n    use_lockfile = true\n  }\n}\n"
            )
        return ws

    def _run(self, env_id: str, ws: Path, *args: str, capture: bool = False) -> str:
        env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        with self.log_path(env_id).open("a") as log:
            log.write(f"\n$ terraform {' '.join(args)}\n")
            log.flush()
            proc = subprocess.run(
                [self.binary, *args],
                cwd=ws,
                env=env,
                text=True,
                stdout=subprocess.PIPE if capture else log,
                stderr=log,
            )
        if proc.returncode != 0:
            tail = self.log_path(env_id).read_text()[-2000:]
            raise TerraformError(f"terraform {args[0]} failed:\n{tail}")
        return proc.stdout if capture else ""

    def plan(self, env_id: str, cloud: str, tfvars: dict, *, destroy: bool = False) -> dict:
        ws = self.prepare(env_id, cloud, tfvars)
        self._run(env_id, ws, "init", "-input=false", "-no-color", "-reconfigure")
        name = "destroy.tfplan" if destroy else "reviewed.tfplan"
        self._run(
            env_id,
            ws,
            "plan",
            "-input=false",
            "-no-color",
            "-lock-timeout=60s",
            f"-out={name}",
            *(["-destroy"] if destroy else []),
        )
        shown = json.loads(self._run(env_id, ws, "show", "-json", name, capture=True))
        counts: dict[str, int] = {}
        for change in shown.get("resource_changes", []):
            action = "+".join(change["change"]["actions"])
            if action != "no-op":
                counts[action] = counts.get(action, 0) + 1
        return {"plan": name, "changes": counts}

    def apply(self, env_id: str, cloud: str, plan: str) -> dict | None:
        ws = self.workspace(env_id, cloud)
        self._run(env_id, ws, "apply", "-input=false", "-no-color", "-lock-timeout=60s", plan)
        if plan == "destroy.tfplan":
            return None
        return json.loads(self._run(env_id, ws, "output", "-json", "handoff", capture=True))
