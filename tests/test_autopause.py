import json
from urllib.parse import parse_qs

import httpx
import pytest

from metaads import autopause
from metaads.autopause import campaign_languages, evaluate, run_job
from metaads.config import load_settings
from metaads.meta import GraphClient

S = load_settings()
RULES = S.autopause
ENV = {"META_AD_ACCOUNT_ID": "act_111", "META_CAMPAIGN_ID_HINDI": "c-hi", "META_CAMPAIGN_ID_TAMIL": "c-ta"}


@pytest.mark.parametrize("results,spend,pause", [
    (20, 20 * 120, False), (20, 20 * 120.2, True),            # >=20 results: cpr limit 120
    (19, 19 * 130, False), (19, 19 * 130.2, True),            # 10-19: 130
    (10, 10 * 130, False), (10, 10 * 130.2, True),
    (9, 9 * 200, False), (9, 9 * 200.2, True),                # 6-9: 200
    (6, 6 * 200, False), (6, 6 * 200.2, True),
    (5, 5 * 225, False), (5, 5 * 225.2, True),                # 3-5: 225
    (3, 3 * 225, False), (3, 3 * 225.2, True),
    (2, 2 * 250, False), (2, 2 * 250.2, True),                # 1-2: 250
    (1, 250, False), (1, 250.5, True),
    (0, 150, False), (0, 150.01, True), (0, 0, False),        # no results: spend limit 150
])
def test_pause_rules_at_every_band_edge(results, spend, pause):
    assert (evaluate(results, spend, RULES) is not None) is pause


def test_only_the_matching_band_counts():
    # 12 results at cpr 125 is over the >=20 limit (120) but that band does not apply: the 10-19 limit is 130
    assert evaluate(12, 12 * 125, RULES) is None


def test_campaigns_come_from_all_language_env_vars_and_unset_ones_are_skipped():
    assert campaign_languages(S, ENV) == {"c-hi": "Hindi", "c-ta": "Tamil"}
    assert campaign_languages(S, {}) == {}


def row(adset_id, campaign, spend, results, name=None, action="start_trial_total"):
    conv = [{"action_type": action, "value": str(results)}] if results else []
    insights = {"data": [{"spend": str(spend), "conversions": conv}]} if spend is not None else None
    return {"id": adset_id, "name": name or f"as {adset_id}", "campaign_id": campaign,
            **({"insights": insights} if insights else {})}


class FakeMeta:
    def __init__(self, pages, fail_pause=(), fail_read=False):
        self.pages, self.fail_pause, self.fail_read = pages, set(fail_pause), fail_read
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
            if op["relative_url"] in self.fail_pause:
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
    monkeypatch.setattr(autopause, "ping", lambda url, suffix="": calls.append(suffix))
    return calls


def go(fake, apply, notes=None, env=ENV):
    notes = notes or Notes()
    client = lambda: GraphClient("tok", "v25.0", transport=httpx.MockTransport(fake), sleep=lambda x: None, backoff=0)
    return run_job(S, client, notes, env, apply, "https://hc-ping.com/x"), notes


def test_one_read_and_one_batch_pause_only_the_losers(pings):
    fake = FakeMeta([[row("1", "c-hi", 200, 0),              # no results, spend 200 > 150: pause
                      row("2", "c-hi", 100, 0),              # no results, spend 100: keep
                      row("3", "c-ta", 1400, 10),            # cpr 140 > 130: pause
                      row("4", "c-ta", 1000, 10),            # cpr 100: keep
                      row("5", "c-other", 9999, 0),          # a campaign not in .env: ignored
                      row("6", "c-hi", None, 0)]])           # never delivered: no insights, keep
    code, notes = go(fake, apply=True)
    assert code == 0 and len(fake.reads) == 1 and len(fake.batches) == 1
    assert fake.reads[0]["path"].endswith("/act_111/adsets")
    assert json.loads(fake.reads[0]["filtering"]) == [{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}]
    assert "date_preset(maximum)" in fake.reads[0]["fields"]
    assert [(op["relative_url"], op["body"]) for op in fake.batches[0]] == [("1", "status=PAUSED"), ("3", "status=PAUSED")]
    assert pings == ["/start", ""]
    assert len(notes.texts) == 1 and "PAUSED: Hindi | as 1" in notes.texts[0] and "2 of 5" in notes.texts[0]


def test_dry_run_reads_once_and_never_writes(pings):
    fake = FakeMeta([[row("1", "c-hi", 200, 0)]])
    code, notes = go(fake, apply=False)
    assert code == 0 and len(fake.reads) == 1 and fake.batches == []
    assert "DRY RUN" in notes.texts[0] and "would pause: Hindi | as 1" in notes.texts[0]


def test_nothing_flagged_means_no_write_and_no_telegram(pings):
    fake = FakeMeta([[row("1", "c-hi", 100, 0), row("2", "c-ta", 500, 5)]])
    code, notes = go(fake, apply=True)
    assert code == 0 and fake.batches == [] and notes.texts == [] and pings == ["/start", ""]


def test_results_use_the_configured_action_type_only(pings):
    fake = FakeMeta([[row("1", "c-hi", 200, 3, action="link_click_total")]])      # not a trial start: still 0 results
    go(fake, apply=True)
    assert len(fake.batches) == 1


def test_pagination_follows_the_cursor(pings):
    fake = FakeMeta([[row("1", "c-hi", 200, 0)], [row("2", "c-ta", 300, 0)]])
    code, _ = go(fake, apply=True)
    assert len(fake.reads) == 2 and [op["relative_url"] for op in fake.batches[0]] == ["1", "2"]


def test_failed_pause_is_reported_and_fails_the_run(pings):
    fake = FakeMeta([[row("1", "c-hi", 200, 0), row("2", "c-hi", 300, 0)]], fail_pause=["1"])
    code, notes = go(fake, apply=True)
    assert code == 1 and pings == ["/start", "/fail"]
    assert "PAUSED: Hindi | as 2" in notes.texts[0] and "NOT paused: Hindi | as 1" in notes.texts[0]
    assert "could not pause as 1" in notes.texts[0]


def test_failed_read_alerts_and_fails(pings):
    code, notes = go(FakeMeta([[]], fail_read=True), apply=True)
    assert code == 1 and pings == ["/start", "/fail"] and "FAILED" in notes.texts[0] and "token expired" in notes.texts[0]


def test_no_campaign_ids_fails_instead_of_silently_doing_nothing(pings):
    fake = FakeMeta([[]])
    code, notes = go(fake, apply=True, env={"META_AD_ACCOUNT_ID": "111"})
    assert code == 1 and fake.reads == [] and "META_CAMPAIGN_ID" in notes.texts[0] and pings == ["/start", "/fail"]
