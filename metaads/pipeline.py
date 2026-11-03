"""Batch orchestration: one complete ad per input item, resumable, rate-limited, delivered as each finishes."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import media
from .bodies import body_folder
from .config import Settings, load_settings
from .jobs import FORMAT_IMAGE, FORMAT_VIDEO, REVIEW_PENDING, JobStore
from .locks import CPU_LOCK
from .imagegen import IMAGE_AD, IMAGE_REPORTER
from .media import PosterWindow
from .messages import FAIL, IMAGE, OK, WAIT, WARN, blocks, entry, headline, lines
from .notify import Notifier, NullNotifier
from .planner import PlanResult, plan_poster_window
from .transcribe import NoSpeechRecognizedError
from .durations import DurationEstimateError, pick_durations

log = logging.getLogger("metaads.pipeline")

# Ends the batch summary when ads wait for the owner; Hermes never sees the summary, so this is what asks.
REVIEW_PROMPT = lines(headline(WAIT, "Waiting for your decision."), "Reply to an ad (or give its id) with:",
                      "• approve (uploads it to Meta)", "• drop", "• or what to change", "'approve all' works.")


# Added to an ad's caption when the poster window is the planner's last-resort guess (no words, or none usable).
POSTER_GUESS_NOTE = "The poster timing is a guess (the speech could not be matched to the words). Ask to move it if it is off."


def _failed_without_speech(job: dict[str, Any]) -> bool:
    """True when the job's previous attempt ended because Whisper recognized nothing in the clip."""
    return (job.get("error") or "").startswith(NoSpeechRecognizedError.__name__)


class ValidationError(ValueError):
    pass


@dataclass
class Services:
    """The remote/slow steps, injectable so the pipeline can be tested without API keys."""
    image: Callable[[str, Path, str], Path]                     # thumbnail text, out, kind (settings.images key)
    video: Callable[[str, str, Path, int | None, Path], Path]   # spoken text, language, out, duration, image
    transcribe: Callable[[Path, str], list[dict]]
    plan: Callable[..., PlanResult]                             # spoken text, language, words, clip seconds, guidance=None
    durations: Callable[[list[dict]], list[dict]] = lambda items: pick_durations(items)


def default_services(settings: Settings | None = None) -> Services:
    from .imagegen import generate_image
    from .transcribe import transcribe_words
    from .videogen import generate_video

    s = settings or load_settings()
    return Services(
        image=lambda thumbnail_text, out, kind: generate_image(thumbnail_text, out, s, kind=kind),
        video=lambda spoken_text, lang, out, dur, image: generate_video(spoken_text, lang, out, dur, image, settings=s),
        transcribe=lambda path, lang: transcribe_words(path, lang, s),
        plan=lambda spoken_text, lang, words, dur, hint=None: plan_poster_window(spoken_text, lang, words, dur, s,
                                                                                 guidance=hint),
        durations=lambda items: pick_durations(items, s),
    )


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy via a temp name in the destination folder (a network mount can drop mid-copy)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, part)
    os.replace(part, dst)


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _owner_alive(owner: str | None) -> bool:
    """Owner tokens look like '<pid>@<boot id>:<random>'. True if that process still runs on this machine."""
    if not owner:
        return False
    try:
        who = owner.split(":")[0]
        pid, _, boot = who.partition("@")
        if boot != _boot_id():                       # written before a reboot
            return False
        os.kill(int(pid), 0)
        state = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"                          # a zombie is dead
    except (ValueError, OSError, IndexError):
        return False


