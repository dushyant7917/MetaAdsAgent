"""The fixed pause and raise rules, as pure functions of an ad set's numbers.

metaads.autopause and metaads.budget apply them to Meta, metaads.planning replays them to forecast spend, so the
forecast cannot drift from what the timers actually do.
"""
from __future__ import annotations


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
