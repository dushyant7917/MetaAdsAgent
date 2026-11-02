"""Fills {{section.key}} placeholders in the Hermes text (SOUL.md and the skills) from settings.yaml when the profile is
deployed, so the bot is never told a number that the config no longer has.

  python -m metaads.skilltext <source.md> <destination.md>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from .config import Settings, load_settings

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def _lookup(s: Settings, dotted: str) -> Any:
    value: Any = s
    for part in dotted.split("."):
        try:
            value = value[part]
        except (KeyError, TypeError):
            raise KeyError(f"unknown settings key {dotted!r} in a {{{{placeholder}}}}") from None
    return value


def render(text: str, s: Settings) -> str:
    """Every {{a.b}} becomes the setting a.b (a list as '4, 6, 8'); an unknown key is an error, never left in the text."""
    def fill(match: re.Match) -> str:
        value = _lookup(s, match.group(1))
        return ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return PLACEHOLDER.sub(fill, text)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m metaads.skilltext <source.md> <destination.md>", file=sys.stderr)
        return 2
    out = render(Path(argv[0]).read_text(), load_settings())
    Path(argv[1]).write_text(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
