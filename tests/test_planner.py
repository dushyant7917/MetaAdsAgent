import re

import pytest
import yaml

from metaads.config import PROJECT_ROOT, load_settings
from metaads.planner import plan_poster_window

S = load_settings()
PRODUCT = yaml.safe_load((PROJECT_ROOT / "config" / "product.yaml").read_text())

# The owner's six hooks and the phrase the poster must appear on (test data only; never shown to the LLM).
CASES = [
    {"text": "News reporter ho to ye video apke liye hai! Aisi news video banani seekhni hai?", "phrase": "Aisi news video banani"},
    {"text": "News channel owners ke liye game changer! Aisi news videos sirf 1 sec mein!", "phrase": "Aisi news video sirf"},
    {"text": "News reporter ho to aisi news video banana aana chaiye!", "phrase": "aisi news video banana"},
    {"text": "News channel chalate ho to ye mat hi dekhna aap! Aisi new video banani hai?", "phrase": "Aisi news video banani"},
    {"text": "Har news reporter ke phone mein ye app hona chaiye! Jis se 1 sec mein aisi video ban jati hai.", "phrase": "aisi news video ban"},
    {"text": "Farzi news reporter kaise hote hain? Wo hote hain jinhe aisi news video banani nai aati!", "phrase": "aisi news video banani"},
]


def _words(text, per_word=0.4, offset=0.3):
    """Fake recognizer output: one word every `per_word` seconds."""
    toks = re.findall(r"[\w']+", text)
    return [{"i": i, "word": t, "start": round(offset + i * per_word, 3), "end": round(offset + (i + 1) * per_word - 0.05, 3)}
            for i, t in enumerate(toks)]


def _span_of(words, phrase):
    """Span from the phrase's first word to its last. The written examples are not exact: one text
    says "aisi video ban" for the phrase "aisi news video ban", and one says "new" for "news"."""
    toks = [t.lower() for t in re.findall(r"[\w']+", phrase)]
    got = [w["word"].lower() for w in words]
    first = next(i for i, g in enumerate(got) if g[:3] == toks[0][:3])
    last = next(i for i in range(first, len(got)) if got[i][:3] == toks[-1][:3])
    return first, last


@pytest.mark.parametrize("example", CASES, ids=lambda e: e["phrase"])
def test_window_covers_chosen_phrase_for_all_examples(example):
    words = _words(example["text"])
    a, b = _span_of(words, example["phrase"])
    clip = words[-1]["end"] + 0.5
    res = plan_poster_window(example["text"], "hi", words, clip, S,
                             llm=lambda prompt: {"start_idx": a, "end_idx": b}, product=PRODUCT)
    assert res.method == "llm" and (res.start_idx, res.end_idx) == (a, b)
    assert res.window.start <= words[a]["start"] and res.window.end >= words[b]["end"]
    assert res.window.end - res.window.start >= S.poster.min_seconds - 1e-6
    assert 0 <= res.window.start < res.window.end <= clip


def test_short_phrase_is_widened_and_clamped_at_clip_end():
    words = _words("sirf ye ek", per_word=0.3)          # clip ends 1.2 s in
    res = plan_poster_window("sirf ye ek", "hi", words, 1.3, S, llm=lambda p: {"start_idx": 2, "end_idx": 2}, product=PRODUCT)
    assert res.window.end == pytest.approx(1.3)
    assert res.window.end - res.window.start == pytest.approx(min(S.poster.min_seconds, 1.3))
    assert res.window.start >= 0


def test_bad_llm_answer_falls_back_to_keyword():
    words = _words("aap ko aisi news video banani hai")
    res = plan_poster_window("x", "hi", words, 4.0, S, llm=lambda p: {"start_idx": 99, "end_idx": 2}, product=PRODUCT)
    assert res.method == "keyword"
    assert "news" in res.phrase.lower()
    assert any("unusable" in n for n in res.notes)


def test_llm_exception_and_no_keyword_falls_back_to_default():
    def boom(prompt):
        raise RuntimeError("quota")
    words = _words("kuch bhi nahi yahan hai")
    res = plan_poster_window("x", "hi", words, 5.0, S, llm=boom, product=PRODUCT)
    assert res.method == "default"
    assert res.window.start == pytest.approx(2.0)
    assert any("LLM failed" in n for n in res.notes)


def test_no_words_gives_the_default_window_without_asking_the_llm():
    def never(prompt):
        raise AssertionError("the LLM must not be asked when there are no words")
    res = plan_poster_window("x", "te", [], 8.0, S, llm=never, product=PRODUCT)
    assert res.method == "default" and res.phrase == ""
    assert res.window.start == pytest.approx(8.0 * S.poster.fallback_start_share)
    assert res.window.end - res.window.start == pytest.approx(max(S.poster.min_seconds, S.poster.fallback_seconds))
    assert any("No words" in n for n in res.notes)


@pytest.mark.parametrize("video", ["video", "वीडियो", "ভিডিও", "వీడియో", "வீடியோ", "ವೀಡಿಯೊ", "વિડિયો"])
def test_the_keyword_fallback_anchors_on_video_in_every_script_not_on_an_earlier_news(video):
    words = [{"i": i, "word": w, "start": i * 0.4, "end": i * 0.4 + 0.35}
             for i, w in enumerate(["news", "channel", "owners", "ye", "aisi", "news", video, "banao", "ab"])]
    res = plan_poster_window("x", "xx", words, 6.0, S, llm=lambda p: {"start_idx": 99, "end_idx": 2}, product=PRODUCT)
    assert res.method == "keyword" and (res.start_idx, res.end_idx) == (5, 8)   # "news <video> banao ab", not the first "news"


def test_prompt_has_indexed_words_the_rule_and_a_format_only_sample():
    seen = {}
    words = _words("aisi news video banani")
    plan_poster_window("Dekho ye naya video", "hi", words, 3.0, S,
                       llm=lambda p: seen.setdefault("p", p) and {"start_idx": 0, "end_idx": 3}, product=PRODUCT)
    p = seen["p"]
    assert "[0]aisi" in p and '"start_idx": X' in p and "Y" in p
    for c in CASES:                                                   # none of the owner's hooks or phrases is in the prompt
        assert c["text"] not in p
    assert "such a news video" in p                                   # the rule is stated in words
