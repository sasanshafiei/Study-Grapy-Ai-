"""Mastery delivery state, in SQL (PostgreSQL is the authority; Redis messages carry identifiers only).

    stream ready --claim--> delivering --complete--> ready (batch cleared, predictions stored)
                               |--model not confirmed at completion--> ready (same batch, no failure)
                               |--response names another model/version/epoch--> ready (same batch, no failure;
                               |      the model becomes incompatible for every stream)
                               |--failure, failures left--> ready (same batch, failure_count + 1, backoff)
                               |--failure exhausted / sequence or epoch problem--> blocked (batch rows failed)
                               |--access revoked--> paused (same batch)
    delivering, lease expired --recovery--> ready | blocked   (counts as a failure)
    paused --maintenance, access restored--> ready
    blocked --operator retry (M10 seam)--> ready (same batch)

Batches. A stream has at most one active batch. Its identity (batch_id, also the request
id) and its exact membership (mastery_outbox.batch_id, immutable by trigger) are committed
BEFORE anything is sent. Every retry, after a timeout, a lost response, an uncertain
commit or a dead worker, resends the same batch id with the same events; events committed
meanwhile wait for the next batch. Features come from the immutable attempt and question
rows, so a resent request is byte-identical.

Order. A batch takes, per topic, a contiguous prefix of the pending events that starts
right after the last delivered sequence number of that topic; a gap blocks the stream
(never skipped). Events of one topic are therefore delivered strictly in topic_seq order,
one batch in flight per stream (head-of-line blocking is intended). Cross-topic order is
approximate (docs/ai-mastery-integration.md).

Fencing. A claim stores a fresh claim_token (renewable; the batch id is not) and a lease
in database time (clock_timestamp() read after the row lock was granted). Every exit of a
claim (completion, failure, block, pause, model suspension) first locks the stream row on
state = 'delivering' AND the token AND the batch, then checks the lease with
clock_timestamp() in a separate statement after that lock (_owns_live_claim). A lost or
expired claim changes nothing: no stream state, counter, batch or global model state; lease
recovery handles it once. No transaction is open during an HTTP call.

Counters. dispatch_count (messages published), claim_count and batch_claims (claims of this
stream and of the current batch, bounded by MASTERY_MAX_BATCH_CLAIMS) and failure_count
(actual delivery failures of the current batch, bounded by MASTERY_MAX_FAILURES) are
separate; pauses and unconfirmed-model releases are claims, not failures. An unavailable AI
service is detected by the capability probe BEFORE any claim, so it consumes nothing.

Lock order: users -> courses -> enrollments -> mastery_model_state -> mastery_streams
-> mastery_outbox -> mastery_scores (app/models.py).
"""

from __future__ import annotations

import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from .pins import PROTOCOL, Pins
from .protocol import BatchEvent, ValidatedUpdate, check_review_bounds

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claim:
    user_id: uuid.UUID
    course_id: uuid.UUID
    batch_id: uuid.UUID
    token: uuid.UUID
    epoch: uuid.UUID
    events: tuple[BatchEvent, ...]
    batch_claims: int


@dataclass(frozen=True)
class NoClaim:
    reason: str  # model_blocked | busy | not_due | nothing_pending | paused | blocked


def safe_code(code: str) -> str:
    """A stored error code: lower-case [a-z0-9_], starting with a letter, at most 40 characters (CHECKed)."""
    cleaned = re.sub(r"[^a-z0-9_]", "_", code.lower()).strip("_") or "error"
    if not cleaned[0].isalpha():
        cleaned = f"e_{cleaned}"
    return cleaned[:40]


def _stream_params(user_id: uuid.UUID, course_id: uuid.UUID) -> dict:
    return {"u": user_id, "c": course_id}


def has_access(session: Session, user_id: uuid.UUID, course_id: uuid.UUID) -> bool:
    """The learner is still an active student enrolled in the course (nothing is sent otherwise)."""
    return (
        session.execute(
            text(
                "SELECT 1 FROM users u JOIN enrollments e ON e.user_id = u.id "
                "JOIN courses c ON c.id = e.course_id "
                "WHERE u.id = :u AND u.status = 'active' AND u.role = 'student' "
                "AND e.course_id = :c AND e.role = 'student'"
            ),
            _stream_params(user_id, course_id),
        ).first()
        is not None
    )


