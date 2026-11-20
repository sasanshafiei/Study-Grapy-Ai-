"""Student-safe views of quizzes and graded attempts, built from persisted rows.

Unanswered questions are read with an explicit column list that never
includes answer_key, explanation, citations, grading_method or generation;
feedback for an answered question comes from that attempt's immutable
snapshot (attempts.feedback and the baseline columns), with citations
re-checked for visibility now (app/quiz/provenance.py). Callers have already
authorized the student for the course in the same transaction, and every
query below is additionally filtered by user and course.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from .provenance import visible_citations

BASELINE_KIND = "baseline"
BASELINE_METHOD = "cumulative_accuracy/1"


def iso(value) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def trend(before: float | None, after: float) -> str:
    """first: no earlier committed M7 attempt on this topic; otherwise up / down / steady."""
    if before is None:
        return "first"
    if after > before:
        return "up"
    if after < before:
        return "down"
    return "steady"


def mastery_view(row, topic_name: str) -> dict:
    return {
        "kind": BASELINE_KIND,
        "method": row.mastery_method,
        "topic_id": str(row.topic_id),
        "topic_name": topic_name,
        "before": row.mastery_before,
        "after": row.mastery_after,
        "attempts": row.stats_attempts,
        "correct": row.stats_correct,
        "trend": trend(row.mastery_before, row.mastery_after),
        "next_review_at": None,
    }


_ATTEMPT_COLUMNS = (
    "a.id, a.quiz_item_id, a.question_id, a.topic_id, a.answer, a.is_correct, a.feedback, a.hint_count, "
    "a.response_ms, a.submitted_at, a.mastery_before, a.mastery_after, a.mastery_method, a.stats_attempts, "
    "a.stats_correct"
)


def feedback_view(session: Session, course_id: uuid.UUID, quiz_id: uuid.UUID, attempt, topic_name: str) -> dict:
    snapshot = attempt.feedback
    cited = visible_citations(session, course_id, snapshot.get("citations"))
    return {
        "attempt_id": str(attempt.id),
        "quiz_id": str(quiz_id),
        "question_id": str(attempt.question_id),
        "answer": attempt.answer,
        "correct": attempt.is_correct,
        "correct_answer": {"choice_id": snapshot["correct_choice_id"], "text": snapshot["correct_text"]},
        "explanation": snapshot["explanation"],
        "citations": cited.citations,
        "citations_omitted": cited.omitted,
        "hint_count": attempt.hint_count,
        "response_ms": attempt.response_ms,
        "submitted_at": iso(attempt.submitted_at),
        "mastery": mastery_view(attempt, topic_name),
    }


def attempt_for_item(session: Session, user_id: uuid.UUID, course_id: uuid.UUID, item_id: uuid.UUID):
    return session.execute(
        text(
            f"SELECT {_ATTEMPT_COLUMNS} FROM attempts a "
            "WHERE a.quiz_item_id = :i AND a.user_id = :u AND a.course_id = :c"
        ),
        {"i": item_id, "u": user_id, "c": course_id},
    ).one_or_none()


def quiz_view(session: Session, user_id: uuid.UUID, course_id: uuid.UUID, quiz_id: uuid.UUID) -> dict | None:
    """The quiz as the student may see it now, or None if it is not theirs in this course."""
    quiz = session.execute(
        text(
            "SELECT id, status, scope, difficulty, requested_count, created_at, completed_at, failure_code "
            "FROM quiz_sessions WHERE id = :q AND user_id = :u AND course_id = :c"
        ),
        {"q": quiz_id, "u": user_id, "c": course_id},
    ).one_or_none()
    if quiz is None:
        return None
    questions = []
    if quiz.status == "ready":
        items = session.execute(
            text(
                "SELECT qi.id AS item_id, qi.position, q.id AS question_id, q.topic_id, t.name AS topic_name, "
                "q.qtype, q.difficulty, q.text AS stem, q.choices, q.hint "
                "FROM quiz_items qi "
                "JOIN questions q ON q.id = qi.question_id AND q.course_id = qi.course_id "
                "JOIN topics t ON t.id = qi.topic_id AND t.course_id = qi.course_id "
                "WHERE qi.session_id = :q AND qi.user_id = :u AND qi.course_id = :c "
                "AND qi.purpose = 'practice' AND q.purpose = 'practice' AND q.status = 'generated' "
                "ORDER BY qi.position"
            ),
            {"q": quiz_id, "u": user_id, "c": course_id},
        ).all()
        attempts = {
            row.quiz_item_id: row
            for row in session.execute(
                text(
                    f"SELECT {_ATTEMPT_COLUMNS} FROM attempts a "
                    "WHERE a.quiz_item_id = ANY(:ids) AND a.user_id = :u AND a.course_id = :c"
                ),
                {"ids": [i.item_id for i in items], "u": user_id, "c": course_id},
            ).all()
        }
        for item in items:
            attempt = attempts.get(item.item_id)
            questions.append(
                {
                    "id": str(item.question_id),
                    "position": item.position,
                    "topic_id": str(item.topic_id),
                    "topic_name": item.topic_name,
                    "type": item.qtype,
                    "difficulty": item.difficulty,
                    "stem": item.stem,
                    "choices": [{"id": c["id"], "text": c["text"]} for c in item.choices],
                    "hint": item.hint,
                    "attempt": None
                    if attempt is None
                    else feedback_view(session, course_id, quiz_id, attempt, item.topic_name),
                }
            )
    return {
        "quiz_id": str(quiz.id),
        "course_id": str(course_id),
        "status": quiz.status,
        "scope": quiz.scope,
        "difficulty": quiz.difficulty,
        "count": quiz.requested_count,
        "created_at": iso(quiz.created_at),
        "completed_at": iso(quiz.completed_at),
        "failure": None if quiz.status != "failed" else public_failure(quiz.failure_code),
        "questions": questions,
    }


# Internal failure codes (quiz_sessions.failure_code) -> (HTTP status of the POST outcome, public code, message).
FAILURES: dict[str, tuple[int, str, str]] = {
    "ai_unavailable": (503, "quiz_ai_unavailable", "The quiz service is not available right now. Try again later."),
    "upstream_timeout": (504, "quiz_timeout", "Generating the quiz took too long. Try again."),
    "deadline": (504, "quiz_timeout", "Generating the quiz took too long. Try again."),
    "rejected_protocol": (502, "quiz_rejected", "The generated quiz did not pass our checks. Try again."),
    "rejected_provenance": (502, "quiz_rejected", "The generated quiz did not pass our checks. Try again."),
}
GENERIC_FAILURE = (503, "quiz_generation_failed", "The quiz could not be generated. Try again.")


def public_failure(code: str | None) -> dict:
    status, public, message = FAILURES.get(code or "", GENERIC_FAILURE)
    return {"code": public, "detail": message, "retry": "new_idempotency_key"}


def failure_status(code: str | None) -> tuple[int, str, str]:
    return FAILURES.get(code or "", GENERIC_FAILURE)
