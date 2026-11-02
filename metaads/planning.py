"""How many new ads the rest of the month's budget can afford. Fixed rules only: no LLM. Read-only.

  budget left      = ad_planning.monthly_budget_inr - this month's spend of the managed campaigns
  committed        = what the ACTIVE ad sets will still spend (rest of today and onward): replayed in 2-hour steps
                     with the real pause rule (metaads.rules.evaluate) and raise rule (decide / new_budget), their
                     forward cost per result being the recent one, so a rising CPR gets them paused in the replay
  cost of a new ad = (1-p)*loser_cost + p*winner_cost, p being the smoothed share of ad sets that became winners
  new ads          = (left - committed - reserve) / cost, the reserve being a share of the budget left, spread over the launch days (from the next 06:00 IST start
                     to month end) and split per language into proven-hook ads (a winning hook on a body it has not
                     had) and new-hook ads (a new hook text on the default body, as a video or an image ad: the
                     format that has been winning gets more, each keeps at least ad_planning.min_format_share)

Meta calls per run: three reads (ad sets with lifetime insights, daily insights of the last days, this month's spend).
Run `python -m metaads.planning` for the JSON; the plan_new_ads MCP tool returns the same.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .adaccount import (attribution_windows_param, fetch_adsets, get_all, insights_field, make_client, managed_account,
                        managed_rows, sum_action)
from .bodies import available_bodies
from .config import Settings
from .jobrunner import bootstrap
from .jobs import JobStore
from .meta import GraphClient, IST, default_start_time
from .money import paise_to_inr
from .performance import (AdSet, format_reason, format_stats, hook_outcomes, image_share, is_win,
                          win_rate)
from .rules import decide, evaluate, new_budget
from .schedule import autopause_every, budget_increase_hour

log = logging.getLogger("metaads.planning")

ADSET_STATUSES = ("ACTIVE", "PAUSED")
DAY = timedelta(days=1)


class PlanningError(RuntimeError):
    pass


@dataclass
class Inputs:
    now: datetime
    languages: list[str]                             # names of the managed campaigns' languages
    spent_mtd: float
    adsets: list[AdSet]
    used_pairs: set[tuple[str, str, str, str | None]]     # (language code, spoken text, thumbnail text, body) already made
    bodies: dict[str, list[str]]                     # language code -> available body stems
    video_calls_today: int


@dataclass
class Projection:
    adset: AdSet
    committed: float                # Rs still to be spent
    paused_at: datetime | None      # when the pause rule would stop it; None = runs to month end
    raises: int                     # budget raises the raise rule would make by month end


# ---------------- time ----------------

def launch_day(now: datetime, settings: Settings) -> date:
    """The day new ads start spending: the date of the uploader's next start time."""
    return datetime.fromisoformat(default_start_time(now, settings.meta.start_hour_ist)).date()


def launch_days_left(now: datetime, settings: Settings) -> int:
    """Days from the launch day through the end of the month; 0 when the launch falls in the next month."""
    day, today = launch_day(now, settings), now.astimezone(IST).date()
    if (day.year, day.month) != (today.year, today.month):
        return 0
    return (month_end(now) - datetime(day.year, day.month, day.day, tzinfo=IST)).days


def month_end(now: datetime) -> datetime:
    """Midnight IST that ends the current month."""
    ist = now.astimezone(IST)
    first = datetime(ist.year, ist.month, 1, tzinfo=IST)
    return (first + timedelta(days=32)).replace(day=1)


def next_midnight(t: datetime) -> datetime:
    ist = t.astimezone(IST)
    return datetime(ist.year, ist.month, ist.day, tzinfo=IST) + DAY


# ---------------- forecast of the ad sets already running ----------------

