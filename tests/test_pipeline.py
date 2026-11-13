import copy
import json
import subprocess
from pathlib import Path

import pytest

from metaads import media
from metaads.config import Settings, load_settings
from metaads.jobs import JobStore
from metaads.pipeline import POSTER_GUESS_NOTE, REVIEW_PROMPT, Pipeline, Services, ValidationError
from metaads.durations import DurationEstimateError
from metaads.planner import plan_poster_window
from metaads.videogen import VideoBlockedError

BASE = load_settings()


def _ff(*args):
    subprocess.run([BASE.paths.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _clip(path, color, size, secs, audio=True):
    args = ["-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d={secs}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={secs}", "-c:a", "aac"]
    _ff(*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))


@pytest.fixture
def env(tmp_path):
    d = copy.deepcopy(BASE.to_dict())
    for k in ("body_dir", "poster_dir", "hooks_dir", "ads_dir", "work_dir"):
        d["paths"][k] = str(tmp_path / k)
    d["paths"]["db"] = str(tmp_path / "db.sqlite")
    d["merge"]["fade_seconds"] = 0.3
    s = Settings(d)
    for lang in ("hi", "ta"):
        (tmp_path / "body_dir" / lang).mkdir(parents=True)
        (tmp_path / "poster_dir" / lang).mkdir(parents=True)
        _clip(tmp_path / "body_dir" / lang / "navy-darkyellow-yellowbox.mp4", "blue", "1080x1920", 3)
        _clip(tmp_path / "body_dir" / lang / "teal-darkmint-mintbox.mp4", "teal", "1080x1920", 3)
        _clip(tmp_path / "poster_dir" / lang / "poster_demo.mp4", "lime", "1080x1920", 5, audio=False)
    return s, tmp_path


class Fakes:
    def __init__(self, fail_image_for=None, video_fail_first=False, block_text=None, durations_fail=False,
                 video_always_fails=False, transcribe_fails=False, no_speech=False):
        self.calls = {"image": 0, "video": 0}
        self.fail_image_for, self.video_fail_first, self.block_text = fail_image_for, video_fail_first, block_text
        self.prompts = []
        self.image_requests = []              # (thumbnail text, kind)
        self.duration_calls = []
        self.plan_hints = []
        self.durations_fail = durations_fail
        self.video_always_fails, self.transcribe_fails = video_always_fails, transcribe_fails
        self.no_speech, self.transcribe_calls = no_speech, 0     # no_speech: Whisper recognizes nothing in any clip

    def services(self, s):
        def image(text, out, kind="reporter"):
            self.calls["image"] += 1
            self.image_requests.append((text, kind))
            if self.fail_image_for and self.fail_image_for in text:
                raise RuntimeError("image API exploded")
            size = "1024x1280" if kind == "image_ad" else "720x1280"
            _ff("-f", "lavfi", "-i", f"color=c=red:s={size}", "-frames:v", "1", str(out))
            return out

        def video(text, lang, out, dur, img):
            self.calls["video"] += 1
            self.prompts.append((text, dur, img))
            if self.block_text and self.block_text in text:
                raise VideoBlockedError("content policy")
            if self.video_fail_first and self.calls["video"] == 1:
                raise RuntimeError("video hiccup")
            if self.video_always_fails:
                raise RuntimeError("Veo is down")
            _clip(out, "red", "720x1280", 4)
            return out

        def transcribe(path, lang):
            self.transcribe_calls += 1
            if self.transcribe_fails:
                raise RuntimeError("whisper crashed")
            if self.no_speech:
                return []
            return [{"i": i, "word": w, "start": 0.3 + i * 0.5, "end": 0.7 + i * 0.5}
                    for i, w in enumerate("aap ko aisi news video banani hai".split())]

        def plan(text, lang, words, dur, hint=None):
            self.plan_hints.append(hint)
            return plan_poster_window(text, lang, words, dur, s, llm=lambda p: {"start_idx": 2, "end_idx": 5},
                                      guidance=hint)

        def durations(items):
            self.duration_calls.append(len(items))
            if self.durations_fail:
                raise DurationEstimateError("Could not estimate the speaking time. Please try again.")
            return [{"speech_seconds": 99.0 if len(it["spoken_text"].split()) > 60 else 3.0,
                     "clip_seconds": 8 if len(it["spoken_text"].split()) > 60 else 4, "attempts": 1} for it in items]

        return Services(image, video, transcribe, plan, durations)


class Recorder:
    def __init__(self):
        self.texts, self.videos, self.photos = [], [], []

    def send_text(self, chat_id, text):
        self.texts.append(text)

    def send_video(self, chat_id, path, caption):
        self.videos.append((str(path), caption))

    def send_photo(self, chat_id, path, caption):
        self.photos.append((str(path), caption))

    send_report = send_text


def _pipeline(env, fakes):
    s, _ = env
    rec = Recorder()
    return Pipeline(s, JobStore(s.paths.db), fakes.services(s), rec), rec


def test_one_ad_per_input_even_for_identical_inputs(env):
    fakes = Fakes()
    p, rec = _pipeline(env, fakes)
    items = [{"language": "hi", "spoken_text": "Aisi news video banani hai?"},
             {"language": "ta", "spoken_text": "Aisi news video banani hai?"},
             {"language": "hi", "spoken_text": "Aisi news video banani hai?", "body": "teal"}]
    batch = p.submit(items, chat_id="42", background=False)
    st = p.status(batch)
    assert (st["total"], st["done"], st["failed"], st["finished"]) == (3, 3, 0, True)
    assert fakes.calls == {"image": 3, "video": 3}                    # nothing shared or skipped
    ads = [Path(a["ad_path"]) for a in st["ads"]]
    assert len({str(a) for a in ads}) == 3 and all(a.exists() for a in ads)
    assert "teal-darkmint-mintbox" in ads[2].name
    assert len(rec.videos) == 3 and rec.texts[-1].startswith("✅ 3 of 3 ads created")
    s, tmp = env
    plan = json.loads((Path(s.paths.work_dir) / st["ads"][0]["job_id"] / "plan.json").read_text())
    assert plan["method"] == "llm" and plan["window"]["end"] > plan["window"]["start"]
    assert (Path(s.paths.ads_dir) / "ta").is_dir()
    # nothing is stored twice: no published hook, no .json beside the ads, and the merged ad moved out of the work folder
    assert not list(Path(s.paths.hooks_dir).rglob("*")) and not list(Path(s.paths.ads_dir).rglob("*.json"))
    assert not list(Path(s.paths.work_dir).rglob("ad*.mp4"))
    info = media.probe(ads[0], s)
    assert (info.width, info.height, info.has_audio) == (1080, 1920, True)


def test_durations_are_requested_only_for_texts_without_one_and_the_estimate_is_logged(env):
    fakes = Fakes()
    p, rec = _pipeline(env, fakes)
    items = [{"language": "hi", "spoken_text": "Aisi news video banani hai?"}, {"language": "ta", "spoken_text": "x y z"},
             {"language": "hi", "spoken_text": "Aisi news video banani hai?", "duration": 8}]      # this one names its length
    batch = p.submit(items, background=False)
    assert fakes.duration_calls == [2]                                # only the two without a duration are sent
    assert sorted(pr[1] for pr in fakes.prompts) == [4, 4, 8]
    job = p.store.get_job(p.status(batch)["ads"][0]["job_id"])
    assert json.loads(job["estimate"])["speech_seconds"] == 3.0
    s, _ = env
    assert json.loads((Path(s.paths.work_dir) / job["id"] / "words.json").read_text())[-1]["end"] > 0
    assert not any("cut off" in caption for _, caption in rec.videos)


def test_failure_in_one_item_does_not_stop_the_others(env):
    fakes = Fakes(block_text="BOOM")
    p, rec = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "spoken_text": "ok one"}, {"language": "hi", "spoken_text": "BOOM two"},
                      {"language": "ta", "spoken_text": "ok three"}], background=False)
    st = p.status(batch)
    assert (st["done"], st["failed"]) == (2, 1)                       # the blocked item failed; it did not become an image ad
    assert [a["status"] for a in st["ads"]] == ["done", "failed", "done"]
    assert [a["ad_format"] for a in st["ads"]] == ["video", "video", "video"]
    assert fakes.calls["video"] == 4 and fakes.calls["image"] == 3    # the blocked item was retried once (its image kept), no image ad made
    assert "content policy" in st["ads"][1]["error"]
    assert rec.texts[-1].startswith("⚠️ 2 of 3 ads created") and "image ads" not in rec.texts[-1]


