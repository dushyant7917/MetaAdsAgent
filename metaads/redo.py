"""Redo part of a finished ad: work out what must be regenerated and what can be reused, propose it, and run it
only after the owner approves.

The ad is built in a fixed order and every stage feeds the next one:

    image -> video (Veo) -> transcribe (Whisper) -> plan (poster window) -> render_hook -> merge

Whisper and the planner read the Veo clip, so they only stay valid while that clip is kept. A change therefore
restarts the chain at one stage and redoes everything after it; everything before it is copied from the old ad.
The old ad is never touched: a redo is a new job whose work folder is seeded with the reused files, so the normal
stage cache skips them.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import media
from .durations import DurationEstimateError
from .media import PosterWindow
from .pipeline import Pipeline, ValidationError

STAGES = ["image", "video", "transcribe", "plan", "render_hook", "merge"]
FILES = {"image": "image.png", "video": "video.mp4", "transcribe": "words.json", "plan": "plan.json",
         "render_hook": "hook.mp4", "merge": "ad.mp4"}
# What the owner wants changed -> the first stage that has to run again.
CHANGES = {
    "text": "image",             # new hook text: the image carries the text, so everything is new
    "image": "image",            # a new reporter image needs a new Veo clip, so everything after is new too
    "video": "video",            # same image, new Veo clip (new voice and picture)
    "duration": "video",         # a different clip length is a new Veo clip
    "poster_timing": "plan",     # same clip, different moment for the poster
    "body": "merge",             # same hook, different body video
}


class RedoPlanner:
    def __init__(self, pipeline: Pipeline):
        self.p = pipeline

    # ---------- what exists ----------
    def _work(self, job_id: str) -> Path:
        return Path(self.p.s.paths.work_dir) / job_id

    def _job(self, job_id: str) -> dict[str, Any]:
        try:
            return self.p.store.get_job(job_id)
        except KeyError:
            raise ValidationError(f"Unknown ad '{job_id}'. Use list_ads to see recent ads.") from None

    def details(self, job_id: str) -> dict[str, Any]:
        """Everything the bot needs to understand an ad before proposing a redo."""
        job = self._job(job_id)
        work = self._work(job_id)
        have = {stage: (work / name).exists() for stage, name in FILES.items()}
        out: dict[str, Any] = {
            "job_id": job_id, "ad_number_in_batch": job["idx"] + 1, "language": job["language"], "text": job["text"],
            "body": job["body"], "clip_seconds_requested": job["duration"], "status": job["status"],
            "stage": job["stage"], "ad_path": job["ad_path"], "hook_path": job["hook_path"], "redo_of": job["redo_of"],
            "stored_stages": [s for s in STAGES if have[s]],
        }
        if job.get("estimate"):
            out["estimated_speech_seconds"] = json.loads(job["estimate"]).get("speech_seconds")
        if have["transcribe"]:
            words = json.loads((work / "words.json").read_text())
            out["spoken_words"] = " ".join(w["word"] for w in words)
            if words:
                out["speech_ends_at"] = words[-1]["end"]
        if have["video"]:
            out["clip_seconds_actual"] = media.probe(work / "video.mp4", self.p.s).duration
        if have["plan"]:
            plan = json.loads((work / "plan.json").read_text())
            out["poster"] = {"from": plan["window"]["start"], "to": plan["window"]["end"],
                             "words": plan["phrase"], "chosen_by": plan["method"]}
        if "speech_ends_at" in out and "clip_seconds_actual" in out:
            out["voice_may_be_cut_off"] = out["speech_ends_at"] >= out["clip_seconds_actual"] - 0.25
        return out

    # ---------- propose ----------
    def plan(self, job_id: str, changes: list[str], new_text: str | None = None, new_body: str | None = None,
             duration: int | None = None, poster_start: float | None = None, poster_end: float | None = None,
             poster_feedback: str | None = None) -> dict[str, Any]:
        """Work out the redo. Changes nothing and costs nothing; the owner must approve the result."""
        job = self._job(job_id)
        if job["status"] != "done":
            raise ValidationError(f"Ad {job_id} is not finished (status {job['status']}), so it cannot be redone yet.")
        changes = list(dict.fromkeys(changes))
        # An explicit value implies its change.
        for arg, change in ((new_text, "text"), (new_body, "body"), (duration, "duration"),
                            (poster_start, "poster_timing"), (poster_feedback, "poster_timing")):
            if arg is not None and change not in changes:
                changes.append(change)
        if not changes:
            raise ValidationError("Say what should change: " + ", ".join(CHANGES))
        bad = [c for c in changes if c not in CHANGES]
        if bad:
            raise ValidationError(f"Unknown change {bad}. Allowed: {', '.join(CHANGES)}")
        for needed, change in ((new_text, "text"), (new_body, "body"), (duration, "duration")):
            if change in changes and needed is None:
                raise ValidationError(f"The '{change}' change needs its new value.")
        if (poster_start is None) != (poster_end is None):
            raise ValidationError("Give both poster_start and poster_end, or neither.")
        if poster_start is not None and not 0 <= poster_start < poster_end:
            raise ValidationError("poster_start must be 0 or more and before poster_end.")
        if poster_start is not None and poster_feedback:
            raise ValidationError("Give either an exact poster window or feedback for the planner, not both.")
        checked = self.p.validate_item({"language": job["language"], "text": new_text or job["text"],
                                        "body": new_body or job["body"], "duration": duration})

        work = self._work(job_id)
        start = min(STAGES.index(CHANGES[c]) for c in changes)
        notes: list[str] = []
        for i in range(start):                                   # a stage to reuse must still be on disk
            if not (work / FILES[STAGES[i]]).exists():
                notes.append(f"The old {STAGES[i]} file is no longer stored, so the redo starts from there.")
                start = i
                break
        redo_stages = STAGES[start:]
        if "video" in redo_stages and self.p._video_calls_today() + 1 > self.p.s.limits.daily_video_cap:
            raise ValidationError(f"Daily video cap {self.p.s.limits.daily_video_cap} is reached, so a new Veo clip "
                                  f"cannot be made today.")

        manual = poster_start is not None and "plan" in redo_stages
        if poster_start is not None and not manual:
            notes.append("The exact poster window is ignored: the clip is made again, so its timing is different. "
                         "The poster is placed on the new clip instead.")
        old_plan = json.loads((work / "plan.json").read_text()) if (work / "plan.json").exists() else None
        old_body = job["body"]
        redo, reuse = [], []
        text_changed = "text" in changes and checked["text"] != job["text"]
        if text_changed and duration is None:
            redo.append({"step": "duration", "what": "Estimate the speaking time of the new text (1 Gemini call)"})
        for stage in redo_stages:
            redo.append({"step": stage, "what": self._redo_text(stage, manual, poster_start, poster_end, duration)})
        for stage in STAGES[:start]:
            reuse.append({"step": stage, "what": self._reuse_text(stage, old_plan, job["duration"])})
        if start <= STAGES.index("video") and not text_changed and duration is None:   # an input of the new Veo clip
            reuse.append({"step": "duration", "what": f"The clip length ({job['duration']} s) and its speaking-time estimate"})
        if "body" not in changes:
            reuse.append({"step": "body", "what": f"The body video ({old_body})"})

        plan_id = "r" + uuid.uuid4().hex[:6]
        args = {"job_id": job_id, "changes": changes, "start": start, "new_text": checked["text"] if text_changed else None,
                "new_body": checked["body"] if "body" in changes else None, "duration": duration,
                "poster_start": poster_start, "poster_end": poster_end, "poster_feedback": poster_feedback}
        self.p.store.create_redo_plan(plan_id, job_id, json.dumps(args))
        return {"plan_id": plan_id, "ad": f"{job_id} ({job['language']}): {job['text'][:80]}",
                "will_redo": redo, "will_reuse": reuse, "notes": notes,
                "costs": self._costs(redo_stages, text_changed and duration is None, manual),
                "next": "Show this plan to the owner. Only if they approve, call approve_redo with plan_id."}

    @staticmethod
    def _redo_text(stage, manual, p_start, p_end, duration) -> str:
        return {
            "image": "Make a new reporter image (1 gpt-image-2 call)",
            "video": "Make a new Veo clip" + (f" of {duration} s" if duration else "") + " (1 Veo call, counts toward the daily cap)",
            "transcribe": "Transcribe the new clip with Whisper (local, free)",
            "plan": (f"Set the poster window to {p_start}-{p_end} s, as asked (no AI call)" if manual
                     else "Choose when the poster appears, with Gemini (1 call)"),
            "render_hook": "Render the hook video with the poster (local, free)",
            "merge": "Join the hook and the body video into the final ad (local, free)",
        }[stage]

    @staticmethod
    def _reuse_text(stage, old_plan: dict | None, clip_seconds: int | None) -> str:
        if stage == "plan" and old_plan:
            w = old_plan["window"]
            return f"The poster window ({w['start']}-{w['end']} s, words: {old_plan['phrase'] or 'n/a'})"
        return {
            "image": "The existing reporter image",
            "video": f"The existing Veo clip ({clip_seconds} s, same voice and picture)",
            "transcribe": "The existing word timings (no new Whisper run)",
            "plan": "The existing poster window",
            "render_hook": "The existing hook video",
        }[stage]

    @staticmethod
    def _costs(redo_stages: list[str], estimate: bool, manual_window: bool) -> dict[str, int]:
        return {"gpt_image_calls": int("image" in redo_stages), "veo_calls": int("video" in redo_stages),
                "gemini_calls": int(estimate) + int("plan" in redo_stages and not manual_window)}

    # ---------- approve and run ----------
    def approve(self, plan_id: str) -> dict[str, Any]:
        row = self.p.store.get_redo_plan(plan_id)
        if row is None:
            raise ValidationError(f"Unknown redo plan '{plan_id}'. Make a new plan first.")
        if row["status"] != "pending":
            raise ValidationError(f"Plan {plan_id} was already {row['status']}.")
        a = json.loads(row["args"])
        job = self._job(a["job_id"])
        start = a["start"]
        if start <= STAGES.index("video") and self.p._video_calls_today() + 1 > self.p.s.limits.daily_video_cap:
            raise ValidationError("Daily video cap reached, so a new Veo clip cannot be made today.")
        if not self.p.store.set_redo_plan_status(plan_id, "pending", "approved"):
            raise ValidationError(f"Plan {plan_id} was already used.")
        try:
            item = {"language": job["language"], "text": a["new_text"] or job["text"],
                    "body": a["new_body"] or job["body"], "duration": a["duration"] or job["duration"],
                    "estimate": job["estimate"], "redo_of": job["id"]}
            if a["new_text"] and a["duration"] is None:
                try:
                    pick = self.p.services.durations([{"language": item["language"], "text": item["text"]}])[0]
                except DurationEstimateError as exc:
                    raise ValidationError(str(exc)) from exc
                item["duration"], item["estimate"] = pick["clip_seconds"], json.dumps(pick)
            elif a["duration"] is not None:
                item["estimate"] = None                                  # chosen by the owner, not estimated
            if a["poster_feedback"] and start <= STAGES.index("plan"):
                item["plan_hint"] = self._hint(job["id"], a["poster_feedback"])
            chat_id = self.p.store.batch_chat_id(job["batch_id"])
            batch_id, [new_id] = self.p.store.create_batch([item], chat_id, self.p.owner)
            self._seed(job["id"], new_id, start, a)
        except Exception:
            self.p.store.set_redo_plan_status(plan_id, "approved", "pending")   # nothing ran; the plan stays usable
            raise
        self.p._start(batch_id, [new_id], True)
        return {"started": True, "batch_id": batch_id, "job_id": new_id, "redo_of": job["id"],
                "message": "Redo started. The new ad arrives in the chat when it is ready; the old one is kept."}

    def _hint(self, old_job_id: str, feedback: str) -> str:
        plan_file = self._work(old_job_id) / "plan.json"
        previous = ""
        if plan_file.exists():
            old = json.loads(plan_file.read_text())
            previous = (f"The earlier choice was the words \"{old['phrase']}\" "
                        f"(from {old['window']['start']} s to {old['window']['end']} s). ")
        return f"{previous}The owner said about it: \"{feedback.strip()}\""

    def _seed(self, old_id: str, new_id: str, start: int, a: dict) -> None:
        """Copy the reusable files into the new job's folder; the normal stage cache then skips those stages."""
        old, new = self._work(old_id), self._work(new_id)
        new.mkdir(parents=True, exist_ok=True)
        for stage in STAGES[:start]:
            shutil.copy2(old / FILES[stage], new / FILES[stage])
        if a["poster_start"] is not None and STAGES[start] == "plan":
            s = self.p.s
            clip = media.probe(new / "video.mp4", s).duration
            lo = max(a["poster_start"], 0.0)
            hi = min(a["poster_end"], clip)
            if hi - lo < s.poster.min_seconds:
                hi = min(lo + s.poster.min_seconds, clip)
            if hi <= lo:
                raise ValidationError(f"The clip is only {clip:.1f} s long; the poster cannot start at {lo} s.")
            plan = {"window": {"start": round(lo, 3), "end": round(hi, 3)}, "start_idx": None, "end_idx": None,
                    "phrase": "", "method": "manual", "notes": ["Window set by the owner"]}
            (new / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
