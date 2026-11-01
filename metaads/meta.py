"""Meta Graph API client and the ad payload builders. Job logic lives in uploader.py.

Few calls on purpose: all images in one multi-file request, all video statuses in one `?ids=` read, and every
ad set + ad (creative inline) of a request in one Graph batch.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

log = logging.getLogger("metaads.meta")

RATE_LIMIT_CODES = {4, 17, 32, 613}     # app, account, page and custom throttling: worth waiting for
BATCH_MAX_OPS = 50
IST = timezone(timedelta(hours=5, minutes=30))
START_HOUR_IST = 6                      # ad sets start at the next 6:00 IST unless the owner names a time


class GraphError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _graph_error(resp: httpx.Response, data: Any) -> GraphError:
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return GraphError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    msg = err.get("error_user_msg") or err.get("message") or "unknown error"
    sub = f"/{err['error_subcode']}" if err.get("error_subcode") else ""
    return GraphError(f"{msg} (code {err.get('code')}{sub})", err.get("code"))


@dataclass
class BatchResult:
    ok: bool
    id: str | None = None
    error: str | None = None


class GraphClient:
    def __init__(self, token: str, version: str, timeout: float = 300, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3, backoff: float = 30):
        self.http = httpx.Client(timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {token}"})
        self.base = f"https://graph.facebook.com/{version}"
        self.video_base = f"https://graph-video.facebook.com/{version}"
        self.sleep, self.max_attempts, self.backoff = sleep, max_attempts, backoff

    def _call(self, send: Callable[[], httpx.Response]) -> Any:
        """Run one request; wait and retry only on rate-limit errors (anything else will not get better)."""
        for attempt in range(1, self.max_attempts + 1):
            resp = send()
            try:
                data = resp.json()
            except ValueError:
                data = None
            if resp.is_success:
                return data if data is not None else {}
            err = _graph_error(resp, data)
            if err.code in RATE_LIMIT_CODES and attempt < self.max_attempts:
                wait = self.backoff * attempt
                log.warning("rate limited (attempt %d/%d): %s; waiting %ss", attempt, self.max_attempts, err, wait)
                self.sleep(wait)
                continue
            raise err
        raise AssertionError("unreachable")

    def get(self, path: str, **params: str) -> Any:
        return self._call(lambda: self.http.get(f"{self.base}/{path}", params=params))

    def upload_video(self, ad_account_id: str, path: str | Path, title: str) -> str:
        """One multipart call, streamed from disk. Returns the video id."""
        def send() -> httpx.Response:
            with open(path, "rb") as f:
                return self.http.post(f"{self.video_base}/act_{ad_account_id}/advideos", data={"title": title},
                                      files={"source": (Path(path).name, f, "video/mp4")})
        vid = self._call(send).get("id")
        if not vid:
            raise GraphError("Meta returned no video id")
        return str(vid)

    def upload_images(self, ad_account_id: str, files: dict[str, Path]) -> dict[str, str]:
        """ALL images in one request. Meta wants the form field name to equal the file name. Returns name -> hash."""
        def send() -> httpx.Response:
            handles = {name: open(p, "rb") for name, p in files.items()}
            try:
                return self.http.post(f"{self.base}/act_{ad_account_id}/adimages",
                                      files={name: (name, h, "application/octet-stream") for name, h in handles.items()})
            finally:
                for h in handles.values():
                    h.close()
        images = self._call(send).get("images") or {}
        return {name: info["hash"] for name, info in images.items() if info.get("hash")}

    def video_statuses(self, video_ids: list[str]) -> dict[str, str]:
        """video_status of every id in ONE call: id -> processing / ready / error."""
        data = self.get("", ids=",".join(video_ids), fields="status")
        return {vid: ((data.get(vid) or {}).get("status") or {}).get("video_status", "unknown") for vid in video_ids}

    def batch(self, groups: list[list[dict[str, Any]]]) -> dict[str, BatchResult]:
        """Send op groups in as few batch calls as possible (max 50 ops each). A group is never split, so an op
        can depend on an earlier op of its own group. Every op needs a unique `name`. Returns name -> result."""
        packed: list[list[dict[str, Any]]] = [[]]
        for group in groups:
            if packed[-1] and len(packed[-1]) + len(group) > BATCH_MAX_OPS:
                packed.append([])
            packed[-1].extend(group)
        out: dict[str, BatchResult] = {}
        for ops in packed:
            if not ops:
                continue
            data = self._call(lambda: self.http.post(self.base + "/", data={"batch": json.dumps(ops)}))
            for op, item in zip(ops, data):
                out[op["name"]] = _batch_result(item)
        return out

    def search_region(self, country_code: str, name: str) -> str:
        data = self.get("search", type="adgeolocation", location_types='["region"]', q=name)
        for d in data.get("data", []):
            if d.get("country_code", "").upper() == country_code.upper() and d.get("name", "").lower() == name.lower():
                return str(d["key"])
        raise GraphError(f"No region match for {name!r} in {country_code}")


def _batch_result(item: dict[str, Any] | None) -> BatchResult:
    if item is None:
        return BatchResult(False, error="no response (the batch timed out before this step)")
    try:
        body = json.loads(item.get("body") or "{}")
    except ValueError:
        body = {}
    if item.get("code") == 200 and (body.get("id") or body.get("success")):      # an update answers {"success": true}
        return BatchResult(True, id=str(body["id"]) if body.get("id") else None)
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return BatchResult(False, error=f"{err.get('error_user_msg') or err.get('message')} (code {err.get('code')})")
    return BatchResult(False, error=f"HTTP {item.get('code')}: {str(item.get('body'))[:300]}")


# ---------------- region keys (cached forever: they never change) ----------------

def resolve_region_keys(client: GraphClient | None, cache_path: str | Path, country: str,
                        states: list[str]) -> list[str | None]:
    """Meta region key per state name. Only states missing from the cache cost a call; with client=None
    (dry run) missing ones come back as None."""
    path = Path(cache_path)
    cache: dict[str, str] = json.loads(path.read_text()) if path.is_file() else {}
    keys, dirty = [], False
    for state in states:
        ck = f"{country}|{state}"
        if ck not in cache and client is not None:
            cache[ck] = client.search_region(country, state)
            dirty = True
        keys.append(cache.get(ck))
    if dirty:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return keys


# ---------------- payload builders ----------------

def default_start_time(now: datetime) -> str:
    """The next 6:00 IST: today if it is not 6am yet, otherwise tomorrow."""
    ist = now.astimezone(IST)
    day = ist.date() + timedelta(days=0 if ist.hour < START_HOUR_IST else 1)
    return datetime(day.year, day.month, day.day, START_HOUR_IST, tzinfo=IST).isoformat()


def parse_start_time(value: str) -> str:
    """An owner-chosen start time as ISO 8601; a time without an offset is read as IST."""
    try:
        t = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"start_time {value!r} is not an ISO 8601 time, e.g. 2026-11-03T09:00") from None
    return (t if t.tzinfo else t.replace(tzinfo=IST)).isoformat(timespec="seconds")


def _preview(text: str, limit: int = 40) -> str:
    text = re.sub(r"[|\s]+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,.;:!?") + "…"


def ad_set_name(language: str, day: str, job_id: str) -> str:
    return f"{language} | {day} | {job_id}"


def ad_name(language: str, body: str | None, text: str, job_id: str) -> str:
    return f"{language} | {body or '-'} | {_preview(text)} | {job_id}"


def build_targeting(cfg: Any, region_keys: list[str | None]) -> dict[str, Any]:
    """Android phones, Facebook + Instagram, the language's states (or the whole country when it has none)."""
    lo, hi = str(cfg.android_os_min or ""), str(cfg.android_os_max or "")
    if lo and hi:
        user_os = f"Android_ver_{lo}_to_{hi}"
    elif lo:
        user_os = f"Android_ver_{lo}_and_above"
    else:
        user_os = "Android"
    geo = {"regions": [{"key": k} for k in region_keys]} if region_keys else {"countries": [cfg.country]}
    return {"publisher_platforms": ["facebook", "instagram"], "device_platforms": ["mobile"], "user_os": [user_os],
            "user_device": ["Android_Smartphone"], "geo_locations": geo, "genders": list(cfg.genders),
            "age_min": cfg.age_min, "targeting_automation": {"advantage_audience": 0}}


