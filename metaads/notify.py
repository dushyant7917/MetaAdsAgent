"""Telegram delivery from the background worker (Bot API, same bot token the Hermes gateway uses)."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("metaads.notify")
MAX_VIDEO_BYTES = 50 * 1024 * 1024        # Bot API upload limit


class Notifier(Protocol):
    def send_text(self, chat_id: str | None, text: str) -> None: ...
    def send_video(self, chat_id: str | None, path: str | Path, caption: str) -> None: ...
    def send_report(self, chat_id: str | None, text: str) -> None: ...   # batch summary / failures


class NullNotifier:
    def send_text(self, chat_id, text):
        log.info("notify (no telegram): %s", text)

    def send_video(self, chat_id, path, caption):
        log.info("notify (no telegram): %s -> %s", caption, path)

    def send_report(self, chat_id, text):
        log.info("notify (no telegram): %s", text)


class TelegramNotifier:
    def __init__(self, token: str, default_chat_id: str | None = None, report_chat_id: str | None = None):
        self.token = token
        self.default_chat_id = default_chat_id        # where ads go when the request names no chat
        self.report_chat_id = report_chat_id          # where summaries and failures go (None: with the ads)
        self.base = f"https://api.telegram.org/bot{token}"

    @classmethod
    def from_env(cls) -> "Notifier":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return NullNotifier()
        allowed = [u.strip() for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()]
        report = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip() or None
        return cls(token, allowed[0] if allowed else None, report)

    def _chat(self, chat_id: str | None) -> str | None:
        return chat_id or self.default_chat_id

    def send_text(self, chat_id, text):
        chat = self._chat(chat_id)
        if not chat:
            log.warning("No Telegram chat id; dropped message: %s", text)
            return
        httpx.post(f"{self.base}/sendMessage", data={"chat_id": chat, "text": text}, timeout=30).raise_for_status()

    def send_report(self, chat_id, text):
        self.send_text(self.report_chat_id or chat_id, text)

    def send_video(self, chat_id, path, caption):
        chat = self._chat(chat_id)
        if not chat:
            log.warning("No Telegram chat id; dropped video: %s", path)
            return
        path = Path(path)
        method, field = ("sendVideo", "video") if path.stat().st_size <= MAX_VIDEO_BYTES else (None, None)
        if method is None:
            self.send_text(chat, f"{caption}\nVideo is over 50 MB, saved at: {path}")
            return
        with path.open("rb") as f:
            httpx.post(f"{self.base}/{method}", data={"chat_id": chat, "caption": caption[:1000],
                                                      "supports_streaming": "true"},
                       files={field: (path.name, f, "video/mp4")}, timeout=300).raise_for_status()
