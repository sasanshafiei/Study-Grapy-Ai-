"""Deterministic grading of one assigned question, idempotent per assignment, in one transaction.

No upstream call happens here. Order inside ONE short transaction:
1. re-authorize practice with FOR SHARE locks (user, course, enrollment);
2. find the assignment with FOR UPDATE: it must belong to this student and
   course, to a `ready` session given by quiz_id, and be a generated practice
   question. Another student's question, an unassigned question, a question
   of another course or quiz, and any exam question all answer the same 404;
3. the answer must be one of that question's persisted choice ids (422
   otherwise, nothing stored); unsupported question types are refused (422);
4. an existing attempt for the assignment: the same choice id replays the
   stored result (200, counters unchanged, the first submission's telemetry
   kept); a different one is 409 already_answered and changes nothing;
5. otherwise grade by choice id against the persisted answer key, update
   topic_practice_stats atomically (INSERT .. ON CONFLICT DO UPDATE ..
   RETURNING) and derive before/after from the returned, serialized counters,
   insert the append-only attempt with its feedback snapshot, commit (201).
The assignment row lock serializes simultaneous submissions of the same
question; UNIQUE (quiz_item_id) is the backstop (the transaction is retried
once and then replays). Lock order: user, course, enrollment, quiz item,
topic statistics, then the attempt insert. Grading never depends on the
client's hint_count or response_ms, which are stored as untrusted telemetry.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser
from ..errors import ApiError
from .access import require_practice, short_tx
from .generation import GRADING_METHOD
from .protocol import SUPPORTED_TYPES
from .views import BASELINE_METHOD, attempt_for_item, feedback_view

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptRequest:
    quiz_id: uuid.UUID
    question_id: uuid.UUID
    answer: str
    hint_count: int
    response_ms: int | None


def _no_store() -> dict[str, str]:
    return {"Cache-Control": "no-store", "Pragma": "no-cache"}


def question_not_found() -> ApiError:
    return ApiError(404, "question_not_found", "Question not found.", headers=_no_store())


def _submit_once(session: Session, user: CurrentUser, course_id: uuid.UUID, req: AttemptRequest) -> tuple[int, dict]:
    require_practice(session, user, course_id, lock=True)
    item = session.execute(
        text(
            "SELECT qi.id AS item_id, qi.topic_id, q.qtype, q.choices, q.answer_key, q.grading_method, "
            "q.explanation, q.citations, t.name AS topic_name "
            "FROM quiz_items qi "
            "JOIN quiz_sessions s ON s.id = qi.session_id AND s.user_id = qi.user_id AND s.course_id = qi.course_id "
            "JOIN questions q ON q.id = qi.question_id AND q.course_id = qi.course_id "
            "JOIN topics t ON t.id = qi.topic_id AND t.course_id = qi.course_id "
            "WHERE qi.session_id = :quiz AND qi.question_id = :q AND qi.user_id = :u AND qi.course_id = :c "
            "AND s.status = 'ready' AND qi.purpose = 'practice' AND q.purpose = 'practice' "
            "AND q.status = 'generated' AND q.generated_for_user_id = :u "
            "FOR UPDATE OF qi"
        ),
        {"quiz": req.quiz_id, "q": req.question_id, "u": user.id, "c": course_id},
    ).one_or_none()
    if item is None:
        raise question_not_found()
    if item.qtype not in SUPPORTED_TYPES or item.grading_method != GRADING_METHOD:
        raise ApiError(422, "unsupported_question_type", "This question cannot be answered here.", headers=_no_store())
    choices = {c["id"]: c["text"] for c in item.choices}
    if req.answer not in choices:
        raise ApiError(422, "invalid_answer", "Choose one of the answers offered.", headers=_no_store())

    existing = attempt_for_item(session, user.id, course_id, item.item_id)
    if existing is not None:
        if existing.answer != req.answer:
            raise ApiError(
                409, "already_answered", "You already answered this question with a different choice.",
                headers=_no_store(), extra={"attempt_id": str(existing.id)},
            )  # fmt: skip
        view = feedback_view(session, course_id, req.quiz_id, existing, item.topic_name)
        return 200, {**view, "replayed": True}

    key = item.answer_key["choice_id"]
    correct = req.answer == key
    stats = session.execute(
        text(
            "INSERT INTO topic_practice_stats AS s (user_id, topic_id, course_id, attempts, correct) "
            "VALUES (:u, :t, :c, 1, :k) ON CONFLICT (user_id, topic_id) DO UPDATE SET "
            "attempts = s.attempts + 1, correct = s.correct + EXCLUDED.correct, updated_at = now() "
            "WHERE s.course_id = EXCLUDED.course_id RETURNING attempts, correct"
        ),
        {"u": user.id, "t": item.topic_id, "c": course_id, "k": int(correct)},
    ).one()
    before = None if stats.attempts == 1 else (stats.correct - int(correct)) / (stats.attempts - 1)
    after = stats.correct / stats.attempts
    feedback = {
        "correct_choice_id": key,
        "correct_text": choices[key],
        "explanation": item.explanation,
        "citations": item.citations,  # the original evidence; visibility is re-checked at every delivery
    }
    session.execute(
        text(
            "INSERT INTO attempts (user_id, question_id, topic_id, course_id, is_correct, hint_count, response_ms, "
            "quiz_item_id, answer, grading_method, feedback, mastery_before, mastery_after, mastery_method, "
            "stats_attempts, stats_correct, submitted_at, attempted_at) VALUES (:u, :q, :t, :c, :ok, :hints, :ms, "
            ":item, :answer, :gm, CAST(:fb AS jsonb), :before, :after, :method, :n, :k, now(), "
            "(now() AT TIME ZONE 'UTC'))"
        ),
        {
            "u": user.id, "q": req.question_id, "t": item.topic_id, "c": course_id, "ok": correct,
            "hints": req.hint_count, "ms": req.response_ms, "item": item.item_id, "answer": req.answer,
            "gm": GRADING_METHOD, "fb": json.dumps(feedback, ensure_ascii=True), "before": before, "after": after,
            "method": BASELINE_METHOD, "n": stats.attempts, "k": stats.correct,
        },
    )  # fmt: skip
    created = attempt_for_item(session, user.id, course_id, item.item_id)
    view = feedback_view(session, course_id, req.quiz_id, created, item.topic_name)
    session.commit()
    log.info("quiz_attempt_graded attempt_id=%s quiz_id=%s", created.id, req.quiz_id)
    return 201, {**view, "replayed": False}


def submit_attempt(
    factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID, req: AttemptRequest
) -> tuple[int, dict]:
    for retry in (False, True):
        try:
            with short_tx(factory) as session:
                return _submit_once(session, user, course_id, req)
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if retry or constraint != "attempts_quiz_item_id_key":
                raise
            log.info("quiz_attempt_race quiz_id=%s", req.quiz_id)  # the other submission won: replay or 409
    raise AssertionError("unreachable")