def project(a: AdSet, now: datetime, end: datetime, settings: Settings) -> Projection:
    """Replay `a` from now to `end` in steps of the auto-pause interval, with the pause and raise rules of the timers,
    the raise being checked once a day from settings.schedules.budget_increase_hour_ist."""
    pause_rules, raise_rules = settings.autopause, settings.budget_increase
    step, raise_hour = autopause_every(settings), budget_increase_hour(settings)
    budget = a.daily_budget
    daily = paise_to_inr(budget) if budget else (a.recent_spend or paise_to_inr(settings.meta.daily_budget))
    delivery = min(1.0, a.recent_spend / daily) if a.recent_spend is not None else 1.0    # share of the budget it spends
    if a.recent_spend is None:
        recent_cpr = a.cpr                                                                  # no recent data: lifetime
    else:
        recent_cpr = a.recent_spend / a.recent_results if a.recent_results else None
    spend, results, committed, raises = a.spend, float(a.results), 0.0, 0
    last_raise = a.last_raise
    if evaluate(int(results), spend, pause_rules):
        return Projection(a, 0.0, now, 0)                                                   # the next autopause stops it
    today_end = next_midnight(now)
    today_left = max(0.0, daily * delivery - a.spend_today)
    raise_checked = now.astimezone(IST).date() if now.astimezone(IST).hour >= raise_hour else None
    t = now
    while t < end:
        nxt = min(t + step, end, next_midnight(t))
        if t < today_end:
            step_spend = today_left * ((nxt - t) / (today_end - now))
        else:
            step_spend = daily * delivery * ((nxt - t) / DAY)
        spend, committed, t = spend + step_spend, committed + step_spend, nxt
        if recent_cpr:
            results += step_spend / recent_cpr
        ist = t.astimezone(IST)
        if budget and ist.hour >= raise_hour and ist.date() != raise_checked:
            raise_checked = ist.date()
            if decide(int(results), last_raise, t.timestamp(), raise_rules):
                raised = new_budget(budget, raise_rules)
                if raised > budget:
                    budget, daily, last_raise, raises = raised, paise_to_inr(raised), t.timestamp(), raises + 1
        if evaluate(int(results), spend, pause_rules):
            return Projection(a, committed, t, raises)
    return Projection(a, committed, None, raises)


# ---------------- what a new ad costs ----------------

def cost_of_new_ad(adsets: list[AdSet], launch_days: int, settings: Settings) -> tuple[float, float, int]:
    """(expected cost Rs, win rate, ad sets with a decided outcome). Decided = paused (a loser unless it won first), or a win."""
    cfg = settings.ad_planning
    p, decided = win_rate(adsets, settings)
    losers = [a.spend for a in adsets if not a.active and not is_win(a, settings)]
    loser_cost = sum(losers) / len(losers) if losers else float(cfg.default_loser_spend_inr)
    winner_cost = paise_to_inr(settings.meta.daily_budget) * min(launch_days, cfg.winner_horizon_days)
    return (1 - p) * loser_cost + p * winner_cost, p, decided


# ---------------- who gets the new ads ----------------

def language_rank(settings: Settings) -> Callable[[str], tuple[int, str]]:
    """Sort key for language names: the configured tie_break_order first, then the rest alphabetically."""
    order = []
    for code in settings.ad_planning.tie_break_order:
        if code not in settings.languages:
            raise PlanningError(f"ad_planning.tie_break_order: unknown language code {code!r}")
        order.append(settings.languages[code])
    return lambda name: (order.index(name) if name in order else len(order), name)


def allocate(total: int, weights: dict[str, float], rank: Callable[[str], object] = str) -> dict[str, int]:
    """Split `total` by weight into whole numbers that add up to `total` (largest remainder; ties go by `rank`)."""
    scale = sum(weights.values())
    raw = {k: total * w / scale for k, w in weights.items()}
    out = {k: math.floor(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), rank(k)))[:total - sum(out.values())]:
        out[k] += 1
    return out


def failing_streak(history: list[AdSet], settings: Settings) -> int:
    """Newest-first count of a hook's ad sets that were paused before reaching a win, until its latest win.
    Ad sets still running below the win bar are undecided and neither count nor reset."""
    streak = 0
    for a in reversed(history):
        if is_win(a, settings):
            break
        if not a.active:
            streak += 1
    return streak


def proven_hooks(adsets: list[AdSet], language: str, settings: Settings) -> tuple[list[AdSet], list[AdSet]]:
    """(reusable, retired): the best winning ad set of each hook of a language, lowest cost per result first.
    A hook is retired once its latest `retire_hook_after_failures` decided ad sets in a row all failed."""
    by_hook: dict[tuple[str, str, str], list[AdSet]] = {}
    for a in sorted(adsets, key=lambda a: (a.made, a.id)):
        if a.language == language and a.hook:
            by_hook.setdefault(a.hook, []).append(a)
    reusable, retired = [], []
    for history in by_hook.values():
        wins = [a for a in history if is_win(a, settings)]
        if wins:
            best = min(wins, key=lambda a: (a.cpr, a.id))
            limit = settings.ad_planning.retire_hook_after_failures
            (retired if failing_streak(history, settings) >= limit else reusable).append(best)
    key = lambda a: (a.cpr, a.id)
    return sorted(reusable, key=key), sorted(retired, key=key)


