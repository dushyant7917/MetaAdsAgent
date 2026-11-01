"""MCP server (stdio) exposing the Meta ads tools to Hermes or any MCP client.

Run: uv run python -m metaads.server
Logs go to stderr; stdout is the protocol channel.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import media
from .config import load_env_file, load_settings
from .notify import TelegramNotifier
from .pipeline import Pipeline, ValidationError
from .redo import RedoPlanner
from .uploader import MetaUploader

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

load_env_file()          # keys live in the Hermes profile's .env (METAADS_ENV_FILE), never in the repo
mcp = FastMCP("metaads")
S = load_settings()
_pipeline: Pipeline | None = None
_uploader: MetaUploader | None = None


def pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline(S, notifier=TelegramNotifier.from_env())
    return _pipeline


def uploader() -> MetaUploader:
    global _uploader
    if _uploader is None:
        p = pipeline()
        _uploader = MetaUploader(S, p.store, notifier=p.notifier)
    return _uploader


def redo() -> RedoPlanner:
    return RedoPlanner(pipeline())


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _adhoc(name: str) -> Path:
    d = Path(S.paths.work_dir) / "adhoc"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_stamp()}_{name}"


async def _blocking(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------- main entry points ----------------

@mcp.tool()
def create_ads(items: list[dict[str, Any]], chat_id: str | None = None) -> dict:
    """Create one complete ad (hook + body, cross-faded) for EACH item, in the background.

    items: list of {language, text, body?, duration?}.
      language: code from list_languages (hi, bn, gu, kn, mr, ta, te).
      text: the hook text the reporter speaks (as the user wrote it).
      body: optional body video name or unique part of it (e.g. "teal"); default navy-darkyellow-yellowbox.
      duration: optional clip length 4, 6 or 8 seconds; by default chosen from text length and language.
    Returns immediately with a batch_id. Each finished ad is sent to Telegram on its own, and a summary
    follows at the end. Use get_batch_status to check progress. The whole request is refused up front
    (nothing runs) if any item is invalid or a limit would be exceeded.
    """
    try:
        batch_id = pipeline().submit(items, chat_id=chat_id)
    except ValidationError as exc:
        return {"started": False, "error": str(exc)}
    return {"started": True, "batch_id": batch_id, "ads": len(items),
            "note": "Each ad is delivered to Telegram as it finishes."}


@mcp.tool()
def get_batch_status(batch_id: str) -> dict:
    """Progress of a create_ads batch: counts, and per-ad status, stage, file paths and errors."""
    try:
        return pipeline().status(batch_id)
    except KeyError:
        return {"error": f"Unknown batch {batch_id}"}


@mcp.tool()
def list_ads(limit: int = 10) -> dict:
    """Recent ads, newest first: job_id, language, text, status, file paths and whether the ad is on Meta
    (meta_status, meta_ad_id). Use it to find which ad the owner means."""
    store = pipeline().store
    ads = []
    for j in store.recent_jobs(max(1, min(limit, 30))):
        meta = store.get_meta_upload(j["id"]) or {}
        ads.append({"job_id": j["id"], "language": j["language"], "text": j["text"][:100], "status": j["status"],
                    "body": j["body"], "ad_path": j["ad_path"], "redo_of": j["redo_of"],
                    "meta_status": meta.get("status"), "meta_ad_id": meta.get("ad_id")})
    return {"ads": ads}


@mcp.tool()
def upload_ads(job_ids: list[str] | None = None, batch_id: str | None = None, chat_id: str | None = None,
               dry_run: bool = False, start_time: str | None = None) -> dict:
    """Upload finished ads to Meta: a new ACTIVE ad set (own budget, the language's states) and ad for EACH ad,
    in the language's campaign. Spends money, so call it only when the owner asks to upload.

    Give job_ids (from list_ads / get_batch_status) OR a batch_id to upload every finished ad of that batch.
    Returns at once with an upload_id; the result (ad set and ad ids, or the reason it failed) arrives in the chat.
    Ads already on Meta are skipped, and asking again for failed ones continues where they stopped. The whole
    request is refused up front if an ad is unfinished or a .env value is missing.
    The ad sets start at the next 6:00 IST. Pass start_time (ISO 8601, e.g. "2026-11-03T09:00", read as IST when no
    offset is given) ONLY when the owner names a start time.
    dry_run=True only shows the exact requests that would be sent, nothing is sent to Meta.
    """
    try:
        return uploader().submit(job_ids=job_ids, batch_id=batch_id, chat_id=chat_id, dry_run=dry_run,
                                start_time=start_time)
    except ValidationError as exc:
        return {"started": False, "error": str(exc)}


@mcp.tool()
def get_upload_status(upload_id: str) -> dict:
    """Progress of an upload_ads request: per-ad status, stage, Meta video/ad set/ad ids and errors."""
    try:
        return uploader().status(upload_id)
    except KeyError:
        return {"error": f"Unknown upload {upload_id}"}


@mcp.tool()
def get_ad_details(job_id: str) -> dict:
    """What a finished ad is made of and what is stored for it: text, body, clip length, the words actually spoken
    with the time the speech ends, whether the voice may be cut off, and when/why the poster appears.
    Read this before proposing a redo."""
    try:
        return redo().details(job_id)
    except ValidationError as exc:
        return {"error": str(exc)}


@mcp.tool()
def plan_redo(job_id: str, changes: list[str], new_text: str | None = None, new_body: str | None = None,
              duration: int | None = None, poster_start: float | None = None, poster_end: float | None = None,
              poster_feedback: str | None = None) -> dict:
    """Plan a redo of part of a finished ad. NOTHING runs and nothing is spent: it returns what will be redone,
    what will be reused from the old ad, and the costs. Show that to the owner and call approve_redo only after
    they agree.

    changes (one or more): "image" (new reporter image; this also needs a new Veo clip and everything after),
    "video" (same image, new Veo clip: new voice and picture), "duration" (new clip length 4/6/8 s, needs
    `duration`; new Veo clip), "poster_timing" (same clip, different moment for the poster; reuses image, Veo clip
    and Whisper), "body" (different body video, needs `new_body`; only re-joins), "text" (new hook text, needs
    `new_text`; redoes everything). Give the smallest set that fixes what the owner said is wrong.
    Poster timing: pass poster_start/poster_end (seconds in the hook) for an exact window, or poster_feedback
    (the owner's words, e.g. "show it when she says the app name") to let the planner choose again.
    """
    try:
        return redo().plan(job_id, changes, new_text, new_body, duration, poster_start, poster_end, poster_feedback)
    except ValidationError as exc:
        return {"error": str(exc)}


@mcp.tool()
def approve_redo(plan_id: str) -> dict:
    """Run a redo plan from plan_redo. Call this ONLY after the owner has approved that plan. Makes a new ad
    (the old one is kept) and sends it to the chat when finished."""
    try:
        return redo().approve(plan_id)
    except ValidationError as exc:
        return {"error": str(exc), "started": False}


@mcp.tool()
def list_languages() -> dict:
    """Supported language codes with names, and the available body videos per language."""
    bodies = {code: sorted(p.stem for p in (Path(S.paths.body_dir) / code).glob("*.mp4")) for code in S.languages}
    return {"languages": S.to_dict()["languages"], "default_body": S.default_body, "bodies": bodies}


# ---------------- reusable building blocks ----------------

@mcp.tool()
async def generate_image(text: str) -> dict:
    """Generate the 9:16 news-reporter thumbnail image (gpt-image-2, high quality) with `text` on it."""
    from .imagegen import generate_image as gen
    out = await _blocking(gen, text, _adhoc("image.png"), S)
    return {"image_path": str(out)}


@mcp.tool()
async def generate_video(image_path: str, text: str, language: str, duration: int | None = None) -> dict:
    """Turn the reporter image into a 9:16 720p Veo video where she speaks `text` in `language` (code).

    duration (4, 6 or 8 seconds) is chosen from the text length and language when omitted.
    """
    from .durations import pick_durations
    from .videogen import generate_video as gen
    if duration is None:
        pick = (await _blocking(pick_durations, [{"language": language, "text": text}], S))[0]
        duration = pick["clip_seconds"]
    out = await _blocking(gen, text, language, _adhoc("video.mp4"), duration, image_path, S)
    return {"video_path": str(out), "seconds": media.probe(out, S).duration}


@mcp.tool()
async def transcribe_words(media_path: str, language: str) -> dict:
    """Word-level timestamps of the speech in a video/audio file (faster-whisper medium; audio is extracted internally)."""
    from .transcribe import transcribe_words as tw
    words = await _blocking(tw, media_path, language, S)
    return {"words": words}


@mcp.tool()
async def plan_poster_window(text: str, language: str, words: list[dict], clip_seconds: float) -> dict:
    """Choose when the poster demo is shown: returns start/end seconds plus the spoken phrase it covers."""
    from .planner import plan_poster_window as plan
    res = await _blocking(plan, text, language, words, clip_seconds, S)
    return {"start": res.window.start, "end": res.window.end, "phrase": res.phrase, "method": res.method,
            "start_idx": res.start_idx, "end_idx": res.end_idx, "notes": res.notes}


@mcp.tool()
async def render_hook(video_path: str, language: str, start: float, end: float, out_path: str | None = None) -> dict:
    """Overlay the language's poster demo full-frame on the hook video between start and end seconds
    (poster is muted, the voice-over keeps playing). Saves to the Hooks folder unless out_path is given."""
    poster = Path(S.paths.poster_dir) / language / "poster_demo.mp4"
    out = Path(out_path) if out_path else Path(S.paths.hooks_dir) / language / f"hook_{_stamp()}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    from .locks import CPU_LOCK

    def run():
        with CPU_LOCK:
            return media.render_hook(video_path, poster, media.PosterWindow(start, end), out, S)

    await _blocking(run)
    return {"hook_path": str(out)}


@mcp.tool()
async def merge_ad(hook_path: str, language: str, body: str | None = None, out_path: str | None = None) -> dict:
    """Join a hook video and the language's body video with a cross-fade. Saves to the Ads folder unless out_path is given."""
    body_file = pipeline()._find_body(language, body)
    out = Path(out_path) if out_path else Path(S.paths.ads_dir) / language / f"ad_{_stamp()}_{body_file.stem}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    from .locks import CPU_LOCK

    def run():
        with CPU_LOCK:
            return media.merge_ad(hook_path, body_file, out, S)

    await _blocking(run)
    return {"ad_path": str(out)}


def main() -> None:
    pipeline().start_watchdog()      # resumes jobs left by a previous server and reports stuck ones
    mcp.run()


if __name__ == "__main__":
    main()
