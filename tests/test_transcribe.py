from types import SimpleNamespace

import numpy as np

from metaads import transcribe
from metaads.config import load_settings

S = load_settings()


class FakeWhisper:
    """Answers with no segments unless its confidence filters are off (what Whisper does on Telugu and Bengali)."""

    def __init__(self, hears_when_unfiltered):
        self.hears_when_unfiltered, self.calls = hears_when_unfiltered, []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        unfiltered = kwargs.get("log_prob_threshold", 0) is None and kwargs.get("no_speech_threshold", 0) is None
        words = [SimpleNamespace(word=" aap", start=0.5, end=0.9), SimpleNamespace(word="video ", start=1.0, end=1.4)]
        return iter([SimpleNamespace(words=words)] if unfiltered and self.hears_when_unfiltered else []), None


def _run(monkeypatch, model):
    monkeypatch.setattr(transcribe.media, "extract_audio", lambda src, wav, s: wav)
    monkeypatch.setattr(transcribe, "_read_wav", lambda path: np.zeros(16000, dtype=np.float32))
    monkeypatch.setattr(transcribe, "_get_model", lambda s: model)
    return transcribe.transcribe_words("clip.mp4", "te", S)


def test_when_the_normal_pass_hears_nothing_whisper_is_asked_again_without_its_confidence_filters(monkeypatch):
    model = FakeWhisper(hears_when_unfiltered=True)
    words = _run(monkeypatch, model)
    assert [w["word"] for w in words] == ["aap", "video"] and words[1]["start"] == 1.0
    assert len(model.calls) == 2 and "log_prob_threshold" not in model.calls[0]


def test_nothing_recognized_in_either_pass_is_an_empty_list(monkeypatch):
    model = FakeWhisper(hears_when_unfiltered=False)
    assert _run(monkeypatch, model) == [] and len(model.calls) == 2


def test_a_normal_pass_that_hears_words_is_not_repeated(monkeypatch):
    class Hears(FakeWhisper):
        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            return iter([SimpleNamespace(words=[SimpleNamespace(word="hi", start=0.1, end=0.3)])]), None
    model = Hears(True)
    assert len(_run(monkeypatch, model)) == 1 and len(model.calls) == 1
