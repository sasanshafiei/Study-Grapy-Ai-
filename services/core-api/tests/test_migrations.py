"""Migration tests against a disposable PostgreSQL + pgvector database.

These upgrade, downgrade and write to the database, so they only run through
the guarded fixtures in conftest.py (see tests/disposable_db.py); without a
disposable database they are skipped. Run them with
services/core-api/scripts/test_migrations.sh.

Two kinds of tests live here:
- Historical baseline tests target revision 0001 explicitly and compare with
  the recorded scaffold schema (tests/fixtures/scaffold_schema.*). They stay
  valid forever because 0001 never changes.
- Current-head tests use the actual Alembic head and the current ORM models,
  so they keep working when later revisions add tables and indexes.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, pool, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

from app import models  # noqa: F401  (registers the tables)
from app.db import Base
from app.migration_safety import REQUIRE_COMMENT_ENV
from tests import schema_snapshot
from tests.alembic_cli import BASELINE_REVISION, head_revision, run_alembic
from tests.disposable_db import DisposableTarget, NotDisposable, verify_connection

FIXTURES = Path(__file__).parent / "fixtures"
SCAFFOLD_SNAPSHOT = FIXTURES / "scaffold_schema.json"  # catalog view of the init_db.py schema
SCAFFOLD_SQL = FIXTURES / "scaffold_schema.sql"  # normalized pg_dump of that same database
BASELINE_TABLES = {
    "users", "courses", "enrollments", "sources", "documents", "chunks",
    "topics", "chunk_topics", "questions", "attempts", "mastery_scores",
}  # fmt: skip
HEAD = head_revision()
# Same options as migrations/env.py uses for `alembic check`.
COMPARE_OPTIONS = {"compare_type": True, "compare_server_default": True}
EMBEDDING_DIM = 384
PG = postgresql.dialect()


# --- helpers -----------------------------------------------------------------------


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _orm_tables() -> set[str]:
    return set(Base.metadata.tables)


def _vector_installed(engine) -> bool:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        ).scalar_one()


def _snapshot(engine) -> dict:
    with engine.connect() as connection:
        return schema_snapshot.snapshot(connection)


def _scaffold_snapshot() -> dict:
    return json.loads(SCAFFOLD_SNAPSHOT.read_text())


def _current(alembic) -> str:
    return alembic("current").stdout.strip()


def _reset_to_base(engine, alembic) -> None:
    if "alembic_version" in _tables(engine):
        alembic("downgrade", "base")
    assert _tables(engine) <= {"alembic_version"}


def _at_baseline(engine, alembic) -> None:
    """Put the database at exactly revision 0001 (historical baseline tests)."""
    _reset_to_base(engine, alembic)
    alembic("upgrade", BASELINE_REVISION)


def _vector_literal(dimensions: int) -> str:
    return "[" + ",".join(["0.5"] * dimensions) + "]"


def _insert_sample_rows(connection, label: str = "sample") -> dict[str, uuid.UUID]:
    """Baseline-schema rows: one per table on the user -> course -> document -> chunk path."""
    user_id = connection.execute(
        text(
            "INSERT INTO users (email, display_name, hashed_password, role, status) "
            "VALUES (:email, 'Sample', 'not-a-real-hash', 'professor', 'active') RETURNING id"
        ),
        {"email": f"{label}@example.test"},
    ).scalar_one()
    course_id = connection.execute(
        text("INSERT INTO courses (owner_id, name, join_code) VALUES (:u, 'Chem 101', :code) RETURNING id"),
        {"u": user_id, "code": label[:12]},
    ).scalar_one()
    document_id = connection.execute(
        text(
            "INSERT INTO documents (course_id, origin, title, language, status) "
            "VALUES (:c, 'class', 'Lecture 1.pdf', 'en', 'pending') RETURNING id"
        ),
        {"c": course_id},
    ).scalar_one()
    chunk_id = connection.execute(
        text(
            "INSERT INTO chunks (document_id, course_id, origin, text, locator, embedding) "
            "VALUES (:d, :c, 'class', 'Moles and mass.', CAST(:loc AS jsonb), CAST(:emb AS vector)) "
            "RETURNING id"
        ),
        {"d": document_id, "c": course_id, "loc": '{"page": 1}', "emb": _vector_literal(EMBEDDING_DIM)},
    ).scalar_one()
    return {"user": user_id, "course": course_id, "document": document_id, "chunk": chunk_id}


def _insert_full_baseline_dataset(connection) -> None:
    """At least one row in every baseline table, through every foreign key."""
    ids = _insert_sample_rows(connection, label="adopt")
    student = connection.execute(
        text(
            "INSERT INTO users (email, display_name, hashed_password, role, status) "
            "VALUES ('adopt-student@example.test', 'Student', 'not-a-real-hash', 'student', 'active') RETURNING id"
        )
    ).scalar_one()
    source = connection.execute(
        text("INSERT INTO sources (kind, domain, licence, enabled) VALUES ('openstax', 'openstax.org', 'CC BY', true) RETURNING id")
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO documents (source_id, origin, title, url, licence, language, content_hash, status) "
            "VALUES (:s, 'external', 'Chemistry 2e 4.4', 'https://openstax.org/x', 'CC BY', 'en', repeat('a', 64), 'indexed')"
        ),
        {"s": source},
    )
    connection.execute(
        text("INSERT INTO enrollments (user_id, course_id, role) VALUES (:u, :c, 'student')"),
        {"u": student, "c": ids["course"]},
    )
    topic = connection.execute(
        text("INSERT INTO topics (course_id, name) VALUES (:c, 'Stoichiometry') RETURNING id"), {"c": ids["course"]}
    ).scalar_one()
    connection.execute(
        text("INSERT INTO chunk_topics (chunk_id, topic_id) VALUES (:ch, :t)"), {"ch": ids["chunk"], "t": topic}
    )
    question = connection.execute(
        text(
            "INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, choices, answer, status) "
            "VALUES (:c, :t, 'practice', 'mcq', 'easy', 'Which reactant limits?', CAST(:choices AS jsonb), 'A', 'approved') "
            "RETURNING id"
        ),
        {"c": ids["course"], "t": topic, "choices": '["A", "B"]'},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO attempts (user_id, question_id, topic_id, is_correct, hint_count, response_ms) "
            "VALUES (:u, :q, :t, true, 0, 1500)"
        ),
        {"u": student, "q": question, "t": topic},
    )
    connection.execute(
        text("INSERT INTO mastery_scores (user_id, topic_id, mastery, attempts) VALUES (:u, :t, 0.5, 1)"),
        {"u": student, "t": topic},
    )


def _table_contents(engine, tables) -> dict[str, tuple[int, str]]:
    """Row count and an order-independent checksum of every row, per table."""
    with engine.connect() as connection:
        return {
            table: tuple(
                connection.execute(
                    text(f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {table} t")
                ).one()
            )
            for table in sorted(tables)
        }


def _relation_oids(engine) -> dict[str, int]:
    """OID of every relation in public; a dropped and re-created table gets a new one."""
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT relname, oid::bigint FROM pg_class WHERE relnamespace = 'public'::regnamespace")
            ).all()
        )


def _scaffold_statements() -> list[str]:
    return [s for s in SCAFFOLD_SQL.read_text().split(";\n\n") if s.strip()]


# --- guards, verified against the live server ----------------------------------------


def test_live_identity_check_accepts_only_this_runs_database(disposable_engine, disposable_target):
    with disposable_engine.connect() as connection:
        verify_connection(connection, disposable_target)

        wrong_token = DisposableTarget(url=disposable_target.url, token="f" * 32)
        other_database = DisposableTarget(
            url=disposable_target.url.set(database="sg_disposable_other"), token=disposable_target.token
        )
        other_user = DisposableTarget(
            url=disposable_target.url.set(username="someone_else"), token=disposable_target.token
        )
        for target in (wrong_token, other_database, other_user):
            with pytest.raises(NotDisposable):
                verify_connection(connection, target)
        connection.rollback()


def test_inherited_libpq_settings_do_not_redirect_the_migration(disposable_target, alembic, alembic_env):
    """Explicit URL values beat PGHOST/PGPORT/PGUSER/PGDATABASE, and the port is always explicit.

    Before the fix, a URL without a port plus PGPORT=1 would have connected to port 1.
    """
    alembic("upgrade", "head")
    url = disposable_target.url
    env = {
        **alembic_env,
        "DATABASE_URL": url.set(port=None).render_as_string(hide_password=False),  # implicit port
        "PGHOST": "localhost",
        "PGPORT": "1",
        "PGUSER": "nobody",
        "PGDATABASE": "postgres",
    }
    result = run_alembic("current", env=env)
    assert result.returncode == 0, result.stderr
    assert f"Migration target: {url.host}:5432/{url.database}" in result.stderr
    assert f"Connected to database '{url.database}'" in result.stderr
    assert result.stdout.strip() == f"{HEAD} (head)"


def test_wrong_marker_stops_a_downgrade_before_any_change(disposable_engine, disposable_target, alembic, alembic_env):
    """env.py checks the marker on the migration's own connection, before any DDL."""
    alembic("upgrade", "head")
    before = _snapshot(disposable_engine)

    env = {**alembic_env, REQUIRE_COMMENT_ENV: "studygraph-disposable-test:" + "0" * 32}
    result = run_alembic("downgrade", "base", env=env)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert REQUIRE_COMMENT_ENV in output and "Stopped before any change" in output
    assert "Running downgrade" not in output
    assert _snapshot(disposable_engine) == before
    assert _current(alembic) == f"{HEAD} (head)"


