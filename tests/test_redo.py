import json
import time
from pathlib import Path

import pytest

from metaads.pipeline import ValidationError
from metaads.redo import RedoPlanner
from test_pipeline import Fakes, _pipeline, env  # noqa: F401  (env is a fixture)

HI = {"language": "hi", "text": "Aisi news video banani hai?"}


def _made(env_, **kw):
    fakes = Fakes()
    p, rec = _pipeline(env_, fakes)
    batch = p.submit([dict(HI, **kw)], chat_id="42", background=False)
    return fakes, p, rec, p.status(batch)["ads"][0]["job_id"], RedoPlanner(p)


def _run(p, rp, job, changes, **kw):
    plan = rp.plan(job, changes, **kw)
    done = rp.approve(plan["plan_id"])
    end = time.time() + 60
    while not p.status(done["batch_id"])["finished"] and time.time() < end:
        time.sleep(0.2)
    assert p.status(done["batch_id"])["done"] == 1
    return plan, done


def steps(items):
    return [i["step"] for i in items]


def test_a_plan_changes_and_spends_nothing_until_it_is_approved(env):
    fakes, p, _, job, rp = _made(env)
    before = dict(fakes.calls)
    plan = rp.plan(job, ["image"])
    assert steps(plan["will_redo"]) == ["image", "video", "transcribe", "plan", "render_hook", "merge"]
    assert steps(plan["will_reuse"]) == ["duration", "body"]                 # only the clip length can be kept
    assert plan["costs"] == {"gpt_image_calls": 1, "veo_calls": 1, "gemini_calls": 1}
    assert fakes.calls == before and len(p.store.recent_jobs()) == 1


def test_a_bad_image_regenerates_the_chain_but_keeps_the_duration(env):
    fakes, p, rec, job, rp = _made(env)
    plan, done = _run(p, rp, job, ["image"])
    assert fakes.calls == {"image": 2, "video": 2} and fakes.duration_calls == [1]    # no second estimate
    new = p.store.get_job(done["job_id"])
    assert new["redo_of"] == job and new["duration"] == p.store.get_job(job)["duration"]
    assert Path(p.store.get_job(job)["ad_path"]).exists()                           # the old ad is kept
    assert rec.videos[-1][1].startswith("Redo · ")
    assert f" · id {rec.videos[-1][1].rsplit(' · id ', 1)[1]}" in rec.videos[-1][1]


def test_wrong_poster_moment_reuses_image_clip_and_whisper_and_gives_the_planner_the_feedback(env):
    fakes, p, _, job, rp = _made(env)
    plan = rp.plan(job, ["poster_timing"], poster_feedback="show it earlier, when she says news")
    assert steps(plan["will_redo"]) == ["plan", "render_hook", "merge"]
    assert steps(plan["will_reuse"]) == ["image", "video", "transcribe", "body"]     # the clip carries its length
    assert plan["costs"] == {"gpt_image_calls": 0, "veo_calls": 0, "gemini_calls": 1}
    rp.approve(plan["plan_id"])
    end = time.time() + 60
    while len(p.store.recent_jobs()) < 2 or p.store.recent_jobs()[0]["status"] != "done":
        assert time.time() < end
        time.sleep(0.2)
    assert fakes.calls == {"image": 1, "video": 1}                                  # no image, no Veo
    hint = fakes.plan_hints[-1]
    assert "show it earlier" in hint and "earlier choice was the words" in hint


def test_an_exact_poster_window_is_used_without_any_ai_call(env):
    fakes, p, _, job, rp = _made(env)
    calls = len(fakes.plan_hints)
    plan, done = _run(p, rp, job, ["poster_timing"], poster_start=1.0, poster_end=2.5)
    assert "no AI call" in plan["will_redo"][0]["what"] and plan["costs"]["gemini_calls"] == 0
    assert len(fakes.plan_hints) == calls and fakes.calls == {"image": 1, "video": 1}
    meta = json.loads(Path(p.store.get_job(done["job_id"])["ad_path"]).with_suffix(".json").read_text())
    assert meta["plan_method"] == "manual" and meta["poster_window"] == {"start": 1.0, "end": 2.5}


