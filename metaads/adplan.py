"""The daily ad plan: how many ads, which hooks, shown to the owner as ONE message and made only after they approve.

The counts (per language, video / image / proven hook on a new body) come from metaads.planning, never from the LLM.
The LLM (Hermes) only writes the texts of the new hooks and their Hindi translations; `propose` checks them against
the counts, the language's script, duplicates and the sample hooks. The plan lives in the jobs database (table
ad_plans), not in a chat, so the owner's reply is handled correctly whatever session it arrives in.

Approving creates the ads exactly like the owner asking for them: new hooks through `Pipeline.submit` (in batches of
at most limits.max_batch_items, review pending) and each winning hook on its new body through a redo plan. The finished
ads then wait for the owner's decision as usual (see the ad-review skill); nothing is uploaded here.
"""
from __future__ import annotations

import logging
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from .config import Settings, load_product
from .jobrunner import ping
from .jobs import FORMAT_IMAGE, FORMAT_VIDEO, PLAN_APPROVED, PLAN_PENDING, PLAN_REJECTED, JobStore
from .messages import PLAN, blocks, entry, headline, lines
from .meta import IST
from .pipeline import Pipeline, ValidationError
from .redo import RedoPlanner

log = logging.getLogger("metaads.adplan")

NEW_HOOK, PROVEN_HOOK = "new_hook", "proven_hook"


class AdPlanError(ValueError):
    """The plan or the request is not acceptable; the message says what to fix."""


def native_words(text: str, script: str) -> int:
    """Words that contain at least one letter of the given Unicode script (e.g. DEVANAGARI)."""
    return sum(any(unicodedata.name(ch, "").startswith(script) for ch in word) for word in text.split())


