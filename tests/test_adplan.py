import copy
from datetime import datetime, timedelta, timezone

import pytest

from metaads.adplan import AdPlanError, AdPlans, native_words
from metaads.config import Settings, load_settings
from metaads.jobs import JobStore
from metaads.pipeline import ValidationError

BASE = load_settings()
IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=IST)

HI_SPOKEN = "News reporter हो तो आज ही ये app इस्तेमाल करो!"
TA_SPOKEN = "நீங்கள் news reporter என்றால் இந்த app உங்களுக்கு தான்!"
TA_THUMB = "news reporter என்றால் இந்த app"


def campaign(name, video=0, image=0, proven=()):
    return {"language": name, "new_ads": video + image + sum(len(p["bodies"]) for p in proven),
            "proven_hook_ads": list(proven), "new_hook_ads": video + image, "new_hook_video_ads": video,
            "new_hook_image_ads": image, "proven_hooks_known": len(proven)}


def plan_of(*campaigns, total=None, launch="2026-11-03"):
    return {"launch_day": launch, "budget_left_inr": 123456.0, "monthly_budget_inr": 200000.0,
            "expected_cost_per_new_ad_inr": 430.0, "format_reason": "video ads: 3 of 20 decided ad sets won; image ads: "
            "1 of 4; so 40% of new hooks are image ads", "notes": [],
            "new_ads_total": sum(c["new_ads"] for c in campaigns) if total is None else total,
            "campaigns": list(campaigns)}


class FakePipeline:
    def __init__(self, fail_for=None):
        self.submitted, self.fail_for = [], fail_for

    def submit(self, items, chat_id=None, background=True, review=True):
        if self.fail_for and any(self.fail_for in i["spoken_text"] for i in items):
            raise ValidationError("Daily video cap reached")
        self.submitted.append((items, review))
        return f"b{len(self.submitted)}"


class FakeRedo:
    def __init__(self, fail_for=None):
        self.planned, self.approved, self.fail_for = [], [], fail_for

    def plan(self, job_id, changes, new_body=None, **kw):
        if self.fail_for == job_id:
            raise ValidationError("The ad is not finished")
        self.planned.append((job_id, changes, new_body))
        return {"plan_id": f"r-{job_id}-{new_body}"}

    def approve(self, plan_id):
        self.approved.append(plan_id)
        return {"job_id": f"new-{plan_id}"}


def make(tmp_path, plan, pipeline=None, redo=None, clock=NOON, **settings_changes):
    d = copy.deepcopy(BASE.to_dict())
    d["ad_planning"].update(settings_changes)
    s = Settings(d)
    store = JobStore(tmp_path / "db.sqlite")
    pipeline, redo = pipeline or FakePipeline(), redo or FakeRedo()
    return AdPlans(s, store, lambda: plan, pipeline, redo, lambda: clock), store, pipeline, redo


def hook(language="hi", fmt="video", spoken=HI_SPOKEN, thumb=None, **kw):
    return {"language": language, "ad_format": fmt, "spoken_text": spoken,
            **({"thumbnail_text": thumb} if thumb else {}), **kw}


def tamil(fmt="video", spoken=TA_SPOKEN, thumb=TA_THUMB, **kw):
    return hook("ta", fmt, spoken, thumb, spoken_hindi="अगर आप news reporter हो तो ये app आपके लिए है!",
                thumbnail_hindi="news reporter हो तो ये app", **kw)


def test_the_heartbeat_pings_when_a_plan_is_made_or_not_needed_and_fails_when_meta_cannot_be_read(tmp_path, monkeypatch):
    pings = []
    monkeypatch.setattr("metaads.adplan.ping", lambda url, suffix="", settings=None: pings.append((url, suffix)))
    ap, *_ = make(tmp_path, plan_of(campaign("Hindi", video=1)))
    ap.heartbeat = "https://hc.example/abc"
    assert ap.propose([hook(spoken="हिंदी hook एक, ये app देखो")])["proposed"] is True
    assert ap.propose([])["proposed"] is False and pings == [("https://hc.example/abc", "")]      # errors: no ping
    ap.plan_new_ads = lambda: {**plan_of(campaign("Hindi")), "notes": []}
    ap.propose([])
    assert pings[-1] == ("https://hc.example/abc", "")                                             # nothing to plan: still done
    def broken():
        raise RuntimeError("Meta is down")
    ap.plan_new_ads = broken
    with pytest.raises(RuntimeError):
        ap.propose([])
    assert pings[-1] == ("https://hc.example/abc", "/fail")


