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

Worker roles (M4). The role follows from the queues a worker consumes,
which Celery has selected before worker_init:
- processing (parse and/or maintenance queues): the checks above, unchanged.
- index (exactly the index queue, nothing else): DATABASE_URL, REDIS_URL and
  AI_SERVICE_URL (plus AI_SERVICE_TOKEN while indexing is enabled); the same
  database checks; a valid AI_SERVICE_URL. Start-up makes NO network call
  to the AI service: a probe here would run outside any Celery time limit.
  Readiness is probed inside the index task, at claim time (cached per
  process), where the task's soft and hard limits apply. It never needs,
  reads or checks the upload store.
- harvest (exactly the harvest queue, M6): DATABASE_URL and REDIS_URL, the
  same database checks, and while HARVEST_ENABLED a valid HARVEST_USER_AGENT.
  No upload store, no AI service, no provider request at start-up.
- mastery (exactly the mastery queue, M8): DATABASE_URL, REDIS_URL and
  AI_SERVICE_URL; while MASTERY_DELIVERY_ENABLED also AI_SERVICE_TOKEN and the
  complete pins (model, version, state epoch, pseudonym namespace and key,
  validated by Settings). No upload store; no AI request at start-up (the
  capability probe runs inside the task, under its time limits).
A worker that consumes the index, harvest or mastery queue together with any
other queue is refused: the roles need different credentials, mounts and egress.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from .. import storage_identity
from ..config import Settings
from ..database_compatibility import (
    REQUIRED_SCHEMA_REVISION,
    IncompatibleDatabase,
    check_database_compatibility,
    requires_unprivileged_role,
)
from ..migration_safety import UnsafeMigrationTarget, describe_target, resolve_migration_url, verify_connection
from ..storage import LocalFileStorage
from ..indexing.client import AiIndexClient, InvalidServiceUrl, parse_service_url
from .dispatch import CeleryPublisher
from .pipeline import WorkerContext

REQUIRE_COMMENT_ENV = "DATABASE_REQUIRE_COMMENT"
WORKER_ENVIRONMENT = ("DATABASE_URL", "REDIS_URL", "UPLOAD_DIR")
BEAT_ENVIRONMENT = ("REDIS_URL",)
INDEX_WORKER_ENVIRONMENT = ("DATABASE_URL", "REDIS_URL", "AI_SERVICE_URL")
HARVEST_WORKER_ENVIRONMENT = ("DATABASE_URL", "REDIS_URL")
MASTERY_WORKER_ENVIRONMENT = ("DATABASE_URL", "REDIS_URL", "AI_SERVICE_URL")
MIN_TOKEN_LENGTH = 32
ROLE_PROCESSING = "processing"
ROLE_INDEX = "index"
ROLE_HARVEST = "harvest"
ROLE_MASTERY = "mastery"
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


def check_database(target: DatabaseTarget, *, environment: str = "development") -> None:
    """Check application compatibility on an identity-verified worker connection.

    Outside development and test the worker's role must also be unprivileged (F05).
    """
    engine = verified_engine(target)
    try:
        with engine.connect() as connection:
            check_database_compatibility(connection, unprivileged=requires_unprivileged_role(environment))
    except (UnsafeMigrationTarget, IncompatibleDatabase) as exc:
        raise PreflightError(str(exc)) from None
    finally:
        engine.dispose()


def worker_preflight(settings: Settings, environ: Mapping[str, str] = os.environ) -> None:
    require_environment(environ, WORKER_ENVIRONMENT)
    target = database_target(environ)
    check_database(target, environment=settings.environment)
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


def worker_role(queues, settings: Settings) -> str:
    """The role of a worker consuming `queues` (names); refuses mixing the index queue with others."""
    names = set(queues or ())
    dedicated = ((settings.index_queue, ROLE_INDEX), (settings.harvest_queue, ROLE_HARVEST),
                 (settings.mastery_queue, ROLE_MASTERY))  # fmt: skip
    for queue, role in dedicated:
        if queue not in names:
            continue
        if names != {queue}:
            raise PreflightError(
                f"the {role} queue {queue!r} must be consumed by a dedicated worker "
                f"(this worker also consumes {', '.join(sorted(names - {queue}))})"
            )
        return role
    return ROLE_PROCESSING