def _model_blocked(session: Session, *, lock: str = "FOR SHARE") -> bool:
    status = session.execute(text(f"SELECT status FROM mastery_model_state WHERE id = 1 {lock}")).scalar_one_or_none()
    return status != "unknown" and status != "compatible"


def _set_failed(session: Session, batch_id: uuid.UUID) -> None:
    session.execute(
        text("UPDATE mastery_outbox SET status = 'failed' WHERE batch_id = :b AND status = 'batched'"), {"b": batch_id}
    )


def _block_locked(session: Session, user_id: uuid.UUID, course_id: uuid.UUID, code: str) -> None:
    """Block a stream whose row the caller has locked; the batch's events become failed (kept)."""
    batch = session.execute(
        text("SELECT batch_id FROM mastery_streams WHERE user_id = :u AND course_id = :c"),
        _stream_params(user_id, course_id),
    ).scalar_one()
    if batch is not None:
        _set_failed(session, batch)
    session.execute(
        text(
            "UPDATE mastery_streams SET state = 'blocked', claim_token = NULL, claimed_at = NULL, "
            "lease_expires_at = NULL, last_dispatched_at = NULL, last_error_code = :code, updated_at = now() "
            "WHERE user_id = :u AND course_id = :c"
        ),
        {**_stream_params(user_id, course_id), "code": safe_code(code)},
    )
    log.warning("mastery_stream_blocked batch_id=%s code=%s", batch, safe_code(code))


# --- batch creation and claim -------------------------------------------------------------------------


def _next_events(session: Session, user_id: uuid.UUID, course_id: uuid.UUID, limit: int) -> list:
    """Per-topic prefixes of the pending events (ordering by (topic_seq, topic_id) keeps every prefix whole)."""
    return session.execute(
        text(
            "SELECT event_id, topic_id, topic_seq FROM mastery_outbox "
            "WHERE user_id = :u AND course_id = :c AND status = 'pending' "
            "ORDER BY topic_seq, topic_id LIMIT :limit FOR UPDATE"
        ),
        {**_stream_params(user_id, course_id), "limit": limit},
    ).all()


def _contiguous(session: Session, user_id: uuid.UUID, rows: list) -> bool:
    """Every topic's selected events start right after its last non-pending event and have no gap."""
    by_topic: dict[uuid.UUID, list[int]] = {}
    for row in rows:
        by_topic.setdefault(row.topic_id, []).append(row.topic_seq)
    done = {
        r.topic_id: r.last
        for r in session.execute(
            text(
                "SELECT topic_id, max(topic_seq) AS last FROM mastery_outbox "
                "WHERE user_id = :u AND topic_id = ANY(:t) AND status <> 'pending' GROUP BY topic_id"
            ),
            {"u": user_id, "t": list(by_topic)},
        ).all()
    }
    for topic, seqs in by_topic.items():
        seqs.sort()
        start = done.get(topic, 0) + 1
        if seqs != list(range(start, start + len(seqs))):
            return False
    return True


def _batch_events(session: Session, batch_id: uuid.UUID) -> list[BatchEvent]:
    rows = session.execute(
        text(
            "SELECT o.event_id, o.topic_id, o.topic_seq, a.is_correct, a.hint_count, a.response_ms, a.submitted_at, "
            "q.qtype, q.difficulty FROM mastery_outbox o "
            "JOIN attempts a ON a.id = o.event_id AND a.user_id = o.user_id AND a.topic_id = o.topic_id "
            "AND a.stats_attempts = o.topic_seq "
            "JOIN questions q ON q.id = a.question_id AND q.course_id = a.course_id "
            "WHERE o.batch_id = :b AND o.status = 'batched' ORDER BY o.topic_id, o.topic_seq"
        ),
        {"b": batch_id},
    ).all()
    return [
        BatchEvent(r.event_id, r.topic_id, r.topic_seq, r.qtype, r.difficulty, bool(r.is_correct),
                   int(r.hint_count or 0), r.response_ms, r.submitted_at)  # fmt: skip
        for r in rows
    ]


