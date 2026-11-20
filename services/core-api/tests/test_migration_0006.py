"""Revision 0006 (M7): upgrade/downgrade safety with legacy data, and database-level integrity at head.

Database tests run only through the guarded fixtures (disposable database).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.alembic_cli import run_alembic
from tests.api_support import remove_quiz_state
from tests.test_migration_0003 import _failed
from tests.test_migration_0003 import scratch3  # noqa: F401  (fixture used by scratch5)
from tests.test_migration_0005 import scratch5  # noqa: F401  (fixture used by scratch6)
from tests.test_migrations import _current, _insert_full_baseline_dataset, _table_contents

REVISION = "0006"
PREVIOUS = "0005"
NEW_TABLES = {"quiz_sessions", "quiz_items", "topic_practice_stats", "quiz_generation_quotas"}
CHOICES = [{"id": "a", "text": "Right"}, {"id": "b", "text": "Wrong"}]
EVIDENCE = [{"chunk_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4()), "origin": "class",
             "locator": {"page": 1}, "quote": "x" * 25}]  # fmt: skip


@pytest.fixture
def scratch6(scratch5, committed_transaction):  # noqa: F811
    """scratch5 plus removal of any M7 state before the database is reset to base."""
    yield
    with committed_transaction() as connection:
        if connection.execute(text("SELECT to_regclass('public.quiz_sessions') IS NOT NULL")).scalar_one():
            users = connection.execute(text("SELECT id FROM users")).scalars().all()
            courses = connection.execute(text("SELECT id FROM courses")).scalars().all()
            remove_quiz_state(connection, users, courses)


def _tables(connection) -> set[str]:
    return set(
        connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars().all()
    )


def test_upgrade_keeps_legacy_rows_backfills_attempts_and_downgrades_cleanly(
    scratch6, disposable_engine, alembic, committed_transaction
):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        _insert_full_baseline_dataset(connection)
    before = _table_contents(disposable_engine, {"questions", "attempts", "topics", "mastery_scores"})
    alembic("upgrade", REVISION)
    with disposable_engine.connect() as connection:
        assert NEW_TABLES <= _tables(connection)
        row = connection.execute(
            text("SELECT a.course_id = q.course_id FROM attempts a JOIN questions q ON q.id = a.question_id")
        ).scalar_one()
        assert row is True
        legacy = connection.execute(
            text("SELECT hint, answer_key, generated_by_session_id, status FROM questions")
        ).one()
        assert tuple(legacy) == (None, None, None, "approved")
        connection.rollback()
    assert _table_contents(disposable_engine, {"mastery_scores", "topics"}) == {
        k: v for k, v in before.items() if k in {"mastery_scores", "topics"}
    }
    alembic("downgrade", PREVIOUS)
    assert _table_contents(disposable_engine, {"questions", "attempts", "topics", "mastery_scores"}) == before
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:  # legacy rows stay editable (no M7 restriction applies)
        connection.execute(text("UPDATE questions SET text = 'Edited legacy question'"))
        connection.execute(text("UPDATE attempts SET hint_count = 7"))
        with pytest.raises(DBAPIError, match="cannot become a graded practice attempt"):
            with connection.begin_nested():
                connection.execute(text("UPDATE attempts SET quiz_item_id = gen_random_uuid()"))
        with pytest.raises(DBAPIError, match="cannot become a generated question"):
            with connection.begin_nested():
                connection.execute(text("UPDATE questions SET status = 'generated'"))


@pytest.mark.parametrize("problem", ["value", "question_topic", "attempt_topic"])
def test_preflight_stops_before_any_change(
    scratch6, disposable_engine, alembic, alembic_env, committed_transaction, problem
):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        _insert_full_baseline_dataset(connection)
        other_course = connection.execute(
            text("INSERT INTO courses (owner_id, name, join_code) SELECT id, 'Other', 'othercode1' FROM users "
                 "WHERE role = 'professor' RETURNING id")  # fmt: skip
        ).scalar_one()
        foreign = connection.execute(
            text("INSERT INTO topics (course_id, name) VALUES (:c, 'Foreign') RETURNING id"), {"c": other_course}
        ).scalar_one()
        if problem == "value":
            connection.execute(text("UPDATE questions SET qtype = 'essay'"))
        elif problem == "question_topic":
            connection.execute(text("UPDATE questions SET topic_id = :t"), {"t": foreign})
        else:
            connection.execute(text("UPDATE attempts SET topic_id = :t"), {"t": foreign})
    output = _failed(run_alembic("upgrade", REVISION, env=alembic_env))
    assert "Migration 0006 stopped before any change" in output
    assert _current(alembic) == f"{PREVIOUS}"
    with disposable_engine.connect() as connection:
        assert not NEW_TABLES & _tables(connection)
        connection.rollback()
    with committed_transaction() as connection:  # leave the scratch database consistent for teardown
        connection.execute(text("DELETE FROM attempts"))
        connection.execute(text("DELETE FROM questions"))


def _m7_chain(connection, user, course, topic, *, ready: bool = True) -> dict:
    session = connection.execute(
        text(
            "INSERT INTO quiz_sessions (user_id, course_id, status, idempotency_key, request_fingerprint, "
            "requested_topic_ids, requested_count, difficulty, scope, attempt_token, lease_expires_at) VALUES "
            "(:u, :c, 'generating', gen_random_uuid(), repeat('a', 64), ARRAY[CAST(:t AS uuid)], 1, 'easy', "
            "'class_only', gen_random_uuid(), now() + interval '1 minute') RETURNING id"
        ),
        {"u": user, "c": course, "t": topic},
    ).scalar_one()
    question = _generated_question(connection, session, user, course, topic)
    item = connection.execute(
        text(
            "INSERT INTO quiz_items (session_id, user_id, course_id, question_id, topic_id, position) "
            "VALUES (:s, :u, :c, :q, :t, 1) RETURNING id"
        ),
        {"s": session, "u": user, "c": course, "q": question, "t": topic},
    ).scalar_one()
    if ready:
        _ready(connection, session)
    return {"session": session, "question": question, "item": item}


def _ready(connection, session) -> None:
    connection.execute(
        text(
            "UPDATE quiz_sessions SET status = 'ready', protocol = 'studygraph.quiz/1', service = 'ai-service', "
            "attempt_token = NULL, lease_expires_at = NULL, completed_at = now() WHERE id = :s"
        ),
        {"s": session},
    )


def _generated_question(connection, session, user, course, topic, **overrides):
    values = {
        "course_id": course, "topic_id": topic, "purpose": "practice", "qtype": "mcq", "difficulty": "easy",
        "text": "Stem?", "choices": json.dumps(CHOICES), "explanation": "Because.", "citations": json.dumps(EVIDENCE),
        "status": "generated", "hint": "Hint.", "answer_key": json.dumps({"choice_id": "a"}),
        "grading_method": "choice_id/1", "generated_by_session_id": session, "generated_for_user_id": user,
        "generation": json.dumps({"protocol": "studygraph.quiz/1"}), "content_hash": "b" * 64, **overrides,
    }  # fmt: skip
    jsonb = {"choices", "citations", "answer_key", "generation"}
    columns = ", ".join(values)
    params = ", ".join(f"CAST(:{k} AS jsonb)" if k in jsonb else f":{k}" for k in values)
    return connection.execute(text(f"INSERT INTO questions ({columns}) VALUES ({params}) RETURNING id"), values).scalar_one()


def _attempt(connection, chain, user, course, topic, **overrides):
    values = {
        "user_id": user, "question_id": chain["question"], "topic_id": topic, "course_id": course,
        "is_correct": True, "hint_count": 0, "response_ms": 100, "quiz_item_id": chain["item"], "answer": "a",
        "grading_method": "choice_id/1", "feedback": json.dumps({"x": 1}), "mastery_before": None,
        "mastery_after": 1.0, "mastery_method": "cumulative_accuracy/1", "stats_attempts": 1, "stats_correct": 1,
        **overrides,
    }  # fmt: skip
    columns = ", ".join([*values, "submitted_at"])
    params = ", ".join([*(f"CAST(:{k} AS jsonb)" if k == "feedback" else f":{k}" for k in values), "now()"])
    connection.execute(text(f"INSERT INTO attempts ({columns}) VALUES ({params})"), values)


@contextmanager
def _refused(connection, match: str | None = None):
    with pytest.raises(DBAPIError, match=match):
        with connection.begin_nested():
            yield


def test_downgrade_refuses_while_m7_state_exists(scratch6, disposable_engine, alembic, alembic_env, committed_transaction):
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        _insert_full_baseline_dataset(connection)
        student = connection.execute(text("SELECT id FROM users WHERE role = 'student'")).scalar_one()
        course, topic = connection.execute(text("SELECT course_id, id FROM topics")).one()
        _m7_chain(connection, student, course, topic)
    output = _failed(run_alembic("downgrade", PREVIOUS, env=alembic_env))
    assert "Downgrade below 0006 refused before any change" in output
    assert _current(alembic) == f"{REVISION} (head)"


def test_offline_sql_carries_the_guards_and_triggers(scratch6, alembic):
    upgrade = alembic("upgrade", f"{PREVIOUS}:{REVISION}", "--sql").stdout
    assert "migration_0006_preflight" in upgrade and "CREATE TRIGGER attempts_guard" in upgrade
    downgrade = alembic("downgrade", f"{REVISION}:{PREVIOUS}", "--sql").stdout
    assert "migration_0006_downgrade_guard" in downgrade


# --- integrity at head ------------------------------------------------------------------------


@pytest.fixture
def m7(world):
    """Two students and two courses at head, and a connection whose transaction is rolled back afterwards."""
    professor = world.user("professor")
    course, other_course = world.course(professor), world.course(professor)
    topic, other_topic = world.topic(course, "T"), world.topic(other_course, "U")
    student, other_student = world.user("student"), world.user("student")
    with world.engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield {
                "c": connection, "course": course, "other_course": other_course, "topic": topic,
                "other_topic": other_topic, "student": student.id, "other": other_student.id,
            }  # fmt: skip
        finally:
            transaction.rollback()


def test_generated_questions_need_complete_metadata_and_their_own_session(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    chain = _m7_chain(c, u, course, topic, ready=False)
    s = chain["session"]
    for overrides, match in (
        ({"hint": None}, "questions_generated_check"),
        ({"citations": json.dumps([])}, "questions_generated_check"),
        ({"answer_key": json.dumps({"choice_id": 1})}, "questions_generated_check"),
        ({"grading_method": "fuzzy"}, "questions_generated_check"),
        ({"purpose": "exam"}, "questions_generated_check"),
        ({"qtype": "short"}, "questions_generated_check"),
        ({"status": "approved"}, "questions_generated_check"),
        ({"generated_for_user_id": None}, "questions_generated_check"),
        ({"generated_for_user_id": m7["other"]}, "questions_generated_session_fkey"),
        ({"topic_id": m7["other_topic"]}, "questions_topic_course_fkey"),
    ):
        with _refused(c, match):
            _generated_question(c, s, u, course, topic, **overrides)
    with _refused(c, "questions_generated_check"):  # the marker cannot be faked without a session
        c.execute(text("INSERT INTO questions (course_id, purpose, qtype, difficulty, text, status) "
                       "VALUES (:c, 'practice', 'mcq', 'easy', 'x', 'generated')"), {"c": course})  # fmt: skip
    _ready(c, s)
    with _refused(c, "only be added to a generating quiz session"):
        _generated_question(c, s, u, course, topic)


def test_assignments_cannot_cross_students_sessions_courses_or_reach_exams(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    mine = _m7_chain(c, u, course, topic, ready=False)
    theirs = _m7_chain(c, m7["other"], course, topic, ready=False)
    exam = c.execute(
        text("INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, status) "
             "VALUES (:c, :t, 'exam', 'mcq', 'easy', 'Exam?', 'approved') RETURNING id"),  # fmt: skip
        {"c": course, "t": topic},
    ).scalar_one()

    def item(question, *, user=u, session=None, position=2, topic_id=topic, purpose="practice"):
        c.execute(
            text("INSERT INTO quiz_items (session_id, user_id, course_id, question_id, topic_id, position, purpose) "
                 "VALUES (:s, :u, :c, :q, :t, :p, :purpose)"),  # fmt: skip
            {"s": session or mine["session"], "u": user, "c": course, "q": question, "t": topic_id, "p": position,
             "purpose": purpose},  # fmt: skip
        )

    for kwargs, match in (
        ({"question": exam}, "quiz_items_"),
        ({"question": exam, "purpose": "exam"}, "quiz_items_purpose_check"),
        ({"question": theirs["question"]}, "quiz_items_question_id_key|quiz_items_generated_question_fkey"),
        ({"question": mine["question"]}, "quiz_items_question_id_key"),
        ({"question": mine["question"], "user": m7["other"]}, "quiz_items_"),
        ({"question": mine["question"], "position": 11}, "quiz_items_position_check"),
        ({"question": mine["question"], "topic_id": m7["other_topic"]}, "quiz_items_"),
    ):
        with _refused(c, match):
            item(**kwargs)
    second_session = c.execute(
        text("INSERT INTO quiz_sessions (user_id, course_id, status, idempotency_key, request_fingerprint, "
             "requested_topic_ids, requested_count, difficulty, scope, protocol, service, completed_at, "
             "failure_code) VALUES (:u, :c, 'failed', gen_random_uuid(), repeat('c', 64), ARRAY[CAST(:t AS uuid)], 1, "
             "'easy', 'class_only', NULL, NULL, now(), 'deadline') RETURNING id"),  # fmt: skip
        {"u": u, "c": course, "t": topic},
    ).scalar_one()
    with _refused(c, "only be assigned to a generating quiz session"):
        item(mine["question"], session=second_session, position=1)


def test_completed_sessions_assignments_and_generated_questions_are_immutable(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    pending = _m7_chain(c, u, course, topic, ready=False)
    with _refused(c, "identity of a quiz session is immutable"):
        c.execute(text("UPDATE quiz_sessions SET requested_count = 2 WHERE id = :s"), {"s": pending["session"]})
    with _refused(c, "cannot have questions"):
        c.execute(text("UPDATE quiz_sessions SET status = 'failed', failure_code = 'x', attempt_token = NULL, "
                       "lease_expires_at = NULL, completed_at = now() WHERE id = :s"), {"s": pending["session"]})  # fmt: skip
    _ready(c, pending["session"])
    q, s = pending["question"], pending["session"]
    for statement in (
        "UPDATE questions SET text = 'changed' WHERE id = :q",
        "UPDATE questions SET choices = '[]'::jsonb WHERE id = :q",
        "UPDATE questions SET answer_key = '{\"choice_id\": \"b\"}'::jsonb WHERE id = :q",
        "UPDATE questions SET hint = 'x', explanation = 'y', citations = '[]'::jsonb WHERE id = :q",
        "UPDATE questions SET status = 'retired' WHERE id = :q",
        "UPDATE questions SET status = 'approved', generated_by_session_id = NULL, generated_for_user_id = NULL, "
        "generation = NULL WHERE id = :q",
        "DELETE FROM questions WHERE id = :q",
        "UPDATE quiz_items SET position = 2 WHERE question_id = :q",
        "DELETE FROM quiz_items WHERE question_id = :q",
        "UPDATE quiz_sessions SET updated_at = now() WHERE id = :s",
        "DELETE FROM quiz_sessions WHERE id = :s",
    ):
        with _refused(c, "immutable|never deleted"):
            c.execute(text(statement), {"q": q, "s": s})


def test_ready_requires_every_requested_assignment(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    session = c.execute(
        text("INSERT INTO quiz_sessions (user_id, course_id, status, idempotency_key, request_fingerprint, "
             "requested_topic_ids, requested_count, difficulty, scope, attempt_token, lease_expires_at) VALUES "
             "(:u, :c, 'generating', gen_random_uuid(), repeat('a', 64), ARRAY[CAST(:t AS uuid)], 2, 'easy', "
             "'class_only', gen_random_uuid(), now() + interval '1 minute') RETURNING id"),  # fmt: skip
        {"u": u, "c": course, "t": topic},
    ).scalar_one()
    with _refused(c, "exactly its requested number"):
        _ready(c, session)


def test_graded_attempts_are_append_only_unique_and_match_the_answer_key(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    chain = _m7_chain(c, u, course, topic)
    for overrides, match in (
        ({"is_correct": False}, "answer key"),
        ({"answer": "z"}, "answer key"),
        ({"hint_count": 2}, "attempts_practice_check"),
        ({"response_ms": -1}, "attempts_practice_check"),
        ({"mastery_after": 1.5}, "attempts_practice_check"),
        ({"stats_correct": 2}, "attempts_practice_check"),
        ({"feedback": None}, "attempts_practice_check"),
        ({"topic_id": m7["other_topic"]}, "attempts_"),
        ({"user_id": m7["other"]}, "attempts_quiz_item_fkey"),
        ({"course_id": m7["other_course"]}, "attempts_"),
    ):
        with _refused(c, match):
            _attempt(c, chain, u, course, topic, **overrides)
    _attempt(c, chain, u, course, topic)
    with _refused(c, "attempts_quiz_item_id_key"):
        _attempt(c, chain, u, course, topic)
    for statement in ("UPDATE attempts SET is_correct = false WHERE quiz_item_id = :i",
                      "UPDATE attempts SET quiz_item_id = NULL WHERE quiz_item_id = :i",
                      "DELETE FROM attempts WHERE quiz_item_id = :i"):  # fmt: skip
        with _refused(c, "append-only"):
            c.execute(text(statement), {"i": chain["item"]})
    with _refused(c, "attempts_practice_check"):  # an attempt without an assignment cannot carry grading columns
        c.execute(
            text("INSERT INTO attempts (user_id, question_id, course_id, is_correct, hint_count, answer) "
                 "VALUES (:u, :q, :c, true, 0, 'a')"),  # fmt: skip
            {"u": u, "q": chain["question"], "c": course},
        )


def test_practice_stats_counters_and_session_checks(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    for attempts, correct, t, match in ((1, 2, topic, "counters"), (-1, 0, topic, "counters"),
                                        (1, 1, m7["other_topic"], "topic_practice_stats_topic_fkey")):  # fmt: skip
        with _refused(c, match):
            c.execute(
                text("INSERT INTO topic_practice_stats (user_id, topic_id, course_id, attempts, correct) "
                     "VALUES (:u, :t, :c, :a, :k)"),  # fmt: skip
                {"u": u, "t": t, "c": course, "a": attempts, "k": correct},
            )
    base = ("INSERT INTO quiz_sessions (user_id, course_id, status, idempotency_key, request_fingerprint, "
            "requested_topic_ids, requested_count, difficulty, scope, attempt_token, lease_expires_at, failure_code, "
            "completed_at, protocol, service) VALUES (:u, :c, {status}, {key}, {fp}, {topics}, {count}, 'easy', "
            "{scope}, {token}, {lease}, {failure}, {completed}, {protocol}, {protocol})")  # fmt: skip
    good = {"status": "'generating'", "key": "gen_random_uuid()", "fp": "repeat('a', 64)",
            "topics": "ARRAY[CAST(:t AS uuid)]", "count": "1", "scope": "'class_only'", "token": "gen_random_uuid()",
            "lease": "now()", "failure": "NULL", "completed": "NULL", "protocol": "NULL"}  # fmt: skip
    for change, match in (
        ({"topics": "ARRAY[CAST(:t AS uuid), NULL]"}, "request_check"),
        ({"topics": "CAST(ARRAY[] AS uuid[])"}, "request_check"),
        ({"count": "11"}, "request_check"),
        ({"scope": "'class_plus_web'"}, "request_check"),
        ({"fp": "'short'"}, "request_check"),
        ({"token": "NULL"}, "state_check"),
        ({"status": "'ready'", "token": "NULL", "lease": "NULL", "protocol": "'p'"}, "state_check"),
        ({"status": "'failed'", "token": "NULL", "lease": "NULL", "completed": "now()"}, "state_check"),
        ({"status": "'cancelled'"}, "status_check|state_check"),
    ):
        with _refused(c, match):
            c.execute(text(base.format(**{**good, **change})), {"u": u, "c": course, "t": topic})
    key = uuid.uuid4()
    c.execute(text(base.format(**{**good, "key": ":k"})), {"u": u, "c": course, "t": topic, "k": key})
    with _refused(c, "quiz_sessions_one_generating_idx|quiz_sessions_user_id_idempotency_key_key"):
        c.execute(text(base.format(**good)), {"u": u, "c": course, "t": topic})
    with _refused(c, "quiz_sessions_user_id_idempotency_key_key"):
        c.execute(text(base.format(**{**good, "key": ":k", "status": "'failed'", "token": "NULL", "lease": "NULL",
                                       "failure": "'x'", "completed": "now()"})),
                  {"u": u, "c": course, "t": topic, "k": key})  # fmt: skip


# --- review v2: NULL-safe CHECKs (H1) -----------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"grading_method": None},
        {"answer_key": json.dumps({})},  # jsonb_typeof(answer_key -> 'choice_id') is NULL
        {"answer_key": None},
        {"answer_key": json.dumps({"choice_id": None})},
        {"answer_key": json.dumps({"choice_id": "z"})},  # not one of the choices
        {"answer_key": json.dumps({"choice_id": "a", "extra": 1})},
        {"answer_key": json.dumps({"choice_id": ""})},
        {"hint": None},
        {"explanation": None},
        {"content_hash": None},
        {"content_hash": "G" * 64},
        {"generation": None},
        {"generation": json.dumps([])},
        {"choices": None},
        {"choices": json.dumps([{"id": "a", "text": "only one"}])},
        {"choices": json.dumps({"a": 1})},
        {"citations": None},
        {"citations": json.dumps({})},
        {"topic_id": None},
        {"generated_for_user_id": None},
    ],
    ids=lambda o: ",".join(f"{k}={'NULL' if v is None else v}"[:40] for k, v in o.items()),
)
def test_generated_question_metadata_is_null_safe(m7, overrides):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    chain = _m7_chain(c, u, course, topic, ready=False)
    with _refused(c, "questions_generated_check|non-array|violates"):
        _generated_question(c, chain["session"], u, course, topic, **overrides)


@pytest.mark.parametrize(
    "column",
    ["answer", "grading_method", "feedback", "mastery_after", "mastery_method", "stats_attempts", "stats_correct",
     "quiz_item_id"],  # fmt: skip
)
def test_graded_attempt_columns_are_null_safe(m7, column):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    chain = _m7_chain(c, u, course, topic)
    with _refused(c, "attempts_practice_check|answer key|ready quiz session"):
        _attempt(c, chain, u, course, topic, **{column: None})
    with _refused(c, "attempts_practice_check"):  # the NULL mastery method of the review, without the trigger's help
        _attempt(c, chain, u, course, topic, mastery_method="other")


def test_models_and_migration_carry_the_same_check_text():
    import importlib.util
    from pathlib import Path

    from sqlalchemy import CheckConstraint

    from app.models import Attempt, Question

    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0006_quizzes_attempts.py"
    spec = importlib.util.spec_from_file_location("migration_0006_under_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def check(model, prefix):
        found = [str(c.sqltext) for c in model.__table__.constraints
                 if isinstance(c, CheckConstraint) and str(c.sqltext).startswith(prefix)]  # fmt: skip
        assert len(found) == 1
        return found[0]

    assert check(Question, "(generated_by_session_id IS NULL") == migration.QUESTION_GENERATED_CHECK
    assert check(Attempt, "(quiz_item_id IS NULL") == migration.ATTEMPT_PRACTICE_CHECK


# --- review v2: lifecycle and legacy bypass (H2) --------------------------------------------------


def _legacy_question_and_attempt(c, course, topic, user):
    question = c.execute(
        text("INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, status) "
             "VALUES (:c, :t, 'practice', 'mcq', 'easy', 'Legacy?', 'approved') RETURNING id"),  # fmt: skip
        {"c": course, "t": topic},
    ).scalar_one()
    attempt = c.execute(  # legacy writers know neither course_id nor the M7 columns
        text("INSERT INTO attempts (user_id, question_id, topic_id, is_correct, hint_count) "
             "VALUES (:u, :q, :t, true, 0) RETURNING id"),  # fmt: skip
        {"u": user, "q": question, "t": topic},
    ).scalar_one()
    return question, attempt


def test_a_generated_question_is_answered_only_through_its_ready_assignment(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    chain = _m7_chain(c, u, course, topic)  # ready
    for user in (u, m7["other"]):  # a legacy-style insert, whoever the student is
        with _refused(c, "only be answered through its quiz assignment"):
            c.execute(
                text("INSERT INTO attempts (user_id, question_id, topic_id, is_correct, hint_count) "
                     "VALUES (:u, :q, :t, true, 0)"),  # fmt: skip
                {"u": user, "q": chain["question"], "t": topic},
            )
    with _refused(c, "attempts_practice_check"):  # grading columns without an assignment
        c.execute(text("INSERT INTO attempts (user_id, question_id, course_id, is_correct, hint_count, answer) "
                       "VALUES (:u, :q, :c, true, 0, 'a')"), {"u": u, "q": chain["question"], "c": course})  # fmt: skip
    pending = _m7_chain(c, m7["other"], course, topic, ready=False)
    with _refused(c, "needs an assignment of a ready quiz session"):  # graded, but its session is still generating
        _attempt(c, pending, m7["other"], course, topic)
    _attempt(c, chain, u, course, topic)  # the legitimate path still works

    question, attempt = _legacy_question_and_attempt(c, course, topic, u)  # legacy operations stay possible
    c.execute(text("UPDATE attempts SET hint_count = 3 WHERE id = :a"), {"a": attempt})
    c.execute(text("UPDATE questions SET text = 'Edited' WHERE id = :q"), {"q": question})
    assert c.execute(text("SELECT course_id FROM attempts WHERE id = :a"), {"a": attempt}).scalar_one() == course
    with _refused(c, "cannot be moved to a generated question"):  # an UPDATE is no way around the rule either
        c.execute(text("UPDATE attempts SET question_id = :q WHERE id = :a"), {"q": chain["question"], "a": attempt})
    with _refused(c, "cannot be moved to a generated question"):
        c.execute(text("UPDATE attempts SET question_id = :q WHERE id = :a"), {"q": pending["question"], "a": attempt})


def test_a_session_cannot_be_inserted_ready(m7):
    c, u, course, topic = m7["c"], m7["student"], m7["course"], m7["topic"]
    with _refused(c, "cannot be inserted ready"):
        c.execute(
            text("INSERT INTO quiz_sessions (user_id, course_id, status, idempotency_key, request_fingerprint, "
                 "requested_topic_ids, requested_count, difficulty, scope, protocol, service, completed_at) VALUES "
                 "(:u, :c, 'ready', gen_random_uuid(), repeat('a', 64), ARRAY[CAST(:t AS uuid)], 1, 'easy', "
                 "'class_only', 'studygraph.quiz/1', 'ai-service', now())"),  # fmt: skip
            {"u": u, "c": course, "t": topic},
        )


@pytest.fixture
def committed_chain(world):
    """A committed generating session with one assigned question, to race finalization against inserts."""
    professor = world.user("professor")
    course = world.course(professor)
    topic = world.topic(course, "T")
    student = world.user("student").id
    with world.engine.begin() as connection:
        chain = _m7_chain(connection, student, course, topic, ready=False)
        extra = _generated_question(connection, chain["session"], student, course, topic, content_hash="c" * 64)
    return {"engine": world.engine, "student": student, "course": course, "topic": topic, "extra": extra, **chain}


def _race(blocker, racer, *, release):
    """Run `racer` (a callable on a fresh connection) while `blocker`'s transaction is open; it must not finish
    until `release()` ends that transaction. Returns what the racer raised, or None."""
    import threading

    outcome: dict = {}

    def run():
        try:
            racer()
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            outcome["done"] = True

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(0.7)
    assert not outcome.get("done"), "the racing statement did not wait for the transaction holding the session row"
    release()
    thread.join(10)
    assert outcome.get("done")
    return outcome.get("error")


def _finalize_sql():
    return ("UPDATE quiz_sessions SET status = 'ready', protocol = 'p', service = 's', attempt_token = NULL, "
            "lease_expires_at = NULL, completed_at = now() WHERE id = :s")  # fmt: skip


def test_an_assignment_in_flight_blocks_finalization_instead_of_slipping_past_it(committed_chain):
    x = committed_chain
    engine = x["engine"]
    with engine.connect() as inserting:
        transaction = inserting.begin()
        inserting.execute(  # a second assignment, uncommitted: the session must not become ready around it
            text("INSERT INTO quiz_items (session_id, user_id, course_id, question_id, topic_id, position) "
                 "VALUES (:s, :u, :c, :q, :t, 2)"),  # fmt: skip
            {"s": x["session"], "u": x["student"], "c": x["course"], "q": x["extra"], "t": x["topic"]},
        )

        def finalize():
            with engine.begin() as other:
                other.execute(text("SET LOCAL lock_timeout = '10s'"))
                other.execute(text(_finalize_sql()), {"s": x["session"]})

        error = _race(inserting, finalize, release=transaction.commit)
    assert error is not None and "exactly its requested number" in str(error)
    with engine.connect() as check:
        assert check.execute(text("SELECT status FROM quiz_sessions WHERE id = :s"), {"s": x["session"]}).scalar_one() == "generating"
        assert check.execute(text("SELECT count(*) FROM quiz_items WHERE session_id = :s"), {"s": x["session"]}).scalar_one() == 2


@pytest.mark.parametrize("what", ["question", "assignment"])
def test_finalization_in_flight_blocks_a_late_insert_instead_of_being_bypassed(committed_chain, what):
    x = committed_chain
    engine = x["engine"]

    def late():
        with engine.begin() as other:
            other.execute(text("SET LOCAL lock_timeout = '10s'"))
            if what == "question":
                _generated_question(other, x["session"], x["student"], x["course"], x["topic"], content_hash="d" * 64)
            else:
                other.execute(
                    text("INSERT INTO quiz_items (session_id, user_id, course_id, question_id, topic_id, position) "
                         "VALUES (:s, :u, :c, :q, :t, 2)"),  # fmt: skip
                    {"s": x["session"], "u": x["student"], "c": x["course"], "q": x["extra"], "t": x["topic"]},
                )

    with engine.connect() as finalizing:
        transaction = finalizing.begin()
        finalizing.execute(text(_finalize_sql()), {"s": x["session"]})  # holds the row, not committed yet
        error = _race(finalizing, late, release=transaction.commit)
    assert error is not None
    assert "only be added to a generating" in str(error) or "only be assigned to a generating" in str(error)
    with engine.connect() as check:
        assert check.execute(text("SELECT status FROM quiz_sessions WHERE id = :s"), {"s": x["session"]}).scalar_one() == "ready"
        assert check.execute(text("SELECT count(*) FROM quiz_items WHERE session_id = :s"), {"s": x["session"]}).scalar_one() == 1
        assert check.execute(text("SELECT count(*) FROM questions WHERE generated_by_session_id = :s"), {"s": x["session"]}).scalar_one() == 2