def check_ai_settings(settings: Settings, environ: Mapping[str, str]) -> None:
    try:
        parse_service_url(environ.get("AI_SERVICE_URL", settings.ai_service_url))
    except InvalidServiceUrl as exc:
        raise PreflightError(str(exc)) from None
    if settings.indexing_enabled:
        token = environ.get("AI_SERVICE_TOKEN", "")
        if not token.strip():
            raise PreflightError("AI_SERVICE_TOKEN must be set explicitly while INDEXING_ENABLED is true")
        if len(token) < MIN_TOKEN_LENGTH or token != token.strip():
            raise PreflightError(f"AI_SERVICE_TOKEN must be at least {MIN_TOKEN_LENGTH} characters, without spaces")


def index_worker_preflight(settings: Settings, environ: Mapping[str, str] = os.environ) -> None:
    """Start-up checks of the index worker; it never touches the upload store."""
    require_environment(environ, INDEX_WORKER_ENVIRONMENT)
    check_ai_settings(settings, environ)
    target = database_target(environ)
    check_database(target, environment=settings.environment)
    log.info(
        "index worker preflight: database %s verified at revision %s",
        describe_target(target.url), REQUIRED_SCHEMA_REVISION,
    )  # fmt: skip
    if not settings.indexing_enabled:
        log.warning("index worker preflight: INDEXING_ENABLED is false; index jobs will not be claimed")
        return
    log.info("index worker preflight: AI service readiness is checked per claim, inside the index task")


def harvest_worker_preflight(settings: Settings, environ: Mapping[str, str] = os.environ) -> None:
    """Start-up checks of the harvest worker: database only; no provider request is made here."""
    require_environment(environ, HARVEST_WORKER_ENVIRONMENT)
    if settings.harvest_enabled:
        from ..harvest.transport import validate_user_agent

        agent = environ.get("HARVEST_USER_AGENT", "")
        if not agent.strip():
            raise PreflightError("HARVEST_USER_AGENT must be set explicitly while HARVEST_ENABLED is true")
        try:
            validate_user_agent(agent)
        except ValueError as exc:
            raise PreflightError(str(exc)) from None
    target = database_target(environ)
    check_database(target, environment=settings.environment)
    log.info(
        "harvest worker preflight: database %s verified at revision %s",
        describe_target(target.url), REQUIRED_SCHEMA_REVISION,
    )  # fmt: skip
    if not settings.harvest_enabled:
        log.warning("harvest worker preflight: HARVEST_ENABLED is false; harvest runs will not be claimed")


def mastery_worker_preflight(settings: Settings, environ: Mapping[str, str] = os.environ) -> None:
    """Start-up checks of the mastery worker: database and configuration only; no AI request here."""
    require_environment(environ, MASTERY_WORKER_ENVIRONMENT)
    try:
        parse_service_url(environ.get("AI_SERVICE_URL", settings.ai_service_url))
    except InvalidServiceUrl as exc:
        raise PreflightError(str(exc)) from None
    if settings.mastery_delivery_enabled:
        token = environ.get("AI_SERVICE_TOKEN", "")
        if not token.strip():
            raise PreflightError("AI_SERVICE_TOKEN must be set explicitly while MASTERY_DELIVERY_ENABLED is true")
        if len(token) < MIN_TOKEN_LENGTH or token != token.strip():
            raise PreflightError(f"AI_SERVICE_TOKEN must be at least {MIN_TOKEN_LENGTH} characters, without spaces")
        from ..mastery.pins import pins_of

        if pins_of(settings) is None:  # Settings already refused malformed or missing pins; defence in depth
            raise PreflightError("the mastery pins must be complete while MASTERY_DELIVERY_ENABLED is true")
    target = database_target(environ)
    check_database(target, environment=settings.environment)
    log.info(
        "mastery worker preflight: database %s verified at revision %s",
        describe_target(target.url), REQUIRED_SCHEMA_REVISION,
    )  # fmt: skip
    if not settings.mastery_delivery_enabled:
        log.warning("mastery worker preflight: MASTERY_DELIVERY_ENABLED is false; no stream will be claimed")


def preflight_for_role(role: str, settings: Settings, environ: Mapping[str, str] = os.environ) -> None:
    if role == ROLE_MASTERY:
        mastery_worker_preflight(settings, environ)
    elif role == ROLE_INDEX:
        index_worker_preflight(settings, environ)
    elif role == ROLE_HARVEST:
        harvest_worker_preflight(settings, environ)
    else:
        worker_preflight(settings, environ)


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

