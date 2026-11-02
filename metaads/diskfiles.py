"""The files an ad leaves on disk, and the one guarded way to delete them, shared by the two cleanup jobs
(metaads.orphan_data and metaads.old_data).

An ad's files are its work folder (work_dir/<job id>/), the published ad and hook files (ad_path, hook_path) and the
.json next to each of those. Nothing is deleted outside work_dir, ads_dir and hooks_dir, and never inside body_dir or
poster_dir: the body and poster demo videos are reused by every ad.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .messages import CLEAN, WARN, blocks, entry, headline

log = logging.getLogger("metaads.diskfiles")

BYTES_PER_MB = 1024 * 1024


class Refused(ValueError):
    """A path the cleanup must not delete: outside the folders it owns, or a body/poster video."""


@dataclass
class Deleted:
    path: Path
    size: int                       # bytes
    group: str                      # what the report groups it under: a folder, or why it went


def path_key(path: str | Path) -> Path:
    """The path with its folders resolved but not the last name, so a symlink is judged by where it sits, never by what
    it points to. Two spellings of the same file give the same key."""
    p = Path(os.path.abspath(path))
    return p.parent.resolve() / p.name


def work_root(settings: Settings) -> Path:
    return Path(settings.paths.work_dir)


def job_files(settings: Settings, job: dict[str, Any]) -> list[Path]:
    """Everything on disk that belongs to one job: work folder, published ad and hook, and their .json sidecars."""
    paths = [work_root(settings) / job["id"]]
    for column in ("ad_path", "hook_path"):
        if job.get(column):
            published = Path(job[column])
            paths += [published, published.with_suffix(".json")]
    return paths


def _resolved(settings: Settings, name: str) -> Path:
    return Path(settings.paths[name]).resolve()


def check_deletable(settings: Settings, path: str | Path) -> Path:
    """The path's key when it may be deleted, else Refused."""
    key = path_key(path)
    for name in ("body_dir", "poster_dir"):
        root = _resolved(settings, name)
        if key == root or root in key.parents:
            raise Refused(f"{path} is in {name}, whose videos every ad reuses")
    if not any(_resolved(settings, name) in key.parents for name in ("work_dir", "ads_dir", "hooks_dir")):
        raise Refused(f"{path} is outside work_dir, ads_dir and hooks_dir")
    return key


def size_of(path: Path) -> int:
    """Bytes of a file, or of everything in a folder; a symlink counts as itself, never as what it points to."""
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = 0
    for folder, _, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue
    return total


def newest_change(path: Path) -> float:
    """Last modification time of a file, or of the most recently changed thing in a folder."""
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for folder, folders, names in os.walk(path, followlinks=False):
            for name in (*folders, *names):
                try:
                    newest = max(newest, os.lstat(os.path.join(folder, name)).st_mtime)
                except OSError:
                    continue
    return newest


def remove(settings: Settings, path: str | Path) -> int:
    """Delete a file, symlink or folder and return the bytes freed (0: it was already gone). Raises Refused for a path
    the cleanup does not own. A symlink is removed itself; what it points to is never followed."""
    key = check_deletable(settings, path)
    if not os.path.lexists(key):
        return 0
    freed = size_of(key)
    if key.is_dir() and not key.is_symlink():
        shutil.rmtree(key)
    else:
        key.unlink()
    return freed


def format_size(size: int) -> str:
    return f"{size / BYTES_PER_MB:,.1f} MB"


def report_text(label: str, items: list[Deleted], errors: list[str], apply: bool, settings: Settings) -> str | None:
    """The alert of a cleanup job: what was deleted, grouped, and what went wrong. Nothing deleted and nothing wrong:
    no alert. A dry run reports what it would delete, so trying the job out shows something."""
    if not items and not errors:
        return None
    count = f"{len(items)} item{'s' if len(items) != 1 else ''}"
    size = format_size(sum(d.size for d in items))
    head = (headline(CLEAN, f"{label}: {count} deleted, {size} freed") if apply
            else headline(CLEAN, f"{label} DRY RUN: {count} would be deleted, {size} would be freed (nothing was changed)"))
    groups: dict[str, list[Deleted]] = {}
    for d in items:
        groups.setdefault(d.group, []).append(d)
    shown = settings.messages.cleanup_names_shown
    entries = []
    for n, (group, members) in enumerate(groups.items(), 1):
        names = [d.path.name for d in members[:shown]]
        if len(members) > shown:
            names.append(f"and {len(members) - shown} more")
        entries.append(entry(f"{group}: {len(members)} item{'s' if len(members) != 1 else ''}, "
                             f"{format_size(sum(d.size for d in members))}", *names, n=n))
    problems = [headline(WARN, e) for e in errors]
    return blocks(head if items else None, *entries, *problems)
