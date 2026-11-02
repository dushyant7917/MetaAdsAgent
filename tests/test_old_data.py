import json

import httpx
import pytest

from metaads import jobrunner
from metaads.config import Settings
from metaads.jobs import JobStore
from metaads.meta import GraphClient
from metaads.old_data import OldDataCleaner, expiry, run_job
from metaads.planning import Planner
from test_budget import FakeMeta
from test_diskfiles import DAY, HOUR, NOW, Disk, add_job, disk, make_file  # noqa: F401  (disk is a fixture)

ENV = {"META_AD_ACCOUNT_ID": "act_111", "META_CAMPAIGN_ID_HINDI": "c-hi"}


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


def go(d, apply, winners=frozenset(), now=NOW, fake=None):
    """Run the job with a Meta that says `winners` are the reusable winning jobs (or answers from `fake`)."""
    notes = Notes()
    fake = fake or FakeMeta([[]])
    client = lambda: GraphClient.from_settings("tok", d.s, transport=httpx.MockTransport(fake), sleep=lambda x: None, backoff=0)
    return run_job(d.s, client, notes, ENV, d.store, apply, "https://hc-ping.com/x", clock=lambda: now), notes


def job(**kw):
    """A row of cleanup_jobs(): an ad that is done, was never uploaded and is not in the review loop."""
    base = {"id": "b1-1", "status": "done", "review": None, "reviewed": None, "updated": NOW - 30 * DAY,
            "upload_status": None, "upload_updated": None, "ad_id": None}
    return {**base, **kw}


CFG = Settings({"uploaded_keep_days": 2, "discarded_keep_days": 2})
UPLOADED = dict(upload_status="done", ad_id="a1")


@pytest.mark.parametrize("row,pending,due", [
    # being made, uploaded right now, or with a redo plan waiting: always kept
    (job(status="queued"), set(), False), (job(status="running", **UPLOADED, upload_updated=NOW - 9 * DAY), set(), False),
    (job(upload_status="running", upload_updated=NOW - 9 * DAY), set(), False),
    (job(**UPLOADED, upload_updated=NOW - 9 * DAY), {"b1-1"}, False),
    # uploaded: 2 days after the upload, whatever the review says
    (job(**UPLOADED, upload_updated=NOW - 1.9 * DAY), set(), False), (job(**UPLOADED, upload_updated=NOW - 2 * DAY), set(), True),
    (job(**UPLOADED, upload_updated=NOW - 3 * DAY, review="approved"), set(), True),
    (job(**UPLOADED, upload_updated=NOW - 3 * DAY, review="pending"), set(), True),
    (job(upload_status="done", ad_id=None, upload_updated=NOW - 9 * DAY), set(), False),    # no ad on Meta: not uploaded
    # upload failed: 2 days after the last try; kept while the owner has not decided
    (job(upload_status="failed", upload_updated=NOW - 1.9 * DAY, review="approved"), set(), False),
    (job(upload_status="failed", upload_updated=NOW - 2.1 * DAY, review="approved"), set(), True),
    (job(upload_status="failed", upload_updated=NOW - 9 * DAY, review="pending"), set(), False),
    # dropped or redone: 2 days after the decision (the last update when no time was kept)
    (job(review="dropped", reviewed=NOW - 1.9 * DAY), set(), False), (job(review="dropped", reviewed=NOW - 2 * DAY), set(), True),
    (job(review="redone", reviewed=NOW - 2 * DAY), set(), True), (job(review="dropped", reviewed=None), set(), True),
    (job(review="dropped", reviewed=NOW - 9 * DAY), {"b1-1"}, False),
    # failed while being made: 2 days after it failed, also when it still says "pending review"
    (job(status="failed", updated=NOW - 1.9 * DAY), set(), False), (job(status="failed", updated=NOW - 2 * DAY), set(), True),
    (job(status="failed", review="pending", updated=NOW - 3 * DAY), set(), True),
    # finished, maybe approved, but not uploaded (yet), or waiting for the owner: kept however old
    (job(review="approved", updated=NOW - 90 * DAY), set(), False), (job(updated=NOW - 90 * DAY), set(), False),
    (job(review="pending", updated=NOW - 90 * DAY), set(), False),
])
def test_when_the_files_of_an_ad_may_go(row, pending, due):
    assert (expiry(row, pending, NOW, CFG) is not None) is due


def test_the_reason_says_why_it_went():
    assert expiry(job(**UPLOADED, upload_updated=NOW - 3 * DAY), set(), NOW, CFG) == "uploaded to Meta 2+ days ago"
    assert expiry(job(review="redone", reviewed=NOW - 3 * DAY), set(), NOW, CFG) == "redone 2+ days ago"
    assert expiry(job(status="failed"), set(), NOW, CFG) == "ad failed 2+ days ago"


def files_of(d, job_id):
    return [d.path("work_dir", job_id), d.path("ads_dir", "hi", f"ad_{job_id}.mp4"), d.path("ads_dir", "hi", f"ad_{job_id}.json"),
            d.path("hooks_dir", "hi", f"hook_{job_id}.mp4"), d.path("hooks_dir", "hi", f"hook_{job_id}.json")]


