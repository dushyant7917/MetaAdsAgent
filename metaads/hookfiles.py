"""Where the files of an ad's hook live. Every file is made once and never copied.

A video ad is built in a fixed order and every stage feeds the next one:

    image -> video (Veo) -> transcribe (Whisper) -> plan (poster window) -> render_hook -> merge

The first five stages make the hook (HOOK_FILES, in the job's work folder work_dir/<job id>/). The last one joins the
hook and a body video into the final ad, which is the only file that is unique to an ad: it goes straight to ads_dir.

A redo or a new ad on a proven hook reuses the hook files of an earlier ad without copying them: the new job stores
`redo_of` (the earlier ad) and `reused_stages` (how many leading stages it takes from it). Stage i of a job is a file of
its own folder when i >= reused_stages, else the same stage of `redo_of`, which may itself reuse from its own `redo_of`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import Settings

STAGES = ["image", "video", "transcribe", "plan", "render_hook", "merge"]
HOOK_STAGES = STAGES[:-1]
MERGE_STAGE = STAGES[-1]
HOOK_FILES = {"image": "image.png", "video": "video.mp4", "transcribe": "words.json", "plan": "plan.json",
              "render_hook": "hook.mp4"}
MERGED_FILE = "ad.mp4"                      # the merge output while it waits to be moved to ads_dir (never kept)
IMAGE_AD_FILE = "image_ad.png"              # the 4:5 image of an image ad while it waits to be moved to ads_dir

Lookup = Callable[[str], dict[str, Any]]    # job id -> job row


class ReusedFileMissing(RuntimeError):
    retryable = False                       # a retry cannot bring the file back


def reused_stages(job: dict[str, Any]) -> int:
    return job.get("reused_stages") or 0


def is_reused(job: dict[str, Any], stage: str) -> bool:
    return HOOK_STAGES.index(stage) < reused_stages(job)


def owner(job: dict[str, Any], stage: str, get: Lookup) -> dict[str, Any]:
    """The job whose work folder holds the file of `stage` that `job` uses."""
    index = HOOK_STAGES.index(stage)
    while index < reused_stages(job):
        job = get(job["redo_of"])
    return job


def chain(job: dict[str, Any], get: Lookup) -> list[dict[str, Any]]:
    """`job` and every earlier ad whose work folder holds hook files it uses (nearest first)."""
    jobs = [job]
    while reused_stages(job):
        job = get(job["redo_of"])
        jobs.append(job)
    return jobs


def work_folder(settings: Settings, job_id: str) -> Path:
    return Path(settings.paths.work_dir) / job_id


def stage_file(settings: Settings, job: dict[str, Any], stage: str, get: Lookup) -> Path:
    """The path of the hook file of `stage` that `job` uses (it may not exist yet when the job makes it itself)."""
    return work_folder(settings, owner(job, stage, get)["id"]) / HOOK_FILES[stage]


def existing_stage_file(settings: Settings, job: dict[str, Any], stage: str, get: Lookup) -> Path:
    """Like stage_file, but a reused file that is gone is an error: it must never be made again in someone else's folder."""
    path = stage_file(settings, job, stage, get)
    if is_reused(job, stage) and not path.is_file():
        raise ReusedFileMissing(f"The {stage} file that ad {job['id']} reuses from ad {job['redo_of']} is gone ({path})")
    return path