def test_native_words_counts_words_with_letters_of_the_script():
    assert native_words("News reporter हो तो यह video", "DEVANAGARI") == 3          # हो, तो, यह
    assert native_words("news video app", "DEVANAGARI") == 0
    assert native_words(TA_SPOKEN, "TAMIL") >= 4 and native_words(TA_SPOKEN, "DEVANAGARI") == 0


def test_a_good_plan_is_stored_and_rendered_with_hindi_in_brackets_only_for_other_languages(tmp_path):
    plan = plan_of(campaign("Hindi", video=1, image=1), campaign("Tamil", video=1))
    ap, store, *_ = make(tmp_path, plan)
    hooks = [hook(), hook(fmt="image", spoken="Reporter का काम आसान, यह app देखो!", thumb="Reporter का काम आसान"), tamil()]
    res = ap.propose(hooks)
    assert res["proposed"] is True and store.latest_ad_plan("pending")["id"] == res["plan_id"]
    msg = res["message"]
    assert "Ad plan for the launch on Tue 03 Nov" in msg and "3 new ads: 2 new-hook videos, 1 new-hook images" in msg
    assert "Rs 123,456 of Rs 200,000" in msg and "40% of new hooks are image ads" in msg
    assert "Hindi (hi): 2 ads" in msg and "Tamil (ta): 1 ad\n" in msg
    assert msg.startswith("📋 Ad plan") and "\n\n2. NEW hook, image ad" in msg and "\n\n3. NEW hook" in msg   # a blank line between ads
    assert "1. NEW hook, video ad\n   Thumbnail and dialogue: " + HI_SPOKEN in msg      # Hindi: no translation in brackets
    assert f"Thumbnail: {TA_THUMB} (हिंदी: news reporter हो तो ये app)" in msg
    assert f"Dialogue: {TA_SPOKEN} (हिंदी: अगर आप news reporter हो तो ये app आपके लिए है!)" in msg
    assert "3. NEW hook, video ad" in msg and "approve" in msg.splitlines()[-1]
    assert "(हिंदी:" not in msg.split("Tamil (ta)")[0]


def test_a_hindi_translation_must_be_in_devanagari_and_not_the_original_text(tmp_path):
    ap, *_ = make(tmp_path, plan_of(campaign("Tamil", video=1)))
    copied = hook("ta", spoken=TA_SPOKEN, thumb=TA_THUMB, spoken_hindi="अगर आप news reporter हो", thumbnail_hindi=TA_THUMB)
    res = ap.propose([copied])
    assert res["proposed"] is False and any("thumbnail_hindi must be a Hindi translation in Devanagari" in e for e in res["errors"])
    latin = hook("ta", spoken=TA_SPOKEN, thumb=TA_THUMB, spoken_hindi="if you are a news reporter", thumbnail_hindi="x")
    assert sum("Devanagari" in e for e in ap.propose([latin])["errors"]) == 2


def test_numbers_run_in_order_down_the_message_whatever_the_language_order(tmp_path):
    plan = plan_of(campaign("Tamil", video=1), campaign("Hindi", video=1))                # the planner lists Tamil first
    ap, *_ = make(tmp_path, plan)
    msg = ap.propose([hook(), tamil()])["message"]
    assert msg.index("Tamil (ta)") < msg.index("Hindi (hi)") and msg.index("1. NEW") < msg.index("2. NEW")


def test_winning_hooks_on_new_bodies_come_from_the_plan_and_need_hindi_for_other_languages(tmp_path):
    proven = {"hook_job_id": "j9", "spoken_text": TA_SPOKEN, "thumbnail_text": TA_THUMB, "results": 61, "cpr_inr": 48.0,
              "bodies": ["teal-darkmint-mintbox", "plum-darkcoral-coralbox"]}
    ap, store, *_ = make(tmp_path, plan_of(campaign("Tamil", proven=[proven])))
    bad = ap.propose([])
    assert bad["proposed"] is False and any("spoken_hindi" in e for e in bad["errors"])
    good = ap.propose([], [{"hook_job_id": "j9", "spoken_hindi": "हिंदी अनुवाद", "thumbnail_hindi": "छोटा अनुवाद"}])
    assert good["proposed"] is True
    items = store.get_ad_plan(good["plan_id"])["items"]
    assert [(i["kind"], i["body"], i["hook_job_id"]) for i in items] == [
        ("proven_hook", "teal-darkmint-mintbox", "j9"), ("proven_hook", "plum-darkcoral-coralbox", "j9")]
    assert "1. WINNING hook (61 results, Rs 48 each) on a new body: teal-darkmint-mintbox" in good["message"]


