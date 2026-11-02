"""MCP server (stdio) exposing the Meta ads tools to Hermes or any MCP client.

Run: uv run python -m metaads.server
Logs go to stderr; stdout is the protocol channel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import media
from .adaccount import make_client, managed_account
from .adplan import AdPlanError, AdPlans
from .bodies import available_bodies
from .config import load_env_file, load_settings
from .jobs import REVIEW_DROPPED
from .notify import TelegramNotifier
from .pipeline import Pipeline, ValidationError
from .planning import Planner
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


def _planner() -> Planner:
    """Raises RuntimeError (a missing .env value)."""
    account_id, campaigns = managed_account(S, os.environ)
    return Planner(S, make_client(S), account_id, campaigns, pipeline().store)


def _new_ads_plan() -> dict:
    """The budget plan for new ads (raises RuntimeError: PlanningError, GraphError, a missing .env value)."""
    return _planner().run()


def ad_plans() -> AdPlans:
    return AdPlans(S, pipeline().store, _new_ads_plan, pipeline(), redo(),
                   heartbeat=os.environ.get("HEALTHCHECK_URL_DAILY_PLAN", "").strip())


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
def create_ads(items: list[dict[str, Any]], chat_id: str | None = None, review: bool = True) -> dict:
    """Create one complete ad for EACH item, in the background: a video ad (hook + body, cross-faded) or an image ad.

    items: list of {language, spoken_text, thumbnail_text?, ad_format?, body?, duration?}.
      language: code from list_languages (hi, bn, gu, kn, mr, ta, te).
      spoken_text: the dialogue the reporter speaks (as the user wrote it). It is also the Meta ad text.
      thumbnail_text: optional words written on the thumbnail image; when omitted the image shows the spoken_text.
      ad_format: "video" (default) or "image" (one 4:5 image with the thumbnail text, no video; cheaper). A video ad
        whose hook video fails is turned into an image ad automatically.
      body: optional body video name or unique part of it (e.g. "teal"); default navy-darkyellow-yellowbox.
      duration: optional clip length 4, 6 or 8 seconds; by default chosen from the spoken text length and language.
    review: True (default) = the finished ads wait for the owner to approve, drop or give feedback before anything
      goes to Meta (the summary on Telegram asks them to; follow the ad-review skill). False only when the owner
      says to just make the ads and not upload them.
    Returns immediately with a batch_id. Each finished ad is sent to Telegram on its own, and a summary
    follows at the end. Use get_batch_status to check progress. The whole request is refused up front
    (nothing runs) if any item is invalid or a limit would be exceeded.
    """
    try:
        batch_id = pipeline().submit(items, chat_id=chat_id, review=review)
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
def list_ads(limit: int | None = None, awaiting_review: bool = False) -> dict:
    """Recent ads, newest first: job_id, language, spoken_text, thumbnail_text, ad_format (video or image),
    status, file paths, the owner's decision (`review`:
    pending, approved, dropped, redone or none) and whether the ad is on Meta (meta_status, meta_ad_id).
    Use it to find which ad the owner means. awaiting_review=True lists only the ads still waiting for the owner's
    decision (review pending, including ones still being made), oldest first; `limit` is ignored then."""
    store = pipeline().store
    ads = []
    jobs = store.awaiting_review_jobs() if awaiting_review else store.recent_jobs(max(1, min(limit or S.limits.list_ads_default, S.limits.list_ads_max)))
    for j in jobs:
        meta = store.get_meta_upload(j["id"]) or {}
        ads.append({"job_id": j["id"], "language": j["language"], "spoken_text": j["spoken_text"][:S.messages.tool_text_chars],
                    "thumbnail_text": j["thumbnail_text"][:S.messages.tool_text_chars], "ad_format": j["ad_format"],
                    "fallback_reason": j["fallback_reason"], "status": j["status"],
                    "body": j["body"], "ad_path": j["ad_path"], "redo_of": j["redo_of"], "review": j["review"],
                    "meta_status": meta.get("status"), "meta_ad_id": meta.get("ad_id")})
    return {"ads": ads}


@mcp.tool()
def upload_ads(job_ids: list[str] | None = None, batch_id: str | None = None, chat_id: str | None = None,
               dry_run: bool = False, start_time: str | None = None) -> dict:
    """Upload finished ads to Meta: a new ACTIVE ad set (own budget, the language's states) and ad for EACH ad,
    in the language's campaign. Spends money, so call it only when the owner approved these ads (or asked to upload them).

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
def drop_ads(job_ids: list[str]) -> dict:
    """Drop ads the owner explicitly said to drop (delete, discard): they will not be uploaded or asked about again.
    Never use it for an ad the owner only criticised without saying drop. Spends nothing and deletes no files."""
    store = pipeline().store
    ids = list(dict.fromkeys(job_ids))
    waiting = {j["id"] for j in store.awaiting_review_jobs()}
    unknown = [j for j in ids if j not in waiting]
    if unknown:
        return {"dropped": [], "error": f"Not waiting for a decision: {', '.join(unknown)}. Check list_ads."}
    store.set_review(ids, REVIEW_DROPPED)
    return {"dropped": ids}


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
def plan_redo(job_id: str, changes: list[str], new_spoken_text: str | None = None,
              new_thumbnail_text: str | None = None, new_body: str | None = None, duration: int | None = None,
              poster_start: float | None = None, poster_end: float | None = None,
              poster_feedback: str | None = None) -> dict:
    """Plan a redo of part of a finished ad. NOTHING runs and nothing is spent: it returns what will be redone,
    what will be reused from the old ad, and the costs. Show that to the owner and call approve_redo only after
    they agree.

    changes (one or more): "image" (new reporter image; this also needs a new Veo clip and everything after),
    "video" (same image, new Veo clip: new voice and picture), "duration" (new clip length 4/6/8 s, needs
    `duration`; new Veo clip), "poster_timing" (same clip, different moment for the poster; reuses image, Veo clip
    and Whisper), "body" (different body video, needs `new_body`; only re-joins), "thumbnail_text" (new words on the
    image, needs `new_thumbnail_text`; a new image, so redoes everything), "spoken_text" (new dialogue, needs
    `new_spoken_text`; keeps the image, makes a new Veo clip). Give the smallest set that fixes what the owner said is
    wrong. An image ad (see list_ads ad_format) can only redo "thumbnail_text", "image" and "spoken_text" and stays an
    image ad; any other change makes it a video ad. A video ad whose video failed (an image ad with a fallback_reason)
    is redone as a video ad: "video" tries the Veo clip again.
    Poster timing: pass poster_start/poster_end (seconds in the hook) for an exact window, or poster_feedback
    (the owner's words, e.g. "show it when she says the app name") to let the planner choose again.
    """
    try:
        return redo().plan(job_id, changes, new_spoken_text, new_thumbnail_text, new_body, duration, poster_start,
                           poster_end, poster_feedback)
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
def plan_new_ads() -> dict:
    """How many NEW ads to create now, from what is left of this month's budget (read-only, creates nothing).

    Use it when the owner asks how many new ads to make today, or how the monthly budget stands. It looks at the
    running ad sets (how much they will still spend before the pause rule stops them, which are winning and will be
    raised) and returns, per language campaign: `proven_hook_ads` (a winning hook, by `hook_job_id`, to put on the
    listed unused `bodies`) and `new_hook_ads` (a count of brand-new hooks on the default body), split into
    `new_hook_video_ads` and `new_hook_image_ads` by which format has been winning (`format_stats`,
    `format_reason`). Also returns the budget figures, `at_risk_adsets`, `scaling_winners` and `notes`. Report these
    numbers; do not make up others.
    """
    try:
        return _new_ads_plan()
    except RuntimeError as e:                    # PlanningError, GraphError and missing .env values
        return {"error": str(e)}


@mcp.tool()
def get_hook_performance(language: str | None = None) -> dict:
    """Which hooks won and which kept failing, to write better new hooks from evidence (read-only).

    language: optional code from list_languages; all languages when omitted. Returns `winners` (hooks whose ad set
    reached the win bar, lowest cost per result first) and `losers` (paused before winning, newest first), each with
    `spoken_text`, `thumbnail_text`, `ad_format`, `body`, `results`, `spend_inr`, `cpr_inr`, plus `format_stats`
    (decided ad sets, wins and win rate of video ads against image ads). Report these numbers; do not make up others.
    """
    if language is not None and language not in S.languages:
        return {"error": f"Unsupported language '{language}'. Supported: {', '.join(S.languages)}"}
    try:
        return _planner().hook_performance(language)
    except RuntimeError as e:                    # GraphError and missing .env values
        return {"error": str(e)}


# ---------------- the daily ad plan (see the ad-daily-plan skill) ----------------

@mcp.tool()
def get_hook_brief() -> dict:
    """What to know before writing new hooks (read-only): the product and style rules, the owner's sample winning hooks
    (`thumbnail_text` and `spoken_text` each), each language's script, the hooks already made per language (do not
    repeat them) and the rule for Hindi translations."""
    return ad_plans().hook_brief()


@mcp.tool()
def propose_ad_plan(new_hooks: list[dict[str, Any]], proven_glosses: list[dict[str, Any]] | None = None) -> dict:
    """Store today's ad plan and get the message to send the owner. Creates no ads, spends nothing.

    It runs the budget plan itself; write exactly as many new hooks per language and ad_format as `plan_new_ads`
    shows in `new_hook_video_ads` and `new_hook_image_ads`.
    new_hooks: [{language, ad_format ("video"|"image"), spoken_text, thumbnail_text?, spoken_hindi?, thumbnail_hindi?}]
      Texts are in the language's own script (English product words stay in Latin letters). thumbnail_text defaults to
      spoken_text. For every language except Hindi give spoken_hindi, and thumbnail_hindi when the thumbnail text
      differs from the spoken text: the owner's Hindi translation, for the message only.
    proven_glosses: [{hook_job_id, spoken_hindi, thumbnail_hindi?}] for each winning hook (`proven_hook_ads`) of a
      language other than Hindi.
    Returns {proposed: true, plan_id, message}: send `message` to the owner exactly as it is. If proposed is false and
    `errors` is given, fix those hooks and call again; `expected_new_hooks` shows the counts needed.
    """
    try:
        return ad_plans().propose(new_hooks, proven_glosses)
    except RuntimeError as e:                    # PlanningError, GraphError and missing .env values
        return {"proposed": False, "error": str(e)}


@mcp.tool()
def get_ad_plan() -> dict:
    """The ad plan waiting for the owner's decision, with the message to show (read-only). Call it first whenever the
    owner answers about a plan, also in a new chat session. `plan_id` is None when none is waiting."""
    return ad_plans().current()


@mcp.tool()
def revise_ad_plan(plan_id: str, replace: list[dict[str, Any]] | None = None, remove: list[int] | None = None) -> dict:
    """Change a waiting plan as the owner asked, then show them the returned `message` (the plan still waits).

    replace: [{n, spoken_text?, thumbnail_text?, spoken_hindi?, thumbnail_hindi?}] new texts for the new-hook item #n
      (the Hindi translations of changed texts are needed for languages other than Hindi). remove: item numbers to drop.
    Ads cannot be added (that would change the budget); winning-hook items can only be dropped."""
    try:
        return ad_plans().revise(plan_id, replace, remove)
    except (AdPlanError, RuntimeError) as e:
        return {"revised": False, "errors": [str(e)]}


@mcp.tool()
def approve_ad_plan(plan_id: str) -> dict:
    """Make the ads of the plan. Call this ONLY after the owner clearly approved this plan. It costs money (image and
    Veo calls) but uploads nothing: each finished ad then waits for the owner's own decision (ad-review skill)."""
    try:
        return ad_plans().approve(plan_id)
    except (AdPlanError, RuntimeError) as e:
        return {"approved": False, "error": str(e)}


@mcp.tool()
def reject_ad_plan(plan_id: str) -> dict:
    """The owner rejected the plan: nothing is made. Call it only when they clearly said so."""
    try:
        return ad_plans().reject(plan_id)
    except AdPlanError as e:
        return {"rejected": False, "error": str(e)}


@mcp.tool()
def list_languages() -> dict:
    """Supported language codes with names, and the available body videos per language."""
    bodies = {code: available_bodies(S, code) for code in S.languages}
    return {"languages": S.to_dict()["languages"], "default_body": S.default_body, "bodies": bodies}


# ---------------- reusable building blocks ----------------

@mcp.tool()
async def generate_image(thumbnail_text: str, image_ad: bool = False) -> dict:
    """Generate the news-reporter thumbnail image (gpt-image-2, high quality) with `thumbnail_text` on it: 9:16 (the
    image a hook video is made from), or 4:5 when image_ad is true (the image of an image ad)."""
    from .imagegen import IMAGE_AD, IMAGE_REPORTER, generate_image as gen
    out = await _blocking(gen, thumbnail_text, _adhoc("image.png"), S, None, IMAGE_AD if image_ad else IMAGE_REPORTER)
    return {"image_path": str(out)}


@mcp.tool()
async def generate_video(image_path: str, spoken_text: str, language: str, duration: int | None = None) -> dict:
    """Turn the reporter image into a 9:16 720p Veo video where she speaks `spoken_text` in `language` (code).

    duration (4, 6 or 8 seconds) is chosen from the text length and language when omitted.
    """
    from .durations import pick_durations
    from .videogen import generate_video as gen
    if duration is None:
        pick = (await _blocking(pick_durations, [{"language": language, "spoken_text": spoken_text}], S))[0]
        duration = pick["clip_seconds"]
    out = await _blocking(gen, spoken_text, language, _adhoc("video.mp4"), duration, image_path, S)
    return {"video_path": str(out), "seconds": media.probe(out, S).duration}


@mcp.tool()
async def transcribe_words(media_path: str, language: str) -> dict:
    """Word-level timestamps of the speech in a video/audio file (faster-whisper medium; audio is extracted internally)."""
    from .transcribe import transcribe_words as tw
    words = await _blocking(tw, media_path, language, S)
    return {"words": words}


@mcp.tool()
async def plan_poster_window(spoken_text: str, language: str, words: list[dict], clip_seconds: float) -> dict:
    """Choose when the poster demo is shown: returns start/end seconds plus the spoken phrase it covers."""
    from .planner import plan_poster_window as plan
    res = await _blocking(plan, spoken_text, language, words, clip_seconds, S)
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
