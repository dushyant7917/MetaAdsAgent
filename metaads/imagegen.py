"""Thumbnail image of the news reporter via OpenAI gpt-image-2: the 9:16 reporter image the hook video is made from,
or the 4:5 image of an image ad. Sizes and aspects come from settings.images."""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from . import media
from .config import Settings, load_settings, require_env
from .ratelimit import RateLimiter, get_limiter

# Which entry of settings.images to use.
IMAGE_REPORTER, IMAGE_AD = "reporter", "image_ad"

# Wording and quotes follow the owner's earlier working Go tool (quoted text renders more exactly).
PROMPT = ('Create a thumbnail of {aspect} aspect ratio which has a beautiful indian female news reporter '
          'holding a mike in one hand and thumbnail text is "{thumbnail_text}"')


def generate_image(thumbnail_text: str, out_png: str | Path, settings: Settings | None = None,
                   limiter: RateLimiter | None = None, kind: str = IMAGE_REPORTER) -> Path:
    """Generate the image with `thumbnail_text` on it and save it as an exact width x height PNG (settings.images[kind])."""
    from openai import OpenAI

    s = settings or load_settings()
    spec = s.images[kind]
    limiter = limiter or get_limiter("image", s.limits.image_rpm)
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"), timeout=s.timeouts.image_request)
    limiter.acquire()
    resp = client.images.generate(model=s.models.image,
                                  prompt=PROMPT.format(aspect=spec["aspect"], thumbnail_text=thumbnail_text),
                                  size=spec["size"], quality=s.models.image_quality, n=1)
    b64 = resp.data[0].b64_json
    if not b64:
        raise RuntimeError("Image API returned no image data")
    out = Path(out_png)
    with tempfile.TemporaryDirectory(prefix="metaads_") as tmp:
        raw = Path(tmp) / "raw.png"
        raw.write_bytes(base64.b64decode(b64))
        media.normalize_image(raw, out, spec["width"], spec["height"], settings=s)   # fit + black bars, no crop
    return out
