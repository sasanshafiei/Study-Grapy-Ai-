"""Short database transactions for the chat path, with server-side time limits.

Every chat database step runs in a worker thread (`run_in_threadpool`, AnyIO's
non-abandoning mode). Cancellation (the pipeline deadline, a client
disconnect) is therefore NOT delivered while the synchronous function runs: the
awaiting task keeps waiting until the thread returns, and only then can the
cancellation take effect. The pipeline deadline is thus not an unconditional
wall-clock bound during database work. What bounds the thread itself is
PostgreSQL: each chat transaction sets, with `SET LOCAL`, a statement timeout
and a lock timeout, so one step lasts at most about SHORT_STATEMENT_TIMEOUT_MS
(plus SHORT_LOCK_TIMEOUT_MS while waiting for a lock) per statement. The work
is never abandoned. After each threadpool call run_chat takes an explicit
cancellation checkpoint (stream.py `_checkpoint`), so an expired deadline or a
cancelled request cannot go on to send another upstream request or emit an
answer. Every chat transaction is read-only and its limits end with it.

Not covered here (infrastructure, see docs/ai-chat-integration.md §7): waiting
for a free connection in SQLAlchemy's pool, opening a new TCP connection, and
a network path that stops answering without closing (the server-side timeouts
cannot fire if the packets never arrive).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..services.course_access import require_read

SHORT_STATEMENT_TIMEOUT_MS = 5000
SHORT_LOCK_TIMEOUT_MS = 2000


def apply_short_timeouts(session: Session) -> None:
    """Start the transaction with statement and lock timeouts that end with it."""
    session.execute(text(f"SET LOCAL statement_timeout = {SHORT_STATEMENT_TIMEOUT_MS}"))
    session.execute(text(f"SET LOCAL lock_timeout = {SHORT_LOCK_TIMEOUT_MS}"))


def authorize_read(factory, user: CurrentUser, course_id: uuid.UUID) -> None:
    """Course read access from the database, in its own short read-only transaction (ApiError if lost)."""
    with factory() as session:
        try:
            session.connection()  # begin the transaction; READ ONLY must precede any query
            session.execute(text("SET TRANSACTION READ ONLY"))
            apply_short_timeouts(session)
            require_read(session, user, course_id)
        finally:
            session.rollback()
