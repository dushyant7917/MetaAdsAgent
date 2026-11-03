"""Word-level timestamps with faster-whisper (multilingual `medium`, int8, CPU)."""
from __future__ import annotations

import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from . import media
from .config import Settings, load_settings
from .locks import CPU_LOCK

class NoSpeechRecognizedError(RuntimeError):
    """Whisper found no words in a clip, even with its confidence filters off."""


_model = None
_model_lock = threading.Lock()

# Whisper drops every segment it is unsure of (low average log-probability, or "probably no speech"). On some Indian
# languages (Telugu, Bengali) that is all of them even though the clip is clearly spoken; switching those two checks
# off returns the words, spelled roughly, with usable timestamps.
UNFILTERED = {"log_prob_threshold": None, "no_speech_threshold": None}


def _get_model(s: Settings):
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(s.models.whisper, device="cpu",
                                  compute_type=s.models.whisper_compute_type,
                                  cpu_threads=s.models.whisper_cpu_threads)
        return _model


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def transcribe_words(media_path: str | Path, language: str,
                     settings: Settings | None = None) -> list[dict]:
    """Return [{i, word, start, end}] for the speech in a video or audio file.

    `language` is the ISO code (hi, ta, ...). Audio extraction happens internally with ffmpeg. An empty list means
    no speech was recognised even after the unfiltered second pass.
    """
    s = settings or load_settings()
    with tempfile.TemporaryDirectory(prefix="metaads_") as tmp:
        wav = media.extract_audio(media_path, Path(tmp) / "audio.wav", s)
        audio = _read_wav(wav)   # ffmpeg-made 16 kHz mono PCM; bypasses faster-whisper's PyAV decoder
        model = _get_model(s)
        with CPU_LOCK:
            words = _recognize(model, audio, language)
            if not words:                       # heard nothing: ask again without Whisper's confidence filters
                words = _recognize(model, audio, language, **UNFILTERED)
    return [{"i": i, "word": w.word.strip(), "start": round(float(w.start), 3), "end": round(float(w.end), 3)}
            for i, w in enumerate(words)]


def _recognize(model, audio: np.ndarray, language: str, **filters) -> list:
    segments, _info = model.transcribe(audio, language=language, word_timestamps=True,
                                       beam_size=5, vad_filter=False, **filters)
    return [w for seg in segments for w in (seg.words or [])]