def claim(
    session_factory: sessionmaker[Session],
    user_id: uuid.UUID,
    course_id: uuid.UUID,
    settings: Settings,
    pins: Pins,
    max_events: int,
) -> Claim | NoClaim:
    """Claim the stream's batch (creating it first if none is active) in one short transaction."""
    params = _stream_params(user_id, course_id)
    with session_factory() as session:
        if _model_blocked(session):
            session.rollback()
            return NoClaim("model_blocked")
        stream = session.execute(
            text(
                "SELECT state, batch_id, batch_epoch, batch_size, batch_claims, next_attempt_at "
                "FROM mastery_streams WHERE user_id = :u AND course_id = :c FOR NO KEY UPDATE SKIP LOCKED"
            ),
            params,
        ).one_or_none()
        if stream is None:
            session.rollback()
            return NoClaim("busy")
        now: datetime = session.execute(text("SELECT clock_timestamp()")).scalar_one()  # after the lock was granted
        if stream.state != "ready" or stream.next_attempt_at > now:
            session.rollback()
            return NoClaim("not_due")
        if not has_access(session, user_id, course_id):
            session.execute(
                text(
                    "UPDATE mastery_streams SET state = 'paused', last_error_code = 'access_revoked', "
                    "last_dispatched_at = NULL, updated_at = now() WHERE user_id = :u AND course_id = :c"
                ),
                params,
            )
            session.commit()
            log.info("mastery_stream_paused batch_id=%s reason=access_revoked", stream.batch_id)
            return NoClaim("paused")
        batch_id = stream.batch_id
        if batch_id is None:
            rows = _next_events(session, user_id, course_id, max_events)
            if not rows:
                session.execute(
                    text("UPDATE mastery_streams SET last_dispatched_at = NULL WHERE user_id = :u AND course_id = :c"),
                    params,
                )
                session.commit()
                return NoClaim("nothing_pending")
            if not _contiguous(session, user_id, rows):
                _block_locked(session, user_id, course_id, "sequence_gap")
                session.commit()
                return NoClaim("blocked")
            batch_id = uuid.uuid4()
            marked = session.execute(
                text(
                    "UPDATE mastery_outbox SET status = 'batched', batch_id = :b, batched_at = :now "
                    "WHERE event_id = ANY(:ids) AND status = 'pending'"
                ),
                {"b": batch_id, "now": now, "ids": [r.event_id for r in rows]},
            ).rowcount
            if marked != len(rows):  # impossible under the row locks; refuse rather than send a partial batch
                session.rollback()
                return NoClaim("busy")
            session.execute(
                text(
                    "UPDATE mastery_streams SET batch_id = :b, batch_epoch = :e, batch_size = :n, batch_created_at = :now, "
                    "batch_claims = 0, failure_count = 0, last_error_code = NULL WHERE user_id = :u AND course_id = :c"
                ),
                {**params, "b": batch_id, "e": pins.state_epoch, "n": len(rows), "now": now},
            )
            batch_size, batch_claims = len(rows), 0
            log.info("mastery_batch_created batch_id=%s events=%d", batch_id, batch_size)
        else:
            batch_size, batch_claims = stream.batch_size, stream.batch_claims
            if stream.batch_epoch != pins.state_epoch:
                _block_locked(session, user_id, course_id, "epoch_changed")
                session.commit()
                return NoClaim("blocked")
            if batch_size > max_events:
                _block_locked(session, user_id, course_id, "capacity_reduced")
                session.commit()
                return NoClaim("blocked")
        if batch_claims >= settings.mastery_max_batch_claims:
            _block_locked(session, user_id, course_id, "claim_limit_exhausted")
            session.commit()
            return NoClaim("blocked")
        events = _batch_events(session, batch_id)
        if len(events) != batch_size or len({e.event_id for e in events}) != batch_size:
            _block_locked(session, user_id, course_id, "batch_inconsistent")
            session.commit()
            return NoClaim("blocked")
        token = uuid.uuid4()
        session.execute(
            text(
                "UPDATE mastery_streams SET state = 'delivering', claim_token = :t, claimed_at = :now, "
                "lease_expires_at = :now + :lease * interval '1 second', claim_count = claim_count + 1, "
                "batch_claims = batch_claims + 1, updated_at = now() WHERE user_id = :u AND course_id = :c"
            ),
            {**params, "t": token, "now": now, "lease": settings.mastery_lease_seconds},
        )
        session.commit()
    return Claim(user_id, course_id, batch_id, token, pins.state_epoch, tuple(events), batch_claims + 1)


