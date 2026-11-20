"""Operator commands. Run them inside a core-api or worker container:

    python -m app.ingestion.admin preflight [--role processing|index]
    python -m app.ingestion.admin init-storage --database NAME [--adopt-existing [--allow-missing]]
    python -m app.ingestion.admin retry-parse --database NAME DOCUMENT_ID
    python -m app.ingestion.admin retry-index --database NAME DOCUMENT_ID

preflight runs the worker start-up checks and changes nothing.

The commands that change state take their target only from the process
environment, never from defaults or a .env file:
- DATABASE_URL, validated by the same rules as migrations
  (app/migration_safety.py: no loopback without MIGRATIONS_ALLOW_LOOPBACK_PORT,
  no PGHOSTADDR/PGSERVICE/PGOPTIONS, ...); every connection is checked for the
  database name, role and schema it promises, and for the COMMENT in
  MIGRATIONS_REQUIRE_DATABASE_COMMENT when that is set;
- --database must repeat the database name;
- init-storage also needs UPLOAD_DIR set explicitly.
They print what they did; nothing printed contains credentials.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .. import storage_identity
from ..config import Settings
from ..migration_safety import (
    UnsafeMigrationTarget,
    describe_target,
    required_database_comment,
    resolve_migration_url,
    verify_connection,
)
from ..storage import LocalFileStorage
from . import jobs, runtime


class AdminError(RuntimeError):
    pass


def guarded_session_factory(database: str, environ: Mapping[str, str]):
    """A session factory for the database DATABASE_URL names, verified on every connection."""
    try:
        url = resolve_migration_url(environ)
        required_comment = required_database_comment(environ)
    except UnsafeMigrationTarget as exc:
        raise AdminError(str(exc)) from None
    if url.database != database:
        raise AdminError(f"--database {database!r} does not match the target {describe_target(url)}")
    engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 10})

    @event.listens_for(engine, "engine_connect")
    def _verify(connection: Connection) -> None:
        verify_connection(connection, url, required_comment)
        connection.rollback()

    return sessionmaker(bind=engine, autoflush=False), engine, describe_target(url)


def cmd_preflight(args, environ: Mapping[str, str]) -> int:
    runtime.preflight_for_role(args.role, Settings(), environ)
    print(f"preflight passed (role {args.role})")
    return 0


def cmd_init_storage(args, environ: Mapping[str, str]) -> int:
    upload_dir = environ.get("UPLOAD_DIR", "").strip()
    if not upload_dir:
        raise AdminError("UPLOAD_DIR must be set explicitly")
    _, engine, target = guarded_session_factory(args.database, environ)
    try:
        result = storage_identity.initialize(
            engine,
            LocalFileStorage(upload_dir),
            adopt_existing=args.adopt_existing,
            allow_missing=args.allow_missing,
        )
    finally:
        engine.dispose()
    print(
        f"{result.action}: database {target} is bound to upload store {result.storage_id} at {upload_dir} "
        f"({result.stored_documents} stored documents, {result.missing_files} missing files)"
    )
    return 0


def cmd_retry_parse(args, environ: Mapping[str, str]) -> int:
    try:
        document_id = uuid.UUID(args.document_id)
    except ValueError:
        raise AdminError("DOCUMENT_ID must be a UUID") from None
    session_factory: sessionmaker[Session]
    session_factory, engine, target = guarded_session_factory(args.database, environ)
    try:
        job_id = jobs.retry_parse(session_factory, document_id)
    except jobs.RetryRefused as exc:
        raise AdminError(f"retry refused: {exc}") from None
    finally:
        engine.dispose()
    print(f"queued parse job {job_id} for document {document_id} in {target}; the dispatch sweep publishes it")
    return 0


def cmd_retry_index(args, environ: Mapping[str, str]) -> int:
    try:
        document_id = uuid.UUID(args.document_id)
    except ValueError:
        raise AdminError("DOCUMENT_ID must be a UUID") from None
    session_factory: sessionmaker[Session]
    session_factory, engine, target = guarded_session_factory(args.database, environ)
    try:
        job_id = jobs.retry_index(session_factory, document_id)
    except jobs.RetryRefused as exc:
        raise AdminError(f"retry refused: {exc}") from None
    finally:
        engine.dispose()
    print(
        f"queued index job {job_id} for document {document_id} in {target}; no extraction is repeated; "
        "the dispatch sweep publishes it while indexing is enabled"
    )
    return 0


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight", help="run the worker start-up checks (changes nothing)")
    pre.add_argument(
        "--role", choices=(runtime.ROLE_PROCESSING, runtime.ROLE_INDEX, runtime.ROLE_HARVEST),
        default=runtime.ROLE_PROCESSING,
        help="processing (parse/maintenance worker, default), index (index worker) or harvest (harvest worker)",
    )
    init = sub.add_parser("init-storage", help="bind the database to the upload store (explicit, once)")
    init.add_argument("--database", required=True, help="the database name DATABASE_URL must point to")
    init.add_argument("--adopt-existing", action="store_true", help="the store or the database already holds uploads")
    init.add_argument("--allow-missing", action="store_true", help="with --adopt-existing: bind even if files are missing")
    retry = sub.add_parser("retry-parse", help="queue a new parse job for a failed document")
    retry.add_argument("--database", required=True, help="the database name DATABASE_URL must point to")
    retry.add_argument("document_id")
    retry_index = sub.add_parser("retry-index", help="queue a new index job for a document whose indexing failed")
    retry_index.add_argument("--database", required=True, help="the database name DATABASE_URL must point to")
    retry_index.add_argument("document_id")
    args = parser.parse_args(argv)
    if getattr(args, "allow_missing", False) and not args.adopt_existing:
        parser.error("--allow-missing requires --adopt-existing")
    commands = {
        "preflight": cmd_preflight,
        "init-storage": cmd_init_storage,
        "retry-parse": cmd_retry_parse,
        "retry-index": cmd_retry_index,
    }
    try:
        return commands[args.command](args, environ)
    except (AdminError, UnsafeMigrationTarget, runtime.PreflightError, storage_identity.StorageIdentityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
