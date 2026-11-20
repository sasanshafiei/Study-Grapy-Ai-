"""Bounded synchronous quiz generation with durable, fenced, idempotent sessions.

Request order (POST /courses/{id}/quizzes, after authentication, practice
authorization, QUIZ_ENABLED, the Idempotency-Key and the body were checked):

1. lookup (short tx): an existing session for (user, Idempotency-Key) is
   answered from the database alone, without the AI service, without the
   generation switch and without quota: same fingerprint and ready -> replay
   (200); generating -> 409 quiz_generation_in_progress (an expired lease is
   recovered to failed first); failed -> the same stable error again; a
   different fingerprint (other course, topics, count, difficulty or scope)
   -> 409 idempotency_key_reused.
2. QUIZ_GENERATION_ENABLED and a configured client, else 503; then an
   in-process slot (global QUIZ_MAX_CONCURRENT, one per user), else 429.
3. capability probe (cached): mock, protocol, scope, types and limits; a
   refusal here creates nothing and consumes no quota (503).
4. reserve (short tx A): re-authorize with FOR SHARE locks, lookup again,
   lock the requested topics, recover this user's expired generating session,
   reserve one unit of the daily quota (atomic upsert) and insert the session
   `generating` with a random attempt token and a lease in database time.
   One generating session per user is a partial unique index (429).
5. generate: exactly one upstream POST (no retry), then strict validation
   of the whole response (app/quiz/protocol.py).
6. persist (short tx C): re-authorize with locks, lock the session FOR
   UPDATE and require status generating + our token + an unexpired lease
   (fencing: a generator that lost its lease never finalizes), lock and
   verify every citation (provenance), check the pipeline deadline, insert
   questions (choices shuffled with a CSPRNG, then labelled) and their
   assignments, mark the session ready, build the response, commit.
Steps 3-5 run under one absolute deadline (QUIZ_PIPELINE_DEADLINE_SECONDS)
enforced by AnyIO. Step 6 runs in a worker thread that AnyIO cannot interrupt,
so it carries the same deadline (a monotonic instant) with it: the transaction
bounds each lock wait and statement by the time left (`SET LOCAL`), and checks
the deadline after every database step and again immediately before COMMIT. A
deadline that expires before COMMIT rolls the transaction back and fails the
session `deadline`. Once COMMIT has been issued the deadline no longer
matters: a committed quiz is `ready`, is returned (or replayed) as such, and
is never reported failed; nothing here, and no HTTP deadline, can roll it
back. What no deadline bounds: COMMIT itself (a stalled network or
synchronous replica), a wait for a pool connection, and delivery of the
response to the client. No database transaction is open during any network
call.

Lease and fencing use the database's wall clock (`clock_timestamp()`), never
`now()`, which PostgreSQL freezes at the start of the transaction: the lease
is checked again after the session row lock was acquired (a lock wait can
outlast the lease) and once more in the finalizing UPDATE itself, whose WHERE
clause repeats status, token and lease. A session whose lease expired is
recovered to failed only if it is still `generating` when the recovering UPDATE
gets its row lock; the outcome reported afterwards is always re-read from the
database, so a completion that raced the recovery is reported as ready.

Before any sensitive body leaves the service (a ready quiz, a replay, graded
feedback) the router authorizes the student again in a short read-only
transaction opened after the data transaction ended (access.authorize_practice).
That narrows, but cannot close, the window between that check and the last
byte on the wire (docs/quiz-api.md, "Revocation").

The attempt token is chosen BEFORE step 4 and is the handle of every cleanup:
a reservation that committed but whose result was lost (deadline, cancellation
or a dropped connection while the thread returned) is failed by
(user, Idempotency-Key, token), fenced on that token, so it is resolved without
ever having seen its id and never overwrites another outcome.

Failures after step 4 mark the session failed with an internal code (fenced
on the token, so it never overwrites another outcome) and answer from the
database, so the first response and every replay agree. Cancellation marks
the session failed in a shielded scope; a crash leaves it generating until
its lease expires and recovery (here on the next request of that user, and in
the maintenance recovery task) marks it failed `lease_expired`. A commit whose
outcome is unknown (connection lost during COMMIT) is resolved the same way:
the fenced failure update is a no-op if the commit happened, and the lookup
then returns the ready quiz.

Quota: every reservation (step 4) consumes one unit, whatever happens next
(ready, rejected, timed out, cancelled, abandoned). Nothing before step 4
consumes quota, and a replay never does. A reservation rolled back in step 4
itself (busy, conflict) releases its unit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
import uuid
from dataclasses import dataclass

import anyio
import anyio.lowlevel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from ..auth import CurrentUser
from ..chat.verify import VerificationFailed
from ..config import Settings
from ..errors import ApiError
from .access import authorize_practice, require_practice, short_tx
from .client import QuizUpstreamError
from .diagnostics import describe, sqlstate_of
from .protocol import (
    PROTOCOL,
    SERVICE,
    ProposedQuestion,
    ProposedQuiz,
    ProtocolViolation,
    build_request,
    negotiate_types,
    validate_response,
)
from .provenance import verify_question_citations
from .views import failure_status, quiz_view

log = logging.getLogger(__name__)

GRADING_METHOD = "choice_id/1"
CHOICE_LABELS = "abcdef"
TRUE_FALSE_CHOICES = ({"id": "true", "text": "True"}, {"id": "false", "text": "False"})
FINGERPRINT_VERSION = 1
RETRY_AFTER_SECONDS = 5


@dataclass(frozen=True)
class QuizRequest:
    course_id: uuid.UUID
    idempotency_key: uuid.UUID
    topic_ids: tuple[uuid.UUID, ...]  # sorted, unique
    count: int
    difficulty: str
    scope: str

    @property
    def fingerprint(self) -> str:
        canonical = {
            "v": FINGERPRINT_VERSION,
            "course_id": str(self.course_id),
            "topic_ids": [str(t) for t in self.topic_ids],
            "count": self.count,
            "difficulty": self.difficulty,
            "scope": self.scope,
        }
        data = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Outcome:
    quiz_id: uuid.UUID
    status: str  # ready | generating | failed
    view: dict | None
    failure_code: str | None


@dataclass(frozen=True)
class Reservation:
    quiz_id: uuid.UUID
    token: uuid.UUID
    topics: list[tuple[uuid.UUID, str]]


class LeaseLost(Exception):
    """The session is no longer ours to finalize (recovered, or completed elsewhere)."""


class GenerationFailed(Exception):
    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


def _no_store() -> dict[str, str]:
    return {"Cache-Control": "no-store", "Pragma": "no-cache"}


# --- database steps (synchronous, each in its own short transaction) ---------------------------


_RECOVER = (
    "UPDATE quiz_sessions SET status = 'failed', failure_code = 'lease_expired', attempt_token = NULL, "
    "lease_expires_at = NULL, completed_at = now(), updated_at = now() "
    "WHERE user_id = :u AND status = 'generating' AND lease_expires_at <= now()"
)


def _recover_user(session: Session, user_id: uuid.UUID) -> None:
    """This user's generating session whose lease expired (database time) becomes failed `lease_expired`."""
    session.execute(text(_RECOVER), {"u": user_id})


