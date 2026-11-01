"""Daily job that raises the budget of ACTIVE ad sets that are doing well. Fixed rules only: no Hermes, no LLM.

An ad set is raised by `increase_pct` when it is ACTIVE and either
  1. this tool last raised it at least `min_hours_between` hours ago, or
  2. this tool never raised it and it has at least `min_results_first_increase` results (learning phase over).
Meta calls per run: ONE read (every ACTIVE ad set of the account with its daily budget and lifetime conversions nested
in) and, only when something is due and --apply is given, ONE batch write. The time of each increase is kept in the
`budget_increases` table of the jobs database (budget changes made by hand in Ads Manager are not seen).
Run it with `python -m metaads.budget` (dry run) or `... --apply` (raises for real); scripts/install_budget_increase_timer.sh
schedules it. Only ever raises, never lowers.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .autopause import campaign_languages, fetch_active_adsets, notify, ping, sum_action
from .config import Settings, load_env_file, load_settings, require_env
from .jobs import JobStore
from .meta import GraphClient
from .notify import Notifier, TelegramNotifier

log = logging.getLogger("metaads.budget")

ADSET_FIELDS = "id,name,campaign_id,daily_budget,insights.date_preset(maximum){conversions}"


@dataclass
class Raise:
    id: str
    name: str
    language: str
    results: int
    old_budget: int                 # paise
    new_budget: int                 # paise
    reason: str


@dataclass
class RunResult:
    checked: int = 0
    due: list[Raise] = field(default_factory=list)
    raised: list[str] = field(default_factory=list)                      # ad set ids Meta confirmed raised
    errors: list[str] = field(default_factory=list)


def decide(results: int, last_increase: float | None, now: float, rules) -> str | None:
    """Reason to raise the budget, or None."""
    if last_increase is not None:
        hours = (now - last_increase) / 3600
        if hours >= rules.min_hours_between:
            return f"{hours:.1f}h since the last increase (>= {rules.min_hours_between}h)"
        return None
    if results >= rules.min_results_first_increase:
        return f"never raised, results={results} (>= {rules.min_results_first_increase})"
    return None


def new_budget(old: int, rules) -> int:
    """`old` raised by increase_pct, capped at max_daily_budget (paise)."""
    return min(round(old * (1 + rules.increase_pct / 100)), int(rules.max_daily_budget))


def raise_op(name: str, adset_id: str, budget: int) -> dict:
    return {"method": "POST", "name": name, "relative_url": adset_id, "body": f"daily_budget={budget}"}


def rupees(paise: int) -> str:
    return f"{paise / 100:,.2f}"


class BudgetRaiser:
    def __init__(self, settings: Settings, client: GraphClient, account_id: str, campaigns: dict[str, str],
                 store: JobStore, clock: Callable[[], float] = time.time):
        self.rules = settings.budget_increase
        self.result_action_type = settings.autopause.result_action_type     # "results" means the same as in autopause
        self.metric_field = settings.autopause.metric_field
        self.client, self.account_id, self.campaigns, self.store, self.clock = client, account_id, campaigns, store, clock

    def run(self, apply: bool) -> RunResult:
        out = RunResult()
        now = self.clock()
        last = self.store.last_budget_increases()
        for row in fetch_active_adsets(self.client, self.account_id, ADSET_FIELDS):
            language = self.campaigns.get(str(row.get("campaign_id")))
            if language is None:
                continue                                             # a campaign this tool does not manage
            out.checked += 1
            adset_id, name = str(row["id"]), row.get("name", "")
            insights = ((row.get("insights") or {}).get("data") or [{}])[0]
            results = sum_action(insights.get(self.metric_field), self.result_action_type)
            reason = decide(results, last.get(adset_id), now, self.rules)
            if not reason:
                continue
            if not str(row.get("daily_budget") or "").isdigit():
                log.warning("%s | %s: due (%s) but it has no daily budget of its own; skipped", language, name, reason)
                continue
            old = int(row["daily_budget"])
            new = new_budget(old, self.rules)
            if new <= old:
                log.info("%s | %s: due (%s) but the budget %s is already at the cap", language, name, reason, rupees(old))
                continue
            log.info("%s | %s: results=%d budget %s -> %s %s: %s", language, name, results, rupees(old), rupees(new),
                     "RAISE" if apply else "DRY would raise", reason)
            out.due.append(Raise(adset_id, name, language, results, old, new, reason))
        if apply and out.due:
            res = self.client.batch([[raise_op(f"raise_{i}", r.id, r.new_budget)] for i, r in enumerate(out.due)])
            for i, r in enumerate(out.due):
                one = res.get(f"raise_{i}")
                if not (one and one.ok):
                    out.errors.append(f"could not raise {r.name} ({r.id}): {one.error if one else 'no result'}")
                    continue
                out.raised.append(r.id)
                try:
                    self.store.record_budget_increase(r.id, r.old_budget, r.new_budget, now)
                except Exception as e:                               # Meta has the new budget; do not hide that
                    out.errors.append(f"raised {r.name} ({r.id}) but could not record it, it may be raised again: {e}")
        return out


def report_text(result: RunResult, apply: bool) -> str:
    mode = "live" if apply else "DRY RUN, nothing changed"
    lines = [f"Budget increase ({mode}): {len(result.due)} of {result.checked} active ad sets due"]
    for r in result.due:
        verb = "RAISED" if r.id in result.raised else ("would raise" if not apply else "NOT raised")
        lines.append(f"{verb}: {r.language} | {r.name} | Rs {rupees(r.old_budget)} -> Rs {rupees(r.new_budget)} | "
                     f"results {r.results} | {r.reason}")
    lines += [f"ERROR: {e}" for e in result.errors]
    return "\n".join(lines)


def run_job(settings: Settings, make_client: Callable[[], GraphClient], notifier: Notifier, environ, store: JobStore,
            apply: bool, heartbeat: str, clock: Callable[[], float] = time.time) -> int:
    ping(heartbeat, "/start")
    try:
        campaigns = campaign_languages(settings, environ)
        if not campaigns:
            raise RuntimeError("No META_CAMPAIGN_ID_<LANGUAGE> is set in .env")
        account_id = (environ.get("META_AD_ACCOUNT_ID") or "").strip().removeprefix("act_")
        if not account_id:
            raise RuntimeError("META_AD_ACCOUNT_ID is not set in .env")
        result = BudgetRaiser(settings, make_client(), account_id, campaigns, store, clock).run(apply)
    except Exception as e:
        log.exception("budget increase failed")
        notify(notifier, f"Budget increase FAILED: {e}")
        ping(heartbeat, "/fail")
        return 1
    log.info("checked %d active ad sets, due %d, raised %d, errors %d",
             result.checked, len(result.due), len(result.raised), len(result.errors))
    if result.due or result.errors:
        notify(notifier, report_text(result, apply))
    ping(heartbeat, "/fail" if result.errors else "")
    return 1 if result.errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Raise the budget of well-performing Meta ad sets by fixed rules.")
    parser.add_argument("--apply", action="store_true", help="really raise budgets (default: dry run, only report)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # its request lines would put the heartbeat URL in the journal

    load_env_file()
    settings = load_settings()
    heartbeat = os.environ.get("HEALTHCHECK_URL_BUDGET_INCREASE", "").strip()
    lock_path = Path(settings.paths.db).parent / "budget.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("previous budget run is still going; skipping this one")
            ping(heartbeat, "/fail")
            return 1
        make_client = lambda: GraphClient(require_env("META_ACCESS_TOKEN"), settings.meta.api_version, timeout=120)
        return run_job(settings, make_client, TelegramNotifier.from_env(), os.environ, JobStore(settings.paths.db),
                       args.apply, heartbeat)


if __name__ == "__main__":
    sys.exit(main())
