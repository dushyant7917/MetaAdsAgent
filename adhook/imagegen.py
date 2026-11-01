"""Thumbnail image of the news reporter via OpenAI gpt-image-2."""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from . import media
from .config import Settings, load_settings, require_env
from .ratelimit import RateLimiter, get_limiter

# Wording and quotes follow the owner's earlier working Go tool (quoted text renders more exactly).
PROMPT = ('Create a thumbnail of 9:16 aspect ratio which has a beautiful indian female news reporter '
          'holding a mike in one hand and thumbnail text is "{text}"')


def generate_image(text: str, out_png: str | Path, settings: Settings | None = None,
                   limiter: RateLimiter | None = None) -> Path:
    """Generate the thumbnail and save it as an exact 720x1280 (9:16) PNG."""
    from openai import OpenAI

    s = settings or load_settings()
    limiter = limiter or get_limiter("image", s.limits.image_rpm)
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"), timeout=s.timeouts.image_request)
    limiter.acquire()
    resp = client.images.generate(model=s.models.image, prompt=PROMPT.format(text=text),
                                  size=s.models.image_size, quality=s.models.image_quality, n=1)
    b64 = resp.data[0].b64_json
    if not b64:
        raise RuntimeError("Image API returned no image data")
    out = Path(out_png)
    with tempfile.TemporaryDirectory(prefix="adhook_") as tmp:
        raw = Path(tmp) / "raw.png"
        raw.write_bytes(base64.b64decode(b64))
        media.normalize_image(raw, out, settings=s)   # gpt-image sizes are not 9:16: fit + black bars, no crop
    return out