# --- leaving the delivering state ---------------------------------------------------------------------


_LOCK_OWNED = (
    "SELECT 1 FROM mastery_streams "
    "WHERE user_id = :u AND course_id = :c AND state = 'delivering' AND claim_token = :t AND batch_id = :b "
    "FOR NO KEY UPDATE"
)
# Evaluated in a separate statement, once the row lock is held: clock_timestamp() then reflects the time
# after any wait for the lock, never a value computed before it.
_LEASE_LIVE = (
    "SELECT lease_expires_at > clock_timestamp() FROM mastery_streams "
    "WHERE user_id = :u AND course_id = :c AND claim_token = :t AND batch_id = :b"
)
_RELEASE = (
    "UPDATE mastery_streams SET state = :state, claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL, "
    "last_dispatched_at = NULL, next_attempt_at = clock_timestamp() + :delay * interval '1 second', "
    "last_error_code = :code, updated_at = now() WHERE user_id = :u AND course_id = :c"
)


def _owns_live_claim(session: Session, c: Claim) -> bool:
    """Lock the stream row if this claim still owns it, then check its lease in database time.

    False for a lost claim (another token, recovered, completed) AND for an expired lease: an
    expired attempt must not change anything. Recovery owns an expired claim (it counts the
    failure once and releases it), and a newer claim must never be overwritten by an older one.
    """
    params = {**_stream_params(c.user_id, c.course_id), "t": c.token, "b": c.batch_id}
    if session.execute(text(_LOCK_OWNED), params).first() is None:
        return False
    return bool(session.execute(text(_LEASE_LIVE), params).scalar_one())


def backoff_seconds(failures: int, settings: Settings) -> float:
    delay = min(settings.mastery_backoff_base_seconds * (2 ** max(failures - 1, 0)), settings.mastery_backoff_max_seconds)
    return delay * random.uniform(0.8, 1.2)


def pause(session_factory: sessionmaker[Session], c: Claim) -> str:
    """Access was revoked before sending: nothing is sent; the batch and its events are kept."""
    with session_factory() as session:
        if not _owns_live_claim(session, c):
            session.rollback()
            log.warning("mastery_claim_stale batch_id=%s", c.batch_id)
            return "stale"
        session.execute(
            text(_RELEASE),
            {**_stream_params(c.user_id, c.course_id), "state": "paused", "delay": 0, "code": "access_revoked"},
        )
        session.commit()
    log.info("mastery_stream_paused batch_id=%s reason=access_revoked", c.batch_id)
    return "paused"


def fail(session_factory: sessionmaker[Session], c: Claim, settings: Settings, code: str) -> str:
    """A delivery failure of this batch (failure_count + 1, never refunded); blocked once exhausted."""
    with session_factory() as session:
        if not _owns_live_claim(session, c):
            session.rollback()
            log.warning("mastery_claim_stale batch_id=%s", c.batch_id)
            return "stale"
        failures = session.execute(
            text(
                "UPDATE mastery_streams SET failure_count = failure_count + 1 WHERE user_id = :u AND course_id = :c "
                "RETURNING failure_count"
            ),
            _stream_params(c.user_id, c.course_id),
        ).scalar_one()
        if failures >= settings.mastery_max_failures:
            _block_locked(session, c.user_id, c.course_id, code)
            session.commit()
            return "blocked"
        session.execute(
            text(_RELEASE),
            {**_stream_params(c.user_id, c.course_id), "state": "ready", "delay": backoff_seconds(failures, settings),
             "code": safe_code(code)},
        )  # fmt: skip
        session.commit()
    log.warning("mastery_batch_failed batch_id=%s code=%s failures=%d", c.batch_id, safe_code(code), failures)
    return "retry"


