import json
import time
from pathlib import Path

import pytest

from metaads.pipeline import ValidationError
from metaads.redo import RedoPlanner
from test_pipeline import Fakes, _pipeline, env  # noqa: F401  (env is a fixture)

HI = {"language": "hi", "spoken_text": "Aisi news video banani hai?"}


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
    assert fakes.calls == before and len(p.store.recent_jobs(10)) == 1


def test_a_bad_image_regenerates_the_chain_but_keeps_the_duration(env):
    fakes, p, rec, job, rp = _made(env)
    plan, done = _run(p, rp, job, ["image"])
    assert fakes.calls == {"image": 2, "video": 2} and fakes.duration_calls == [1]    # no second estimate
    new = p.store.get_job(done["job_id"])
    assert new["redo_of"] == job and new["duration"] == p.store.get_job(job)["duration"]
    assert Path(p.store.get_job(job)["ad_path"]).exists()                           # the old ad is kept
    caption = rec.videos[-1][1]
    assert caption.startswith("Redo · Ad 1/1 · Hindi\nSays: ") and caption.rsplit("\n", 1)[1].startswith("id ")


def test_wrong_poster_moment_reuses_image_clip_and_whisper_and_gives_the_planner_the_feedback(env):
    fakes, p, _, job, rp = _made(env)
    plan = rp.plan(job, ["poster_timing"], poster_feedback="show it earlier, when she says news")
    assert steps(plan["will_redo"]) == ["plan", "render_hook", "merge"]
    assert steps(plan["will_reuse"]) == ["image", "video", "transcribe", "body"]     # the clip carries its length
    assert plan["costs"] == {"gpt_image_calls": 0, "veo_calls": 0, "gemini_calls": 1}
    rp.approve(plan["plan_id"])
    end = time.time() + 60
    while len(p.store.recent_jobs(10)) < 2 or p.store.recent_jobs(10)[0]["status"] != "done":
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
    plan_file = Path(env[0].paths.work_dir) / done["job_id"] / "plan.json"          # the new ad's own plan file
    assert json.loads(plan_file.read_text())["method"] == "manual"
    assert json.loads(plan_file.read_text())["window"] == {"start": 1.0, "end": 2.5}


def test_moving_the_poster_of_an_ad_with_no_transcript_needs_no_whisper_and_no_veo(env):
    fakes = Fakes(no_speech=True)
    p, _ = _pipeline(env, fakes)
    job = p.status(p.submit([HI], chat_id="42", background=False))["ads"][0]["job_id"]
    rp = RedoPlanner(p)
    assert fakes.transcribe_calls == 1 and fakes.calls == {"image": 1, "video": 1}
    plan, done = _run(p, rp, job, ["poster_timing"], poster_start=1.0, poster_end=2.5)
    assert steps(plan["will_redo"]) == ["plan", "render_hook", "merge"] and not plan["notes"]
    assert fakes.transcribe_calls == 1 and fakes.calls == {"image": 1, "video": 1}     # only the hook is rendered again
    plan_file = Path(env[0].paths.work_dir) / done["job_id"] / "plan.json"          # the new ad's own plan file
    assert json.loads(plan_file.read_text())["method"] == "manual"
    assert json.loads(plan_file.read_text())["window"] == {"start": 1.0, "end": 2.5}


def test_a_different_body_only_rejoins_the_existing_hook(env):
    fakes, p, _, job, rp = _made(env)
    plan, done = _run(p, rp, job, ["body"], new_body="teal")
    assert steps(plan["will_redo"]) == ["merge"] and "render_hook" in steps(plan["will_reuse"])
    assert "teal" in Path(p.store.get_job(done["job_id"])["ad_path"]).name
    assert fakes.calls == {"image": 1, "video": 1}
    new = p.store.get_job(done["job_id"])
    assert (new["redo_of"], new["reused_stages"]) == (job, 5)                          # the five hook files are the old ad's
    assert not [f for f in (Path(env[0].paths.work_dir) / done["job_id"]).iterdir()]   # nothing was copied into its folder


def test_new_spoken_text_keeps_the_image_and_estimates_the_duration_again(env):
    fakes, p, _, job, rp = _made(env, thumbnail_text="Short headline")
    plan, done = _run(p, rp, job, ["spoken_text"], new_spoken_text="Ye bilkul nayi news video hai")
    assert steps(plan["will_redo"])[0] == "duration" and "image" not in steps(plan["will_redo"])
    assert steps(plan["will_reuse"]).count("image") == 1
    assert fakes.duration_calls == [1, 1] and fakes.calls == {"image": 1, "video": 2}      # same image, new clip
    new = p.store.get_job(done["job_id"])
    assert new["spoken_text"] == "Ye bilkul nayi news video hai" and new["thumbnail_text"] == "Short headline"
    assert fakes.prompts[-1][0] == "Ye bilkul nayi news video hai"


