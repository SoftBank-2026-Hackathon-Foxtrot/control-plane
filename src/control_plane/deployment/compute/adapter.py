"""Durable, single-target COMPUTE preparation adapter. Run on the target VM."""

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime

from opentelemetry.trace import Status, StatusCode

from control_plane.deployment.tools.report_observations import compute_report
from control_plane.deployment.tools.telemetry import get_telemetry

from . import runner

PROFILE = "container-stage-v1"
REFS = ("sourceRef", "targetRef", "workloadScopeRef", "bindingRef")
ACTIONS = ["PREPARE", "START_SYNC", "RECONCILE"]


class Problem(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


def now():
    return datetime.now(UTC).isoformat()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def registration(handoff, binding):
    outputs = handoff["outputs"]
    if outputs.get("compute", {}).get("runtime", "ON_PREM") != "ON_PREM":
        raise ValueError(
            "Managed services are deployed and checked through the provisioner"
            "; the local Docker COMPUTE adapter requires an on-prem node"
        )
    target = handoff["target"]
    if (
        not target["id"]
        or not outputs["compute"]["id"]
        or not handoff["migration_id"]
        or handoff["readiness"] != "INFRA_APPLIED"
        or outputs["schema_version"] != 1
        or outputs["cloud"] != target["cloud"]
        or target["cloud"] not in ("aws", "gcp")
    ):
        raise ValueError("Use the completed provisioning handoff")
    refs = {key: binding[key] for key in REFS}
    if (
        not all(isinstance(v, str) and v for v in refs.values())
        or refs["targetRef"] == refs["sourceRef"]
    ):
        raise ValueError("Register distinct source and target resources and the app references")
    return {
        **refs,
        "workflowId": handoff["migration_id"],
        "environment": target,
        "vmId": outputs["compute"]["id"],
        "bindingRevision": runner.binding_revision(binding),
    }


def actions(row):
    if row["active"]:
        return []
    if row["health"] != "OK":
        return ["RECONCILE"]
    return {"CREATED": ["PREPARE"], "PREPARED": ["START_SYNC"], "SYNCING": []}[row["stage"]]


def status(row, operations=()):
    plan = json.loads(row["plan"])
    unknown = {"result": "UNKNOWN", "reasons": ["PREPARATION_ONLY"]}
    check = {
        "code": "BUSINESS_PROBE",
        "stage": "ADVANCE",
        "required": True,
        "decision": unknown,
        "evidenceRef": None,
        "observedAt": row["updated"],
        "validUntil": row["updated"],
        "transitionId": None,
        "topologyRevision": 0,
    }
    return {
        "migrationId": row["id"],
        "workflowId": plan["workflowId"],
        "resourceId": plan["resourceId"],
        "kind": "COMPUTE",
        "revision": row["revision"],
        "lifecycle": row["stage"],
        "health": row["health"],
        "phase": "compute." + row["stage"].lower(),
        "activeOperationId": row["active"],
        "transitionId": None,
        "transitionState": "NONE",
        "topologyRevision": 0,
        "allowedActions": actions(row),
        "observed": {
            "observedAt": row["updated"],
            "route": "UNKNOWN",
            "businessWriteAccess": "UNKNOWN",
            "targetWritesPossible": True,
        },
        "readiness": dict.fromkeys(("cutover", "resume", "rollback", "advance"), unknown),
        "rollback": {"method": "UNAVAILABLE", "reasons": ["PREPARATION_ONLY"]},
        "checks": [check],
        "progress": {"completed": None, "total": None, "unit": "containers"},
        "details": {
            "compute": {
                **(json.loads(row["result"]) if row["result"] else {}),
                "vmId": json.loads(row["target"])["vmId"],
                "operations": list(operations),
            }
        },
    }


class Adapter:
    def __init__(self, db_path, binding_path, handoff):
        self.path = db_path
        self.binding = binding_path
        self.target = registration(handoff, runner.load_binding(binding_path)[0])
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS migrations(id TEXT PRIMARY KEY, plan TEXT, target TEXT,
                    stage TEXT, health TEXT, revision INTEGER, active TEXT,
                    updated TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS operations(
                    id TEXT PRIMARY KEY, migration TEXT, action TEXT,
                    step TEXT, status TEXT, error TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS requests(path TEXT, key TEXT, body TEXT, response TEXT,
                    PRIMARY KEY(path, key));
            """)
            # Additive migration preserves existing operation journals.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(operations)")}
            for column in ("requested_at", "started_at", "finished_at"):
                if column not in columns:
                    db.execute(f"ALTER TABLE operations ADD COLUMN {column} TEXT")
            # Never replay an external Docker command interrupted by a process exit.
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE migrations SET health='UNKNOWN', active=NULL,
                    updated=?, revision=revision+1
                          WHERE active IN (SELECT id FROM operations WHERE status='RUNNING')""",
                (now(),),
            )
            db.execute(
                "UPDATE operations SET status='UNKNOWN', error='Execution interrup"
                "ted' WHERE status='RUNNING'"
            )

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def capabilities(self):
        return {
            "apiVersion": "v1",
            "profiles": [
                {
                    "kind": "COMPUTE",
                    "profile": PROFILE,
                    "sourceConditions": ["registered source image ID"],
                    "targetConditions": [
                        self.target["environment"]["cloud"],
                        "Linux VM with local Docker",
                    ],
                    "actions": ACTIONS,
                    "rollbackModes": ["BEFORE_TARGET_WRITE"],
                    "constraints": ["staging only; no target traffic, workers or cutover"],
                }
            ],
        }

    def get(self, kind, item):
        with self.transaction() as db:
            if kind == "migrations":
                operations = [
                    {
                        "operationId": op["id"],
                        "migrationId": item,
                        "action": op["action"],
                        "status": op["status"],
                        "requestedAt": op["requested_at"],
                        "startedAt": op["started_at"],
                        "finishedAt": op["finished_at"],
                    }
                    for op in db.execute(
                        "SELECT * FROM operations WHERE migration=? ORDER BY rowid DESC LIMIT 20",
                        (item,),
                    )
                ]
                return status(self.migration(db, item), operations)
            row = db.execute("SELECT * FROM operations WHERE id=?", (item,)).fetchone()
            if not row:
                raise Problem(404, "NOT_FOUND", "Unknown operation")
            error = (
                {
                    "code": "EXECUTION_" + row["status"],
                    "message": row["error"],
                    "operationId": item,
                    "blockers": [],
                }
                if row["error"]
                else None
            )
            return {
                "operationId": item,
                "migrationId": row["migration"],
                "action": row["action"],
                "requestedAt": row["requested_at"],
                "startedAt": row["started_at"],
                "finishedAt": row["finished_at"],
                "status": row["status"],
                "statusUrl": "/v1/operations/" + item,
                "error": error,
                "result": json.loads(row["result"]) if row["result"] else None,
            }

    @staticmethod
    def migration(db, item):
        row = db.execute("SELECT * FROM migrations WHERE id=?", (item,)).fetchone()
        if not row:
            raise Problem(404, "NOT_FOUND", "Unknown migration")
        return row

    def post(self, migration_id, key, body):
        if not key or not isinstance(body, dict):
            raise Problem(400, "INVALID_REQUEST", "Expected Idempotency-Key and a JSON object")
        path = (
            "/v1/migrations" if migration_id is None else f"/v1/migrations/{migration_id}/actions"
        )
        with self.transaction() as db:
            prior = db.execute(
                "SELECT * FROM requests WHERE path=? AND key=?", (path, key)
            ).fetchone()
            if prior:
                if prior["body"] != canonical(body):
                    raise Problem(409, "IDEMPOTENCY_CONFLICT", "Key was used with another body")
                return json.loads(prior["response"])
            response = (
                self.create(db, body)
                if migration_id is None
                else self.accept(db, migration_id, body)
            )
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?)",
                (path, key, canonical(body), canonical(response)),
            )
            return response

    def create(self, db, plan):
        fields = {"workflowId", "resourceId", "kind", "profile", *REFS, "policy"}
        if (
            not fields <= plan.keys() <= fields | {"replicationRef"}
            or plan.get("replicationRef") is not None
            or not all(isinstance(plan[k], str) and plan[k] for k in fields - {"policy"})
        ):
            raise Problem(400, "INVALID_REQUEST", "Expected a registered COMPUTE plan")
        policy = plan["policy"]
        if (
            not isinstance(policy, dict)
            or policy.keys() != {"rollbackMode", "maxQuiesceSeconds"}
            or type(policy["maxQuiesceSeconds"]) is not int
            or policy["maxQuiesceSeconds"] < 1
        ):
            raise Problem(400, "INVALID_REQUEST", "Invalid plan policy")
        if (plan["kind"], plan["profile"], policy["rollbackMode"]) != (
            "COMPUTE",
            PROFILE,
            "BEFORE_TARGET_WRITE",
        ):
            raise Problem(
                422, "UNSUPPORTED_PROFILE", "Unsupported kind, profile or rollback policy"
            )
        if any(plan[k] != self.target[k] for k in (*REFS, "workflowId")):
            raise Problem(
                422, "PRECONDITION_FAILED", "Plan references do not match the registered handoff"
            )
        item = "mig-" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO migrations VALUES (?,?,?,'CREATED','OK',1,NULL,?,NULL)",
            (item, canonical(plan), canonical(self.target), now()),
        )
        return [201, "/v1/migrations/" + item, status(self.migration(db, item))]

    def accept(self, db, item, body):
        row = self.migration(db, item)
        if (
            body.keys() != {"action", "expectedRevision"}
            or type(body["expectedRevision"]) is not int
        ):
            raise Problem(400, "INVALID_REQUEST", "Expected action and expectedRevision")
        action = body["action"]
        if action not in ACTIONS:
            raise Problem(422, "UNSUPPORTED_PROFILE", "Action outside the preparation profile")
        if body["expectedRevision"] != row["revision"]:
            raise Problem(409, "REVISION_CONFLICT", "Refresh migration status")
        if db.execute("SELECT 1 FROM migrations WHERE active IS NOT NULL").fetchone():
            raise Problem(409, "RESOURCE_BUSY", "Target has an active operation")
        if (
            action not in actions(row)
            or db.execute(
                "SELECT 1 FROM migrations WHERE id<>? AND health='UNKNOWN'", (item,)
            ).fetchone()
        ):
            raise Problem(409, "INVALID_STATE", "Reconcile the affected migration first")
        if row["target"] != canonical(self.target):
            raise Problem(
                422, "PRECONDITION_FAILED", "Restore the registered VM and binding for this plan"
            )
        op = "op-" + uuid.uuid4().hex
        step = action
        if action == "RECONCILE":
            step = db.execute(
                "SELECT step FROM operations WHERE migration=? ORDER BY rowid DESC LIMIT 1", (item,)
            ).fetchone()["step"]
        db.execute(
            (
                "INSERT INTO operations (id,migration,action,step,status,requested"
                "_at) VALUES (?,?,?,?,'QUEUED',?)"
            ),
            (op, item, action, step, now()),
        )
        db.execute(
            "UPDATE migrations SET active=?, revision=revision+1, updated=? WHERE id=?",
            (op, now(), item),
        )
        url = "/v1/operations/" + op
        return [
            202,
            url,
            {
                "operationId": op,
                "migrationId": item,
                "action": action,
                "status": "QUEUED",
                "statusUrl": url,
            },
        ]

    def run_next(self):
        with self.transaction() as db:
            op = db.execute(
                "SELECT * FROM operations WHERE status='QUEUED' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not op:
                return False
            db.execute(
                "UPDATE operations SET status='RUNNING', started_at=? WHERE id=?", (now(), op["id"])
            )
            row = self.migration(db, op["migration"])
        telemetry = get_telemetry("iac")
        with telemetry.operation(
            op["action"],
            workflow=json.loads(row["plan"])["workflowId"],
            migration=row["id"],
            operation=op["id"],
        ) as span:
            stage, result, error = row["stage"], None, None
            try:
                if row["target"] != canonical(self.target):
                    raise ValueError("Target registration changed")
                result = runner.execute(
                    op["step"],
                    self.binding,
                    self.target["targetRef"],
                    expected_revision=self.target["bindingRevision"],
                    observe_only=op["action"] == "RECONCILE",
                )
                stage = "PREPARED" if op["step"] == "PREPARE" else "SYNCING"
                outcome, health = "SUCCEEDED", "OK"
            except Exception:
                # START_SYNC may have created a container before an observation failed.
                outcome = "FAILED" if op["step"] == "PREPARE" else "UNKNOWN"
                health, error = (
                    outcome,
                    f"{op['step']} could not be verified; inspect the registered target",
                )
            with self.transaction() as db:
                evidence = canonical(result) if result else None
                db.execute(
                    "UPDATE operations SET status=?, error=?, result=?, finished_at=? WHERE id=?",
                    (outcome, error, evidence, now(), op["id"]),
                )
                db.execute(
                    """UPDATE migrations SET stage=?, health=?, active=NULL, updated=?,
                              revision=revision+1, result=? WHERE id=?""",
                    (stage, health, now(), evidence, row["id"]),
                )
            if outcome != "SUCCEEDED":
                span.set_status(Status(StatusCode.ERROR, outcome))
            try:
                view = self.get("migrations", row["id"])
                report = compute_report(view["workflowId"], view)
                telemetry.snapshot("COMPUTE", report["data"])
            except Exception:
                pass  # Telemetry does not change the persisted command result.
        return True

    def publish_observations(self):
        telemetry = get_telemetry("iac")
        if not telemetry.enabled:
            return
        with self.transaction() as db:
            ids = [
                row[0]
                for row in db.execute("SELECT id FROM migrations ORDER BY updated DESC LIMIT 256")
            ]
        for mid in ids:
            view = self.get("migrations", mid)
            report = compute_report(view["workflowId"], view)
            telemetry.snapshot("COMPUTE", report["data"])
