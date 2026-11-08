import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from metaads.config import Settings, load_settings
from metaads.jobs import JobStore
from metaads.meta import IST, GraphClient
from metaads.money import inr_to_paise, paise_to_inr, rupees
from metaads.performance import AdSet, format_stats, hook_outcomes, image_share
from metaads.planning import (Inputs, PlanningError, Planner, allocate, build_plan, launch_days_left, month_end,
                              next_midnight, project)

BASE = load_settings().to_dict()
NOW = datetime(2026, 11, 1, 14, 0, tzinfo=IST)               # 2pm: the next 06:00 IST start is 2 Nov -> 29 launch days
DEFAULT_BODY = BASE["default_body"]


def settings(tmp_path=None, **ad_planning) -> Settings:
    data = copy.deepcopy(BASE)
    data["ad_planning"].update(ad_planning)
    if tmp_path is not None:
        data["paths"]["body_dir"] = str(tmp_path)
    return Settings(data)


S = settings()


def adset(id="1", language="Hindi", active=True, budget=50000, spend=0.0, results=0, recent_spend=None,
          recent_results=0.0, spend_today=0.0, last_raise=None, job_id=None, hook=None, made=0.0,
          ad_format=None, body=None) -> AdSet:
    return AdSet(id, f"as {id}", language, active, budget, spend, results, recent_spend, recent_results, spend_today,
                 last_raise, job_id, hook, made, ad_format, body)


def inputs(adsets=(), spent=0.0, now=NOW, used=(), bodies=None, videos=0, languages=("Hindi", "Tamil")) -> Inputs:
    return Inputs(now, list(languages), spent, list(adsets), set(used), bodies or {}, videos)


def test_money_conversions_round_trip():
    assert inr_to_paise(500) == 50000 and paise_to_inr(50000) == 500
    assert inr_to_paise(paise_to_inr(12345)) == 12345 and rupees(123456) == "1,234.56"


@pytest.mark.parametrize("now,days", [
    (datetime(2026, 11, 1, 14, 0, tzinfo=IST), 29),            # after 06:00: launch 2 Nov .. 30 Nov
    (datetime(2026, 11, 1, 3, 0, tzinfo=IST), 30),             # before 06:00: today's 06:00 is the launch
    (datetime(2026, 11, 29, 14, 0, tzinfo=IST), 1),            # launch 30 Nov, the last day
    (datetime(2026, 11, 30, 14, 0, tzinfo=IST), 0),            # launch falls in December: next month's budget
    (datetime(2026, 11, 30, 3, 0, tzinfo=IST), 1),
])
def test_launch_days_start_at_the_next_start_time(now, days):
    assert launch_days_left(now, S) == days


def test_utc_clock_is_read_in_ist():
    assert launch_days_left(datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc), S) == 29      # = 14:00 IST


def test_allocate_adds_up_and_follows_weights():
    assert allocate(9, {"a": 2, "b": 1}) == {"a": 6, "b": 3}
    assert sum(allocate(10, {"a": 1, "b": 1, "c": 1}).values()) == 10
    assert allocate(0, {"a": 1, "b": 3}) == {"a": 0, "b": 0}


def test_allocate_gives_leftovers_to_the_ranked_first():
    assert allocate(4, {"a": 1, "b": 1, "c": 1}, rank={"c": 0, "b": 1, "a": 2}.get) == {"a": 1, "b": 1, "c": 2}


ALL = ("Bengali", "Gujarati", "Hindi", "Kannada", "Marathi", "Tamil", "Telugu")


def test_tied_languages_follow_the_configured_preference_order():
    plan = build_plan(inputs(languages=ALL), settings(reserve_pct=10))                 # 9 ads over 7 equal languages
    assert plan["new_ads_total"] == 9
    assert {c["language"]: c["new_ads"] for c in plan["campaigns"]} == {
        "Hindi": 2, "Telugu": 2, "Kannada": 1, "Marathi": 1, "Gujarati": 1, "Bengali": 1, "Tamil": 1}


def test_languages_missing_from_the_order_come_last_and_the_order_is_configurable():
    plan = build_plan(inputs(languages=ALL), settings(reserve_pct=10, tie_break_order=["ta", "bn"]))
    assert {c["language"] for c in plan["campaigns"] if c["new_ads"] == 2} == {"Tamil", "Bengali"}


