import json
from urllib.parse import parse_qs

import httpx
import pytest

from metaads import jobrunner
from metaads.budget import run_job
from metaads.config import load_settings
from metaads.jobs import JobStore
from metaads.meta import GraphClient
from metaads.rules import decide, new_budget

S = load_settings()
RULES = S.budget_increase
ENV = {"META_AD_ACCOUNT_ID": "act_111", "META_CAMPAIGN_ID_HINDI": "c-hi", "META_CAMPAIGN_ID_TAMIL": "c-ta"}
NOW = 1_800_000_000.0
HOUR = 3600


def ago(hours):
    return NOW - hours * HOUR


@pytest.mark.parametrize("results,last,due", [
    (0, ago(70), True), (0, ago(69.9), False),                 # raised before: only the 70 hours count, not results
    (500, ago(69.9), False), (500, ago(200), True),
    (50, None, True), (49, None, False), (0, None, False),     # never raised: 50 results
])
def test_when_a_budget_is_due(results, last, due):
    assert (decide(results, last, NOW, RULES) is not None) is due


@pytest.mark.parametrize("old,expected", [
    (50000, 60000), (60000, 72000), (33333, 40000), (33334, 40001),   # +20%, rounded to whole paise
    (450000, 500000), (480000, 500000),                               # clamped to max_daily_budget
    (500000, 500000), (600000, 500000),                               # at or over the cap: never above it
])
def test_new_budget_is_capped(old, expected):
    assert new_budget(old, RULES) == expected


def row(adset_id, campaign, budget_paise, results, name=None):
    conv = [{"action_type": "start_trial_total", "value": str(results)}] if results else []
    out = {"id": adset_id, "name": name or f"as {adset_id}", "campaign_id": campaign,
           "insights": {"data": [{"conversions": conv}]}}
    if budget_paise is not None:
        out["daily_budget"] = str(budget_paise)
    return out


