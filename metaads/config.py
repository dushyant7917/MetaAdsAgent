"""Settings loader. YAML in config/settings.yaml, path values expanded, optional env override."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings:
    """Read-only attribute view over a nested dict: settings.video.fps."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError:
            raise AttributeError(name) from None
        return Settings(value) if isinstance(value, dict) else value

    def __getitem__(self, name: str) -> Any:
        return self._data[name]

    def __contains__(self, name: object) -> bool:
        return name in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return self._data


def _resolve_path(value: str) -> str:
    path = Path(os.path.expanduser(value))
    return str(path if path.is_absolute() else PROJECT_ROOT / path)


@lru_cache(maxsize=1)
def load_settings(path: str | None = None) -> Settings:
    cfg_path = Path(path or os.environ.get("METAADS_SETTINGS") or PROJECT_ROOT / "config" / "settings.yaml")
    data = yaml.safe_load(cfg_path.read_text())
    data["paths"] = {k: _resolve_path(v) for k, v in data["paths"].items()}
    return Settings(data)


def load_env_file(path: str | None = None) -> int:
    """Load KEY=VALUE lines from the project's .env (or METAADS_ENV_FILE) without overriding real env vars.

    Hermes gives stdio MCP servers a filtered environment, so the server reads the keys from .env itself
    instead of having secrets copied into Hermes's config.yaml. Returns how many variables were set.
    """
    path = path or os.environ.get("METAADS_ENV_FILE") or str(PROJECT_ROOT / ".env")
    if not path or not Path(path).is_file():
        return 0
    loaded = 0
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = re.split(r"\s+#", value, maxsplit=1)[0]          # inline comment: KEY=value  # note
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is not set")
    return value