def test_an_unknown_language_in_the_order_is_an_error():
    with pytest.raises(PlanningError):
        build_plan(inputs(languages=ALL), settings(tie_break_order=["hi", "xx"]))


def test_empty_account_spreads_the_free_budget_over_the_launch_days():
    plan = build_plan(inputs(), S)
    # left 200000, reserve 20% = 40000, free 160000; cost = 0.85*200 (prior loser) + 0.15*3500 (7 days of Rs 500) = 695
    assert plan["budget_left_inr"] == 200000 and plan["reserve_inr"] == 40000
    assert plan["committed_by_running_adsets_inr"] == 0
    assert plan["expected_cost_per_new_ad_inr"] == 695
    assert plan["launch_days_left"] == 29 and plan["launch_day"] == "2026-11-02"
    assert plan["new_ads_total"] == 8                          # ceil(160000 / 695 / 29)
    assert any("prior" in n for n in plan["notes"])


@pytest.mark.parametrize("day,pct", [(1, 20), (21, 20), (22, 10), (30, 10)])
def test_reserve_is_20_percent_for_three_weeks_then_10(day, pct):
    plan = build_plan(inputs(now=datetime(2026, 11, day, 14, 0, tzinfo=IST)), S)
    assert plan["reserve_inr"] == 200000 * pct / 100


def test_nothing_left_means_no_new_ads():
    plan = build_plan(inputs(spent=200000), S)
    assert plan["new_ads_total"] == 0 and plan["notes"]


def test_a_launch_in_next_month_plans_nothing():
    plan = build_plan(inputs(now=datetime(2026, 11, 30, 14, 0, tzinfo=IST)), S)
    assert plan["new_ads_total"] == 0 and plan["launch_days_left"] == 0 and any("next month" in n for n in plan["notes"])


def test_small_budget_still_launches_one_a_day_rather_than_never():
    plan = build_plan(inputs(spent=200000 - 9000), settings(reserve_pct=0))
    assert plan["new_ads_total"] == 1                          # 9000 / 695 = 12 ads over 29 days -> 1 today


def test_missing_monthly_budget_is_an_error():
    for bad in (0, None):
        with pytest.raises(PlanningError):
            build_plan(inputs(), settings(monthly_budget_inr=bad))


def test_rest_of_today_is_deducted_from_what_a_running_ad_set_will_spend():
    a = adset(spend=6000, results=60, recent_spend=500, recent_results=5, spend_today=400, last_raise=NOW.timestamp())
    p = project(a, NOW, next_midnight(NOW), S)
    assert p.committed == pytest.approx(100) and p.paused_at is None       # 500 budget - 400 already spent today


def test_a_losing_ad_set_is_forecast_to_be_paused_soon_and_costs_little():
    a = adset(spend=100, results=0, recent_spend=100, recent_results=0)
    p = project(a, NOW, month_end(NOW), S)
    assert p.paused_at is not None and p.paused_at - NOW < timedelta(hours=12) and p.committed < 100


def test_an_already_flagged_ad_set_costs_nothing():
    p = project(adset(spend=300, results=0), NOW, month_end(NOW), S)
    assert p.committed == 0 and p.paused_at == NOW


def test_a_worsening_cost_per_result_is_paused_before_month_end():
    # cpr 100 lifetime is fine, but the recent days cost 250 per result, so the lifetime figure drifts over 120
    a = adset(spend=2000, results=20, recent_spend=500, recent_results=2, spend_today=250)    # half the day gone
    p = project(a, NOW, month_end(NOW), S)
    assert p.paused_at is not None and timedelta(days=1) < p.paused_at - NOW < timedelta(days=3)


def test_a_steady_winner_runs_to_month_end_and_is_raised():
    a = adset(spend=6000, results=60, recent_spend=500, recent_results=5)                 # cpr 100 throughout
    p = project(a, NOW, month_end(NOW), S)
    assert p.paused_at is None and p.raises >= 1
    assert p.committed > 29 * 500 * 1.2                        # the raises, not just Rs 500 a day


