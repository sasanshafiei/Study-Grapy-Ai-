"""Revision 0003: document_sections, job scheduling/fencing columns, storage_identity.

Database tests run only through the guarded fixtures (disposable database);
the offline-SQL tests need no database. Tests that move the database between
revisions use `scratch3`, which removes any M2/M3 state the downgrade guards
would (rightly) refuse to drop, and leaves the database empty at base.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.alembic_cli import BASELINE_REVISION, run_alembic
from tests.test_migration_0002 import _base_rows, _job, _run_script, _stored
from tests.test_migrations import (
    BASELINE_TABLES,
    _current,
    _insert_full_baseline_dataset,
    _relation_oids,
    _reset_to_base,
    _scaffold_snapshot,
    _snapshot,
    _table_contents,
    _tables,
)

REVISION = "0003"
PREVIOUS = "0002"
M2_TABLES = BASELINE_TABLES | {"ingestion_jobs"}
NEW_JOB_COLUMNS = ("attempt_token", "lease_expires_at", "available_at", "last_dispatched_at")


@pytest.fixture
def scratch3(disposable_engine, alembic, committed_transaction):
    _reset_to_base(disposable_engine, alembic)
    yield
    with committed_transaction() as connection:
        for table in ("document_sections", "storage_identity"):
            if connection.execute(text(f"SELECT to_regclass('public.{table}') IS NOT NULL")).scalar_one():
                connection.execute(text(f"DELETE FROM {table}"))
        if connection.execute(text("SELECT to_regclass('public.ingestion_jobs') IS NOT NULL")).scalar_one():
            connection.execute(text("DELETE FROM ingestion_jobs"))
            connection.execute(text("DELETE FROM documents WHERE storage_key IS NOT NULL"))
            connection.execute(
                text(
                    "UPDATE documents SET uploaded_by = NULL, original_filename = NULL, storage_key = NULL, "
                    "mime_type = NULL, size_bytes = NULL, page_count = NULL, kind = NULL, error_message = NULL"
                )
            )
    _reset_to_base(disposable_engine, alembic)


def _failed(result) -> str:
    assert result.returncode != 0, "the command was expected to fail"
    return result.stdout + result.stderr


def _columns(engine, table: str) -> list[tuple]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position"
            ),
            {"t": table},
        ).all()


def _m2_dataset(connection) -> dict:
    """Representative M2 state: legacy rows, stored uploads, queued/failed/succeeded jobs."""
    _insert_full_baseline_dataset(connection)
    ids = _base_rows(connection)
    queued_doc = _stored(connection, ids, content_hash="aa" * 32)
    failed_doc = _stored(connection, ids, content_hash="bb" * 32, status="failed", error_message="This PDF is encrypted.")
    retried_doc = _stored(connection, ids, content_hash="cc" * 32)
    jobs = {
        "queued": _job(connection, queued_doc),
        "failed": _job(connection, failed_doc, status="failed", attempt_count=3, started_at="2026-11-19T12:00:00Z",
                       finished_at="2026-11-19T12:05:00Z", error_message="parser crashed"),
        "requeued": _job(connection, retried_doc, attempt_count=1, started_at="2026-11-19T12:00:00Z"),
    }  # fmt: skip
    return {"ids": ids, "jobs": jobs, "documents": [queued_doc, failed_doc, retried_doc]}


# --- upgrade ---------------------------------------------------------------------------------


def test_upgrade_from_0002_keeps_every_row_and_makes_queued_jobs_dispatchable(
    scratch3, disposable_engine, alembic, committed_transaction
):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        data = _m2_dataset(connection)
    contents_before = _table_contents(disposable_engine, M2_TABLES)
    oids_before = _relation_oids(disposable_engine)

    result = alembic("upgrade", REVISION)

    assert f"Running upgrade {PREVIOUS} -> {REVISION}" in result.stderr
    oids_after = _relation_oids(disposable_engine)
    assert {name: oids_after[name] for name in oids_before} == oids_before  # nothing rebuilt
    assert _table_contents(disposable_engine, M2_TABLES - {"ingestion_jobs"}) == {
        k: v for k, v in contents_before.items() if k != "ingestion_jobs"
    }
    with disposable_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, status, attempt_count, error_message, attempt_token, lease_expires_at, "
                "last_dispatched_at, available_at, created_at, now() AS migrated_at FROM ingestion_jobs"
            )
        ).mappings().all()
        assert connection.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM storage_identity")).scalar_one() == 0
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == set(data["jobs"].values())
    for row in rows:
        assert (row["attempt_token"], row["lease_expires_at"], row["last_dispatched_at"]) == (None, None, None)
        assert row["available_at"] <= row["migrated_at"]  # eligible at once (the migration's own time)
        assert row["available_at"] >= row["created_at"]
    assert by_id[data["jobs"]["failed"]]["error_message"] == "parser crashed"
    assert by_id[data["jobs"]["requeued"]]["attempt_count"] == 1


def test_fresh_upgrade_to_head_and_new_columns(scratch3, disposable_engine, alembic):
    alembic("upgrade", "head")
    assert _current(alembic) == f"{REVISION} (head)"
    columns = {name: (dtype, nullable, default) for name, dtype, nullable, default in _columns(disposable_engine, "ingestion_jobs")}
    assert columns["attempt_token"] == ("uuid", "YES", None)
    assert columns["lease_expires_at"] == ("timestamp with time zone", "YES", None)
    assert columns["available_at"] == ("timestamp with time zone", "NO", "now()")
    assert columns["last_dispatched_at"] == ("timestamp with time zone", "YES", None)
    assert [c[0] for c in _columns(disposable_engine, "document_sections")] == [
        "id", "document_id", "ordinal", "text", "locator", "created_at"
    ]  # fmt: skip
    assert [c[0] for c in _columns(disposable_engine, "storage_identity")] == ["id", "storage_id", "created_at"]


def test_upgrade_stops_before_any_change_while_a_job_is_running(
    scratch3, disposable_engine, alembic, alembic_env, committed_transaction
):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        doc = _stored(connection, ids)
        connection.execute(
            text(
                "INSERT INTO ingestion_jobs (document_id, kind, status, attempt_count, started_at) "
                "VALUES (:d, 'parse', 'running', 1, now())"
            ),
            {"d": doc},
        )
    before = _snapshot(disposable_engine)

    output = _failed(run_alembic("upgrade", REVISION, env=alembic_env))

    assert "Migration 0003 stopped before any change. 1 ingestion_jobs rows are running" in output
    assert _snapshot(disposable_engine) == before
    assert _current(alembic).split()[0] == PREVIOUS
    with committed_transaction() as connection:  # the documented way forward
        connection.execute(text("UPDATE ingestion_jobs SET status = 'queued'"))
    alembic("upgrade", REVISION)


# --- constraints at head -----------------------------------------------------------------------


def _section(connection, document_id, ordinal=0, text_value="Text", locator=None):
    locator = {"page": 1} if locator is None else locator
    return connection.execute(
        text(
            "INSERT INTO document_sections (document_id, ordinal, text, locator) "
            "VALUES (:d, :o, :t, CAST(:l AS jsonb)) RETURNING id"
        ),
        {"d": document_id, "o": ordinal, "t": text_value, "l": locator if isinstance(locator, str) else json.dumps(locator)},
    ).scalar_one()


def _violation(rolled_back_transaction, action) -> IntegrityError:
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        document = _stored(connection, ids)
        with pytest.raises(IntegrityError) as caught:
            action(connection, document)
    return caught.value


@pytest.mark.parametrize(
    "locator",
    [
        "{}", '{"page": 0}', '{"page": -1}', '{"page": 1.5}', '{"page": "1"}', '{"page": null}',
        '{"page": 2147483648}', '{"page": 1, "line": 3}', '{"heading": "4.4"}', "[1]", '"page"', "1", "null",
    ],
)  # fmt: skip
def test_locator_must_be_exactly_a_positive_page(head_database, rolled_back_transaction, locator):
    error = _violation(rolled_back_transaction, lambda c, d: _section(c, d, locator=locator))
    assert error.orig.diag.constraint_name == "document_sections_locator_check"


@pytest.mark.parametrize("value", ["", " ", " \n\t\r "])
def test_section_text_must_not_be_blank(head_database, rolled_back_transaction, value):
    error = _violation(rolled_back_transaction, lambda c, d: _section(c, d, text_value=value))
    assert error.orig.diag.constraint_name == "document_sections_text_check"


def test_ordinal_is_non_negative_and_unique_per_document(head_database, rolled_back_transaction):
    error = _violation(rolled_back_transaction, lambda c, d: _section(c, d, ordinal=-1))
    assert error.orig.diag.constraint_name == "document_sections_ordinal_check"
    error = _violation(rolled_back_transaction, lambda c, d: (_section(c, d, 0), _section(c, d, 0, "Again")))
    assert error.orig.diag.constraint_name == "document_sections_document_id_ordinal_key"
    with rolled_back_transaction() as connection:  # another document may reuse the ordinal
        ids = _base_rows(connection)
        a, b = _stored(connection, ids, content_hash="aa" * 32), _stored(connection, ids, content_hash="bb" * 32)
        _section(connection, a, 0)
        _section(connection, b, 0)


@pytest.mark.parametrize("column", ["document_id", "ordinal", "text", "locator"])
def test_section_columns_reject_null(head_database, rolled_back_transaction, column):
    def insert(connection, document):
        values = {"document_id": document, "ordinal": 0, "text": "T", "locator": '{"page": 1}'}
        values[column] = None
        connection.execute(
            text(
                "INSERT INTO document_sections (document_id, ordinal, text, locator) "
                "VALUES (:document_id, :ordinal, :text, CAST(:locator AS jsonb))"
            ),
            values,
        )

    assert _violation(rolled_back_transaction, insert).orig.sqlstate == "23502"


def test_valid_sections_and_ordered_retrieval_use_the_unique_index(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        document = _stored(connection, ids)
        for ordinal, page in ((2, 9), (0, 1), (1, 4)):
            _section(connection, document, ordinal, f"Page {page} → H₂O", {"page": page})
        rows = connection.execute(
            text("SELECT ordinal, locator FROM document_sections WHERE document_id = :d ORDER BY ordinal"),
            {"d": document},
        ).all()
        assert [(r.ordinal, r.locator) for r in rows] == [(0, {"page": 1}), (1, {"page": 4}), (2, {"page": 9})]
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            connection.execute(
                text("EXPLAIN SELECT * FROM document_sections WHERE document_id = :d ORDER BY ordinal"), {"d": document}
            ).scalars()
        )
        assert "document_sections_document_id_ordinal_key" in plan


def test_sections_go_with_their_document(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        document = _stored(connection, ids)
        _section(connection, document)
        connection.execute(text("DELETE FROM documents WHERE id = :d"), {"d": document})
        assert connection.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 0
    error = _violation(rolled_back_transaction, lambda c, d: _section(c, uuid.uuid4()))
    assert error.orig.diag.constraint_name == "document_sections_document_id_fkey"


@pytest.mark.parametrize(
    "columns",
    [
        {"attempt_token": None},
        {"lease_expires_at": None},
        {"lease_expires_at": "2026-11-19T11:00:00Z"},  # before the attempt started
    ],
)
def test_running_job_needs_its_token_and_a_lease_after_its_start(head_database, rolled_back_transaction, columns):
    values = {"status": "running", "attempt_count": 1, "started_at": "2026-11-19T12:00:00Z",
              "attempt_token": uuid.uuid4(), "lease_expires_at": "2026-11-19T13:00:00Z", **columns}  # fmt: skip
    error = _violation(rolled_back_transaction, lambda c, d: _job(c, d, **values))
    assert error.orig.diag.constraint_name == "ingestion_jobs_lease_check"


def test_valid_job_states_with_the_new_columns(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        document = _stored(connection, ids)
        running = _job(connection, document, status="running", attempt_count=1, started_at="2026-11-19T12:00:00Z",
                       attempt_token=uuid.uuid4(), lease_expires_at="2026-11-19T13:00:00Z")  # fmt: skip
        connection.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'succeeded', finished_at = '2026-11-19T12:01:00Z', "
                "lease_expires_at = NULL WHERE id = :j"
            ),
            {"j": running},
        )
        _job(connection, document, kind="index")  # queued index job, no token or lease
        row = connection.execute(
            text("SELECT available_at, last_dispatched_at FROM ingestion_jobs WHERE kind = 'index'")
        ).one()
        assert row.available_at is not None and row.last_dispatched_at is None


def test_storage_identity_is_a_single_row(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        connection.execute(text("INSERT INTO storage_identity (storage_id) VALUES (gen_random_uuid())"))
        with pytest.raises(IntegrityError) as caught:
            with connection.begin_nested():
                connection.execute(text("INSERT INTO storage_identity (id, storage_id) VALUES (2, gen_random_uuid())"))
        assert caught.value.orig.diag.constraint_name == "storage_identity_single_row_check"
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO storage_identity (storage_id) VALUES (gen_random_uuid())"))


# --- downgrade -------------------------------------------------------------------------------


def test_downgrade_without_m3_state_restores_0002_exactly_and_upgrades_again(
    scratch3, disposable_engine, alembic, committed_transaction
):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        _m2_dataset(connection)
    snapshot_0002 = _snapshot(disposable_engine)
    contents = _table_contents(disposable_engine, M2_TABLES)
    alembic("upgrade", REVISION)

    result = alembic("downgrade", PREVIOUS)

    assert f"Running downgrade {REVISION} -> {PREVIOUS}" in result.stderr
    assert _snapshot(disposable_engine) == snapshot_0002
    assert _table_contents(disposable_engine, M2_TABLES) == contents
    alembic("upgrade", "head")
    assert _current(alembic) == f"{REVISION} (head)"


@pytest.mark.parametrize("state", ["section", "running", "token", "lease_only", "dispatched", "backoff", "identity"])
def test_downgrade_refuses_while_m3_state_exists(
    scratch3, disposable_engine, alembic, alembic_env, committed_transaction, state
):
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        document = _stored(connection, ids)
        if state == "section":
            _section(connection, document)
        elif state == "running":
            _job(connection, document, status="running", attempt_count=1, started_at="2026-11-19T12:00:00Z")
        elif state == "token":
            _job(connection, document, status="failed", attempt_count=1, started_at="2026-11-19T12:00:00Z",
                 finished_at="2026-11-19T12:01:00Z", attempt_token=uuid.uuid4())  # fmt: skip
        elif state == "lease_only":
            _job(connection, document, lease_expires_at="2026-11-19T13:00:00Z")
        elif state == "dispatched":
            _job(connection, document, last_dispatched_at="2026-11-19T12:00:00Z")
        elif state == "backoff":
            connection.execute(
                text("INSERT INTO ingestion_jobs (document_id, kind, available_at) VALUES (:d, 'parse', now() + interval '1 hour')"),
                {"d": document},
            )
        else:
            connection.execute(text("INSERT INTO storage_identity (storage_id) VALUES (gen_random_uuid())"))
    before = _snapshot(disposable_engine)
    tables = M2_TABLES | {"document_sections", "storage_identity"}
    contents = _table_contents(disposable_engine, tables)

    for target in (PREVIOUS, BASELINE_REVISION, "base"):
        output = _failed(run_alembic("downgrade", target, env=alembic_env))
        assert "Downgrade below 0003 refused before any change" in output

    assert _snapshot(disposable_engine) == before
    assert _table_contents(disposable_engine, tables) == contents
    assert _current(alembic) == f"{REVISION} (head)"


def test_queued_job_due_now_does_not_block_the_downgrade(scratch3, disposable_engine, alembic, committed_transaction):
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        _job(connection, _stored(connection, ids))  # available_at = now(): no schedule is lost
    alembic("downgrade", PREVIOUS)
    assert NEW_JOB_COLUMNS[0] not in [c[0] for c in _columns(disposable_engine, "ingestion_jobs")]


# --- offline SQL -------------------------------------------------------------------------------


def _offline(*args: str) -> str:
    result = run_alembic(*args, "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_offline_upgrade_sql_locks_and_checks_before_any_change():
    sql = _offline("upgrade", f"{PREVIOUS}:{REVISION}")
    preflight = sql.index("DO $migration_0003_preflight$")
    assert preflight < sql.index("ALTER TABLE ingestion_jobs ADD COLUMN attempt_token")
    assert "LOCK TABLE ingestion_jobs IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "RAISE EXCEPTION 'Migration 0003 stopped before any change. % ingestion_jobs rows" in sql
    assert "CREATE TABLE document_sections" in sql and "CREATE TABLE storage_identity" in sql
    assert "ON DELETE CASCADE" in sql


def test_offline_downgrade_sql_carries_the_guard():
    sql = _offline("downgrade", f"{REVISION}:{PREVIOUS}")
    guard = sql.index("DO $migration_0003_downgrade_guard$")
    assert guard < sql.index("DROP TABLE storage_identity") < sql.index("DROP TABLE document_sections")
    assert "LOCK TABLE document_sections, storage_identity, ingestion_jobs IN ACCESS EXCLUSIVE MODE" in sql


def test_offline_upgrade_sql_enforces_the_preflight_when_executed(
    scratch3, disposable_engine, alembic, committed_transaction
):
    sql = _offline("upgrade", f"{PREVIOUS}:{REVISION}")
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        document = _stored(connection, ids)
        connection.execute(
            text(
                "INSERT INTO ingestion_jobs (document_id, kind, status, attempt_count, started_at) "
                "VALUES (:d, 'parse', 'running', 1, now())"
            ),
            {"d": document},
        )
    before = _snapshot(disposable_engine)
    with pytest.raises(Exception, match="Migration 0003 stopped before any change"):
        with committed_transaction() as connection:
            _run_script(connection, sql)
    assert _snapshot(disposable_engine) == before
    with committed_transaction() as connection:
        connection.execute(text("UPDATE ingestion_jobs SET status = 'queued'"))
        _run_script(connection, sql)
    assert _current(alembic).split()[0] == REVISION


def test_historical_baseline_is_untouched_by_0003(scratch3, disposable_engine, alembic):
    alembic("upgrade", "head")
    alembic("downgrade", BASELINE_REVISION)
    assert _snapshot(disposable_engine) == _scaffold_snapshot()
    alembic("downgrade", "base")
    assert _tables(disposable_engine) <= {"alembic_version"}
