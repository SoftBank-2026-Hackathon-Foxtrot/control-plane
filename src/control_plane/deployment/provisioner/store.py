"""SQLite records: environments, their jobs, and idempotency keys. One file, one process."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS environments(
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, cloud TEXT NOT NULL, template TEXT NOT NULL,
  deployment_id TEXT NOT NULL, status TEXT NOT NULL, request TEXT NOT NULL, tfvars TEXT NOT NULL,
  plan TEXT, handoff TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY, body_hash TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL);
"""
JSON_COLUMNS = ("request", "tfvars", "plan", "handoff", "readiness", "applied_snapshot")


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(environments)")}
            if "applied_snapshot" not in columns:
                self.db.execute("ALTER TABLE environments ADD COLUMN applied_snapshot TEXT")
            if "readiness" not in columns:
                self.db.execute("ALTER TABLE environments ADD COLUMN readiness TEXT")
                # Old READY meant only apply completion. Never invent an app health result.
                self.db.execute("UPDATE environments SET status='INFRA_READY' WHERE status='READY'")

    def _env(self, row) -> dict | None:
        if row is None:
            return None
        env = dict(row)
        for col in JSON_COLUMNS:
            env[col] = json.loads(env[col]) if env[col] else None
        return env

    def insert_environment(self, env: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO environments (id,org_id,cloud,template,deploymen"
                "t_id,status,request,tfvars,"
                "plan,handoff,error,created_at,updated_at,readiness,applied_snapshot) VALUES "
                "(:id,:org_id,:cloud,:template,:deployment_id,:status,:request,:tfvars,"
                ":plan,:handoff,:error,:created_at,:updated_at,:readiness,:applied_snapshot)",
                {
                    **env,
                    **{
                        c: json.dumps(env.get(c)) if env.get(c) is not None else None
                        for c in JSON_COLUMNS
                    },
                    "error": env.get("error"),
                },
            )

    def update_environment(self, env_id: str, now: str, **fields) -> None:
        sets, values = [], {"id": env_id, "updated_at": now}
        for key, value in fields.items():
            sets.append(f"{key}=:{key}")
            values[key] = json.dumps(value) if key in JSON_COLUMNS and value is not None else value
        with self.lock:
            self.db.execute(
                f"UPDATE environments SET {', '.join(sets)}, updated_at=:updated_at WHERE id=:id",
                values,
            )

    def environment(self, env_id: str) -> dict | None:
        with self.lock:
            return self._env(
                self.db.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
            )

    def environments(self) -> list[dict]:
        with self.lock:
            return [
                self._env(r)
                for r in self.db.execute("SELECT * FROM environments ORDER BY created_at DESC")
            ]

    def active_environment(self, org_id: str, cloud: str, template: str) -> dict | None:
        with self.lock:
            return self._env(
                self.db.execute(
                    "SELECT * FROM environments WHERE org_id=? AND cloud=? AND te"
                    "mplate=? AND status NOT IN ('DESTROYED','FAILED')",
                    (org_id, cloud, template),
                ).fetchone()
            )

    def environments_for_owner(self, owner: str, template: str) -> list[dict]:
        with self.lock:
            return [
                self._env(row)
                for row in self.db.execute(
                    "SELECT * FROM environments WHERE org_id=? AND template=? "
                    "ORDER BY created_at DESC LIMIT 100",
                    (owner, template),
                )
            ]

    def insert_job(self, job: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO jobs VALUES (:id,:environment_id,:kind,:status,N"
                "ULL,:created_at,NULL,NULL)",
                job,
            )

    def update_job(self, job_id: str, **fields) -> None:
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE jobs SET {sets} WHERE id=:id", {**fields, "id": job_id})

    def job(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def busy(self, env_id: str) -> bool:
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM jobs WHERE environment_id=? AND status IN ('QUEUED','RUNNING')",
                    (env_id,),
                ).fetchone()
                is not None
            )

    def next_queued(self) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def interrupted(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE status='RUNNING'")]

    def remembered(self, key: str) -> tuple[str, int, dict] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        return (row["body_hash"], row["status"], json.loads(row["response"])) if row else None

    def remember(self, key: str, body_hash: str, status: int, response: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO idempotency VALUES (?,?,?,?)",
                (key, body_hash, status, json.dumps(response)),
            )
