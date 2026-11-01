"""Decide when the poster demo is shown: the LLM picks the word span, code turns it into seconds."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import yaml

from .config import PROJECT_ROOT, Settings, load_settings, require_env
from .media import PosterWindow

LLMFn = Callable[[str], dict]

_SCHEMA = {
    "type": "object",
    "properties": {
        "start_idx": {"type": "integer"},
        "end_idx": {"type": "integer"},
        "phrase": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["start_idx", "end_idx"],
}


@dataclass
class PlanResult:
    window: PosterWindow
    start_idx: int | None
    end_idx: int | None
    phrase: str
    method: str                     # llm | keyword | default
    notes: list[str] = field(default_factory=list)


def build_prompt(text: str, language: str, words: list[dict], product: dict, guidance: str | None = None) -> str:
    """X, Y only show the output format; no example hooks are given, so nothing biases the choice."""
    transcript = " ".join(f'[{w["i"]}]{w["word"]}' for w in words)
    feedback = (f"FEEDBACK ON AN EARLIER CHOICE\n{guidance.strip()}\n"
                "Choose a stretch that fixes this, while still following THE RULE.\n\n") if guidance else ""
    return (
        "TASK\n"
        "Choose the stretch of speech during which a poster demo video is shown over a news reporter.\n\n"
        "CONTEXT\n"
        "In a short vertical video ad, a news reporter speaks a hook text. For part of the speech a poster demo "
        "video is shown full-screen over her while her voice keeps playing.\n"
        f"The poster: {product['poster_description'].strip()}\n\n"
        "THE RULE\n"
        f"{product['rule'].strip()}\n\n"
        f"{feedback}"
        "INPUT\n"
        f"Language code: {language}\n"
        f'Hook text as written: "{text}"\n'
        "The words actually spoken, as recognized from the audio, each with an index number in brackets. The "
        "recognized spelling can differ from the written text and can be in another script, so match by meaning "
        "and sound, not by exact spelling:\n"
        f"{transcript}\n\n"
        "OUTPUT FORMAT\n"
        "One JSON object:\n"
        '{"start_idx": X, "end_idx": Y, "phrase": "<the recognized words from X to Y>", "reason": "<one short sentence>"}\n'
        "where X is the index of the first word and Y the index of the last word of the stretch (both inclusive, "
        "X not greater than Y, both taken from the index numbers above, all words between them included).")


def gemini_llm(settings: Settings | None = None) -> LLMFn:
    from google import genai
    from google.genai import types

    s = settings or load_settings()
    client = genai.Client(api_key=require_env("GEMINI_API_KEY"),
                          http_options=types.HttpOptions(timeout=s.timeouts.llm_request * 1000))

    def ask(prompt: str) -> dict:
        resp = client.models.generate_content(
            model=s.models.llm, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=_SCHEMA, temperature=0))
        return json.loads(resp.text)

    return ask


def _valid(answer: dict, n: int) -> bool:
    try:
        a, b = int(answer["start_idx"]), int(answer["end_idx"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= a <= b < n


def _keyword_span(words: list[dict], keywords: list[str]) -> tuple[int, int] | None:
    keys = [k.lower() for k in keywords]
    hits = [w["i"] for w in words if any(k in w["word"].lower() for k in keys)]
    if not hits:
        return None
    first = hits[0]
    return max(first - 1, 0), min(first + 2, len(words) - 1)


def seconds_for_span(words: list[dict], a: int, b: int, clip_duration: float, s: Settings) -> PosterWindow:
    """Word span -> padded seconds, widened to the minimum poster time and clamped to the clip."""
    start = max(words[a]["start"] - s.poster.pad_before, 0.0)
    end = min(words[b]["end"] + s.poster.pad_after, clip_duration)
    need = s.poster.min_seconds
    if end - start < need:
        end = min(start + need, clip_duration)          # grow forward first
    if end - start < need:
        start = max(end - need, 0.0)                    # then backward
    return PosterWindow(round(start, 3), round(end, 3))


def plan_poster_window(text: str, language: str, words: list[dict], clip_duration: float,
                       settings: Settings | None = None, llm: LLMFn | None = None,
                       product: dict | None = None, guidance: str | None = None) -> PlanResult:
    s = settings or load_settings()
    if not words:
        raise ValueError("No recognized words: cannot place the poster")
    product = product or yaml.safe_load((PROJECT_ROOT / "config" / "product.yaml").read_text())
    notes: list[str] = []
    try:
        answer = (llm or gemini_llm(s))(build_prompt(text, language, words, product, guidance))
        if _valid(answer, len(words)):
            a, b = int(answer["start_idx"]), int(answer["end_idx"])
            phrase = " ".join(w["word"] for w in words[a:b + 1])
            return PlanResult(seconds_for_span(words, a, b, clip_duration, s), a, b, phrase, "llm", notes)
        notes.append(f"LLM answer unusable: {answer!r}")
    except Exception as exc:  # network, quota, bad JSON: fall back rather than fail the ad
        notes.append(f"LLM failed: {type(exc).__name__}: {exc}")

    span = _keyword_span(words, product.get("fallback_keywords", []))
    if span:
        a, b = span
        phrase = " ".join(w["word"] for w in words[a:b + 1])
        return PlanResult(seconds_for_span(words, a, b, clip_duration, s), a, b, phrase, "keyword", notes)

    start = clip_duration * 0.4
    window = PosterWindow(round(start, 3), round(min(start + max(s.poster.min_seconds, 2.0), clip_duration), 3))
    notes.append("No keyword found; using the middle of the clip")
    return PlanResult(window, None, None, "", "default", notes)
