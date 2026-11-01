import pytest

from metaads import videogen
from metaads.config import load_settings
from metaads.ratelimit import RateLimiter
from metaads.videogen import VideoBlockedError, VideoGenerationError, generate_video

BASE = load_settings()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(videogen.time, "sleep", lambda s: None)


# ---------------- Veo on Vertex ----------------

class FakeOp:
    def __init__(self, done, response=None, error=None):
        self.done, self.response, self.error = done, response, error


def _video_response(data=b"MP4DATA"):
    video = type("Vid", (), {"video_bytes": data})()
    return type("Resp", (), {"generated_videos": [type("GV", (), {"video": video})()]})()


def _fake_vertex(monkeypatch, errors=None, final=None):
    seen = {"calls": 0}
    errors = list(errors or [])

    class Models:
        def generate_videos(self, **kw):
            seen["calls"] += 1
            seen["kw"] = kw
            if errors:
                raise errors.pop(0)
            return FakeOp(False)

    class Operations:
        def __init__(self):
            self.polls = 0

        def get(self, op):
            self.polls += 1
            seen["polls"] = self.polls
            return final or FakeOp(True, _video_response())

    class Client:
        models, operations = Models(), Operations()

    monkeypatch.setattr(videogen, "vertex_client", lambda s: Client())
    return seen


def _png(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNGbytes")
    return p


def test_vertex_sends_image_prompt_and_settings(monkeypatch, tmp_path):
    seen = _fake_vertex(monkeypatch)
    text = "Aisi news video banani hai?"
    out = generate_video(text, "hi", tmp_path / "o.mp4", 6, _png(tmp_path), BASE, RateLimiter(1000))
    kw = seen["kw"]
    assert out.read_bytes() == b"MP4DATA" and seen["polls"] == 1       # polled until done
    assert kw["model"] == "veo-3.1-lite-generate-001"
    assert kw["prompt"] == 'The reporter in image speaks "Aisi news video banani hai?" in Hindi'   # exact wording
    assert kw["image"].image_bytes == b"\x89PNGbytes" and kw["image"].mime_type == "image/png"
    c = kw["config"]
    assert (c.aspect_ratio, c.resolution, c.duration_seconds, c.number_of_videos, c.generate_audio) == ("9:16", "720p", 6, 1, True)


def test_vertex_rate_limit_is_retried(monkeypatch, tmp_path):
    class E(Exception):
        code = 429
    seen = _fake_vertex(monkeypatch, errors=[E("RESOURCE_EXHAUSTED"), E("RESOURCE_EXHAUSTED")])
    generate_video("x", "hi", tmp_path / "o.mp4", 4, _png(tmp_path), BASE, RateLimiter(1000))
    assert seen["calls"] == 3


def test_vertex_other_error_is_not_retried(monkeypatch, tmp_path):
    seen = _fake_vertex(monkeypatch, errors=[RuntimeError("boom")])
    with pytest.raises(VideoGenerationError, match="boom"):
        generate_video("x", "hi", tmp_path / "o.mp4", 4, _png(tmp_path), BASE, RateLimiter(1000))
    assert seen["calls"] == 1


def test_vertex_safety_filtered_result_is_final(monkeypatch, tmp_path):
    empty = type("R", (), {"generated_videos": [], "rai_media_filtered_reasons": ["face"]})()
    _fake_vertex(monkeypatch, final=FakeOp(True, empty))
    with pytest.raises(VideoBlockedError) as e:
        generate_video("x", "hi", tmp_path / "o.mp4", 4, _png(tmp_path), BASE, RateLimiter(1000))
    assert e.value.retryable is False


def test_vertex_needs_the_image(tmp_path):
    with pytest.raises(VideoGenerationError, match="needs the thumbnail"):
        generate_video("x", "hi", tmp_path / "o.mp4", 4, None, BASE, RateLimiter(1000))


def test_vertex_client_requires_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        videogen.vertex_client(BASE)