def test_a_raised_budget_stops_at_the_cap():
    a = adset(budget=500000, spend=6000, results=60, recent_spend=5000, recent_results=50)
    p = project(a, NOW, month_end(NOW), S)
    assert p.raises == 0                                        # already at max_daily_budget


def test_running_ad_sets_reduce_what_is_free_for_new_ads():
    winner = adset(spend=6000, results=60, recent_spend=500, recent_results=5)
    plan = build_plan(inputs([winner]), S)
    assert plan["committed_by_running_adsets_inr"] > 29 * 500
    assert plan["new_ads_total"] < build_plan(inputs(), S)["new_ads_total"]
    assert [w["adset"] for w in plan["scaling_winners"]] == ["as 1"]


def test_at_risk_ad_sets_are_listed_with_days_to_pause():
    plan = build_plan(inputs([adset(spend=100, results=0, recent_spend=100)]), S)
    assert plan["at_risk_adsets"][0]["adset"] == "as 1" and plan["at_risk_adsets"][0]["paused_in_days"] < 1


def test_win_rate_and_loser_cost_come_from_history_once_there_is_some():
    history = [adset(id=f"w{i}", spend=3000, results=60) for i in range(5)]                       # cpr 50: wins
    history += [adset(id=f"l{i}", active=False, spend=300, results=0) for i in range(5)]          # paused losers
    plan = build_plan(inputs(history), S)
    assert plan["win_rate"] == pytest.approx((5 + 0.15 * 10) / (10 + 10), abs=1e-3)
    p = plan["win_rate"]
    assert plan["expected_cost_per_new_ad_inr"] == pytest.approx((1 - p) * 300 + p * 3500, abs=0.01)
    assert not any("prior" in n for n in plan["notes"])          # 10 decided ad sets are enough history


def test_the_daily_video_cap_turns_the_extra_new_hooks_into_image_ads():
    plan = build_plan(inputs(videos=BASE["limits"]["daily_video_cap"] - 2), S)
    videos = sum(c["new_hook_video_ads"] for c in plan["campaigns"])
    images = sum(c["new_hook_image_ads"] for c in plan["campaigns"])
    assert videos == 2 and videos + images == sum(c["new_hook_ads"] for c in plan["campaigns"]) == plan["new_ads_total"]
    assert images > 0 and any("video cap" in n for n in plan["notes"])


def _decided(fmt: str, wins: int, losses: int, start: int = 0) -> list[AdSet]:
    """Ad sets of one format with a decided outcome: wins reached the bar (and were paused later, so they commit no
    more spend), losses were paused early."""
    won = [adset(id=f"{fmt}w{start + i}", active=False, spend=3000, results=60, ad_format=fmt) for i in range(wins)]
    lost = [adset(id=f"{fmt}l{start + i}", active=False, spend=300, results=0, ad_format=fmt) for i in range(losses)]
    return won + lost


def _new_hook_formats(plan: dict) -> tuple[int, int]:
    return (sum(c["new_hook_video_ads"] for c in plan["campaigns"]), sum(c["new_hook_image_ads"] for c in plan["campaigns"]))


def test_format_stats_count_decided_ad_sets_per_format_and_ignore_foreign_ones():
    stats = format_stats(_decided("video", 2, 8) + _decided("image", 5, 5) + [adset(id="x", spend=3000, results=60)], S)
    assert (stats["video"]["decided"], stats["video"]["wins"]) == (10, 2) and (stats["image"]["wins"], stats["image"]["decided"]) == (5, 10)
    assert stats["video"]["win_rate"] == pytest.approx((2 + 1.5) / 20, abs=1e-3)     # smoothed with the prior
    assert stats["image"]["win_rate"] == pytest.approx((5 + 1.5) / 20, abs=1e-3)


def test_the_format_that_wins_more_gets_more_new_hooks():
    video_better = build_plan(inputs(_decided("video", 8, 2) + _decided("image", 1, 9)), S)
    image_better = build_plan(inputs(_decided("video", 1, 9) + _decided("image", 8, 2)), S)
    v1, i1 = _new_hook_formats(video_better)
    v2, i2 = _new_hook_formats(image_better)
    assert v1 > i1 and i2 > v2 and v1 + i1 == v2 + i2 > 0
    assert "image ads" in video_better["format_reason"] and video_better["format_stats"]["video"]["wins"] == 8