def test_other_database_on_the_same_server_is_refused_and_untouched(disposable_target, alembic_env):
    """Same server, same credentials, correct marker value, but the wrong database."""
    other = disposable_target.url.set(database="postgres")
    env = {**alembic_env, "DATABASE_URL": other.render_as_string(hide_password=False)}

    result = run_alembic("upgrade", "head", env=env)

    assert result.returncode != 0
    assert REQUIRE_COMMENT_ENV in result.stdout + result.stderr
    engine = create_engine(other, poolclass=pool.NullPool)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT to_regclass('public.alembic_version')")).scalar_one() is None
        assert connection.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        ).scalar_one() == 0
    engine.dispose()


@pytest.mark.parametrize("redirect", ["search_path", "role"])
def test_role_defaults_that_change_schema_or_role_are_refused(
    redirect, disposable_engine, disposable_target, alembic, alembic_env, committed_transaction
):
    """A login role whose defaults move the session elsewhere cannot run migrations.

    search_path: without the check, the tables would land in schema sg_side.
    role: the session would act as another role than the one the URL names.
    """
    _reset_to_base(disposable_engine, alembic)
    before = _snapshot(disposable_engine)
    role = f"sg_alt_{secrets.token_hex(3)}"
    password = secrets.token_hex(16)
    owner = disposable_target.url.username
    with committed_transaction() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        if redirect == "search_path":
            connection.exec_driver_sql(f"CREATE SCHEMA sg_side AUTHORIZATION {role}")
            connection.exec_driver_sql(f"ALTER ROLE {role} SET search_path = sg_side")
        else:
            connection.exec_driver_sql(f'GRANT "{owner}" TO {role}')
            connection.exec_driver_sql(f"ALTER ROLE {role} SET role = '{owner}'")
    try:
        url = disposable_target.url.set(username=role, password=password)
        env = {**alembic_env, "DATABASE_URL": url.render_as_string(hide_password=False)}
        result = run_alembic("upgrade", "head", env=env)

        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert ("default schema" if redirect == "search_path" else "different role") in output
        assert "Running upgrade" not in output and password not in output
        assert _snapshot(disposable_engine) == before
        with disposable_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname = 'sg_side'")
            ).scalar_one() == 0
    finally:
        with committed_transaction() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS sg_side CASCADE")
            connection.exec_driver_sql(f"DROP OWNED BY {role}")
            connection.exec_driver_sql(f"DROP ROLE {role}")


