"""Reading the ad account: which campaigns this tool manages, the Meta client, ad sets with their insights.

Shared by the timers (metaads.autopause, metaads.budget), the planner (metaads.planning) and the uploader.
"""
from __future__ import annotations

import json
from typing import Iterator, Mapping

from .config import Settings, require_env
from .meta import GraphClient, attribution_window

PAGE_SIZE = "100"


def insights_field(settings: Settings, metrics: str) -> str:
    """The `insights` field of an ad set read: lifetime `metrics`, conversions counted in the click window that new ad
    sets are attributed with (settings.meta.attribution_click_days). With one window requested Meta's `value` of a
    conversion is the count in exactly that window, which is what sum_action reads."""
    return f"insights.date_preset(maximum).action_attribution_windows([\"{attribution_window(settings.meta)}\"]){{{metrics}}}"


def attribution_windows_param(settings: Settings) -> str:
    """The `action_attribution_windows` parameter of an /insights request, the same window as insights_field."""
    return json.dumps([attribution_window(settings.meta)])


def sum_action(actions: list[dict] | None, action_type: str) -> int:
    """Total of one action type in a Graph `actions`/`conversions` list."""
    total = 0.0
    for a in actions or []:
        if a.get("action_type") == action_type:
            try:
                total += float(a.get("value"))
            except (TypeError, ValueError):
                continue
    return round(total)


def campaign_env_var(language_name: str) -> str:
    return f"META_CAMPAIGN_ID_{language_name.upper()}"


def campaign_languages(settings: Settings, environ: Mapping[str, str]) -> dict[str, str]:
    """campaign id -> language name, from META_CAMPAIGN_ID_<LANGUAGE> of every configured language."""
    out = {}
    for name in settings.languages.to_dict().values():
        cid = (environ.get(campaign_env_var(name)) or "").strip()
        if cid:
            out[cid] = name
    return out


def managed_account(settings: Settings, environ: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """(ad account id without 'act_', campaign id -> language name), or RuntimeError when .env lacks either."""
    campaigns = campaign_languages(settings, environ)
    if not campaigns:
        raise RuntimeError("No META_CAMPAIGN_ID_<LANGUAGE> is set in .env")
    account_id = (environ.get("META_AD_ACCOUNT_ID") or "").strip().removeprefix("act_")
    if not account_id:
        raise RuntimeError("META_AD_ACCOUNT_ID is not set in .env")
    return account_id, campaigns


def make_client(settings: Settings) -> GraphClient:
    return GraphClient.from_settings(require_env("META_ACCESS_TOKEN"), settings)


def get_all(client: GraphClient, path: str, **params: str) -> list[dict]:
    """Every row of a Graph list endpoint: one call, more only when it has more than PAGE_SIZE rows."""
    params = {"limit": PAGE_SIZE, **params}
    rows: list[dict] = []
    while True:
        data = client.get(path, **params)
        rows += data.get("data") or []
        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not (paging.get("next") and after):
            return rows
        params["after"] = after


def effective_status_filter(statuses: tuple[str, ...]) -> str:
    return json.dumps([{"field": "effective_status", "operator": "IN", "value": list(statuses)}])


def fetch_adsets(client: GraphClient, account_id: str, fields: str, statuses: tuple[str, ...] = ("ACTIVE",)) -> list[dict]:
    """Account-level ad set read for the given effective statuses."""
    return get_all(client, f"act_{account_id}/adsets", fields=fields, filtering=effective_status_filter(statuses))


def first_insight(row: dict) -> dict:
    """The single nested `insights` row of an ad set (empty when it has no delivery yet)."""
    return ((row.get("insights") or {}).get("data") or [{}])[0]


def managed_rows(rows: list[dict], campaigns: dict[str, str]) -> Iterator[tuple[str, dict, dict]]:
    """(language, ad set row, its insight) for the ad sets of managed campaigns; other campaigns are skipped."""
    for row in rows:
        language = campaigns.get(str(row.get("campaign_id")))
        if language is not None:
            yield language, row, first_insight(row)