def test_a_different_body_only_rejoins_the_existing_hook(env):
    fakes, p, _, job, rp = _made(env)
    plan, done = _run(p, rp, job, ["body"], new_body="teal")
    assert steps(plan["will_redo"]) == ["merge"] and "render_hook" in steps(plan["will_reuse"])
    assert "teal" in Path(p.store.get_job(done["job_id"])["ad_path"]).name
    assert fakes.calls == {"image": 1, "video": 1}


def test_a_new_text_redoes_everything_and_estimates_the_duration_again(env):
    fakes, p, _, job, rp = _made(env)
    plan, done = _run(p, rp, job, ["text"], new_text="Ye bilkul nayi news video hai")
    assert steps(plan["will_redo"])[0] == "duration" and fakes.duration_calls == [1, 1]
    assert fakes.calls == {"image": 2, "video": 2}
    assert p.store.get_job(done["job_id"])["text"] == "Ye bilkul nayi news video hai"


def test_a_longer_clip_redoes_the_video_but_keeps_the_image(env):
    fakes, p, _, job, rp = _made(env)
    plan, done = _run(p, rp, job, ["duration"], duration=8)
    assert steps(plan["will_redo"]) == ["video", "transcribe", "plan", "render_hook", "merge"]
    assert fakes.calls == {"image": 1, "video": 2} and fakes.prompts[-1][1] == 8


def test_a_missing_stored_file_moves_the_start_back_and_says_so(env):
    s, _ = env
    fakes, p, _, job, rp = _made(env)
    (Path(s.paths.work_dir) / job / "words.json").unlink()
    plan = rp.plan(job, ["poster_timing"])
    assert steps(plan["will_redo"])[0] == "transcribe" and plan["notes"] and "transcribe" in plan["notes"][0]


def test_a_plan_can_be_approved_only_once_and_only_for_finished_ads(env):
    fakes, p, _, job, rp = _made(env)
    plan = rp.plan(job, ["body"], new_body="teal")
    rp.approve(plan["plan_id"])
    with pytest.raises(ValidationError, match="already"):
        rp.approve(plan["plan_id"])
    with pytest.raises(ValidationError, match="Unknown redo plan"):
        rp.approve("rnope")
    p.store.update_job(job, status="running")
    with pytest.raises(ValidationError, match="not finished"):
        rp.plan(job, ["body"], new_body="teal")


def test_bad_requests_are_refused(env):
    fakes, p, _, job, rp = _made(env)
    for kw, msg in (({"changes": []}, "Say what should change"), ({"changes": ["colour"]}, "Unknown change"),
                    ({"changes": ["body"]}, "needs its new value"),
                    ({"changes": ["poster_timing"], "poster_start": 1.0}, "both poster_start"),
                    ({"changes": ["duration"], "duration": 5}, "Duration must be one of")):
        with pytest.raises(ValidationError, match=msg):
            rp.plan(job, **kw)
    with pytest.raises(ValidationError, match="Unknown ad"):
        rp.plan("bnope-1", ["image"])


def test_details_tell_the_bot_what_the_ad_is_made_of(env):
    _, p, _, job, rp = _made(env)
    d = rp.details(job)
    assert d["language"] == "hi" and d["stored_stages"] == ["image", "video", "transcribe", "plan", "render_hook", "merge"]
    assert d["poster"]["to"] > d["poster"]["from"] and d["spoken_words"] and "voice_may_be_cut_off" in d


def test_a_redo_replaces_the_waiting_ad_and_waits_itself(env):
    _, p, _, job, rp = _made(env)
    assert p.store.get_job(job)["review"] == "pending"
    _, done = _run(p, rp, job, ["body"], new_body="teal")
    assert p.store.get_job(job)["review"] == "redone"
    assert p.store.get_job(done["job_id"])["review"] == "pending"


def test_redoing_an_ad_that_was_not_waiting_leaves_its_state_alone(env):
    _, p, _, job, rp = _made(env)
    p.store.set_review([job], "approved")
    _, done = _run(p, rp, job, ["body"], new_body="teal")
    assert p.store.get_job(job)["review"] == "approved"
    assert p.store.get_job(done["job_id"])["review"] == "pending"