def test_schema_snapshot_session_cannot_write(disposable_target, alembic):
    """The snapshot tool used before adopting a real database is read-only."""
    alembic("upgrade", "head")
    engine = schema_snapshot.read_only_engine(disposable_target.url)
    with engine.connect() as connection:
        assert schema_snapshot.snapshot(connection)["tables"]
        with pytest.raises(DBAPIError, match="read-only transaction"):
            connection.execute(text("CREATE TABLE snapshot_write_probe (id integer)"))
        connection.rollback()
    assert "snapshot_write_probe" not in _tables(engine)


# --- current head ------------------------------------------------------------------


def test_upgrade_from_empty_database_installs_pgvector_and_tables(
    disposable_engine, alembic, committed_transaction
):
    _reset_to_base(disposable_engine, alembic)
    # Start without pgvector, as a fresh server without infra/postgres/init.sql
    # would: the migration itself must install the extension.
    with committed_transaction() as connection:
        connection.execute(text("DROP EXTENSION IF EXISTS vector"))
    assert not _vector_installed(disposable_engine)

    result = alembic("upgrade", "head")

    assert f"Running upgrade  -> {BASELINE_REVISION}" in result.stderr  # the root revision runs first
    assert _tables(disposable_engine) == _orm_tables() | {"alembic_version"}
    assert _vector_installed(disposable_engine)
    assert _current(alembic) == f"{HEAD} (head)"