class Pipeline:
    def __init__(self, settings: Settings | None = None, store: JobStore | None = None,
                 services: Services | None = None, notifier: Notifier | None = None):
        self.s = settings or load_settings()
        self.store = store or JobStore(self.s.paths.db)
        self.services = services or default_services(self.s)
        self.notifier = notifier or NullNotifier()
        self.owner = f"{os.getpid()}@{_boot_id()}:{uuid.uuid4().hex[:6]}"   # who runs the jobs this instance starts
        self._active: set[str] = set()                 # jobs with a live thread in this process
        self._active_lock = threading.Lock()
        self._stuck_reported: set[tuple[str, float]] = set()

    # ---------- validation ----------
    def _find_body(self, language: str, wanted: str | None) -> Path:
        folder = body_folder(self.s, language)
        name = wanted or self.s.default_body
        exact = folder / (name if name.endswith(".mp4") else name + ".mp4")
        if exact.exists():
            return exact
        matches = sorted(p for p in folder.glob("*.mp4") if name.lower() in p.stem.lower())
        if len(matches) == 1:
            return matches[0]
        options = ", ".join(p.stem for p in sorted(folder.glob("*.mp4")))
        raise ValidationError(f"Body '{name}' for {language}: "
                              f"{'ambiguous' if matches else 'not found'}. Available: {options}")

    def validate_item(self, item: dict[str, Any]) -> dict[str, Any]:
        lang = (item.get("language") or "").strip().lower()
        spoken_text = (item.get("spoken_text") or "").strip()
        thumbnail_text = (item.get("thumbnail_text") or "").strip() or spoken_text    # same words unless told otherwise
        ad_format = item.get("ad_format") or FORMAT_VIDEO
        if lang not in self.s.languages:
            raise ValidationError(f"Unsupported language '{lang}'. Supported: {', '.join(self.s.languages)}")
        if ad_format not in (FORMAT_VIDEO, FORMAT_IMAGE):
            raise ValidationError(f"ad_format must be '{FORMAT_VIDEO}' or '{FORMAT_IMAGE}', got '{ad_format}'")
        if not spoken_text:
            raise ValidationError("Empty spoken_text (the dialogue; the hook text goes in spoken_text, and "
                                  "thumbnail_text is optional)")
        if ad_format == FORMAT_IMAGE:                  # a single image: no poster, no body video, no clip length
            wanted = item.get("body")
            body = self._find_body(lang, wanted).stem if wanted else None
            return {"language": lang, "spoken_text": spoken_text, "thumbnail_text": thumbnail_text, "body": body,
                    "duration": None, "ad_format": FORMAT_IMAGE}
        poster = Path(self.s.paths.poster_dir) / lang / "poster_demo.mp4"
        if not poster.exists():
            raise ValidationError(f"Poster video missing: {poster}")
        body = self._find_body(lang, item.get("body"))
        duration = item.get("duration")
        if duration is not None and duration not in self.s.hook.durations:
            raise ValidationError(f"Duration must be one of {self.s.hook.durations}")
        return {"language": lang, "spoken_text": spoken_text, "thumbnail_text": thumbnail_text, "body": body.stem,
                "duration": duration, "ad_format": FORMAT_VIDEO}

    # ---------- submit / resume ----------
    def submit(self, items: list[dict[str, Any]], chat_id: str | None = None, background: bool = True,
               review: bool = True) -> str:
        """Validate everything first (all-or-nothing), then run one ad per item. Returns the batch id.
        With `review` every finished ad waits for the owner's decision before it may go to Meta."""
        if not items:
            raise ValidationError("No ads requested")
        if len(items) > self.s.limits.max_batch_items:
            raise ValidationError(f"At most {self.s.limits.max_batch_items} ads per request, got {len(items)}")
        errors, clean = [], []
        for n, it in enumerate(items, 1):
            try:
                clean.append(self.validate_item(it))
            except ValidationError as exc:
                errors.append(f"Ad {n}: {exc}")
        if errors:
            raise ValidationError("; ".join(errors))
        # Clip lengths for every text that does not name one, from one Gemini call per text (retried on failure).
        pending = [i for i, it in enumerate(clean) if it["ad_format"] == FORMAT_VIDEO and it["duration"] is None]
        if pending:
            try:
                picks = self.services.durations(
                    [{"language": clean[i]["language"], "spoken_text": clean[i]["spoken_text"]} for i in pending])
            except DurationEstimateError as exc:
                raise ValidationError(str(exc)) from exc
            for i, pick in zip(pending, picks):                  # a text longer than the longest clip gets that clip
                clean[i]["duration"] = pick["clip_seconds"]
                clean[i]["estimate"] = json.dumps(pick)
        used, videos = self.store.video_calls_today(), sum(it["ad_format"] == FORMAT_VIDEO for it in clean)
        if used + videos > self.s.limits.daily_video_cap:
            raise ValidationError(f"Daily video cap {self.s.limits.daily_video_cap}: {used} used today, "
                                  f"{videos} requested")
        if review:
            for item in clean:
                item["review"] = REVIEW_PENDING
        batch_id, job_ids = self.store.create_batch(clean, chat_id, self.owner)
        self._start(batch_id, job_ids, background)
        return batch_id

    def resume(self, batch_id: str, background: bool = True) -> list[str]:
        todo = [j["id"] for j in self.store.batch_jobs(batch_id) if j["status"] != "done"]
        if todo:
            self._start(batch_id, todo, background)
        return todo

    def _start(self, batch_id: str, job_ids: list[str], background: bool) -> None:
        if background:
            threading.Thread(target=self._run_batch, args=(batch_id, job_ids), daemon=True,
                             name=f"metaads-{batch_id}").start()
        else:
            self._run_batch(batch_id, job_ids)

    def _run_batch(self, batch_id: str, job_ids: list[str]) -> None:
        chat_id = self.store.batch_chat_id(batch_id)
        with ThreadPoolExecutor(max_workers=self.s.limits.api_concurrency) as pool:
            list(pool.map(lambda jid: self._run_guarded(jid, chat_id), job_ids))
        jobs = self.store.batch_jobs(batch_id)
        ok = [j for j in jobs if j["status"] == "done"]
        failed = [j for j in jobs if j["status"] == "failed"]
        images = sum(j["ad_format"] == FORMAT_IMAGE for j in ok)
        fallbacks = sum(bool(j["fallback_reason"]) for j in ok)
        image_note = (f"{images} of them are image ads" + (f" ({fallbacks} because the hook video failed)." if fallbacks else ".")
                      if images else None)
        waiting = any(j["status"] == "done" and j["review"] == REVIEW_PENDING for j in jobs)
        self._safe(self.notifier.send_report, chat_id, blocks(
            headline(WARN if failed else OK, f"{len(ok)} of {len(jobs)} ads created"), image_note,
            *(entry(f"Ad {j['idx'] + 1} ({self.s.languages[j['language']]}) failed", j["error"]) for j in failed),
            REVIEW_PROMPT if waiting else None))

    def _run_guarded(self, job_id: str, chat_id: str | None) -> None:
        with self._active_lock:
            if job_id in self._active:                # already running here (e.g. a manual resume of a live job)
                return
            self._active.add(job_id)
        try:
            self.store.update_job(job_id, owner=self.owner)
            self._run_with_retry(job_id, chat_id)
        finally:
            with self._active_lock:
                self._active.discard(job_id)

    # ---------- recovery and watchdog ----------
    def recover(self) -> list[str]:
        """Take over unfinished jobs whose server process is gone (restart, crash, reboot) and run them again.

        Finished stages are cached on disk, so only the missing work is redone. The claim is atomic, so two
        servers never run the same job. Returns the ids of the jobs taken over.
        """
        taken: dict[str, list[dict]] = {}
        for job in self.store.unfinished_jobs():
            if _owner_alive(job["owner"]) or job["id"] in self._active:
                continue
            if self.store.claim_job(job["id"], job["owner"], self.owner):
                taken.setdefault(job["batch_id"], []).append(job)
        for batch_id, jobs in taken.items():
            chat_id = self.store.batch_chat_id(batch_id)
            names = ", ".join(f"Ad {j['idx'] + 1} ({self.s.languages[j['language']]})" for j in jobs)
            self._safe(self.notifier.send_report, chat_id, blocks(
                headline(WARN, f"The server restarted while {len(jobs)} ad(s) were unfinished"), f"Resuming: {names}."))
            self._start(batch_id, [j["id"] for j in jobs], True)
        return [j["id"] for jobs in taken.values() for j in jobs]

    def check_stuck(self, now: float | None = None) -> list[str]:
        """Report (once per stage) each running job that has not reached a new stage for watchdog.stuck_minutes."""
        now = time.time() if now is None else now
        limit = self.s.watchdog.stuck_minutes * 60
        reported = []
        for job in self.store.unfinished_jobs():
            idle = now - job["updated"]
            key = (job["id"], job["updated"])
            if job["status"] != "running" or idle < limit or key in self._stuck_reported:
                continue
            self._stuck_reported.add(key)
            reported.append(job["id"])
            self._safe(self.notifier.send_report, self.store.batch_chat_id(job["batch_id"]), blocks(
                headline(WARN, f"Ad {job['idx'] + 1} ({self.s.languages[job['language']]}) may be stuck"),
                f"It has been at stage '{job['stage']}' for {idle / 60:.0f} min with no progress."))
        return reported

    def start_watchdog(self) -> threading.Thread:
        """Background thread: recover jobs left behind by a dead server, then report stuck jobs, every check_seconds."""
        def loop():
            while True:
                try:
                    self.recover()
                    self.check_stuck()
                except Exception:
                    log.exception("watchdog tick failed")
                time.sleep(self.s.watchdog.check_seconds)

        thread = threading.Thread(target=loop, daemon=True, name="metaads-watchdog")
        thread.start()
        return thread

    def _run_with_retry(self, job_id: str, chat_id: str | None) -> None:
        attempts = self.s.limits.job_attempts
        for attempt in range(1, attempts + 1):       # automatic retries; stages already done are not repeated
            try:
                self.store.update_job(job_id, status="running", attempts=attempt)
                self.run_job(job_id, chat_id)
                return
            except Exception as exc:
                log.exception("job %s attempt %s failed", job_id, attempt)
                err = f"{type(exc).__name__}: {exc}"
                final = attempt == attempts or not getattr(exc, "retryable", True)   # policy blocks cannot succeed on retry
                self.store.update_job(job_id, status="failed" if final else "running", error=err)
                if final:
                    break
        job = self.store.get_job(job_id)
        if job["stage"] == "video" and job["ad_format"] == FORMAT_VIDEO:        # the hook video could not be made
            reason = job["error"]
            try:
                self.make_image_ad(job_id, chat_id, fallback_reason=reason)
                return
            except Exception as exc:
                log.exception("job %s image-ad fallback failed", job_id)
                self.store.update_job(job_id, status="failed",
                                      error=f"{reason}; the image-ad fallback also failed: {type(exc).__name__}: {exc}")
                job = self.store.get_job(job_id)
        self._safe(self.notifier.send_report, chat_id, blocks(
            headline(FAIL, f"Ad {job['idx'] + 1} ({self.s.languages[job['language']]}) failed at stage '{job['stage']}'"),
            job["error"]))

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("notification failed")

    # ---------- one ad ----------
    def run_job(self, job_id: str, chat_id: str | None = None) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["ad_format"] == FORMAT_IMAGE:
            return self.make_image_ad(job_id, chat_id)
        lang, spoken_text, body_stem, duration = job["language"], job["spoken_text"], job["body"], job["duration"]
        work = Path(self.s.paths.work_dir) / job_id
        work.mkdir(parents=True, exist_ok=True)
        stage = lambda name: self.store.update_job(job_id, stage=name)

        image = work / "image.png"
        if not image.exists():
            stage("image")
            self.services.image(job["thumbnail_text"], image, IMAGE_REPORTER)

        clip = work / "video.mp4"
        if not clip.exists():
            stage("video")
            if self.store.video_calls_today() >= self.s.limits.daily_video_cap:
                raise RuntimeError(f"Daily video cap of {self.s.limits.daily_video_cap} reached")
            self.store.record_video_call()
            tmp = work / "video.part.mp4"
            self.services.video(spoken_text, lang, tmp, duration, image)
            os.replace(tmp, clip)
        clip_duration = media.probe(clip, self.s).duration

        words_file = work / "words.json"
        if not words_file.exists():
            stage("transcribe")
            last_attempt = job["attempts"] >= self.s.limits.job_attempts
            # On the last attempt after a clip with no recognizable speech, Whisper is not asked again: the poster is
            # placed without words (an empty words.json records that, so a redo does not transcribe it again either).
            words = [] if last_attempt and _failed_without_speech(job) else self.services.transcribe(clip, lang)
            if not words and not last_attempt:
                raise NoSpeechRecognizedError(f"Whisper recognized no words in the {lang} clip, even without its "
                                              f"confidence filters")
            words_file.write_text(json.dumps(words, ensure_ascii=False))
        words = json.loads(words_file.read_text())

        plan_file = work / "plan.json"
        if not plan_file.exists():
            stage("plan")
            result = self.services.plan(spoken_text, lang, words, clip_duration, job.get("plan_hint"))
            plan_file.write_text(json.dumps(asdict(result), ensure_ascii=False))
        plan = json.loads(plan_file.read_text())
        window = PosterWindow(plan["window"]["start"], plan["window"]["end"])

        poster = Path(self.s.paths.poster_dir) / lang / "poster_demo.mp4"
        hook = work / "hook.mp4"
        if not hook.exists():
            stage("render_hook")
            with CPU_LOCK:
                media.render_hook(clip, poster, window, work / "hook.part.mp4", self.s)
            os.replace(work / "hook.part.mp4", hook)

        body = self._find_body(lang, body_stem)
        ad = work / "ad.mp4"
        if not ad.exists():
            stage("merge")
            with CPU_LOCK:
                media.merge_ad(hook, body, work / "ad.part.mp4", self.s)
            os.replace(work / "ad.part.mp4", ad)

        stage("publish")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        hook_dst = Path(self.s.paths.hooks_dir) / lang / f"hook_{stamp}_{job_id}.mp4"
        ad_dst = Path(self.s.paths.ads_dir) / lang / f"ad_{stamp}_{job_id}_{body.stem}.mp4"
        meta = {
            "job_id": job_id, "redo_of": job.get("redo_of"), "created": datetime.now().isoformat(timespec="seconds"), "language": lang,
            "ad_format": FORMAT_VIDEO, "spoken_text": spoken_text, "thumbnail_text": job["thumbnail_text"],
            "body": body.stem, "video_duration": duration, "clip_seconds": clip_duration,
            "models": {"image": self.s.models.image, "quality": self.s.models.image_quality,
                       "video": self.s.models.video, "whisper": self.s.models.whisper, "llm": self.s.models.llm},
            "duration_estimate": json.loads(job["estimate"]) if job.get("estimate") else None,
            "measured_speech_end": words[-1]["end"] if words else None,
            "speech_may_be_cut_off": bool(words) and words[-1]["end"] >= clip_duration - 0.25,
            "poster_window": plan["window"], "poster_phrase": plan["phrase"], "plan_method": plan["method"],
            "plan_notes": plan["notes"], "words": words, "hook_file": str(hook_dst), "ad_file": str(ad_dst),
        }
        for src, dst in ((hook, hook_dst), (ad, ad_dst)):
            _atomic_copy(src, dst)
            dst.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        self.store.update_job(job_id, status="done", stage="done", error=None,
                              hook_path=str(hook_dst), ad_path=str(ad_dst))
        warn = (headline(WARN, "The voice may be cut off at the end of the hook (the clip was probably too short).")
                if meta["speech_may_be_cut_off"] else None)
        guess = headline(WARN, POSTER_GUESS_NOTE) if plan["method"] == "default" else None
        self._safe(self.notifier.send_video, chat_id, ad_dst, self._caption(job, warn, guess))
        return meta

    def _caption(self, job: dict[str, Any], *notes: str | None, label: str = "") -> str:
        """Telegram caption of a finished ad, one fact per line and always ending with its id (the owner replies with
        it); the thumbnail words are shown only when they differ from the dialogue."""
        total = len(self.store.batch_jobs(job["batch_id"]))
        thumb = f"Thumbnail: {job['thumbnail_text'][:self.s.messages.thumbnail_chars]}" if job["thumbnail_text"] != job["spoken_text"] else None
        return lines(f"{label}{'Redo · ' if job.get('redo_of') else ''}Ad {job['idx'] + 1}/{total} · "
                     f"{self.s.languages[job['language']]}", f"Says: {job['spoken_text'][:self.s.messages.spoken_chars]}", thumb, *notes,
                     f"id {job['id']}")

    def make_image_ad(self, job_id: str, chat_id: str | None = None, fallback_reason: str | None = None) -> dict[str, Any]:
        """The ad is one 4:5 image with the thumbnail text: asked for (ad_format image), or made because the hook
        video failed (`fallback_reason` is then that failure). No poster, body or audio."""
        job = self.store.get_job(job_id)
        lang = job["language"]
        work = Path(self.s.paths.work_dir) / job_id
        work.mkdir(parents=True, exist_ok=True)
        image = work / "image_ad.png"
        if not image.exists():
            self.store.update_job(job_id, stage="image_ad")
            tmp = work / "image_ad.part.png"
            self.services.image(job["thumbnail_text"], tmp, IMAGE_AD)
            os.replace(tmp, image)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ad_dst = Path(self.s.paths.ads_dir) / lang / f"ad_{stamp}_{job_id}_image.png"
        meta = {"job_id": job_id, "redo_of": job.get("redo_of"), "created": datetime.now().isoformat(timespec="seconds"),
                "language": lang, "ad_format": FORMAT_IMAGE, "spoken_text": job["spoken_text"],
                "thumbnail_text": job["thumbnail_text"], "fallback_reason": fallback_reason,
                "models": {"image": self.s.models.image, "quality": self.s.models.image_quality},
                "image_ad": dict(self.s.images[IMAGE_AD]), "ad_file": str(ad_dst)}
        _atomic_copy(image, ad_dst)
        ad_dst.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        self.store.update_job(job_id, status="done", stage="done", error=None, ad_path=str(ad_dst), hook_path=None,
                              ad_format=FORMAT_IMAGE, fallback_reason=fallback_reason)
        why = headline(WARN, f"The hook video failed: {fallback_reason[:self.s.messages.reason_chars]}") if fallback_reason else None
        self._safe(self.notifier.send_photo, chat_id, ad_dst, self._caption(job, why, label=f"{IMAGE} Image ad · "))
        return meta

    # ---------- status ----------
    def status(self, batch_id: str) -> dict[str, Any]:
        jobs = self.store.batch_jobs(batch_id)
        counts = {k: sum(j["status"] == k for j in jobs) for k in ("queued", "running", "done", "failed")}
        return {"batch_id": batch_id, "total": len(jobs), **counts,
                "finished": counts["queued"] + counts["running"] == 0,
                "ads": [{"n": j["idx"] + 1, "job_id": j["id"], "language": j["language"], "status": j["status"], "stage": j["stage"],
                         "ad_format": j["ad_format"], "fallback_reason": j["fallback_reason"],
                         "ad_path": j["ad_path"], "hook_path": j["hook_path"], "error": j["error"]} for j in jobs]}
