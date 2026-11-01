import copy
import json
import subprocess
from pathlib import Path

import pytest

from adhook import media
from adhook.config import Settings, load_settings
from adhook.jobs import JobStore
from adhook.pipeline import Pipeline, Services, ValidationError
from adhook.durations import DurationEstimateError
from adhook.planner import plan_poster_window
from adhook.videogen import VideoBlockedError

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
    def __init__(self, fail_image_for=None, video_fail_first=False, block_text=None, durations_fail=False):
        self.calls = {"image": 0, "video": 0}
        self.fail_image_for, self.video_fail_first, self.block_text = fail_image_for, video_fail_first, block_text
        self.prompts = []
        self.duration_calls = []
        self.plan_hints = []
        self.durations_fail = durations_fail

    def services(self, s):
        def image(text, out):
            self.calls["image"] += 1
            if self.fail_image_for and self.fail_image_for in text:
                raise RuntimeError("image API exploded")
            _ff("-f", "lavfi", "-i", "color=c=red:s=720x1280", "-frames:v", "1", str(out))
            return out

        def video(text, lang, out, dur, img):
            self.calls["video"] += 1
            self.prompts.append((text, dur, img))
            if self.block_text and self.block_text in text:
                raise VideoBlockedError("content policy")
            if self.video_fail_first and self.calls["video"] == 1:
                raise RuntimeError("video hiccup")
            _clip(out, "red", "720x1280", 4)
            return out

        def transcribe(path, lang):
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
            return [{"speech_seconds": 99.0 if len(it["text"].split()) > 60 else 3.0,
                     "clip_seconds": 8 if len(it["text"].split()) > 60 else 4, "attempts": 1} for it in items]

        return Services(image, video, transcribe, plan, durations)


class Recorder:
    def __init__(self):
        self.texts, self.videos = [], []

    def send_text(self, chat_id, text):
        self.texts.append(text)

    def send_video(self, chat_id, path, caption):
        self.videos.append((str(path), caption))

    send_report = send_text


def _pipeline(env, fakes):
    s, _ = env
    rec = Recorder()
    return Pipeline(s, JobStore(s.paths.db), fakes.services(s), rec), rec


def test_one_ad_per_input_even_for_identical_inputs(env):
    fakes = Fakes()
    p, rec = _pipeline(env, fakes)
    items = [{"language": "hi", "text": "Aisi news video banani hai?"},
             {"language": "ta", "text": "Aisi news video banani hai?"},
             {"language": "hi", "text": "Aisi news video banani hai?", "body": "teal"}]
    batch = p.submit(items, chat_id="42", background=False)
    st = p.status(batch)
    assert (st["total"], st["done"], st["failed"], st["finished"]) == (3, 3, 0, True)
    assert fakes.calls == {"image": 3, "video": 3}                    # nothing shared or skipped
    ads = [Path(a["ad_path"]) for a in st["ads"]]
    assert len({str(a) for a in ads}) == 3 and all(a.exists() for a in ads)
    assert "teal-darkmint-mintbox" in ads[2].name
    assert len(rec.videos) == 3 and rec.texts[-1].startswith("3 of 3 ads created")
    s, tmp = env
    meta = json.loads(ads[0].with_suffix(".json").read_text())
    assert meta["language"] == "hi" and meta["plan_method"] == "llm" and meta["poster_window"]["end"] > meta["poster_window"]["start"]
    assert (Path(s.paths.hooks_dir) / "hi").is_dir() and (Path(s.paths.ads_dir) / "ta").is_dir()
    info = media.probe(ads[0], s)
    assert (info.width, info.height, info.has_audio) == (1080, 1920, True)


def test_durations_are_requested_only_for_texts_without_one_and_the_estimate_is_logged(env):
    fakes = Fakes()
    p, _ = _pipeline(env, fakes)
    items = [{"language": "hi", "text": "Aisi news video banani hai?"}, {"language": "ta", "text": "x y z"},
             {"language": "hi", "text": "Aisi news video banani hai?", "duration": 8}]      # this one names its length
    batch = p.submit(items, background=False)
    assert fakes.duration_calls == [2]                                # only the two without a duration are sent
    assert sorted(pr[1] for pr in fakes.prompts) == [4, 4, 8]
    ad = p.status(batch)["ads"][0]["ad_path"]
    meta = json.loads(Path(ad).with_suffix(".json").read_text())
    assert meta["duration_estimate"]["speech_seconds"] == 3.0 and meta["measured_speech_end"] > 0
    assert meta["speech_may_be_cut_off"] is False