def test_the_split_is_purely_on_merit_with_no_floor():
    stats = format_stats(_decided("video", 30, 10) + _decided("image", 1, 39), S)       # image has won once: split by rates
    assert image_share(stats, S) == pytest.approx(stats["image"]["win_rate"] / (stats["image"]["win_rate"] + stats["video"]["win_rate"]))
    assert 0 < image_share(stats, S) < 0.10                                            # no floor lifts it


def test_a_format_that_never_wins_gets_no_new_hooks_while_the_other_wins():
    give_up = S.ad_planning.format_give_up_after
    stats = format_stats(_decided("video", 8, 2) + _decided("image", 0, give_up), S)
    assert image_share(stats, S) == 0
    assert image_share(format_stats(_decided("video", 0, give_up) + _decided("image", 8, 2), S), S) == 1
    assert image_share(format_stats(_decided("video", 0, give_up) + _decided("image", 0, give_up), S), S) == pytest.approx(0.5)
    assert image_share(format_stats(_decided("video", 8, 2) + _decided("image", 0, give_up - 1), S), S) > 0   # not enough tries yet
    plan = build_plan(inputs(_decided("video", 8, 2) + _decided("image", 0, give_up)), S)
    assert sum(c["new_hook_image_ads"] for c in plan["campaigns"]) == 0 and sum(c["new_hook_video_ads"] for c in plan["campaigns"]) > 0
    assert "no image ads" in plan["format_reason"]


def test_the_video_cap_drops_new_hooks_instead_of_making_image_ads_that_do_not_win():
    losing = _decided("video", 8, 2) + _decided("image", 0, S.ad_planning.format_give_up_after)
    plan = build_plan(inputs(losing, videos=BASE["limits"]["daily_video_cap"] - 2), S)
    assert sum(c["new_hook_image_ads"] for c in plan["campaigns"]) == 0 and sum(c["new_hook_video_ads"] for c in plan["campaigns"]) == 2
    assert plan["new_ads_total"] == sum(c["new_ads"] for c in plan["campaigns"]) and any("image ads have not won" in n for n in plan["notes"])


def test_with_no_history_new_hooks_split_evenly_and_the_split_adds_up():
    plan = build_plan(inputs(), S)
    v, i = _new_hook_formats(plan)
    assert plan["format_stats"]["video"]["decided"] == 0 and abs(v - i) <= 1
    for c in plan["campaigns"]:
        assert c["new_hook_video_ads"] + c["new_hook_image_ads"] == c["new_hook_ads"] <= c["new_ads"]


def test_proven_hook_ads_do_not_count_as_image_or_video_new_hooks():
    win = adset(id="9", spend=3000, results=60, job_id="j1", hook=("hi", "the hook", "the hook"), ad_format="video")
    plan = build_plan(inputs([win], bodies={"hi": [DEFAULT_BODY, "teal-darkmint-mintbox"]}), S)
    hindi = next(c for c in plan["campaigns"] if c["language"] == "Hindi")
    assert hindi["new_ads"] == hindi["new_hook_video_ads"] + hindi["new_hook_image_ads"] + sum(
        len(h["bodies"]) for h in hindi["proven_hook_ads"])


def test_hook_outcomes_list_winners_and_recent_losers_with_both_texts():
    won = adset(id="1", spend=3000, results=60, hook=("hi", "good words", "good headline"), ad_format="image", body=None)
    lost_old = adset(id="2", active=False, spend=300, results=0, hook=("hi", "old fail", "old fail"), made=1.0, ad_format="video")
    lost_new = adset(id="3", active=False, spend=300, results=2, hook=("hi", "new fail", "new fail"), made=2.0, ad_format="video")
    running = adset(id="4", spend=100, results=3, hook=("hi", "still running", "x"))
    other = adset(id="5", language="Tamil", spend=3000, results=60, hook=("ta", "tamil words", "tamil"))
    out = hook_outcomes([won, lost_old, lost_new, running, other], S, "Hindi")
    assert [w["spoken_text"] for w in out["winners"]] == ["good words"] and out["winners"][0]["thumbnail_text"] == "good headline"
    assert out["winners"][0]["ad_format"] == "image" and out["winners"][0]["cpr_inr"] == 50.0
    assert [l["spoken_text"] for l in out["losers"]] == ["new fail", "old fail"]       # newest first, running one left out
    assert hook_outcomes([won, other], S)["winners"][1]["spoken_text"] == "tamil words"  # all languages when none is named
    assert len(hook_outcomes([won] * 9, S, "Hindi", limit=3)["winners"]) == 3


