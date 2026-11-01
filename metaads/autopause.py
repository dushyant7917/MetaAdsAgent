"""Every-2-hours job that pauses losing ad sets. Fixed rules only: no Hermes, no LLM.

Meta calls per run: ONE read (every ACTIVE ad set of the account with its lifetime spend and conversions nested in)
and, only when something is flagged and --apply is given, ONE batch write that pauses all flagged ad sets.
Run it with `python -m metaads.autopause` (dry run) or `... --apply` (pauses for real); scripts/install_autopause_timer.sh
schedules it. Only ever pauses, never reactivates.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

from . import jobrunner
from .adaccount import fetch_adsets, make_client, managed_account, managed_rows, sum_action
from .config import Settings
from .meta import GraphClient
from .notify import Notifier, TelegramNotifier
from .rules import evaluate

log = logging.getLogger("metaads.autopause")

ADSET_FIELDS = "id,name,campaign_id,insights.date_preset(maximum){spend,conversions}"


@dataclass
class AdSetStat:
    id: str
    name: str
    language: str
    spend: float
    results: int


@dataclass
class RunResult:
    checked: int = 0
    flagged: list[tuple[AdSetStat, str]] = field(default_factory=list)   # (ad set, reason)
    paused: list[str] = field(default_factory=list)                      # ad set ids Meta confirmed paused
    errors: list[str] = field(default_factory=list)


def pause_op(name: str, adset_id: str) -> dict:
    return {"method": "POST", "name": name, "relative_url": adset_id, "body": "status=PAUSED"}


class AutoPauser:
    def __init__(self, settings: Settings, client: GraphClient, account_id: str, campaigns: dict[str, str]):
        self.rules = settings.autopause
        self.client = client
        self.account_id = account_id
        self.campaigns = campaigns

    def run(self, apply: bool) -> RunResult:
        out = RunResult()
        for language, row, insights in managed_rows(fetch_adsets(self.client, self.account_id, ADSET_FIELDS),
                                                    self.campaigns):
            stat = AdSetStat(str(row["id"]), row.get("name", ""), language, float(insights.get("spend") or 0),
                             sum_action(insights.get(self.rules.metric_field), self.rules.result_action_type))
            out.checked += 1
            reason = evaluate(stat.results, stat.spend, self.rules)
            log.info("%s | %s: spend=%.2f results=%d%s", language, stat.name, stat.spend, stat.results,
                     f" -> {'PAUSE' if apply else 'DRY would pause'}: {reason}" if reason else "")
            if reason:
                out.flagged.append((stat, reason))
        if apply and out.flagged:
            results = self.client.batch([[pause_op(f"pause_{i}", s.id)] for i, (s, _) in enumerate(out.flagged)])
            for i, (stat, _) in enumerate(out.flagged):
                res = results.get(f"pause_{i}")
                if res and res.ok:
                    out.paused.append(stat.id)
                else:
                    out.errors.append(f"could not pause {stat.name} ({stat.id}): {res.error if res else 'no result'}")
        return out


def report_text(result: RunResult, apply: bool) -> str:
    mode = "live" if apply else "DRY RUN, nothing paused"
    lines = [f"Auto-pause ({mode}): {len(result.flagged)} of {result.checked} active ad sets flagged"]
    for stat, reason in result.flagged:
        verb = "PAUSED" if stat.id in result.paused else ("would pause" if not apply else "NOT paused")
        lines.append(f"{verb}: {stat.language} | {stat.name} | spend {stat.spend:.2f}, results {stat.results} | {reason}")
    lines += [f"ERROR: {e}" for e in result.errors]
    return "\n".join(lines)


def run_job(settings: Settings, make_client: Callable[[], GraphClient], notifier: Notifier, environ, apply: bool,
            heartbeat: str) -> int:
    def execute() -> RunResult:
        account_id, campaigns = managed_account(settings, environ)
        return AutoPauser(settings, make_client(), account_id, campaigns).run(apply)

    return jobrunner.run_job(
        "Auto-pause", heartbeat, notifier, execute,
        summary=lambda r: f"checked {r.checked} active ad sets, flagged {len(r.flagged)}, paused {len(r.paused)}, "
                          f"errors {len(r.errors)}",
        report=lambda r: report_text(r, apply) if r.flagged or r.errors else None)


def main(argv: list[str] | None = None) -> int:
    return jobrunner.run_cli(
        argv, label="auto-pause", description="Pause losing Meta ad sets by fixed rules.",
        apply_help="really pause (default: dry run, only report)", lock_name="autopause.lock",
        heartbeat_env="HEALTHCHECK_URL_AUTOPAUSE",
        run=lambda settings, apply, heartbeat: run_job(settings, lambda: make_client(settings),
                                                       TelegramNotifier.from_env(), os.environ, apply, heartbeat))


if __name__ == "__main__":
    sys.exit(main())
