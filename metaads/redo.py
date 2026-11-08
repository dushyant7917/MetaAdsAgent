"""Redo part of a finished ad: work out what must be regenerated and what can be reused, propose it, and run it
only after the owner approves.

The ad is built in a fixed order and every stage feeds the next one:

    image -> video (Veo) -> transcribe (Whisper) -> plan (poster window) -> render_hook -> merge

Whisper and the planner read the Veo clip, so they only stay valid while that clip is kept. A change therefore
restarts the chain at one stage and redoes everything after it; everything before it is reused from the old ad.
The old ad is never touched and nothing is copied: a redo is a new job that records `redo_of` and how many leading
stages it reuses (`reused_stages`, see metaads.hookfiles), so the normal stage cache finds those files in the old
ad's folder and skips them.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from . import hookfiles, media
from .durations import DurationEstimateError
from .jobs import FORMAT_IMAGE, FORMAT_VIDEO, REVIEW_PENDING, REVIEW_REDONE
from .media import PosterWindow
from .pipeline import Pipeline, ValidationError

STAGES, FILES = hookfiles.STAGES, hookfiles.HOOK_FILES
IMAGE_AD_CHANGES = {"thumbnail_text", "image", "spoken_text"}   # what can be redone on an image ad and keep it an image ad
# What the owner wants changed -> the first stage that has to run again.
CHANGES = {
    "thumbnail_text": "image",   # new words on the image: a new image, so a new clip and everything after
    "spoken_text": "video",      # new dialogue: the image is kept, a new Veo clip is made from it
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
    def _file(self, job: dict[str, Any], stage: str) -> Path:
        """The hook file of `stage` that `job` uses: its own, or one of the ad it reuses from."""
        return hookfiles.stage_file(self.p.s, job, stage, self.p.store.get_job)

    def _job(self, job_id: str) -> dict[str, Any]:
        try:
            return self.p.store.get_job(job_id)
        except KeyError:
            raise ValidationError(f"Unknown ad '{job_id}'. Use list_ads to see recent ads.") from None

    def details(self, job_id: str) -> dict[str, Any]:
        """Everything the bot needs to understand an ad before proposing a redo."""
        job = self._job(job_id)
        files = {stage: self._file(job, stage) for stage in FILES}
        have = {stage: path.exists() for stage, path in files.items()}
        out: dict[str, Any] = {
            "job_id": job_id, "ad_number_in_batch": job["idx"] + 1, "language": job["language"],
            "spoken_text": job["spoken_text"], "thumbnail_text": job["thumbnail_text"], "ad_format": job["ad_format"],
            "body": job["body"], "clip_seconds_requested": job["duration"], "status": job["status"],
            "stage": job["stage"], "ad_path": job["ad_path"], "redo_of": job["redo_of"],
            "stored_stages": [s for s in FILES if have[s]],
        }
        if job.get("estimate"):
            out["estimated_speech_seconds"] = json.loads(job["estimate"]).get("speech_seconds")
        if have["transcribe"]:
            words = json.loads(files["transcribe"].read_text())
            out["spoken_words"] = " ".join(w["word"] for w in words)
            if words:
                out["speech_ends_at"] = words[-1]["end"]
        if have["video"]:
            out["clip_seconds_actual"] = media.probe(files["video"], self.p.s).duration
        if have["plan"]:
            plan = json.loads(files["plan"].read_text())
            out["poster"] = {"from": plan["window"]["start"], "to": plan["window"]["end"],
                             "words": plan["phrase"], "chosen_by": plan["method"]}
        if "speech_ends_at" in out and "clip_seconds_actual" in out:
            out["voice_may_be_cut_off"] = out["speech_ends_at"] >= out["clip_seconds_actual"] - 0.25
        return out

    # ---------- propose ----------
    def plan(self, job_id: str, changes: list[str], new_spoken_text: str | None = None,
             new_thumbnail_text: str | None = None, new_body: str | None = None, duration: int | None = None,
             poster_start: float | None = None, poster_end: float | None = None,
             poster_feedback: str | None = None) -> dict[str, Any]:
        """Work out the redo. Changes nothing and costs nothing; the owner must approve the result."""
        job = self._job(job_id)
        if job["status"] != "done":
            raise ValidationError(f"Ad {job_id} is not finished (status {job['status']}), so it cannot be redone yet.")
        changes = list(dict.fromkeys(changes))
        # An explicit value implies its change.
        for arg, change in ((new_spoken_text, "spoken_text"), (new_thumbnail_text, "thumbnail_text"),
                            (new_body, "body"), (duration, "duration"),
                            (poster_start, "poster_timing"), (poster_feedback, "poster_timing")):
            if arg is not None and change not in changes:
                changes.append(change)
        if not changes:
            raise ValidationError("Say what should change: " + ", ".join(CHANGES))
        bad = [c for c in changes if c not in CHANGES]
        if bad:
            raise ValidationError(f"Unknown change {bad}. Allowed: {', '.join(CHANGES)}")
        for needed, change in ((new_spoken_text, "spoken_text"), (new_thumbnail_text, "thumbnail_text"),
                               (new_body, "body"), (duration, "duration")):
            if change in changes and needed is None:
                raise ValidationError(f"The '{change}' change needs its new value.")
        if (poster_start is None) != (poster_end is None):
            raise ValidationError("Give both poster_start and poster_end, or neither.")
        if poster_start is not None and not 0 <= poster_start < poster_end:
            raise ValidationError("poster_start must be 0 or more and before poster_end.")
        if poster_start is not None and poster_feedback:
            raise ValidationError("Give either an exact poster window or feedback for the planner, not both.")
        is_image_ad = job["ad_format"] == FORMAT_IMAGE
        if is_image_ad and set(changes) <= IMAGE_AD_CHANGES:
            return self._plan_image_ad(job, changes, new_spoken_text, new_thumbnail_text)
        checked = self.p.validate_item({"language": job["language"], "spoken_text": new_spoken_text or job["spoken_text"],
                                        "thumbnail_text": new_thumbnail_text or job["thumbnail_text"],
                                        "body": new_body or job["body"], "duration": duration})

        start = min(STAGES.index(CHANGES[c]) for c in changes)
        notes: list[str] = []
        if is_image_ad:
            notes.append("This was an image ad; the redo makes a video ad from the same hook.")
        for i in range(start):                                   # a stage to reuse must still be on disk
            if not self._file(job, STAGES[i]).exists():
                notes.append(f"The old {STAGES[i]} file is no longer stored, so the redo starts from there.")
                start = i
                break
        redo_stages = STAGES[start:]
        if "video" in redo_stages and self.p.store.video_calls_today() + 1 > self.p.s.limits.daily_video_cap:
            raise ValidationError(f"Daily video cap {self.p.s.limits.daily_video_cap} is reached, so a new Veo clip "
                                  f"cannot be made today.")

        manual = poster_start is not None and "plan" in redo_stages
        if poster_start is not None and not manual:
            notes.append("The exact poster window is ignored: the clip is made again, so its timing is different. "
                         "The poster is placed on the new clip instead.")
        plan_file = self._file(job, "plan")
        old_plan = json.loads(plan_file.read_text()) if plan_file.exists() else None
        old_body = job["body"]
        redo, reuse = [], []
        spoken_changed = "spoken_text" in changes and checked["spoken_text"] != job["spoken_text"]
        thumbnail_changed = "thumbnail_text" in changes and checked["thumbnail_text"] != job["thumbnail_text"]
        needs_estimate = (spoken_changed or job["duration"] is None) and duration is None   # an image ad has no clip length yet
        if needs_estimate:
            redo.append({"step": "duration", "what": "Estimate the speaking time of the dialogue (1 Gemini call)"})
        for stage in redo_stages:
            redo.append({"step": stage, "what": self._redo_text(stage, manual, poster_start, poster_end, duration)})
        for stage in STAGES[:start]:
            reuse.append({"step": stage, "what": self._reuse_text(stage, old_plan, job["duration"])})
        if start <= STAGES.index("video") and not needs_estimate and duration is None:   # an input of the new Veo clip
            reuse.append({"step": "duration", "what": f"The clip length ({job['duration']} s) and its speaking-time estimate"})
        if "body" not in changes:
            reuse.append({"step": "body", "what": f"The body video ({old_body})"})

        plan_id = "r" + uuid.uuid4().hex[:6]
        args = {"job_id": job_id, "changes": changes, "start": start, "format": FORMAT_VIDEO,
                "new_spoken_text": checked["spoken_text"] if spoken_changed else None,
                "new_thumbnail_text": checked["thumbnail_text"] if thumbnail_changed else None,
                "new_body": checked["body"] if "body" in changes else None, "duration": duration,
                "poster_start": poster_start, "poster_end": poster_end, "poster_feedback": poster_feedback}
        self.p.store.create_redo_plan(plan_id, job_id, json.dumps(args))
        return {"plan_id": plan_id, "ad": f"{job_id} ({job['language']}): {job['spoken_text'][:self.p.s.messages.ad_label_chars]}",
                "will_redo": redo, "will_reuse": reuse, "notes": notes,
                "costs": self._costs(redo_stages, needs_estimate, manual),
                "next": "Show this plan to the owner. Only if they approve, call approve_redo with plan_id."}

    def _plan_image_ad(self, job: dict[str, Any], changes: list[str], new_spoken_text: str | None,
                       new_thumbnail_text: str | None) -> dict[str, Any]:
        """Redo of an image ad the owner asked for, changing only the image words (a new image) or the dialogue (the
        Meta ad copy; the image is kept). It has one stage and the result is again an image ad. Any other change
        (body, video, poster) turns it into a video ad: see plan()."""
        checked = self.p.validate_item({"language": job["language"], "ad_format": FORMAT_IMAGE,
                                        "spoken_text": new_spoken_text or job["spoken_text"],
                                        "thumbnail_text": new_thumbnail_text or job["thumbnail_text"],
                                        "body": job["body"]})
        new_image = "image" in changes or ("thumbnail_text" in changes and checked["thumbnail_text"] != job["thumbnail_text"])
        spoken_changed = "spoken_text" in changes and checked["spoken_text"] != job["spoken_text"]
        notes = []
        if not new_image and not (job["ad_path"] and Path(job["ad_path"]).is_file()):
            new_image = True
            notes.append("The old image is no longer stored, so a new image is made.")
        redo = [{"step": "image_ad", "what": "Make a new 4:5 image (1 gpt-image-2 call)"}] if new_image else []
        reuse = [] if new_image else [{"step": "image_ad", "what": "The existing 4:5 image"}]
        if spoken_changed:
            redo.append({"step": "ad_copy", "what": "Use the new dialogue as the Meta ad text"})
        plan_id = "r" + uuid.uuid4().hex[:6]
        args = {"job_id": job["id"], "changes": changes, "start": 0 if new_image else 1, "format": FORMAT_IMAGE,
                "new_spoken_text": checked["spoken_text"] if spoken_changed else None,
                "new_thumbnail_text": checked["thumbnail_text"] if checked["thumbnail_text"] != job["thumbnail_text"] else None,
                "new_body": None, "duration": None, "poster_start": None, "poster_end": None, "poster_feedback": None}
        self.p.store.create_redo_plan(plan_id, job["id"], json.dumps(args))
        return {"plan_id": plan_id, "ad": f"{job['id']} ({job['language']}, image ad): {job['spoken_text'][:self.p.s.messages.ad_label_chars]}",
                "will_redo": redo, "will_reuse": reuse, "notes": notes,
                "costs": {"gpt_image_calls": int(new_image), "veo_calls": 0, "gemini_calls": 0},
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
        is_image_ad = a.get("format") == FORMAT_IMAGE
        if (not is_image_ad and start <= STAGES.index("video")
                and self.p.store.video_calls_today() + 1 > self.p.s.limits.daily_video_cap):
            raise ValidationError("Daily video cap reached, so a new Veo clip cannot be made today.")
        if not self.p.store.set_redo_plan_status(plan_id, "pending", "approved"):
            raise ValidationError(f"Plan {plan_id} was already used.")
        try:
            item = {"language": job["language"], "spoken_text": a["new_spoken_text"] or job["spoken_text"],
                    "thumbnail_text": a["new_thumbnail_text"] or job["thumbnail_text"],
                    "body": a["new_body"] or job["body"], "duration": a["duration"] or job["duration"],
                    "estimate": job["estimate"], "redo_of": job["id"], "review": REVIEW_PENDING,
                    "ad_format": a.get("format", FORMAT_VIDEO)}
            if is_image_ad:
                item["duration"], item["estimate"] = None, None
            elif (a["new_spoken_text"] or item["duration"] is None) and a["duration"] is None:
                try:
                    pick = self.p.services.durations([{"language": item["language"],
                                                       "spoken_text": item["spoken_text"]}])[0]
                except DurationEstimateError as exc:
                    raise ValidationError(str(exc)) from exc
                item["duration"], item["estimate"] = pick["clip_seconds"], json.dumps(pick)
            elif a["duration"] is not None:
                item["estimate"] = None                                  # chosen by the owner, not estimated
            if a["poster_feedback"] and start <= STAGES.index("plan"):
                item["plan_hint"] = self._hint(job, a["poster_feedback"])
            if not is_image_ad:
                for stage in STAGES[:start]:                             # reused, never copied: it must still exist
                    if not self._file(job, stage).exists():
                        raise ValidationError(f"The {stage} file of ad {job['id']} is gone, so this plan cannot reuse it. "
                                              f"Make a new plan.")
                item["reused_stages"] = start
            chat_id = self.p.store.batch_chat_id(job["batch_id"])
            batch_id, [new_id] = self.p.store.create_batch([item], chat_id, self.p.owner)
            if job["review"] == REVIEW_PENDING:                          # the old ad is replaced, not waiting any more
                self.p.store.set_review([job["id"]], REVIEW_REDONE)
            if is_image_ad and start > 0:                                # the same image file is the new ad too
                self.p.store.update_job(new_id, ad_path=job["ad_path"])
            elif a["poster_start"] is not None and STAGES[start] == "plan":
                self._manual_poster_window(job, new_id, a)
        except Exception:
            self.p.store.set_redo_plan_status(plan_id, "approved", "pending")   # nothing ran; the plan stays usable
            raise
        self.p._start(batch_id, [new_id], True)
        return {"started": True, "batch_id": batch_id, "job_id": new_id, "redo_of": job["id"],
                "message": "Redo started. The new ad arrives in the chat when it is ready; the old one is kept."}

    def _hint(self, job: dict[str, Any], feedback: str) -> str:
        plan_file = self._file(job, "plan")
        previous = ""
        if plan_file.exists():
            old = json.loads(plan_file.read_text())
            previous = (f"The earlier choice was the words \"{old['phrase']}\" "
                        f"(from {old['window']['start']} s to {old['window']['end']} s). ")
        return f"{previous}The owner said about it: \"{feedback.strip()}\""

    def _manual_poster_window(self, job: dict[str, Any], new_id: str, a: dict) -> None:
        """The owner gave the exact poster window: the new ad's own plan file, made from the reused clip's length."""
        s = self.p.s
        clip = media.probe(self._file(job, "video"), s).duration
        lo = max(a["poster_start"], 0.0)
        hi = min(a["poster_end"], clip)
        if hi - lo < s.poster.min_seconds:
            hi = min(lo + s.poster.min_seconds, clip)
        if hi <= lo:
            raise ValidationError(f"The clip is only {clip:.1f} s long; the poster cannot start at {lo} s.")
        plan = {"window": {"start": round(lo, 3), "end": round(hi, 3)}, "start_idx": None, "end_idx": None,
                "phrase": "", "method": "manual", "notes": ["Window set by the owner"]}
        new = hookfiles.work_folder(s, new_id)
        new.mkdir(parents=True, exist_ok=True)
        (new / FILES["plan"]).write_text(json.dumps(plan, ensure_ascii=False))