@pytest.mark.parametrize("hooks,needle", [
    ([hook()], "Hindi image hooks: the plan needs 1, got 0"),
    ([hook(), hook(fmt="image", spoken="दूसरा hook ये है, ये app देखो", thumb="दूसरा hook"), hook(spoken="तीसरा hook यहाँ है, ये app देखो")],
     "Hindi video hooks: the plan needs 1, got 2"),
    ([hook(spoken="news video app only latin letters"), hook(fmt="image", spoken=HI_SPOKEN + " यह दूसरा")], "needs at least 2 words in Devanagari"),
    ([hook(), hook(fmt="image", spoken=HI_SPOKEN)], "appears twice in the plan"),
    ([hook(spoken="News reporter हो तो यह video आपके लिए है! ऐसी news video बनानी सीखनी है?"), hook(fmt="image", spoken="कुछ नया हिंदी hook, देखो ये app")],
     "already exists"),
    ([hook(thumb="only latin thumbnail"), hook(fmt="image", spoken="कुछ नया हिंदी hook, देखो ये app")], "thumbnail text needs at least 1 word"),
])
def test_a_plan_that_does_not_fit_the_rules_is_refused_with_the_reasons_and_nothing_is_stored(tmp_path, hooks, needle):
    ap, store, *_ = make(tmp_path, plan_of(campaign("Hindi", video=1, image=1)))
    res = ap.propose(hooks)
    assert res["proposed"] is False and any(needle in e for e in res["errors"]), res
    assert res["expected_new_hooks"] == {"Hindi video": 1, "Hindi image": 1}
    assert store.latest_ad_plan() is None


def test_a_hook_already_made_is_refused_and_a_missing_hindi_translation_too(tmp_path):
    ap, store, *_ = make(tmp_path, plan_of(campaign("Tamil", video=1)))
    store.create_batch([{"language": "ta", "spoken_text": TA_SPOKEN, "thumbnail_text": TA_THUMB}])
    res = ap.propose([tamil()])
    assert any("already exists" in e for e in res["errors"])
    res = ap.propose([hook("ta", spoken=TA_SPOKEN + " நிச்சயம்", thumb=TA_THUMB, spoken_hindi="x")])
    assert any("thumbnail_hindi (the Hindi translation) is missing" in e for e in res["errors"])
    res = ap.propose([hook("ta", spoken=TA_SPOKEN + " நிச்சயம்")])
    assert any("spoken_hindi" in e for e in res["errors"])


def test_when_the_budget_allows_nothing_there_is_no_plan_and_an_older_one_expires(tmp_path):
    ap, store, *_ = make(tmp_path, plan_of(campaign("Hindi", video=1)))
    first = ap.propose([hook()])["plan_id"]
    ap.plan_new_ads = lambda: {**plan_of(campaign("Hindi")), "notes": ["The budget left does not cover another ad."]}
    res = ap.propose([])
    assert res["proposed"] is False and "No new ads planned" in res["message"] and "does not cover" in res["message"]
    assert store.get_ad_plan(first)["status"] == "superseded" and ap.current()["plan_id"] is None


def test_a_new_plan_supersedes_the_old_one_and_only_the_newest_can_be_decided(tmp_path):
    ap, store, *_ = make(tmp_path, plan_of(campaign("Hindi", video=1)))
    old = ap.propose([hook()])["plan_id"]
    new = ap.propose([hook(spoken="एक बिल्कुल नया hook, ये app देखो")])["plan_id"]
    assert store.get_ad_plan(old)["status"] == "superseded" and ap.current()["plan_id"] == new
    with pytest.raises(AdPlanError, match="superseded"):
        ap.approve(old)
    with pytest.raises(AdPlanError, match="Unknown plan"):
        ap.reject("nope")