class FakeMeta:
    def __init__(self, pages, fail=(), fail_read=False):
        self.pages, self.fail, self.fail_read = pages, set(fail), fail_read
        self.reads: list[dict] = []
        self.batches: list[list[dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            if self.fail_read:
                return httpx.Response(400, json={"error": {"message": "token expired", "code": 190}})
            self.reads.append({"path": req.url.path, **dict(req.url.params)})
            after = req.url.params.get("after")
            i = int(after) if after else 0
            paging = {"next": "https://x", "cursors": {"after": str(i + 1)}} if i + 1 < len(self.pages) else {}
            return httpx.Response(200, json={"data": self.pages[i], "paging": paging})
        ops = json.loads(parse_qs(req.read().decode())["batch"][0])
        self.batches.append(ops)
        out = []
        for op in ops:
            if op["relative_url"] in self.fail:
                out.append({"code": 400, "body": json.dumps({"error": {"message": "nope", "code": 100}})})
            else:
                out.append({"code": 200, "body": json.dumps({"success": True})})
        return httpx.Response(200, json=out)


class Notes:
    def __init__(self):
        self.texts: list[str] = []

    def send_report(self, chat_id, text):
        self.texts.append(text)


@pytest.fixture
def pings(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(jobrunner, "ping", lambda url, suffix="": calls.append(suffix))
    return calls


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "t.db")


def go(fake, store, apply, env=ENV, now=NOW):
    notes = Notes()
    client = lambda: GraphClient.from_settings("tok", load_settings(), transport=httpx.MockTransport(fake), sleep=lambda x: None, backoff=0)
    return run_job(S, client, notes, env, store, apply, "https://hc-ping.com/x", clock=lambda: now), notes


def test_one_read_and_one_batch_raise_only_the_due_ones(pings, store):
    store.record_budget_increase("3", 50000, 60000, ago(80))       # raised 80 h ago: due again
    store.record_budget_increase("4", 50000, 60000, ago(10))       # raised 10 h ago: not due
    fake = FakeMeta([[row("1", "c-hi", 50000, 50),                 # never raised, 50 results: due
                      row("2", "c-hi", 50000, 49),                 # never raised, 49 results: not yet
                      row("3", "c-ta", 60000, 0),                  # due by time alone
                      row("4", "c-ta", 60000, 99),
                      row("5", "c-other", 50000, 99),              # a campaign not in .env: ignored
                      row("6", "c-hi", None, 99),                  # no daily budget of its own: skipped
                      row("7", "c-hi", 500000, 99)]])              # already at the cap: skipped
    code, notes = go(fake, store, apply=True)
    assert code == 0 and len(fake.reads) == 1 and len(fake.batches) == 1
    assert fake.reads[0]["path"].endswith("/act_111/adsets")
    assert json.loads(fake.reads[0]["filtering"]) == [{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}]
    assert "daily_budget" in fake.reads[0]["fields"] and "date_preset(maximum)" in fake.reads[0]["fields"]
    assert 'action_attribution_windows(["7d_click"])' in fake.reads[0]["fields"]            # the same click window as the ad sets
    assert [(op["relative_url"], op["body"]) for op in fake.batches[0]] == [("1", "daily_budget=60000"),
                                                                            ("3", "daily_budget=72000")]
    assert pings == ["/start", ""]
    text = notes.texts[0]
    assert text.startswith("📈 Budget increase: 2 ad sets raised\n\n1. Hindi · as 1\n   Daily budget: Rs 500.00 → Rs 600.00\n"
                           "   Why: 50 results and never raised before")
    assert "\n\n2. " in text and "hours since its last raise" in text and "as 2" not in text.split("\n\n2. ")[0]
    last = store.last_budget_increases()
    assert last["1"] == NOW and last["3"] == NOW and last["4"] == ago(10) and "2" not in last


def test_a_second_run_right_after_does_nothing(pings, store):
    pages = [[row("1", "c-hi", 50000, 80)]]
    go(FakeMeta(pages), store, apply=True)
    fake = FakeMeta([[row("1", "c-hi", 60000, 80)]])
    code, notes = go(fake, store, apply=True, now=NOW + 48 * HOUR)
    assert code == 0 and fake.batches == [] and notes.texts == []
    fake = FakeMeta([[row("1", "c-hi", 60000, 80)]])
    go(fake, store, apply=True, now=NOW + 72 * HOUR)               # the daily run 3 days later
    assert [op["body"] for op in fake.batches[0]] == ["daily_budget=72000"]


def test_budget_is_clamped_to_the_cap(pings, store):
    fake = FakeMeta([[row("1", "c-hi", 450000, 80)]])
    go(fake, store, apply=True)
    assert fake.batches[0][0]["body"] == "daily_budget=500000"


def test_dry_run_reads_once_and_never_writes_or_records(pings, store):
    fake = FakeMeta([[row("1", "c-hi", 50000, 80)]])
    code, notes = go(fake, store, apply=False)
    assert code == 0 and len(fake.reads) == 1 and fake.batches == [] and store.last_budget_increases() == {}
    assert "DRY RUN: 1 ad set would be raised" in notes.texts[0] and "1. Hindi · as 1" in notes.texts[0]


def test_nothing_due_means_no_write_and_no_telegram(pings, store):
    fake = FakeMeta([[row("1", "c-hi", 50000, 10)]])
    code, notes = go(fake, store, apply=True)
    assert code == 0 and fake.batches == [] and notes.texts == [] and pings == ["/start", ""]


def test_results_use_the_configured_action_type_only(pings, store):
    r = row("1", "c-hi", 50000, 80)
    r["insights"]["data"][0]["conversions"][0]["action_type"] = "link_click_total"
    fake = FakeMeta([[r]])
    go(fake, store, apply=True)
    assert fake.batches == []


def test_pagination_follows_the_cursor(pings, store):
    fake = FakeMeta([[row("1", "c-hi", 50000, 80)], [row("2", "c-ta", 50000, 80)]])
    code, _ = go(fake, store, apply=True)
    assert len(fake.reads) == 2 and [op["relative_url"] for op in fake.batches[0]] == ["1", "2"]


def test_failed_raise_is_reported_not_recorded_and_fails_the_run(pings, store):
    fake = FakeMeta([[row("1", "c-hi", 50000, 80), row("2", "c-hi", 50000, 80)]], fail=["1"])
    code, notes = go(fake, store, apply=True)
    assert code == 1 and pings == ["/start", "/fail"]
    assert "Budget increase: 1 ad set raised" in notes.texts[0] and "1. Hindi · as 2" in notes.texts[0]
    assert "Hindi · as 1" not in notes.texts[0]                                                      # not raised: not listed
    assert "⚠️ could not raise as 1" in notes.texts[0]
    assert set(store.last_budget_increases()) == {"2"}             # the failed one is retried tomorrow


def test_failed_read_alerts_and_fails(pings, store):
    code, notes = go(FakeMeta([[]], fail_read=True), store, apply=True)
    assert code == 1 and pings == ["/start", "/fail"] and "Budget increase failed" in notes.texts[0] and "token expired" in notes.texts[0]


def test_no_campaign_ids_fails_instead_of_silently_doing_nothing(pings, store):
    fake = FakeMeta([[]])
    code, notes = go(fake, store, apply=True, env={"META_AD_ACCOUNT_ID": "111"})
    assert code == 1 and fake.reads == [] and "META_CAMPAIGN_ID" in notes.texts[0] and pings == ["/start", "/fail"]
