"""Fail-fast checks on the database a migration command is about to change.

Alembic runs inside the core-api container, where docker-compose sets
DATABASE_URL to the `db` service. Run anywhere else, an innocent-looking
configuration can point at a different server: a `localhost` URL on a
developer machine reaches whatever PostgreSQL listens there (possibly an
unrelated local installation), Docker Desktop's host aliases reach the same
machine from inside a container, and libpq silently fills in or overrides
connection settings from PG* environment variables.

Checked before any connection is opened (resolve_migration_url):

1. DATABASE_URL must be set in the process environment. Unlike the app
   settings, migrations never fall back to a .env file or a built-in default.
2. The URL must be PostgreSQL with an explicit user, a single plain host name
   or canonical IP address, a valid port, and a database name. Multi-host
   lists, socket paths, percent-encoded hosts and legacy numeric IPv4 forms
   (127.1, 2130706433, 0x7f000001, 0177.0.0.1) are refused. Only connection
   options that cannot change the target or the session identity (TLS,
   timeouts, application_name, ...) are accepted as query parameters.
3. The port is always passed to the driver explicitly (5432 when the URL has
   none), so PGPORT can never change it. Explicit host, port, user and
   database also override PGHOST, PGPORT, PGUSER and PGDATABASE. PGHOSTADDR,
   PGSERVICE and PGOPTIONS would still apply on top of the URL, so the
   command refuses to run while they are set.
4. Loopback, unspecified and Docker host-alias addresses are refused unless
   MIGRATIONS_ALLOW_LOOPBACK_PORT is set to exactly the port being used (for
   example a disposable CI service container published on the runner).

Checked on the migration's own connection before any DDL (verify_connection):

5. current_database() is the URL's database; current_user and session_user
   are the URL's user (no role switched in by role or database defaults);
   current_schema() is `public` (no search_path redirect).
6. If MIGRATIONS_REQUIRE_DATABASE_COMMENT is set, the database COMMENT must
   equal it exactly. The disposable-database tests use this so a destructive
   command verifies the marker on the very connection that runs it.

Nothing here prints a username or password: targets are described as
host:port/database only, and URL parse errors are reported without the URL.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import ArgumentError

DATABASE_URL_ENV = "DATABASE_URL"
ALLOW_LOOPBACK_PORT_ENV = "MIGRATIONS_ALLOW_LOOPBACK_PORT"
REQUIRE_COMMENT_ENV = "MIGRATIONS_REQUIRE_DATABASE_COMMENT"
DEFAULT_PORT = 5432
EXPECTED_SCHEMA = "public"

# Names that resolve to the machine the command runs on. host.docker.internal
# and gateway.docker.internal are Docker Desktop aliases for the host machine,
# so from inside a container they are as local as localhost is outside it.
_LOCAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)

# Query parameters passed through to libpq that cannot change which server,
# database, role or schema the session uses. Anything else (host, hostaddr,
# port, dbname, user, service, options, target_session_attrs, ...) is refused.
ALLOWED_QUERY_KEYS = frozenset(
    {
        "application_name",
        "channel_binding",
        "client_encoding",
        "connect_timeout",
        "gssencmode",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "require_auth",
        "ssl_max_protocol_version",
        "ssl_min_protocol_version",
        "sslcert",
        "sslcompression",
        "sslcrl",
        "sslcrldir",
        "sslkey",
        "sslmode",
        "sslpassword",
        "sslrootcert",
        "sslsni",
        "tcp_user_timeout",
    }
)

# libpq environment variables that still take effect when the URL names the
# host, port, user and database explicitly (confirmed for PGHOSTADDR with
# psycopg 3.2.3 / libpq 17; PGSERVICE and PGOPTIONS by libpq's documented
# precedence rules).
REFUSED_LIBPQ_ENV = ("PGHOSTADDR", "PGSERVICE", "PGOPTIONS")

# One host name or IP address: letters, digits, dots, hyphens, underscores and
# (for IPv6) colons. Excludes multi-host lists, socket paths and encodings.
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")

_IDENTITY_QUERY = text(
    """
    SELECT current_database(), current_user, session_user, current_schema(),
           shobj_description(d.oid, 'pg_database'), current_setting('server_version')
    FROM pg_database d
    WHERE d.datname = current_database()
    """
)


class UnsafeMigrationTarget(RuntimeError):
    """Raised when a migration command must not run against the given database."""


def describe_target(url: URL) -> str:
    """host:port/database, with no username or password."""
    return f"{url.host or '<no host>'}:{url.port or DEFAULT_PORT}/{url.database or '<no database>'}"


def _bare_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def _legacy_ipv4(name: str) -> ipaddress.IPv4Address | None:
    """The address libc would read from a non-canonical numeric IPv4 form."""
    try:
        ipaddress.ip_address(name)
        return None  # canonical form, handled by ipaddress itself
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(name))
    except OSError:
        return None


def is_local_host(host: str) -> bool:
    """True for loopback/unspecified addresses and names for the local machine."""
    name = _bare_host(host)
    if name in _LOCAL_HOSTNAMES or name.endswith(".localhost"):
        return True
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(name)
    except ValueError:
        address = _legacy_ipv4(name) if _HOST_PATTERN.match(name) else None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address is not None and (address.is_loopback or address.is_unspecified)


def parse_database_url(raw: str, *, variable: str = DATABASE_URL_ENV) -> URL:
    """Parse a URL without ever echoing it back (it may contain a password)."""
    try:
        return make_url(raw)
    except (ArgumentError, ValueError):
        # SQLAlchemy's messages can repeat parts of the URL; do not chain them.
        raise UnsafeMigrationTarget(f"{variable} is not a valid database URL.") from None


def validate_url(url: URL, *, variable: str = DATABASE_URL_ENV) -> URL:
    """Shape checks shared by migrations and the test guard.

    Returns the URL with its port made explicit, which is the URL that must be
    handed to the driver.
    """
    if url.get_backend_name() != "postgresql":
        raise UnsafeMigrationTarget(f"{variable} must point to PostgreSQL; got '{url.get_backend_name()}'.")
    if not url.host:
        raise UnsafeMigrationTarget(
            f"{variable} has no host. Name the database server explicitly "
            "instead of relying on a local socket or PG* environment variables."
        )
    if not url.database:
        raise UnsafeMigrationTarget(f"{variable} has no database name.")
    if not url.username:
        raise UnsafeMigrationTarget(
            f"{variable} has no user. Name it explicitly; otherwise libpq picks "
            "PGUSER or the operating-system user."
        )

    host = url.host.strip("[]")
    if not _HOST_PATTERN.match(host):
        raise UnsafeMigrationTarget(
            f"{variable} must name one plain host name or IP address. Multi-host "
            "lists, socket paths and encoded characters are not supported."
        )
    if _legacy_ipv4(_bare_host(host)) is not None:
        raise UnsafeMigrationTarget(
            f"{variable} uses a non-canonical numeric host; write IPv4 addresses "
            "in dotted-decimal form (a.b.c.d)."
        )

    port = url.port if url.port is not None else DEFAULT_PORT
    if not 1 <= port <= 65535:
        raise UnsafeMigrationTarget(f"{variable} has an invalid port.")

    for key, value in url.query.items():
        if key not in ALLOWED_QUERY_KEYS:
            raise UnsafeMigrationTarget(
                f"{variable} sets the query parameter '{key}', which is not allowed "
                "for migrations because it can change the server, database, role or "
                f"schema. Allowed: {', '.join(sorted(ALLOWED_QUERY_KEYS))}."
            )
        if not isinstance(value, str):
            raise UnsafeMigrationTarget(f"{variable} gives the query parameter '{key}' more than once.")

    return url.set(port=port)


def check_libpq_environment(environ: Mapping[str, str], *, variable: str = DATABASE_URL_ENV) -> None:
    """Refuse PG* variables that libpq would apply on top of an explicit URL."""
    active = [name for name in REFUSED_LIBPQ_ENV if environ.get(name, "").strip()]
    if active:
        raise UnsafeMigrationTarget(
            f"{', '.join(active)} is set in the environment. libpq applies it on top "
            f"of {variable} (PGHOSTADDR can send the connection to another address, "
            "PGSERVICE pulls settings from a service file, PGOPTIONS can switch the "
            "role or search_path). Unset it for migration commands."
        )


def check_migration_target(url: URL, environ: Mapping[str, str]) -> URL:
    """Validate a migration target; return the URL to hand to the driver."""
    url = validate_url(url)
    if is_local_host(url.host):
        allowed_port = environ.get(ALLOW_LOOPBACK_PORT_ENV, "").strip()
        if allowed_port != str(url.port):
            raise UnsafeMigrationTarget(
                f"Refusing to run migrations against {describe_target(url)}: that "
                "address is the local machine, which may run an unrelated "
                "PostgreSQL server. Run migrations inside the core-api container "
                "(docker compose exec core-api alembic ...). If you really mean "
                f"this local server, set {ALLOW_LOOPBACK_PORT_ENV} to its port. "
                "See docs/database-migrations.md."
            )
    check_libpq_environment(environ)
    return url


def resolve_migration_url(environ: Mapping[str, str]) -> URL:
    """Return the checked migration target from DATABASE_URL in `environ`."""
    raw = environ.get(DATABASE_URL_ENV, "").strip()
    if not raw:
        raise UnsafeMigrationTarget(
            f"{DATABASE_URL_ENV} is not set. Migrations read it from the "
            "environment only (never from a .env file or a default), so run "
            "them inside the core-api container, where docker-compose sets it: "
            "docker compose exec core-api alembic ..."
        )
    return check_migration_target(parse_database_url(raw), environ)


def required_database_comment(environ: Mapping[str, str]) -> str | None:
    """The database COMMENT the target must carry, if one is required."""
    if REQUIRE_COMMENT_ENV not in environ:
        return None
    value = environ[REQUIRE_COMMENT_ENV].strip()
    if not value:
        raise UnsafeMigrationTarget(f"{REQUIRE_COMMENT_ENV} is set but empty; refusing to guess.")
    return value


def check_connected_identity(
    url: URL,
    *,
    database: str,
    current_user: str,
    session_user: str,
    schema: str | None,
    comment: str | None,
    required_comment: str | None,
) -> None:
    """Compare what the server reports with what the configuration promised."""
    where = describe_target(url)
    if database != url.database:
        raise UnsafeMigrationTarget(f"{where}: the connection reached a different database.")
    if current_user != url.username or session_user != url.username:
        raise UnsafeMigrationTarget(
            f"{where}: the session runs as a different role than the URL names "
            "(check role or database defaults that SET ROLE)."
        )
    if schema != EXPECTED_SCHEMA:
        raise UnsafeMigrationTarget(
            f"{where}: the default schema is {schema!r}, expected '{EXPECTED_SCHEMA}' "
            "(check search_path defaults on the role or database)."
        )
    if required_comment is not None and comment != required_comment:
        raise UnsafeMigrationTarget(
            f"{where}: the database does not carry the COMMENT required by "
            f"{REQUIRE_COMMENT_ENV}; refusing to touch it."
        )


def verify_connection(connection: Connection, url: URL, required_comment: str | None) -> str:
    """Run the identity checks on an open connection; return the server version.

    The query opens a transaction; the caller decides whether to end it.
    """
    database, current_user, session_user, schema, comment, server_version = connection.execute(
        _IDENTITY_QUERY
    ).one()
    check_connected_identity(
        url,
        database=database,
        current_user=current_user,
        session_user=session_user,
        schema=schema,
        comment=comment,
        required_comment=required_comment,
    )
    return server_version