def test_approving_makes_new_hooks_in_batches_with_review_and_winning_hooks_as_body_redos(tmp_path):
    proven = {"hook_job_id": "j9", "spoken_text": HI_SPOKEN, "thumbnail_text": HI_SPOKEN, "results": 61, "cpr_inr": 48.0,
              "bodies": ["teal-darkmint-mintbox"]}
    plan = plan_of(campaign("Hindi", video=12, proven=[proven]))
    ap, store, pipe, redo = make(tmp_path, plan)
    ap.s.limits.to_dict()["max_batch_items"] = 10                                    # a plan bigger than one batch is split
    hooks = [hook(spoken=f"नया hook नंबर {i}, ये app देखो", thumb=f"नया {i} hook") for i in range(12)]
    plan_id = ap.propose(hooks)["plan_id"]
    res = ap.approve(plan_id)
    assert res["approved"] is True and res["ads_started"] == 13 and res["errors"] == []
    assert [len(items) for items, _ in pipe.submitted] == [10, 2]                    # the batch size is 10 here
    assert all(review is True for _, review in pipe.submitted)
    assert pipe.submitted[0][0][0] == {"language": "hi", "spoken_text": "नया hook नंबर 0, ये app देखो",
                                       "thumbnail_text": "नया 0 hook", "ad_format": "video"}
    assert redo.planned == [("j9", ["body"], "teal-darkmint-mintbox")] and redo.approved == ["r-j9-teal-darkmint-mintbox"]
    assert store.get_ad_plan(plan_id)["status"] == "approved"
    with pytest.raises(AdPlanError, match="approved"):
        ap.approve(plan_id)                                                           # the approval is used once


def test_image_ads_of_the_plan_are_created_as_image_ads(tmp_path):
    ap, _, pipe, _ = make(tmp_path, plan_of(campaign("Hindi", video=1, image=1)))
    plan_id = ap.propose([hook(), hook(fmt="image", spoken="एक और hook, ये app देखो", thumb="एक और hook")])["plan_id"]
    ap.approve(plan_id)
    assert sorted(i["ad_format"] for i in pipe.submitted[0][0]) == ["image", "video"]


def test_one_failing_part_does_not_stop_the_rest_and_a_plan_that_started_nothing_stays_usable(tmp_path):
    proven = {"hook_job_id": "j9", "spoken_text": HI_SPOKEN, "thumbnail_text": HI_SPOKEN, "results": 61, "cpr_inr": 48.0,
              "bodies": ["teal-darkmint-mintbox"]}
    ap, store, pipe, redo = make(tmp_path, plan_of(campaign("Hindi", video=1, proven=[proven])),
                                 pipeline=FakePipeline(fail_for="BOOM"), redo=FakeRedo())
    plan_id = ap.propose([hook(spoken="BOOM नया hook, ये app देखो")])["plan_id"]
    res = ap.approve(plan_id)
    assert res["approved"] is True and res["ads_started"] == 1 and any("#2" in e or "#1" in e for e in res["errors"])
    assert store.get_ad_plan(plan_id)["status"] == "approved"
    # nothing at all could be started: the plan goes back to waiting
    ap2, store2, *_ = make(tmp_path / "b", plan_of(campaign("Hindi", video=1)), pipeline=FakePipeline(fail_for="BOOM"))
    pid = ap2.propose([hook(spoken="BOOM नया hook, ये app देखो")])["plan_id"]
    res = ap2.approve(pid)
    assert res["approved"] is False and store2.get_ad_plan(pid)["status"] == "pending"


def test_a_plan_cannot_be_approved_once_its_launch_has_started(tmp_path):
    plan = plan_of(campaign("Hindi", video=1))
    ap, store, pipe, _ = make(tmp_path, plan, clock=datetime(2026, 11, 3, 6, 0, tzinfo=IST))
    plan_id = ap.propose([hook()])["plan_id"]
    with pytest.raises(AdPlanError, match="launch of 2026-11-03, which has started"):
        ap.approve(plan_id)
    assert pipe.submitted == [] and store.get_ad_plan(plan_id)["status"] == "pending"
    ap.clock = lambda: datetime(2026, 11, 3, 5, 59, tzinfo=IST)
    assert ap.approve(plan_id)["approved"] is True


