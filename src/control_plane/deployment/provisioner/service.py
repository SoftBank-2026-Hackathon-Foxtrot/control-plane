"""Environment lifecycle: accept a request, then plan → apply (or destroy) in one background worker.

Status of an environment:
  PLANNING → PLANNED (mode=plan, waiting for /apply) → APPLYING → INFRA_READY
  CHECK_APP: VERIFYING_APP → READY / APP_NOT_READY (infrastructure evidence is retained)
  any step can end in FAILED; a job cut off by a restart ends in NEEDS_RECONCILIATION
  READY/PLANNED/FAILED → DESTROYING → DESTROYED

Jobs run one at a time. Terraform holds the state lock, and one worker keeps the cloud calls
and the log readable; environment creation takes minutes either way."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import threading
import uuid
from contextlib import suppress
from functools import wraps

from opentelemetry.trace import Status, StatusCode

from control_plane.deployment.tools.telemetry import get_telemetry

from . import spec
from .drivers.terraform import TerraformDriver
from .readiness import AppChecker
from .store import Store
from .terraform import Terraform, TerraformError


class Conflict(Exception):
    """409 with a machine-readable code."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra


class NotFound(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def admission(function):
    @wraps(function)
    def run(self, *args, **kwargs):
        with self.store.lock:
            return function(self, *args, **kwargs)

    return run


class Service:
    def __init__(
        self, store: Store, terraform: Terraform, catalog: dict, config: dict, checker=None
    ):
        self.store, self.tf, self.catalog, self.config = store, terraform, catalog, config
        self.driver = TerraformDriver(terraform)
        self.onprem = None
        self.checker = checker or AppChecker(config.get("readiness"))
        self.wake = threading.Event()
        self.today = lambda: dt.date.today()

    # --- requests ---------------------------------------------------------------------------
    @admission
    def create(self, body: dict, idempotency_key: str | None = None) -> tuple[int, dict]:
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if idempotency_key:
            seen = self.store.remembered(idempotency_key)
            if seen:
                if seen[0] != digest:
                    raise Conflict(
                        "IDEMPOTENCY_CONFLICT",
                        "this Idempotency-Key was used with a different body",
                    )
                return seen[1], seen[2]
        req = spec.parse(body, self.catalog, self.config)
        existing = self.store.active_environment(req["org"]["id"], req["cloud"], req["template"])
        if existing:
            raise Conflict(
                "ENVIRONMENT_EXISTS",
                "this org already has an active environment of this app on this cloud",
                environmentId=existing["id"],
            )
        suffix = secrets.token_hex(2)
        tfvars = spec.render(req, self.catalog, self.config, suffix, self.today())
        if req["cloud"] == "onprem":
            for other in self.store.environments():
                if (
                    other["cloud"] == "onprem"
                    and other["status"] != "DESTROYED"
                    and other["tfvars"].get("node_id") == tfvars["node_id"]
                ):
                    raise Conflict(
                        "NODE_RESERVED",
                        "remove the existing app deployment before reusing this node",
                    )
            # Check registration, ownership and preparation before queueing remote work.
            self.tf.resolve(tfvars)
        env_id = f"env-{uuid.uuid4().hex[:12]}"
        stamp = now()
        self.store.insert_environment(
            {
                "id": env_id,
                "org_id": req["org"]["id"],
                "cloud": req["cloud"],
                "template": req["template"],
                "deployment_id": tfvars["deployment_id"],
                "status": "PLANNING",
                "request": req,
                "tfvars": tfvars,
                "plan": None,
                "handoff": None,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
        job = self._enqueue(env_id, "PLAN" if req["mode"] == "plan" else "PLAN_APPLY")
        response = {
            "environmentId": env_id,
            "jobId": job,
            "deploymentId": tfvars["deployment_id"],
            "status": "PLANNING",
        }
        if idempotency_key:
            self.store.remember(idempotency_key, digest, 202, response)
        return 202, response

    @admission
    def apply(self, env_id: str) -> tuple[int, dict]:
        env = self._get(env_id)
        retry = env["cloud"] == "onprem" and env["status"] == "RETRYABLE"
        if env["status"] != "PLANNED" and not retry:
            raise Conflict(
                "INVALID_STATE", f"only a PLANNED environment can be applied (now {env['status']})"
            )
        self._not_busy(env_id)
        self.store.update_environment(env_id, now(), status="APPLYING")
        return 202, {
            "environmentId": env_id,
            "jobId": self._enqueue(env_id, "RETRY_APPLY" if retry else "APPLY"),
            "status": "APPLYING",
        }

    @admission
    def update(self, env_id: str, body: dict) -> tuple[int, dict]:
        """Change a READY (or FAILED) environment in place: new app settings, sizing or access.

        The body takes the same app / sizing / access fields as creation; they are merged into the
        stored request. Org, cloud, template and target stay. Runtime changes must be explicit."""
        env = self._get(env_id)
        if env["status"] not in ("READY", "INFRA_READY", "APP_NOT_READY", "FAILED"):
            raise Conflict(
                "INVALID_STATE",
                f"only a READY or FAILED environment can be updated (now {env['status']})",
            )
        self._not_busy(env_id)
        spec.require(
            type(body) is dict and not (body.keys() - {"app", "sizing", "access"}),
            "update takes app, sizing and access only",
        )
        for field in ("app", "sizing", "access"):
            spec.require(type(body.get(field, {})) is dict, f"{field} must be an object")
        if env["cloud"] == "onprem":
            raise Conflict(
                "UNSUPPORTED_UPDATE",
                "on-prem containers require an explicit replacement deployment",
            )
        if not env["tfvars"].get("app", {}).get("entrypoint"):
            raise Conflict(
                "UNSUPPORTED_LEGACY_RUNTIME",
                "create a Fargate or Cloud Run deployment for this application",
            )
        stored = env["request"]
        merged = {
            "org": stored["org"],
            "cloud": stored["cloud"],
            "target": stored["target"],
            "workflowId": stored.get("workflowId"),
            "app": {"template": stored["template"], **stored["app"], **body.get("app", {})},
            "sizing": {**stored["sizing"], **body.get("sizing", {})},
            "access": {**stored["access"], **body.get("access", {})},
        }
        req = spec.parse(merged, self.catalog, self.config)
        suffix = env["deployment_id"].rsplit("-", 1)[1]  # keep the same resources
        tfvars = spec.render(req, self.catalog, self.config, suffix, self.today())
        if req["cloud"] == "aws":
            tfvars["final_snapshot_identifier"] = env["tfvars"]["final_snapshot_identifier"]
        tfvars["metadata"] = env["tfvars"]["metadata"]  # tags stay as created
        self.store.update_environment(
            env_id,
            now(),
            request=req,
            tfvars=tfvars,
            status="PLANNING",
            error=None,
            readiness={
                "infrastructure": {"status": "PENDING"},
                "application": {"status": "PENDING"},
            },
        )
        return 202, {
            "environmentId": env_id,
            "jobId": self._enqueue(env_id, "PLAN_APPLY"),
            "status": "PLANNING",
        }

    @admission
    def check_app(self, env_id: str) -> tuple[int, dict]:
        env = self._get(env_id)
        if env["status"] not in ("READY", "INFRA_READY", "APP_NOT_READY"):
            raise Conflict("INVALID_STATE", "app checks require completed infrastructure")
        self._not_busy(env_id)
        readiness = env.get("readiness") or self.view(env)["readiness"]
        readiness["application"] = {**readiness.get("application", {}), "status": "PENDING"}
        self.store.update_environment(env_id, now(), status="VERIFYING_APP", readiness=readiness)
        return 202, {
            "environmentId": env_id,
            "jobId": self._enqueue(env_id, "CHECK_APP"),
            "status": "VERIFYING_APP",
        }

    @admission
    def reconcile(self, env_id: str) -> tuple[int, dict]:
        env = self._get(env_id)
        if env["cloud"] != "onprem" or env["status"] not in (
            "NEEDS_RECONCILIATION",
            "FAILED",
            "APP_NOT_READY",
            "INFRA_READY",
            "READY",
        ):
            raise Conflict(
                "INVALID_STATE",
                "on-prem reconciliation needs a completed or interrupted deployment",
            )
        self._not_busy(env_id)
        self.store.update_environment(env_id, now(), status="RECONCILING")
        return 202, {
            "environmentId": env_id,
            "jobId": self._enqueue(env_id, "RECONCILE"),
            "status": "RECONCILING",
        }

    def bind_workflow(self, env_id: str, body: dict) -> tuple[int, dict]:
        """Attach an already-deployed release environment without another Terraform apply."""
        env = self._get(env_id)
        self._not_busy(env_id)
        spec.require(
            type(body) is dict
            and set(body) == {"workflowId"}
            and type(body["workflowId"]) is str
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["workflowId"]),
            "workflowId is required",
        )
        self.store.update_environment(
            env_id, now(), request={**env["request"], "workflowId": body["workflowId"]}
        )
        return 200, self.get(env_id)

    @admission
    def destroy(self, env_id: str) -> tuple[int, dict]:
        env = self._get(env_id)
        if env["status"] in ("DESTROYED", "DESTROYING"):
            return 200, self.view(env)
        if env["status"] == "NEEDS_RECONCILIATION":
            raise Conflict(
                "NEEDS_RECONCILIATION",
                "a job was cut off; check the cloud and the state before destroying",
            )
        self._not_busy(env_id)
        self.store.update_environment(env_id, now(), status="DESTROYING")
        return 202, {
            "environmentId": env_id,
            "jobId": self._enqueue(env_id, "DESTROY"),
            "status": "DESTROYING",
        }

    def view(self, env: dict) -> dict:
        handoff = env["handoff"] or {}
        return {
            "environmentId": env["id"],
            "org": env["request"]["org"],
            "cloud": env["cloud"],
            "template": env["template"],
            "workflowId": env["request"].get("workflowId"),
            "sizing": env["tfvars"]["sizing"],
            "runtime": {"aws": "ECS_FARGATE", "gcp": "CLOUD_RUN", "onprem": "ON_PREM"}[
                env["cloud"]
            ],
            "deploymentId": env["deployment_id"],
            "status": env["status"],
            "error": env["error"],
            "plan": env["plan"],
            "appUrl": (handoff.get("app") or {}).get("url"),
            "handoff": env["handoff"],
            "readiness": env.get("readiness")
            or {
                "infrastructure": {
                    "status": "READY" if env["status"] == "INFRA_READY" else "UNKNOWN",
                    "observedAt": None,
                },
                "application": {"status": "UNKNOWN", "observedAt": None},
            },
            "createdAt": env["created_at"],
            "updatedAt": env["updated_at"],
        }

    def get(self, env_id: str) -> dict:
        return self.view(self._get(env_id))

    def job(self, job_id: str) -> dict:
        job = self.store.job(job_id)
        if not job:
            raise NotFound(job_id)
        env = self.store.environment(job["environment_id"])
        log = self.tf.log_path(env["id"])
        tail = log.read_text()[-4000:] if log.exists() else ""
        return {
            "jobId": job["id"],
            "environmentId": job["environment_id"],
            "kind": job["kind"],
            "status": job["status"],
            "error": job["error"],
            "createdAt": job["created_at"],
            "startedAt": job["started_at"],
            "finishedAt": job["finished_at"],
            "logTail": tail,
        }

    # --- worker -----------------------------------------------------------------------------
    def recover(self) -> None:
        """A job that was running when the process stopped may have changed the cloud halfway."""
        for job in self.store.interrupted():
            if job["kind"] == "CHECK_APP":
                env = self._get(job["environment_id"])
                readiness = env.get("readiness") or self.view(env)["readiness"]
                readiness["application"] = {
                    "status": "UNKNOWN",
                    "reason": "App check interrupted",
                    "observedAt": now(),
                }
                self.store.update_environment(
                    env["id"], now(), status="INFRA_READY", readiness=readiness
                )
                self.store.update_job(
                    job["id"], status="FAILED", finished_at=now(), error="App check interrupted"
                )
                continue
            self.store.update_job(
                job["id"],
                status="NEEDS_RECONCILIATION",
                finished_at=now(),
                error="process stopped while the job was running",
            )
            self.store.update_environment(
                job["environment_id"],
                now(),
                status="NEEDS_RECONCILIATION",
                error="process stopped while a job was running",
            )

        if self.onprem:
            self.onprem.recover()

    def run_forever(self, stop: threading.Event) -> None:
        def observe():
            while not stop.is_set():
                with suppress(Exception):
                    self.publish_observations()
                stop.wait(5)

        observer = threading.Thread(target=observe, name="otel-provisioner", daemon=True)
        observer.start()
        try:
            while not stop.is_set():
                if not self.run_next():
                    self.wake.wait(2)
                    self.wake.clear()
        finally:
            stop.set()
            observer.join(timeout=3)
            get_telemetry("iac").close()

    def run_next(self) -> bool:
        job = self.store.next_queued()
        if not job:
            return self.onprem.run_next() if self.onprem else False
        self.execute(job)
        return True

    def execute(self, job: dict) -> None:
        env = self.get(job["environment_id"])
        with get_telemetry("iac").operation(
            job["kind"], workflow=env.get("workflowId") or "", operation=job["id"]
        ) as span:
            self._execute_job(job)
            if self.store.job(job["id"])["status"] != "SUCCEEDED":
                span.set_status(Status(StatusCode.ERROR, "JOB_UNCONFIRMED"))

    def _execute_job(self, job: dict) -> None:
        env = self.store.environment(job["environment_id"])
        cloud, env_id = env["cloud"], env["id"]
        self.store.update_job(job["id"], status="RUNNING", started_at=now())
        try:
            if job["kind"] in ("PLAN", "PLAN_APPLY"):
                plan = self.tf.plan(env_id, cloud, env["tfvars"])
                self.store.update_environment(
                    env_id,
                    now(),
                    plan=plan,
                    status="PLANNED" if job["kind"] == "PLAN" else "APPLYING",
                )
            if job["kind"] in ("APPLY", "PLAN_APPLY", "RETRY_APPLY", "RECONCILE"):
                if job["kind"] == "RECONCILE":
                    handoff = self.tf.reconcile(env_id, cloud, env["tfvars"])
                    if handoff and handoff.get("retryOwnedEmpty"):
                        self.store.update_environment(
                            env_id,
                            now(),
                            status="RETRYABLE",
                            handoff=None,
                            error=None,
                            readiness=None,
                        )
                        self.store.update_job(job["id"], status="SUCCEEDED", finished_at=now())
                        return
                elif job["kind"] == "RETRY_APPLY":
                    handoff = self.tf.apply(
                        env_id, cloud, "reviewed.tfplan", retry_owned_empty=True
                    )
                else:
                    handoff = self.tf.apply(env_id, cloud, "reviewed.tfplan")
                if job["kind"] == "RECONCILE" and handoff is None:
                    self.store.update_environment(
                        env_id, now(), status="DESTROYED", handoff=None, error=None, readiness=None
                    )
                    self.store.update_job(job["id"], status="SUCCEEDED", finished_at=now())
                    self._publish(env_id, job["id"])
                    return
                self.store.update_environment(
                    env_id,
                    now(),
                    handoff=handoff,
                    status="INFRA_READY",
                    error=None,
                    readiness={
                        "infrastructure": {
                            "status": "READY",
                            "observedAt": now(),
                            "jobId": job["id"],
                        },
                        "application": {"status": "PENDING", "observedAt": None},
                    },
                )
            if job["kind"] in ("APPLY", "PLAN_APPLY", "RETRY_APPLY", "RECONCILE", "CHECK_APP"):
                # A separate job keeps apply evidence if this process stops while probing.
                if job["kind"] != "CHECK_APP":
                    self._enqueue(env_id, "CHECK_APP")
                else:
                    self.store.update_environment(env_id, now(), status="VERIFYING_APP")
                    current = self._get(env_id)
                    readiness = current.get("readiness") or self.view(current)["readiness"]
                    application = self.checker.check(
                        current["handoff"], current["tfvars"].get("app")
                    )
                    previous = readiness.get("application", {})
                    if application.get("status") != "READY":
                        application["lastSuccessAt"] = previous.get("lastSuccessAt")
                    readiness["application"] = application
                    status = (
                        "READY"
                        if application["status"] == "READY"
                        else (
                            "INFRA_READY"
                            if application["status"] in ("UNKNOWN", "NOT_CONFIGURED")
                            else "APP_NOT_READY"
                        )
                    )
                    self.store.update_environment(env_id, now(), status=status, readiness=readiness)
                    if status != "READY":
                        self.store.update_job(
                            job["id"],
                            status="FAILED",
                            error=application.get("reason"),
                            finished_at=now(),
                        )
                        self._publish(env_id, job["id"])
                        return
            if job["kind"] == "DESTROY":
                plan = self.tf.plan(env_id, cloud, env["tfvars"], destroy=True)
                self.tf.apply(env_id, cloud, plan["plan"])
                self.store.update_environment(
                    env_id, now(), status="DESTROYED", handoff=None, error=None, readiness=None
                )
        except (TerraformError, OSError, ValueError, Conflict, NotFound) as exc:
            from .node import NodeEffectUnconfirmed

            if isinstance(exc, (Conflict, NotFound)) and cloud != "onprem":
                raise
            if isinstance(exc, NodeEffectUnconfirmed) or (
                isinstance(exc, (Conflict, NotFound)) and job["kind"] != "CHECK_APP"
            ):
                self.store.update_job(
                    job["id"], status="NEEDS_RECONCILIATION", error=str(exc), finished_at=now()
                )
                self.store.update_environment(
                    env_id, now(), status="NEEDS_RECONCILIATION", error=str(exc)
                )
                return
            message = str(exc)[-2000:]
            self.store.update_job(job["id"], status="FAILED", error=message, finished_at=now())
            if cloud == "onprem" and job["kind"] == "CHECK_APP":
                current = self._get(env_id)
                readiness = current.get("readiness") or self.view(current)["readiness"]
                readiness["application"] = {
                    "status": "UNKNOWN",
                    "reason": message,
                    "observedAt": now(),
                }
                self.store.update_environment(
                    env_id, now(), status="INFRA_READY", readiness=readiness
                )
                self._publish(env_id, job["id"])
                return
            self.store.update_environment(env_id, now(), status="FAILED", error=message)
            self._publish(env_id, job["id"])
            return
        self.store.update_job(job["id"], status="SUCCEEDED", finished_at=now())
        self._publish(env_id, job["id"])

    def _publish(self, env_id: str, job_id: str) -> None:
        """Observation delivery never changes the result of a Terraform or app check job."""
        telemetry = get_telemetry("iac")
        env = self.get(env_id)
        if not telemetry.enabled or not env.get("workflowId"):
            return
        from control_plane.deployment.tools.report_observations import provisioner_reports

        result = {"status": "QUEUED_OTEL", "observedAt": now()}
        try:
            for report in provisioner_reports(env["workflowId"], env, self.job(job_id)):
                if not telemetry.snapshot(report["resource"], report["data"]):
                    result["status"] = "OMITTED"
        except Exception as exc:
            result.update(status="FAILED", reason=type(exc).__name__)
        readiness = env["readiness"]
        readiness["reporting"] = result
        self.store.update_environment(env_id, now(), readiness=readiness)

    # --- helpers ----------------------------------------------------------------------------
    def _enqueue(self, env_id: str, kind: str) -> str:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        self.store.insert_job(
            {
                "id": job_id,
                "environment_id": env_id,
                "kind": kind,
                "status": "QUEUED",
                "created_at": now(),
            }
        )
        self.wake.set()
        return job_id

    def _get(self, env_id: str) -> dict:
        env = self.store.environment(env_id)
        if not env:
            raise NotFound(env_id)
        return env

    def _not_busy(self, env_id: str) -> None:
        if self.store.busy(env_id):
            raise Conflict("RESOURCE_BUSY", "a job for this environment is still queued or running")

    def publish_observations(self):
        if not get_telemetry("iac").enabled:
            return
        for env in self.store.environments()[:256]:
            if not env["request"].get("workflowId"):
                continue
            with self.store.lock:
                job = self.store.db.execute(
                    "SELECT id FROM jobs WHERE environment_id=? ORDER BY created_at DESC LIMIT 1",
                    (env["id"],),
                ).fetchone()
            if job:
                self._publish(env["id"], job["id"])
