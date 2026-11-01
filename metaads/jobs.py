"""SQLite store for batches, per-ad jobs and video-generation usage (for the daily cap)."""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, created REAL, chat_id TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, batch_id TEXT, idx INTEGER, language TEXT, text TEXT, body TEXT, duration INTEGER,
  status TEXT, stage TEXT, error TEXT, hook_path TEXT, ad_path TEXT, attempts INTEGER DEFAULT 0,
  created REAL, updated REAL, estimate TEXT, owner TEXT, redo_of TEXT, plan_hint TEXT
);
CREATE TABLE IF NOT EXISTS redo_plans (
  id TEXT PRIMARY KEY, job_id TEXT, args TEXT, status TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS video_calls (ts REAL);
CREATE TABLE IF NOT EXISTS meta_uploads (
  job_id TEXT PRIMARY KEY, upload_id TEXT, status TEXT, stage TEXT, error TEXT,
  video_id TEXT, image_hash TEXT, adset_id TEXT, ad_id TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS budget_increases (
  adset_id TEXT, ts REAL, old_budget INTEGER, new_budget INTEGER
);
CREATE INDEX IF NOT EXISTS budget_increases_adset ON budget_increases (adset_id, ts);
"""

META_FIELDS = {"upload_id", "status", "stage", "error", "video_id", "image_hash", "adset_id", "ad_id"}
JOB_FIELDS = {"status", "stage", "error", "hook_path", "ad_path", "attempts", "duration", "body", "estimate", "owner"}


class JobStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            have = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
            for col in ("estimate", "owner", "redo_of", "plan_hint"):
                if col not in have:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")    # database created before this column

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def create_batch(self, items: list[dict[str, Any]], chat_id: str | None = None,
                     owner: str | None = None) -> tuple[str, list[str]]:
        batch_id = "b" + uuid.uuid4().hex[:8]
        now = time.time()
        job_ids = []
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO batches VALUES (?,?,?)", (batch_id, now, chat_id))
            for idx, it in enumerate(items):
                job_id = f"{batch_id}-{idx + 1}"
                job_ids.append(job_id)
                c.execute("INSERT INTO jobs (id,batch_id,idx,language,text,body,duration,status,stage,created,updated,"
                          "estimate,owner,redo_of,plan_hint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (job_id, batch_id, idx, it["language"], it["text"], it.get("body"), it.get("duration"),
                           "queued", "queued", now, now, it.get("estimate"), owner, it.get("redo_of"),
                           it.get("plan_hint")))
        return batch_id, job_ids

    def update_job(self, job_id: str, **fields: Any) -> None:
        bad = set(fields) - JOB_FIELDS
        if bad:
            raise ValueError(f"Unknown job fields: {bad}")
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with closing(self._conn()) as c, c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def get_job(self, job_id: str) -> dict[str, Any]:
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def batch_chat_id(self, batch_id: str) -> str | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT chat_id FROM batches WHERE id=?", (batch_id,)).fetchone()
        return row["chat_id"] if row else None

    def batch_jobs(self, batch_id: str) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY idx", (batch_id,)).fetchall()
        if not rows:
            raise KeyError(batch_id)
        return [dict(r) for r in rows]

    def recent_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created DESC, idx LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- redo plans: a plan is proposed first and only runs after the owner approves it
    def create_redo_plan(self, plan_id: str, job_id: str, args: str) -> None:
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO redo_plans VALUES (?,?,?,?,?)", (plan_id, job_id, args, "pending", time.time()))

    def get_redo_plan(self, plan_id: str) -> dict[str, Any] | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM redo_plans WHERE id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def set_redo_plan_status(self, plan_id: str, old: str, new: str) -> bool:
        """Atomic, so an approval can only be used once."""
        with closing(self._conn()) as c, c:
            return c.execute("UPDATE redo_plans SET status=? WHERE id=? AND status=?",
                             (new, plan_id, old)).rowcount == 1

    def unfinished_jobs(self) -> list[dict[str, Any]]:
        """Jobs that still have to run: queued, or running (possibly left behind by a dead process)."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM jobs WHERE status IN ('queued','running') "
                             "ORDER BY batch_id, idx").fetchall()
        return [dict(r) for r in rows]

    def claim_job(self, job_id: str, old_owner: str | None, new_owner: str) -> bool:
        """Take a job over from `old_owner`. Atomic, so two servers can never both claim the same job."""
        with closing(self._conn()) as c, c:
            cur = c.execute("UPDATE jobs SET owner=?, updated=? WHERE id=? AND owner IS ? "
                            "AND status IN ('queued','running')", (new_owner, time.time(), job_id, old_owner))
            return cur.rowcount == 1

    def record_video_call(self) -> None:
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO video_calls VALUES (?)", (time.time(),))

    def video_calls_since(self, since: float) -> int:
        with closing(self._conn()) as c:
            return c.execute("SELECT COUNT(*) FROM video_calls WHERE ts>=?", (since,)).fetchone()[0]

    # ---- budget increases made by metaads.budget: when each ad set was last raised (budgets are in paise)
    def last_budget_increases(self) -> dict[str, float]:
        """ad set id -> time of its most recent increase, for every ad set that ever had one."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT adset_id, MAX(ts) AS ts FROM budget_increases GROUP BY adset_id").fetchall()
        return {r["adset_id"]: r["ts"] for r in rows}

    def record_budget_increase(self, adset_id: str, old_budget: int, new_budget: int, ts: float | None = None) -> None:
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO budget_increases VALUES (?,?,?,?)",
                      (adset_id, time.time() if ts is None else ts, old_budget, new_budget))

    # ---- Meta uploads: what already exists on Meta for a job, so a rerun never repeats a call or an entity
    def upsert_meta_upload(self, job_id: str, **fields: Any) -> None:
        bad = set(fields) - META_FIELDS
        if bad:
            raise ValueError(f"Unknown meta upload fields: {bad}")
        fields["updated"] = time.time()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        sets = ", ".join(f"{k}=excluded.{k}" for k in fields)
        with closing(self._conn()) as c, c:
            c.execute(f"INSERT INTO meta_uploads (job_id, {cols}) VALUES (?, {marks}) "
                      f"ON CONFLICT(job_id) DO UPDATE SET {sets}", (job_id, *fields.values()))

    def get_meta_upload(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM meta_uploads WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def meta_uploads_for(self, upload_id: str) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM meta_uploads WHERE upload_id=? ORDER BY job_id", (upload_id,)).fetchall()
        return [dict(r) for r in rows]
