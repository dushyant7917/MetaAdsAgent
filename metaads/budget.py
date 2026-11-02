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

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import jobrunner
from .adaccount import fetch_adsets, insights_field, make_client, managed_account, managed_rows, sum_action
from .config import Settings
from .jobs import JobStore
from .messages import RAISED, WARN, blocks, entry, headline
from .meta import GraphClient
from .money import rupees
from .notify import Notifier, TelegramNotifier
from .rules import decide, new_budget

log = logging.getLogger("metaads.budget")



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


def raise_op(name: str, adset_id: str, budget: int) -> dict:
    return {"method": "POST", "name": name, "relative_url": adset_id, "body": f"daily_budget={budget}"}


class BudgetRaiser:
    def __init__(self, settings: Settings, client: GraphClient, account_id: str, campaigns: dict[str, str],
                 store: JobStore, clock: Callable[[], float] = time.time):
        self.rules = settings.budget_increase
        self.result_action_type = settings.autopause.result_action_type     # "results" means the same as in autopause
        self.metric_field = settings.autopause.metric_field
        self.adset_fields = f"id,name,campaign_id,daily_budget,{insights_field(settings, self.metric_field)}"
        self.client, self.account_id, self.campaigns, self.store, self.clock = client, account_id, campaigns, store, clock

    def run(self, apply: bool) -> RunResult:
        out = RunResult()
        now = self.clock()
        last = self.store.last_budget_increases()
        for language, row, insights in managed_rows(fetch_adsets(self.client, self.account_id, self.adset_fields),
                                                    self.campaigns):
            out.checked += 1
            adset_id, name = str(row["id"]), row.get("name", "")
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


def report_text(result: RunResult, apply: bool) -> str | None:
    """The alert: which ad sets got a higher budget and why. Nothing raised and nothing failed: no alert. A dry run
    (nothing is ever raised) reports what it would raise, so trying the job out shows something."""
    shown = [r for r in result.due if r.id in result.raised] if apply else result.due
    if not shown and not result.errors:
        return None
    count = f"{len(shown)} ad set{'s' if len(shown) != 1 else ''}"
    head = (headline(RAISED, f"Budget increase: {count} raised") if apply
            else headline(RAISED, f"Budget increase DRY RUN: {count} would be raised (nothing was changed)"))
    items = [entry(f"{r.language} · {r.name}", f"Daily budget: Rs {rupees(r.old_budget)} → Rs {rupees(r.new_budget)}",
                   f"Why: {r.reason}", f"Results so far: {r.results}", n=i) for i, r in enumerate(shown, 1)]
    problems = [headline(WARN, e) for e in result.errors]
    return blocks(head if shown else None, *items, *problems)


def run_job(settings: Settings, make_client: Callable[[], GraphClient], notifier: Notifier, environ, store: JobStore,
            apply: bool, heartbeat: str, clock: Callable[[], float] = time.time) -> int:
    def execute() -> RunResult:
        account_id, campaigns = managed_account(settings, environ)
        return BudgetRaiser(settings, make_client(), account_id, campaigns, store, clock).run(apply)

    return jobrunner.run_job(
        "Budget increase", heartbeat, notifier, execute,
        summary=lambda r: f"checked {r.checked} active ad sets, due {len(r.due)}, raised {len(r.raised)}, "
                          f"errors {len(r.errors)}",
        report=lambda r: report_text(r, apply))


def main(argv: list[str] | None = None) -> int:
    return jobrunner.run_cli(
        argv, label="budget", description="Raise the budget of well-performing Meta ad sets by fixed rules.",
        apply_help="really raise budgets (default: dry run, only report)", lock_name="budget.lock",
        heartbeat_env="HEALTHCHECK_URL_BUDGET_INCREASE",
        run=lambda settings, apply, heartbeat: run_job(settings, lambda: make_client(settings),
                                                       TelegramNotifier.from_env(), os.environ,
                                                       JobStore(settings.paths.db), apply, heartbeat))


if __name__ == "__main__":
    sys.exit(main())