_state: dict = {
    "pid": None, "engine": None, "context": None, "publisher": None, "index_context": None, "harvest_context": None,
    "mastery_context": None,
}  # fmt: skip


def reset_after_fork() -> None:
    engine = _state.get("engine")
    if engine is not None:
        engine.dispose(close=False)  # never close connections that belong to the parent
    _state.update(
        pid=None, engine=None, context=None, publisher=None, index_context=None, harvest_context=None,
        mastery_context=None,
    )  # fmt: skip


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
        index_context=None,
        harvest_context=None,
        mastery_context=None,
    )


def context(settings: Settings, celery_app, environ: Mapping[str, str] = os.environ) -> WorkerContext:
    _ensure_process_state(settings, celery_app, environ)
    return _state["context"]


def index_context(settings: Settings, celery_app, environ: Mapping[str, str] = os.environ):
    """The index worker's per-process context: verified database sessions and the AI client.

    No storage object is involved. The readiness cache lives in the client,
    so it is per pool process.
    """
    from ..indexing.pipeline import IndexContext

    _ensure_process_state(settings, celery_app, environ)
    if _state["index_context"] is None:
        _state["index_context"] = IndexContext(
            session_factory=_state["context"].session_factory, settings=settings, client=AiIndexClient(settings)
        )
    return _state["index_context"]


def harvest_context(settings: Settings, celery_app, environ: Mapping[str, str] = os.environ):
    """The harvest worker's per-process context: verified sessions and the Wikipedia/OpenStax connector factories.

    The storage object of the shared context is never used by harvesting.
    """
    from ..harvest.executor import HarvestContext
    from ..harvest.openstax import OpenStaxConnector
    from ..harvest.transport import ProviderTransport
    from ..harvest.wikipedia import WikipediaConnector

    _ensure_process_state(settings, celery_app, environ)
    if _state["harvest_context"] is None:
        transports: list[ProviderTransport] = []

        def transport() -> ProviderTransport:
            # Built on first use (after the HARVEST_ENABLED and gate checks), with the
            # validated User-Agent; never at worker start-up.
            if not transports:
                transports.append(ProviderTransport(settings.harvest_user_agent or ""))
            return transports[0]

        def connector(language: str) -> WikipediaConnector:
            return WikipediaConnector(transport(), settings, language)

        def openstax_connector(book: str | None) -> OpenStaxConnector:
            return OpenStaxConnector(transport(), settings, book)

        _state["harvest_context"] = HarvestContext(
            session_factory=_state["context"].session_factory, settings=settings, connector_factory=connector,
            openstax_factory=openstax_connector,
        )  # fmt: skip
    return _state["harvest_context"]


def mastery_context(settings: Settings, celery_app, environ: Mapping[str, str] = os.environ):
    """The mastery worker's per-process context: verified sessions and the mastery client (probe cache inside)."""
    from ..mastery.client import AiMasteryClient
    from ..mastery.runner import MasteryContext

    _ensure_process_state(settings, celery_app, environ)
    if _state["mastery_context"] is None:
        _state["mastery_context"] = MasteryContext(
            session_factory=_state["context"].session_factory, settings=settings, client=AiMasteryClient(settings)
        )
    return _state["mastery_context"]


def stream_of(args: tuple, kwargs: dict) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(user, course) of a mastery message, or None unless it is exactly {"user_id", "course_id"} canonical UUIDs."""
    if args or set(kwargs) != {"user_id", "course_id"}:
        return None
    ids = []
    for key in ("user_id", "course_id"):
        value = kwargs[key]
        if not isinstance(value, str):
            return None
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            return None
        if str(parsed) != value:
            return None
        ids.append(parsed)
    return ids[0], ids[1]


def run_id_of(args: tuple, kwargs: dict) -> uuid.UUID | None:
    """The run id of a harvest message, or None unless it is exactly {"run_id": "<canonical uuid>"}."""
    if args or set(kwargs) != {"run_id"} or not isinstance(kwargs["run_id"], str):
        return None
    try:
        run_id = uuid.UUID(kwargs["run_id"])
    except ValueError:
        return None
    return run_id if str(run_id) == kwargs["run_id"] else None


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
