"""How many new ads the rest of the month's budget can afford. Fixed rules only: no LLM. Read-only.

  budget left      = ad_planning.monthly_budget_inr - this month's spend of the managed campaigns
  committed        = what the ACTIVE ad sets will still spend (rest of today and onward): replayed in 2-hour steps
                     with the real pause rule (metaads.rules.evaluate) and raise rule (decide / new_budget), their
                     forward cost per result being the recent one, so a rising CPR gets them paused in the replay
  cost of a new ad = (1-p)*loser_cost + p*winner_cost, p being the smoothed share of ad sets that became winners
  new ads          = (left - committed - reserve) / cost, the reserve being a share of the budget left, spread over the launch days (from the next 06:00 IST start
                     to month end) and split per language into proven-hook ads (a winning hook on a body it has not
                     had) and new-hook ads (a new hook text on the default body)

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

from .adaccount import fetch_adsets, get_all, make_client, managed_account, managed_rows, sum_action
from .bodies import available_bodies
from .config import Settings
from .jobrunner import bootstrap
from .jobs import JobStore
from .meta import GraphClient, IST, default_start_time
from .money import paise_to_inr
from .rules import decide, evaluate, new_budget

log = logging.getLogger("metaads.planning")

ADSET_STATUSES = ("ACTIVE", "PAUSED")
STEP = timedelta(hours=2)           # metaads.autopause runs every 2 hours
RAISE_HOUR_IST = 5                  # metaads.budget runs daily at 5:00 IST
DAY = timedelta(days=1)


class PlanningError(RuntimeError):
    pass


@dataclass
class AdSet:
    id: str
    name: str
    language: str                   # language name, e.g. Hindi
    active: bool
    daily_budget: int | None        # paise; None when the budget belongs to the campaign
    spend: float                    # lifetime, Rs
    results: int                    # lifetime
    recent_spend: float | None      # Rs per day over the last full days it delivered; None without recent delivery
    recent_results: float           # per day, same days
    spend_today: float
    last_raise: float | None        # epoch of the last increase by metaads.budget
    job_id: str | None              # the job whose ad this is, when this tool made it
    hook: tuple[str, str] | None    # (language code, hook text)
    made: float = 0.0               # epoch the ad was made, to order a hook's ad sets

    @property
    def cpr(self) -> float | None:
        return self.spend / self.results if self.results else None


@dataclass
class Inputs:
    now: datetime
    languages: list[str]                             # names of the managed campaigns' languages
    spent_mtd: float
    adsets: list[AdSet]
    used_pairs: set[tuple[str, str, str | None]]     # (language code, hook text, body) already made
    bodies: dict[str, list[str]]                     # language code -> available body stems
    video_calls_today: int


@dataclass
class Projection:
    adset: AdSet
    committed: float                # Rs still to be spent
    paused_at: datetime | None      # when the pause rule would stop it; None = runs to month end
    raises: int                     # budget raises the raise rule would make by month end


# ---------------- time ----------------

def launch_day(now: datetime) -> date:
    """The day new ads start spending: the date of the uploader's next start time."""
    return datetime.fromisoformat(default_start_time(now)).date()


def launch_days_left(now: datetime) -> int:
    """Days from the launch day through the end of the month; 0 when the launch falls in the next month."""
    day, today = launch_day(now), now.astimezone(IST).date()
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
    """Replay `a` from now to `end` in 2-hour steps with the pause and raise rules of the timers."""
    pause_rules, raise_rules = settings.autopause, settings.budget_increase
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
    raise_checked = now.astimezone(IST).date() if now.astimezone(IST).hour >= RAISE_HOUR_IST else None
    t = now
    while t < end:
        nxt = min(t + STEP, end, next_midnight(t))
        if t < today_end:
            step_spend = today_left * ((nxt - t) / (today_end - now))
        else:
            step_spend = daily * delivery * ((nxt - t) / DAY)
        spend, committed, t = spend + step_spend, committed + step_spend, nxt
        if recent_cpr:
            results += step_spend / recent_cpr
        ist = t.astimezone(IST)
        if budget and ist.hour >= RAISE_HOUR_IST and ist.date() != raise_checked:
            raise_checked = ist.date()
            if decide(int(results), last_raise, t.timestamp(), raise_rules):
                raised = new_budget(budget, raise_rules)
                if raised > budget:
                    budget, daily, last_raise, raises = raised, paise_to_inr(raised), t.timestamp(), raises + 1
        if evaluate(int(results), spend, pause_rules):
            return Projection(a, committed, t, raises)
    return Projection(a, committed, None, raises)


# ---------------- what a new ad costs ----------------

def is_win(a: AdSet, settings: Settings) -> bool:
    """Reached performing_min_results. Autopause checks every 2 hours, so an ad set that got that far was never
    stopped on the way; pausing it later (fatigue, a worse CPR) does not make it any less of a winner."""
    return a.results >= settings.ad_planning.performing_min_results


