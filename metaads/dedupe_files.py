"""One-off migration of data made before nothing was stored twice (see metaads.hookfiles): every copy that earlier versions
left on disk and that is byte for byte the same as a file that stays is replaced by a reference to that file.
  1. A redo whose first stages were copied from the ad it came from: those copies go and the job records `redo_of` +
     `reused_stages` instead. Only the leading stages that are identical to the earlier ad's file count.
  2. An image ad redo that kept the image: it points to the earlier ad's image file, its own copy goes.
  3. `ad.mp4` / `image_ad.png` left in a work folder after the same file was copied to ads_dir.
  4. A published hook copy in hooks_dir when the work folder still has the same hook video (hook_path is cleared).
  5. The .json beside published files: its facts are all in the database and the work files.
A file is deleted only after it was compared with a surviving copy, so the only copy of anything is never deleted. The
database is changed before any file is deleted, so a failure leaves a spare copy, never a missing file.
Run it with `python -m metaads.dedupe_files` (dry run) or `... --apply`. It shares old_data.lock with the old data
cleanup, so the two never run together. Once it has been run on every machine this module can be deleted.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hookfiles, jobrunner
from .config import Settings
from .diskfiles import Deleted, Refused, remove, report_text, size_of
from .jobs import FORMAT_IMAGE, FORMAT_VIDEO, JobStore

log = logging.getLogger("metaads.dedupe_files")

LABEL = "Duplicate files cleanup"
CHUNK = 1 << 20


@dataclass
class RunResult:
    checked: int = 0
    linked: list[str] = field(default_factory=list)                      # ads that now point to files of another ad
    due: list[Deleted] = field(default_factory=list)                     # duplicates found
    deleted: list[Deleted] = field(default_factory=list)                 # really deleted
    errors: list[str] = field(default_factory=list)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def identical(a: Path, b: Path) -> bool:
    """Both are files with the same bytes (and not the same file)."""
    if not (a.is_file() and b.is_file()) or a.resolve() == b.resolve():
        return False
    return a.stat().st_size == b.stat().st_size and digest(a) == digest(b)


class Deduper:
    def __init__(self, settings: Settings, store: JobStore):
        self.s, self.store = settings, store

    def run(self, apply: bool) -> RunResult:
        out = RunResult()
        jobs = self.store.cleanup_jobs()                                 # oldest first: an ad comes after the one it reuses
        by_id = {j["id"]: j for j in jobs}
        out.checked = len(jobs)
        seen: set[Path] = set()
        for job in jobs:
            if job["status"] != "done":
                continue
            updates: dict[str, Any] = {}
            found: list[Deleted] = [Deleted(p, size_of(p), "the .json beside a published file") for p in self._sidecars(job)]
            self._reuse_hook_files(job, by_id, updates, found)
            self._share_image(job, by_id, updates, found)
            job.update(updates)                                          # what follows (and later ads) see the references
            self._left_over(job, by_id, updates, found)
            job.update(updates)
            found = [d for d in found if d.path not in seen]
            seen |= {d.path for d in found}
            if updates.keys() & {"reused_stages", "ad_path"}:
                out.linked.append(job["id"])
            out.due += found
            if apply:
                if updates:
                    self.store.relink_files(job["id"], **updates)        # first: a spare copy is better than a missing file
                for item in found:
                    try:
                        if remove(self.s, item.path):
                            out.deleted.append(item)
                    except Refused as e:
                        log.warning("not deleted: %s", e)
                    except OSError as e:
                        out.errors.append(f"could not delete {item.path}: {e}")
        for item in out.due:
            log.info("%s: %s (%d bytes) %s", item.group, item.path, item.size, "DELETE" if apply else "DRY would delete")
        return out

    def _reuse_hook_files(self, job: dict, by_id: dict[str, dict], updates: dict, found: list[Deleted]) -> None:
        parent = by_id.get(job["redo_of"] or "")
        if parent is None or job["ad_format"] != FORMAT_VIDEO or hookfiles.reused_stages(job):
            return
        same = []
        for stage in hookfiles.HOOK_STAGES:
            own = hookfiles.stage_file(self.s, job, stage, by_id.__getitem__)
            earlier = hookfiles.stage_file(self.s, parent, stage, by_id.__getitem__)
            if not identical(own, earlier):
                break
            same.append(own)
        if same:
            updates["reused_stages"] = len(same)
            found += [Deleted(p, size_of(p), f"the same hook file as ad {parent['id']}") for p in same]

    def _share_image(self, job: dict, by_id: dict[str, dict], updates: dict, found: list[Deleted]) -> None:
        parent = by_id.get(job["redo_of"] or "")
        if parent is None or job["ad_format"] != FORMAT_IMAGE or not job["ad_path"] or not parent["ad_path"]:
            return
        mine, theirs = Path(job["ad_path"]), Path(parent["ad_path"])
        if mine != theirs and identical(mine, theirs):
            updates["ad_path"] = str(theirs)
            found.append(Deleted(mine, size_of(mine), f"the same image as ad {parent['id']}"))

    def _left_over(self, job: dict, by_id: dict[str, dict], updates: dict, found: list[Deleted]) -> None:
        work = hookfiles.work_folder(self.s, job["id"])
        published = Path(job["ad_path"]) if job["ad_path"] else None
        if published:
            for name in (hookfiles.MERGED_FILE, hookfiles.IMAGE_AD_FILE):
                if identical(work / name, published):
                    found.append(Deleted(work / name, size_of(work / name), "the finished ad is in ads_dir"))
        hook = Path(job["hook_path"]) if job["hook_path"] else None
        if hook and hook.is_file():
            own = hookfiles.stage_file(self.s, job, "render_hook", by_id.__getitem__)
            if identical(own, hook):
                updates["hook_path"] = None
                found.append(Deleted(hook, size_of(hook), "the published hook is the hook video of the work folder"))

    @staticmethod
    def _sidecars(job: dict) -> list[Path]:
        paths = [Path(job[column]) for column in ("ad_path", "hook_path") if job[column]]
        return [p.with_suffix(".json") for p in paths if p.with_suffix(".json").exists()]


def run_job(settings: Settings, store: JobStore, apply: bool) -> int:
    result = Deduper(settings, store).run(apply)
    log.info("checked %d ads, linked %d, duplicates %d, deleted %d, errors %d",
             result.checked, len(result.linked), len(result.due), len(result.deleted), len(result.errors))
    print(report_text(LABEL, result.deleted if apply else result.due, result.errors, apply, settings)
          or f"{LABEL}: nothing is stored twice.")
    if result.linked:
        print(f"{len(result.linked)} ads now {'point' if apply else 'would point'} to files of the ad they came from: "
              + ", ".join(result.linked))
    return 1 if result.errors else 0


def main(argv: list[str] | None = None) -> int:
    return jobrunner.run_cli(
        argv, label="duplicate files", description="Replace copies on disk by references to the file that stays.",
        apply_help="really change the database and delete (default: dry run, only report)", lock_name="old_data.lock",
        heartbeat_env="HEALTHCHECK_URL_DEDUPE_FILES",
        run=lambda settings, apply, heartbeat: run_job(settings, JobStore(settings.paths.db), apply))


if __name__ == "__main__":
    sys.exit(main())