def test_proven_hooks_only_get_bodies_they_have_not_had():
    bodies = [DEFAULT_BODY, "teal-darkmint-mintbox", "plum-darkcoral-coralbox"]
    win = adset(id="9", spend=3000, results=60, job_id="j1", hook=("hi", "the hook", "the hook"))
    used = {("hi", "the hook", "the hook", None), ("hi", "the hook", "the hook", "teal-darkmint-mintbox")}                # None = default body
    plan = build_plan(inputs([win], bodies={"hi": bodies}, used=used), S)
    hindi = next(c for c in plan["campaigns"] if c["language"] == "Hindi")
    assert hindi["proven_hook_ads"] == [{"hook_job_id": "j1", "spoken_text": "the hook", "thumbnail_text": "the hook", "results": 60, "cpr_inr": 50.0,
                                         "bodies": ["plum-darkcoral-coralbox"]}]
    assert hindi["new_ads"] == hindi["new_hook_ads"] + 1
    assert sum(c["new_ads"] for c in plan["campaigns"]) == plan["new_ads_total"]


def test_a_winner_paused_later_for_fatigue_is_still_a_winner():
    tired = adset(id="9", active=False, spend=9000, results=60, job_id="j1", hook=("hi", "h", "h"))        # cpr 150 when paused
    early_loser = adset(id="8", active=False, spend=300, results=3)                                     # paused before 50
    plan = build_plan(inputs([tired, early_loser], bodies={"hi": ["x"]}), S)
    hindi = next(c for c in plan["campaigns"] if c["language"] == "Hindi")
    assert hindi["proven_hooks_known"] == 1
    p = (1 + 0.15 * 10) / (2 + 10)                                 # 1 win, 1 loser (paused at 300 before 50 results)
    assert plan["win_rate"] == pytest.approx(p, abs=1e-3)
    assert plan["expected_cost_per_new_ad_inr"] == pytest.approx((1 - p) * 300 + p * 3500, abs=0.01)


def hook_history(*outcomes: str) -> list[AdSet]:
    """One hook's ad sets oldest first: W = won (60 results), L = paused before 50, U = still running below 50."""
    spec = {"W": dict(active=False, spend=3000, results=60), "L": dict(active=False, spend=200, results=1),
            "U": dict(active=True, spend=100, results=5)}
    return [adset(id=str(i), made=float(i), job_id=f"j{i}", hook=("hi", "h", "h"), **spec[o]) for i, o in enumerate(outcomes)]


@pytest.mark.parametrize("outcomes,retired", [
    ("W", False), ("WLL", False), ("WLLL", True),            # three failed remixes in a row after the win
    ("WLLLW", False), ("WLLLWLL", False),                    # a later win resets the count
    ("WLLU", False), ("WLLULL", True), ("WLULLU", True),     # a running, undecided ad set neither counts nor resets
])
def test_a_hook_is_retired_after_three_failed_remixes_in_a_row(outcomes, retired):
    plan = build_plan(inputs(hook_history(*outcomes), bodies={"hi": ["x"]}), S)
    hindi = next(c for c in plan["campaigns"] if c["language"] == "Hindi")
    assert hindi["proven_hooks_known"] == (0 if retired else 1)
    assert any("hook retired" in n for n in plan["notes"]) is retired


def test_a_language_with_a_proven_hook_gets_a_bigger_share():
    win = adset(id="9", spend=3000, results=60, job_id="j1", hook=("hi", "h", "h"))
    plan = build_plan(inputs([win], bodies={"hi": ["x", "y"]}), S)
    by = {c["language"]: c["new_ads"] for c in plan["campaigns"]}
    assert by["Hindi"] > by["Tamil"] and by["Hindi"] + by["Tamil"] == plan["new_ads_total"]