def cost_of_new_ad(adsets: list[AdSet], launch_days: int, settings: Settings) -> tuple[float, float, int]:
    """(expected cost Rs, win rate, ad sets with a decided outcome). Decided = paused (a loser unless it won first), or a win."""
    cfg = settings.ad_planning
    decided = [a for a in adsets if not a.active or is_win(a, settings)]
    wins = sum(1 for a in decided if is_win(a, settings))
    p = (wins + cfg.prior_win_rate * cfg.prior_weight) / (len(decided) + cfg.prior_weight)
    losers = [a.spend for a in adsets if not a.active and not is_win(a, settings)]
    loser_cost = sum(losers) / len(losers) if losers else float(cfg.default_loser_spend_inr)
    winner_cost = paise_to_inr(settings.meta.daily_budget) * min(launch_days, cfg.winner_horizon_days)
    return (1 - p) * loser_cost + p * winner_cost, p, len(decided)


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
    by_hook: dict[tuple[str, str], list[AdSet]] = {}
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
    used = {(lang, text, body or default_body) for lang, text, body in inp.used_pairs}
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
    days = launch_days_left(inp.now)
    end = month_end(inp.now)
    projections = [project(a, inp.now, end, settings) for a in inp.adsets if a.active]
    committed = sum(p.committed for p in projections)
    reserve = max(0.0, remaining) * reserve_pct(inp.now, settings) / 100
    free = remaining - committed - reserve
    cost, win_rate, decided = cost_of_new_ad(inp.adsets, days, settings)

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

    video_room = max(0, settings.limits.daily_video_cap - inp.video_calls_today)
    campaigns_out, planned = [], 0
    for lang in sorted(languages, key=lambda l: (-shares[l], rank(l))):
        n = shares[lang]
        proven = min(len(candidates[lang]), round(cfg.exploit_share * n))
        fresh = n - proven
        if fresh > video_room:                                   # each new hook costs a video; the day's cap is shared
            short, fresh = fresh - video_room, video_room
            proven += min(short, len(candidates[lang]) - proven)
            notes.append(f"{lang}: capped by the daily video cap ({settings.limits.daily_video_cap}).")
        video_room -= fresh
        picked: dict[str, dict] = {}
        for w, body in candidates[lang][:proven]:
            entry = picked.setdefault(w.id, {"hook_job_id": w.job_id, "hook_text": w.hook[1], "results": w.results,
                                             "cpr_inr": inr(w.cpr), "bodies": []})
            entry["bodies"].append(body)
        planned += proven + fresh
        campaigns_out.append({"language": lang, "new_ads": proven + fresh, "proven_hook_ads": list(picked.values()),
                              "new_hook_ads": fresh, "proven_hooks_known": len(winners[lang])})

    paused = sorted((p for p in projections if p.paused_at), key=lambda p: p.paused_at)
    return {
        "as_of": inp.now.astimezone(IST).isoformat(timespec="minutes"),
        "monthly_budget_inr": inr(monthly),
        "spent_this_month_inr": inr(inp.spent_mtd),
        "budget_left_inr": inr(remaining),
        "launch_day": launch_day(inp.now).isoformat(),
        "launch_days_left": days,
        "committed_by_running_adsets_inr": inr(committed),
        "reserve_inr": inr(reserve),
        "free_for_new_ads_inr": inr(free),
        "new_adset_daily_budget_inr": inr(fresh_daily),
        "expected_cost_per_new_ad_inr": inr(cost),
        "win_rate": round(win_rate, 3),
        "new_ads_total": planned,
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

    def _insights(self, **params: str) -> list[dict]:
        managed = json.dumps([{"field": "campaign.id", "operator": "IN", "value": list(self.campaigns)}])
        return get_all(self.client, f"act_{self.account_id}/insights", filtering=managed, **params)

    def collect(self, now: datetime) -> Inputs:
        rules, cfg = self.s.autopause, self.s.ad_planning
        adset_fields = (f"id,name,campaign_id,effective_status,daily_budget,"
                        f"insights.date_preset(maximum){{spend,{rules.metric_field}}}")
        today = now.astimezone(IST).date()
        window = {"since": (today - timedelta(days=cfg.trend_days)).isoformat(), "until": today.isoformat()}
        daily: dict[str, list[dict]] = {}
        for r in self._insights(level="adset", time_increment="1", time_range=json.dumps(window),
                                fields=f"adset_id,spend,{rules.metric_field}"):
            daily.setdefault(str(r["adset_id"]), []).append(r)
        month = self._insights(level="campaign", date_preset="this_month", fields="campaign_id,spend")
        jobs = {j["adset_id"]: j for j in self.store.uploaded_jobs()}
        last_raise = self.store.last_budget_increases()

        adsets = []
        rows = fetch_adsets(self.client, self.account_id, adset_fields, ADSET_STATUSES)
        for language, row, insight in managed_rows(rows, self.campaigns):
            adset_id = str(row["id"])
            full = [r for r in daily.get(adset_id, []) if r["date_start"] < today.isoformat()]
            spend_today = sum(float(r.get("spend") or 0) for r in daily.get(adset_id, []) if r["date_start"] == today.isoformat())
            job = jobs.get(adset_id)
            adsets.append(AdSet(
                id=adset_id, name=row.get("name", ""), language=language, active=row.get("effective_status") == "ACTIVE",
                daily_budget=int(row["daily_budget"]) if str(row.get("daily_budget") or "").isdigit() else None,
                spend=float(insight.get("spend") or 0),
                results=sum_action(insight.get(rules.metric_field), rules.result_action_type),
                recent_spend=sum(float(r.get("spend") or 0) for r in full) / len(full) if full else None,
                recent_results=sum(sum_action(r.get(rules.metric_field), rules.result_action_type) for r in full) / len(full)
                if full else 0.0,
                spend_today=spend_today, last_raise=last_raise.get(adset_id),
                job_id=job["job_id"] if job else None, hook=(job["language"], job["text"]) if job else None,
                made=job["created"] if job else 0.0))
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
