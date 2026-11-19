"""Revision 0002: document upload metadata and ingestion_jobs.

Database tests run only through the guarded fixtures (disposable database);
the offline-SQL tests at the end need no database. Every test that leaves
the database at 0002 leaves it without upload data, because the 0002
downgrade guard (rightly) refuses to drop such data and the runner downgrades
to base after pytest.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.alembic_cli import BASELINE_REVISION, run_alembic
from tests.test_migrations import (
    BASELINE_TABLES,
    _current,
    _insert_full_baseline_dataset,
    _insert_sample_rows,
    _relation_oids,
    _reset_to_base,
    _scaffold_snapshot,
    _snapshot,
    _table_contents,
    _tables,
)

REVISION = "0002"
SHA = "ab" * 32
NEW_DOCUMENT_COLUMNS = [
    "uploaded_by", "original_filename", "storage_key", "mime_type",
    "size_bytes", "page_count", "kind", "error_message",
]  # fmt: skip
BASELINE_DOCUMENT_COLUMNS = (
    "id, course_id, source_id, origin, title, url, licence, language, content_hash, status, fetched_at, created_at"
)


@pytest.fixture
def scratch(disposable_engine, alembic, committed_transaction):
    """For tests that move the database between revisions.

    Starts empty at base and, whatever the test did or wherever it failed,
    removes any upload data (which the 0002 downgrade guard would refuse to
    drop) and leaves the database empty at base again, so no other test
    depends on the order in which they run.
    """
    _reset_to_base(disposable_engine, alembic)
    yield
    with committed_transaction() as connection:
        if connection.execute(text("SELECT to_regclass('public.ingestion_jobs') IS NOT NULL")).scalar_one():
            connection.execute(text("DELETE FROM ingestion_jobs"))
            connection.execute(text("DELETE FROM documents WHERE storage_key IS NOT NULL"))
            connection.execute(text(f"UPDATE documents SET {', '.join(f'{c} = NULL' for c in NEW_DOCUMENT_COLUMNS)}"))
    _reset_to_base(disposable_engine, alembic)


def _documents_baseline_view(engine) -> list:
    with engine.connect() as connection:
        return connection.execute(
            text(f"SELECT {BASELINE_DOCUMENT_COLUMNS} FROM documents ORDER BY id")
        ).all()


def _failed(result) -> str:
    assert result.returncode != 0, "the command was expected to fail"
    return result.stdout + result.stderr


# --- upgrade ---------------------------------------------------------------------------


def test_upgrade_from_0001_keeps_legacy_data_and_adds_nullable_columns(
    scratch, disposable_engine, alembic, committed_transaction
):
    alembic("upgrade", BASELINE_REVISION)
    with committed_transaction() as connection:
        _insert_full_baseline_dataset(connection)
        # Legacy rows the scaffold allowed: a non-canonical hash and two class
        # documents of one course with the same hash. 0002 must accept them unchanged.
        course = connection.execute(text("SELECT id FROM courses LIMIT 1")).scalar_one()
        for title in ("Old A.pdf", "Old B.pdf"):
            connection.execute(
                text(
                    "INSERT INTO documents (course_id, origin, title, language, content_hash, status) "
                    "VALUES (:c, 'class', :t, 'en', 'ABCDEF-not-canonical', 'stale')"
                ),
                {"c": course, "t": title},
            )
    other_tables = BASELINE_TABLES - {"documents"}
    contents_before = _table_contents(disposable_engine, other_tables)
    documents_before = _documents_baseline_view(disposable_engine)
    oids_before = _relation_oids(disposable_engine)

    result = alembic("upgrade", REVISION)

    assert f"Running upgrade {BASELINE_REVISION} -> {REVISION}" in result.stderr
    assert _table_contents(disposable_engine, other_tables) == contents_before
    assert _documents_baseline_view(disposable_engine) == documents_before
    oids_after = _relation_oids(disposable_engine)
    assert {name: oids_after[name] for name in oids_before} == oids_before  # no table was rebuilt
    with disposable_engine.connect() as connection:
        filled = connection.execute(
            text(f"SELECT count(*) FROM documents WHERE num_nonnulls({', '.join(NEW_DOCUMENT_COLUMNS)}) > 0")
        ).scalar_one()
        assert filled == 0, "no metadata may be invented for legacy rows"
        assert connection.execute(text("SELECT count(*) FROM ingestion_jobs")).scalar_one() == 0


@pytest.mark.parametrize(
    "column, bad_value",
    [("origin", "zq-origin-x"), ("status", "zq-status-x")],  # values that appear nowhere else
)
def test_upgrade_stops_before_any_change_on_values_outside_the_new_checks(
    scratch, disposable_engine, alembic, alembic_env, committed_transaction, column, bad_value
):
    alembic("upgrade", BASELINE_REVISION)
    with committed_transaction() as connection:
        ids = _insert_sample_rows(connection)
        connection.execute(
            text(f"UPDATE documents SET {column} = :v WHERE id = :d"), {"v": bad_value, "d": ids["document"]}
        )
    before = _snapshot(disposable_engine)

    output = _failed(run_alembic("upgrade", REVISION, env=alembic_env))

    assert "Migration 0002 stopped before any change" in output
    assert bad_value not in output  # counts only, never data values
    assert _snapshot(disposable_engine) == before == _scaffold_snapshot()
    assert _current(alembic).split()[0] == BASELINE_REVISION


# --- constraints at head ---------------------------------------------------------------


def _base_rows(connection) -> dict[str, uuid.UUID]:
    ids = _insert_sample_rows(connection, label=uuid.uuid4().hex[:10])
    ids["course2"] = connection.execute(
        text("INSERT INTO courses (owner_id, name, join_code) VALUES (:u, 'Chem 102', :code) RETURNING id"),
        {"u": ids["user"], "code": uuid.uuid4().hex[:12]},
    ).scalar_one()
    return ids


def _stored(connection, ids, **overrides):
    """Insert a stored class upload; overrides replace (or with None, null) single columns."""
    values = {
        "id": uuid.uuid4(),
        "course_id": ids["course"],
        "origin": "class",
        "title": "Lecture.pdf",
        "language": "en",
        "status": "pending",
        "content_hash": SHA,
        "uploaded_by": ids["user"],
        "original_filename": "Lecture.pdf",
        "storage_key": f"{uuid.uuid4()}/{uuid.uuid4()}.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1234,
        "kind": "notes",
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{c}" for c in values)
    connection.execute(text(f"INSERT INTO documents ({columns}) VALUES ({placeholders})"), values)
    return values["id"]


def _violates(rolled_back_transaction, action, constraint: str | tuple[str, ...] | None = None):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        with pytest.raises(IntegrityError) as caught:
            action(connection, ids)
    if constraint is not None:
        expected = (constraint,) if isinstance(constraint, str) else constraint
        assert caught.value.orig.diag.constraint_name in expected
    return caught.value


@pytest.mark.parametrize(
    "missing",
    ["original_filename", "mime_type", "size_bytes", "content_hash"],
)
def test_stored_document_requires_every_file_metadata_field(head_database, rolled_back_transaction, missing):
    _violates(
        rolled_back_transaction,
        lambda c, ids: _stored(c, ids, **{missing: None}),
        "documents_stored_file_check",
    )


@pytest.mark.parametrize("bad_hash", ["AB" * 32, "ab" * 31, "zz" * 32, "ab" * 31 + "a\n", ""])
def test_stored_document_requires_a_canonical_sha256(head_database, rolled_back_transaction, bad_hash):
    _violates(
        rolled_back_transaction,
        lambda c, ids: _stored(c, ids, content_hash=bad_hash),
        "documents_stored_file_check",
    )


@pytest.mark.parametrize("missing", ["course_id", "uploaded_by"])
def test_stored_class_document_requires_course_and_uploader(head_database, rolled_back_transaction, missing):
    _violates(
        rolled_back_transaction,
        lambda c, ids: _stored(c, ids, **{missing: None}),
        "documents_class_upload_check",
    )


@pytest.mark.parametrize(
    "column, value, constraint",
    [
        ("size_bytes", 0, "documents_size_bytes_check"),
        ("size_bytes", -5, "documents_size_bytes_check"),
        ("page_count", 0, "documents_page_count_check"),
        ("kind", "lecture", "documents_kind_check"),
        ("origin", "upload", "documents_origin_check"),
        ("status", "queued", "documents_status_check"),
    ],
)
def test_document_value_checks(head_database, rolled_back_transaction, column, value, constraint):
    _violates(rolled_back_transaction, lambda c, ids: _stored(c, ids, **{column: value}), constraint)


def test_legacy_and_external_documents_stay_valid(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        legacy = (
            "INSERT INTO documents (course_id, origin, title, language, content_hash, status) "
            "VALUES (:c, 'class', 'Old.pdf', 'en', 'Not-A-Canonical-Hash', 'pending')"
        )
        connection.execute(text(legacy), {"c": ids["course"]})
        connection.execute(text(legacy), {"c": ids["course"]})  # same legacy hash twice: not deduplicated
        connection.execute(
            text(
                "INSERT INTO documents (origin, title, url, language, content_hash, status) "
                "VALUES ('external', 'OpenStax 4.4', 'https://openstax.org/x', 'en', :h, 'stale')"
            ),
            {"h": SHA},
        )
        # A stored external file needs no course or uploader (future harvesting).
        _stored(connection, ids, origin="external", course_id=None, uploaded_by=None, kind="textbook")
        _stored(connection, ids, page_count=12, error_message="Could not read page 3.", status="failed")
        connection.execute(text("SELECT 1"))  # still a usable transaction: nothing was rejected


def test_dedup_is_per_course_for_stored_class_uploads(head_database, rolled_back_transaction):
    _violates(
        rolled_back_transaction,
        lambda c, ids: (_stored(c, ids), _stored(c, ids)),
        "documents_course_id_content_hash_idx",
    )
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        _stored(connection, ids)
        _stored(connection, ids, course_id=ids["course2"])  # same file, other course
        _stored(connection, ids, origin="external")  # same file, not a class upload
        connection.execute(  # a legacy row with that hash and no stored file
            text(
                "INSERT INTO documents (course_id, origin, title, language, content_hash, status) "
                "VALUES (:c, 'class', 'Old.pdf', 'en', :h, 'pending')"
            ),
            {"c": ids["course"], "h": SHA},
        )


def test_storage_key_is_unique(head_database, rolled_back_transaction):
    key = f"{uuid.uuid4()}/{uuid.uuid4()}.pdf"
    _violates(
        rolled_back_transaction,
        lambda c, ids: (_stored(c, ids, storage_key=key), _stored(c, ids, storage_key=key, content_hash="cd" * 32)),
        "documents_storage_key_key",
    )


def test_uploader_must_exist(head_database, rolled_back_transaction):
    _violates(
        rolled_back_transaction,
        lambda c, ids: _stored(c, ids, uploaded_by=uuid.uuid4()),
        "documents_uploaded_by_fkey",
    )


# --- ingestion_jobs -----------------------------------------------------------------------


def _job(connection, document_id, **columns):
    values = {"document_id": document_id, "kind": "parse", **columns}
    if values.get("status") == "running" and "attempt_token" not in values:
        # Since revision 0003 a running job also needs an attempt token and a lease
        # (ingestion_jobs_lease_check). The tests here are about the 0002 checks, so
        # running rows get both, and the 0002 constraint under test is the one violated.
        values.update(attempt_token=uuid.uuid4(), lease_expires_at="2026-11-19T13:00:00Z")
    names = ", ".join(values)
    placeholders = ", ".join(f":{c}" for c in values)
    return connection.execute(
        text(f"INSERT INTO ingestion_jobs ({names}) VALUES ({placeholders}) RETURNING id"), values
    ).scalar_one()


def test_new_job_defaults(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        job = _job(connection, ids["document"])
        row = connection.execute(
            text(
                "SELECT status, attempt_count, max_attempts, created_at, updated_at, started_at, finished_at, "
                "pg_typeof(created_at)::text AS created_type FROM ingestion_jobs WHERE id = :j"
            ),
            {"j": job},
        ).one()
    assert (row.status, row.attempt_count, row.max_attempts) == ("queued", 0, 3)
    assert row.created_at.tzinfo is not None and row.created_type == "timestamp with time zone"
    assert (row.started_at, row.finished_at) == (None, None)


@pytest.mark.parametrize(
    "columns, constraint",
    [
        ({"kind": "harvest"}, "ingestion_jobs_kind_check"),
        # PostgreSQL reports the first failing CHECK by name; lifecycle also rejects unknown statuses.
        ({"status": "done"}, ("ingestion_jobs_lifecycle_check", "ingestion_jobs_status_check")),
        ({"attempt_count": -1}, "ingestion_jobs_attempts_check"),
        ({"max_attempts": 0, "attempt_count": 0}, "ingestion_jobs_attempts_check"),
        ({"attempt_count": 4, "max_attempts": 3}, "ingestion_jobs_attempts_check"),
        ({"status": "queued", "finished_at": "2026-11-19T12:00:00Z"}, "ingestion_jobs_lifecycle_check"),
        ({"status": "running", "attempt_count": 1}, "ingestion_jobs_lifecycle_check"),  # no started_at
        ({"status": "running", "started_at": "2026-11-19T12:00:00Z"}, "ingestion_jobs_lifecycle_check"),  # 0 attempts
        (
            {"status": "running", "attempt_count": 1, "started_at": "2026-11-19T12:00:00Z",
             "finished_at": "2026-11-19T12:01:00Z"},
            "ingestion_jobs_lifecycle_check",
        ),
        ({"status": "succeeded", "attempt_count": 1, "started_at": "2026-11-19T12:00:00Z"}, "ingestion_jobs_lifecycle_check"),
        (
            {"status": "succeeded", "attempt_count": 1, "started_at": "2026-11-19T12:00:00Z",
             "finished_at": "2026-11-19T11:00:00Z"},
            "ingestion_jobs_lifecycle_check",
        ),
        ({"status": "failed"}, "ingestion_jobs_lifecycle_check"),  # no finished_at
    ],
)  # fmt: skip
def test_job_checks_reject_invalid_states(head_database, rolled_back_transaction, columns, constraint):
    _violates(rolled_back_transaction, lambda c, ids: _job(c, ids["document"], **columns), constraint)


@pytest.mark.parametrize("column", ["kind", "status", "attempt_count", "max_attempts"])
def test_job_columns_reject_null(head_database, rolled_back_transaction, column):
    error = _violates(rolled_back_transaction, lambda c, ids: _job(c, ids["document"], **{column: None}))
    assert error.orig.sqlstate == "23502"  # not_null_violation


@pytest.mark.parametrize(
    "columns",
    [
        {"status": "queued", "attempt_count": 1, "started_at": "2026-11-19T12:00:00Z"},  # re-queued after a failure
        {"status": "running", "attempt_count": 1, "started_at": "2026-11-19T12:00:00Z"},
        {"status": "succeeded", "attempt_count": 2, "started_at": "2026-11-19T12:00:00Z",
         "finished_at": "2026-11-19T12:05:00Z"},
        {"status": "failed", "attempt_count": 3, "started_at": "2026-11-19T12:00:00Z",
         "finished_at": "2026-11-19T12:05:00Z", "error_message": "parser crashed"},
        {"status": "failed", "finished_at": "2026-11-19T12:05:00Z"},  # given up before it ever ran
    ],
)  # fmt: skip
def test_job_checks_accept_valid_states(head_database, rolled_back_transaction, columns):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        _job(connection, ids["document"], **columns)


def test_one_active_job_per_document_and_kind(head_database, rolled_back_transaction):
    _violates(
        rolled_back_transaction,
        lambda c, ids: (
            _job(c, ids["document"]),
            _job(c, ids["document"], status="running", attempt_count=1, started_at="2026-11-19T12:00:00Z"),
        ),
        "ingestion_jobs_document_id_kind_idx",
    )
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        finished = {"status": "failed", "finished_at": "2026-11-19T12:00:00Z"}
        _job(connection, ids["document"], **finished)
        _job(connection, ids["document"], **finished)  # history may hold many finished jobs
        _job(connection, ids["document"])  # one queued parse job
        _job(connection, ids["document"], kind="index")  # and one queued index job


def test_jobs_need_a_document_and_go_with_it(head_database, rolled_back_transaction):
    _violates(rolled_back_transaction, lambda c, ids: _job(c, uuid.uuid4()), "ingestion_jobs_document_id_fkey")
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        document = _stored(connection, ids)
        _job(connection, document)
        connection.execute(text("DELETE FROM documents WHERE id = :d"), {"d": document})
        assert connection.execute(
            text("SELECT count(*) FROM ingestion_jobs WHERE document_id = :d"), {"d": document}
        ).scalar_one() == 0


# --- downgrade -----------------------------------------------------------------------------


def test_downgrade_without_upload_data_restores_the_baseline_exactly(
    scratch, disposable_engine, alembic, committed_transaction
):
    alembic("upgrade", BASELINE_REVISION)
    with committed_transaction() as connection:
        _insert_full_baseline_dataset(connection)
    contents = _table_contents(disposable_engine, BASELINE_TABLES)
    alembic("upgrade", REVISION)

    result = alembic("downgrade", BASELINE_REVISION)

    assert f"Running downgrade {REVISION} -> {BASELINE_REVISION}" in result.stderr
    assert _snapshot(disposable_engine) == _scaffold_snapshot()
    assert _table_contents(disposable_engine, BASELINE_TABLES) == contents
    alembic("upgrade", "head")  # and forward again
    assert _current(alembic).endswith("(head)")


@pytest.mark.parametrize("data", ["job", "upload_metadata", "kind_only"])
def test_downgrade_refuses_while_upload_data_exists(
    scratch, disposable_engine, alembic, alembic_env, committed_transaction, data
):
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        if data == "job":
            _job(connection, ids["document"])
        elif data == "upload_metadata":
            _stored(connection, ids)
        else:
            connection.execute(text("UPDATE documents SET kind = 'slides' WHERE id = :d"), {"d": ids["document"]})
    before = _snapshot(disposable_engine)
    contents = _table_contents(disposable_engine, BASELINE_TABLES | {"ingestion_jobs"})

    for target in (BASELINE_REVISION, "base"):
        output = _failed(run_alembic("downgrade", target, env=alembic_env))
        assert "Downgrade below 0002 refused before any change" in output

    assert _snapshot(disposable_engine) == before
    assert _table_contents(disposable_engine, BASELINE_TABLES | {"ingestion_jobs"}) == contents
    assert _current(alembic).split()[0] == REVISION

    # Removing the data deliberately is the documented way forward.
    with committed_transaction() as connection:
        connection.execute(text("DELETE FROM ingestion_jobs"))
        connection.execute(text("DELETE FROM documents WHERE storage_key IS NOT NULL"))
        connection.execute(text("UPDATE documents SET kind = NULL"))
    alembic("downgrade", "base")
    assert _tables(disposable_engine) == {"alembic_version"}


# --- offline SQL (no database) -----------------------------------------------------------


def _offline(*args: str) -> str:
    result = run_alembic(*args, "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_offline_upgrade_sql_carries_the_preflight_check():
    sql = _offline("upgrade", f"{BASELINE_REVISION}:{REVISION}")
    preflight = sql.index("DO $migration_0002_preflight$")
    assert preflight < sql.index("ALTER TABLE documents ADD COLUMN uploaded_by")
    assert "RAISE EXCEPTION 'Migration 0002 stopped before any change. % documents rows" in sql  # single %, as PL/pgSQL expects
    assert "CREATE UNIQUE INDEX documents_course_id_content_hash_idx" in sql
    assert "WHERE origin = 'class' AND storage_key IS NOT NULL" in sql
    assert "CREATE TABLE ingestion_jobs" in sql


def test_offline_downgrade_sql_carries_the_guard():
    sql = _offline("downgrade", f"{REVISION}:{BASELINE_REVISION}")
    guard = sql.index("DO $migration_0002_downgrade_guard$")
    assert guard < sql.index("DROP TABLE ingestion_jobs")
    assert "LOCK TABLE ingestion_jobs, documents IN ACCESS EXCLUSIVE MODE" in sql


def test_offline_upgrade_sql_enforces_the_preflight_when_executed(
    scratch, disposable_engine, alembic, committed_transaction
):
    """SQL rendered with --sql and run later (as with psql) still refuses invalid legacy data."""
    sql = _offline("upgrade", f"{BASELINE_REVISION}:{REVISION}")
    alembic("upgrade", BASELINE_REVISION)
    with committed_transaction() as connection:
        ids = _insert_sample_rows(connection)
        connection.execute(text("UPDATE documents SET status = 'archived' WHERE id = :d"), {"d": ids["document"]})
    before = _snapshot(disposable_engine)

    with pytest.raises(Exception, match="Migration 0002 stopped before any change"):
        with committed_transaction() as connection:
            _run_script(connection, sql)
    assert _snapshot(disposable_engine) == before

    with committed_transaction() as connection:
        connection.execute(text("UPDATE documents SET status = 'pending'"))
        _run_script(connection, sql)
    assert _current(alembic).split()[0] == REVISION


def _run_script(connection, sql: str) -> None:
    """Send a whole SQL script as-is (no parameter processing), the way psql would."""
    cursor = connection.connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()
