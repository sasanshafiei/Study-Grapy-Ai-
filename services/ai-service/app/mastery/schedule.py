"""Review scheduling: fixed expanding intervals (execution plan cut item 4).

The anchor is the CE commit time (`submitted_at`) of the topic's last applied event, so a
replayed batch gets the same schedule. A review that is already due is reported as "now";
CE accepts review times within [db_now - 1 day, db_now + 365 days], so the result is clamped
into [now, now + 364 days]. A trained half-life model can replace `interval_days` later
without changing the wire format.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

LADDER_DAYS = (1, 2, 4, 7, 14, 30, 60)
WEAK_MASTERY = 0.6
MAX_AHEAD = timedelta(days=364)


def interval_days(mastery: float, correct_streak: int) -> int:
    """Weak or just-missed topics come back tomorrow; a correct streak spaces reviews out."""
    if correct_streak == 0 or mastery < WEAK_MASTERY:
        return LADDER_DAYS[0]
    return LADDER_DAYS[min(correct_streak, len(LADDER_DAYS) - 1)]


def next_review(last_practice: datetime, mastery: float, correct_streak: int, now: datetime) -> datetime:
    due = last_practice + timedelta(days=interval_days(mastery, correct_streak))
    return min(max(due, now), now + MAX_AHEAD)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp without offset")
    return parsed.astimezone(timezone.utc)
