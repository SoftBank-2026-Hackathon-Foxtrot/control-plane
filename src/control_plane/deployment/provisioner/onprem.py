"""Durable v2 preparation jobs for registered, preallocated Linux hosts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import handoff, inventory
from .drivers.base import DriverError
from .service import Conflict, NotFound
from .spec import require


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class OnPremService:
    def __init__(self, store, catalog, driver, checker, *, ttl=900, clock=None):
        self.store, self.catalog, self.driver, self.checker = store, catalog, driver, checker
        self.ttl = max(60, min(int(ttl), 3600))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _get(self, env_id):
        env = self.store.get("environments", env_id)
        if env is None:
            raise NotFound(env_id)
        return env

    def _save(self, env):
        env["revision"] += 1
        if env.get("handoff"):
            env["handoff"]["revision"] = env["revision"]
        env["updatedAt"] = now()
        self.store.put("environments", env)

    def _queue(self, env, kind):
        job = {"id": f"job-{uuid.uuid4().hex[:12]}", "environmentId": env["id"], "kind": kind,
               "status": "QUEUED", "error": None, "createdAt": now(), "startedAt": None, "finishedAt": None}
        self.store.put("jobs", job)
        return job

    def _busy(self, env_id):
        if any(j["environmentId"] == env_id and j["status"] in ("QUEUED", "RUNNING") for j in self.store.all("jobs")):
            raise Conflict("RESOURCE_BUSY", "a preparation job is queued or running")

    def _remembered(self, path, body, key):
        require(type(key) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key), "Idempotency-Key required")
        scope = f"v2:{path}:{key}"
        previous = self.store.remembered(scope)
        fingerprint = digest(body)
        if previous and previous[0] != fingerprint:
            raise Conflict("IDEMPOTENCY_CONFLICT", "this key was used with a different request")
        return scope, fingerprint, previous[1] if previous else None

    def create(self, body, key):
        req = inventory.request(body)
        with self.store.transaction():
            scope, fingerprint, previous = self._remembered(f"{req['org']}/environments", body, key)
            if previous:
                return 202, previous
            resolved = self.catalog.resolve(req)
            env_id = f"env-{uuid.uuid4().hex[:12]}"
            # Reserve both catalog identity and endpoint; changing a host's alias cannot double-book it.
            keys = [key for h in resolved["inventory"]["hosts"]
                    for key in (f"host:{req['target']['siteRef']}:{h['id']}", f"address:{h['address']}",
                                f"service:{h['serviceAddress']}")]
            try:
                self.store.reserve(env_id, keys)
            except sqlite3.IntegrityError:
                raise Conflict("HOST_RESERVED", "a registered host is already reserved") from None
            stamp = now()
            env = {"id": env_id, "revision": 1, "request": req, "resolved": resolved,
                   "workflowId": req["workflowId"], "status": "PLANNING", "plan": None, "planSeal": None,
                   "handoff": None, "inspection": None, "appliedSeal": None, "error": None,
                   "readiness": {"infrastructure": {"status": "PENDING", "observedAt": None},
                                 "application": {"status": "NOT_DEPLOYED" if resolved["binding"] else "NOT_CONFIGURED",
                                                 "observedAt": None}},
                   "createdAt": stamp, "updatedAt": stamp, "intent": "prepare"}
            self.store.put("environments", env)
            job = self._queue(env, "PLAN_APPLY" if req["mode"] == "apply" else "PLAN")
            response = self._accepted(env, job)
            self.store.remember(scope, fingerprint, response)
            return 202, response

    def command(self, env_id, action, body, key):
        with self.store.transaction():
            env = self._get(env_id)
            scope, fingerprint, previous = self._remembered(f"{env['request']['org']}/{env_id}/{action}", body, key)
            if previous:
                return 202, previous
            self._busy(env_id)
            required = {"expectedRevision"}
            optional = {"planId"} if action == "apply" else (
                {"intent"} if action == "plan" else {"workflowId"} if action == "workflow" else set())
            inventory.fields(body, required, optional, action)
            if type(body["expectedRevision"]) is not int or body["expectedRevision"] != env["revision"]:
                raise Conflict("STALE_REVISION", "reload the environment before changing it")
            if action == "plan":
                if env["status"] in ("NEEDS_RECONCILIATION", "RELEASED"):
                    raise Conflict("INVALID_STATE", "reconcile interrupted work before planning")
                intent = body.get("intent", "prepare")
                require(intent in ("prepare", "cleanup"), "intent must be prepare or cleanup")
                if intent == "cleanup" and env["workflowId"]:
                    raise Conflict("WORKFLOW_BOUND", "cleanup requires a workflow release integration; bound hosts are retained")
                if intent == "prepare" and env["workflowId"] and env.get("appliedSeal"):
                    raise Conflict("WORKFLOW_BOUND", "cannot reconfigure services while a migration workflow owns them")
                current = self.catalog.resolve(env["request"])
                if current != env["resolved"]:
                    raise Conflict("REFERENCE_CHANGED", "registered references are immutable; restore the catalog or register a new environment")
                env.update(status="PLANNING", intent=intent, plan=None, planSeal=None, error=None)
                kind = "PLAN"
            elif action == "apply":
                if env["status"] != "PLANNED" or body.get("planId") != (env["plan"] or {}).get("planId"):
                    raise Conflict("STALE_PLAN", "apply requires the current reviewed plan")
                if self.clock() >= datetime.fromisoformat(env["plan"]["expiresAt"]):
                    raise Conflict("STALE_PLAN", "plan expired; create a new plan")
                env.update(status="APPLYING" if env["intent"] == "prepare" else "RELEASING", error=None)
                kind = "APPLY"
            elif action == "check-app":
                if env["status"] not in ("READY", "INFRA_READY", "APP_NOT_READY"):
                    raise Conflict("INVALID_STATE", "application checks need completed infrastructure")
                env["status"] = "VERIFYING_APP"
                env["readiness"]["application"]["status"] = "PENDING"
                kind = "CHECK_APP"
            elif action == "reconcile":
                if env["status"] not in ("NEEDS_RECONCILIATION", "FAILED", "READY", "APP_NOT_READY", "INFRA_READY"):
                    raise Conflict("INVALID_STATE", "no execution to reconcile")
                env.update(status="RECONCILING", error=None)
                kind = "RECONCILE"
            elif action == "workflow":
                value = body.get("workflowId")
                require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value), "workflowId required")
                if env["workflowId"] not in (None, value):
                    raise Conflict("WORKFLOW_BOUND", "a preparation environment cannot be rebound to another workflow")
                env["workflowId"] = value
                env["request"]["workflowId"] = value
                if env["handoff"]:
                    env["handoff"]["workflowId"] = value
                # Plans bind the environment request. Binding invalidates outstanding plans.
                if env["status"] == "PLANNED":
                    env.update(status="REGISTERED", plan=None, planSeal=None)
                self._save(env)
                response = {"environmentId": env_id, "status": env["status"], "revision": env["revision"]}
                self.store.remember(scope, fingerprint, response)
                return 202, response
            else:
                raise NotFound(action)
            self._save(env)
            job = self._queue(env, kind)
            response = self._accepted(env, job)
            self.store.remember(scope, fingerprint, response)
            return 202, response

    @staticmethod
    def _accepted(env, job):
        return {"environmentId": env["id"], "jobId": job["id"], "status": env["status"], "revision": env["revision"]}

    @staticmethod
    def view(env):
        return {"environmentId": env["id"], "org": env["request"]["org"], "driver": "ansible",
                "target": env["request"]["target"], "profileRef": env["request"]["profileRef"],
                "appBindingRef": env["request"].get("appBindingRef"), "workflowId": env["workflowId"],
                **{key: env[key] for key in ("status", "revision", "plan", "handoff", "readiness",
                                             "error", "createdAt", "updatedAt")}}

    def get(self, env_id):
        with self.store.transaction():
            return self.view(self._get(env_id))

    def environments(self):
        with self.store.transaction():
            return [self.view(env) for env in self.store.all("environments")]

    def job(self, job_id):
        with self.store.transaction():
            job = self.store.get("jobs", job_id)
            if not job:
                raise NotFound(job_id)
            return {"jobId": job["id"], **{k: v for k, v in job.items() if k != "id"}}

    def recover(self):
        with self.store.transaction():
            for job in self.store.all("jobs"):
                if job["status"] != "RUNNING":
                    continue
                env = self._get(job["environmentId"])
                if job["kind"] == "CHECK_APP":
                    env["status"] = "INFRA_READY"
                    env["readiness"]["application"] = {"status": "UNKNOWN", "observedAt": now(),
                                                       "reason": "App check interrupted"}
                    job["status"] = "FAILED"
                else:
                    env["status"] = job["status"] = "NEEDS_RECONCILIATION"
                    env["readiness"]["infrastructure"]["status"] = "UNKNOWN"
                env["error"] = job["error"] = {"code": "INTERRUPTED", "message": "inspect actual hosts before retrying"}
                job["finishedAt"] = now()
                self._save(env)
                self.store.put("jobs", job)

    def run_next(self):
        with self.store.transaction():
            job = next((j for j in self.store.all("jobs") if j["status"] == "QUEUED"), None)
            if not job:
                return False
            job.update(status="RUNNING", startedAt=now())
            self.store.put("jobs", job)
            env = self._get(job["environmentId"])
        mutating = False
        try:
            def progress(value):
                with self.store.transaction():
                    job["progress"] = value
                    self.store.put("jobs", job)
            context = {"jobId": job["id"], "intent": env["intent"], "progress": progress}
            if job["kind"] in ("PLAN", "PLAN_APPLY"):
                current = self.catalog.resolve(env["request"])
                if current != env["resolved"]:
                    raise DriverError("REFERENCE_CHANGED", "catalog references changed")
                planned = self.driver.plan(env, env["resolved"], context)
                env.update(plan={**planned["public"], "planId": f"plan-{uuid.uuid4().hex[:12]}",
                                 "expiresAt": (self.clock() + timedelta(seconds=self.ttl)).isoformat()},
                           planSeal=planned["seal"], status="PLANNED")
                # Persist the exact plan before a process can begin changing hosts.
                with self.store.transaction():
                    self._save(env)
            if job["kind"] in ("APPLY", "PLAN_APPLY"):
                if self.clock() >= datetime.fromisoformat(env["plan"]["expiresAt"]):
                    raise DriverError("STALE_PLAN", "plan expired; re-plan before applying")
                if self.catalog.resolve(env["request"]) != env["resolved"]:
                    raise DriverError("REFERENCE_CHANGED", "catalog references changed")
                # Driver verifies the seal before starting any remote mutation.
                context["seal"] = env["planSeal"]
                env["status"] = "APPLYING" if env["intent"] == "prepare" else "RELEASING"
                with self.store.transaction():
                    self._save(env)
                mutating = True
                if env["intent"] == "cleanup":
                    inspected = self.driver.cleanup(env, env["plan"], context)
                    env.update(status="RELEASED", inspection=inspected, handoff=None)
                    env["readiness"] = {"infrastructure": {"status": "RELEASED", "observedAt": now()},
                                        "application": {"status": "NOT_CONFIGURED", "observedAt": None}}
                    # Keep reservation: retained data must not be silently adopted by the next env.
                else:
                    inspected = self.driver.apply(env, env["plan"], context)
                    self._ready(env, job, inspected)
                    env["appliedSeal"] = env["planSeal"]
            elif job["kind"] == "RECONCILE":
                inspected = self.driver.inspect(env, context)
                env["inspection"] = inspected
                if env["intent"] == "cleanup" and inspected.get("released"):
                    env.update(status="RELEASED", handoff=None)
                    env["readiness"]["infrastructure"] = {"status": "RELEASED", "observedAt": now()}
                elif inspected.get("ready") and env.get("planSeal"):
                    self._ready(env, job, inspected)
                    env["appliedSeal"] = env["planSeal"]
                elif inspected.get("hosts") and all(h.get("configurationMatches") for h in inspected["hosts"].values()):
                    # An observed, owned but incomplete deployment can be explicitly re-planned.
                    # Never retry as a side effect of reconciliation itself.
                    env["status"] = "FAILED"
                    env["readiness"]["infrastructure"] = {"status": "NOT_READY", "observedAt": now()}
                    job.update(status="FAILED", error={"code": "SERVICES_INCOMPLETE", "message": "Owned services need an explicit repair plan"})
                else:
                    env["status"] = "NEEDS_RECONCILIATION"
                    env["readiness"]["infrastructure"] = {"status": "UNKNOWN", "observedAt": now()}
                    raise DriverError("RECONCILIATION_REQUIRED", "registered services are incomplete or differ from the plan", uncertain=True)
            elif job["kind"] == "CHECK_APP":
                app = self.checker.check_onprem(env["handoff"], env["resolved"]["binding"])
                previous = env["readiness"]["application"]
                if app["status"] != "READY":
                    app["lastSuccessAt"] = previous.get("lastSuccessAt")
                env["readiness"]["application"] = app
                if env["handoff"]:
                    env["handoff"]["application"]["deploymentStatus"] = "VERIFIED" if app["status"] == "READY" else "UNVERIFIED"
                env["status"] = "READY" if app["status"] == "READY" else (
                    "INFRA_READY" if app["status"] in ("UNKNOWN", "NOT_CONFIGURED") else "APP_NOT_READY")
                if app["status"] != "READY":
                    job.update(status="FAILED", error={"code": "APP_NOT_READY", "message": app.get("reason", "App not ready")})
            env["error"] = None
            job["status"] = "SUCCEEDED" if job["status"] == "RUNNING" else job["status"]
        except Exception as exc:
            # Never return Runner exception text, inventory variables or raw task output.
            code = exc.code if isinstance(exc, DriverError) else "PREPARATION_FAILED"
            uncertain = exc.uncertain if isinstance(exc, DriverError) else mutating
            env["status"] = "NEEDS_RECONCILIATION" if uncertain else "FAILED"
            if uncertain:
                env["readiness"]["infrastructure"]["status"] = "UNKNOWN"
            env["error"] = job["error"] = {"code": code, "message": "Preparation did not complete; inspect the registered hosts and job stage"}
            job["status"] = env["status"]
        job["finishedAt"] = now()
        with self.store.transaction():
            self._save(env)
            self.store.put("jobs", job)
            if job["status"] == "SUCCEEDED" and job["kind"] in ("APPLY", "PLAN_APPLY") and env["intent"] == "prepare" and env["resolved"]["binding"]:
                self._queue(env, "CHECK_APP")
        return True

    @staticmethod
    def _ready(env, job, inspected):
        if not inspected.get("ready"):
            raise DriverError("INFRA_NOT_READY", "service verification failed", uncertain=True)
        env.update(status="INFRA_READY", inspection=inspected, handoff=handoff.onprem(env, inspected))
        env["readiness"] = {
            "infrastructure": {"status": "READY", "observedAt": inspected["observedAt"], "jobId": job["id"],
                               "scope": "HOST_AND_SERVICE_PREPARATION"},
            "application": {"status": "NOT_DEPLOYED" if env["resolved"]["binding"] else "NOT_CONFIGURED", "observedAt": None},
        }