def exists(d, job_id):
    return [p.exists() for p in files_of(d, job_id)]


def test_everything_of_an_old_uploaded_ad_goes_and_a_recent_one_stays(pings, disk):
    add_job(disk, "b1-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    add_job(disk, "b1-2", review="approved", upload="done", upload_updated=NOW - 1 * DAY)
    body = make_file(disk.path("body_dir", "hi", "navy.mp4"), 77)
    code, notes = go(disk, apply=True)
    assert code == 0 and pings == ["/start", ""]
    assert exists(disk, "b1-1") == [False] * 5 and exists(disk, "b1-2") == [True] * 5 and body.exists()
    assert notes.texts[0].startswith("🧹 Old data cleanup: 5 items deleted, 0.0 MB freed\n\n1. uploaded to Meta 2+ days ago: 5 items")
    assert disk.store.get_job("b1-1")["id"] == "b1-1"                     # the row stays: its hook is never repeated


def test_dropped_failed_and_unanswered_ads(pings, disk):
    add_job(disk, "d-1", review="dropped", reviewed=NOW - 3 * DAY)
    add_job(disk, "d-2", review="dropped", reviewed=NOW - 1 * DAY)
    add_job(disk, "f-1", status="failed", review="pending", updated=NOW - 5 * DAY)
    add_job(disk, "p-1", review="pending", created=NOW - 50 * DAY)         # waits for the owner however old
    add_job(disk, "a-1", review="approved", created=NOW - 50 * DAY)        # approved, not uploaded yet
    go(disk, apply=True)
    assert [any(exists(disk, j)) for j in ("d-1", "d-2", "f-1", "p-1", "a-1")] == [False, True, False, True, True]


def test_a_redo_plan_waiting_for_an_answer_protects_the_ad_for_a_while(pings, disk):
    add_job(disk, "r-1", review="dropped", reviewed=NOW - 5 * DAY)
    add_job(disk, "r-2", review="dropped", reviewed=NOW - 5 * DAY)
    for job_id, created in (("r-1", NOW - 1 * DAY), ("r-2", NOW - 3 * DAY)):
        disk.store.create_redo_plan(f"plan-{job_id}", job_id, "{}")
        from contextlib import closing
        with closing(disk.store._conn()) as c, c:
            c.execute("UPDATE redo_plans SET created=? WHERE id=?", (created, f"plan-{job_id}"))
    go(disk, apply=True)
    assert any(exists(disk, "r-1")) and not any(exists(disk, "r-2"))      # a plan nobody answered for 3 days protects nothing


def test_a_winning_hook_keeps_its_work_folder_but_not_its_published_files(pings, disk):
    add_job(disk, "w-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    add_job(disk, "l-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    notes = Notes()
    cleaner = OldDataCleaner(disk.s, disk.store, lambda: {"w-1"}, lambda: NOW)
    result = cleaner.run(apply=True)
    assert exists(disk, "w-1") == [True, False, False, False, False]
    assert exists(disk, "l-1") == [False] * 5
    assert result.kept_winner_folders == 1 and result.errors == []


def test_meta_is_read_only_when_something_is_due(pings, disk):
    add_job(disk, "b1-1", review="approved", upload="done", upload_updated=NOW - 1 * DAY)
    fake = FakeMeta([[]])
    code, notes = go(disk, apply=True, fake=fake)
    assert code == 0 and fake.reads == [] and notes.texts == []
    add_job(disk, "b1-2", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    fake = FakeMeta([[]])
    go(disk, apply=True, fake=fake)
    assert len(fake.reads) == 1 and fake.reads[0]["path"].endswith("/act_111/adsets")


def test_when_meta_cannot_be_read_nothing_is_deleted(pings, disk):
    add_job(disk, "b1-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    code, notes = go(disk, apply=True, fake=FakeMeta([[]], fail_read=True))
    assert code == 1 and pings == ["/start", "/fail"] and all(exists(disk, "b1-1"))
    assert "Old data cleanup failed" in notes.texts[0] and "token expired" in notes.texts[0]


def test_a_dry_run_deletes_nothing_but_reports(pings, disk):
    add_job(disk, "b1-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    code, notes = go(disk, apply=False)
    assert code == 0 and all(exists(disk, "b1-1"))
    assert notes.texts[0].startswith("🧹 Old data cleanup DRY RUN: 5 items would be deleted")


def test_a_second_run_finds_nothing_and_stays_quiet(pings, disk):
    add_job(disk, "b1-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    go(disk, apply=True)
    fake = FakeMeta([[]])
    code, notes = go(disk, apply=True, fake=fake)
    assert code == 0 and notes.texts == [] and fake.reads == []            # nothing left on disk, so no Meta read either


def test_the_keep_days_come_from_the_settings(tmp_path, pings):
    d = Disk(tmp_path, old_data={"uploaded_keep_days": 5, "discarded_keep_days": 1})
    add_job(d, "u-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    add_job(d, "d-1", review="dropped", reviewed=NOW - 1.5 * DAY)
    go(d, apply=True)
    assert all(exists(d, "u-1")) and not any(exists(d, "d-1"))
    go(d, apply=True, now=NOW + 3 * DAY)
    assert not any(exists(d, "u-1"))


def test_a_path_in_the_database_outside_the_folders_is_left_alone(pings, disk):
    add_job(disk, "b1-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    stray = make_file(disk.root / "elsewhere" / "ad.mp4", 5)
    body = make_file(disk.path("body_dir", "hi", "navy.mp4"), 77)
    from contextlib import closing
    with closing(disk.store._conn()) as c, c:
        c.execute("UPDATE jobs SET ad_path=?, hook_path=? WHERE id=?", (str(stray), str(body), "b1-1"))
    code, _ = go(disk, apply=True)
    assert code == 0 and stray.exists() and body.exists() and not disk.path("work_dir", "b1-1").exists()


def test_a_failed_delete_is_reported_and_fails_the_run(pings, disk, monkeypatch):
    from metaads import old_data
    add_job(disk, "b1-1", review="approved", upload="done", upload_updated=NOW - 3 * DAY)
    real = old_data.remove
    monkeypatch.setattr(old_data, "remove", lambda s, p: (_ for _ in ()).throw(PermissionError("denied"))
                        if p.name == "ad_b1-1.mp4" else real(s, p))
    code, notes = go(disk, apply=True)
    assert code == 1 and pings[-1] == "/fail" and "could not delete" in notes.texts[0]
    assert disk.path("ads_dir", "hi", "ad_b1-1.mp4").exists() and not disk.path("work_dir", "b1-1").exists()


# ---------- which work folders the planner says must stay ----------
def adset_row(adset_id, campaign, results, active):
    conv = [{"action_type": "start_trial_total", "value": str(results)}] if results else []
    return {"id": adset_id, "name": f"as {adset_id}", "campaign_id": campaign, "effective_status": "ACTIVE" if active else "PAUSED",
            "insights": {"data": [{"spend": "100", "conversions": conv}]}}


def test_every_winning_job_of_a_hook_that_is_not_retired_is_reusable(disk):
    # hook "a": two winners, the second one the more expensive; its newest ad set is a loser, but only one failure
    for i, created in enumerate((1, 2, 3), 1):
        add_job(disk, f"a-{i}", hook="a", upload="done", upload_updated=NOW, created=NOW - (10 - created) * DAY)
    # hook "b": won once, then 3 ad sets in a row were paused before 50 results: retired
    for i, created in enumerate((1, 2, 3, 4), 1):
        add_job(disk, f"b-{i}", hook="b", upload="done", upload_updated=NOW, created=NOW - (10 - created) * DAY)
    add_job(disk, "c-1", hook="c", upload="done", upload_updated=NOW)                      # running below 50: undecided
    add_job(disk, "z-1", hook="z", upload="done", upload_updated=NOW)                      # its ad set is in another campaign
    rows = [adset_row("as-a-1", "c-hi", 60, False), adset_row("as-a-2", "c-hi", 55, True), adset_row("as-a-3", "c-hi", 10, False),
            adset_row("as-b-1", "c-hi", 50, False), adset_row("as-b-2", "c-hi", 5, False), adset_row("as-b-3", "c-hi", 0, False),
            adset_row("as-b-4", "c-hi", 9, False), adset_row("as-c-1", "c-hi", 20, True),
            adset_row("as-z-1", "c-other", 99, True), adset_row("not-ours", "c-hi", 99, True)]
    fake = FakeMeta([rows])
    client = GraphClient.from_settings("tok", disk.s, transport=httpx.MockTransport(fake), sleep=lambda x: None, backoff=0)
    keep = Planner(disk.s, client, "111", {"c-hi": "Hindi"}, disk.store).reusable_hook_jobs()
    assert keep == {"a-1", "a-2"}
    assert len(fake.reads) == 1                                              # one read, not the planner's three
    assert json.loads(fake.reads[0]["filtering"])[0]["value"] == ["ACTIVE", "PAUSED"]
    assert "date_preset(maximum)" in fake.reads[0]["fields"]


def test_a_later_win_resets_the_retirement_of_a_hook(disk):
    for i in range(1, 6):
        add_job(disk, f"b-{i}", hook="b", upload="done", upload_updated=NOW, created=NOW - (10 - i) * DAY)
    rows = [adset_row("as-b-1", "c-hi", 5, False), adset_row("as-b-2", "c-hi", 5, False), adset_row("as-b-3", "c-hi", 5, False),
            adset_row("as-b-4", "c-hi", 70, False), adset_row("as-b-5", "c-hi", 5, False)]      # 3 failures, then a win, then 1
    client = GraphClient.from_settings("tok", disk.s, transport=httpx.MockTransport(FakeMeta([rows])), sleep=lambda x: None, backoff=0)
    assert Planner(disk.s, client, "111", {"c-hi": "Hindi"}, disk.store).reusable_hook_jobs() == {"b-4"}
