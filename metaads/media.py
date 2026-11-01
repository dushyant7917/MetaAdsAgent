"""All video/audio/image processing, done with ffmpeg and ffprobe."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, load_settings


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool


@dataclass(frozen=True)
class PosterWindow:
    start: float   # seconds in the hook timeline
    end: float


def _run(cmd: list[str], timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError(f"{Path(cmd[0]).name} did not finish within {timeout:.0f}s") from None
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise MediaError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc.stdout


def probe(path: str | Path, settings: Settings | None = None) -> MediaInfo:
    s = settings or load_settings()
    out = _run([s.paths.ffprobe, "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", str(path)], s.timeouts.ffmpeg)
    data = json.loads(out)
    video = next((x for x in data["streams"] if x["codec_type"] == "video"), None)
    audio = next((x for x in data["streams"] if x["codec_type"] == "audio"), None)
    fps = None
    if video and video.get("r_frame_rate", "0/0") not in ("0/0", ""):
        num, den = video["r_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else None
    duration = float(data["format"].get("duration") or (video or audio or {}).get("duration") or 0)
    return MediaInfo(duration, video and video.get("width"), video and video.get("height"), fps, audio is not None)


def _ffmpeg(args: list[str], settings: Settings) -> None:
    _run([settings.paths.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], settings.timeouts.ffmpeg)


def _encode_args(s: Settings) -> list[str]:
    v = s.video
    return ["-c:v", "libx264", "-preset", v.preset, "-crf", str(v.crf), "-pix_fmt", "yuv420p",
            "-r", str(v.fps), "-c:a", "aac", "-b:a", v.audio_bitrate, "-ar", str(v.audio_rate),
            "-ac", "2", "-movflags", "+faststart"]


def extract_audio(video: str | Path, wav: str | Path, settings: Settings | None = None) -> Path:
    """16 kHz mono wav, the input format Whisper wants."""
    s = settings or load_settings()
    _ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], s)
    return Path(wav)


def normalize_image(src: str | Path, dst: str | Path, width: int = 720, height: int = 1280,
                    settings: Settings | None = None) -> Path:
    """Scale to fit inside width x height (9:16 by default) and pad the short side with black; nothing is cropped."""
    s = settings or load_settings()
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    _ffmpeg(["-i", str(src), "-vf", vf, "-frames:v", "1", str(dst)], s)
    return Path(dst)


def poster_speed(poster_duration: float, window: PosterWindow, settings: Settings) -> float:
    """Speed factor that fits the whole poster into the window, clamped to the configured range."""
    wanted = poster_duration / max(window.end - window.start, 0.1)
    return min(max(wanted, settings.poster.min_speed), settings.poster.max_speed)


def render_hook(source_clip: str | Path, poster: str | Path, window: PosterWindow, out: str | Path,
                settings: Settings | None = None) -> Path:
    """Hook = generated clip (voice-over plays throughout) with the muted poster shown full-frame in `window`.

    The whole poster is retimed to fit the window; if the speed clamp stops it fitting exactly,
    it is cut at the window end (too long) or holds its last frame (too short).
    """
    s = settings or load_settings()
    clip, post = probe(source_clip, s), probe(poster, s)
    w, h, fps = s.video.width, s.video.height, s.video.fps
    speed = poster_speed(post.duration, window, s)
    length = window.end - window.start
    graph = (
        f"[0:v]fps={fps},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p[base];"
        f"[1:v]fps={fps},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p,"
        f"setpts=(PTS-STARTPTS)/{speed:.6f},tpad=stop_mode=clone:stop_duration={length:.3f},"
        f"setpts=PTS+{window.start:.3f}/TB[post];"
        f"[base][post]overlay=enable='between(t,{window.start:.3f},{window.end:.3f})':eof_action=pass[v];"
        f"[0:a]loudnorm=I={s.video.loudness_target}:TP=-1.5:LRA=11,aresample={s.video.audio_rate},"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
    )
    _ffmpeg(["-i", str(source_clip), "-i", str(poster), "-filter_complex", graph,
             "-map", "[v]", "-map", "[a]", "-t", f"{clip.duration:.3f}", *_encode_args(s), str(out)], s)
    return Path(out)


def merge_ad(hook: str | Path, body: str | Path, out: str | Path,
             settings: Settings | None = None) -> Path:
    """Hook + body joined with an xfade/acrossfade transition (re-encode, one pass)."""
    s = settings or load_settings()
    fade = s.merge.fade_seconds
    hook_info = probe(hook, s)
    if hook_info.duration <= fade:
        raise MediaError(f"Hook ({hook_info.duration:.2f}s) is not longer than the fade ({fade}s)")
    w, h, fps, rate = s.video.width, s.video.height, s.video.fps, s.video.audio_rate
    vnorm = f"fps={fps},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p"
    anorm = f"aresample={rate},aformat=sample_fmts=fltp:channel_layouts=stereo"
    graph = (
        f"[0:v]{vnorm}[v0];[1:v]{vnorm}[v1];"
        f"[v0][v1]xfade=transition={s.merge.transition}:duration={fade}:offset={hook_info.duration - fade:.3f}[v];"
        f"[0:a]{anorm}[a0];[1:a]{anorm}[a1];[a0][a1]acrossfade=d={fade}[a]"
    )
    _ffmpeg(["-i", str(hook), "-i", str(body), "-filter_complex", graph,
             "-map", "[v]", "-map", "[a]", *_encode_args(s), str(out)], s)
    return Path(out)


def extract_frame(video: str | Path, t: float, dst: str | Path, settings: Settings | None = None) -> Path:
    s = settings or load_settings()
    _ffmpeg(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(dst)], s)
    return Path(dst)