def block(session_factory: sessionmaker[Session], c: Claim, code: str, *, model_reason: str | None = None) -> str:
    """Terminal for this stream until an operator acts; with model_reason the model is blocked for everyone."""
    with session_factory() as session:
        if model_reason is not None:
            session.execute(text("SELECT 1 FROM mastery_model_state WHERE id = 1 FOR NO KEY UPDATE"))
        if not _owns_live_claim(session, c):
            session.rollback()
            log.warning("mastery_claim_stale batch_id=%s", c.batch_id)
            return "stale"
        if model_reason is not None:
            _mark_model_incompatible(session, model_reason)
        _block_locked(session, c.user_id, c.course_id, code)
        session.commit()
    return "blocked"


def suspend_for_model(session_factory: sessionmaker[Session], c: Claim, reason: str) -> str:
    """The update response named another protocol, model, version or state epoch (or claimed to be a mock).

    The model, not this learner, is the problem: mastery_model_state becomes 'incompatible'
    (sticky: dispatch and claims stop for every stream and every prediction is hidden until an
    operator reconciles and calls clear_model_block). This stream is released to ready WITH its
    batch and without counting a failure, so the same batch is resent after reconciliation.
    Fenced like every other exit: a lost or expired claim changes nothing, global state included.
    Lock order: mastery_model_state, then the stream.
    """
    with session_factory() as session:
        session.execute(text("SELECT 1 FROM mastery_model_state WHERE id = 1 FOR NO KEY UPDATE"))
        if not _owns_live_claim(session, c):
            session.rollback()
            log.warning("mastery_claim_stale batch_id=%s", c.batch_id)
            return "stale"
        _mark_model_incompatible(session, reason)
        session.execute(
            text(_RELEASE),
            {**_stream_params(c.user_id, c.course_id), "state": "ready", "delay": 0, "code": safe_code(reason)},
        )
        session.commit()
    return "model_blocked"


# --- completion -------------------------------------------------------------------------------------------