def test_failure_in_a_stage_other_than_the_video_is_still_a_failure(env):
    fakes = Fakes(transcribe_fails=True)
    p, rec = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "spoken_text": "ok one"}], background=False)
    st = p.status(batch)
    assert (st["done"], st["failed"]) == (0, 1) and "whisper crashed" in st["ads"][0]["error"]
    assert not rec.photos


def test_a_clip_with_no_recognized_speech_gets_a_guessed_poster_window_on_the_last_attempt(env):
    fakes = Fakes(no_speech=True)
    p, rec = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "spoken_text": "ok one"}], chat_id="42", background=False)
    st = p.status(batch)["ads"][0]
    job = p.store.get_job(st["job_id"])
    assert (job["status"], job["attempts"]) == ("done", 2) and fakes.calls["video"] == 1   # no new Veo clip
    assert fakes.transcribe_calls == 1                                # the last attempt does not ask Whisper again
    s, _ = env
    work = Path(s.paths.work_dir) / st["job_id"]
    assert json.loads((work / "words.json").read_text()) == []        # recorded, so a redo does not transcribe again
    plan = json.loads((work / "plan.json").read_text())
    assert plan["method"] == "default" and plan["window"]["start"] == pytest.approx(4 * s.poster.fallback_start_share)
    caption = rec.videos[0][1]
    assert POSTER_GUESS_NOTE in caption and caption.rstrip().endswith(f"id {st['job_id']}")


