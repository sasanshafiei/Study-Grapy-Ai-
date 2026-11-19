"""Fixtures for the database-backed tests.

They need a disposable PostgreSQL database; scripts/test_migrations.sh creates
one and runs the whole suite. Without TEST_DATABASE_URL / TEST_DATABASE_TOKEN
the database tests are skipped and only the tests that need no database run.
See tests/disposable_db.py for the safety checks.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import Connection, Engine

from tests.alembic_cli import head_revision, run_alembic
from tests.disposable_db import (
    DisposableTarget,
    MissingTestDatabase,
    target_from_env,
    verify_connection,
)


@pytest.fixture(scope="session")
def disposable_target() -> DisposableTarget:
    try:
        return target_from_env(os.environ)
    except MissingTestDatabase as exc:
        pytest.skip(
            f"needs a disposable test database ({exc}); "
            "run services/core-api/scripts/test_migrations.sh"
        )
    # NotDisposable is deliberately not caught: a misconfigured target is an
    # error that stops every database test, never a silent skip.


@pytest.fixture(scope="session")
def disposable_engine(disposable_target: DisposableTarget) -> Iterator[Engine]:
    engine = create_engine(disposable_target.url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        verify_connection(connection, disposable_target)
        connection.rollback()
    yield engine
    engine.dispose()


@pytest.fixture
def committed_transaction(
    disposable_engine: Engine, disposable_target: DisposableTarget
) -> Callable[[], AbstractContextManager[Connection]]:
    """Open a transaction that commits, after re-checking the database identity."""

    @contextmanager
    def open_transaction() -> Iterator[Connection]:
        with disposable_engine.begin() as connection:
            verify_connection(connection, disposable_target)
            yield connection

    return open_transaction


@pytest.fixture
def rolled_back_transaction(
    disposable_engine: Engine, disposable_target: DisposableTarget
) -> Callable[[], AbstractContextManager[Connection]]:
    """Open a transaction that is always rolled back, after re-checking identity."""

    @contextmanager
    def open_transaction() -> Iterator[Connection]:
        with disposable_engine.connect() as connection:
            verify_connection(connection, disposable_target)
            try:
                yield connection
            finally:
                connection.rollback()

    return open_transaction


@pytest.fixture
def alembic_env(disposable_target: DisposableTarget) -> dict[str, str]:
    """Environment for an alembic subprocess aimed at the disposable database.

    MIGRATIONS_REQUIRE_DATABASE_COMMENT makes migrations/env.py verify this
    run's marker on the migration's own connection before any DDL.
    """
    return {
        "DATABASE_URL": disposable_target.url.render_as_string(hide_password=False),
        "MIGRATIONS_REQUIRE_DATABASE_COMMENT": disposable_target.marker,
    }


@pytest.fixture
def alembic(disposable_engine: Engine, disposable_target: DisposableTarget, alembic_env: dict[str, str]):
    """Run the alembic CLI against the disposable database.

    Two checks guard every command: this fixture verifies the database on its
    own connection first, and env.py verifies the same marker again on the
    connection that actually runs the migration, before any DDL.
    """

    def run(*args: str):
        with disposable_engine.connect() as connection:
            verify_connection(connection, disposable_target)
            connection.rollback()
        result = run_alembic(*args, env=alembic_env)
        if result.returncode != 0:
            pytest.fail(
                f"`alembic {' '.join(args)}` exited with {result.returncode}\n"
                f"--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}"
            )
        return result

    return run


@pytest.fixture
def head_database(disposable_engine: Engine, disposable_target: DisposableTarget, alembic) -> None:
    """The disposable database at the current head; migrates only when it is not there.

    Other tests move the shared database between revisions, so this is checked
    per test rather than once per session.
    """
    with disposable_engine.connect() as connection:
        verify_connection(connection, disposable_target)
        current = None
        if connection.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one():
            current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        connection.rollback()
    if current != head_revision():
        alembic("upgrade", "head")
