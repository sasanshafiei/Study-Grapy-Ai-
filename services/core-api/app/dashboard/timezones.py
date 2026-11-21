"""Local days for the student dashboard: IANA time zones, DST, midnight and empty days.

Rules (docs/dashboard-api.md, "Time"):
- The client sends `tz`, an IANA zone name from Python's tz database (zoneinfo); default UTC.
  Aliases such as "localtime", "Factory" and the posix/ and right/ trees are refused (422).
- Day boundaries are computed HERE, in Python, and passed to PostgreSQL as explicit UTC
  instants. PostgreSQL's own tz database is never consulted, so a difference between the
  two tz database versions cannot shift a bucket.
- A local day d is the half-open interval [start(d), start(d + 1)), where start(d) is the
  first instant whose local date is d: local midnight resolved with fold=0. In a spring-
  forward gap at midnight (e.g. America/Havana) that is the first existing instant of the
  day (01:00); a DST day is 23 or 25 hours long. In zones that fall back across midnight,
  the repeated hour belongs to the day that started first (boundaries stay monotonic).
- "Today" is the local date of the snapshot's database time (as_of). Every day in a window
  is listed, empty days included; an empty day has 0 attempts and accuracy null.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

MAX_ZONE_CHARS = 64
_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")
_EXCLUDED = frozenset({"localtime", "Factory", "posixrules"})


class InvalidTimezone(ValueError):
    pass


@lru_cache(maxsize=1)
def available_zones() -> frozenset[str]:
    return frozenset(
        z for z in zoneinfo.available_timezones() if z not in _EXCLUDED and not z.startswith(("posix/", "right/"))
    )


@dataclass(frozen=True)
class Day:
    day: date
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive


def parse_timezone(value: object) -> tuple[str, zoneinfo.ZoneInfo]:
    if value is None:
        return "UTC", zoneinfo.ZoneInfo("UTC")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_ZONE_CHARS
        or not _ZONE.match(value)
        or value not in available_zones()
    ):
        raise InvalidTimezone(value)
    return value, zoneinfo.ZoneInfo(value)


def day_start(day: date, zone: zoneinfo.ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone).replace(fold=0).astimezone(timezone.utc)


def local_date(instant: datetime, zone: zoneinfo.ZoneInfo) -> date:
    return instant.astimezone(zone).date()


def days_ending_today(as_of: datetime, zone: zoneinfo.ZoneInfo, count: int) -> list[Day]:
    """`count` consecutive local days, oldest first, the last one being today."""
    today = local_date(as_of, zone)
    first = today - timedelta(days=count - 1)
    days = []
    for offset in range(count):
        d = first + timedelta(days=offset)
        start, end = day_start(d, zone), day_start(d + timedelta(days=1), zone)
        if not start < end:  # cannot happen with fold=0 boundaries; refuse rather than mis-bucket
            raise InvalidTimezone("non-monotonic day boundaries")
        days.append(Day(d, start, end))
    return days
