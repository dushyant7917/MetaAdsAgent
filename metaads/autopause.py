"""Every-2-hours job that pauses losing ad sets. Fixed rules only: no Hermes, no LLM.

Meta calls per run: ONE read (every ACTIVE ad set of the account with its lifetime spend and conversions nested in)
and, only when something is flagged and --apply is given, ONE batch write that pauses all flagged ad sets.
Run it with `python -m metaads.autopause` (dry run) or `... --apply` (pauses for real); scripts/install_autopause_timer.sh
schedules it. Only ever pauses, never reactivates.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .config import Settings, load_env_file, load_settings, require_env
from .meta import GraphClient
from .notify import Notifier, TelegramNotifier

log = logging.getLogger("metaads.autopause")

ADSET_FIELDS = "id,name,campaign_id,insights.date_preset(maximum){spend,conversions}"
ACTIVE_ONLY = json.dumps([{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}])
PAGE_SIZE = "100"


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


def evaluate(results: int, spend: float, rules) -> str | None:
    """Reason to pause, or None. Only the band with the highest results bound that matches is considered."""
    if results == 0:
        if spend > rules.zero_results_max_spend:
            return f"results=0 spend={spend:.2f} > {rules.zero_results_max_spend}"
        return None
    cpr = spend / results
    for min_results, max_cpr in sorted(rules.cpr_bands, reverse=True):
        if results >= min_results:
            if cpr > max_cpr:
                return f"results={results} (>={min_results}) cpr={cpr:.2f} > {max_cpr}"
            return None
    return None


def sum_action(actions: list[dict], action_type: str) -> int:
    total = 0.0
    for a in actions or []:
        if a.get("action_type") == action_type:
            try:
                total += float(a.get("value"))
            except (TypeError, ValueError):
                continue
    return round(total)


def campaign_languages(settings: Settings, environ) -> dict[str, str]:
    """campaign id -> language name, from META_CAMPAIGN_ID_<LANGUAGE> of every configured language."""
    out = {}
    for name in settings.languages.to_dict().values():
        cid = (environ.get(f"META_CAMPAIGN_ID_{name.upper()}") or "").strip()
        if cid:
            out[cid] = name
    return out


def fetch_active_adsets(client: GraphClient, account_id: str, fields: str = ADSET_FIELDS) -> list[dict]:
    """One account-level read (more only when over PAGE_SIZE active ad sets)."""
    params = {"fields": fields, "filtering": ACTIVE_ONLY, "limit": PAGE_SIZE}
    rows: list[dict] = []
    while True:
        data = client.get(f"act_{account_id}/adsets", **params)
        rows += data.get("data") or []
        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not (paging.get("next") and after):
            return rows
        params["after"] = after


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
        for row in fetch_active_adsets(self.client, self.account_id):
            language = self.campaigns.get(str(row.get("campaign_id")))
            if language is None:
                continue                                             # a campaign this tool does not manage
            insights = ((row.get("insights") or {}).get("data") or [{}])[0]
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


def ping(url: str, suffix: str = "") -> None:
    """healthchecks.io style heartbeat; a failed ping is logged and never fails the run."""
    if not url:
        return
    for attempt in (1, 2):
        try:
            httpx.get(url.rstrip("/") + suffix, timeout=10).raise_for_status()
            return
        except httpx.HTTPError as e:
            log.warning("heartbeat ping %r failed (attempt %d): %s", suffix or "ok", attempt, e)


def notify(notifier: Notifier, text: str) -> None:
    try:
        notifier.send_report(None, text)
    except Exception as e:                                           # the pausing already happened; just log it
        log.warning("Telegram notice failed: %s", e)


def run_job(settings: Settings, make_client: Callable[[], GraphClient], notifier: Notifier, environ, apply: bool,
            heartbeat: str) -> int:
    ping(heartbeat, "/start")
    try:
        campaigns = campaign_languages(settings, environ)
        if not campaigns:
            raise RuntimeError("No META_CAMPAIGN_ID_<LANGUAGE> is set in .env")
        account_id = (environ.get("META_AD_ACCOUNT_ID") or "").strip().removeprefix("act_")
        if not account_id:
            raise RuntimeError("META_AD_ACCOUNT_ID is not set in .env")
        result = AutoPauser(settings, make_client(), account_id, campaigns).run(apply)
    except Exception as e:
        log.exception("auto-pause failed")
        notify(notifier, f"Auto-pause FAILED: {e}")
        ping(heartbeat, "/fail")
        return 1
    log.info("checked %d active ad sets, flagged %d, paused %d, errors %d",
             result.checked, len(result.flagged), len(result.paused), len(result.errors))
    if result.flagged or result.errors:
        notify(notifier, report_text(result, apply))
    ping(heartbeat, "/fail" if result.errors else "")
    return 1 if result.errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pause losing Meta ad sets by fixed rules.")
    parser.add_argument("--apply", action="store_true", help="really pause (default: dry run, only report)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # its request lines would put the heartbeat URL in the journal

    load_env_file()
    settings = load_settings()
    heartbeat = os.environ.get("HEALTHCHECK_URL_AUTOPAUSE", "").strip()
    lock_path = Path(settings.paths.db).parent / "autopause.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("previous auto-pause run is still going; skipping this one")
            ping(heartbeat, "/fail")
            return 1
        make_client = lambda: GraphClient(require_env("META_ACCESS_TOKEN"), settings.meta.api_version, timeout=120)
        return run_job(settings, make_client, TelegramNotifier.from_env(), os.environ, args.apply, heartbeat)


if __name__ == "__main__":
    sys.exit(main())
