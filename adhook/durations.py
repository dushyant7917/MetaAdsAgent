"""Decide the Veo clip length (4, 6 or 8 s) for each hook text with ONE Gemini call per text.

Gemini estimates how long a text takes to speak (any language or script). Texts are asked concurrently and
independently, so one odd text cannot disturb the estimate of another. The prompt contains no example hooks or
numbers, only the format, so nothing biases the estimates. The code adds the lead-in/tail allowance and picks the
smallest allowed clip. Each call is retried on errors and on unusable answers; there is no fallback estimator, so
a persistent failure is reported instead of guessed around.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .config import Settings, load_settings, require_env

LLMFn = Callable[[str], list[dict]]

_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "speech_seconds": {"type": "number"}},
        "required": ["id", "speech_seconds"],
    },
}


class DurationEstimateError(RuntimeError):
    """Gemini could not give a usable speaking time for every text, even after retries."""


def build_prompt(items: list[dict]) -> str:
    """One prompt for the given texts (the caller passes one). X, Y, Z only show the output format; no real values are given, to avoid bias."""
    todo = "\n".join(f'{i}. ({it["language"]}) "{it["text"]}"' for i, it in enumerate(items))
    return (
        "TASK\n"
        "Estimate how long each hook text below takes to speak aloud, in seconds.\n\n"
        "CONTEXT\n"
        "In a short video ad, a news reporter speaks each hook text to the camera. The voice is a clear AI "
        "news-reader voice at a normal, clear pace: not rushed, not slow, every word clearly pronounced, with a "
        "short pause at the end of each sentence. Each text is written in the language given in brackets (language codes: hi Hindi, mr Marathi, "
        "bn Bengali, gu Gujarati, kn Kannada, ta Tamil, te Telugu), either in that language's own script or in "
        "Roman letters (such as Hinglish). Numbers and symbols are spoken as words in that language.\n\n"
        "HOW TO ESTIMATE\n"
        "Say each text aloud in your head, as a native speaker of that language would read it at a normal, clear "
        "pace, word by word, and time it. Judge each text on its own. Do not add silence before or after the speech; "
        "that is handled elsewhere.\n\n"
        "INPUT FORMAT\n"
        "One text per line: <id>. (<language code>) \"<text>\"\n\n"
        "OUTPUT FORMAT\n"
        "A JSON array with exactly one object per input id, no missing or extra ids:\n"
        '[{"id": 0, "speech_seconds": X}, {"id": 1, "speech_seconds": Y}, {"id": 2, "speech_seconds": Z}]\n'
        "where X, Y and Z stand for the estimated seconds as decimal numbers.\n\n"
        "TEXTS\n" + todo)


def gemini_llm(settings: Settings | None = None) -> LLMFn:
    from google import genai
    from google.genai import types

    s = settings or load_settings()
    client = genai.Client(api_key=require_env("GEMINI_API_KEY"),
                          http_options=types.HttpOptions(timeout=s.timeouts.llm_request * 1000))

    def ask(prompt: str) -> list[dict]:
        resp = client.models.generate_content(
            model=s.models.llm, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=_SCHEMA, temperature=0))
        return json.loads(resp.text)

    return ask


def clip_for(speech_seconds: float, s: Settings) -> int:
    """Smallest allowed clip that holds the speech plus the lead-in/tail; the longest clip if none is long enough."""
    need = speech_seconds + s.hook.lead_tail_seconds
    return next((d for d in sorted(s.hook.durations) if need <= d), max(s.hook.durations))


def _estimate_one(item: dict, ask: LLMFn, s: Settings) -> tuple[dict | None, str]:
    """One text, one call per attempt. Returns (answer or None, last error)."""
    last_error = "no answer"
    for attempt in range(1, s.hook.duration_retries + 2):
        try:
            for a in ask(build_prompt([item])):
                secs = float(a["speech_seconds"])
                if int(a["id"]) == 0 and 0.2 <= secs <= 120:               # reject nonsense values
                    return {"speech_seconds": round(secs, 2), "attempts": attempt}, last_error
            last_error = "unusable answer"
        except Exception as exc:                                              # network, quota, bad JSON, bad fields
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt <= s.hook.duration_retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    return None, last_error


def pick_durations(items: list[dict], settings: Settings | None = None, llm: LLMFn | None = None) -> list[dict]:
    """items: [{language, text}] -> [{speech_seconds, clip_seconds (longest allowed if the text is longer), attempts}], same order.

    One call per text, run concurrently. A text without a usable answer is asked again (up to
    hook.duration_retries more times); if some still have none, DurationEstimateError is raised.
    """
    s = settings or load_settings()
    ask = llm or gemini_llm(s)
    with ThreadPoolExecutor(max_workers=max(1, min(len(items), s.limits.api_concurrency))) as pool:
        results = list(pool.map(lambda it: _estimate_one(it, ask, s), items))
    failed = [err for answer, err in results if answer is None]
    if failed:
        raise DurationEstimateError(
            f"Could not estimate the speaking time for {len(failed)} of {len(items)} text(s) after "
            f"{s.hook.duration_retries + 1} attempts ({failed[0]}). Please try again.")
    return [{**answer, "clip_seconds": clip_for(answer["speech_seconds"], s)} for answer, _ in results]