def complete(
    session_factory: sessionmaker[Session], c: Claim, update: ValidatedUpdate, pins: Pins
) -> str:
    """Persist a validated response atomically: scores, delivered events, stream back to ready.

    Returns "completed", "stale" (lost the claim, lease expired: nothing written; recovery
    resends the same batch), "model_blocked" (claim released, no failure) or "blocked".
    """
    params = _stream_params(c.user_id, c.course_id)
    with session_factory() as session:
        model = session.execute(
            text("SELECT status, model_name, model_version, state_epoch, pseudonym_key_id FROM mastery_model_state "
                 "WHERE id = 1 FOR SHARE")  # fmt: skip
        ).one()
        if not _owns_live_claim(session, c):
            session.rollback()
            log.warning("mastery_completion_stale batch_id=%s", c.batch_id)
            return "stale"
        current = (model.model_name, model.model_version, model.state_epoch, model.pseudonym_key_id)
        if model.status != "compatible" or current != (pins.model_name, pins.model_version, pins.state_epoch, pins.key_id):
            session.execute(
                text(_RELEASE),
                {**params, "state": "ready", "delay": 0, "code": "model_not_confirmed"},
            )
            session.commit()
            return "model_blocked"
        if c.epoch != pins.state_epoch:
            _block_locked(session, c.user_id, c.course_id, "epoch_changed")
            session.commit()
            return "blocked"
        now: datetime = session.execute(text("SELECT clock_timestamp()")).scalar_one()
        try:
            check_review_bounds(update.scores, now)
        except Exception:  # ProtocolViolation: counts as a failure of this delivery
            session.rollback()
            raise
        events = session.execute(
            text("SELECT event_id, status FROM mastery_outbox WHERE batch_id = :b ORDER BY event_id FOR UPDATE"),
            {"b": c.batch_id},
        ).all()
        if len(events) != len(c.events) or any(e.status != "batched" for e in events):
            _block_locked(session, c.user_id, c.course_id, "batch_inconsistent")
            session.commit()
            return "blocked"
        first_seq: dict[uuid.UUID, int] = {}
        for event in c.events:
            first_seq[event.topic_id] = min(first_seq.get(event.topic_id, event.topic_seq), event.topic_seq)
        prior = {
            r.topic_id: r
            for r in session.execute(
                text(
                    "SELECT topic_id, model_epoch, through_seq FROM mastery_scores WHERE user_id = :u "
                    "AND topic_id = ANY(:t) ORDER BY topic_id FOR UPDATE"
                ),
                {"u": c.user_id, "t": sorted(first_seq, key=str)},
            ).all()
        }
        for topic, first in first_seq.items():
            row = prior.get(topic)
            accepted = row.through_seq if row is not None and row.model_epoch == pins.state_epoch else 0
            if first != accepted + 1:
                _block_locked(session, c.user_id, c.course_id, "sequence_mismatch")
                session.commit()
                return "blocked"
        for score in update.scores:
            written = session.execute(
                text(
                    "INSERT INTO mastery_scores AS m (user_id, topic_id, course_id, mastery, attempts, next_review_at, "
                    "updated_at, protocol, model_name, model_version, model_epoch, pseudonym_key_id, through_seq, "
                    "attempts_seen, batch_id, received_at) VALUES (:u, :t, :c, :m, :seq, :review, "
                    "(now() AT TIME ZONE 'UTC'), :protocol, :name, :version, :epoch, :key_id, :seq, :seq, :b, :now) "
                    "ON CONFLICT (user_id, topic_id) DO UPDATE SET mastery = EXCLUDED.mastery, "
                    "attempts = EXCLUDED.attempts, next_review_at = EXCLUDED.next_review_at, "
                    "updated_at = EXCLUDED.updated_at, protocol = EXCLUDED.protocol, model_name = EXCLUDED.model_name, "
                    "model_version = EXCLUDED.model_version, model_epoch = EXCLUDED.model_epoch, "
                    "pseudonym_key_id = EXCLUDED.pseudonym_key_id, through_seq = EXCLUDED.through_seq, "
                    "attempts_seen = EXCLUDED.attempts_seen, batch_id = EXCLUDED.batch_id, "
                    "received_at = EXCLUDED.received_at "
                    "WHERE m.course_id = EXCLUDED.course_id AND (m.model_epoch IS NULL "
                    "OR m.model_epoch IS DISTINCT FROM EXCLUDED.model_epoch OR m.through_seq < EXCLUDED.through_seq)"
                ),
                {
                    "u": c.user_id, "t": score.topic_id, "c": c.course_id, "m": score.mastery, "seq": score.through_seq,
                    "review": score.next_review_at, "protocol": PROTOCOL, "name": pins.model_name,
                    "version": pins.model_version, "epoch": pins.state_epoch, "key_id": pins.key_id, "b": c.batch_id,
                    "now": now,
                },
            ).rowcount  # fmt: skip
            if written != 1:  # a newer prediction exists: never overwritten by an older sequence
                session.rollback()
                log.warning("mastery_completion_stale batch_id=%s reason=newer_prediction", c.batch_id)
                return "stale"
        session.execute(
            text(
                "UPDATE mastery_outbox SET status = 'delivered', delivered_at = :now, "
                "outcome = CASE WHEN event_id = ANY(:applied) THEN 'applied' ELSE 'duplicate' END "
                "WHERE batch_id = :b AND status = 'batched'"
            ),
            {"b": c.batch_id, "now": now, "applied": list(update.applied)},
        )
        session.execute(
            text(
                "UPDATE mastery_streams SET state = 'ready', batch_id = NULL, batch_epoch = NULL, batch_size = NULL, "
                "batch_created_at = NULL, batch_claims = 0, claim_token = NULL, claimed_at = NULL, "
                "lease_expires_at = NULL, last_dispatched_at = NULL, failure_count = 0, last_error_code = NULL, "
                "next_attempt_at = :now, updated_at = now() WHERE user_id = :u AND course_id = :c"
            ),
            {**params, "now": now},
        )
        session.commit()
    log.info(
        "mastery_batch_delivered batch_id=%s applied=%d duplicate=%d topics=%d",
        c.batch_id, len(update.applied), len(update.duplicate), len(update.scores),
    )  # fmt: skip
    return "completed"