def build_adset_fields(cfg: Any, name: str, campaign_id: str, start_time: str, targeting: dict[str, Any],
                       app_id: str, store_url: str) -> dict[str, str]:
    return {
        "name": name, "campaign_id": campaign_id, "daily_budget": str(cfg.daily_budget),
        "billing_event": "IMPRESSIONS", "optimization_goal": "OFFSITE_CONVERSIONS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "start_time": start_time, "status": "ACTIVE",
        "destination_type": "APP",
        "promoted_object": json.dumps({"application_id": app_id, "object_store_url": store_url,
                                       "custom_event_type": cfg.conversion_event}),
        "targeting": json.dumps(targeting),
    }


def build_creative(cfg: Any, name: str, page_id: str, ig_user_id: str, video_id: str, image_hash: str,
                   text: str, store_url: str) -> dict[str, Any]:
    """Single-video creative, sent inline with the ad (no separate /adcreatives call)."""
    spec: dict[str, Any] = {"page_id": page_id, "video_data": {
        "video_id": video_id, "image_hash": image_hash, "message": text, "title": text,
        "call_to_action": {"type": cfg.cta, "value": {"link": store_url}}}}
    if ig_user_id:
        spec["instagram_user_id"] = ig_user_id
    return {"name": name, "object_story_spec": spec}


def adset_op(name: str, ad_account_id: str, fields: dict[str, str]) -> dict[str, Any]:
    return {"method": "POST", "name": name, "relative_url": f"act_{ad_account_id}/adsets",
            "body": urlencode(fields), "omit_response_on_success": False}


def ad_op(name: str, ad_account_id: str, ad_name_: str, creative: dict[str, Any],
          adset_id: str | None, adset_op_name: str) -> dict[str, Any]:
    """The ad refers to its ad set by the id the same batch just created (Graph batch JSONPath reference),
    or by a literal id when the ad set already exists from an earlier, partly failed run."""
    body = urlencode({"name": ad_name_, "creative": json.dumps(creative), "status": "ACTIVE"})
    body += f"&adset_id={adset_id}" if adset_id else f"&adset_id={{result={adset_op_name}:$.id}}"
    return {"method": "POST", "name": name, "relative_url": f"act_{ad_account_id}/ads",
            "body": body, "omit_response_on_success": False}
