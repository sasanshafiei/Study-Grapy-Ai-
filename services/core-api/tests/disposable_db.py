"""Safeguards for the tests that migrate, downgrade and write to a database.

Those tests are destructive (`alembic downgrade base` drops every table), so
they only run against a disposable database that proves its identity in
several independent ways:

1. Explicit configuration. TEST_DATABASE_URL and TEST_DATABASE_TOKEN must
   both be set. There is no fallback to DATABASE_URL or the app settings: with
   neither set the database tests are skipped, with only one set they error.
2. Static checks on the URL: the same shape rules as migrations
   (app.migration_safety.validate_url), a database name starting with
   `sg_disposable_`, a host that is a separate server reached by name (never
   this machine or a Docker host alias, never the docker-compose development
   database service `db`), a target different from DATABASE_URL, and none of
   the PG* variables that libpq would apply on top of the URL.
3. Checks on the live connection (app.migration_safety.verify_connection):
   database, current_user/session_user and schema match the URL, and the
   database COMMENT equals `studygraph-disposable-test:<TEST_DATABASE_TOKEN>`.
   scripts/test_migrations.sh writes that comment when it creates the
   throwaway server, outside the test process, with a fresh random token.

Where the checks run:
- In-process test connections verify on the connection they then use.
- Alembic commands run in a subprocess that receives the marker through
  MIGRATIONS_REQUIRE_DATABASE_COMMENT; migrations/env.py verifies it on the
  migration's own connection before any DDL. The fixture additionally checks
  the database on a separate connection first (defence in depth).

Error messages never contain the password or the token.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.engine import URL, Connection

from app.migration_safety import (
    DATABASE_URL_ENV,
    UnsafeMigrationTarget,
    check_connected_identity,
    check_libpq_environment,
    describe_target,
    is_local_host,
    parse_database_url,
    validate_url,
    verify_connection as verify_migration_connection,
)

TEST_URL_ENV = "TEST_DATABASE_URL"
TEST_TOKEN_ENV = "TEST_DATABASE_TOKEN"
DISPOSABLE_NAME_PREFIX = "sg_disposable_"
MARKER_PREFIX = "studygraph-disposable-test:"
MIN_TOKEN_LENGTH = 16
# Service name of the development database in docker-compose.yml.
DEV_DATABASE_HOSTS = frozenset({"db"})


class MissingTestDatabase(Exception):
    """No test database is configured; database tests are skipped."""


class NotDisposable(RuntimeError):
    """The configured test database failed a safety check; tests must stop."""


@dataclass(frozen=True)
class DisposableTarget:
    url: URL  # validated, with an explicit port
    token: str

    @property
    def marker(self) -> str:
        return MARKER_PREFIX + self.token

    def __repr__(self) -> str:  # never show the password or token in test output
        return f"DisposableTarget({describe_target(self.url)})"


def target_from_env(environ: Mapping[str, str]) -> DisposableTarget:
    """Read and statically check the disposable test database configuration."""
    raw = environ.get(TEST_URL_ENV, "").strip()
    token = environ.get(TEST_TOKEN_ENV, "").strip()
    if not raw and not token:
        raise MissingTestDatabase(f"{TEST_URL_ENV} and {TEST_TOKEN_ENV} are not set")
    if not raw or not token:
        raise NotDisposable(f"Set both {TEST_URL_ENV} and {TEST_TOKEN_ENV}, not only one of them.")
    if len(token) < MIN_TOKEN_LENGTH:
        raise NotDisposable(f"{TEST_TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters long.")
    try:
        url = parse_database_url(raw, variable=TEST_URL_ENV)
    except UnsafeMigrationTarget as exc:
        raise NotDisposable(str(exc)) from None
    return DisposableTarget(url=check_url(url, environ), token=token)


def check_url(url: URL, environ: Mapping[str, str]) -> URL:
    """Static checks that need no connection; returns the URL with an explicit port."""
    try:
        url = validate_url(url, variable=TEST_URL_ENV)
        check_libpq_environment(environ, variable=TEST_URL_ENV)
    except UnsafeMigrationTarget as exc:
        raise NotDisposable(str(exc)) from None

    where = describe_target(url)
    if not url.database.startswith(DISPOSABLE_NAME_PREFIX):
        raise NotDisposable(f"{where}: the database name must start with {DISPOSABLE_NAME_PREFIX!r}.")
    if is_local_host(url.host):
        raise NotDisposable(
            f"{where}: the test database must be a separate server reached by name, not this machine."
        )
    if url.host.lower() in DEV_DATABASE_HOSTS:
        raise NotDisposable(f"{where}: that is the docker-compose development database service.")

    app_raw = environ.get(DATABASE_URL_ENV, "").strip()
    if app_raw:
        try:
            app_url = validate_url(parse_database_url(app_raw))
        except UnsafeMigrationTarget:
            app_url = None
        if app_url is not None and _same_target(url, app_url):
            raise NotDisposable(f"{TEST_URL_ENV} points at the same database as {DATABASE_URL_ENV}.")
    return url


def _same_target(a: URL, b: URL) -> bool:
    def key(url: URL) -> tuple[str, int | None, str | None]:
        return ((url.host or "").strip("[]").lower(), url.port, url.database)

    return key(a) == key(b)


def check_identity(
    target: DisposableTarget,
    *,
    database: str,
    current_user: str,
    session_user: str,
    schema: str | None,
    comment: str | None,
) -> None:
    """Compare what the server reports with what the configuration promised."""
    try:
        check_connected_identity(
            target.url,
            database=database,
            current_user=current_user,
            session_user=session_user,
            schema=schema,
            comment=comment,
            required_comment=target.marker,
        )
    except UnsafeMigrationTarget as exc:
        raise NotDisposable(str(exc)) from None


def verify_connection(connection: Connection, target: DisposableTarget) -> None:
    """Run the live identity checks on an open connection (the one about to be used)."""
    try:
        verify_migration_connection(connection, target.url, target.marker)
    except UnsafeMigrationTarget as exc:
        raise NotDisposable(str(exc)) from None