# --- model state (capability probes) ----------------------------------------------------------------------


def _mark_model_incompatible(session: Session, reason: str) -> None:
    """Sticky: an already incompatible model keeps its first reason (the caller holds the row lock)."""
    changed = session.execute(
        text(
            "UPDATE mastery_model_state SET status = 'incompatible', reason = :r, probed_at = clock_timestamp(), "
            "updated_at = now() WHERE id = 1 AND status <> 'incompatible'"
        ),
        {"r": safe_code(reason)},
    ).rowcount
    if changed:
        log.error("mastery_model_blocked reason=%s", safe_code(reason))


def probe_started_at(session_factory: sessionmaker[Session]) -> datetime:
    with session_factory() as session:
        value = session.execute(text("SELECT clock_timestamp()")).scalar_one()
        session.rollback()
    return value


def record_probe(
    session_factory: sessionmaker[Session], started_at: datetime, status: str, reason: str | None, pins: Pins,
    service: str | None,
) -> bool:
    """Record a definite probe outcome; an older probe never overwrites a newer one, and 'incompatible' is sticky.

    Only an operator (clear_model_block) leaves 'incompatible': a remote that changes back
    is not trusted automatically, and a learner state loss is not repaired by a probe.
    """
    if status not in ("compatible", "incompatible"):
        return False
    with session_factory() as session:
        if status == "compatible":
            changed = session.execute(
                text(
                    "UPDATE mastery_model_state SET status = 'compatible', reason = NULL, protocol = :p, service = :s, "
                    "model_name = :n, model_version = :v, state_epoch = :e, pseudonym_key_id = :k, "
                    "probe_started_at = :started, probed_at = clock_timestamp(), updated_at = now() "
                    "WHERE id = 1 AND status <> 'incompatible' "
                    "AND (probe_started_at IS NULL OR probe_started_at <= :started)"
                ),
                {"p": PROTOCOL, "s": service, "n": pins.model_name, "v": pins.model_version, "e": pins.state_epoch,
                 "k": pins.key_id, "started": started_at},
            ).rowcount  # fmt: skip
        else:
            changed = session.execute(
                text(
                    "UPDATE mastery_model_state SET status = 'incompatible', reason = :r, "
                    "probe_started_at = :started, probed_at = clock_timestamp(), updated_at = now() "
                    "WHERE id = 1 AND status <> 'incompatible' "
                    "AND (probe_started_at IS NULL OR probe_started_at <= :started)"
                ),
                {"r": safe_code(reason or "incompatible"), "started": started_at},
            ).rowcount
            if changed:
                log.error("mastery_model_blocked reason=%s", reason)
        session.commit()
    return bool(changed)


# --- maintenance: recovery, dispatch reservations, paused streams --------------------------------------------


def recover(session_factory: sessionmaker[Session], settings: Settings, limit: int) -> tuple[int, int]:
    """Expired leases count as failed deliveries (whatever MASTERY_DELIVERY_ENABLED says). (requeued, blocked)."""
    requeued = blocked = 0
    with session_factory() as session:
        expired = session.execute(
            text(
                "SELECT user_id, course_id, claim_token, batch_id FROM mastery_streams "
                "WHERE state = 'delivering' AND lease_expires_at < clock_timestamp() "
                "ORDER BY lease_expires_at, user_id, course_id LIMIT :limit"
            ),
            {"limit": limit},
        ).all()
        session.rollback()
    for row in expired:
        with session_factory() as session:
            locked = session.execute(
                text(
                    "SELECT failure_count FROM mastery_streams WHERE user_id = :u AND course_id = :c "
                    "AND state = 'delivering' AND claim_token = :t AND lease_expires_at < clock_timestamp() "
                    "FOR NO KEY UPDATE SKIP LOCKED"
                ),
                {"u": row.user_id, "c": row.course_id, "t": row.claim_token},
            ).one_or_none()
            if locked is None:
                session.rollback()
                continue
            failures = locked.failure_count + 1
            session.execute(
                text("UPDATE mastery_streams SET failure_count = :f WHERE user_id = :u AND course_id = :c"),
                {"u": row.user_id, "c": row.course_id, "f": failures},
            )
            if failures >= settings.mastery_max_failures:
                _block_locked(session, row.user_id, row.course_id, "lease_expired")
                blocked += 1
            else:
                session.execute(
                    text(_RELEASE),
                    {"u": row.user_id, "c": row.course_id, "state": "ready",
                     "delay": backoff_seconds(failures, settings), "code": "lease_expired"},
                )  # fmt: skip
                requeued += 1
            session.commit()
        log.warning("mastery_lease_recovered batch_id=%s failures=%d", row.batch_id, failures)
    return requeued, blocked


