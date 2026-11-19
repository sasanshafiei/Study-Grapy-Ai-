"""M3 review H3: the worker's database target and the verification of its operational connections.

The first group needs no database (target validation). The second uses the
disposable database: every connection of the worker engine is identity-checked
before anything runs on it, so a wrong target cannot claim or change a job.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.ingestion import pipeline, runtime
from app.ingestion.pipeline import WorkerContext
from app.ingestion.runtime import DatabaseTarget, PreflightError, database_target, verified_engine
from app.migration_safety import UnsafeMigrationTarget
from app.storage import LocalFileStorage
from tests.api_support import bind_storage
from tests.pdf_samples import make_pdf
from tests.worker_support import add_stored_document, job_row

GOOD = "postgresql+psycopg://worker:fake-worker-pw@db-host.internal:5432/studygraph"


# --- target validation (no database) ----------------------------------------------------------


@pytest.mark.parametrize(
    "environ, message",
    [
        ({}, "DATABASE_URL is not set"),
        ({"DATABASE_URL": "  "}, "DATABASE_URL is not set"),
        ({"DATABASE_URL": "postgresql+psycopg://w:p@127.0.0.1:5432/studygraph"}, "local machine"),
        ({"DATABASE_URL": "postgresql+psycopg://w:p@localhost/studygraph"}, "local machine"),
        ({"DATABASE_URL": "postgresql+psycopg://w:p@host.docker.internal:5433/studygraph"}, "local machine"),
        ({"DATABASE_URL": GOOD, "PGHOSTADDR": "10.0.0.9"}, "PGHOSTADDR"),
        ({"DATABASE_URL": GOOD, "PGSERVICE": "other"}, "PGSERVICE"),
        ({"DATABASE_URL": GOOD, "PGOPTIONS": "-c search_path=evil"}, "PGOPTIONS"),
        ({"DATABASE_URL": GOOD + "?host=elsewhere"}, "query parameter"),
        ({"DATABASE_URL": "mysql://w:p@db-host.internal/studygraph"}, "PostgreSQL"),
        ({"DATABASE_URL": "postgresql+psycopg://db-host.internal/studygraph"}, "no user"),
        ({"DATABASE_URL": GOOD, "DATABASE_REQUIRE_COMMENT": "   "}, "set but empty"),
    ],
    ids=[
        "missing", "blank", "loopback-ip", "localhost", "docker-host-alias", "PGHOSTADDR", "PGSERVICE",
        "PGOPTIONS", "host-override-query", "not-postgres", "no-user", "blank-required-comment",
    ],
)  # fmt: skip
def test_unsafe_worker_targets_are_refused(environ, message):
    with pytest.raises(PreflightError, match=message) as caught:
        database_target(environ)
    assert "fake-worker-pw" not in str(caught.value)


def test_loopback_needs_the_explicit_port_allowance():
    environ = {"DATABASE_URL": "postgresql+psycopg://w:p@127.0.0.1:5433/studygraph"}
    with pytest.raises(PreflightError):
        database_target(environ)
    target = database_target({**environ, "MIGRATIONS_ALLOW_LOOPBACK_PORT": "5433"})
    assert target.url.port == 5433


def test_valid_target_is_canonical_and_never_shows_credentials():
    target = database_target({"DATABASE_URL": GOOD.replace(":5432", ""), "DATABASE_REQUIRE_COMMENT": "marker-x"})
    assert target.url.port == 5432  # made explicit, so PGPORT can never change it
    assert target.required_comment == "marker-x"
    assert "fake-worker-pw" not in repr(target)


def test_worker_preflight_refuses_an_unsafe_target_before_connecting():
    environ = {"DATABASE_URL": GOOD, "REDIS_URL": "redis://r:6379/0", "UPLOAD_DIR": "/tmp/x", "PGOPTIONS": "-c role=x"}
    with pytest.raises(PreflightError, match="PGOPTIONS"):
        runtime.worker_preflight(Settings(), environ)


# --- operational connections (disposable database) ------------------------------------------------


def _target(disposable_target, *, comment: str | None, database: str | None = None) -> DatabaseTarget:
    url = disposable_target.url if database is None else disposable_target.url.set(database=database)
    return DatabaseTarget(url=url, required_comment=comment)


def test_every_operational_connection_is_verified(world, disposable_target):
    engine = verified_engine(_target(disposable_target, comment=disposable_target.marker))
    try:
        calls = []

        @event.listens_for(engine, "engine_connect")
        def _count(connection):
            calls.append(1)

        factory = sessionmaker(bind=engine)
        for _ in range(3):
            with factory() as session:
                assert session.execute(text("SELECT 1")).scalar_one() == 1
        assert len(calls) == 3  # NullPool: a new, verified connection each time
    finally:
        engine.dispose()


@pytest.mark.parametrize("case", ["wrong-marker", "other-database"])
def test_a_wrong_database_is_refused_before_any_statement(world, disposable_target, case):
    if case == "wrong-marker":
        target = _target(disposable_target, comment="studygraph-disposable-test:not-this-one")
    else:  # same server and role, another database (it does not carry this run's marker)
        target = _target(disposable_target, comment=disposable_target.marker, database="postgres")
    engine = verified_engine(target)
    try:
        with pytest.raises(UnsafeMigrationTarget):
            with sessionmaker(bind=engine)() as session:
                session.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def test_a_worker_on_an_unverified_connection_cannot_touch_a_job(world, disposable_target, tmp_path):
    owner = world.user("professor")
    course = world.course(owner)
    bind_storage(world.engine, tmp_path / "store")
    store = LocalFileStorage(tmp_path / "store")
    doc = add_stored_document(world.engine, world, store, course, owner, make_pdf("guarded"))
    engine = verified_engine(_target(disposable_target, comment="studygraph-disposable-test:not-this-one"))
    ctx = WorkerContext(session_factory=sessionmaker(bind=engine), storage=store, settings=Settings(upload_dir=store.configured_root))
    try:
        try:
            result = pipeline.run_parse_job(doc.job_id, ctx)
        except UnsafeMigrationTarget:
            result = None
        assert result is None or result.outcome in {"database_unavailable", "storage_unavailable"}
    finally:
        engine.dispose()
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_count"], row["attempt_token"]) == ("queued", 0, None)


def test_the_worker_context_uses_the_verified_target(world, disposable_target, tmp_path, monkeypatch):
    environ = {
        "DATABASE_URL": disposable_target.url.render_as_string(hide_password=False),
        "DATABASE_REQUIRE_COMMENT": "studygraph-disposable-test:not-this-one",
    }
    runtime.reset_after_fork()
    try:
        ctx = runtime.context(Settings(upload_dir=tmp_path), celery_app=None, environ=environ)
        with pytest.raises(UnsafeMigrationTarget):
            with ctx.session_factory() as session:
                session.execute(text("SELECT 1"))
    finally:
        runtime.reset_after_fork()
    environ["DATABASE_REQUIRE_COMMENT"] = disposable_target.marker
    try:
        ctx = runtime.context(Settings(upload_dir=tmp_path), celery_app=None, environ=environ)
        with ctx.session_factory() as session:
            assert session.execute(text("SELECT current_database()")).scalar_one() == disposable_target.url.database
    finally:
        runtime.reset_after_fork()
