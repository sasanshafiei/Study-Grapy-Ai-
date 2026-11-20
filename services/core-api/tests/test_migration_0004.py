"""Revision 0004: courses.code and the M5 lookup indexes.

Database tests run only through the guarded fixtures (disposable database);
the offline-SQL tests need no database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.alembic_cli import BASELINE_REVISION, run_alembic
from tests.test_migration_0002 import _base_rows, _run_script
from tests.test_migration_0003 import (
    _columns,
    _failed,
    _m2_dataset,
    scratch3,  # noqa: F401  (fixture used by scratch4)
)
from tests.test_migrations import (
    BASELINE_TABLES,
    _current,
    _relation_oids,
    _scaffold_snapshot,
    _snapshot,
    _table_contents,
)

REVISION = "0004"
PREVIOUS = "0003"
NEW_INDEXES = {"courses_owner_id_idx", "enrollments_course_id_idx", "topics_course_id_idx"}
UNCHANGED_TABLES = BASELINE_TABLES - {"courses"} | {"ingestion_jobs", "document_sections"}


@pytest.fixture
def scratch4(scratch3, disposable_engine, committed_transaction):  # noqa: F811 (the imported fixture)
    """scratch3 (empty database at base before and after) plus removal of course codes."""
    yield
    with committed_transaction() as connection:
        has_code = connection.execute(
            text("SELECT count(*) FROM information_schema.columns WHERE table_name = 'courses' AND column_name = 'code'")
        ).scalar_one()
        if has_code:
            connection.execute(text("UPDATE courses SET code = ''"))


def _indexes(engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")).scalars())


def _course_rows(engine) -> list[tuple]:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT id, owner_id, name, join_code, created_at FROM courses ORDER BY id")
        ).all()


def test_upgrade_keeps_every_row_and_gives_legacy_courses_the_empty_code(
    scratch4, disposable_engine, alembic, committed_transaction
):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        _m2_dataset(connection)
    courses_before = _course_rows(disposable_engine)
    contents = _table_contents(disposable_engine, UNCHANGED_TABLES)
    oids = _relation_oids(disposable_engine)
    assert len(courses_before) >= 1

    alembic("upgrade", REVISION)

    assert _course_rows(disposable_engine) == courses_before
    assert _table_contents(disposable_engine, UNCHANGED_TABLES) == contents
    with disposable_engine.connect() as connection:
        assert set(connection.execute(text("SELECT code FROM courses")).scalars()) == {""}
    new_oids = _relation_oids(disposable_engine)
    assert all(new_oids[name] == oid for name, oid in oids.items())  # nothing was dropped and re-created
    assert NEW_INDEXES <= _indexes(disposable_engine)
    columns = {name: (dtype, nullable, default) for name, dtype, nullable, default in _columns(disposable_engine, "courses")}
    assert columns["code"] == ("character varying", "NO", "''::character varying")
    assert _current(alembic).split()[0] == REVISION


@pytest.mark.parametrize("value", [" padded", "trailing ", "tab\there", "bell\x07"])
def test_course_code_must_be_trimmed_and_printable(head_database, rolled_back_transaction, value):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        with pytest.raises(IntegrityError, match="courses_code_check"):
            connection.execute(text("UPDATE courses SET code = :v WHERE id = :c"), {"v": value, "c": ids["course"]})


def test_course_code_rejects_null_and_long_values(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        connection.execute(text("UPDATE courses SET code = 'CHEM 101' WHERE id = :c"), {"c": ids["course"]})
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE courses SET code = NULL WHERE id = :c"), {"c": ids["course"]})
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        with pytest.raises(Exception, match="too long"):
            connection.execute(text("UPDATE courses SET code = :v WHERE id = :c"), {"v": "X" * 33, "c": ids["course"]})


def test_downgrade_without_codes_restores_0003_exactly_and_upgrades_again(
    scratch4, disposable_engine, alembic, committed_transaction
):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        _m2_dataset(connection)
    snapshot_0003 = _snapshot(disposable_engine)
    contents = _table_contents(disposable_engine, UNCHANGED_TABLES | {"courses"})
    alembic("upgrade", REVISION)

    result = alembic("downgrade", PREVIOUS)

    assert f"Running downgrade {REVISION} -> {PREVIOUS}" in result.stderr
    assert _snapshot(disposable_engine) == snapshot_0003
    assert _table_contents(disposable_engine, UNCHANGED_TABLES | {"courses"}) == contents
    alembic("upgrade", "head")
    assert _current(alembic).endswith("(head)")


def test_downgrade_refuses_while_a_course_code_exists(
    scratch4, disposable_engine, alembic, alembic_env, committed_transaction
):
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        connection.execute(text("UPDATE courses SET code = 'CHEM 101' WHERE id = :c"), {"c": ids["course"]})
    before = _snapshot(disposable_engine)

    for target in (PREVIOUS, BASELINE_REVISION, "base"):
        output = _failed(run_alembic("downgrade", target, env=alembic_env))
        assert "Downgrade below 0004 refused before any change. 1 courses have a course code" in output

    assert _snapshot(disposable_engine) == before
    assert _current(alembic).split()[0] == REVISION
    with disposable_engine.connect() as connection:
        assert connection.execute(text("SELECT code FROM courses WHERE id = :c"), {"c": ids["course"]}).scalar_one() == "CHEM 101"


def _offline(*args: str) -> str:
    result = run_alembic(*args, "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_offline_upgrade_sql_locks_before_any_change():
    sql = _offline("upgrade", f"{PREVIOUS}:{REVISION}")
    lock = sql.index("LOCK TABLE courses, enrollments, topics IN SHARE ROW EXCLUSIVE MODE")
    assert lock < sql.index("ALTER TABLE courses ADD COLUMN code VARCHAR(32) DEFAULT ''::character varying NOT NULL")
    for name in NEW_INDEXES:
        assert f"CREATE INDEX {name}" in sql
    assert "courses_code_check" in sql


def test_offline_downgrade_sql_carries_the_guard():
    sql = _offline("downgrade", f"{REVISION}:{PREVIOUS}")
    guard = sql.index("DO $migration_0004_downgrade_guard$")
    assert guard < sql.index("ALTER TABLE courses DROP COLUMN code")
    assert "LOCK TABLE courses IN ACCESS EXCLUSIVE MODE" in sql


def test_offline_downgrade_sql_enforces_the_guard_when_executed(
    scratch4, disposable_engine, alembic, committed_transaction
):
    sql = _offline("downgrade", f"{REVISION}:{PREVIOUS}")
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        connection.execute(text("UPDATE courses SET code = 'X1' WHERE id = :c"), {"c": ids["course"]})
    before = _snapshot(disposable_engine)
    with pytest.raises(Exception, match="Downgrade below 0004 refused"):
        with committed_transaction() as connection:
            _run_script(connection, sql)
    assert _snapshot(disposable_engine) == before


def test_historical_baseline_is_untouched_by_0004(scratch4, disposable_engine, alembic):
    alembic("upgrade", "head")
    alembic("downgrade", BASELINE_REVISION)
    assert _snapshot(disposable_engine) == _scaffold_snapshot()