def read_quiz(factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID, quiz_id: uuid.UUID) -> dict:
    """GET quiz: re-authorized in the same transaction; an abandoned generation is reported as failed."""
    with short_tx(factory) as session:
        require_practice(session, user, course_id)
        _recover_user(session, user.id)
        view = quiz_view(session, user.id, course_id, quiz_id)
        if view is None:
            raise ApiError(404, "quiz_not_found", "Quiz not found.", headers=_no_store())
        session.commit()
        return view


def _find(session: Session, user: CurrentUser, req: QuizRequest) -> Outcome | None:
    row = session.execute(
        text(
            "SELECT id, course_id, status, request_fingerprint, failure_code, lease_expires_at <= now() AS expired "
            "FROM quiz_sessions WHERE user_id = :u AND idempotency_key = :k"
        ),
        {"u": user.id, "k": req.idempotency_key},
    ).one_or_none()
    if row is None:
        return None
    if row.course_id != req.course_id or row.request_fingerprint != req.fingerprint:
        raise ApiError(
            409,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different quiz request. Use a new key.",
            headers=_no_store(),
        )
    status, failure = row.status, row.failure_code
    if status == "generating" and row.expired:
        _recover_user(session, user.id)
        # The recovering UPDATE does nothing if the generator finalized first (it waited for that row lock and then
        # re-checked the status), so report what the database holds now, never an assumed failure.
        current = session.execute(
            text("SELECT status, failure_code FROM quiz_sessions WHERE id = :i"), {"i": row.id}
        ).one()
        status, failure = current.status, current.failure_code
    view = quiz_view(session, user.id, req.course_id, row.id) if status == "ready" else None
    return Outcome(row.id, status, view, failure)


