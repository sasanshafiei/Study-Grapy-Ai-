"""Worker process context and start-up checks.

Database target (M3 review fix). Workers take their database only from
DATABASE_URL in the process environment and validate it with the same
primitives as migrations (app/migration_safety.py): PostgreSQL URL shape, an
explicit port, no loopback or Docker host-alias address unless
MIGRATIONS_ALLOW_LOOPBACK_PORT names that port, and none of PGHOSTADDR,
PGSERVICE, PGOPTIONS (which libpq would apply on top of the URL). The
operational engine is built from that validated URL, and *every* connection
it opens is verified before anything runs on it: the database, role and
schema are the ones the URL names and, when DATABASE_REQUIRE_COMMENT is set,
the database carries exactly that COMMENT. A variable set to an empty value is
an error, never "no check".

Prefork safety: the worker's main process never keeps a connection (the
preflight disposes its engine), and each pool process builds its own engine
lazily, keyed by its pid, with NullPool: no connection or pool is ever shared
across fork. worker_process_init also drops anything a child might have
inherited.

Start-up checks (worker_preflight), run before the worker consumes anything:
- DATABASE_URL, REDIS_URL and UPLOAD_DIR must be set in the process
  environment: a worker never falls back to built-in defaults or a .env file.
- The database target is valid (above), reachable, verified, and at the
  schema revision this code needs. Workers never run migrations.
- The storage identity is checked and reported; it is not required at start
  (an operator may initialize the store after starting the worker), because
  every claim checks it again and fails closed.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from .. import storage_identity
from ..config import Settings
from ..migration_safety import UnsafeMigrationTarget, describe_target, resolve_migration_url, verify_connection
from ..storage import LocalFileStorage
from .dispatch import CeleryPublisher
from .pipeline import WorkerContext

REQUIRED_SCHEMA_REVISION = "0003"
REQUIRE_COMMENT_ENV = "DATABASE_REQUIRE_COMMENT"
WORKER_ENVIRONMENT = ("DATABASE_URL", "REDIS_URL", "UPLOAD_DIR")
BEAT_ENVIRONMENT = ("REDIS_URL",)
EX_CONFIG = 78

log = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    pass


def require_environment(environ: Mapping[str, str], names: tuple[str, ...]) -> None:
    missing = [name for name in names if not environ.get(name, "").strip()]
    if missing:
        raise PreflightError(
            f"{', '.join(missing)} must be set explicitly in the environment; "
            "workers and Beat never use built-in defaults"
        )


@dataclass(frozen=True)
class DatabaseTarget:
    url: URL  # validated, with an explicit port
    required_comment: str | None

    def __repr__(self) -> str:  # never shows credentials
        return f"DatabaseTarget({describe_target(self.url)})"


def database_target(environ: Mapping[str, str]) -> DatabaseTarget:
    """The validated database target of a worker, from the environment only."""
    try:
        url = resolve_migration_url(environ)
    except UnsafeMigrationTarget as exc:
        raise PreflightError(str(exc)) from None
    required: str | None = None
    if REQUIRE_COMMENT_ENV in environ:
        required = environ[REQUIRE_COMMENT_ENV].strip()
        if not required:
            raise PreflightError(f"{REQUIRE_COMMENT_ENV} is set but empty; refusing to guess")
    return DatabaseTarget(url=url, required_comment=required)


def verified_engine(target: DatabaseTarget) -> Engine:
    """A NullPool engine for the target whose every connection is identity-checked before use.

    A failed check raises UnsafeMigrationTarget from the first statement of
    the session, so nothing (no claim, no job or document write) runs on a
    connection to the wrong database.
    """
    engine = create_engine(target.url, poolclass=NullPool, connect_args={"connect_timeout": 10})

    @event.listens_for(engine, "engine_connect")
    def _verify(connection: Connection) -> None:
        verify_connection(connection, target.url, target.required_comment)
        connection.rollback()

    return engine


def check_database(target: DatabaseTarget) -> None:
    """Reach the target through a verified connection and check its schema revision."""
    engine = verified_engine(target)
    try:
        with engine.connect() as connection:
            exists = connection.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one()
            revision = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
                if exists
                else None
            )
            connection.rollback()
    except UnsafeMigrationTarget as exc:
        raise PreflightError(str(exc)) from None
    finally:
        engine.dispose()
    if revision != REQUIRED_SCHEMA_REVISION:
        raise PreflightError(
            f"database schema is at {revision or 'no revision'}, this worker needs {REQUIRED_SCHEMA_REVISION}; "
            "run the migration first (workers never migrate)"
        )


def worker_preflight(settings: Settings, environ: Mapping[str, str] = os.environ) -> None:
    require_environment(environ, WORKER_ENVIRONMENT)
    target = database_target(environ)
    check_database(target)
    log.info("worker preflight: database %s verified at revision %s", describe_target(target.url), REQUIRED_SCHEMA_REVISION)
    engine = verified_engine(target)
    try:
        with sessionmaker(bind=engine)() as session:
            storage_id = storage_identity.verify(session, LocalFileStorage(settings.upload_dir))
        log.info("worker preflight: upload store %s verified", storage_id)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal: every claim checks it again
        log.error("worker preflight: upload store identity not established (%s); jobs will not be claimed", exc)
    finally:
        engine.dispose()


def beat_preflight(environ: Mapping[str, str] = os.environ) -> None:
    require_environment(environ, BEAT_ENVIRONMENT)


def exit_on_failure(check, *args) -> None:
    """Run a start-up check; on failure log it and end the process before it consumes anything."""
    try:
        check(*args)
    except Exception as exc:  # noqa: BLE001
        log.critical("start-up check failed: %s", exc)
        logging.shutdown()
        os._exit(EX_CONFIG)


# --- per-process context ---------------------------------------------------------------------

_state: dict = {"pid": None, "engine": None, "context": None, "publisher": None}


def reset_after_fork() -> None:
    engine = _state.get("engine")
    if engine is not None:
        engine.dispose(close=False)  # never close connections that belong to the parent
    _state.update(pid=None, engine=None, context=None, publisher=None)


def _ensure_process_state(settings: Settings, celery_app, environ: Mapping[str, str]) -> None:
    if _state["pid"] == os.getpid():
        return
    reset_after_fork()
    engine = verified_engine(database_target(environ))
    _state.update(
        pid=os.getpid(),
        engine=engine,
        context=WorkerContext(
            session_factory=sessionmaker(bind=engine, autoflush=False),
            storage=LocalFileStorage(settings.upload_dir),
            settings=settings,
        ),
        publisher=CeleryPublisher(celery_app, settings),
    )


def context(settings: Settings, celery_app, environ: Mapping[str, str] = os.environ) -> WorkerContext:
    _ensure_process_state(settings, celery_app, environ)
    return _state["context"]


def publisher(settings: Settings, celery_app, environ: Mapping[str, str] = os.environ) -> CeleryPublisher:
    _ensure_process_state(settings, celery_app, environ)
    return _state["publisher"]


def parse_job_id(args: tuple, kwargs: dict) -> uuid.UUID | None:
    """The job id of a parse message, or None if the message is not exactly {"job_id": "<uuid>"}."""
    if args or set(kwargs) != {"job_id"} or not isinstance(kwargs["job_id"], str):
        return None
    try:
        job_id = uuid.UUID(kwargs["job_id"])
    except ValueError:
        return None
    return job_id if str(job_id) == kwargs["job_id"] else None
