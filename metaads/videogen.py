"""Talking-reporter clip: the gpt-image thumbnail -> Veo image-to-video on Google Cloud (Vertex AI)."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .config import Settings, load_settings, require_env
from .ratelimit import RateLimiter, get_limiter


class VideoGenerationError(RuntimeError):
    retryable = True


class VideoBlockedError(VideoGenerationError):
    """Content-policy refusal that is final (the pipeline does not retry it either)."""
    retryable = False


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(exc, "code", None)


def _backoff(attempt: int, s: Settings) -> None:
    time.sleep(min(s.generation.backoff_seconds * 2 ** attempt, s.generation.backoff_max_seconds))


def vertex_prompt(spoken_text: str, language: str, s: Settings) -> str:
    return s.generation.prompt.format(language=s.languages[language], spoken_text=spoken_text)


def vertex_config(duration: int, s: Settings):
    from google.genai import types
    return types.GenerateVideosConfig(aspect_ratio="9:16", resolution=s.models.video_resolution,
                                      duration_seconds=duration, number_of_videos=1, generate_audio=True)


def vertex_client(s: Settings):
    from google import genai
    from google.genai import types
    return genai.Client(vertexai=True, project=require_env("GOOGLE_CLOUD_PROJECT"),
                        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip() or "us-central1",
                        http_options=types.HttpOptions(timeout=s.timeouts.vertex_request * 1000))


def _generate(spoken_text: str, language: str, out: Path, duration: int, image_path: str | Path | None,
                     s: Settings, limiter: RateLimiter, max_retries: int) -> Path:
    from google.genai import types

    if not image_path:
        raise VideoGenerationError("Veo image-to-video needs the thumbnail image")
    client = vertex_client(s)
    image = types.Image(image_bytes=Path(image_path).read_bytes(), mime_type="image/png")
    prompt, config = vertex_prompt(spoken_text, language, s), vertex_config(duration, s)

    for attempt in range(max_retries + 1):
        limiter.acquire()                        # every attempt counts against the rate limit
        try:
            op = client.models.generate_videos(model=s.models.video, prompt=prompt, image=image, config=config)
            break
        except Exception as exc:
            if _status(exc) == 429 and attempt < max_retries:
                _backoff(attempt, s)
                continue
            raise VideoGenerationError(f"Veo request failed: {exc}") from exc

    deadline = time.monotonic() + s.generation.timeout_seconds
    while not op.done:
        if time.monotonic() > deadline:
            raise VideoGenerationError(f"Veo did not finish within {s.generation.timeout_seconds}s")
        time.sleep(s.generation.poll_seconds)
        op = client.operations.get(op)
    if op.error:
        raise VideoGenerationError(f"Veo failed: {op.error}")
    resp = op.response
    if not resp or not resp.generated_videos:
        reasons = getattr(resp, "rai_media_filtered_reasons", None)
        raise VideoBlockedError(f"Veo returned no video (blocked by safety filters): {reasons}")
    video = resp.generated_videos[0].video
    data = getattr(video, "video_bytes", None)
    if not data:                                  # fall back to a Files-style download if bytes are not inline
        client.files.download(file=video)
        data = video.video_bytes
    out.write_bytes(data)
    return out


def generate_video(spoken_text: str, language: str, out_mp4: str | Path, duration: int,
                   image_path: str | Path | None = None, settings: Settings | None = None,
                   limiter: RateLimiter | None = None, max_retries: int | None = None) -> Path:
    """Generate the 9:16 clip of the reporter in `image_path` speaking `spoken_text` in `language` (code)."""
    s = settings or load_settings()
    limiter = limiter or get_limiter("video", s.limits.video_rpm)
    retries = s.generation.max_retries if max_retries is None else max_retries
    return _generate(spoken_text, language, Path(out_mp4), duration, image_path, s, limiter, retries)
