"""SQLite job queue: FIFO, one job running at a time, survives restarts (running -> queued on open).

States: queued -> running -> done | error. `changed` (threading.Condition) is notified on every state/progress
change so HTTP long-polls can wait on it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    args TEXT NOT NULL,
    state TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    result TEXT,
    error TEXT,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, seq);
"""

TERMINAL = ("done", "error")


class JobQueue:
    """Thread-safe SQLite-backed job queue."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.changed = threading.Condition(self._lock)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self.requeued = self._db.execute(
                "UPDATE jobs SET state='queued', progress=0, message='requeued after restart', started=NULL "
                "WHERE state='running'").rowcount

    # ------------------------------------------------------------------ helpers
    def _notify(self) -> None:
        self.changed.notify_all()

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["args"] = json.loads(d["args"])
        for k in ("result", "error"):
            d[k] = json.loads(d[k]) if d[k] is not None else None
        return d

    # ------------------------------------------------------------------ API
    def submit(self, kind: str, args: dict | None = None) -> str:
        jid = uuid.uuid4().hex[:16]
        with self._lock:
            self._db.execute("INSERT INTO jobs(id, kind, args, state, created) VALUES (?,?,?,?,?)",
                             (jid, kind, json.dumps(args or {}), "queued", time.time()))
            self._notify()
        return jid

    def get(self, jid: str) -> dict | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def claim(self) -> dict | None:
        """Atomically move the oldest queued job to running (None when nothing is queued or one is running)."""
        with self._lock:
            if self._db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                return None
            r = self._db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if r is None:
                return None
            self._db.execute("UPDATE jobs SET state='running', started=?, attempts=attempts+1, message=NULL "
                             "WHERE id=?", (time.time(), r["id"]))
            self._notify()
            return self.get(r["id"])

    def progress(self, jid: str, value: float, message: str | None = None) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET progress=?, message=COALESCE(?, message) WHERE id=? AND state='running'",
                             (max(0.0, min(1.0, float(value))), message, jid))
            self._notify()

    def finish(self, jid: str, result: Any) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET state='done', progress=1, result=?, finished=? WHERE id=?",
                             (json.dumps(result, default=str), time.time(), jid))
            self._notify()

    def fail(self, jid: str, error: dict | str, result: Any = None) -> None:
        err = error if isinstance(error, dict) else {"error": str(error), "hint": ""}
        with self._lock:
            self._db.execute("UPDATE jobs SET state='error', error=?, result=?, finished=? WHERE id=?",
                             (json.dumps(err, default=str), json.dumps(result, default=str) if result is not None
                              else None, time.time(), jid))
            self._notify()

    def wait(self, jid: str, timeout: float) -> dict | None:
        """Block until job jid is done/error, its state/progress changes, or timeout. Returns the job."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            job = self.get(jid)
            if job is None or job["state"] in TERMINAL or timeout <= 0:
                return job
            start = (job["state"], job["progress"])
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return job
                self.changed.wait(left)
                job = self.get(jid)
                if job is None or job["state"] in TERMINAL:
                    return job
                if job["state"] != start[0]:
                    return job

    def counts(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state").fetchall()
            cur = self._db.execute("SELECT id, kind, progress FROM jobs WHERE state='running' LIMIT 1").fetchone()
        d: dict[str, Any] = {s: 0 for s in ("queued", "running", "done", "error")}
        d.update({r["state"]: r["n"] for r in rows})
        d["current"] = dict(cur) if cur else None
        return d

    def close(self) -> None:
        with self._lock:
            self._db.close()
