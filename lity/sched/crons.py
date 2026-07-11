"""Tiny schedule-spec parser. Formats:
  in:10m / in:2h / in:45s          one-shot timer
  every:30m / every:6h             recurring interval
  daily:09:00                      recurring daily (UTC)
  weekly:mon:09:00                 recurring weekly (UTC)
"""

import re
from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%d %H:%M:%S"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MIN_EVERY_SECONDS = 5  # floor for recurring intervals — protects the model/db from every:1s spam


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _duration(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([smhd])", s.strip())
    if not m:
        raise ValueError(f"bad duration '{s}' (use e.g. 10m, 2h, 45s, 1d)")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def spec_kind(spec: str) -> str:
    return "timer" if spec.strip().lower().startswith("in:") else "cron"


def next_run(spec: str, after: datetime | None = None) -> str:
    now = after or _now()
    s = spec.strip().lower()

    if s.startswith("in:"):
        return (now + _duration(s[3:])).strftime(FMT)

    if s.startswith("every:"):
        d = _duration(s[6:])
        if d.total_seconds() < MIN_EVERY_SECONDS:
            raise ValueError(f"recurring interval must be at least {MIN_EVERY_SECONDS}s")
        return (now + d).strftime(FMT)

    if s.startswith("daily:"):
        hh, mm = map(int, s[6:].split(":"))
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand.strftime(FMT)

    if s.startswith("weekly:"):
        _, day, hhmm = s.split(":", 2)
        if day not in DAYS:
            raise ValueError(f"bad weekday '{day}'")
        hh, mm = map(int, hhmm.split(":"))
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        ahead = (DAYS.index(day) - cand.weekday()) % 7
        cand += timedelta(days=ahead)
        if cand <= now:
            cand += timedelta(days=7)
        return cand.strftime(FMT)

    raise ValueError(f"unknown spec '{spec}' (use in:/every:/daily:/weekly:)")