def test_an_empty_transcript_is_not_cached_so_each_earlier_attempt_asks_whisper_again(env):
    s, _ = env
    s.to_dict()["limits"]["job_attempts"] = 3
    fakes = Fakes(no_speech=True)
    p, _ = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "spoken_text": "ok one"}], background=False)
    assert p.status(batch)["ads"][0]["status"] == "done"
    assert fakes.transcribe_calls == 2                                # attempts 1 and 2 asked; attempt 3 skipped Whisper


def test_when_the_hook_video_fails_even_after_the_retry_the_ad_fails_and_no_image_ad_is_made(env):
    fakes = Fakes(video_always_fails=True)
    p, rec = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "spoken_text": "Dialogue words", "thumbnail_text": "Headline"}],
                     chat_id="42", background=False)
    st = p.status(batch)["ads"][0]
    assert fakes.calls["video"] == 2                                  # tried, retried, then gave up
    job = p.store.get_job(st["job_id"])
    assert (job["status"], job["ad_format"], job["stage"]) == ("failed", "video", "video") and "Veo is down" in job["error"]
    assert fakes.image_requests == [("Headline", "reporter")]         # no image ad was made
    assert not rec.photos and not rec.videos and "Veo is down" in rec.texts[-1]


def test_an_image_ad_can_be_asked_for_and_makes_no_video_duration_or_cap_use(env):
    fakes = Fakes()
    p, rec = _pipeline(env, fakes)
    batch = p.submit([{"language": "ta", "spoken_text": "Dialogue words", "thumbnail_text": "Headline", "ad_format": "image"}],
                     background=False)
    job = p.store.get_job(p.status(batch)["ads"][0]["job_id"])
    assert (job["status"], job["ad_format"], job["duration"]) == ("done", "image", None)
    assert fakes.calls == {"image": 1, "video": 0} and fakes.duration_calls == []
    assert fakes.image_requests == [("Headline", "image_ad")] and p.store.video_calls_today() == 0
    assert len(rec.photos) == 1 and rec.photos[0][1].startswith("🖼 Image ad · Ad 1/1 · Tamil\nSays: Dialogue words\nThumbnail: Headline\nid ")


def test_the_thumbnail_text_goes_to_the_image_and_the_spoken_text_to_the_voice(env):
    fakes = Fakes()
    p, _ = _pipeline(env, fakes)
    p.submit([{"language": "hi", "spoken_text": "What she says", "thumbnail_text": "What the image says"},
              {"language": "hi", "spoken_text": "Same words"}], background=False)
    assert sorted(fakes.image_requests) == [("Same words", "reporter"), ("What the image says", "reporter")]
    assert sorted(pr[0] for pr in fakes.prompts) == ["Same words", "What she says"]
    jobs = p.store.recent_jobs(10)
    assert sorted((j["spoken_text"], j["thumbnail_text"]) for j in jobs) == [
        ("Same words", "Same words"), ("What she says", "What the image says")]


def test_the_daily_video_cap_counts_only_video_ads(env):
    s, _ = env
    d = copy.deepcopy(s.to_dict())
    d["limits"]["daily_video_cap"] = 1
    p = Pipeline(Settings(d), JobStore(s.paths.db), Fakes().services(s), Recorder())
    p.submit([{"language": "hi", "spoken_text": "a", "ad_format": "image"}] * 3 + [{"language": "hi", "spoken_text": "b"}],
             background=False)
    with pytest.raises(ValidationError, match="Daily video cap"):
        p.submit([{"language": "hi", "spoken_text": "c"}], background=False)


