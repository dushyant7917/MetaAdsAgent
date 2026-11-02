import pytest

from metaads import durations
from metaads.config import load_settings
import re

from metaads.durations import DurationEstimateError, build_prompt, clip_for, pick_durations

S = load_settings()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(durations.time, "sleep", lambda s: None)


def texts(n):
    return [{"language": "hi", "spoken_text": f"text number {i}"} for i in range(n)]


class FakeLLM:
    """Replies per text (calls run concurrently, so the script is keyed by the number in 'text number N').

    script: {n: [step, ...]}; a step is seconds (float), a raw reply (list) or an Exception to raise.
    A text with no script entry gets 3.0 s every time."""
    def __init__(self, script=None):
        self.script, self.prompts = {n: list(v) for n, v in (script or {}).items()}, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        n = int(re.search(r"text number (\d+)", prompt).group(1))
        step = self.script[n].pop(0) if self.script.get(n) else 3.0
        if isinstance(step, Exception):
            raise step
        return step if isinstance(step, list) else [{"id": 0, "speech_seconds": step}]


def test_clip_is_the_smallest_allowed_length_that_holds_speech_plus_tail():
    assert [clip_for(x, S) for x in (1.0, 3.4, 3.6, 5.4, 5.6, 7.4, 7.6, 12)] == [4, 4, 6, 6, 8, 8, 8, 8]


@pytest.mark.parametrize("n", [1, 5, 10])
def test_each_text_gets_its_own_call_and_answers_come_back_in_order(n):
    llm = FakeLLM({i: [2.0 + i * 0.5] for i in range(n)})
    out = pick_durations(texts(n), S, llm=llm)
    assert len(llm.prompts) == n and len(out) == n
    assert [o["speech_seconds"] for o in out] == [2.0 + i * 0.5 for i in range(n)]
    assert all(o["attempts"] == 1 for o in out)
    for p in llm.prompts:                                                  # one text per prompt, never a mix
        assert len(re.findall(r'^\d+\. \(', p, re.M)) == 1 and p.count("text number") == 1


def test_different_languages_and_scripts_are_listed_with_their_codes():
    items = [{"language": "ta", "spoken_text": "செய்தி வீடியோ"}, {"language": "hi", "spoken_text": "Aisi news video banani hai?"}]
    prompts = []
    pick_durations(items, S, llm=lambda p: prompts.append(p) or [{"id": 0, "speech_seconds": 2.0}])
    assert any('0. (ta) "செய்தி வீடியோ"' in p for p in prompts)
    assert any('0. (hi) "Aisi news video banani hai?"' in p for p in prompts)


def test_prompt_has_no_example_hooks_or_numbers_that_could_bias_the_answer():
    p = build_prompt([])
    assert '"speech_seconds": X' in p and "Y" in p and "Z" in p          # placeholders show the format only
    assert set(re.findall(r"\d+", p)) <= {"0", "1", "2"}                  # only the ids in the format sample
    assert "example" not in p.lower()
    assert 'hi Hindi' in p and 'ta Tamil' in p
    assert "normal, clear pace" in p and "brisk" not in p                # a rushed pace would shorten the estimates


def test_error_is_retried_then_succeeds():
    llm = FakeLLM({0: [RuntimeError("503 overloaded"), RuntimeError("429 quota"), 3.0]})
    out = pick_durations(texts(1), S, llm=llm)
    assert len(llm.prompts) == 3 and out[0]["attempts"] == 3 and out[0]["clip_seconds"] == 4


def test_only_the_text_without_a_usable_answer_is_asked_again():
    llm = FakeLLM({1: [RuntimeError("blip"), 6.5], 2: [4.0]})
    out = pick_durations(texts(3), S, llm=llm)
    assert len(llm.prompts) == 4 and sum("text number 1" in p for p in llm.prompts) == 2
    assert [o["speech_seconds"] for o in out] == [3.0, 6.5, 4.0] and [o["attempts"] for o in out] == [1, 2, 1]
    assert [o["clip_seconds"] for o in out] == [4, 8, 6]


def test_nonsense_values_are_treated_as_missing_and_retried():
    llm = FakeLLM({0: [-1, 2.5]})
    out = pick_durations(texts(2), S, llm=llm)
    assert [o["speech_seconds"] for o in out] == [2.5, 3.0]


def test_malformed_reply_is_retried():
    llm = FakeLLM({0: [[{"nope": 1}], 3.0]})                # not a list of {id, speech_seconds}
    assert pick_durations(texts(1), S, llm=llm)[0]["speech_seconds"] == 3.0


def test_gives_up_after_the_configured_retries_with_a_clear_error():
    llm = FakeLLM({0: [RuntimeError("quota")] * 10, 1: [RuntimeError("quota")] * 10})
    with pytest.raises(DurationEstimateError, match="2 of 2.*quota"):
        pick_durations(texts(2), S, llm=llm)
    assert len(llm.prompts) == 2 * (S.hook.duration_retries + 1)


def test_one_failing_text_fails_the_request_but_the_others_are_still_estimated():
    llm = FakeLLM({1: [RuntimeError("quota")] * 10})
    with pytest.raises(DurationEstimateError, match="1 of 3"):
        pick_durations(texts(3), S, llm=llm)


def test_text_longer_than_the_maximum_clip_gets_the_maximum_clip():
    out = pick_durations(texts(2), S, llm=FakeLLM({0: [9.0], 1: [4.0]}))
    assert [o["clip_seconds"] for o in out] == [8, 6] and out[0]["speech_seconds"] == 9.0