@dataclass
class AdPlans:
    s: Settings
    store: JobStore
    plan_new_ads: Callable[[], dict]            # metaads.planning's plan (raises RuntimeError when Meta is unreachable)
    pipeline: Pipeline
    redo: RedoPlanner
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    heartbeat: str = ""                         # healthchecks.io URL: pinged when a plan was made, /fail when Meta failed

    # ---------- for the hook writer ----------
    def hook_brief(self) -> dict[str, Any]:
        """What to write and what already exists: the brief and sample hooks, each language's script, the hooks
        already made (newest first), and the rule for Hindi translations."""
        cfg = self.s.ad_planning
        return {
            "brief": load_product()["hook_brief"],
            "languages": self.s.languages.to_dict(),
            "scripts": self.s.language_scripts.to_dict(),
            "min_native_words": cfg.min_native_words,
            "recent_hooks": {code: self.store.known_hooks(code, cfg.recent_hooks_limit) for code in self.s.languages},
            "hindi_translation_rule": f"Every text of a language other than {self.s.languages[cfg.gloss_language]} needs "
                                      f"its {self.s.languages[cfg.gloss_language]} translation (spoken_hindi, and "
                                      f"thumbnail_hindi when the thumbnail text differs from the spoken text). It is "
                                      f"for the owner's DM only and never goes into an ad.",
        }

    # ---------- propose ----------
    def propose(self, new_hooks: list[dict], proven_glosses: list[dict] | None = None) -> dict[str, Any]:
        """Check the written hooks against today's plan, store the plan and return the message for the owner.

        new_hooks: [{language, ad_format, spoken_text, thumbnail_text?, spoken_hindi?, thumbnail_hindi?}], exactly as
          many per language and format as the plan asks for.
        proven_glosses: [{hook_job_id, spoken_hindi, thumbnail_hindi?}] for the winning hooks of non-Hindi languages.
        """
        try:
            plan = self.plan_new_ads()
        except RuntimeError:
            ping(self.heartbeat, "/fail", self.s)
            raise
        code_of = {name: code for code, name in self.s.languages.items()}
        wanted = {(code_of[c["language"]], fmt): c[key] for c in plan["campaigns"]
                  for fmt, key in ((FORMAT_VIDEO, "new_hook_video_ads"), (FORMAT_IMAGE, "new_hook_image_ads"))}
        if plan["new_ads_total"] == 0:
            self.store.supersede_pending_ad_plans()                 # an older plan would no longer fit the budget
            ping(self.heartbeat, settings=self.s)                    # the run did its job: there is nothing to plan
            return {"proposed": False, "message": self._nothing_message(plan), "notes": plan["notes"]}

        errors: list[str] = []
        supplied: dict[tuple[str, str], int] = {}
        items: list[dict] = []
        for h in new_hooks:
            item, problems = self._new_hook(h)
            errors += problems
            if item:
                key = (item["language"], item["ad_format"])
                supplied[key] = supplied.get(key, 0) + 1
                items.append(item)
        for key in sorted(set(wanted) | set(supplied)):
            if supplied.get(key, 0) != wanted.get(key, 0):
                errors.append(f"{self.s.languages[key[0]]} {key[1]} hooks: the plan needs {wanted.get(key, 0)}, "
                              f"got {supplied.get(key, 0)}")
        errors += self._duplicates(items)

        glosses = {g["hook_job_id"]: g for g in proven_glosses or []}
        proven: list[dict] = []
        for c in plan["campaigns"]:
            code = code_of[c["language"]]
            for entry in c["proven_hook_ads"]:
                for body in entry["bodies"]:
                    item = {"kind": PROVEN_HOOK, "ad_format": FORMAT_VIDEO, "language": code, "body": body,
                            "hook_job_id": entry["hook_job_id"], "spoken_text": entry["spoken_text"],
                            "thumbnail_text": entry["thumbnail_text"], "results": entry["results"],
                            "cpr_inr": entry["cpr_inr"], **self._glosses(glosses.get(entry["hook_job_id"]))}
                    errors += self._gloss_problems(item, f"winning hook of {entry['hook_job_id']}")
                    proven.append(item)
        if errors:
            return {"proposed": False, "errors": sorted(set(errors)),
                    "expected_new_hooks": {f"{self.s.languages[c]} {f}": n for (c, f), n in sorted(wanted.items())}}

        position = {code_of[c["language"]]: n for n, c in enumerate(plan["campaigns"])}
        items = proven + items
        items.sort(key=lambda i: (position[i["language"]], i["kind"] != PROVEN_HOOK, i["ad_format"] != FORMAT_VIDEO))
        for n, item in enumerate(items, 1):
            item["n"] = n
        plan_id = "p" + uuid.uuid4().hex[:6]
        summary = {k: plan[k] for k in ("launch_day", "budget_left_inr", "monthly_budget_inr",
                                        "expected_cost_per_new_ad_inr", "format_reason", "notes")}
        self.store.create_ad_plan(plan_id, plan["launch_day"], items, summary)
        ping(self.heartbeat, settings=self.s)
        return {"proposed": True, "plan_id": plan_id, "message": self.render(self.store.get_ad_plan(plan_id))}

    def _new_hook(self, h: dict) -> tuple[dict | None, list[str]]:
        code = (h.get("language") or "").strip().lower()
        fmt = h.get("ad_format") or FORMAT_VIDEO
        spoken = (h.get("spoken_text") or "").strip()
        thumbnail = (h.get("thumbnail_text") or "").strip() or spoken
        label = f"{code or '?'} {fmt} hook {spoken[:self.s.messages.hook_label_chars]!r}"
        errors = []
        if code not in self.s.languages:
            return None, [f"{label}: unknown language {code!r}"]
        if fmt not in (FORMAT_VIDEO, FORMAT_IMAGE):
            errors.append(f"{label}: ad_format must be video or image")
        if not spoken:
            return None, errors + [f"{label}: spoken_text is empty"]
        item = {"kind": NEW_HOOK, "ad_format": fmt, "language": code, "body": None, "hook_job_id": None,
                "spoken_text": spoken, "thumbnail_text": thumbnail, **self._glosses(h)}
        errors += self._script_problems(item, label) + self._gloss_problems(item, label)
        return (item if not errors else None), errors

    @staticmethod
    def _glosses(h: dict | None) -> dict[str, str]:
        h = h or {}
        return {"spoken_hindi": (h.get("spoken_hindi") or "").strip(), "thumbnail_hindi": (h.get("thumbnail_hindi") or "").strip()}

    def _script_problems(self, item: dict, label: str) -> list[str]:
        script, need = self.s.language_scripts[item["language"]], self.s.ad_planning.min_native_words
        errors = []
        if native_words(item["spoken_text"], script) < need:
            errors.append(f"{label}: the spoken text needs at least {need} words in {script.title()} script")
        if native_words(item["thumbnail_text"], script) < 1:
            errors.append(f"{label}: the thumbnail text needs at least 1 word in {script.title()} script")
        return errors

    def _gloss_problems(self, item: dict, label: str) -> list[str]:
        """Languages other than the owner's (settings.ad_planning.gloss_language) need a translation into it of every
        text shown to them: written in that language's script and not just the original text again."""
        gloss_language = self.s.ad_planning.gloss_language
        if item["language"] == gloss_language:
            return []
        gloss_script, gloss_name = self.s.language_scripts[gloss_language], self.s.languages[gloss_language]
        errors = []
        for gloss, text, field, needed in (
                (item["spoken_hindi"], item["spoken_text"], "spoken_hindi", True),
                (item["thumbnail_hindi"], item["thumbnail_text"], "thumbnail_hindi", item["thumbnail_text"] != item["spoken_text"])):
            if not needed:
                continue
            if not gloss:
                errors.append(f"{label}: {field} (the {gloss_name} translation) is missing")
            elif gloss == text or native_words(gloss, gloss_script) < 1:
                errors.append(f"{label}: {field} must be a {gloss_name} translation in {gloss_script.title()} script, "
                              f"not the original text")
        return errors

    def _duplicates(self, items: list[dict]) -> list[str]:
        """Exact repeats of another hook in the plan, of an ad already made, or of a sample hook."""
        samples = {h["spoken_text"] for h in load_product()["hook_brief"]["sample_hooks"]}
        made = {code: {h["spoken_text"] for h in self.store.known_hooks(code)} for code in self.s.languages}
        errors, seen, n = [], set(), self.s.messages.hook_label_chars
        for i in items:
            if i["kind"] != NEW_HOOK:
                continue
            key = (i["language"], i["spoken_text"])
            if key in seen:
                errors.append(f"{i['language']} hook {i['spoken_text'][:n]!r} appears twice in the plan")
            elif i["spoken_text"] in made[i["language"]] or i["spoken_text"] in samples:
                errors.append(f"{i['language']} hook {i['spoken_text'][:n]!r} already exists (made earlier or a sample)")
            seen.add(key)
        return errors

    # ---------- read, change, decide ----------
    def current(self) -> dict[str, Any]:
        """The pending plan with its message, or what happened to the latest one."""
        plan = self.store.latest_ad_plan(PLAN_PENDING)
        if plan:
            return {"plan_id": plan["id"], "status": plan["status"], "message": self.render(plan),
                    "items": plan["items"]}
        last = self.store.latest_ad_plan()
        return {"plan_id": None, "status": None,
                "last_plan": {"plan_id": last["id"], "status": last["status"], "launch_day": last["launch_day"]} if last else None,
                "message": "No ad plan is waiting for a decision."}

    def revise(self, plan_id: str, replace: list[dict] | None = None, remove: list[int] | None = None) -> dict[str, Any]:
        """Change texts of new-hook items or drop items. Adding items is not possible (it would change the spend)."""
        plan = self._pending(plan_id)
        items = {i["n"]: i for i in plan["items"]}
        errors = []
        for n in remove or []:
            if items.pop(n, None) is None:
                errors.append(f"#{n} is not in the plan")
        for r in replace or []:
            item = items.get(r.get("n"))
            if item is None:
                errors.append(f"#{r.get('n')} is not in the plan")
            elif item["kind"] != NEW_HOOK:
                errors.append(f"#{item['n']} is a winning hook on a new body; drop it instead of changing its text")
            else:
                spoken = (r.get("spoken_text") or item["spoken_text"]).strip()
                if (r.get("thumbnail_text") or "").strip():
                    thumbnail = r["thumbnail_text"].strip()
                else:                                              # it followed the dialogue, or it keeps its own words
                    thumbnail = spoken if item["thumbnail_text"] == item["spoken_text"] else item["thumbnail_text"]
                spoken_hindi = (r.get("spoken_hindi") or "").strip() or ("" if spoken != item["spoken_text"] else item["spoken_hindi"])
                thumbnail_hindi = ((r.get("thumbnail_hindi") or "").strip()
                                   or ("" if thumbnail != item["thumbnail_text"] else item["thumbnail_hindi"]))
                changed = {**item, "spoken_text": spoken, "thumbnail_text": thumbnail,
                           "spoken_hindi": spoken_hindi, "thumbnail_hindi": thumbnail_hindi}
                label = f"#{item['n']}"
                errors += self._script_problems(changed, label) + self._gloss_problems(changed, label)
                items[item["n"]] = changed
        errors += self._duplicates(list(items.values()))
        if errors:
            return {"revised": False, "errors": sorted(set(errors))}
        if not items:
            return {"revised": False, "errors": ["Nothing would be left in the plan; reject it instead."]}
        self.store.update_ad_plan_items(plan_id, sorted(items.values(), key=lambda i: i["n"]))
        return {"revised": True, "plan_id": plan_id, "message": self.render(self.store.get_ad_plan(plan_id))}

    def reject(self, plan_id: str) -> dict[str, Any]:
        self._pending(plan_id)
        if not self.store.set_ad_plan_status(plan_id, PLAN_PENDING, PLAN_REJECTED):
            raise AdPlanError(f"Plan {plan_id} was already decided.")
        return {"rejected": True, "plan_id": plan_id}

    def approve(self, plan_id: str) -> dict[str, Any]:
        """Create the ads of the plan. Only the newest pending plan whose launch has not started can be approved."""
        plan = self._pending(plan_id)
        if self.clock() >= datetime.combine(date.fromisoformat(plan["launch_day"]), datetime.min.time(),
                                            tzinfo=IST).replace(hour=self.s.meta.start_hour_ist):
            raise AdPlanError(f"Plan {plan_id} was for the launch of {plan['launch_day']}, which has started. "
                              "Wait for the next plan.")
        if not self.store.set_ad_plan_status(plan_id, PLAN_PENDING, PLAN_APPROVED):
            raise AdPlanError(f"Plan {plan_id} was already decided.")
        items, errors, started = plan["items"], [], 0
        new = [i for i in items if i["kind"] == NEW_HOOK]
        size = self.s.limits.max_batch_items
        for chunk in (new[k:k + size] for k in range(0, len(new), size)):
            try:
                batch_id = self.pipeline.submit([{k: i[k] for k in ("language", "spoken_text", "thumbnail_text", "ad_format")}
                                                 for i in chunk], review=True)
            except ValidationError as exc:
                errors += [f"#{i['n']}: {exc}" for i in chunk]
                for i in chunk:
                    i["error"] = str(exc)
                continue
            started += len(chunk)
            for i in chunk:
                i["batch_id"] = batch_id
        for i in (i for i in items if i["kind"] == PROVEN_HOOK):
            try:
                i["job_id"] = self.redo.approve(self.redo.plan(i["hook_job_id"], ["body"], new_body=i["body"])["plan_id"])["job_id"]
                started += 1
            except ValidationError as exc:
                errors.append(f"#{i['n']}: {exc}")
                i["error"] = str(exc)
        self.store.update_ad_plan_items(plan_id, items)
        if started == 0:                                           # nothing began: the plan stays usable
            self.store.set_ad_plan_status(plan_id, PLAN_APPROVED, PLAN_PENDING)
        return {"approved": started > 0, "plan_id": plan_id, "ads_started": started, "errors": errors,
                "message": (f"Making {started} ads. They arrive here one by one, then each waits for your decision."
                            if started else "Nothing could be started; the plan is still waiting.")}

    def _pending(self, plan_id: str) -> dict[str, Any]:
        plan = self.store.get_ad_plan(plan_id)
        if plan is None:
            raise AdPlanError(f"Unknown plan '{plan_id}'. Call get_ad_plan for the current one.")
        if plan["status"] != PLAN_PENDING:
            raise AdPlanError(f"Plan {plan_id} is {plan['status']}, not waiting for a decision.")
        newest = self.store.latest_ad_plan(PLAN_PENDING)
        if newest["id"] != plan_id:
            raise AdPlanError(f"Plan {plan_id} is not the newest. Call get_ad_plan for the current one.")
        return plan

    # ---------- the message the owner reads ----------
    def _nothing_message(self, plan: dict) -> str:
        day = date.fromisoformat(plan["launch_day"]).strftime("%a %d %b")
        return f"No new ads planned for the launch on {day}. " + " ".join(plan["notes"])

    def render(self, plan: dict) -> str:
        """The Telegram text: totals, then per language the numbered ads with both texts (and Hindi in brackets)."""
        items, summary = plan["items"], plan["summary"]
        day = date.fromisoformat(plan["launch_day"]).strftime("%a %d %b")
        count = lambda kind=None, fmt=None: sum((kind is None or i["kind"] == kind) and (fmt is None or i["ad_format"] == fmt)
                                                for i in items)
        totals = lines(
            f"{len(items)} new ads: {count(NEW_HOOK, FORMAT_VIDEO)} new-hook videos, {count(NEW_HOOK, FORMAT_IMAGE)} "
            f"new-hook images, {count(PROVEN_HOOK)} winning hooks on a new body.",
            f"Budget left Rs {summary['budget_left_inr']:,.0f} of Rs {summary['monthly_budget_inr']:,.0f}; about "
            f"Rs {summary['expected_cost_per_new_ad_inr']:,.0f} per new ad.",
            f"Image or video: {summary['format_reason']}.", *summary["notes"])
        by_language = []
        for code in dict.fromkeys(i["language"] for i in sorted(items, key=lambda i: i["n"])):   # numbers run in order
            mine = [i for i in items if i["language"] == code]
            by_language.append(blocks(f"{self.s.languages[code]} ({code}): {len(mine)} ad{'s' if len(mine) != 1 else ''}",
                                      *(self._item(i) for i in mine)))
        return blocks(headline(PLAN, f"Ad plan for the launch on {day} (ads start {self.s.meta.start_hour_ist}:00 IST)"), totals, *by_language,
                      "Reply: approve (makes all of these), change #n to <new text>, drop #n, or reject.")

    def _item(self, i: dict) -> str:
        label = self.s.ad_planning.gloss_label
        gloss = lambda text: f" ({label}: {text})" if text else ""
        if i["kind"] == PROVEN_HOOK:
            title = f"WINNING hook ({i['results']} results, Rs {i['cpr_inr']:,.0f} each) on a new body: {i['body']}"
        else:
            title = f"NEW hook, {i['ad_format']} ad"
        if i["thumbnail_text"] == i["spoken_text"]:
            return entry(title, f"Thumbnail and dialogue: {i['spoken_text']}{gloss(i['spoken_hindi'])}", n=i["n"])
        return entry(title, f"Thumbnail: {i['thumbnail_text']}{gloss(i['thumbnail_hindi'])}",
                     f"Dialogue: {i['spoken_text']}{gloss(i['spoken_hindi'])}", n=i["n"])
