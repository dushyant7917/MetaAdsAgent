"""Daily job that deletes the files and folders on disk that no ad points to, so the disk does not fill up with leftovers.

It looks at the direct children of
  work_dir               a work folder is linked to the job whose id is its name
  ads_dir/<language>/    a file is linked when it is a job's ad_path (or the .json beside it)
  hooks_dir/<language>/  the same for a job's hook_path
and deletes what is not linked once nothing in it has changed for `orphan_data.min_age_hours` (a job may still be
writing it). That covers crash leftovers (.part files), ad-hoc output (work_dir/adhoc, the make_hook and merge_ad tools)
and files of an ad whose path changed (a video ad that fell back to an image ad). The body and poster demo videos are
never looked at and never deleted. Files of an ad that IS in the database are the business of metaads.old_data.
No Meta calls. It deletes nothing when the jobs table is empty or work_dir, ads_dir or hooks_dir is missing (an
unmounted drive would make every file look like an orphan).
Run it with `python -m metaads.orphan_data` (dry run) or `... --apply` (deletes for real);
scripts/install_orphan_data_cleanup_timer.sh schedules it.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import jobrunner
from .config import Settings
from .diskfiles import Deleted, Refused, job_files, newest_change, path_key, remove, report_text, size_of
from .jobs import JobStore
from .notify import Notifier, TelegramNotifier

log = logging.getLogger("metaads.orphan_data")

LABEL = "Orphan data cleanup"
SECONDS_PER_HOUR = 3600
ROOT_SETTINGS = ("work_dir", "ads_dir", "hooks_dir")


@dataclass
class RunResult:
    checked: int = 0
    orphans: list[Deleted] = field(default_factory=list)                 # found, old enough to go
    deleted: list[Deleted] = field(default_factory=list)                 # really deleted
    errors: list[str] = field(default_factory=list)


class OrphanCleaner:
    def __init__(self, settings: Settings, store: JobStore, clock: Callable[[], float] = time.time):
        self.s, self.store, self.clock = settings, store, clock
        self.min_age = settings.orphan_data.min_age_hours * SECONDS_PER_HOUR

    def folders(self) -> Iterator[tuple[str, Path]]:
        """(name the report groups by, folder) of every folder whose children are checked."""
        yield "work", Path(self.s.paths.work_dir)
        for code in self.s.languages:
            yield f"ads/{code}", Path(self.s.paths.ads_dir) / code
            yield f"hooks/{code}", Path(self.s.paths.hooks_dir) / code

    def run(self, apply: bool) -> RunResult:
        out = RunResult()
        jobs = self.store.cleanup_jobs()
        if not jobs:
            out.errors.append("The jobs table is empty, so every file would look like an orphan. Nothing was deleted.")
            return out
        missing = [name for name in ROOT_SETTINGS if not Path(self.s.paths[name]).is_dir()]
        if missing:
            out.errors.append(f"{', '.join(missing)} not found (drive not mounted?). Nothing was deleted.")
            return out
        linked = {path_key(p) for job in jobs for p in job_files(self.s, job)}
        newest_allowed = self.clock() - self.min_age
        for group, folder in self.folders():
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                out.checked += 1
                if path_key(path) in linked:
                    continue
                try:
                    if newest_change(path) > newest_allowed:
                        continue
                    out.orphans.append(Deleted(path, size_of(path), group))
                except OSError as e:                                         # gone or unreadable meanwhile
                    out.errors.append(f"could not inspect {path}: {e}")
        for item in out.orphans:
            log.info("%s: orphan %s (%d bytes) %s", item.group, item.path.name, item.size,
                     "DELETE" if apply else "DRY would delete")
        if apply:
            for item in out.orphans:
                try:
                    if remove(self.s, item.path):
                        out.deleted.append(item)
                except Refused as e:
                    log.warning("not deleted: %s", e)
                except OSError as e:
                    out.errors.append(f"could not delete {item.path}: {e}")
        return out


def run_job(settings: Settings, notifier: Notifier, store: JobStore, apply: bool, heartbeat: str,
            clock: Callable[[], float] = time.time) -> int:
    return jobrunner.run_job(
        LABEL, heartbeat, notifier, lambda: OrphanCleaner(settings, store, clock).run(apply),
        summary=lambda r: f"checked {r.checked}, orphans {len(r.orphans)}, deleted {len(r.deleted)}, errors {len(r.errors)}",
        report=lambda r: report_text(LABEL, r.deleted if apply else r.orphans, r.errors, apply, settings))


def main(argv: list[str] | None = None) -> int:
    return jobrunner.run_cli(
        argv, label="orphan data", description="Delete the files and folders on disk that no ad points to.",
        apply_help="really delete (default: dry run, only report)", lock_name="orphan_data.lock",
        heartbeat_env="HEALTHCHECK_URL_ORPHAN_DATA_CLEANUP",
        run=lambda settings, apply, heartbeat: run_job(settings, TelegramNotifier.from_env(), JobStore(settings.paths.db),
                                                       apply, heartbeat))


if __name__ == "__main__":
    sys.exit(main())
