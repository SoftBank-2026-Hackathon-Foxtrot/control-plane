"""An additive journal: v1 cloud rows and wire representations remain untouched."""
import json
import sqlite3
import threading
from contextlib import contextmanager


class OnPremStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS onprem_environments(id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS onprem_jobs(id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS onprem_hosts(host TEXT PRIMARY KEY, environment_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS onprem_requests(scope TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, table, id):
        assert table in ("environments", "jobs")
        row = self.db.execute(f"SELECT record FROM onprem_{table} WHERE id=?", (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, table, record):
        assert table in ("environments", "jobs")
        self.db.execute(f"INSERT INTO onprem_{table} VALUES (?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                        (record["id"], json.dumps(record)))

    def all(self, table):
        assert table in ("environments", "jobs")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT record FROM onprem_{table} ORDER BY rowid")]

    def remembered(self, scope):
        row = self.db.execute("SELECT digest,response FROM onprem_requests WHERE scope=?", (scope,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def remember(self, scope, digest, response):
        self.db.execute("INSERT INTO onprem_requests VALUES (?,?,?)", (scope, digest, json.dumps(response)))

    def reserve(self, environment_id, hosts):
        for host in hosts:
            self.db.execute("INSERT INTO onprem_hosts VALUES (?,?)", (host, environment_id))

    def release(self, environment_id):
        self.db.execute("DELETE FROM onprem_hosts WHERE environment_id=?", (environment_id,))