# ---------------- reading Meta ----------------

def conv(n):
    return [{"action_type": "start_trial_total", "value": str(n)}] if n else []


class FakeMeta:
    def __init__(self):
        self.reads: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        self.reads.append({"path": req.url.path, **params})
        if req.url.path.endswith("/adsets"):
            data = [
                {"id": "10", "name": "Hindi winner", "campaign_id": "c-hi", "effective_status": "ACTIVE",
                 "daily_budget": "50000", "insights": {"data": [{"spend": "3000", "conversions": conv(60)}]}},
                {"id": "11", "name": "Hindi loser", "campaign_id": "c-hi", "effective_status": "PAUSED",
                 "daily_budget": "50000", "insights": {"data": [{"spend": "300", "conversions": []}]}},
                {"id": "12", "name": "elsewhere", "campaign_id": "c-other", "effective_status": "ACTIVE",
                 "daily_budget": "50000", "insights": {"data": [{"spend": "9999", "conversions": []}]}},
            ]
        elif params["level"] == "adset":
            assert json.loads(params["time_range"]) == {"since": "2026-10-29", "until": "2026-11-01"}
            data = [{"adset_id": "10", "date_start": f"2026-10-{d}", "spend": "500", "conversions": conv(5)}
                    for d in (29, 30, 31)] + [{"adset_id": "10", "date_start": "2026-11-01", "spend": "200",
                                               "conversions": conv(2)}]
        else:
            data = [{"campaign_id": "c-hi", "spend": "4000"}, {"campaign_id": "c-ta", "spend": "1000"}]
        return httpx.Response(200, json={"data": data})


def test_planner_reads_three_times_and_links_ad_sets_to_their_hooks(tmp_path):
    for stem in (DEFAULT_BODY, "teal-darkmint-mintbox"):
        (tmp_path / "hi").mkdir(exist_ok=True)
        (tmp_path / "hi" / f"{stem}.mp4").touch()
    cfg = settings(tmp_path)
    store = JobStore(tmp_path / "jobs.db")
    _, (job_id,) = store.create_batch([{"language": "hi", "spoken_text": "winning hook", "thumbnail_text": "winning hook", "body": None}])
    store.upsert_meta_upload(job_id, adset_id="10")
    fake = FakeMeta()
    client = GraphClient.from_settings("tok", load_settings(), transport=httpx.MockTransport(fake), sleep=lambda x: None, backoff=0)

    plan = Planner(cfg, client, "111", {"c-hi": "Hindi", "c-ta": "Tamil"}, store, clock=lambda: NOW).run()

    assert len(fake.reads) == 3
    assert any(r.get("action_attribution_windows") == '["7d_click"]' for r in fake.reads)     # daily results: same click window
    assert any('action_attribution_windows(["7d_click"])' in r.get("fields", "") for r in fake.reads)
    assert all(json.loads(r["filtering"])[0]["value"] == ["c-hi", "c-ta"] for r in fake.reads if "insights" in r["path"])
    assert plan["spent_this_month_inr"] == 5000 and plan["budget_left_inr"] == 195000     # c-other is not counted
    hindi = next(c for c in plan["campaigns"] if c["language"] == "Hindi")
    assert hindi["proven_hooks_known"] == 1
    assert hindi["proven_hook_ads"] == [{"hook_job_id": job_id, "spoken_text": "winning hook", "thumbnail_text": "winning hook", "results": 60,
                                         "cpr_inr": 50.0, "bodies": ["teal-darkmint-mintbox"]}]    # default body is taken
    assert plan["scaling_winners"][0]["adset"] == "Hindi winner"


def test_planner_refuses_before_any_call_when_the_budget_is_unset(tmp_path):
    fake = FakeMeta()
    client = GraphClient.from_settings("tok", load_settings(), transport=httpx.MockTransport(fake), sleep=lambda x: None, backoff=0)
    with pytest.raises(PlanningError):
        Planner(settings(tmp_path, monthly_budget_inr=0), client, "111", {"c-hi": "Hindi"}, JobStore(tmp_path / "j.db")).run()
    assert fake.reads == []