def test_an_old_database_with_one_text_column_gets_both_new_ones(tmp_path):
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, batch_id TEXT, idx INTEGER, language TEXT, text TEXT,"
                        " body TEXT, duration INTEGER, status TEXT, stage TEXT, error TEXT, hook_path TEXT,"
                        " ad_path TEXT, attempts INTEGER DEFAULT 0, created REAL, updated REAL, estimate TEXT);"
                        "INSERT INTO jobs (id, batch_id, idx, language, text, status) VALUES ('j', 'b', 0, 'hi', 'old words', 'done');")
    store = JobStore(db)
    job = store.get_job("j")
    assert (job["spoken_text"], job["thumbnail_text"], job["ad_format"]) == ("old words", "old words", "video")
    assert "text" not in job and store.hook_body_pairs() == {("hi", "old words", "old words", None)}


def test_retry_resumes_without_repeating_the_image_step(env):
    s, tmp = env
    d = copy.deepcopy(s.to_dict())
    fakes = Fakes(video_fail_first=True)
    p = Pipeline(Settings(d), JobStore(s.paths.db), fakes.services(Settings(d)), Recorder())
    batch = p.submit([{"language": "hi", "spoken_text": "retry me"}], background=False)
    assert p.status(batch)["done"] == 1
    assert fakes.calls == {"image": 1, "video": 2}                    # image kept, only the failed step redone
    assert fakes.prompts[-1][2] is not None                           # the image reached the video step


def test_transient_video_error_is_retried_once(env):
    fakes = Fakes(video_fail_first=True)
    p, _ = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "spoken_text": "retry me"}], background=False)
    assert p.status(batch)["done"] == 1 and fakes.calls["video"] == 2


def test_validation_is_all_or_nothing(env):
    p, _ = _pipeline(env, Fakes())
    with pytest.raises(ValidationError, match="Unsupported language 'xx'"):
        p.submit([{"language": "hi", "spoken_text": "fine"}, {"language": "xx", "spoken_text": "bad"}], background=False)
    with pytest.raises(ValidationError, match="not found"):
        p.submit([{"language": "hi", "spoken_text": "fine", "body": "purple"}], background=False)
    with pytest.raises(ValidationError, match="At most"):
        p.submit([{"language": "hi", "spoken_text": "x"}] * (BASE.limits.max_batch_items + 1), background=False)
    assert p.store.video_calls_since(0) == 0                           # nothing ran


def test_duration_failure_refuses_the_batch_before_anything_is_generated(env):
    fakes = Fakes(durations_fail=True)
    p, _ = _pipeline(env, fakes)
    with pytest.raises(ValidationError, match="Could not estimate"):
        p.submit([{"language": "hi", "spoken_text": "a"}, {"language": "ta", "spoken_text": "b"}], background=False)
    assert fakes.calls == {"image": 0, "video": 0} and fakes.duration_calls == [2]


def test_daily_video_cap_refuses_whole_batch(env):
    s, _ = env
    d = copy.deepcopy(s.to_dict())
    d["limits"]["daily_video_cap"] = 2
    p = Pipeline(Settings(d), JobStore(s.paths.db), Fakes().services(s), Recorder())
    with pytest.raises(ValidationError, match="Daily video cap"):
        p.submit([{"language": "hi", "spoken_text": "a"}] * 3, background=False)


# ---------- recovery after a restart, stuck-job report, timeouts ----------
import sqlite3
import time

from metaads import pipeline as pipeline_mod

DEAD = f"999999999@{pipeline_mod._boot_id()}:dead"          # a server process that no longer exists


def _orphan(p, stage="video", owner=DEAD, status="running", updated_ago=0):
    """A job a previous server left behind, as if it died while the job was at `stage`."""
    batch, ids = p.store.create_batch([{"language": "hi", "spoken_text": "Aisi news video banani hai?", "thumbnail_text": "Aisi news video banani hai?", "duration": 4}],
                                      "42", owner)
    p.store.update_job(ids[0], status=status, stage=stage)
    if updated_ago:
        with sqlite3.connect(p.store.path) as c:
            c.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - updated_ago, ids[0]))
    return batch, ids[0]


def _wait_finished(p, batch, seconds=60):
    end = time.time() + seconds
    while not p.status(batch)["finished"] and time.time() < end:
        time.sleep(0.2)
    assert p.status(batch)["finished"]


def test_jobs_left_by_a_dead_server_are_resumed_from_their_cached_stages(env):
    s, _ = env
    fakes = Fakes()
    p, rec = _pipeline(env, fakes)
    batch, job = _orphan(p)
    work = Path(s.paths.work_dir) / job
    work.mkdir(parents=True)
    _ff("-f", "lavfi", "-i", "color=c=red:s=720x1280", "-frames:v", "1", str(work / "image.png"))   # paid for already
    assert p.recover() == [job]
    _wait_finished(p, batch)
    assert p.status(batch)["done"] == 1
    assert fakes.calls == {"image": 0, "video": 1}                     # the cached image was not paid for again
    assert any("Resuming: Ad 1 (Hindi)" in t for t in rec.texts) and len(rec.videos) == 1
    assert p.store.get_job(job)["owner"] == p.owner


