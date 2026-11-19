"""Fixtures for the database-backed tests.

They need a disposable PostgreSQL database; scripts/test_migrations.sh creates
one and runs the whole suite. Without TEST_DATABASE_URL / TEST_DATABASE_TOKEN
the database tests are skipped and only the tests that need no database run.
See tests/disposable_db.py for the safety checks.

The broker tests (M3) also need the disposable Redis the runner starts
(TEST_REDIS_URL / TEST_REDIS_TOKEN, checked by tests/disposable_redis.py);
without it they are skipped.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest
from sqlalchemy import create_engine, event, pool, text
from sqlalchemy.engine import Connection, Engine

from tests.alembic_cli import head_revision, run_alembic
from tests.api_support import ApiHarness, World, bind_storage
from tests.disposable_db import (
    DisposableTarget,
    MissingTestDatabase,
    target_from_env,
    verify_connection,
)
from tests.disposable_redis import DisposableRedis, MissingTestRedis, redis_target_from_env, verify_redis
from tests.worker_support import CeleryProcess, Infra


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


# --- API tests --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api_engine(disposable_target: DisposableTarget) -> Iterator[Engine]:
    """Engine for the application under test; every new connection re-verifies the database."""
    engine = create_engine(disposable_target.url, poolclass=pool.NullPool)

    @event.listens_for(engine, "engine_connect")
    def _verify(connection: Connection) -> None:
        verify_connection(connection, disposable_target)
        connection.rollback()

    yield engine
    engine.dispose()


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


@pytest.fixture
def world(head_database, api_engine: Engine) -> Iterator[World]:
    """Rows created by one test, deleted again when it ends (whatever happened)."""
    created = World(api_engine)
    try:
        yield created
    finally:
        created.cleanup()


@pytest.fixture
def api(world: World, tmp_path) -> Iterator[ApiHarness]:
    harness = ApiHarness(world, tmp_path)
    try:
        yield harness
    finally:
        harness.client.close()


@pytest.fixture
def make_api(world: World, tmp_path) -> Iterator[Callable[..., ApiHarness]]:
    """Like `api`, with custom settings (e.g. a small upload limit)."""
    harnesses: list[ApiHarness] = []

    def build(**options) -> ApiHarness:
        harness = ApiHarness(world, tmp_path / f"app{len(harnesses)}", **options)
        harnesses.append(harness)
        return harness

    try:
        yield build
    finally:
        for harness in harnesses:
            harness.client.close()


# --- M3: broker and real worker processes -----------------------------------------------------


@pytest.fixture(scope="session")
def disposable_redis() -> DisposableRedis:
    try:
        target = redis_target_from_env(os.environ)
    except MissingTestRedis as exc:
        pytest.skip(f"needs a disposable Redis ({exc}); run services/core-api/scripts/test_migrations.sh")
    verify_redis(target)  # NotDisposableRedis is an error, never a skip
    return target


@pytest.fixture
def infra(world, disposable_target, disposable_redis, tmp_path) -> Infra:
    """A bound upload store, unique queues and the verified targets, for workers and publishers."""
    upload_dir = tmp_path / "store"
    bind_storage(world.engine, upload_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return Infra(
        database_url=disposable_target.url.render_as_string(hide_password=False),
        database_marker=disposable_target.marker,
        redis_url=disposable_redis.url,
        upload_dir=upload_dir,
        work_dir=work_dir,
    )


@pytest.fixture
def celery_processes(world) -> Iterator[list[CeleryProcess]]:
    """Processes registered here are stopped when the test ends, before its rows are deleted."""
    processes: list[CeleryProcess] = []
    try:
        yield processes
    finally:
        for process in processes:
            process.stop()
