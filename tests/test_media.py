import subprocess

import pytest

from metaads import media
from metaads.config import load_settings
from metaads.media import PosterWindow

S = load_settings()


def _synth(path, color, size, seconds, audio=True, rate=24000, channels=1):
    cmd = [S.paths.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=25:d={seconds}"]
    if audio:
        layout = "mono" if channels == 1 else "stereo"
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
                "-af", f"aformat=channel_layouts={layout}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def _pixel(video, t, tmp_path):
    frame = tmp_path / f"f_{t}.png"
    media.extract_frame(video, t, frame, S)
    raw = subprocess.run([S.paths.ffmpeg, "-loglevel", "error", "-i", str(frame), "-vf", "scale=1:1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return tuple(raw[:3])


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    return {
        "veo": _synth(d / "veo.mp4", "red", "720x1280", 6),
        "poster": _synth(d / "poster.mp4", "lime", "1080x1920", 5, audio=False),
        "body": _synth(d / "body.mp4", "blue", "1080x1920", 6, rate=48000, channels=2),
    }


def test_probe(clips):
    info = media.probe(clips["veo"], S)
    assert (info.width, info.height, info.has_audio) == (720, 1280, True)
    assert 5.8 < info.duration < 6.3
    assert not media.probe(clips["poster"], S).has_audio


def test_poster_speed_clamps():
    w = PosterWindow(1.0, 3.5)           # 2.5 s window, 5 s poster -> 2x
    assert media.poster_speed(5.0, w, S) == pytest.approx(2.0)
    assert media.poster_speed(5.0, PosterWindow(1.0, 1.5), S) == S.poster.max_speed
    assert media.poster_speed(1.0, PosterWindow(0.0, 6.0), S) == S.poster.min_speed


def test_render_hook_shows_poster_only_in_window(clips, tmp_path):
    out = media.render_hook(clips["veo"], clips["poster"], PosterWindow(2.0, 4.0), tmp_path / "hook.mp4", S)
    info = media.probe(out, S)
    assert (info.width, info.height, info.has_audio) == (1080, 1920, True)
    assert info.duration == pytest.approx(media.probe(clips["veo"], S).duration, abs=0.15)
    red = lambda p: p[0] > 150 and p[1] < 100
    green = lambda p: p[1] > 150 and p[0] < 150
    assert red(_pixel(out, 1.0, tmp_path))
    assert green(_pixel(out, 3.0, tmp_path))
    assert red(_pixel(out, 5.0, tmp_path))


def test_merge_ad_crossfades(clips, tmp_path):
    hook = media.render_hook(clips["veo"], clips["poster"], PosterWindow(2.0, 4.0), tmp_path / "hook.mp4", S)
    ad = media.merge_ad(hook, clips["body"], tmp_path / "ad.mp4", S)
    info = media.probe(ad, S)
    expected = media.probe(hook, S).duration + media.probe(clips["body"], S).duration - S.merge.fade_seconds
    assert info.duration == pytest.approx(expected, abs=0.2)
    assert (info.width, info.height, info.has_audio) == (1080, 1920, True)
    # mid-fade the frame is neither pure hook nor pure body color
    mid = media.probe(hook, S).duration - S.merge.fade_seconds / 2
    r, g, b = _pixel(ad, mid, tmp_path)
    assert r > 40 and b > 40


def test_normalize_image(tmp_path):
    src = tmp_path / "a.png"
    subprocess.run([S.paths.ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=1024x1536", "-frames:v", "1", str(src)], check=True)
    out = media.normalize_image(src, tmp_path / "b.png", settings=S)
    info = media.probe(out, S)
    assert (info.width, info.height) == (720, 1280)


def test_normalize_image_pads_with_black_and_never_crops(tmp_path):
    src = tmp_path / "a.png"                       # 2:3 is wider than 9:16 -> fits the width, bars on top and bottom
    subprocess.run([S.paths.ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=1024x1536", "-frames:v", "1", str(src)], check=True)
    out = tmp_path / "b.png"
    media.normalize_image(src, out, settings=S)

    def pixel(x, y):
        raw = subprocess.run([S.paths.ffmpeg, "-loglevel", "error", "-i", str(out), "-vf", f"crop=1:1:{x}:{y}",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
        return tuple(raw)

    assert pixel(0, 640)[0] > 200 and pixel(719, 640)[0] > 200      # full width kept (red at both side edges)
    assert pixel(360, 10) == (0, 0, 0) and pixel(360, 1270) == (0, 0, 0)   # black bars top and bottom
    assert pixel(360, 110)[0] > 200 and pixel(360, 1170)[0] > 200   # image starts right after a 100 px bar


def test_a_gpt_image_size_becomes_exactly_4_by_5_with_black_bars_and_an_exact_one_is_unchanged(tmp_path):
    image_ad = S.images.image_ad
    w, h = image_ad.width, image_ad.height
    assert (w, h) == (1024, 1280) and image_ad.size == "1024x1280"                # the size the API returned exactly 4:5
    for size, bars in (("1024x1536", "sides"), ("1024x1024", "top-bottom"), (f"{w}x{h}", None)):
        src = tmp_path / f"{size}.png"
        subprocess.run([S.paths.ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s={size}",
                        "-frames:v", "1", str(src)], check=True)
        out = tmp_path / f"out-{size}.png"
        media.normalize_image(src, out, w, h, settings=S)
        info = media.probe(out, S)
        assert (info.width, info.height) == (w, h)
        raw = subprocess.run([S.paths.ffmpeg, "-loglevel", "error", "-i", str(out), "-vf", f"crop=1:1:{0}:{h // 2}",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
        left_edge_is_black = tuple(raw) == (0, 0, 0)
        assert left_edge_is_black is (bars == "sides")                          # 2:3 is narrower than 4:5: bars left and right
