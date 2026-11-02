"""Values that used to be written into the code (or the Hermes text) now come from config/settings.yaml: changing the
setting must change the behaviour."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from metaads import notify, schedule, skilltext, videogen
from metaads.adplan import AdPlans
from metaads.config import Settings, load_settings
from metaads.jobs import JobStore
from metaads.adaccount import attribution_windows_param, insights_field, sum_action
from metaads.meta import GraphClient, build_adset_fields, default_start_time
from metaads.planning import launch_day
from metaads.ratelimit import RateLimiter
from test_adplan import FakePipeline, FakeRedo, campaign, hook, make, plan_of, tamil
from metaads.pipeline import Pipeline
from test_pipeline import Fakes, Recorder, env  # noqa: F401  (env is a fixture)

BASE = load_settings()
IST = timezone(timedelta(hours=5, minutes=30))


def changed(**sections) -> Settings:
    """The settings with some values replaced: changed(limits={"job_attempts": 3})."""
    d = copy.deepcopy(BASE.to_dict())
    for name, values in sections.items():
        d[name].update(values)
    return Settings(d)


# ---------- schedules ----------
def test_the_daily_plan_time_in_ist_becomes_a_utc_cron_expression():
    assert schedule.plan_cron_utc(BASE) == "30 6 * * *"                                   # 12:00 IST
    assert schedule.plan_cron_utc(changed(schedules={"daily_plan_ist": "00:15"})) == "45 18 * * *"   # the day before in UTC
    assert schedule.plan_cron_utc(changed(schedules={"daily_plan_ist": "05:30"})) == "0 0 * * *"


def test_the_autopause_interval_must_divide_a_day_and_the_budget_hour_must_be_an_hour():
    assert schedule.autopause_every(BASE) == timedelta(hours=2) and schedule.budget_increase_hour(BASE) == 5
    assert schedule.autopause_every(changed(schedules={"autopause_every_hours": 4})) == timedelta(hours=4)
    for bad in (5, 0, 25):
        with pytest.raises(ValueError, match="divide 24"):
            schedule.autopause_every(changed(schedules={"autopause_every_hours": bad}))
    with pytest.raises(ValueError, match="0-23"):
        schedule.budget_increase_hour(changed(schedules={"budget_increase_hour_ist": 24}))


def test_the_start_hour_comes_from_the_settings():
    now = datetime(2026, 11, 1, 8, 0, tzinfo=IST)
    assert default_start_time(now, 6) == "2026-11-02T06:00:00+05:30"
    assert default_start_time(now, 9) == "2026-11-01T09:00:00+05:30"
    assert launch_day(now, changed(meta={"start_hour_ist": 9})).isoformat() == "2026-11-01"
    assert launch_day(now, BASE).isoformat() == "2026-11-02"


# ---------- attribution ----------
def test_new_ad_sets_are_attributed_with_the_click_window_from_the_settings():
    spec = lambda s: json.loads(build_adset_fields(s.meta, "n", "c", "2026-11-02T06:00:00+05:30", {}, "app", "url")["attribution_spec"])
    assert spec(BASE) == [{"event_type": "CLICK_THROUGH", "window_days": 7}]               # a click, up to 7 days; no view-through
    assert spec(changed(meta={"attribution_click_days": 1})) == [{"event_type": "CLICK_THROUGH", "window_days": 1}]


def test_results_are_read_in_the_same_click_window():
    assert 'insights.date_preset(maximum).action_attribution_windows(["7d_click"]){spend,conversions}' == insights_field(BASE, "spend,conversions")
    assert attribution_windows_param(BASE) == '["7d_click"]'
    one_day = changed(meta={"attribution_click_days": 1})
    assert '["1d_click"]' in insights_field(one_day, "conversions") and attribution_windows_param(one_day) == '["1d_click"]'
    # with one window requested Meta's `value` is that window's count, which is what the rules read
    assert sum_action([{"action_type": "start_trial_total", "value": "3", "7d_click": "3"}], "start_trial_total") == 3


# ---------- Hermes text ----------
def test_placeholders_in_the_hermes_text_are_filled_from_the_settings():
    text = "{{limits.max_batch_items}} ads, clips of {{hook.durations}} s, raise {{ budget_increase.increase_pct }}%"
    assert skilltext.render(text, BASE) == (
        f"{BASE.limits.max_batch_items} ads, clips of 4, 6, 8 s, raise {BASE.budget_increase.increase_pct}%")
    assert "9" in skilltext.render("{{meta.start_hour_ist}}", changed(meta={"start_hour_ist": 9}))
    with pytest.raises(KeyError, match="no.such.key"):
        skilltext.render("{{no.such.key}}", BASE)


def test_every_placeholder_of_the_real_hermes_text_resolves_and_no_figure_is_left_to_go_stale():
    root = Path(__file__).resolve().parent.parent / "hermes"
    for path in [root / "SOUL.md", *root.glob("skills/*/SKILL.md")]:
        assert "{{" not in skilltext.render(path.read_text(), BASE), path
    all_text = "\n".join(p.read_text() for p in [root / "SOUL.md", *root.glob("skills/*/SKILL.md")])
    for stale in ("every 2 hours", "5:00 IST", "6:00 IST", "12:00 IST", "70 hours", "20%", "4 requests per minute", "up to 3 times"):
        assert stale not in all_text, f"{stale!r} is written into the Hermes text; use a {{{{placeholder}}}}"


# ---------- retries, timeouts, limits ----------
def test_the_number_of_attempts_per_ad_comes_from_the_settings(env):
    s, tmp = env
    for attempts in (1, 3):
        d = copy.deepcopy(s.to_dict())
        d["limits"]["job_attempts"] = attempts
        fakes = Fakes(video_always_fails=True)
        p = Pipeline(Settings(d), JobStore(tmp / f"db{attempts}.sqlite"), fakes.services(Settings(d)), Recorder())
        p.submit([{"language": "hi", "spoken_text": "Dialogue words"}], background=False)
        assert fakes.calls["video"] == attempts


def test_vertex_retries_and_backoff_come_from_the_settings(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(videogen.time, "sleep", waits.append)
    s = changed(generation={"max_retries": 2, "backoff_seconds": 3, "backoff_max_seconds": 5})
    videogen._backoff(0, s), videogen._backoff(1, s), videogen._backoff(5, s)
    assert waits == [3, 5, 5]                                                              # 3, 6 capped at 5, capped at 5


def test_graph_client_timeouts_and_rate_limit_retries_come_from_the_settings():
    s = changed(meta={"max_attempts": 5, "backoff_seconds": 7}, timeouts={"meta_read": 11, "meta_upload": 22})
    read, upload = GraphClient.from_settings("t", s), GraphClient.from_settings("t", s, upload=True)
    assert (read.max_attempts, read.backoff, read.http.timeout.read) == (5, 7, 11)
    assert upload.http.timeout.read == 22 and read.base.endswith(f"/{s.meta.api_version}")


def test_telegram_timeouts_come_from_the_settings(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: seen.append(kw["timeout"]) or
                        type("R", (), {"raise_for_status": lambda self: None})())
    s = changed(timeouts={"telegram_text": 1, "telegram_photo": 2, "telegram_video": 3})
    n = notify.TelegramNotifier("tok", "42", settings=s)
    image, video = tmp_path / "a.png", tmp_path / "a.mp4"
    image.write_bytes(b"x"), video.write_bytes(b"x")
    n.send_text(None, "hi"), n.send_photo(None, image, "c"), n.send_video(None, video, "c")
    assert seen == [1, 2, 3]


def test_the_caption_lengths_come_from_the_settings(env):
    s, tmp = env
    d = copy.deepcopy(s.to_dict())
    d["messages"].update({"spoken_chars": 5, "thumbnail_chars": 4})
    fakes = Fakes()
    p = Pipeline(Settings(d), JobStore(tmp / "caps.sqlite"), fakes.services(Settings(d)), Recorder())
    batch, [job_id] = p.store.create_batch([{"language": "hi", "spoken_text": "abcdefghij", "thumbnail_text": "klmnopqrst"}])
    caption = p._caption(p.store.get_job(job_id))
    assert "Says: abcde\n" in caption and "Thumbnail: klmn\n" in caption


# ---------- the daily plan ----------
def test_the_plan_message_and_the_stale_check_use_the_configured_start_hour(tmp_path):
    plan = plan_of(campaign("Hindi", video=1))
    ap, store, pipe, _ = make(tmp_path, plan)
    ap.s = changed(meta={"start_hour_ist": 9})
    res = ap.propose([hook()])
    assert "(ads start 9:00 IST)" in res["message"]
    ap.clock = lambda: datetime(2026, 11, 3, 8, 59, tzinfo=IST)
    assert ap.approve(res["plan_id"])["approved"] is True                                  # 6:00 IST has passed, 9:00 has not


def test_the_owners_language_and_its_label_come_from_the_settings(tmp_path):
    plan = plan_of(campaign("Hindi", video=1))
    ap, *_ = make(tmp_path, plan)
    ap.s = changed(ad_planning={"gloss_language": "ta", "gloss_label": "தமிழ்"})
    # Hindi is no longer the owner's language, so a Hindi hook now needs a Tamil translation (not Devanagari)
    res = ap.propose([hook()])
    assert any("spoken_hindi (the Tamil translation) is missing" in e for e in res["errors"])
    good = ap.propose([hook(spoken_hindi="நீங்கள் news reporter என்றால் இந்த app")])
    assert good["proposed"] is True and "(தமிழ்: நீங்கள் news reporter என்றால் இந்த app)" in good["message"]
    # and a Tamil hook needs none any more
    ap.plan_new_ads = lambda: plan_of(campaign("Tamil", video=1))
    assert ap.propose([hook("ta", spoken="நீங்கள் news reporter என்றால் இந்த app உங்களுக்கு தான்!", thumb="news reporter என்றால் இந்த app")])["proposed"] is True
