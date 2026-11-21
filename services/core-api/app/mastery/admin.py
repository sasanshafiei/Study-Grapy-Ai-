"""Operator seams for M10 (no endpoint in M8): audited, explicit, never automatic.

retry_stream() puts a blocked (or paused) stream back to ready with its SAME batch: the
failed events become batched again, the failure and claim counters of the batch restart,
and the stream records who did it and when (operator_retry_count, last_operator_retry_at,
last_operator_actor_id). It does not help a batch built for another state epoch: an epoch
transition needs the reconciliation procedure in docs/ai-mastery-integration.md.

clear_model_block() returns mastery_model_state from 'incompatible' to 'unknown', so the
next capability probe decides again. It does not reset any learner state: after an
ai_state_mismatch the remote state must be reconciled first (same document).

Both write a log line with the actor and fixed codes only.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

REASON = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _actor_is_admin(session: Session, actor_id: uuid.UUID) -> bool:
    return (
        session.execute(
            text("SELECT 1 FROM users WHERE id = :a AND role = 'admin' AND status = 'active' FOR SHARE"), {"a": actor_id}
        ).first()
        is not None
    )


def retry_stream(
    session_factory: sessionmaker[Session], user_id: uuid.UUID, course_id: uuid.UUID, actor_id: uuid.UUID, reason: str
) -> str:
    """Returns "retried", "not_blocked", "unknown_stream" or "not_allowed"."""
    if not REASON.match(reason):
        raise ValueError("reason must be a short lower-case code")
    with session_factory() as session:
        if not _actor_is_admin(session, actor_id):
            session.rollback()
            return "not_allowed"
        row = session.execute(
            text(
                "SELECT state, batch_id FROM mastery_streams WHERE user_id = :u AND course_id = :c FOR NO KEY UPDATE"
            ),
            {"u": user_id, "c": course_id},
        ).one_or_none()
        if row is None:
            session.rollback()
            return "unknown_stream"
        if row.state not in ("blocked", "paused"):
            session.rollback()
            return "not_blocked"
        if row.batch_id is not None:
            session.execute(
                text("UPDATE mastery_outbox SET status = 'batched' WHERE batch_id = :b AND status = 'failed'"),
                {"b": row.batch_id},
            )
        session.execute(
            text(
                "UPDATE mastery_streams SET state = 'ready', failure_count = 0, "
                "batch_claims = 0, last_error_code = NULL, "
                "next_attempt_at = now(), last_dispatched_at = NULL, operator_retry_count = operator_retry_count + 1, "
                "last_operator_retry_at = now(), last_operator_actor_id = :a, updated_at = now() "
                "WHERE user_id = :u AND course_id = :c"
            ),
            {"u": user_id, "c": course_id, "a": actor_id},
        )
        session.commit()
    log.warning("mastery_operator_retry actor_id=%s batch_id=%s reason=%s", actor_id, row.batch_id, reason)
    return "retried"


def clear_model_block(session_factory: sessionmaker[Session], actor_id: uuid.UUID, reason: str) -> str:
    """Returns "cleared", "not_blocked" or "not_allowed"."""
    if not REASON.match(reason):
        raise ValueError("reason must be a short lower-case code")
    with session_factory() as session:
        if not _actor_is_admin(session, actor_id):
            session.rollback()
            return "not_allowed"
        changed = session.execute(
            text(
                "UPDATE mastery_model_state SET status = 'unknown', reason = NULL, protocol = NULL, service = NULL, "
                "model_name = NULL, model_version = NULL, state_epoch = NULL, pseudonym_key_id = NULL, "
                "probe_started_at = clock_timestamp(), probed_at = NULL, updated_at = now() "
                "WHERE id = 1 AND status = 'incompatible'"
            )
        ).rowcount
        session.commit()
    if not changed:
        return "not_blocked"
    log.warning("mastery_operator_model_unblocked actor_id=%s reason=%s", actor_id, reason)
    return "cleared"