def test_repeated_upgrade_is_a_no_op(disposable_engine, alembic):
    alembic("upgrade", "head")
    before = _snapshot(disposable_engine)

    result = alembic("upgrade", "head")

    assert "Running upgrade" not in result.stderr
    assert _snapshot(disposable_engine) == before
    assert _current(alembic) == f"{HEAD} (head)"


def _ddl_type(type_) -> str:
    rendered = type_.compile(dialect=PG)
    # PostgreSQL stores FLOAT without a precision as double precision.
    return {"FLOAT": "DOUBLE PRECISION"}.get(rendered, rendered)


_CONSTRAINT_KINDS = {"p": "PRIMARY KEY", "f": "FOREIGN KEY", "u": "UNIQUE", "c": "CHECK", "x": "EXCLUDE"}


def _orm_constraint_names(table) -> set[tuple[str, str]]:
    """(name, kind) of every constraint the ORM emits for `table`."""
    ddl = str(CreateTable(table).compile(dialect=PG))
    return set(re.findall(r"CONSTRAINT (\w+) (PRIMARY KEY|FOREIGN KEY|UNIQUE|CHECK|EXCLUDE)", ddl))


def _db_constraint_names(connection, table_name: str) -> set[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT conname, contype::text FROM pg_constraint "
            "WHERE conrelid = to_regclass(:t) AND contype IN ('p', 'f', 'u', 'c', 'x')"
        ),
        {"t": f"public.{table_name}"},
    ).all()
    return {(name, _CONSTRAINT_KINDS[kind]) for name, kind in rows}


def test_reflected_schema_matches_the_orm_models(disposable_engine, alembic):
    """SQLAlchemy reflection (and the catalog, for names) of head == app/models.py."""
    alembic("upgrade", "head")
    inspector = inspect(disposable_engine)
    assert set(inspector.get_table_names()) - {"alembic_version"} == _orm_tables()

    with disposable_engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            reflected_columns = inspector.get_columns(table.name)
            assert [c["name"] for c in reflected_columns] == [c.name for c in table.columns], table.name
            for reflected in reflected_columns:
                column = table.columns[reflected["name"]]
                where = f"{table.name}.{column.name}"
                assert _ddl_type(reflected["type"]) == _ddl_type(column.type), where
                assert reflected["nullable"] == column.nullable, where
                expected_default = (
                    None if column.server_default is None else str(column.server_default.arg.compile(dialect=PG))
                )
                assert reflected["default"] == expected_default, where

            # Constraint names: what the ORM (naming convention) emits == what exists.
            assert _db_constraint_names(connection, table.name) == _orm_constraint_names(table), table.name

            primary_key = inspector.get_pk_constraint(table.name)
            assert primary_key["constrained_columns"] == [c.name for c in table.primary_key.columns]

            reflected_fks = {
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                    fk["options"].get("ondelete"),
                    fk["options"].get("onupdate"),
                )
                for fk in inspector.get_foreign_keys(table.name)
            }
            orm_fks = {
                (
                    tuple(c.name for c in fk.columns),
                    fk.referred_table.name,
                    tuple(element.column.name for element in fk.elements),
                    fk.ondelete,
                    fk.onupdate,
                )
                for fk in table.foreign_key_constraints
            }
            assert reflected_fks == orm_fks, table.name

            reflected_uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table.name)}
            orm_uniques = {
                tuple(c.name for c in u.columns) for u in table.constraints if isinstance(u, sa.UniqueConstraint)
            }
            assert reflected_uniques == orm_uniques, table.name

            # Indexes that do not back a constraint must be exactly the ORM's Index objects.
            reflected_indexes = {
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in inspector.get_indexes(table.name)
                if not i.get("duplicates_constraint")
            }
            orm_indexes = {
                (
                    re.match(r"CREATE (?:UNIQUE )?INDEX (\w+)", str(CreateIndex(index).compile(dialect=PG))).group(1),
                    tuple(c.name for c in index.columns),
                    bool(index.unique),
                )
                for index in table.indexes
            }
            assert reflected_indexes == orm_indexes, table.name
        connection.rollback()