def test_jobs_of_a_live_server_are_left_alone_and_a_claim_is_atomic(env):
    p, _ = _pipeline(env, Fakes())
    _, mine = _orphan(p, owner=p.owner)
    batch, theirs = _orphan(p, owner=DEAD)
    assert p.store.claim_job(theirs, DEAD, "me") is True
    assert p.store.claim_job(theirs, DEAD, "someone else") is False     # the second server loses the race
    p.store.update_job(theirs, owner=DEAD)
    assert p.recover() == [theirs]                                       # only the dead server's job is taken
    _wait_finished(p, batch)
    assert p.store.get_job(mine)["status"] == "running" and p.store.get_job(mine)["stage"] == "video"


def test_finished_and_failed_jobs_are_not_resumed(env):
    p, _ = _pipeline(env, Fakes())
    _, a = _orphan(p, status="done")
    _, b = _orphan(p, status="failed")
    assert p.recover() == []


def test_a_job_with_no_progress_is_reported_once(env):
    p, rec = _pipeline(env, Fakes())
    _, stale = _orphan(p, owner=p.owner, updated_ago=26 * 60)
    _, fresh = _orphan(p, owner=p.owner, updated_ago=60)
    _, queued = _orphan(p, owner=p.owner, status="queued", updated_ago=60 * 60)
    assert p.check_stuck() == [stale]
    assert "Ad 1 (Hindi) may be stuck" in rec.texts[-1] and "stage 'video'" in rec.texts[-1] and "26 min" in rec.texts[-1]
    assert p.check_stuck() == []                                        # not repeated for the same stage
    p.store.update_job(stale, stage="transcribe")                       # progress, then stuck again: reported again
    with sqlite3.connect(p.store.path) as c:
        c.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 30 * 60, stale))
    assert p.check_stuck() == [stale]


def test_a_hung_ffmpeg_call_fails_instead_of_blocking_forever():
    with pytest.raises(media.MediaError, match="did not finish within"):
        media._run(["sleep", "5"], timeout=0.2)
    assert all(v > 0 for v in BASE.timeouts.to_dict().values())


def test_an_old_database_gets_the_owner_column(tmp_path):
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, batch_id TEXT, idx INTEGER, language TEXT, text TEXT,"
                        " body TEXT, duration INTEGER, status TEXT, stage TEXT, error TEXT, hook_path TEXT,"
                        " ad_path TEXT, attempts INTEGER DEFAULT 0, created REAL, updated REAL, estimate TEXT);")
    JobStore(db)
    with sqlite3.connect(db) as c:
        assert "owner" in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}


def test_new_ads_wait_for_the_owner_and_the_summary_asks_for_a_decision(env):
    fakes = Fakes(fail_image_for="BOOM")
    p, rec = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "spoken_text": "ok one"}, {"language": "hi", "spoken_text": "BOOM two"}], background=False)
    waiting = p.store.awaiting_review_jobs()
    assert [j["id"] for j in waiting] == [f"{batch}-1"]               # the failed ad is not waiting for a decision
    assert rec.texts[-1].endswith(REVIEW_PROMPT)


def test_without_review_nothing_waits_and_nobody_is_asked(env):
    p, rec = _pipeline(env, Fakes())
    p.submit([{"language": "hi", "spoken_text": "ok one"}], background=False, review=False)
    assert p.store.awaiting_review_jobs() == [] and REVIEW_PROMPT not in rec.texts[-1]


def test_a_summary_with_only_failures_does_not_ask_for_a_decision(env):
    p, rec = _pipeline(env, Fakes(fail_image_for="BOOM"))
    p.submit([{"language": "hi", "spoken_text": "BOOM one"}], background=False)
    assert REVIEW_PROMPT not in rec.texts[-1]


def test_set_review_stamps_the_decision_time_and_rejects_unknown_states(env):
    p, _ = _pipeline(env, Fakes())
    batch = p.submit([{"language": "hi", "spoken_text": "ok one"}], background=False)
    job = f"{batch}-1"
    assert p.store.get_job(job)["reviewed"] is None
    p.store.set_review([job], "dropped")
    row = p.store.get_job(job)
    assert row["review"] == "dropped" and row["reviewed"] and p.store.awaiting_review_jobs() == []
    with pytest.raises(ValueError):
        p.store.set_review([job], "maybe")
