"""Provider request budgets, reserved atomically with database time (one row per source).

A request may start only after a reservation succeeded. One UPDATE, by the
database clock, both checks and consumes the budget:

- blocked_until <= now(): no Retry-After / maxlag block and the minimum
  spacing since the previous request (min_interval_ms) has passed;
- the fixed window (window_seconds from window_started_at) still has room,
  or has ended (then a new window starts with this request).

It also moves blocked_until to now() + min_interval_ms, so concurrent workers
(several harvest processes, or several runs) cannot exceed the window or the
spacing: PostgreSQL serializes the UPDATEs on the row. A refused reservation
returns how long to wait; the caller sleeps briefly or yields its run.

Throttling (HTTP 429/503 with or without Retry-After, maxlag, ratelimited)
moves blocked_until forward to now() + the provider's FULL delay, and never
backwards: no request is made earlier than the provider permits. The delay
is never shortened by a cap. A cap (HARVEST_RETRY_AFTER_MAX_SECONDS) bounds
only how soon the run is looked at again (its scheduling); the budget still
refuses requests until blocked_until. A delay that cannot be represented
safely (non-finite, or longer than MAX_BLOCK_SECONDS) suspends the source
(sources.enabled = false) for operator intervention instead.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


log = logging.getLogger(__name__)

# The longest provider delay stored as a block (one year). A longer or non-finite delay is not scheduled:
# the source is suspended so that an operator decides.
MAX_BLOCK_SECONDS = 366 * 86400


class BudgetMissing(Exception):
    """The source has no budget row (not initialized with init-source)."""


@dataclass(frozen=True)
class Reservation:
    granted: bool
    wait_seconds: float = 0.0


_WINDOW_OVER = "window_started_at + window_seconds * interval '1 second' <= now()"

_RESERVE = text(
    f"""
    UPDATE harvest_provider_budgets
    SET window_started_at = CASE WHEN {_WINDOW_OVER} THEN now() ELSE window_started_at END,
        used_requests = CASE WHEN {_WINDOW_OVER} THEN 1 ELSE used_requests + 1 END,
        blocked_until = now() + min_interval_ms * interval '1 millisecond',
        updated_at = now()
    WHERE source_id = :source AND blocked_until <= now()
      AND ({_WINDOW_OVER} OR used_requests < max_requests)
    RETURNING used_requests
    """
)

_WAIT = text(
    f"""
    SELECT greatest(
        extract(epoch FROM blocked_until - now()),
        CASE WHEN NOT ({_WINDOW_OVER}) AND used_requests >= max_requests
             THEN extract(epoch FROM window_started_at + window_seconds * interval '1 second' - now())
             ELSE 0 END,
        0)::float8 AS wait
    FROM harvest_provider_budgets WHERE source_id = :source
    """
)


def reserve(session_factory: sessionmaker[Session], source_id: uuid.UUID) -> Reservation:
    with session_factory() as session:
        granted = session.execute(_RESERVE, {"source": source_id}).scalar_one_or_none()
        if granted is not None:
            session.commit()
            return Reservation(True)
        wait = session.execute(_WAIT, {"source": source_id}).scalar_one_or_none()
        session.rollback()
    if wait is None:
        raise BudgetMissing(str(source_id))
    return Reservation(False, max(float(wait), 0.001))


def block(session_factory: sessionmaker[Session], source_id: uuid.UUID, seconds: float) -> bool:
    """Honour a provider delay: no request to this source before now() + seconds, never earlier.

    Returns True when the full delay is stored (blocked_until only moves
    forward). Returns False when the delay is not representable safely: the
    source is then suspended (enabled = false) and an operator must re-enable
    it after reading the provider's notice. No cap shortens the delay.
    """
    delay = float(seconds)
    if math.isfinite(delay) and delay <= MAX_BLOCK_SECONDS:
        with session_factory() as session:
            session.execute(
                text(
                    "UPDATE harvest_provider_budgets SET blocked_until = greatest(blocked_until, "
                    "now() + :delay * interval '1 second'), updated_at = now() WHERE source_id = :source"
                ),
                {"source": source_id, "delay": max(delay, 0.0)},
            )
            session.commit()
        return True
    with session_factory() as session:
        session.execute(text("UPDATE sources SET enabled = false WHERE id = :source"), {"source": source_id})
        session.commit()
    log.error("harvest_source_suspended source_id=%s reason=unrepresentable_retry_after", source_id)
    return False


def blocked_for(session_factory: sessionmaker[Session], source_id: uuid.UUID) -> float:
    with session_factory() as session:
        wait = session.execute(_WAIT, {"source": source_id}).scalar_one_or_none()
        session.rollback()
    if wait is None:
        raise BudgetMissing(str(source_id))
    return float(wait)