def test_autogenerate_finds_no_difference_from_the_orm(disposable_engine, alembic):
    alembic("upgrade", "head")
    with disposable_engine.connect() as connection:
        context = MigrationContext.configure(connection=connection, opts=COMPARE_OPTIONS)
        differences = compare_metadata(context, Base.metadata)
        connection.rollback()
    assert differences == []


def test_alembic_check_and_current(alembic):
    alembic("upgrade", "head")
    check = alembic("check")
    assert "No new upgrade operations detected" in check.stdout + check.stderr
    assert _current(alembic) == f"{HEAD} (head)"


def test_downgrade_with_data_then_upgrade_again(disposable_engine, alembic, committed_transaction):
    """Baseline data survives every later upgrade; the full chain downgrades and re-applies."""
    _at_baseline(disposable_engine, alembic)
    with committed_transaction() as connection:
        _insert_sample_rows(connection)
    alembic("upgrade", "head")
    with disposable_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 1

    alembic("downgrade", "base")

    assert _tables(disposable_engine) == {"alembic_version"}
    assert _current(alembic) == ""
    assert _vector_installed(disposable_engine), "downgrade must keep the shared vector extension"

    alembic("upgrade", "head")

    assert _tables(disposable_engine) == _orm_tables() | {"alembic_version"}
    with disposable_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
    assert _current(alembic) == f"{HEAD} (head)"


# --- historical baseline (revision 0001) ---------------------------------------------


def test_baseline_revision_reproduces_the_scaffold_schema(disposable_engine, alembic):
    """PostgreSQL catalog view of revision 0001 == the schema init_db.py created."""
    _at_baseline(disposable_engine, alembic)

    expected = _scaffold_snapshot()
    actual = _snapshot(disposable_engine)
    assert actual == expected, schema_snapshot.diff(expected, actual)

    constraints = actual["constraints"]
    assert sum("PRIMARY KEY" in c for c in constraints) == 11
    assert sum("FOREIGN KEY" in c for c in constraints) == 17
    assert sum(": UNIQUE" in c for c in constraints) == 5
    assert set(actual["tables"]) == BASELINE_TABLES
    assert "chunks.embedding: vector(384)" in actual["columns"]
    assert "vector (public)" in actual["extensions"]


def _violation(connection, statement: str, params: dict | None = None):
    with pytest.raises((IntegrityError, DataError)) as info:
        with connection.begin_nested():
            connection.execute(text(statement), params or {})
    return info.value.orig


def test_baseline_constraints_and_server_defaults_are_enforced(disposable_engine, alembic, rolled_back_transaction):
    _at_baseline(disposable_engine, alembic)
    with rolled_back_transaction() as connection:
        ids = _insert_sample_rows(connection)
        assert all(isinstance(value, uuid.UUID) for value in ids.values())  # gen_random_uuid()
        created_at = connection.execute(
            text("SELECT created_at FROM users WHERE id = :id"), {"id": ids["user"]}
        ).scalar_one()
        assert created_at is not None  # now()

        duplicate_email = _violation(
            connection,
            "INSERT INTO users (email, display_name, hashed_password, role, status) "
            "VALUES ('sample@example.test', 'Other', 'x', 'student', 'active')",
        )
        assert duplicate_email.diag.constraint_name == "users_email_key"

        unknown_owner = _violation(
            connection, "INSERT INTO courses (owner_id, name, join_code) VALUES (gen_random_uuid(), 'X', 'JOIN02')"
        )
        assert unknown_owner.diag.constraint_name == "courses_owner_id_fkey"

        enrol = "INSERT INTO enrollments (user_id, course_id, role) VALUES (:u, :c, 'student')"
        connection.execute(text(enrol), {"u": ids["user"], "c": ids["course"]})
        duplicate_enrolment = _violation(connection, enrol, {"u": ids["user"], "c": ids["course"]})
        assert duplicate_enrolment.diag.constraint_name == "enrollments_user_id_course_id_key"

        missing_title = _violation(
            connection, "INSERT INTO documents (origin, language, status) VALUES ('class', 'en', 'pending')"
        )
        assert missing_title.diag.column_name == "title"

        # Python-side ORM defaults are not database defaults (faithful to the scaffold).
        missing_origin = _violation(
            connection,
            "INSERT INTO chunks (document_id, text) VALUES (:d, 'no origin given')",
            {"d": ids["document"]},
        )
        assert missing_origin.diag.column_name == "origin"