def hook_candidates(winners: list[AdSet], inp: Inputs, default_body: str) -> list[tuple[AdSet, str]]:
    """(winning ad set, body) pairs where that hook has not been made on that body yet."""
    used = {(lang, spoken, thumbnail, body or default_body) for lang, spoken, thumbnail, body in inp.used_pairs}
    return [(w, body) for w in winners for body in inp.bodies.get(w.hook[0], []) if (*w.hook, body) not in used]


def inr(x: float) -> float:
    return round(x, 2)


# ---------------- the plan ----------------

def reserve_pct(now: datetime, settings: Settings) -> float:
    """Share of the budget left that is held back: larger early in the month, smaller in the last week."""
    cfg = settings.ad_planning
    return cfg.late_reserve_pct if now.astimezone(IST).day >= cfg.late_month_from_day else cfg.reserve_pct


def require_monthly_budget(settings: Settings) -> float:
    budget = float(settings.ad_planning.get("monthly_budget_inr") or 0)
    if budget <= 0:
        raise PlanningError("ad_planning.monthly_budget_inr is not set in config/settings.yaml")
    return budget


def build_plan(inp: Inputs, settings: Settings) -> dict:
    cfg = settings.ad_planning
    monthly = require_monthly_budget(settings)
    fresh_daily = paise_to_inr(settings.meta.daily_budget)
    notes: list[str] = []

    remaining = monthly - inp.spent_mtd
    days = launch_days_left(inp.now, settings)
    end = month_end(inp.now)
    projections = [project(a, inp.now, end, settings) for a in inp.adsets if a.active]
    committed = sum(p.committed for p in projections)
    reserve = max(0.0, remaining) * reserve_pct(inp.now, settings) / 100
    free = remaining - committed - reserve
    cost, pooled_win_rate, decided = cost_of_new_ad(inp.adsets, days, settings)

    total = 0
    if days == 0:
        notes.append("The next launch falls in next month, so it is paid from next month's budget: no new ads now.")
    elif free > 0 and cost > 0:
        affordable = free / cost
        total = min(math.ceil(affordable / days), math.floor(affordable), cfg.max_new_per_day)
    if days and total == 0:
        notes.append("The budget left after the running ad sets and the reserve does not cover another ad.")
    if decided < cfg.prior_weight:
        notes.append(f"Only {decided} ad sets have a decided outcome yet, so the win rate leans on the configured "
                     f"prior ({cfg.prior_win_rate:.0%}) and will sharpen as results come in.")

    languages = inp.languages
    hooks = {lang: proven_hooks(inp.adsets, lang, settings) for lang in languages}
    winners = {lang: reusable for lang, (reusable, _) in hooks.items()}
    for lang, (_, retired) in hooks.items():
        for w in retired:
            notes.append(f"{lang}: hook retired, its last {cfg.retire_hook_after_failures} ad sets in a row were paused "
                         f"before {cfg.performing_min_results} results: {w.hook[1]!r}")
    candidates = {lang: hook_candidates(winners[lang], inp, settings.default_body) for lang in languages}
    rank = language_rank(settings)
    shares = allocate(total, {lang: 1 + len(winners[lang]) for lang in languages}, rank)

    order = sorted(languages, key=lambda l: (-shares[l], rank(l)))
    split = {}
    for lang in order:
        proven = min(len(candidates[lang]), round(cfg.exploit_share * shares[lang]))
        split[lang] = (proven, shares[lang] - proven)
    # New hooks come as video or image ads. The format that wins more gets more; both always get a share, so the
    # weaker one keeps being tried. Rounded once over all languages, then spread by who has the most new hooks.
    stats = format_stats(inp.adsets, settings)
    share = image_share(stats, settings)
    fresh_total = sum(fresh for _, fresh in split.values())
    images = (allocate(round(fresh_total * share), {lang: fresh for lang, (_, fresh) in split.items() if fresh}, rank)
              if fresh_total else {})
    video_room = max(0, settings.limits.daily_video_cap - inp.video_calls_today)
    campaigns_out, planned = [], 0
    for lang in order:
        proven, fresh = split[lang]
        image = images.get(lang, 0)
        video = fresh - image
        if video > video_room:                                   # only videos cost a Veo call, and the day's cap is shared
            image, video = image + video - video_room, video_room
            notes.append(f"{lang}: some new hooks became image ads because of the daily video cap "
                         f"({settings.limits.daily_video_cap}).")
        video_room -= video
        picked: dict[str, dict] = {}
        for w, body in candidates[lang][:proven]:
            entry = picked.setdefault(w.id, {"hook_job_id": w.job_id, "spoken_text": w.hook[1],
                                             "thumbnail_text": w.hook[2], "results": w.results,
                                             "cpr_inr": inr(w.cpr), "bodies": []})
            entry["bodies"].append(body)
        planned += proven + fresh
        campaigns_out.append({"language": lang, "new_ads": proven + fresh, "proven_hook_ads": list(picked.values()),
                              "new_hook_ads": fresh, "new_hook_video_ads": video, "new_hook_image_ads": image,
                              "proven_hooks_known": len(winners[lang])})

    paused = sorted((p for p in projections if p.paused_at), key=lambda p: p.paused_at)
    return {
        "as_of": inp.now.astimezone(IST).isoformat(timespec="minutes"),
        "monthly_budget_inr": inr(monthly),
        "spent_this_month_inr": inr(inp.spent_mtd),
        "budget_left_inr": inr(remaining),
        "launch_day": launch_day(inp.now, settings).isoformat(),
        "launch_days_left": days,
        "committed_by_running_adsets_inr": inr(committed),
        "reserve_inr": inr(reserve),
        "free_for_new_ads_inr": inr(free),
        "new_adset_daily_budget_inr": inr(fresh_daily),
        "expected_cost_per_new_ad_inr": inr(cost),
        "win_rate": round(pooled_win_rate, 3),
        "new_ads_total": planned,
        "format_stats": stats,
        "format_reason": format_reason(stats, share),
        "campaigns": campaigns_out,
        "scaling_winners": [{"adset": p.adset.name, "language": p.adset.language, "results": p.adset.results,
                             "cpr_inr": inr(p.adset.cpr) if p.adset.cpr else None, "raises_forecast": p.raises}
                            for p in projections if p.paused_at is None and (p.raises or p.adset.last_raise)],
        "at_risk_adsets": [{"adset": p.adset.name, "language": p.adset.language, "results": p.adset.results,
                            "spend_inr": inr(p.adset.spend), "paused_in_days": round((p.paused_at - inp.now) / DAY, 1)}
                           for p in paused],
        "notes": notes,
    }


