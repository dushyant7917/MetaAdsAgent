"""Daily job that deletes the files of ads that are done with, so the disk does not fill up. Fixed rules only.

An ad's files are its work folder, its published ad and hook files and the .json beside each. They go when
  1. the ad was uploaded to Meta `old_data.uploaded_keep_days` ago, or its upload failed that long ago, or
  2. you dropped or redid it `old_data.discarded_keep_days` ago, or it failed (never finished) that long ago.
They stay while the ad is being made, awaits your approve/drop/redo, is being uploaded, or has a redo plan waiting for
your answer (for `discarded_keep_days` at most). Ads that are approved but not uploaded yet stay too.
The one exception is the work folder of a winning ad whose hook is still reusable: a new ad on a proven hook is a redo
of it and reuses its image, clip, words and poster plan, so that folder stays until the hook is retired
(ad_planning.retire_hook_after_failures); its published files go like any other. Finding the winners is ONE Meta read
(metaads.planning.Planner.reusable_hook_jobs), made only when something is due; if it fails nothing is deleted.
The database rows are never deleted: known_hooks and hook_body_pairs keep every hook text and pair from being repeated.
The body and poster demo videos are never touched. Files no ad points to are the business of metaads.orphan_data.
Run it with `python -m metaads.old_data` (dry run) or `... --apply` (deletes for real);
scripts/install_old_data_cleanup_timer.sh schedules it.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from . import jobrunner
from .adaccount import make_client, managed_account
from .config import Settings
from .diskfiles import Deleted, Refused, job_files, remove, report_text, size_of
from .jobs import REVIEW_DROPPED, REVIEW_PENDING, REVIEW_REDONE, JobStore
from .meta import GraphClient
from .notify import Notifier, TelegramNotifier
from .planning import Planner

log = logging.getLogger("metaads.old_data")

LABEL = "Old data cleanup"
IN_PROGRESS = ("queued", "running")


@dataclass
class RunResult:
    checked: int = 0
    due: list[Deleted] = field(default_factory=list)                     # old enough to go
    deleted: list[Deleted] = field(default_factory=list)                 # really deleted
    kept_winner_folders: int = 0
    errors: list[str] = field(default_factory=list)


def older_than(timestamp: float | None, days: float, now: float) -> bool:
    return timestamp is not None and now - timestamp >= timedelta(days=days).total_seconds()


def expiry(job: dict[str, Any], pending_redo: set[str], now: float, cfg: Settings) -> str | None:
    """Why this ad's files may go now (the report groups by it), or None while they are still wanted. `job` is a row of
    JobStore.cleanup_jobs(); `pending_redo` the ads with an unanswered redo plan."""
    if job["status"] in IN_PROGRESS or job["id"] in pending_redo or job["upload_status"] in IN_PROGRESS:
        return None
    uploaded = job["upload_status"] == "done" and job["ad_id"]
    if uploaded:
        return (f"uploaded to Meta {cfg.uploaded_keep_days}+ days ago"
                if older_than(job["upload_updated"], cfg.uploaded_keep_days, now) else None)
    if job["review"] == REVIEW_PENDING and job["status"] != "failed":
        return None                                                      # waiting for approve / drop / redo
    if job["upload_status"] == "failed":
        return (f"upload to Meta failed {cfg.uploaded_keep_days}+ days ago"
                if older_than(job["upload_updated"], cfg.uploaded_keep_days, now) else None)
    if job["review"] in (REVIEW_DROPPED, REVIEW_REDONE):
        return (f"{job['review']} {cfg.discarded_keep_days}+ days ago"
                if older_than(job["reviewed"] or job["updated"], cfg.discarded_keep_days, now) else None)
    if job["status"] == "failed":
        return (f"ad failed {cfg.discarded_keep_days}+ days ago"
                if older_than(job["updated"], cfg.discarded_keep_days, now) else None)
    return None                                                          # done, maybe approved, not uploaded yet


class OldDataCleaner:
    def __init__(self, settings: Settings, store: JobStore, winner_jobs: Callable[[], set[str]],
                 clock: Callable[[], float] = time.time):
        self.s, self.store, self.winner_jobs, self.clock = settings, store, winner_jobs, clock
        self.rules = settings.old_data

    def run(self, apply: bool) -> RunResult:
        out = RunResult()
        now = self.clock()
        jobs = self.store.cleanup_jobs()
        out.checked = len(jobs)
        pending_redo = self.store.pending_redo_job_ids(now - timedelta(days=self.rules.discarded_keep_days).total_seconds())
        due = []
        for job in jobs:
            reason = expiry(job, pending_redo, now, self.rules)
            files = [p for p in job_files(self.s, job) if os.path.lexists(p)] if reason else []
            if files:
                due.append((job, reason, files))
        if not due:
            return out
        winners = self.winner_jobs()                                     # raises when Meta cannot be read: nothing is deleted
        for job, reason, files in due:
            work_folder = job_files(self.s, job)[0]
            for path in files:
                if path == work_folder and job["id"] in winners:
                    out.kept_winner_folders += 1
                    continue
                out.due.append(Deleted(path, size_of(path), reason))
        for item in out.due:
            log.info("%s: %s (%d bytes) %s", item.group, item.path, item.size, "DELETE" if apply else "DRY would delete")
        if apply:
            for item in out.due:
                try:
                    if remove(self.s, item.path):
                        out.deleted.append(item)
                except Refused as e:
                    log.warning("not deleted: %s", e)
                except OSError as e:
                    out.errors.append(f"could not delete {item.path}: {e}")
        return out


def run_job(settings: Settings, make_client: Callable[[], GraphClient], notifier: Notifier, environ, store: JobStore,
            apply: bool, heartbeat: str, clock: Callable[[], float] = time.time) -> int:
    def winner_jobs() -> set[str]:
        account_id, campaigns = managed_account(settings, environ)
        return Planner(settings, make_client(), account_id, campaigns, store).reusable_hook_jobs()

    return jobrunner.run_job(
        LABEL, heartbeat, notifier, lambda: OldDataCleaner(settings, store, winner_jobs, clock).run(apply),
        summary=lambda r: f"checked {r.checked} ads, due {len(r.due)}, deleted {len(r.deleted)}, "
                          f"winner work folders kept {r.kept_winner_folders}, errors {len(r.errors)}",
        report=lambda r: report_text(LABEL, r.deleted if apply else r.due, r.errors, apply, settings))


def main(argv: list[str] | None = None) -> int:
    return jobrunner.run_cli(
        argv, label="old data", description="Delete the files of ads that were uploaded, dropped or failed long enough ago.",
        apply_help="really delete (default: dry run, only report)", lock_name="old_data.lock",
        heartbeat_env="HEALTHCHECK_URL_OLD_DATA_CLEANUP",
        run=lambda settings, apply, heartbeat: run_job(settings, lambda: make_client(settings), TelegramNotifier.from_env(),
                                                       os.environ, JobStore(settings.paths.db), apply, heartbeat))


if __name__ == "__main__":
    sys.exit(main())
