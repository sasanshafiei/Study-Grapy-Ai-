"""Revision 0008 (M9-A1) on the disposable database: upgrade/downgrade safety and database-level integrity.

Identifiers MIG-01 ... MIG-25 refer to docs/M9 test plan §4.1 (M9_IMPLEMENTATION_TEST_PLAN.md). MIG-03
(head ORM parity, `alembic check`) is tests/test_migrations.py; MIG-18 (worker revision) is
tests/test_celery_config.py and tests/test_m8_unit.py plus the runtime check below.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests import exam_support as ex
from tests import schema_snapshot
from tests.alembic_cli import run_alembic
from tests.m8_support import graded_attempt
from tests.test_migration_0003 import _failed, scratch3  # noqa: F401  (fixture chain)
from tests.test_migration_0005 import scratch5  # noqa: F401
from tests.test_migration_0006 import _race, scratch6  # noqa: F401
from tests.test_migration_0007 import at_0006_with_m7  # noqa: F401
from tests.test_migrations import _current, _table_contents

REVISION, PREVIOUS = "0008", "0007"
NEW_TABLES = {"exams", "exam_items", "exam_blueprint_rows", "exam_question_revisions", "exam_audit_events",
              "exam_idempotency_keys"}  # fmt: skip
NEW_QUESTION_COLUMNS = ("version", "created_by", "updated_at", "approved_revision_no", "bloom_level")
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0008_professor_exams.py"


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0008_under_test", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables(engine) -> set[str]:
    with engine.connect() as c:
        return set(c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())


def _question_columns(engine) -> list[str]:
    with engine.connect() as c:
        return list(c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'questions' "
            "ORDER BY ordinal_position"
        )).scalars())  # fmt: skip


def _questions_checksum(engine, columns: list[str]) -> tuple:
    row = "ROW(" + ", ".join(columns) + ")::text"
    with engine.connect() as c:
        return tuple(c.execute(text(f"SELECT count(*), md5(coalesce(string_agg({row}, '|' ORDER BY {row}), '')) FROM questions")).one())


def _triggers_and_functions(engine) -> dict:
    with engine.connect() as c:
        triggers = c.execute(text(
            "SELECT tgrelid::regclass::text || '.' || tgname, pg_get_triggerdef(oid) FROM pg_trigger "
            "WHERE NOT tgisinternal ORDER BY 1"
        )).all()  # fmt: skip
        functions = c.execute(text(
            "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', md5(pg_get_functiondef(p.oid)) "
            "FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f' ORDER BY 1"
        )).all()  # fmt: skip
    return {"triggers": [tuple(r) for r in triggers], "functions": [tuple(r) for r in functions]}


def _snapshot(engine) -> dict:
    with engine.connect() as c:
        return schema_snapshot.snapshot(c)


@pytest.fixture
def at_0007(at_0006_with_m7, disposable_engine, alembic, committed_transaction):  # noqa: F811
    """0007 with baseline, M7 and M8 data, plus a legacy exam-purpose question with a legacy attempt on it."""
    ids = at_0006_with_m7
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as c:
        legacy = ex.legacy_question(c, ids["course"], purpose="exam")
        c.execute(
            text("INSERT INTO attempts (user_id, question_id, topic_id, is_correct, hint_count) VALUES (:u, :q, :t, true, 0)"),
            {"u": ids["students"][0], "q": legacy, "t": ids["topics"][0]},
        )
        ex.legacy_question(c, ids["course"], purpose="practice")
    return {**ids, "legacy_exam_question": legacy}


# --- historical upgrade / downgrade -----------------------------------------------------------------------------


def test_mig01_upgrade_keeps_every_existing_row_and_adds_only_null_columns(at_0007, disposable_engine, alembic):
    tables = _tables(disposable_engine) - {"alembic_version"}
    old_columns = _question_columns(disposable_engine)
    before = _table_contents(disposable_engine, tables - {"questions"})
    questions_before = _questions_checksum(disposable_engine, old_columns)
    alembic("upgrade", REVISION)
    assert _table_contents(disposable_engine, tables - {"questions"}) == before
    assert _questions_checksum(disposable_engine, old_columns) == questions_before
    assert _question_columns(disposable_engine) == old_columns + list(NEW_QUESTION_COLUMNS)
    assert NEW_TABLES <= _tables(disposable_engine)
    with disposable_engine.connect() as c:
        nulls = " OR ".join(f"{column} IS NOT NULL" for column in NEW_QUESTION_COLUMNS)
        assert c.execute(text(f"SELECT count(*) FROM questions WHERE {nulls}")).scalar_one() == 0
        assert set(ex.m9_row_counts(c).values()) == {0}
        assert c.execute(text("SELECT count(*) FROM questions WHERE generated_by_session_id IS NOT NULL")).scalar_one() == 10
    alembic("upgrade", REVISION)  # no-op


def test_mig02_preflight_reports_legacy_exam_rows_without_refusing(at_0007, disposable_engine, alembic):
    notices: list[str] = []
    with disposable_engine.connect() as c:
        c.connection.driver_connection.add_notice_handler(lambda diag: notices.append(diag.message_primary))
        c.execute(text(_migration().UPGRADE_PREFLIGHT))
        c.rollback()
    assert notices == [
        "Migration 0008 preflight: 1 legacy exam-purpose questions and 1 attempts on them; they are kept unchanged "
        "and are invisible to the exam API."
    ]
    alembic("upgrade", REVISION)
    with disposable_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM attempts WHERE question_id = :q"),
                         {"q": at_0007["legacy_exam_question"]}).scalar_one() == 1  # fmt: skip


def test_mig05_downgrade_without_m9_state_restores_the_0007_schema_and_data(at_0007, disposable_engine, alembic):
    tables = _tables(disposable_engine) - {"alembic_version"}
    schema = _snapshot(disposable_engine)
    routines = _triggers_and_functions(disposable_engine)
    data = _table_contents(disposable_engine, tables)
    alembic("upgrade", REVISION)
    assert _snapshot(disposable_engine) != schema
    alembic("downgrade", PREVIOUS)
    assert _current(alembic).split()[0] == PREVIOUS
    assert _snapshot(disposable_engine) == schema
    assert _triggers_and_functions(disposable_engine) == routines
    assert _table_contents(disposable_engine, tables) == data
    assert not NEW_TABLES & _tables(disposable_engine)
    alembic("upgrade", REVISION)
    alembic("downgrade", PREVIOUS)
    assert _snapshot(disposable_engine) == schema


def _state(kind: str, c, ids) -> None:
    course, professor = ids["course"], ex.user(c)
    c.execute(text("UPDATE courses SET owner_id = :p WHERE id = :c"), {"p": professor, "c": course})
    if kind == "bloom_level":
        c.execute(text("UPDATE questions SET bloom_level = 'apply' WHERE id = :q"), {"q": ids["legacy_exam_question"]})
    elif kind == "exam_audit_events":
        c.execute(text("INSERT INTO exam_audit_events (course_id, actor_id, entity_kind, entity_id, action, details) "
                       "VALUES (:c, :u, 'exam', gen_random_uuid(), 'exam_created', '{}'::jsonb)"), {"c": course, "u": professor})  # fmt: skip
    elif kind == "exam_idempotency_keys":
        c.execute(text("INSERT INTO exam_idempotency_keys (user_id, idempotency_key, operation, request_fingerprint, "
                       "course_id, resource_id) VALUES (:u, gen_random_uuid(), 'exam_create', repeat('a', 64), :c, "
                       "gen_random_uuid())"), {"c": course, "u": professor})  # fmt: skip
    elif kind == "bank_question":
        ex.bank_question(c, course, professor)
    elif kind == "exam_question_revisions":
        ex.approved_question(c, course, professor)
    elif kind == "exams":
        ex.exam(c, course, professor)
    elif kind == "exam_blueprint_rows":
        ex.blueprint_row(c, ex.exam(c, course, professor), course, 1)
    elif kind == "exam_items":
        ex.item(c, ex.exam(c, course, professor), course, ex.bank_question(c, course, professor), 1)


@pytest.mark.parametrize("kind", sorted(NEW_TABLES | {"bank_question", "bloom_level"}))
def test_mig06_downgrade_refuses_while_m9_state_exists(at_0007, disposable_engine, alembic, alembic_env, committed_transaction, kind):
    alembic("upgrade", REVISION)
    with committed_transaction() as c:
        _state(kind, c, at_0007)
    schema = _snapshot(disposable_engine)
    output = _failed(run_alembic("downgrade", PREVIOUS, env=alembic_env))
    assert "Downgrade below 0008 refused before any change" in output
    assert _current(alembic).split()[0] == REVISION
    assert _snapshot(disposable_engine) == schema


def test_mig07_offline_sql_carries_the_preflight_guard_triggers_and_deferrable_keys(scratch6, alembic):  # noqa: F811
    upgrade = alembic("upgrade", f"{PREVIOUS}:{REVISION}", "--sql").stdout
    assert "migration_0008_preflight" in upgrade and "LOCK TABLE questions, attempts IN SHARE ROW EXCLUSIVE MODE" in upgrade
    assert upgrade.index("migration_0008_preflight") < upgrade.index("ALTER TABLE questions ADD COLUMN version")
    for name in ("questions_bank_guard", "exam_question_revisions_guard", "exams_guard", "exam_items_guard",
                 "exam_blueprint_rows_guard", "exam_audit_events_guard", "attempts_exam_guard", "exams_truncate_guard"):
        assert f"CREATE TRIGGER {name}" in upgrade, name
    assert upgrade.count("DEFERRABLE INITIALLY DEFERRED") == 2
    assert "DROP " not in upgrade.split("migration_0008_preflight")[1].split("CREATE FUNCTION")[0]
    downgrade = alembic("downgrade", f"{REVISION}:{PREVIOUS}", "--sql").stdout
    assert "migration_0008_downgrade_guard" in downgrade
    assert downgrade.index("migration_0008_downgrade_guard") < downgrade.index("DROP TRIGGER")


# --- rules at head ------------------------------------------------------------------------------------------------


@pytest.fixture
def m9(world):
    professor = world.user("professor")
    course, other = world.course(professor, code="CHEM 101"), world.course(professor, code="PHYS 1")
    topic, other_topic = world.topic(course, "Stoichiometry"), world.topic(other, "Kinematics")
    student = world.user("student")
    world.enroll(student, course)
    return {"engine": world.engine, "professor": professor.id, "course": course, "other": other, "topic": topic,
            "other_topic": other_topic, "student": student.id, "world": world}  # fmt: skip


def _refused(engine, fn, match: str):
    with pytest.raises(DBAPIError, match=match):
        with engine.begin() as c:
            fn(c)


def _ok(engine, fn):
    with engine.begin() as c:
        return fn(c)


def test_mig04_models_and_migration_carry_the_same_check_text():
    from app import schema_checks

    migration = _migration()
    names = [n for n in vars(schema_checks) if n.isupper() and n in vars(migration)]
    m9_names = {"BLOOM_LEVELS_SQL", "QUESTION_BLOOM_CHECK", "QUESTION_CONTENT_SHAPE", "QUESTION_BANK_CHECK",
                "REVISION_PURPOSE_CHECK", "REVISION_DOMAIN_CHECK", "REVISION_CONTENT_CHECK", "EXAM_STATUS_CHECK",
                "EXAM_FIELDS_CHECK", "EXAM_FINGERPRINT_CHECK", "EXAM_STATE_CHECK", "EXAM_ITEM_PURPOSE_CHECK",
                "EXAM_ITEM_VALUES_CHECK", "BLUEPRINT_VALUES_CHECK", "AUDIT_ACTIONS", "AUDIT_SHAPE_CHECK",
                "IDEMPOTENCY_CHECK"}  # fmt: skip
    assert m9_names <= set(names)
    for name in m9_names:
        assert getattr(schema_checks, name) == getattr(migration, name), name


def test_mig08_bank_check_is_true_for_legacy_rows_and_strict_for_bank_rows(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    _ok(e, lambda c: ex.legacy_question(c, course, purpose="exam"))
    _ok(e, lambda c: ex.legacy_question(c, course, purpose="practice", bloom_level="apply", status="draft"))
    _ok(e, lambda c: ex.legacy_question(c, course, purpose="exam", status="retired", choices=None))
    _ok(e, lambda c: ex.bank_question(c, course, prof))
    violations = [
        {"purpose": "practice"}, {"created_by": None}, {"hint": "h"}, {"grading_method": "choice_id/1"},
        {"answer": "A"}, {"content_hash": "x" * 64}, {"content_hash": None}, {"text": "   "}, {"text": "s" * 4001},
        {"explanation": "e" * 4001}, {"citations": [{"document_id": str(uuid.uuid4()), "locator": {"page": 1}}] * 5},
        {"citations": {"a": 1}}, {"answer_key": None}, {"choices": None},
    ]  # fmt: skip
    for change in violations:
        _refused(e, lambda c, change=change: ex.bank_question(c, course, prof, **change), "questions_bank_check|questions_purpose|null value")
    # a partial marker is neither a legacy row nor a bank row
    _refused(e, lambda c: c.execute(text("UPDATE questions SET updated_at = now() WHERE course_id = :c AND version IS NULL"),
                                    {"c": course}), "cannot become a bank question")  # fmt: skip
    _refused(e, lambda c: ex.legacy_question(c, course, updated_at=None, created_by=prof), "questions_bank_check")
    _refused(e, lambda c: ex.legacy_question(c, course, bloom_level="memorize"), "questions_bloom_level_check")


def test_mig09_content_shape_per_type(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    six = [{"id": x, "text": x.upper()} for x in "abcdef"]
    seven = six + [{"id": "g", "text": "G"}]
    for change in (
        {"choices": [{"id": "a", "text": "A"}]}, {"choices": seven}, {"answer_key": {"choice_id": "c"}},
        {"answer_key": {"choice_id": "a", "extra": 1}}, {"answer_key": {"choice_id": 1}}, {"choices": {"id": "a"}},
        {"answer_key": {"model_answer": "x", "rubric": None}},
    ):  # fmt: skip
        _refused(e, lambda c, change=change: ex.bank_question(c, course, prof, **change), "questions_bank_check")
    _ok(e, lambda c: ex.bank_question(c, course, prof, choices=six, answer_key={"choice_id": "f"}))
    for change in (
        {"choices": MCQ}, {"answer_key": {"choice_id": "a"}}, {"answer_key": {"choice_id": "true", "x": 1}},
    ):  # fmt: skip
        _refused(e, lambda c, change=change: ex.bank_question(c, course, prof, qtype="true_false", **change), "questions_bank_check")
    for change in (
        {"choices": MCQ}, {"answer_key": {"rubric": None}}, {"answer_key": {"model_answer": "", "rubric": None}},
        {"answer_key": {"model_answer": "m" * 4001, "rubric": None}}, {"answer_key": {"model_answer": "m"}},
        {"answer_key": {"model_answer": "m", "rubric": 5}}, {"answer_key": {"model_answer": "m", "rubric": "r" * 4001}},
        {"answer_key": {"model_answer": "m", "rubric": None, "x": 1}},
    ):  # fmt: skip
        _refused(e, lambda c, change=change: ex.bank_question(c, course, prof, qtype="short", **change), "questions_bank_check")
    _ok(e, lambda c: ex.bank_question(c, course, prof, qtype="short", answer_key={"model_answer": "m" * 4000, "rubric": "r"}))


MCQ = ex.MCQ_CHOICES


def test_mig10_bank_guard_rules_and_legacy_freedom(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    _refused(e, lambda c: ex.bank_question(c, course, prof, version=2), "starts as a draft at version 1")
    _refused(e, lambda c: ex.bank_question(c, course, prof, status="approved"), "starts as a draft at version 1")
    q = _ok(e, lambda c: ex.bank_question(c, course, prof))
    p = {"q": q}
    _refused(e, lambda c: c.execute(text("UPDATE questions SET text = 'x' WHERE id = :q"), p), "increments its version")
    _refused(e, lambda c: c.execute(text("UPDATE questions SET text = 'x', version = version + 2 WHERE id = :q"), p), "increments")
    _refused(e, lambda c: c.execute(text("UPDATE questions SET version = version + 1, created_by = :u WHERE id = :q"),
                                    {**p, "u": m9["student"]}), "identity of a bank question")  # fmt: skip
    _refused(e, lambda c: c.execute(text("UPDATE questions SET version = version + 1, course_id = :o WHERE id = :q"),
                                    {**p, "o": m9["other"]}), "identity of a bank question")  # fmt: skip
    _refused(e, lambda c: c.execute(text("UPDATE questions SET version = version + 1, created_at = created_at - interval '1 day' "
                                         "WHERE id = :q"), p), "identity")  # fmt: skip
    _refused(e, lambda c: c.execute(text("DELETE FROM questions WHERE id = :q"), p), "never deleted")
    _ok(e, lambda c: c.execute(text("UPDATE questions SET text = 'Edited?', version = version + 1 WHERE id = :q"), p))
    # approval without a revision, or with a revision of other content, is refused
    _refused(e, lambda c: c.execute(text("UPDATE questions SET status = 'approved', version = version + 1 WHERE id = :q"), p),
             "must equal its approved revision")  # fmt: skip
    _ok(e, lambda c: ex.approve(c, q, prof))
    _refused(e, lambda c: c.execute(text("UPDATE questions SET text = 'Other?', version = version + 1 WHERE id = :q"), p),
             "invalid bank question transition")  # approved -> approved with a content change
    _refused(e, lambda c: c.execute(text("UPDATE questions SET status = 'draft', approved_revision_no = NULL, "
                                         "version = version + 1 WHERE id = :q"), p), "approved revision")  # fmt: skip
    # edit (approved -> draft, revision kept), then a hash that does not match the newest revision cannot be approved
    _ok(e, lambda c: c.execute(text("UPDATE questions SET status = 'draft', text = 'v2?', content_hash = repeat('c', 64), "
                                    "version = version + 1 WHERE id = :q"), p))  # fmt: skip
    _refused(e, lambda c: c.execute(text("UPDATE questions SET status = 'approved', version = version + 1 WHERE id = :q"), p),
             "must equal its approved revision")  # fmt: skip
    _refused(e, lambda c: c.execute(text("UPDATE questions SET status = 'retired', text = 'v3?', version = version + 1 "
                                         "WHERE id = :q"), p), "cannot change its content")  # fmt: skip
    _ok(e, lambda c: c.execute(text("UPDATE questions SET status = 'retired', version = version + 1 WHERE id = :q"), p))
    _refused(e, lambda c: c.execute(text("UPDATE questions SET status = 'draft', version = version + 1 WHERE id = :q"), p),
             "retired bank question is immutable")  # fmt: skip
    # legacy rows keep their freedom; they can never become bank rows
    legacy = _ok(e, lambda c: ex.legacy_question(c, course, purpose="exam"))
    _ok(e, lambda c: c.execute(text("UPDATE questions SET text = 'free', bloom_level = 'apply' WHERE id = :q"), {"q": legacy}))
    _refused(e, lambda c: c.execute(text("UPDATE questions SET version = 1, created_by = :u, updated_at = now(), "
                                         "content_hash = repeat('a', 64), status = 'draft', choices = CAST(:ch AS jsonb), "
                                         "answer_key = '{\"choice_id\": \"a\"}'::jsonb WHERE id = :q"),
                                    {"q": legacy, "u": prof, "ch": json.dumps(MCQ)}), "cannot become a bank question")  # fmt: skip
    _ok(e, lambda c: c.execute(text("DELETE FROM questions WHERE id = :q"), {"q": legacy}))


def test_mig11_revisions_are_immutable_contiguous_exam_purpose_and_course_scoped(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    q = _ok(e, lambda c: ex.bank_question(c, course, prof, topic_id=m9["topic"]))
    _refused(e, lambda c: c.execute(text(ex.INSERT_REVISION), ex.revision_values(c, q, prof, revision_no=2)), "contiguous")
    _ok(e, lambda c: ex.approve(c, q, prof))
    key = {"q": q}
    _refused(e, lambda c: c.execute(text("UPDATE exam_question_revisions SET stem = 'x' WHERE question_id = :q"), key), "immutable")
    _refused(e, lambda c: c.execute(text("DELETE FROM exam_question_revisions WHERE question_id = :q"), key), "immutable")
    # a revision for a practice/legacy question: the trigger refuses, and so would the purpose key
    legacy = _ok(e, lambda c: ex.legacy_question(c, course, purpose="practice", choices=json.dumps(MCQ)))
    values = None

    def legacy_revision(c):
        row = ex.revision_values(c, q, prof)
        c.execute(text(ex.INSERT_REVISION), {**row, "question_id": legacy, "revision_no": 1})

    _refused(e, legacy_revision, "only approve a draft bank question|exam_question_revisions_question_fkey")

    def cross_course_topic(c):
        c.execute(text("UPDATE questions SET status = 'draft', version = version + 1, content_hash = repeat('d', 64) "
                       "WHERE id = :q"), key)  # fmt: skip
        row = ex.revision_values(c, q, prof)
        c.execute(text("ALTER TABLE exam_question_revisions DISABLE TRIGGER exam_question_revisions_guard"))
        c.execute(text(ex.INSERT_REVISION), {**row, "topic_id": m9["other_topic"]})

    _refused(e, cross_course_topic, "exam_question_revisions_topic_fkey")
    assert values is None


def test_mig12_exam_items_are_course_and_purpose_scoped_and_draft_only(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    q1, q2 = _ok(e, lambda c: (ex.approved_question(c, course, prof), ex.approved_question(c, course, prof)))
    foreign = _ok(e, lambda c: ex.approved_question(c, m9["other"], prof))
    practice = _ok(e, lambda c: ex.legacy_question(c, course, purpose="practice"))
    exam = _ok(e, lambda c: ex.exam(c, course, prof))
    _refused(e, lambda c: ex.item(c, exam, course, practice, 1), "exam_items_question_fkey")
    _refused(e, lambda c: ex.item(c, exam, course, foreign, 1), "exam_items_question_fkey")
    _refused(e, lambda c: c.execute(text("INSERT INTO exam_items (exam_id, course_id, question_id, purpose, position, points) "
                                         "VALUES (:e, :c, :q, 'practice', 1, 1)"), {"e": exam, "c": course, "q": practice}),
             "exam_items_purpose_check")  # fmt: skip
    a = _ok(e, lambda c: ex.item(c, exam, course, q1, 1))
    _refused(e, lambda c: ex.item(c, exam, course, q1, 2), "exam_items_exam_id_question_id_key")
    _refused(e, lambda c: ex.item(c, exam, course, q2, 1), "exam_items_exam_id_position_key")  # at commit
    for points in (0, 101):
        _refused(e, lambda c, points=points: ex.item(c, exam, course, q2, 2, points=points), "exam_items_values_check")
    b = _ok(e, lambda c: ex.item(c, exam, course, q2, 2))

    def swap(c):  # transient duplicates are allowed inside the transaction (deferred)
        c.execute(text("UPDATE exam_items SET position = 1 WHERE id = :b"), {"b": b})
        c.execute(text("UPDATE exam_items SET position = 2 WHERE id = :a"), {"a": a})

    _ok(e, swap)
    _ok(e, lambda c: ex.finalize(c, exam, prof))
    _refused(e, lambda c: c.execute(text("UPDATE exam_items SET points = 3 WHERE id = :a"), {"a": a}), "while their exam is a draft")
    _refused(e, lambda c: c.execute(text("DELETE FROM exam_items WHERE id = :a"), {"a": a}), "while their exam is a draft")
    _refused(e, lambda c: ex.item(c, exam, course, _ok(e, lambda d: ex.approved_question(d, course, prof)), 3), "draft")
    _ok(e, lambda c: ex.archive(c, exam))
    _refused(e, lambda c: c.execute(text("UPDATE exam_items SET points = 3 WHERE id = :a"), {"a": a}), "while their exam is a draft")
    draft = _ok(e, lambda c: ex.exam(c, course, prof))
    it = _ok(e, lambda c: ex.item(c, draft, course, q1, 1))
    _refused(e, lambda c: c.execute(text("UPDATE exam_items SET question_id = :q WHERE id = :i"), {"q": q2, "i": it}), "identity")
    _refused(e, lambda c: c.execute(text("UPDATE exam_items SET exam_id = :x WHERE id = :i"), {"x": exam, "i": it}),
             "draft|identity")  # fmt: skip


def test_mig13_exam_transitions_and_finalization_completeness(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    _refused(e, lambda c: c.execute(text("INSERT INTO exams (course_id, created_by, title, status, version) "
                                         "VALUES (:c, :u, 'T', 'draft', 2)"), {"c": course, "u": prof}), "starts as a draft")  # fmt: skip
    exam = _ok(e, lambda c: ex.exam(c, course, prof))
    key = {"e": exam}
    _refused(e, lambda c: ex.finalize(c, exam, prof), "positions 1..n")  # zero items
    q1, q2, q3 = _ok(e, lambda c: tuple(ex.approved_question(c, course, prof) for _ in range(3)))
    _ok(e, lambda c: (ex.item(c, exam, course, q1, 1), ex.item(c, exam, course, q2, 3)))
    _refused(e, lambda c: ex.finalize(c, exam, prof), "positions 1..n")  # a gap
    _ok(e, lambda c: c.execute(text("UPDATE exam_items SET position = 2 WHERE exam_id = :e AND question_id = :q"), {"e": exam, "q": q2}))
    _refused(e, lambda c: ex.finalize(c, exam, prof, pin=False), "all pinned")
    _refused(e, lambda c: ex.finalize(c, exam, prof, item_count=3), "matching totals|exams_state_check")
    _refused(e, lambda c: ex.finalize(c, exam, prof, total_points=99), "matching totals")
    _refused(e, lambda c: c.execute(text("UPDATE exams SET title = 'x' WHERE id = :e"), key), "increments its version")
    _refused(e, lambda c: c.execute(text("DELETE FROM exams WHERE id = :e"), key), "never deleted")
    _refused(e, lambda c: c.execute(text("UPDATE exams SET version = version + 1, course_id = :o WHERE id = :e"),
                                    {**key, "o": m9["other"]}), "identity")  # fmt: skip

    def finalize_with_draft_question(c):
        ex.item(c, exam, course, ex.bank_question(c, course, prof), 3)
        ex.finalize(c, exam, prof)

    _refused(e, finalize_with_draft_question, "exam_items_revision_fkey|all pinned|current approved revision")

    def pin_old_revision(c):  # q3 approved twice: pinning revision 1 is not its current approved revision
        c.execute(text("UPDATE questions SET status = 'draft', text = 'v2?', content_hash = repeat('e', 64), "
                       "version = version + 1 WHERE id = :q"), {"q": q3})  # fmt: skip
        ex.approve(c, q3, prof)
        ex.item(c, exam, course, q3, 3)
        ex.pin_items(c, exam)
        c.execute(text("UPDATE exam_items SET question_revision_no = 1 WHERE question_id = :q AND exam_id = :e"), {"q": q3, **key})
        ex.finalize(c, exam, prof, pin=False)

    _refused(e, pin_old_revision, "current approved revision")
    _ok(e, lambda c: ex.finalize(c, exam, prof))
    _refused(e, lambda c: c.execute(text("UPDATE exams SET status = 'draft', version = version + 1 WHERE id = :e"), key),
             "can only be archived|exams_state_check")  # fmt: skip
    _refused(e, lambda c: c.execute(text("UPDATE exams SET title = 'New', version = version + 1 WHERE id = :e"), key),
             "can only be archived")  # fmt: skip
    _ok(e, lambda c: ex.archive(c, exam))
    _refused(e, lambda c: c.execute(text("UPDATE exams SET version = version + 1 WHERE id = :e"), key), "archived exam is immutable")
    draft = _ok(e, lambda c: ex.exam(c, course, prof))
    _ok(e, lambda c: ex.archive(c, draft))
    _refused(e, lambda c: c.execute(text("UPDATE exams SET version = version + 1, status = 'finalized' WHERE id = :e"), {"e": draft}),
             "archived exam is immutable")  # fmt: skip


@pytest.fixture
def committed_exam(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    with e.begin() as c:
        exam = ex.exam(c, course, prof)
        q1, q2 = ex.approved_question(c, course, prof), ex.approved_question(c, course, prof)
        ex.item(c, exam, course, q1, 1)
    return {**m9, "exam": exam, "q2": q2}


def test_mig14_an_item_insert_in_flight_blocks_finalization(committed_exam):
    x = committed_exam
    e = x["engine"]
    with e.connect() as inserting:
        transaction = inserting.begin()
        ex.item(inserting, x["exam"], x["course"], x["q2"], 2)

        def finalize():
            with e.begin() as other:
                other.execute(text("SET LOCAL lock_timeout = '10s'"))
                other.execute(text(ex.FINALIZE), {"u": x["professor"], "n": 1, "t": 2, "chars": 10, "bs": "none",
                                                  "reason": None, "fp": ex.FINGERPRINT, "e": x["exam"]})  # fmt: skip

        ex.pin_items(inserting, x["exam"])
        error = _race(inserting, finalize, release=transaction.commit)
    assert error is not None and "matching totals" in str(error)


def test_mig14_finalization_in_flight_blocks_a_late_item_insert(committed_exam):
    x = committed_exam
    e = x["engine"]

    def late():
        with e.begin() as other:
            other.execute(text("SET LOCAL lock_timeout = '10s'"))
            ex.item(other, x["exam"], x["course"], x["q2"], 2)

    with e.connect() as finalizing:
        transaction = finalizing.begin()
        ex.finalize(finalizing, x["exam"], x["professor"])
        error = _race(finalizing, late, release=transaction.commit)
    assert error is not None and "while their exam is a draft" in str(error)
    with e.connect() as c:
        assert c.execute(text("SELECT count(*) FROM exam_items WHERE exam_id = :e"), {"e": x["exam"]}).scalar_one() == 1


def test_mig15_no_attempt_can_reach_an_exam_question(m9):
    e, course, prof, student = m9["engine"], m9["course"], m9["professor"], m9["student"]
    bank = _ok(e, lambda c: ex.bank_question(c, course, prof))
    legacy_exam = _ok(e, lambda c: ex.legacy_question(c, course, purpose="exam"))
    practice = _ok(e, lambda c: ex.legacy_question(c, course, purpose="practice"))
    insert = "INSERT INTO attempts (user_id, question_id, course_id, is_correct, hint_count) VALUES (:u, :q, :c, true, 0) RETURNING id"
    for question in (bank, legacy_exam):
        _refused(e, lambda c, q=question: c.execute(text(insert), {"u": student, "q": q, "c": course}), "cannot be attempted")
    attempt = _ok(e, lambda c: c.execute(text(insert), {"u": student, "q": practice, "c": course}).scalar_one())
    _refused(e, lambda c: c.execute(text("UPDATE attempts SET question_id = :q WHERE id = :a"), {"q": legacy_exam, "a": attempt}),
             "cannot be attempted")  # fmt: skip
    _ok(e, lambda c: c.execute(text("UPDATE attempts SET response_ms = 5 WHERE id = :a"), {"a": attempt}))
    _ok(e, lambda c: c.execute(text("DELETE FROM attempts WHERE id = :a"), {"a": attempt}))


def test_mig16_m7_and_m8_rules_are_unchanged(m9):
    e, course, topic, student, prof = m9["engine"], m9["course"], m9["topic"], m9["student"], m9["professor"]
    event = graded_attempt(e, student, course, topic)  # M7 grading path + M8 outbox trigger
    with e.connect() as c:
        assert c.execute(text("SELECT count(*) FROM mastery_outbox WHERE event_id = :e"), {"e": event}).scalar_one() == 1
        generated = c.execute(text("SELECT question_id FROM attempts WHERE id = :a"), {"a": event}).scalar_one()

    def generated_exam_question(c):
        c.execute(text("UPDATE questions SET purpose = 'exam' WHERE id = :q"), {"q": generated})

    _refused(e, generated_exam_question, "generated questions are immutable")

    def new_generated_with_exam_purpose(c):
        session = c.execute(text("SELECT session_id FROM quiz_items WHERE question_id = :q"), {"q": generated}).scalar_one()
        c.execute(text("INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, choices, explanation, "
                       "citations, status, hint, answer_key, grading_method, generated_by_session_id, generated_for_user_id, "
                       "generation, content_hash) SELECT course_id, topic_id, 'exam', qtype, difficulty, text, choices, "
                       "explanation, citations, status, hint, answer_key, grading_method, :s, generated_for_user_id, "
                       "generation, content_hash FROM questions WHERE id = :q"), {"s": session, "q": generated})  # fmt: skip

    _refused(e, new_generated_with_exam_purpose, "questions_generated_check|generating quiz session")
    bank = _ok(e, lambda c: ex.approved_question(c, course, prof, topic_id=topic))

    def assign_bank_question(c):
        session = c.execute(text("SELECT session_id FROM quiz_items WHERE question_id = :q"), {"q": generated}).scalar_one()
        c.execute(text("INSERT INTO quiz_items (session_id, user_id, course_id, question_id, topic_id, position) "
                       "VALUES (:s, :u, :c, :q, :t, 2)"), {"s": session, "u": student, "c": course, "q": bank, "t": topic})  # fmt: skip

    _refused(e, assign_bank_question, "quiz_items_question_fkey|generating quiz session|quiz_items_generated_question_fkey")


def test_mig17_audit_events_are_append_only_and_idempotency_keys_unique(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    insert = ("INSERT INTO exam_audit_events (course_id, actor_id, entity_kind, entity_id, action, details) "
              "VALUES (:c, :u, 'exam', gen_random_uuid(), :a, CAST(:d AS jsonb)) RETURNING id")  # fmt: skip
    event = _ok(e, lambda c: c.execute(text(insert), {"c": course, "u": prof, "a": "exam_created", "d": "{}"}).scalar_one())
    _refused(e, lambda c: c.execute(text("UPDATE exam_audit_events SET details = '{}'::jsonb WHERE id = :i"), {"i": event}), "append-only")
    _refused(e, lambda c: c.execute(text("DELETE FROM exam_audit_events WHERE id = :i"), {"i": event}), "append-only")
    for action, details in (("exam_deleted", "{}"), ("exam_created", "[]"), ("exam_created", json.dumps({"x": "y" * 5000}))):
        _refused(e, lambda c, a=action, d=details: c.execute(text(insert), {"c": course, "u": prof, "a": a, "d": d}),
                 "exam_audit_events_shape_check")  # fmt: skip
    key = uuid.uuid4()
    insert_key = ("INSERT INTO exam_idempotency_keys (user_id, idempotency_key, operation, request_fingerprint, course_id, "
                  "resource_id) VALUES (:u, :k, :op, repeat('a', 64), :c, gen_random_uuid())")  # fmt: skip
    _ok(e, lambda c: c.execute(text(insert_key), {"u": prof, "k": key, "op": "exam_create", "c": course}))
    _refused(e, lambda c: c.execute(text(insert_key), {"u": prof, "k": key, "op": "question_create", "c": course}),
             "exam_idempotency_keys_pkey")  # fmt: skip
    _refused(e, lambda c: c.execute(text(insert_key), {"u": prof, "k": uuid.uuid4(), "op": "x", "c": course}), "shape_check")


def test_mig18_workers_require_current_revision():
    from app.ingestion import runtime

    assert runtime.REQUIRED_SCHEMA_REVISION == "0013"


def test_mig19_true_false_rows_and_revisions_are_canonical(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    for choices in (None, list(reversed(ex.TF_CHOICES)), [{"id": "true", "text": "Yes"}, {"id": "false", "text": "No"}],
                    ex.TF_CHOICES[:1], []):  # fmt: skip
        _refused(e, lambda c, ch=choices: ex.bank_question(c, course, prof, qtype="true_false", choices=ch), "questions_bank_check")
    q = _ok(e, lambda c: ex.bank_question(c, course, prof, qtype="true_false", answer_key={"choice_id": "false"}))

    def non_canonical_revision(c):
        values = ex.revision_values(c, q, prof)
        c.execute(text("ALTER TABLE exam_question_revisions DISABLE TRIGGER exam_question_revisions_guard"))
        c.execute(text(ex.INSERT_REVISION), {**values, "choices": json.dumps(list(reversed(ex.TF_CHOICES)))})

    _refused(e, non_canonical_revision, "exam_question_revisions_content_check")
    _ok(e, lambda c: ex.approve(c, q, prof))


def test_mig20_revision_allocation_is_serialized_gapless_and_a_copy_of_the_working_copy(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    q = _ok(e, lambda c: ex.bank_question(c, course, prof, citations=[{"document_id": str(uuid.uuid4()), "locator": {"page": 2}}]))
    for change, match in (({"content_sha256": "f" * 64}, "exact copy"), ({"stem": "Other?"}, "exact copy"),
                          ({"source_refs": json.dumps([])}, "exact copy"), ({"revision_no": 3}, "contiguous"),
                          ({"explanation": "x"}, "exact copy"), ({"difficulty": "hard"}, "exact copy")):  # fmt: skip
        _refused(e, lambda c, ch=change: c.execute(text(ex.INSERT_REVISION), {**ex.revision_values(c, q, prof), **ch}), match)

    def other_locator(c):
        values = ex.revision_values(c, q, prof)
        refs = json.loads(values["source_refs"])
        refs[0]["locator"] = {"page": 3}
        c.execute(text(ex.INSERT_REVISION), {**values, "source_refs": json.dumps(refs)})

    _refused(e, other_locator, "exact copy")
    with e.connect() as first:
        transaction = first.begin()
        ex.approve(first, q, prof)  # revision 1, question approved, both uncommitted

        def second():
            with e.begin() as other:
                other.execute(text("SET LOCAL lock_timeout = '10s'"))
                other.execute(text(ex.INSERT_REVISION), ex.revision_values(other, q, prof, revision_no=1))

        error = _race(first, second, release=transaction.commit)
    assert error is not None and ("only approve a draft" in str(error) or "exam_question_revisions_pkey" in str(error))
    with e.connect() as c:
        assert c.execute(text("SELECT array_agg(revision_no) FROM exam_question_revisions WHERE question_id = :q"),
                         {"q": q}).scalar_one() == [1]  # fmt: skip


def _blueprint_exam(c, m9, *, rows=((2, {"qtype": "mcq"}),), items=None):
    course, prof = m9["course"], m9["professor"]
    exam = ex.exam(c, course, prof)
    row_ids = [ex.blueprint_row(c, exam, course, i + 1, count=n, **crit) for i, (n, crit) in enumerate(rows)]
    position = 0
    for row_index, specs in (items or {}).items():
        for spec in specs:
            position += 1
            question = ex.approved_question(c, course, prof, **spec)
            ex.item(c, exam, course, question, position, row=None if row_index is None else row_ids[row_index])
    return exam, row_ids


def test_mig21_blueprint_decision_is_verified_against_pinned_revisions(m9):
    e, prof = m9["engine"], m9["professor"]
    mcq = {"qtype": "mcq"}
    cases = {
        "underfilled": {0: [mcq]},
        "overfilled": {0: [mcq, mcq, mcq]},
        "unassigned": {0: [mcq, mcq], None: [mcq]},
        "criteria": {0: [mcq, {"qtype": "short", "choices": None, "answer_key": {"model_answer": "m", "rubric": None}}]},
    }
    for name, items in cases.items():
        def attempt(c, items=items):
            exam, _ = _blueprint_exam(c, m9, items=items)
            ex.finalize(c, exam, prof, blueprint_status="fulfilled")

        _refused(e, attempt, "blueprint status fulfilled requires")

        def deviation(c, items=items):
            exam, _ = _blueprint_exam(c, m9, items=items)
            ex.finalize(c, exam, prof, blueprint_status="deviation_accepted", reason="Lab cancelled this week.")
            return exam

        assert _ok(e, deviation)

    def none_with_rows(c):
        exam, _ = _blueprint_exam(c, m9, items={0: [mcq, mcq]})
        ex.finalize(c, exam, prof, blueprint_status="none")

    _refused(e, none_with_rows, "requires an exam without blueprint rows")

    def fulfilled(c):
        exam, _ = _blueprint_exam(c, m9, items={0: [mcq, mcq]})
        ex.finalize(c, exam, prof, blueprint_status="fulfilled")

    _ok(e, fulfilled)

    def deviation_when_fulfilled(c):
        exam, _ = _blueprint_exam(c, m9, items={0: [mcq, mcq]})
        ex.finalize(c, exam, prof, blueprint_status="deviation_accepted", reason="Not really a deviation.")

    _refused(e, deviation_when_fulfilled, "not fulfilled")

    def deviation_without_reason(c):
        exam, _ = _blueprint_exam(c, m9, items={0: [mcq]})
        ex.finalize(c, exam, prof, blueprint_status="deviation_accepted", reason=None)

    _refused(e, deviation_without_reason, "exams_state_check")

    def reason_with_other_status(c):
        exam, _ = _blueprint_exam(c, m9, items={0: [mcq, mcq]})
        ex.finalize(c, exam, prof, blueprint_status="fulfilled", reason="A reason without a deviation.")

    _refused(e, reason_with_other_status, "exams_state_check")

    def short_reason(c):
        exam, _ = _blueprint_exam(c, m9, items={0: [mcq]})
        ex.finalize(c, exam, prof, blueprint_status="deviation_accepted", reason="short")

    _refused(e, short_reason, "exams_state_check")

    def edited_after_assignment(c):  # the row criterion is checked against the PINNED revision
        exam, _ = _blueprint_exam(c, m9, rows=((1, {"difficulty": "medium"}),), items={0: [{}]})
        question = c.execute(text("SELECT question_id FROM exam_items WHERE exam_id = :e"), {"e": exam}).scalar_one()
        c.execute(text("UPDATE questions SET status = 'draft', difficulty = 'hard', content_hash = repeat('9', 64), "
                       "version = version + 1 WHERE id = :q"), {"q": question})  # fmt: skip
        ex.approve(c, question, prof)
        ex.finalize(c, exam, prof, blueprint_status="fulfilled")

    _refused(e, edited_after_assignment, "blueprint status fulfilled requires")


def test_mig22_finalized_version_and_archival_keep_the_frozen_columns(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    exam = _ok(e, lambda c: ex.exam(c, course, prof))
    _ok(e, lambda c: (ex.item(c, exam, course, ex.approved_question(c, course, prof), 1), ex.bump(c, exam)))

    def wrong_finalized_version(c):
        ex.pin_items(c, exam)
        c.execute(text(ex.FINALIZE.replace("finalized_version = version + 1", "finalized_version = version")),
                  {"u": prof, "n": 1, "t": 2, "chars": 10, "bs": "none", "reason": None, "fp": ex.FINGERPRINT, "e": exam})  # fmt: skip

    _refused(e, wrong_finalized_version, "finalized_version must be")
    for chars in (0, 300001):
        _refused(e, lambda c, n=chars: ex.finalize(c, exam, prof, chars=n), "exams_state_check")
    _ok(e, lambda c: ex.finalize(c, exam, prof, chars=300000))
    columns = ("finalized_version, content_sha256, content_chars, blueprint_status, finalized_at, course_code_snapshot, "
               "finalize_preview_fingerprint, item_count, total_points")  # fmt: skip
    with e.connect() as c:
        frozen = c.execute(text(f"SELECT {columns}, version FROM exams WHERE id = :e"), {"e": exam}).one()
    assert frozen.finalized_version == frozen.version == 3
    for change in ("content_sha256 = repeat('c', 64)", "content_chars = 5", "finalized_version = 4", "blueprint_status = 'fulfilled'",
                   "course_code_snapshot = 'X'"):  # fmt: skip
        _refused(e, lambda c, ch=change: c.execute(text(f"UPDATE exams SET status = 'archived', archived_at = now(), {ch}, "
                                                        "version = version + 1 WHERE id = :e"), {"e": exam}), "can only be archived")  # fmt: skip
    _ok(e, lambda c: ex.archive(c, exam))
    with e.connect() as c:
        after = c.execute(text(f"SELECT {columns}, version, status FROM exams WHERE id = :e"), {"e": exam}).one()
    assert tuple(after)[:-2] == tuple(frozen)[:-1] and after.version == frozen.version + 1 and after.status == "archived"


@pytest.mark.parametrize("table", ["exams", "exam_items", "exam_question_revisions", "exam_audit_events"])
def test_mig23_truncate_is_refused(m9, table):
    _refused(m9["engine"], lambda c: c.execute(text(f"TRUNCATE {table} CASCADE")), "TRUNCATE of")


def test_mig24_position_constraints_are_deferred_and_row_keys_exam_scoped(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    with e.connect() as c:
        rows = c.execute(text(
            "SELECT conname, condeferrable, condeferred FROM pg_constraint WHERE conname IN "
            "('exam_items_exam_id_position_key', 'exam_blueprint_rows_exam_id_position_key', "
            "'exam_blueprint_rows_id_exam_id_key', 'exams_id_course_id_key')"
        )).all()  # fmt: skip
    assert {r.conname: (r.condeferrable, r.condeferred) for r in rows} == {
        "exam_items_exam_id_position_key": (True, True),
        "exam_blueprint_rows_exam_id_position_key": (True, True),
        "exam_blueprint_rows_id_exam_id_key": (False, False),
        "exams_id_course_id_key": (False, False),
    }
    exam = _ok(e, lambda c: ex.exam(c, course, prof))
    other_exam = _ok(e, lambda c: ex.exam(c, course, prof))
    ids = _ok(e, lambda c: [ex.blueprint_row(c, exam, course, p) for p in range(1, 6)])

    def swap(c):
        c.execute(text("UPDATE exam_blueprint_rows SET position = 2 WHERE id = :a"), {"a": ids[0]})
        c.execute(text("UPDATE exam_blueprint_rows SET position = 1 WHERE id = :b"), {"b": ids[1]})

    _ok(e, swap)

    def reverse(c):
        for index, row in enumerate(ids):
            c.execute(text("UPDATE exam_blueprint_rows SET position = :p WHERE id = :r"), {"p": 5 - index, "r": row})

    _ok(e, reverse)
    with e.connect() as c:
        assert [r for r in c.execute(text("SELECT id FROM exam_blueprint_rows WHERE exam_id = :e ORDER BY position"),
                                     {"e": exam}).scalars()] == list(reversed(ids))  # fmt: skip
    _refused(e, lambda c: c.execute(text("UPDATE exam_blueprint_rows SET position = 1 WHERE id = :r"), {"r": ids[0]}),
             "exam_blueprint_rows_exam_id_position_key")  # persisting duplicate, at commit

    def immediate(c):
        c.execute(text("UPDATE exam_blueprint_rows SET position = 1 WHERE id = :r"), {"r": ids[0]})
        c.execute(text("SET CONSTRAINTS exam_blueprint_rows_exam_id_position_key IMMEDIATE"))

    _refused(e, immediate, "exam_blueprint_rows_exam_id_position_key")
    _refused(e, lambda c: c.execute(text("UPDATE exam_blueprint_rows SET exam_id = :x WHERE id = :r"), {"x": other_exam, "r": ids[0]}),
             "identity of a blueprint row")  # fmt: skip
    _refused(e, lambda c: c.execute(text("UPDATE exam_blueprint_rows SET course_id = :o WHERE id = :r"), {"o": m9["other"], "r": ids[0]}),
             "identity of a blueprint row")  # fmt: skip
    question = _ok(e, lambda c: ex.approved_question(c, course, prof))

    def transient_cross_exam(c):  # the row key is checked per statement, not at commit
        item = ex.item(c, other_exam, course, question, 1)
        c.execute(text("UPDATE exam_items SET blueprint_row_id = :r WHERE id = :i"), {"r": ids[0], "i": item})
        c.execute(text("UPDATE exam_items SET blueprint_row_id = NULL WHERE id = :i"), {"i": item})

    _refused(e, transient_cross_exam, "exam_items_blueprint_row_fkey")
    _refused(e, lambda c: c.execute(text("INSERT INTO exam_blueprint_rows (exam_id, course_id, position, question_count, points_each, "
                                         "topic_id) VALUES (:e, :c, 9, 1, 1, :t)"), {"e": exam, "c": course, "t": m9["other_topic"]}),
             "exam_blueprint_rows_topic_fkey")  # fmt: skip
    for values in ("51, 1, 1", "1, 0, 1", "1, 201, 1", "1, 1, 0", "1, 1, 101"):
        _refused(e, lambda c, v=values: c.execute(text(f"INSERT INTO exam_blueprint_rows (exam_id, course_id, position, "
                                                       f"question_count, points_each) VALUES (:e, :c, {v})"), {"e": exam, "c": course}),
                 "exam_blueprint_rows_values_check")  # fmt: skip


def test_mig25_finalize_preview_fingerprint_is_formatted_present_exactly_when_finalized_and_frozen(m9):
    e, course, prof = m9["engine"], m9["course"], m9["professor"]
    exam = _ok(e, lambda c: ex.exam(c, course, prof))
    _refused(e, lambda c: c.execute(text("UPDATE exams SET finalize_preview_fingerprint = :f, version = version + 1 WHERE id = :e"),
                                    {"f": ex.FINGERPRINT, "e": exam}), "exams_state_check")  # fmt: skip
    _ok(e, lambda c: ex.item(c, exam, course, ex.approved_question(c, course, prof), 1))
    for bad in ("pf2:" + "0" * 64, "pf1:" + "A" * 64, "pf1:" + "0" * 63, None):
        _refused(e, lambda c, f=bad: ex.finalize(c, exam, prof, fingerprint=f), "exams_fingerprint_check|exams_state_check")
    _ok(e, lambda c: ex.finalize(c, exam, prof))
    change = "UPDATE exams SET finalize_preview_fingerprint = 'pf1:' || repeat('1', 64), version = version + 1{} WHERE id = :e"
    _refused(e, lambda c: c.execute(text(change.format("")), {"e": exam}), "can only be archived")
    _refused(e, lambda c: c.execute(text(change.format(", status = 'archived', archived_at = now()")), {"e": exam}),
             "can only be archived")  # fmt: skip
    _ok(e, lambda c: ex.archive(c, exam))
    with e.connect() as c:
        assert c.execute(text("SELECT finalize_preview_fingerprint FROM exams WHERE id = :e"), {"e": exam}).scalar_one() == ex.FINGERPRINT