def test_new_thumbnail_text_makes_a_new_image_and_keeps_the_dialogue_and_duration(env):
    fakes, p, _, job, rp = _made(env)
    plan, done = _run(p, rp, job, ["thumbnail_text"], new_thumbnail_text="Nayi headline")
    assert steps(plan["will_redo"])[0] == "image" and "duration" not in steps(plan["will_redo"])
    assert fakes.duration_calls == [1] and fakes.calls == {"image": 2, "video": 2}
    new = p.store.get_job(done["job_id"])
    assert new["spoken_text"] == HI["spoken_text"] and new["thumbnail_text"] == "Nayi headline"
    assert fakes.image_requests[-1] == ("Nayi headline", "reporter")
    assert fakes.prompts[-1][0] == HI["spoken_text"]                                       # the voice still says the old words


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
    assert d["language"] == "hi" and d["stored_stages"] == ["image", "video", "transcribe", "plan", "render_hook"]   # the final ad is not stored twice
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


def _image_ad(env_, **kw):
    fakes = Fakes()
    p, rec = _pipeline(env_, fakes)
    batch = p.submit([dict(HI, ad_format="image", thumbnail_text="Headline", **kw)], chat_id="42", background=False)
    return fakes, p, rec, p.status(batch)["ads"][0]["job_id"], RedoPlanner(p)


def test_an_image_ad_with_new_thumbnail_text_gets_a_new_image_and_stays_an_image_ad(env):
    fakes, p, rec, job, rp = _image_ad(env)
    plan = rp.plan(job, ["thumbnail_text"], new_thumbnail_text="Nayi headline")
    assert steps(plan["will_redo"]) == ["image_ad"] and plan["costs"] == {"gpt_image_calls": 1, "veo_calls": 0, "gemini_calls": 0}
    done = rp.approve(plan["plan_id"])
    _wait(p, done)
    new = p.store.get_job(done["job_id"])
    assert (new["ad_format"], new["thumbnail_text"], new["spoken_text"], new["redo_of"]) == ("image", "Nayi headline", HI["spoken_text"], job)
    assert fakes.image_requests[-1] == ("Nayi headline", "image_ad") and fakes.calls == {"image": 2, "video": 0}
    assert len(rec.photos) == 2 and "Redo" in rec.photos[-1][1]


def test_an_image_ad_with_new_spoken_text_keeps_its_image_and_only_changes_the_ad_copy(env):
    fakes, p, _, job, rp = _image_ad(env)
    plan = rp.plan(job, ["spoken_text"], new_spoken_text="Ye nayi baat hai")
    assert steps(plan["will_redo"]) == ["ad_copy"] and steps(plan["will_reuse"]) == ["image_ad"]
    assert plan["costs"]["gpt_image_calls"] == 0
    done = rp.approve(plan["plan_id"])
    _wait(p, done)
    new = p.store.get_job(done["job_id"])
    assert new["ad_format"] == "image" and new["spoken_text"] == "Ye nayi baat hai" and new["thumbnail_text"] == "Headline"
    assert fakes.calls == {"image": 1, "video": 0}                                       # the same image file was reused


def test_asking_for_a_body_on_an_image_ad_turns_it_into_a_video_ad(env):
    fakes, p, _, job, rp = _image_ad(env)
    plan = rp.plan(job, ["body"], new_body="teal")
    assert steps(plan["will_redo"])[:2] == ["duration", "image"] and any("image ad" in n for n in plan["notes"])
    done = rp.approve(plan["plan_id"])
    _wait(p, done)
    new = p.store.get_job(done["job_id"])
    assert new["ad_format"] == "video" and "teal" in Path(new["ad_path"]).name and new["thumbnail_text"] == "Headline"
    assert fakes.calls == {"image": 2, "video": 1}                  # a reporter image for the video, plus the old 4:5 one
    assert fakes.duration_calls == [1] and new["duration"] == 4      # the image ad had no clip length: it is estimated now
    assert any("Gemini" in step["what"] for step in plan["will_redo"] if step["step"] == "duration")


def _wait(p, done):
    end = time.time() + 60
    while not p.status(done["batch_id"])["finished"] and time.time() < end:
        time.sleep(0.2)
    assert p.status(done["batch_id"])["done"] == 1
