"""Scaffolding shared by the fixed-rule jobs run by systemd timers (metaads.autopause, metaads.budget).

A job supplies what it does and how it reports; this runs it with the heartbeat, failure alert, single-instance
lock and --apply flag every job needs.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Protocol, TypeVar

import httpx

from .config import Settings, load_env_file, load_settings
from .notify import Notifier

log = logging.getLogger("metaads.jobrunner")


class JobResult(Protocol):
    errors: list[str]


R = TypeVar("R", bound=JobResult)


def ping(url: str, suffix: str = "") -> None:
    """healthchecks.io style heartbeat; a failed ping is logged and never fails the run."""
    if not url:
        return
    for attempt in (1, 2):
        try:
            httpx.get(url.rstrip("/") + suffix, timeout=10).raise_for_status()
            return
        except httpx.HTTPError as e:
            log.warning("heartbeat ping %r failed (attempt %d): %s", suffix or "ok", attempt, e)


def notify(notifier: Notifier, text: str) -> None:
    try:
        notifier.send_report(None, text)
    except Exception as e:                                           # the job's work already happened; just log it
        log.warning("Telegram notice failed: %s", e)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # its request lines would put the heartbeat URL in the journal


def bootstrap() -> Settings:
    """Logging, .env and settings for a command-line entry point."""
    configure_logging()
    load_env_file()
    return load_settings()


@contextmanager
def single_instance(lock_path: Path) -> Iterator[bool]:
    """Yields True when this process got the lock, False when a previous run still holds it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def run_job(label: str, heartbeat: str, notifier: Notifier, execute: Callable[[], R], summary: Callable[[R], str],
            report: Callable[[R], str | None]) -> int:
    """Heartbeat around `execute()`; a raised error alerts and fails, `report(result)` is sent when it has text,
    and a result with errors fails the run. Returns the exit code."""
    ping(heartbeat, "/start")
    try:
        result = execute()
    except Exception as e:
        log.exception("%s failed", label)
        notify(notifier, f"{label} FAILED: {e}")
        ping(heartbeat, "/fail")
        return 1
    log.info("%s", summary(result))
    text = report(result)
    if text:
        notify(notifier, text)
    ping(heartbeat, "/fail" if result.errors else "")
    return 1 if result.errors else 0


def run_cli(argv: list[str] | None, *, label: str, description: str, apply_help: str, lock_name: str,
            heartbeat_env: str, run: Callable[[Settings, bool, str], int]) -> int:
    """The `--apply` command line of a job: dry run by default, one instance at a time, heartbeat from the env."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--apply", action="store_true", help=apply_help)
    args = parser.parse_args(argv)
    settings = bootstrap()
    heartbeat = os.environ.get(heartbeat_env, "").strip()
    with single_instance(Path(settings.paths.db).parent / lock_name) as acquired:
        if not acquired:
            log.error("previous %s run is still going; skipping this one", label)
            ping(heartbeat, "/fail")
            return 1
        return run(settings, args.apply, heartbeat)
