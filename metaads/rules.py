"""The fixed pause and raise rules, as pure functions of an ad set's numbers.

metaads.autopause and metaads.budget apply them to Meta, metaads.planning replays them to forecast spend, so the
forecast cannot drift from what the timers actually do.
"""
from __future__ import annotations

from .money import rs


def evaluate(results: int, spend: float, rules) -> str | None:
    """Reason to pause (in plain words, it goes into the alert), or None. Only the band with the highest results bound
    that matches is considered."""
    if results == 0:
        if spend > rules.zero_results_max_spend:
            return f"no results after spending {rs(spend)} (the limit is {rs(rules.zero_results_max_spend)})"
        return None
    cpr = spend / results
    for min_results, max_cpr in sorted(rules.cpr_bands, reverse=True):
        if results >= min_results:
            if cpr > max_cpr:
                return (f"cost per result {rs(cpr)} is above the {rs(max_cpr)} limit for ad sets with "
                        f"{min_results}+ results (this one has {results})")
            return None
    return None


def decide(results: int, last_increase: float | None, now: float, rules) -> str | None:
    """Reason to raise the budget (in plain words, it goes into the alert), or None."""
    if last_increase is not None:
        hours = (now - last_increase) / 3600
        if hours >= rules.min_hours_between:
            return f"{hours:.0f} hours since its last raise (it is raised again after {rules.min_hours_between} hours)"
        return None
    if results >= rules.min_results_first_increase:
        return f"{results} results and never raised before (the first raise is at {rules.min_results_first_increase} results)"
    return None


def new_budget(old: int, rules) -> int:
    """`old` raised by increase_pct, capped at max_daily_budget (paise)."""
    return min(round(old * (1 + rules.increase_pct / 100)), int(rules.max_daily_budget))
