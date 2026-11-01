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
from .config import Settings, load_settings
from .jobs import JobStore
from .locks import CPU_LOCK
from .media import PosterWindow
from .notify import Notifier, NullNotifier
from .planner import PlanResult, plan_poster_window
from .durations import DurationEstimateError, pick_durations

log = logging.getLogger("metaads.pipeline")


class ValidationError(ValueError):
    pass


@dataclass
class Services:
    """The remote/slow steps, injectable so the pipeline can be tested without API keys."""
    image: Callable[[str, Path], Path]
    video: Callable[[str, str, Path, int | None, Path], Path]   # text, language, out, duration, image
    transcribe: Callable[[Path, str], list[dict]]
    plan: Callable[..., PlanResult]                             # text, language, words, clip seconds, guidance=None
    durations: Callable[[list[dict]], list[dict]] = lambda items: pick_durations(items)


def default_services(settings: Settings | None = None) -> Services:
    from .imagegen import generate_image
    from .transcribe import transcribe_words
    from .videogen import generate_video

    s = settings or load_settings()
    return Services(
        image=lambda text, out: generate_image(text, out, s),
        video=lambda text, lang, out, dur, image: generate_video(text, lang, out, dur, image, settings=s),
        transcribe=lambda path, lang: transcribe_words(path, lang, s),
        plan=lambda text, lang, words, dur, hint=None: plan_poster_window(text, lang, words, dur, s, guidance=hint),
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
        folder = Path(self.s.paths.body_dir) / language
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
        text = (item.get("text") or "").strip()
        if lang not in self.s.languages:
            raise ValidationError(f"Unsupported language '{lang}'. Supported: {', '.join(self.s.languages)}")
        if not text:
            raise ValidationError("Empty hook text")
        poster = Path(self.s.paths.poster_dir) / lang / "poster_demo.mp4"
        if not poster.exists():
            raise ValidationError(f"Poster video missing: {poster}")
        body = self._find_body(lang, item.get("body"))
        duration = item.get("duration")
        if duration is not None and duration not in self.s.hook.durations:
            raise ValidationError(f"Duration must be one of {self.s.hook.durations}")
        return {"language": lang, "text": text, "body": body.stem, "duration": duration}

    def _video_calls_today(self) -> int:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.store.video_calls_since(midnight)

    # ---------- submit / resume ----------
    def submit(self, items: list[dict[str, Any]], chat_id: str | None = None, background: bool = True) -> str:
        """Validate everything first (all-or-nothing), then run one ad per item. Returns the batch id."""
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
        pending = [i for i, it in enumerate(clean) if it["duration"] is None]
        if pending:
            try:
                picks = self.services.durations(
                    [{"language": clean[i]["language"], "text": clean[i]["text"]} for i in pending])
            except DurationEstimateError as exc:
                raise ValidationError(str(exc)) from exc
            for i, pick in zip(pending, picks):                  # a text longer than the longest clip gets that clip
                clean[i]["duration"] = pick["clip_seconds"]
                clean[i]["estimate"] = json.dumps(pick)
        used = self._video_calls_today()
        if used + len(clean) > self.s.limits.daily_video_cap:
            raise ValidationError(f"Daily video cap {self.s.limits.daily_video_cap}: {used} used today, "
                                  f"{len(clean)} requested")
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
        lines = [f"{len(ok)} of {len(jobs)} ads created."]
        lines += [f"Ad {j['idx'] + 1} ({j['language']}) failed: {j['error']}" for j in jobs if j["status"] == "failed"]
        self._safe(self.notifier.send_report, chat_id, "\n".join(lines))

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
            names = ", ".join(f"Ad {j['idx'] + 1} ({j['language']})" for j in jobs)
            self._safe(self.notifier.send_report, chat_id,
                       f"The server was restarted while {len(jobs)} ad(s) were unfinished. Resuming: {names}.")
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
            self._safe(self.notifier.send_report, self.store.batch_chat_id(job["batch_id"]),
                       f"Ad {job['idx'] + 1} ({job['language']}) has been at stage '{job['stage']}' for "
                       f"{idle / 60:.0f} min with no progress. It may be stuck.")
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
        for attempt in (1, 2):                       # one automatic retry; cached stages are not redone
            try:
                self.store.update_job(job_id, status="running", attempts=attempt)
                self.run_job(job_id, chat_id)
                return
            except Exception as exc:
                log.exception("job %s attempt %s failed", job_id, attempt)
                err = f"{type(exc).__name__}: {exc}"
                final = attempt == 2 or not getattr(exc, "retryable", True)   # policy blocks cannot succeed on retry
                self.store.update_job(job_id, status="failed" if final else "running", error=err)
                if final:
                    break
        job = self.store.get_job(job_id)
        self._safe(self.notifier.send_report, chat_id,
                   f"Ad {job['idx'] + 1} ({job['language']}) failed at stage '{job['stage']}': {job['error']}")

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("notification failed")

    # ---------- one ad ----------
    def run_job(self, job_id: str, chat_id: str | None = None) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        lang, text, body_stem, duration = job["language"], job["text"], job["body"], job["duration"]
        work = Path(self.s.paths.work_dir) / job_id
        work.mkdir(parents=True, exist_ok=True)
        stage = lambda name: self.store.update_job(job_id, stage=name)

        image = work / "image.png"
        if not image.exists():
            stage("image")
            self.services.image(text, image)

        clip = work / "video.mp4"
        if not clip.exists():
            stage("video")
            if self._video_calls_today() >= self.s.limits.daily_video_cap:
                raise RuntimeError(f"Daily video cap of {self.s.limits.daily_video_cap} reached")
            self.store.record_video_call()
            tmp = work / "video.part.mp4"
            self.services.video(text, lang, tmp, duration, image)
            os.replace(tmp, clip)
        clip_duration = media.probe(clip, self.s).duration

        words_file = work / "words.json"
        if not words_file.exists():
            stage("transcribe")
            words_file.write_text(json.dumps(self.services.transcribe(clip, lang), ensure_ascii=False))
        words = json.loads(words_file.read_text())

        plan_file = work / "plan.json"
        if not plan_file.exists():
            stage("plan")
            result = self.services.plan(text, lang, words, clip_duration, job.get("plan_hint"))
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
            "text": text, "body": body.stem, "video_duration": duration, "clip_seconds": clip_duration,
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
        total = len(self.store.batch_jobs(job["batch_id"]))
        warn = "\n⚠ The voice may be cut off at the end of the hook (the clip was probably too short)." \
            if meta["speech_may_be_cut_off"] else ""
        self._safe(self.notifier.send_video, chat_id, ad_dst,
                   f"{'Redo · ' if job.get('redo_of') else ''}Ad {job['idx'] + 1}/{total} · {lang} · {text[:120]} · id {job_id}{warn}")
        return meta

    # ---------- status ----------
    def status(self, batch_id: str) -> dict[str, Any]:
        jobs = self.store.batch_jobs(batch_id)
        counts = {k: sum(j["status"] == k for j in jobs) for k in ("queued", "running", "done", "failed")}
        return {"batch_id": batch_id, "total": len(jobs), **counts,
                "finished": counts["queued"] + counts["running"] == 0,
                "ads": [{"n": j["idx"] + 1, "job_id": j["id"], "language": j["language"], "status": j["status"], "stage": j["stage"],
                         "ad_path": j["ad_path"], "hook_path": j["hook_path"], "error": j["error"]} for j in jobs]}