def test_baseline_embedding_column_accepts_only_384_dimensions(disposable_engine, alembic, rolled_back_transaction):
    _at_baseline(disposable_engine, alembic)
    with rolled_back_transaction() as connection:
        ids = _insert_sample_rows(connection)
        stored = connection.execute(
            text("SELECT vector_dims(embedding) FROM chunks WHERE id = :id"), {"id": ids["chunk"]}
        ).scalar_one()
        assert stored == EMBEDDING_DIM

        wrong_size = _violation(
            connection,
            "INSERT INTO chunks (document_id, origin, text, embedding) "
            "VALUES (:d, 'class', 'too short', CAST(:emb AS vector))",
            {"d": ids["document"], "emb": _vector_literal(EMBEDDING_DIM - 1)},
        )
        assert f"expected {EMBEDDING_DIM} dimensions, not {EMBEDDING_DIM - 1}" in str(wrong_size)


# --- adopting a legacy (init_db.py) database --------------------------------------------


def test_adopting_a_legacy_scaffold_database_with_stamp(disposable_engine, alembic, committed_transaction):
    """The documented adoption procedure, rehearsed on a throwaway copy of the legacy schema."""
    _reset_to_base(disposable_engine, alembic)
    with committed_transaction() as connection:
        # A database built by init_db.py has no Alembic bookkeeping at all. This
        # is test setup on the throwaway database, not a recovery procedure.
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        # Re-create the legacy schema from the pg_dump of the real scaffold database.
        for statement in _scaffold_statements():
            connection.exec_driver_sql(statement)

    # 1. The legacy database is the recorded scaffold schema, without Alembic.
    assert _snapshot(disposable_engine) == _scaffold_snapshot()
    assert "alembic_version" not in _tables(disposable_engine)

    # 2. Representative data in every table.
    with committed_transaction() as connection:
        _insert_full_baseline_dataset(connection)
    contents_before = _table_contents(disposable_engine, BASELINE_TABLES)
    assert all(count >= 1 for count, _ in contents_before.values())
    oids_before = _relation_oids(disposable_engine)

    # 3. Stamp.
    result = alembic("stamp", BASELINE_REVISION)
    assert f"Running stamp_revision  -> {BASELINE_REVISION}" in result.stderr
    assert "Running upgrade" not in result.stderr

    # 4. The revision is recorded.
    assert _current(alembic).split()[0] == BASELINE_REVISION

    # 5. Nothing else changed: same schema, same rows, and every relation kept its
    #    OID (a dropped and re-created table would get a new one). Only Alembic's
    #    table and its primary-key index are new.
    assert _snapshot(disposable_engine) == _scaffold_snapshot()
    assert _table_contents(disposable_engine, BASELINE_TABLES) == contents_before
    oids_after = _relation_oids(disposable_engine)
    assert set(oids_after) - set(oids_before) == {"alembic_version", "alembic_version_pkc"}
    assert {name: oids_after[name] for name in oids_before} == oids_before

    # 6. From here the database is managed normally.
    alembic("upgrade", "head")
    check = alembic("check")
    assert "No new upgrade operations detected" in check.stdout + check.stderr
    assert _current(alembic) == f"{HEAD} (head)"
    with disposable_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM attempts")).scalar_one() == 1

    alembic("downgrade", "base")  # leave the throwaway database empty for other tests