def test_revise_changes_texts_keeps_numbers_and_cannot_add_ads(tmp_path):
    proven = {"hook_job_id": "j9", "spoken_text": HI_SPOKEN, "thumbnail_text": HI_SPOKEN, "results": 61, "cpr_inr": 48.0,
              "bodies": ["teal-darkmint-mintbox"]}
    ap, store, *_ = make(tmp_path, plan_of(campaign("Hindi", video=2, proven=[proven])))
    plan_id = ap.propose([hook(spoken="पहला नया hook, ये app देखो"), hook(spoken="दूसरा नया hook, ये app देखो")])["plan_id"]
    assert [i["kind"] for i in store.get_ad_plan(plan_id)["items"]] == ["proven_hook", "new_hook", "new_hook"]   # #1 is the winner
    res = ap.revise(plan_id, replace=[{"n": 3, "spoken_text": "बदला हुआ hook, ये app देखो"}], remove=[1])
    assert res["revised"] is True
    items = store.get_ad_plan(plan_id)["items"]
    assert [i["n"] for i in items] == [2, 3] and items[1]["spoken_text"] == "बदला हुआ hook, ये app देखो"     # numbers stay
    assert items[1]["thumbnail_text"] == items[1]["spoken_text"]                 # it followed the dialogue before, still does
    assert "3. NEW hook, video ad\n   Thumbnail and dialogue: बदला हुआ hook" in res["message"] and "\n1. " not in res["message"]
    assert ap.revise(plan_id, replace=[{"n": 2, "spoken_text": "x y"}])["errors"][0].startswith("#2: the spoken text needs")
    assert "appears twice in the plan" in ap.revise(plan_id, replace=[{"n": 3, "spoken_text": "पहला नया hook, ये app देखो"}])["errors"][0]
    assert "not in the plan" in ap.revise(plan_id, remove=[42])["errors"][0]
    assert "Nothing would be left" in ap.revise(plan_id, remove=[2, 3])["errors"][0]


def test_revising_a_winning_hook_text_is_refused_and_a_changed_text_needs_a_new_translation(tmp_path):
    proven = {"hook_job_id": "j9", "spoken_text": TA_SPOKEN, "thumbnail_text": TA_SPOKEN, "results": 61, "cpr_inr": 48.0,
              "bodies": ["teal-darkmint-mintbox"]}
    ap, store, *_ = make(tmp_path, plan_of(campaign("Tamil", video=1, proven=[proven])))
    plan_id = ap.propose([tamil()], [{"hook_job_id": "j9", "spoken_hindi": "अनुवाद"}])["plan_id"]
    n_new = next(i["n"] for i in store.get_ad_plan(plan_id)["items"] if i["kind"] == "new_hook")
    n_win = next(i["n"] for i in store.get_ad_plan(plan_id)["items"] if i["kind"] == "proven_hook")
    assert "drop it instead" in ap.revise(plan_id, replace=[{"n": n_win, "spoken_text": TA_SPOKEN + " மேலும்"}])["errors"][0]
    res = ap.revise(plan_id, replace=[{"n": n_new, "spoken_text": TA_SPOKEN + " மேலும்"}])
    assert any("spoken_hindi" in e for e in res["errors"])                          # the old translation no longer fits
    res = ap.revise(plan_id, replace=[{"n": n_new, "spoken_text": TA_SPOKEN + " மேலும்", "spoken_hindi": "नया अनुवाद"}])
    assert res["revised"] is True and "(हिंदी: नया अनुवाद)" in res["message"]


def test_reject_and_the_current_plan_view(tmp_path):
    ap, store, pipe, _ = make(tmp_path, plan_of(campaign("Hindi", video=1)))
    assert ap.current()["plan_id"] is None and ap.current()["last_plan"] is None
    plan_id = ap.propose([hook()])["plan_id"]
    cur = ap.current()
    assert cur["plan_id"] == plan_id and cur["message"].startswith("📋 Ad plan for the launch") and cur["items"][0]["n"] == 1
    assert ap.reject(plan_id) == {"rejected": True, "plan_id": plan_id}
    after = ap.current()
    assert after["plan_id"] is None and after["last_plan"]["status"] == "rejected" and pipe.submitted == []


def test_the_hook_brief_has_the_samples_scripts_and_recent_hooks(tmp_path):
    ap, store, *_ = make(tmp_path, plan_of(campaign("Hindi", video=1)))
    store.create_batch([{"language": "hi", "spoken_text": "पुराना hook", "thumbnail_text": "पुराना"}])
    brief = ap.hook_brief()
    assert len(brief["brief"]["sample_hooks"]) == 18 and brief["scripts"]["ta"] == "TAMIL" and brief["min_native_words"] == 2
    assert brief["recent_hooks"]["hi"] == [{"language": "hi", "spoken_text": "पुराना hook", "thumbnail_text": "पुराना"}]
    assert brief["recent_hooks"]["ta"] == [] and "Hindi translation" in brief["hindi_translation_rule"]
    assert all({"thumbnail_text", "spoken_text"} <= set(h) for h in brief["brief"]["sample_hooks"])
