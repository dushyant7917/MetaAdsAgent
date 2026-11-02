"""When the recurring jobs run (settings.schedules), for the code that forecasts them and for the installer scripts
that register them, so the two cannot drift apart.

  python -m metaads.schedule autopause-hours   -> 2          (systemd: OnCalendar=*-*-* 00/<hours>:00:00)
  python -m metaads.schedule budget-hour       -> 5          (systemd: OnCalendar=*-*-* 05:00:00 Asia/Kolkata)
  python -m metaads.schedule orphan-data-hour  -> 3          (the same, for metaads.orphan_data)
  python -m metaads.schedule old-data-hour     -> 4          (the same, for metaads.old_data)
  python -m metaads.schedule plan-cron         -> 30 6 * * * (Hermes cron, whose clock is UTC)
"""
from __future__ import annotations

import sys
from datetime import timedelta

from .config import Settings, load_settings
from .meta import IST

HOURS_PER_DAY = 24


def autopause_every(s: Settings) -> timedelta:
    hours = int(s.schedules.autopause_every_hours)
    if not 0 < hours <= HOURS_PER_DAY or HOURS_PER_DAY % hours:
        raise ValueError(f"schedules.autopause_every_hours must divide {HOURS_PER_DAY}, got {hours}")
    return timedelta(hours=hours)


def daily_hour_ist(s: Settings, key: str) -> int:
    """The hour (IST) a once-a-day job runs, from schedules.<key>."""
    hour = int(s.schedules[key])
    if not 0 <= hour < HOURS_PER_DAY:
        raise ValueError(f"schedules.{key} must be 0-23, got {hour}")
    return hour


def budget_increase_hour(s: Settings) -> int:
    return daily_hour_ist(s, "budget_increase_hour_ist")


def plan_cron_utc(s: Settings) -> str:
    """The daily-plan time (HH:MM IST) as a cron expression on the UTC clock Hermes uses. India has no daylight saving."""
    hour, minute = (int(x) for x in str(s.schedules.daily_plan_ist).split(":"))
    utc = (hour * 60 + minute - round(IST.utcoffset(None).total_seconds() / 60)) % (HOURS_PER_DAY * 60)
    return f"{utc % 60} {utc // 60} * * *"


def main(argv: list[str]) -> int:
    s = load_settings()
    answers = {"autopause-hours": lambda: int(autopause_every(s).total_seconds() // 3600),
               "budget-hour": lambda: budget_increase_hour(s), "plan-cron": lambda: plan_cron_utc(s),
               "orphan-data-hour": lambda: daily_hour_ist(s, "orphan_data_cleanup_hour_ist"),
               "old-data-hour": lambda: daily_hour_ist(s, "old_data_cleanup_hour_ist")}
    if len(argv) != 1 or argv[0] not in answers:
        print(f"usage: python -m metaads.schedule {{{'|'.join(answers)}}}", file=sys.stderr)
        return 2
    print(answers[argv[0]]())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
