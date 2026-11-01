"""The pre-made body videos (the "themes" an ad's hook is merged with): <body_dir>/<language code>/<stem>.mp4."""
from __future__ import annotations

from pathlib import Path

from .config import Settings


def body_folder(settings: Settings, language_code: str) -> Path:
    return Path(settings.paths.body_dir) / language_code


def available_bodies(settings: Settings, language_code: str) -> list[str]:
    """Stems of the body videos that exist for a language, sorted."""
    return sorted(p.stem for p in body_folder(settings, language_code).glob("*.mp4"))