def lookup(factory: sessionmaker[Session], user: CurrentUser, req: QuizRequest) -> Outcome | None:
    """The existing outcome of this Idempotency-Key, re-authorized in the same transaction."""
    with short_tx(factory) as session:
        require_practice(session, user, req.course_id)
        outcome = _find(session, user, req)
        session.commit()  # only a lease recovery writes here
        return outcome


def reserve(
    factory: sessionmaker[Session],
    user: CurrentUser,
    req: QuizRequest,
    settings: Settings,
    token: uuid.UUID | None = None,
) -> Reservation | Outcome:
    """Step 4. `token` is chosen by the caller so that it can fail a reservation whose result it never received."""
    token = token or uuid.uuid4()
    with short_tx(factory) as session:
        require_practice(session, user, req.course_id, lock=True)
        existing = _find(session, user, req)
        if existing is not None:
            session.commit()
            return existing
        topics = session.execute(
            text("SELECT id, name FROM topics WHERE course_id = :c AND id = ANY(:ids) ORDER BY id FOR SHARE"),
            {"c": req.course_id, "ids": list(req.topic_ids)},
        ).all()
        if len(topics) != len(req.topic_ids):
            raise ApiError(422, "invalid_topics", "Choose topics of this course.", headers=_no_store())
        _recover_user(session, user.id)
        used = session.execute(
            text(
                "INSERT INTO quiz_generation_quotas (user_id, quota_day, used) "
                "VALUES (:u, (now() AT TIME ZONE 'UTC')::date, 1) "
                "ON CONFLICT (user_id, quota_day) DO UPDATE SET used = quiz_generation_quotas.used + 1, "
                "updated_at = now() WHERE quiz_generation_quotas.used < :limit RETURNING used"
            ),
            {"u": user.id, "limit": settings.quiz_daily_generation_quota},
        ).scalar_one_or_none()
        if used is None:
            # Quota exhausted. A concurrent request with the SAME key may just have consumed the last unit and
            # committed its session (this upsert waited for it): decide after rolling back, from the durable outcome.
            wait = session.execute(
                text(
                    "SELECT ceil(extract(epoch FROM date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day' "
                    "- (now() AT TIME ZONE 'UTC')))::int"
                )
            ).scalar_one()
            session.rollback()
            exhausted = wait
        else:
            exhausted = None
        quiz_id = None if exhausted is not None else session.execute(
            text(
                "INSERT INTO quiz_sessions (user_id, course_id, status, idempotency_key, request_fingerprint, "
                "requested_topic_ids, requested_count, difficulty, scope, attempt_token, lease_expires_at) "
                "VALUES (:u, :c, 'generating', :k, :f, CAST(:t AS uuid[]), :n, :d, :s, :token, "
                "now() + make_interval(secs => :lease)) ON CONFLICT DO NOTHING RETURNING id"
            ),
            {
                "u": user.id, "c": req.course_id, "k": req.idempotency_key, "f": req.fingerprint,
                "t": [str(t) for t in req.topic_ids], "n": req.count, "d": req.difficulty, "s": req.scope,
                "token": token, "lease": settings.quiz_lease_seconds,
            },
        ).scalar_one_or_none()  # fmt: skip
        if quiz_id is None:
            session.rollback()  # releases the quota unit as well
        else:
            session.commit()
            return Reservation(quiz_id, token, [(t.id, t.name) for t in topics])
    # A conflict: the same key committed concurrently, or another generation of this user is running; or the quota
    # looked exhausted. Recheck the durable idempotency outcome before answering either refusal.
    existing = lookup(factory, user, req)
    if existing is not None:
        return existing
    if exhausted is not None:
        raise ApiError(
            429,
            "quiz_quota_exceeded",
            "You have reached today's limit of new quizzes. Try again tomorrow.",
            headers={**_no_store(), "Retry-After": str(max(1, exhausted))},
        )
    raise ApiError(
        429,
        "quiz_generation_busy",
        "Another quiz is being generated for you. Wait for it to finish.",
        headers={**_no_store(), "Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def _shuffled(question: ProposedQuestion) -> tuple[list[dict], str]:
    """Public choices in delivery order and the correct choice id. MCQ order comes from a CSPRNG, and
    labels are assigned after shuffling, so neither the AI's answer position nor the label carries it."""
    if question.qtype == "true_false":
        return [dict(c) for c in TRUE_FALSE_CHOICES], "true" if question.answer_bool else "false"
    order = list(range(len(question.choices)))
    secrets.SystemRandom().shuffle(order)
    choices = [{"id": CHOICE_LABELS[i], "text": question.choices[original]} for i, original in enumerate(order)]
    return choices, CHOICE_LABELS[order.index(question.answer_index)]


def _content_hash(question: ProposedQuestion, choices: list[dict], key: str, evidence: list[dict]) -> str:
    content = {
        "type": question.qtype, "difficulty": question.difficulty, "topic_id": str(question.topic_id),
        "stem": question.stem, "choices": choices, "answer_key": {"choice_id": key}, "hint": question.hint,
        "explanation": question.explanation, "citations": evidence,
    }  # fmt: skip
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()


_WAIT_TIMEOUT_STATES = frozenset({"57014", "55P03"})  # query_canceled (statement_timeout), lock_not_available
_DEFAULT_STATEMENT_MS = 5000
_DEFAULT_LOCK_MS = 2000


def _check_deadline(deadline_at: float, step: str) -> None:
    if time.monotonic() >= deadline_at:
        raise TimeoutError(f"pipeline deadline expired {step}")


def _bound_waits(session: Session, deadline_at: float, step: str) -> None:
    """Never wait on the database longer than the pipeline has left (PostgreSQL enforces it, SET LOCAL)."""
    _check_deadline(deadline_at, step)
    left_ms = max(1, math.ceil((deadline_at - time.monotonic()) * 1000))  # rounded up: the timer never fires early
    session.execute(text(f"SET LOCAL statement_timeout = {min(_DEFAULT_STATEMENT_MS, left_ms)}"))
    session.execute(text(f"SET LOCAL lock_timeout = {min(_DEFAULT_LOCK_MS, left_ms)}"))


def persist(
    factory: sessionmaker[Session],
    user: CurrentUser,
    req: QuizRequest,
    reservation: Reservation,
    proposed: ProposedQuiz,
    deadline_at: float,
) -> dict:
    """Fenced finalization: the ready quiz view, or LeaseLost / VerificationFailed / TimeoutError / ApiError.

    TimeoutError means the deadline expired (or a statement/lock timeout derived from it fired) BEFORE commit, so
    nothing was committed. Whatever happens after COMMIT was issued is outside any deadline (module docstring).
    """
    with short_tx(factory) as session:
        try:
            return _persist(session, user, req, reservation, proposed, deadline_at)
        except DBAPIError as exc:
            if sqlstate_of(exc) in _WAIT_TIMEOUT_STATES and time.monotonic() >= deadline_at:
                raise TimeoutError("pipeline deadline expired while waiting for the database") from None
            raise


def _persist(
    session: Session,
    user: CurrentUser,
    req: QuizRequest,
    reservation: Reservation,
    proposed: ProposedQuiz,
    deadline_at: float,
) -> dict:
    _bound_waits(session, deadline_at, "before authorization")
    require_practice(session, user, req.course_id, lock=True)
    _bound_waits(session, deadline_at, "before the session lock")
    live = session.execute(
        text(
            "SELECT status, attempt_token FROM quiz_sessions "
            "WHERE id = :q AND user_id = :u AND course_id = :c FOR UPDATE"
        ),
        {"q": reservation.quiz_id, "u": user.id, "c": req.course_id},
    ).one_or_none()
    if live is None or live.status != "generating" or live.attempt_token != reservation.token:
        raise LeaseLost()
    # The lease is judged by the database's wall clock AFTER the lock was acquired: now() is the transaction's start,
    # and a wait for this very lock can outlast the lease.
    if not session.execute(
        text("SELECT lease_expires_at > clock_timestamp() FROM quiz_sessions WHERE id = :q"), {"q": reservation.quiz_id}
    ).scalar_one():
        raise LeaseLost()
    _bound_waits(session, deadline_at, "after the session lock")
    topics = session.execute(
        text("SELECT id FROM topics WHERE course_id = :c AND id = ANY(:ids) ORDER BY id FOR SHARE"),
        {"c": req.course_id, "ids": list(req.topic_ids)},
    ).scalars().all()
    if len(topics) != len(req.topic_ids):
        raise VerificationFailed("topic_gone")
    evidence = verify_question_citations(session, req.course_id, proposed.questions)
    _bound_waits(session, deadline_at, "after provenance")
    generation = {
        "protocol": PROTOCOL,
        "service": SERVICE,
        "request_id": str(reservation.quiz_id),
        "scope": req.scope,
        "model": {"name": proposed.model_name, "version": proposed.model_version},
    }
    for position, (question, cited) in enumerate(zip(proposed.questions, evidence), start=1):
        choices, key = _shuffled(question)
        question_id = session.execute(
            text(
                "INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, choices, "
                "explanation, citations, status, hint, answer_key, grading_method, generated_by_session_id, "
                "generated_for_user_id, generation, content_hash) VALUES (:c, :t, 'practice', :qt, :d, :stem, "
                "CAST(:choices AS jsonb), :expl, CAST(:cit AS jsonb), 'generated', :hint, CAST(:key AS jsonb), "
                ":gm, :s, :u, CAST(:gen AS jsonb), :h) RETURNING id"
            ),
            {
                "c": req.course_id, "t": question.topic_id, "qt": question.qtype, "d": question.difficulty,
                "stem": question.stem, "choices": json.dumps(choices), "expl": question.explanation,
                "cit": json.dumps(cited), "hint": question.hint, "key": json.dumps({"choice_id": key}),
                "gm": GRADING_METHOD, "s": reservation.quiz_id, "u": user.id, "gen": json.dumps(generation),
                "h": _content_hash(question, choices, key, cited),
            },
        ).scalar_one()  # fmt: skip
        session.execute(
            text(
                "INSERT INTO quiz_items (session_id, user_id, course_id, question_id, topic_id, position) "
                "VALUES (:s, :u, :c, :q, :t, :p)"
            ),
            {"s": reservation.quiz_id, "u": user.id, "c": req.course_id, "q": question_id,
             "t": question.topic_id, "p": position},
        )  # fmt: skip
        _check_deadline(deadline_at, f"while inserting question {position}")
    _bound_waits(session, deadline_at, "before finalization")
    # Fencing, last word: status, token and the lease (database wall clock, now) are repeated in the UPDATE itself.
    finalized = session.execute(
        text(
            "UPDATE quiz_sessions SET status = 'ready', protocol = :p, service = :svc, attempt_token = NULL, "
            "lease_expires_at = NULL, completed_at = now(), updated_at = now() "
            "WHERE id = :q AND status = 'generating' AND attempt_token = :t AND lease_expires_at > clock_timestamp() "
            "RETURNING id"
        ),
        {"p": PROTOCOL, "svc": SERVICE, "q": reservation.quiz_id, "t": reservation.token},
    ).first()
    if finalized is None:
        raise LeaseLost()
    view = quiz_view(session, user.id, req.course_id, reservation.quiz_id)
    _check_deadline(deadline_at, "before commit")
    session.commit()
    return view


def mark_failed(factory: sessionmaker[Session], quiz_id: uuid.UUID, token: uuid.UUID, code: str) -> bool:
    """Fenced: only the holder of the attempt token can fail a generating session."""
    with short_tx(factory) as session:
        changed = session.execute(
            text(
                "UPDATE quiz_sessions SET status = 'failed', failure_code = :code, attempt_token = NULL, "
                "lease_expires_at = NULL, completed_at = now(), updated_at = now() "
                "WHERE id = :q AND status = 'generating' AND attempt_token = :t RETURNING id"
            ),
            {"q": quiz_id, "t": token, "code": code},
        ).first()
        session.commit()
        return changed is not None


def mark_failed_by_key(
    factory: sessionmaker[Session], user_id: uuid.UUID, idempotency_key: uuid.UUID, token: uuid.UUID, code: str
) -> bool:
    """Fenced like mark_failed, addressed by (user, key, token): for a reservation whose id was never received.

    A no-op when no reservation with this token committed, and when the session already reached any other
    outcome (ready, or failed/recovered by someone else): it never overwrites a committed outcome.
    """
    with short_tx(factory) as session:
        changed = session.execute(
            text(
                "UPDATE quiz_sessions SET status = 'failed', failure_code = :code, attempt_token = NULL, "
                "lease_expires_at = NULL, completed_at = now(), updated_at = now() "
                "WHERE user_id = :u AND idempotency_key = :k AND status = 'generating' AND attempt_token = :t "
                "RETURNING id"
            ),
            {"u": user_id, "k": idempotency_key, "t": token, "code": code},
        ).first()
        session.commit()
        return changed is not None


def recover_expired(factory: sessionmaker[Session], batch: int = 100) -> list[uuid.UUID]:
    """Maintenance: generating sessions whose lease expired (database time) become failed `lease_expired`."""
    with short_tx(factory) as session:
        ids = session.execute(
            text(
                "UPDATE quiz_sessions SET status = 'failed', failure_code = 'lease_expired', attempt_token = NULL, "
                "lease_expires_at = NULL, completed_at = now(), updated_at = now() WHERE id IN ("
                "SELECT id FROM quiz_sessions WHERE status = 'generating' AND lease_expires_at <= now() "
                "ORDER BY lease_expires_at LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING id"
            ),
            {"n": batch},
        ).scalars().all()
        session.commit()
        return list(ids)


def cleanup_quotas(factory: sessionmaker[Session], retention_days: int) -> int:
    with short_tx(factory) as session:
        removed = session.execute(
            text("DELETE FROM quiz_generation_quotas WHERE quota_day < (now() AT TIME ZONE 'UTC')::date - :d"),
            {"d": retention_days},
        ).rowcount
        session.commit()
        return removed


# --- responses ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    status: int
    body: dict
    headers: dict[str, str]


def answer_outcome(outcome: Outcome, course_id: uuid.UUID, *, created: bool = False) -> Answer:
    if outcome.status == "ready":
        headers = {**_no_store(), "Location": f"/api/v1/courses/{course_id}/quizzes/{outcome.quiz_id}"}
        if not created:
            headers["Idempotent-Replayed"] = "true"
        return Answer(201 if created else 200, outcome.view, headers)
    extra = {"quiz_id": str(outcome.quiz_id), "status": outcome.status}
    if outcome.status == "generating":
        raise ApiError(
            409,
            "quiz_generation_in_progress",
            "This quiz is still being generated. Try again in a moment with the same Idempotency-Key.",
            headers={**_no_store(), "Retry-After": str(RETRY_AFTER_SECONDS)},
            extra=extra,
        )
    status, code, message = failure_status(outcome.failure_code)
    raise ApiError(status, code, message, headers=_no_store(), extra={**extra, "retry": "new_idempotency_key"})


# --- the pipeline ------------------------------------------------------------------------------


async def _checkpoint() -> None:
    """Deliver a deadline or cancellation that arrived while a worker thread ran (see app/chat/db.py)."""
    await anyio.lowlevel.checkpoint()
    if anyio.current_effective_deadline() <= anyio.current_time():
        raise TimeoutError("pipeline deadline expired")


async def create_quiz(state, user: CurrentUser, req: QuizRequest) -> Answer:
    settings: Settings = state.settings
    factory = state.session_factory
    existing = await run_in_threadpool(lookup, factory, user, req)
    if existing is not None:
        return await _deliverable(factory, user, req, answer_outcome(existing, req.course_id))  # replay: no AI, quota
    client = state.quiz_client
    if not settings.quiz_generation_enabled or client is None or not client.configured:
        raise ApiError(503, "quiz_generation_unavailable", "Generating new quizzes is not available right now.",
                       headers=_no_store())  # fmt: skip
    lease = state.quiz_limiter.try_acquire(user.id)
    if lease is None:
        raise ApiError(
            429,
            "quiz_generation_busy",
            "Too many quizzes are being generated right now. Wait a moment and try again.",
            headers={**_no_store(), "Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    try:
        answer = await _generate(state, client, user, req)
    finally:
        lease.release()
    return await _deliverable(factory, user, req, answer)


async def _deliverable(factory, user: CurrentUser, req: QuizRequest, answer: Answer) -> Answer:
    """Fresh authorization right before a quiz body is handed to the transport (its own short transaction).

    The data was read or committed earlier, possibly seconds ago; if access was lost since, the body is withheld
    (the quiz itself stays stored and replayable if access returns).
    """
    if answer.status in (200, 201):
        await run_in_threadpool(authorize_practice, factory, user, req.course_id)
    return answer


async def _generate(state, client, user: CurrentUser, req: QuizRequest) -> Answer:
    settings: Settings = state.settings
    factory = state.session_factory
    total = settings.quiz_pipeline_deadline_seconds
    deadline_at = time.monotonic() + total
    # Chosen before the reservation exists: it is the only handle that still works if the reservation commits but its
    # result is lost to a deadline, a cancellation or a dropped connection (see _fail).
    token = uuid.uuid4()
    reserving = False  # True from the moment a reservation may have committed until the outcome is settled
    quiz_id: uuid.UUID | None = None
    try:
        try:
            with anyio.fail_after(total):
                capabilities = await client.capabilities()
                types = negotiate_types(capabilities)
                if (
                    not capabilities.ready
                    or not types
                    or req.scope not in capabilities.scopes
                    or req.count > capabilities.max_questions
                    or len(req.topic_ids) > capabilities.max_topics
                ):
                    log.warning("quiz_unavailable reason=%s", capabilities.reason if not capabilities.ready else "limits")
                    raise ApiError(503, "quiz_ai_unavailable",
                                   "The quiz service is not available right now. Try again later.",
                                   headers=_no_store())  # fmt: skip
                reserving = True
                try:
                    result = await run_in_threadpool(reserve, factory, user, req, settings, token)
                except ApiError:
                    reserving = False  # reserve rolled back and refused: nothing of ours was committed
                    raise
                await _checkpoint()
                if isinstance(result, Outcome):
                    reserving = False  # an existing outcome of this key; no reservation of ours
                    return answer_outcome(result, req.course_id)
                quiz_id = result.quiz_id
                payload = build_request(
                    quiz_id, req.course_id, req.scope, result.topics, req.count, req.difficulty, types
                )
                try:
                    body = await client.generate(payload)
                except QuizUpstreamError as exc:
                    code = "upstream_timeout" if exc.code in ("deadline", "timeout") else "ai_unavailable"
                    raise GenerationFailed(code, exc.code) from None
                try:
                    proposed = validate_response(body, payload)
                except ProtocolViolation as exc:
                    raise GenerationFailed("rejected_protocol", exc.reason) from None
        except TimeoutError:
            if not reserving:
                raise ApiError(504, "quiz_timeout", "Generating the quiz took too long. Try again.",
                               headers=_no_store()) from None  # fmt: skip
            raise GenerationFailed("deadline", "pipeline") from None
        reservation = result  # a Reservation: Outcome results returned above
        try:
            view = await run_in_threadpool(persist, factory, user, req, reservation, proposed, deadline_at)
        except VerificationFailed as exc:
            raise GenerationFailed("rejected_provenance", exc.reason) from None
        except TimeoutError:
            raise GenerationFailed("deadline", "persist") from None
        except LeaseLost:
            log.warning("quiz_lease_lost quiz_id=%s", reservation.quiz_id)
            return await _resolved(factory, user, req)
        reserving = False  # committed: ready is final, no later failure may touch it
        log.info("quiz_generated quiz_id=%s count=%d", reservation.quiz_id, req.count)
        return answer_outcome(Outcome(reservation.quiz_id, "ready", view, None), req.course_id, created=True)
    except GenerationFailed as failure:
        log.warning("quiz_failed quiz_id=%s code=%s reason=%s", quiz_id, failure.code, failure.reason)
        await _fail(factory, user, req, token, failure.code)
        return await _resolved(factory, user, req, absent=failure.code)
    except ApiError:
        if reserving:  # access lost during generation
            log.info("quiz_access_lost quiz_id=%s", quiz_id)
            await _fail(factory, user, req, token, "access_lost")
        raise
    except anyio.get_cancelled_exc_class():
        if reserving:
            log.warning("quiz_failed quiz_id=%s code=cancelled", quiz_id)
            await _fail(factory, user, req, token, "cancelled")
        raise
    except Exception as exc:
        if not reserving:
            raise
        # Includes a COMMIT whose outcome is unknown: the fenced update below is a no-op if it happened.
        # Only the class and SQLSTATE are logged: a StatementError would print the SQL parameters (answers, passages).
        log.error("quiz_failed quiz_id=%s code=server_error %s", quiz_id, describe(exc))
        await _fail(factory, user, req, token, "server_error")
        return await _resolved(factory, user, req, absent="server_error")


async def _fail(factory, user: CurrentUser, req: QuizRequest, token: uuid.UUID, code: str) -> None:
    """Fail OUR reservation (by key and token), shielded; a no-op unless it is still our generating session."""
    with anyio.CancelScope(shield=True):
        await run_in_threadpool(mark_failed_by_key, factory, user.id, req.idempotency_key, token, code)


async def _resolved(factory, user: CurrentUser, req: QuizRequest, *, absent: str = "server_error") -> Answer:
    """Answer with whatever outcome the database now holds for this key (re-authorized, re-read).

    `absent` is the failure to report when no session exists for the key at all, i.e. the reservation never committed.
    """
    with anyio.CancelScope(shield=True):
        outcome = await run_in_threadpool(lookup, factory, user, req)
    if outcome is None:
        status, code, message = failure_status(absent)
        raise ApiError(status, code, message, headers=_no_store(), extra={"retry": "new_idempotency_key"})
    return answer_outcome(outcome, req.course_id)