_DUE = """
    SELECT s.user_id, s.course_id FROM mastery_streams s
    WHERE s.state = 'ready' AND s.next_attempt_at <= now()
      AND (s.last_dispatched_at IS NULL OR s.last_dispatched_at <= now() - :redispatch * interval '1 second')
      AND (s.batch_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM mastery_outbox o WHERE o.user_id = s.user_id AND o.course_id = s.course_id
            AND o.status = 'pending'))
    ORDER BY s.next_attempt_at, s.user_id, s.course_id LIMIT :limit
    FOR NO KEY UPDATE OF s SKIP LOCKED
"""


def reserve(session: Session, settings: Settings, limit: int) -> tuple[list[tuple[uuid.UUID, uuid.UUID]], object]:
    rows = session.execute(
        text(
            f"WITH due AS ({_DUE}) UPDATE mastery_streams s SET last_dispatched_at = now(), "
            "dispatch_count = s.dispatch_count + 1, updated_at = now() FROM due "
            "WHERE s.user_id = due.user_id AND s.course_id = due.course_id RETURNING s.user_id, s.course_id, "
            "s.last_dispatched_at"
        ),
        {"limit": limit, "redispatch": settings.mastery_redispatch_after_seconds},
    ).all()
    if not rows:
        return [], None
    return sorted(((r.user_id, r.course_id) for r in rows), key=str), rows[0].last_dispatched_at


def release_reservations(session_factory: sessionmaker[Session], streams: list, reserved_at) -> int:
    released = 0
    with session_factory() as session:
        for user_id, course_id in streams:
            released += session.execute(
                text(
                    "UPDATE mastery_streams SET last_dispatched_at = NULL, updated_at = now() "
                    "WHERE user_id = :u AND course_id = :c AND state = 'ready' AND last_dispatched_at = :at"
                ),
                {"u": user_id, "c": course_id, "at": reserved_at},
            ).rowcount
        session.commit()
    return released


def resume_paused(session_factory: sessionmaker[Session], limit: int) -> int:
    """Paused streams whose learner has access again become ready (their batch is kept)."""
    resumed = 0
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT user_id, course_id FROM mastery_streams WHERE state = 'paused' "
                "ORDER BY updated_at, user_id, course_id LIMIT :limit FOR NO KEY UPDATE SKIP LOCKED"
            ),
            {"limit": limit},
        ).all()
        for row in rows:
            if has_access(session, row.user_id, row.course_id):
                session.execute(
                    text(
                        "UPDATE mastery_streams SET state = 'ready', last_error_code = NULL, next_attempt_at = now(), "
                        "last_dispatched_at = NULL, updated_at = now() WHERE user_id = :u AND course_id = :c"
                    ),
                    {"u": row.user_id, "c": row.course_id},
                )
                resumed += 1
            else:
                session.execute(
                    text("UPDATE mastery_streams SET updated_at = now() WHERE user_id = :u AND course_id = :c"),
                    {"u": row.user_id, "c": row.course_id},
                )
        session.commit()
    return resumed


def model_blocked(session_factory: sessionmaker[Session]) -> bool:
    with session_factory() as session:
        blocked = _model_blocked(session, lock="")
        session.rollback()
    return blocked
