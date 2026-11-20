"""In-process limits for M5: chat concurrency slots and the join rate limit.

SINGLE PROCESS ONLY. Both limiters keep their state in this process's memory.
With several API worker processes or replicas every process enforces its own
limits, so the effective limits multiply. That is acceptable for development;
a shared (e.g. Redis-backed) implementation is required before a multi-replica
deployment (docs/courses-api.md, docs/ai-chat-integration.md).

Both are thread-safe (a lock around every state change), so they work from the
event loop and from threadpool code alike. Neither ever waits: a request that
does not fit is refused at once.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable


class Lease:
    """One held chat slot (global + per-user). release() is idempotent and thread-safe."""

    def __init__(self, limiter: "ConcurrencyLimiter", user_id: uuid.UUID) -> None:
        self._limiter = limiter
        self._user_id = user_id
        self._released = False
        self._lock = threading.Lock()

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._limiter._release(self._user_id)


class ConcurrencyLimiter:
    def __init__(self, max_total: int, max_per_user: int) -> None:
        if max_total < 1 or max_per_user < 1:
            raise ValueError("limits must be positive")
        self.max_total = max_total
        self.max_per_user = max_per_user
        self._lock = threading.Lock()
        self._total = 0
        self._per_user: dict[uuid.UUID, int] = {}

    def try_acquire(self, user_id: uuid.UUID) -> Lease | None:
        """A lease, or None when the global or the caller's own limit is reached."""
        with self._lock:
            held = self._per_user.get(user_id, 0)
            if self._total >= self.max_total or held >= self.max_per_user:
                return None
            self._total += 1
            self._per_user[user_id] = held + 1
        return Lease(self, user_id)

    def _release(self, user_id: uuid.UUID) -> None:
        with self._lock:
            self._total -= 1
            held = self._per_user.get(user_id, 0) - 1
            if held > 0:
                self._per_user[user_id] = held
            else:
                self._per_user.pop(user_id, None)  # no entry is kept for idle users

    def in_use(self) -> tuple[int, int]:
        """(slots held in total, users holding at least one slot), for tests and diagnostics."""
        with self._lock:
            return self._total, len(self._per_user)


class RateLimiter:
    """Sliding-window limit of `attempts` per `window` seconds per user.

    Memory is bounded: at most `max_users` users are tracked, each with at most
    `attempts` timestamps. Beyond max_users the least recently seen user is
    forgotten (their window starts again), which only ever loosens that one
    user's limit; it never refuses an unrelated user.
    """

    def __init__(
        self,
        attempts: int,
        window: float,
        max_users: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.attempts = attempts
        self.window = window
        self.max_users = max_users
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: OrderedDict[uuid.UUID, deque[float]] = OrderedDict()

    def hit(self, user_id: uuid.UUID) -> float | None:
        """Record an attempt. None if allowed, else the seconds until the next one is."""
        now = self._clock()
        with self._lock:
            hits = self._hits.get(user_id)
            if hits is None:
                hits = deque()
                self._hits[user_id] = hits
                while len(self._hits) > self.max_users:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(user_id)
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.attempts:
                return max(0.0, self.window - (now - hits[0]))
            hits.append(now)
            return None

    def tracked_users(self) -> int:
        with self._lock:
            return len(self._hits)
