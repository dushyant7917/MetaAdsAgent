"""Daily job that deletes the files of ads that are done with, so the disk does not fill up. Fixed rules only.

An ad's files are its work folder and its published ad (older ads also have a published hook and a .json beside each
file). They go when
  1. the ad was uploaded to Meta `old_data.uploaded_keep_days` ago, or its upload failed that long ago, or
  2. you dropped or redid it `old_data.discarded_keep_days` ago, or it failed (never finished) that long ago.
They stay while the ad is being made, awaits your approve/drop/redo, is being uploaded, or has a redo plan waiting for
your answer (for `discarded_keep_days` at most). Ads that are approved but not uploaded yet stay too.
Nothing is stored twice (see metaads.hookfiles): the hook files (image, clip, words, poster plan, hook video) live in the
work folder of the ad that made them and later ads on the same hook only point to them, so a work folder holds hook
data and nothing else. The one exception to the rules above is that hook data:
  - while the ad's hook is a winner that is not retired (ad_planning.retire_hook_after_failures), and
  - while the ad's ad set still runs below the win bar: its outcome is unknown and it may win days after the upload.
It goes once the ad set is paused without winning, or its hook is retired. A work folder also stays while any ad that is
still wanted reuses hook files from it, and a file two ads share stays while either is wanted. Everything else of the
ad (the finished ad, older copies and leftovers in a kept folder) goes like any other. Image ads keep nothing: a proven
image hook is made again as a new 9:16 image. Finding the ads to keep is ONE Meta read
(metaads.planning.Planner.hook_jobs_to_keep), made only when something is due; if it fails nothing is deleted.
The database rows are never deleted: the jobs table keeps every hook text and hook-body pair from being repeated.
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

from . import hookfiles, jobrunner
from .adaccount import make_client, managed_account
from .config import Settings
from .diskfiles import Deleted, Refused, job_files, path_key, remove, report_text, size_of
from .jobs import REVIEW_DROPPED, REVIEW_PENDING, REVIEW_REDONE, JobStore
from .meta import GraphClient
from .notify import Notifier, TelegramNotifier
from .planning import Planner

log = logging.getLogger("metaads.old_data")

LABEL = "Old data cleanup"
IN_PROGRESS = ("queued", "running")
HOOK_FILE_NAMES = set(hookfiles.HOOK_FILES.values())


@dataclass
class RunResult:
    checked: int = 0
    due: list[Deleted] = field(default_factory=list)                     # old enough to go
    deleted: list[Deleted] = field(default_factory=list)                 # really deleted
    kept_hook_folders: int = 0
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
    def __init__(self, settings: Settings, store: JobStore, keep_jobs: Callable[[], set[str]],
                 clock: Callable[[], float] = time.time):
        self.s, self.store, self.keep_jobs, self.clock = settings, store, keep_jobs, clock
        self.rules = settings.old_data

    def run(self, apply: bool) -> RunResult:
        out = RunResult()
        now = self.clock()
        jobs = self.store.cleanup_jobs()
        by_id = {j["id"]: j for j in jobs}
        out.checked = len(jobs)
        pending_redo = self.store.pending_redo_job_ids(now - timedelta(days=self.rules.discarded_keep_days).total_seconds())
        due, wanted = [], []
        for job in jobs:
            reason = expiry(job, pending_redo, now, self.rules)
            if reason is None:
                wanted.append(job)
                continue
            files = [p for p in job_files(self.s, job) if os.path.lexists(p)]
            if files:
                due.append((job, reason, files))
        if not due:
            return out
        keep = self.keep_jobs()                                          # raises when Meta cannot be read: nothing is deleted
        # Due files that still stay: every file of an ad that is still wanted (a redo that keeps the image shares the
        # file with the ad it came from), and the work folders that hold hook files of an ad still wanted or to keep
        # (a later ad reuses hook files of an earlier ad's folder, never a copy).
        protected = {path_key(p) for job in wanted for p in job_files(self.s, job)}
        hook_folders = set()
        for job in [*wanted, *(by_id[i] for i in keep if i in by_id)]:
            try:
                hook_folders |= {earlier["id"] for earlier in hookfiles.chain(job, by_id.__getitem__)}
            except KeyError:
                log.warning("ad %s reuses hook files of an ad that is not in the database", job["id"])
        for job, reason, files in due:
            work_folder = job_files(self.s, job)[0]
            for path in files:
                if path == work_folder and job["id"] in hook_folders:
                    out.kept_hook_folders += 1
                    for extra in sorted(work_folder.iterdir()):          # hook files stay, anything else goes
                        if extra.name not in HOOK_FILE_NAMES:
                            out.due.append(Deleted(extra, size_of(extra), reason))
                elif path_key(path) not in protected:
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
    def keep_jobs() -> set[str]:
        account_id, campaigns = managed_account(settings, environ)
        return Planner(settings, make_client(), account_id, campaigns, store).hook_jobs_to_keep()

    return jobrunner.run_job(
        LABEL, heartbeat, notifier, lambda: OldDataCleaner(settings, store, keep_jobs, clock).run(apply),
        summary=lambda r: f"checked {r.checked} ads, due {len(r.due)}, deleted {len(r.deleted)}, "
                          f"hook work folders kept {r.kept_hook_folders}, errors {len(r.errors)}",
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
