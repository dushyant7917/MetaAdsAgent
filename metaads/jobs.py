"""SQLite store for batches, per-ad jobs and video-generation usage (for the daily cap)."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, created REAL, chat_id TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, batch_id TEXT, idx INTEGER, language TEXT, spoken_text TEXT, thumbnail_text TEXT, body TEXT,
  duration INTEGER, status TEXT, stage TEXT, error TEXT, hook_path TEXT, ad_path TEXT, attempts INTEGER DEFAULT 0,
  created REAL, updated REAL, estimate TEXT, owner TEXT, redo_of TEXT, plan_hint TEXT, review TEXT, reviewed REAL,
  ad_format TEXT DEFAULT 'video', fallback_reason TEXT
);
CREATE TABLE IF NOT EXISTS redo_plans (
  id TEXT PRIMARY KEY, job_id TEXT, args TEXT, status TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS ad_plans (
  id TEXT PRIMARY KEY, created REAL, launch_day TEXT, status TEXT, items TEXT, summary TEXT
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
# What the owner decided about a finished ad: None = not part of the review loop, else one of REVIEW_STATES.
REVIEW_PENDING, REVIEW_APPROVED, REVIEW_DROPPED, REVIEW_REDONE = "pending", "approved", "dropped", "redone"
# The daily ad plan the owner is asked to approve before any ad is made.
PLAN_PENDING, PLAN_APPROVED, PLAN_REJECTED, PLAN_SUPERSEDED = "pending", "approved", "rejected", "superseded"
REVIEW_STATES = {REVIEW_PENDING, REVIEW_APPROVED, REVIEW_DROPPED, REVIEW_REDONE}
JOB_FIELDS = {"status", "stage", "error", "hook_path", "ad_path", "attempts", "duration", "body", "estimate", "owner",
              "ad_format", "fallback_reason"}
# What kind of file an ad is: a hook + body video, or a single 4:5 image.
FORMAT_VIDEO, FORMAT_IMAGE = "video", "image"


class JobStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            have = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
            if "text" in have:                                       # database from before the two texts were split
                c.execute("ALTER TABLE jobs RENAME COLUMN text TO spoken_text")
                have = (have - {"text"}) | {"spoken_text"}
            for col, kind in (("estimate", "TEXT"), ("owner", "TEXT"), ("redo_of", "TEXT"), ("plan_hint", "TEXT"),
                              ("review", "TEXT"), ("reviewed", "REAL"), ("thumbnail_text", "TEXT"),
                              ("ad_format", "TEXT DEFAULT 'video'"), ("fallback_reason", "TEXT")):
                if col not in have:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {kind}")    # database created before this column
            c.execute("UPDATE jobs SET thumbnail_text = spoken_text WHERE thumbnail_text IS NULL")
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=load_settings().timeouts.database)
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
                c.execute("INSERT INTO jobs (id,batch_id,idx,language,spoken_text,thumbnail_text,body,duration,status,"
                          "stage,created,updated,estimate,owner,redo_of,plan_hint,review,ad_format) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (job_id, batch_id, idx, it["language"], it["spoken_text"], it["thumbnail_text"], it.get("body"),
                           it.get("duration"), "queued", "queued", now, now, it.get("estimate"), owner,
                           it.get("redo_of"), it.get("plan_hint"), it.get("review"),
                           it.get("ad_format", FORMAT_VIDEO)))
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

    def recent_jobs(self, limit: int) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created DESC, idx LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- review: what the owner decided about each finished ad (see REVIEW_STATES)
    def set_review(self, job_ids: list[str], review: str) -> None:
        if review not in REVIEW_STATES:
            raise ValueError(f"Unknown review state: {review}")
        now = time.time()
        with closing(self._conn()) as c, c:
            c.executemany("UPDATE jobs SET review=?, reviewed=? WHERE id=?", [(review, now, j) for j in job_ids])

    def awaiting_review_jobs(self) -> list[dict[str, Any]]:
        """Ads waiting for the owner's decision (also those still being made), oldest first."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM jobs WHERE review=? AND status != 'failed' ORDER BY created, idx",
                             (REVIEW_PENDING,)).fetchall()
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

    def video_calls_today(self) -> int:
        """Video generations since local midnight: what the daily video cap counts."""
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.video_calls_since(midnight)

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

    # ---- daily ad plans: proposed first, ads are made only after the owner approves
    def create_ad_plan(self, plan_id: str, launch_day: str, items: list[dict], summary: dict) -> None:
        """Store a new pending plan; every older pending plan is superseded (only the newest can be approved)."""
        with closing(self._conn()) as c, c:
            c.execute("UPDATE ad_plans SET status=? WHERE status=?", (PLAN_SUPERSEDED, PLAN_PENDING))
            c.execute("INSERT INTO ad_plans VALUES (?,?,?,?,?,?)",
                      (plan_id, time.time(), launch_day, PLAN_PENDING, json.dumps(items, ensure_ascii=False),
                       json.dumps(summary, ensure_ascii=False)))

    def supersede_pending_ad_plans(self) -> None:
        with closing(self._conn()) as c, c:
            c.execute("UPDATE ad_plans SET status=? WHERE status=?", (PLAN_SUPERSEDED, PLAN_PENDING))

    @staticmethod
    def _ad_plan(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {**dict(row), "items": json.loads(row["items"]), "summary": json.loads(row["summary"])}

    def get_ad_plan(self, plan_id: str) -> dict[str, Any] | None:
        with closing(self._conn()) as c:
            return self._ad_plan(c.execute("SELECT * FROM ad_plans WHERE id=?", (plan_id,)).fetchone())

    def latest_ad_plan(self, status: str | None = None) -> dict[str, Any] | None:
        with closing(self._conn()) as c:
            row = (c.execute("SELECT * FROM ad_plans WHERE status=? ORDER BY created DESC LIMIT 1", (status,))
                   if status else c.execute("SELECT * FROM ad_plans ORDER BY created DESC LIMIT 1")).fetchone()
        return self._ad_plan(row)

    def update_ad_plan_items(self, plan_id: str, items: list[dict]) -> None:
        with closing(self._conn()) as c, c:
            c.execute("UPDATE ad_plans SET items=? WHERE id=?", (json.dumps(items, ensure_ascii=False), plan_id))

    def set_ad_plan_status(self, plan_id: str, old: str, new: str) -> bool:
        """Atomic: False when the plan was not in state `old` (someone else got there first)."""
        with closing(self._conn()) as c, c:
            return c.execute("UPDATE ad_plans SET status=? WHERE id=? AND status=?", (new, plan_id, old)).rowcount == 1

    # ---- hook texts already used, so new hooks are not repeats and the writer sees the recent ones
    def known_hooks(self, language: str | None = None, limit: int | None = None) -> list[dict[str, str]]:
        """(spoken_text, thumbnail_text) of ads that did not fail, newest first; one language or all."""
        sql = "SELECT DISTINCT language, spoken_text, thumbnail_text FROM jobs WHERE status != 'failed'"
        args: list[Any] = []
        if language:
            sql += " AND language = ?"
            args.append(language)
        sql += " ORDER BY created DESC" + (" LIMIT ?" if limit else "")
        if limit:
            args.append(limit)
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # ---- what the planner needs to tell winning hooks from fresh ones
    def uploaded_jobs(self) -> list[dict[str, Any]]:
        """Jobs that have an ad set on Meta: job id, language code, both hook texts, body (None = default), format,
        made-at time and ad set id."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT j.id AS job_id, j.language, j.spoken_text, j.thumbnail_text, j.body, j.ad_format, "
                             "j.created, u.adset_id FROM jobs j "
                             "JOIN meta_uploads u ON u.job_id = j.id WHERE u.adset_id IS NOT NULL").fetchall()
        return [dict(r) for r in rows]

    def hook_body_pairs(self) -> set[tuple[str, str, str, str | None]]:
        """(language code, spoken text, thumbnail text, body) of every video ad ever made that did not fail: pairs not
        to repeat. Image ads are left out: no body video was ever made with their hook."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT language, spoken_text, thumbnail_text, body FROM jobs "
                             "WHERE status != 'failed' AND ad_format = ?", (FORMAT_VIDEO,)).fetchall()
        return {(r["language"], r["spoken_text"], r["thumbnail_text"], r["body"]) for r in rows}