def test_failure_in_one_item_does_not_stop_the_others(env):
    fakes = Fakes(block_text="BOOM")
    p, rec = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "text": "ok one"}, {"language": "hi", "text": "BOOM two"},
                      {"language": "ta", "text": "ok three"}], background=False)
    st = p.status(batch)
    assert (st["done"], st["failed"]) == (2, 1)
    assert "content policy" in st["ads"][1]["error"]
    assert fakes.calls["video"] == 3                                  # the blocked item was NOT retried
    assert rec.texts[-1].startswith("2 of 3 ads created") and "Ad 2" in rec.texts[-1]


def test_retry_resumes_without_repeating_the_image_step(env):
    s, tmp = env
    d = copy.deepcopy(s.to_dict())
    fakes = Fakes(video_fail_first=True)
    p = Pipeline(Settings(d), JobStore(s.paths.db), fakes.services(Settings(d)), Recorder())
    batch = p.submit([{"language": "hi", "text": "retry me"}], background=False)
    assert p.status(batch)["done"] == 1
    assert fakes.calls == {"image": 1, "video": 2}                    # image kept, only the failed step redone
    assert fakes.prompts[-1][2] is not None                           # the image reached the video step


def test_transient_video_error_is_retried_once(env):
    fakes = Fakes(video_fail_first=True)
    p, _ = _pipeline(env, fakes)
    batch = p.submit([{"language": "hi", "text": "retry me"}], background=False)
    assert p.status(batch)["done"] == 1 and fakes.calls["video"] == 2


def test_validation_is_all_or_nothing(env):
    p, _ = _pipeline(env, Fakes())
    with pytest.raises(ValidationError, match="Unsupported language 'xx'"):
        p.submit([{"language": "hi", "text": "fine"}, {"language": "xx", "text": "bad"}], background=False)
    with pytest.raises(ValidationError, match="not found"):
        p.submit([{"language": "hi", "text": "fine", "body": "purple"}], background=False)
    with pytest.raises(ValidationError, match="At most"):
        p.submit([{"language": "hi", "text": "x"}] * 11, background=False)
    assert p.store.video_calls_since(0) == 0                           # nothing ran


def test_duration_failure_refuses_the_batch_before_anything_is_generated(env):
    fakes = Fakes(durations_fail=True)
    p, _ = _pipeline(env, fakes)
    with pytest.raises(ValidationError, match="Could not estimate"):
        p.submit([{"language": "hi", "text": "a"}, {"language": "ta", "text": "b"}], background=False)
    assert fakes.calls == {"image": 0, "video": 0} and fakes.duration_calls == [2]


def test_daily_video_cap_refuses_whole_batch(env):
    s, _ = env
    d = copy.deepcopy(s.to_dict())
    d["limits"]["daily_video_cap"] = 2
    p = Pipeline(Settings(d), JobStore(s.paths.db), Fakes().services(s), Recorder())
    with pytest.raises(ValidationError, match="Daily video cap"):
        p.submit([{"language": "hi", "text": "a"}] * 3, background=False)


# ---------- recovery after a restart, stuck-job report, timeouts ----------
import sqlite3
import time

from adhook import pipeline as pipeline_mod

DEAD = f"999999999@{pipeline_mod._boot_id()}:dead"          # a server process that no longer exists


def _orphan(p, stage="video", owner=DEAD, status="running", updated_ago=0):
    """A job a previous server left behind, as if it died while the job was at `stage`."""
    batch, ids = p.store.create_batch([{"language": "hi", "text": "Aisi news video banani hai?", "duration": 4}],
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
    assert any("Resuming: Ad 1 (hi)" in t for t in rec.texts) and len(rec.videos) == 1
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
    assert "Ad 1 (hi)" in rec.texts[-1] and "stage 'video'" in rec.texts[-1] and "26 min" in rec.texts[-1]
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
