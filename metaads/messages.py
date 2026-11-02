"""Plain-text layout of everything sent to Telegram, alerts and DMs alike, in one place so every message reads the
same way on a phone: a headline, then items separated by blank lines, each item a title line with its details indented
underneath. Telegram gets plain text here (no markup to escape); structure comes from blank lines, numbering and a few
icons. Build a message with `blocks(headline(...), entry(...), ...)`."""
from __future__ import annotations

OK, WARN, FAIL, WAIT = "✅", "⚠️", "❌", "⏳"
PAUSED, RAISED, IMAGE, VIDEO, PLAN = "⏸", "📈", "🖼", "🎬", "📋"


def headline(icon: str, text: str) -> str:
    """The first line of a message: what happened, with an icon."""
    return f"{icon} {text}" if icon else text


def lines(*parts: str | None) -> str:
    """The lines of ONE block, kept together; empty parts are dropped."""
    return "\n".join(p for p in parts if p)


def blocks(*parts: str | None) -> str:
    """Blocks separated by a blank line so each one stands out; empty blocks are dropped."""
    return "\n\n".join(p for p in parts if p)


def entry(title: str, *details: str | None, n: int | None = None) -> str:
    """One item of a list: a title line (numbered when `n` is given), its details indented underneath."""
    first = f"{n}. {title}" if n is not None else title
    return lines(first, *(f"   {d}" for d in details if d))