# ---------------- reading Meta and the jobs database ----------------

class Planner:
    def __init__(self, settings: Settings, client: GraphClient, account_id: str, campaigns: dict[str, str],
                 store: JobStore, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.s, self.client, self.account_id, self.campaigns, self.store, self.clock = (
            settings, client, account_id, campaigns, store, clock)

    def run(self) -> dict:
        require_monthly_budget(self.s)                               # before any Meta call
        return build_plan(self.collect(self.clock()), self.s)

    def hook_performance(self, language_code: str | None = None) -> dict:
        """Hooks that won and hooks that kept failing, with both texts and numbers (one read of Meta plus the jobs
        database), and how image and video ads compare. For writing new hooks from evidence."""
        adsets = self.collect(self.clock()).adsets
        name = self.s.languages[language_code] if language_code else None
        return {**hook_outcomes(adsets, self.s, name), "format_stats": format_stats(adsets, self.s)}

    def _insights(self, **params: str) -> list[dict]:
        managed = json.dumps([{"field": "campaign.id", "operator": "IN", "value": list(self.campaigns)}])
        return get_all(self.client, f"act_{self.account_id}/insights", filtering=managed, **params)

    def _adset_fields(self) -> str:
        rules = self.s.autopause
        return (f"id,name,campaign_id,effective_status,daily_budget,"
                f"{insights_field(self.s, f'spend,{rules.metric_field}')}")

    def _adset(self, language: str, row: dict, insight: dict, job: dict | None, last_raise: float | None,
               recent_spend: float | None = None, recent_results: float = 0.0, spend_today: float = 0.0) -> AdSet:
        """An ad set from its Meta row, its lifetime insight and the job behind it; the delivery of the last days is
        only known to `collect`."""
        rules = self.s.autopause
        return AdSet(
            id=str(row["id"]), name=row.get("name", ""), language=language, active=row.get("effective_status") == "ACTIVE",
            daily_budget=int(row["daily_budget"]) if str(row.get("daily_budget") or "").isdigit() else None,
            spend=float(insight.get("spend") or 0),
            results=sum_action(insight.get(rules.metric_field), rules.result_action_type),
            recent_spend=recent_spend, recent_results=recent_results, spend_today=spend_today, last_raise=last_raise,
            job_id=job["job_id"] if job else None, hook=(job["language"], job["spoken_text"], job["thumbnail_text"]) if job else None,
            made=job["created"] if job else 0.0, ad_format=job["ad_format"] if job else None,
            body=job["body"] if job else None)

    def reusable_hook_jobs(self) -> set[str]:
        """Jobs whose work folder must be kept: every winning job of a hook that is still reusable (not retired). A new
        ad on a proven hook is a redo of one of them and reuses its image, clip, words and poster plan. One Meta read
        (the ad sets with their lifetime results); nothing else of `collect` is needed."""
        jobs = {j["adset_id"]: j for j in self.store.uploaded_jobs()}
        adsets = [self._adset(language, row, insight, jobs.get(str(row["id"])), None)
                  for language, row, insight in managed_rows(
                      fetch_adsets(self.client, self.account_id, self._adset_fields(), ADSET_STATUSES), self.campaigns)]
        keep: set[str] = set()
        for language in set(self.campaigns.values()):
            reusable_hooks = {a.hook for a in proven_hooks(adsets, language, self.s)[0]}
            keep |= {a.job_id for a in adsets if a.language == language and a.hook in reusable_hooks and a.job_id
                     and is_win(a, self.s)}
        return keep

    def collect(self, now: datetime) -> Inputs:
        rules = self.s.autopause
        today = now.astimezone(IST).date()
        window = {"since": (today - timedelta(days=self.s.ad_planning.trend_days)).isoformat(), "until": today.isoformat()}
        daily: dict[str, list[dict]] = {}
        for r in self._insights(level="adset", time_increment="1", time_range=json.dumps(window),
                                fields=f"adset_id,spend,{rules.metric_field}",
                                action_attribution_windows=attribution_windows_param(self.s)):
            daily.setdefault(str(r["adset_id"]), []).append(r)
        month = self._insights(level="campaign", date_preset="this_month", fields="campaign_id,spend")
        jobs = {j["adset_id"]: j for j in self.store.uploaded_jobs()}
        last_raise = self.store.last_budget_increases()

        adsets = []
        rows = fetch_adsets(self.client, self.account_id, self._adset_fields(), ADSET_STATUSES)
        for language, row, insight in managed_rows(rows, self.campaigns):
            adset_id = str(row["id"])
            full = [r for r in daily.get(adset_id, []) if r["date_start"] < today.isoformat()]
            spend_today = sum(float(r.get("spend") or 0) for r in daily.get(adset_id, []) if r["date_start"] == today.isoformat())
            adsets.append(self._adset(
                language, row, insight, jobs.get(adset_id), last_raise.get(adset_id),
                recent_spend=sum(float(r.get("spend") or 0) for r in full) / len(full) if full else None,
                recent_results=sum(sum_action(r.get(rules.metric_field), rules.result_action_type) for r in full) / len(full)
                if full else 0.0,
                spend_today=spend_today))
        return Inputs(now=now, languages=sorted(set(self.campaigns.values())), spent_mtd=sum(float(r.get("spend") or 0) for r in month), adsets=adsets,
                      used_pairs=self.store.hook_body_pairs(),
                      bodies={code: available_bodies(self.s, code) for code in self.s.languages},
                      video_calls_today=self.store.video_calls_today())


def main() -> int:
    settings = bootstrap()
    try:
        account_id, campaigns = managed_account(settings, os.environ)
        plan = Planner(settings, make_client(settings), account_id, campaigns, JobStore(settings.paths.db)).run()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
