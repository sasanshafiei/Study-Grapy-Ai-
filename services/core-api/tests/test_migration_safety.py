"""The migration target guard (app.migration_safety) and the alembic CLI around it.

None of these tests opens a database connection. The CLI refusal tests only
use targets that, inside a container, could not reach a real server even if
the guard were broken (loopback is the container itself; `db` does not resolve
on the test network). Host aliases such as host.docker.internal are covered
by unit tests only.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.migration_safety import (
    ALLOW_LOOPBACK_PORT_ENV,
    REQUIRE_COMMENT_ENV,
    UnsafeMigrationTarget,
    check_connected_identity,
    check_migration_target,
    describe_target,
    is_local_host,
    required_database_comment,
    resolve_migration_url,
)
from tests.alembic_cli import BASELINE_REVISION, CORE_API_DIR, head_revision, run_alembic, script_directory

SECRET = "Sup3r-Secret-Pw"


def _url(host_part: str, database: str = "studygraph", user: str = "studygraph") -> str:
    credentials = f"{user}:{SECRET}@" if user else ""
    return f"postgresql+psycopg://{credentials}{host_part}/{database}"


def _target_id(raw_url: str) -> str:
    """Test id without credentials: backend, host:port/database and query keys."""
    try:
        url = make_url(raw_url)
    except ValueError:
        return "unparseable"
    query = "?" + "&".join(sorted(url.query)) if url.query else ""
    return f"{url.get_backend_name()}:{url.host or '-'}:{url.port or '-'}/{url.database or '-'}{query}"


# --- local-machine detection ----------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "localhost.localdomain",
        "api.localhost",
        "127.0.0.1",
        "127.0.0.2",
        "127.1",
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
        "::1",
        "[::1]",
        "0:0:0:0:0:0:0:1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "host.docker.internal",
        "gateway.docker.internal",
    ],
)
def test_local_hosts_are_recognised(host):
    assert is_local_host(host)


@pytest.mark.parametrize("host", ["db", "postgres", "db.internal.example", "10.0.0.5", "studygraph-migtest-x-db"])
def test_remote_hosts_are_not_local(host):
    assert not is_local_host(host)


# --- URL validation -------------------------------------------------------------

REFUSED_URLS = [
    # this machine, in canonical and non-canonical spellings
    _url("localhost:5432"),
    _url("localhost"),
    _url("127.0.0.1:5433"),
    _url("[::1]:5432"),
    _url("[0:0:0:0:0:0:0:1]:5432"),
    _url("[::ffff:127.0.0.1]:5432"),
    _url("0.0.0.0:5432"),
    _url("host.docker.internal:5432"),
    _url("127.1:5432"),
    _url("2130706433:5432"),
    _url("0x7f000001:5432"),
    _url("0177.0.0.1:5432"),
    _url("1.2.3:5432"),  # non-canonical even when not loopback
    # more than one host, or no TCP host at all
    _url("db,localhost:5432"),
    _url("db,replica:5432"),
    _url("%2Ftmp:5432"),
    "postgresql+psycopg:///studygraph",
    # missing or invalid parts
    _url("db:5432", database=""),
    _url("db:5432", user=""),
    _url("db:abc"),
    _url("db:0"),
    _url("db:70000"),
    # query parameters that change target, identity or schema
    _url("db:5432") + "?host=localhost",
    _url("db:5432") + "?hostaddr=127.0.0.1",
    _url("db:5432") + "?port=5433",
    _url("db:5432") + "?dbname=postgres",
    _url("db:5432") + "?user=postgres",
    _url("db:5432") + "?service=prod",
    _url("db:5432") + "?options=-csearch_path%3Dother",
    _url("db:5432") + "?target_session_attrs=any",
    _url("db:5432") + "?sslmode=require&sslmode=disable",
    "sqlite:///studygraph.db",
]


@pytest.mark.parametrize("raw_url", REFUSED_URLS, ids=_target_id)
def test_unsafe_targets_are_refused_without_leaking_the_password(raw_url):
    with pytest.raises(UnsafeMigrationTarget) as info:
        resolve_migration_url({"DATABASE_URL": raw_url})
    assert SECRET not in str(info.value)


@pytest.mark.parametrize(
    "raw_url",
    [
        _url("db:5432"),  # the docker-compose service, as set inside core-api
        _url("db"),
        _url("10.0.0.5:5432"),
        _url("postgres.staging.example:6543", database="studygraph_staging"),
        _url("postgres.staging.example:6543")
        + "?sslmode=verify-full&sslrootcert=/certs/ca.pem&connect_timeout=5&application_name=alembic",
    ],
    ids=_target_id,
)
def test_explicit_remote_targets_are_allowed(raw_url):
    url = resolve_migration_url({"DATABASE_URL": raw_url})
    assert url.port is not None


def test_the_driver_always_gets_an_explicit_port():
    """libpq would otherwise use PGPORT (confirmed with psycopg 3.2.3 / libpq 17)."""
    url = resolve_migration_url({"DATABASE_URL": _url("db"), "PGPORT": "5433"})
    assert url.port == 5432
    connect_kwargs = create_engine(url).dialect.create_connect_args(url)[1]
    assert (connect_kwargs["host"], connect_kwargs["port"]) == ("db", 5432)
    assert (connect_kwargs["dbname"], connect_kwargs["user"]) == ("studygraph", "studygraph")


def test_loopback_needs_the_exact_effective_port_opt_in():
    check_migration_target(make_url(_url("localhost:5433")), {ALLOW_LOOPBACK_PORT_ENV: "5433"})

    for allowed in ("5432", "", "yes", "1"):
        with pytest.raises(UnsafeMigrationTarget):
            check_migration_target(make_url(_url("localhost:5433")), {ALLOW_LOOPBACK_PORT_ENV: allowed})

    # No port in the URL means 5432, whatever PGPORT says; an opt-in for 5433
    # does not open it.
    with pytest.raises(UnsafeMigrationTarget):
        check_migration_target(make_url(_url("localhost")), {ALLOW_LOOPBACK_PORT_ENV: "5433", "PGPORT": "5433"})
    url = check_migration_target(make_url(_url("localhost")), {ALLOW_LOOPBACK_PORT_ENV: "5432", "PGPORT": "5433"})
    assert url.port == 5432


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS"])
def test_libpq_variables_that_override_the_url_are_refused(variable):
    value = {"PGHOSTADDR": "127.0.0.1", "PGSERVICE": "prod", "PGOPTIONS": "-c search_path=other"}[variable]
    with pytest.raises(UnsafeMigrationTarget, match=variable):
        resolve_migration_url({"DATABASE_URL": _url("db:5432"), variable: value})
    # An empty value is the same as unset.
    resolve_migration_url({"DATABASE_URL": _url("db:5432"), variable: "  "})


def test_libpq_defaults_that_explicit_values_override_are_tolerated():
    """PGHOST/PGPORT/PGUSER/PGDATABASE lose to the explicit URL values (see the DB test)."""
    environ = {"DATABASE_URL": _url("db:5432"), "PGHOST": "localhost", "PGPORT": "1", "PGUSER": "x", "PGDATABASE": "y"}
    assert describe_target(resolve_migration_url(environ)) == "db:5432/studygraph"


def test_missing_database_url_is_refused():
    with pytest.raises(UnsafeMigrationTarget, match="DATABASE_URL is not set"):
        resolve_migration_url({})
    with pytest.raises(UnsafeMigrationTarget, match="DATABASE_URL is not set"):
        resolve_migration_url({"DATABASE_URL": "   "})


@pytest.mark.parametrize(
    "raw",
    [f"not a url {SECRET}", f"postgresql+psycopg://u:{SECRET}@db:abc/x"],
    ids=["free-text", "non-numeric-port"],
)
def test_unparseable_url_is_refused_without_echoing_it(raw):
    with pytest.raises(UnsafeMigrationTarget, match="not a valid database URL") as info:
        resolve_migration_url({"DATABASE_URL": raw})
    assert SECRET not in str(info.value)


def test_describe_target_never_includes_credentials():
    described = describe_target(make_url(_url("db:5432")))
    assert described == "db:5432/studygraph"
    assert SECRET not in described and "studygraph:" not in described


# --- identity checks on a live connection (pure part) ----------------------------

GOOD_IDENTITY = {
    "database": "studygraph",
    "current_user": "studygraph",
    "session_user": "studygraph",
    "schema": "public",
    "comment": "marker",
}


def test_matching_identity_is_accepted():
    check_connected_identity(make_url(_url("db:5432")), **GOOD_IDENTITY, required_comment="marker")
    check_connected_identity(make_url(_url("db:5432")), **{**GOOD_IDENTITY, "comment": None}, required_comment=None)


@pytest.mark.parametrize(
    "override",
    [
        {"database": "postgres"},
        {"current_user": "postgres"},  # e.g. ALTER ROLE ... SET role
        {"session_user": "postgres"},
        {"schema": "other"},  # e.g. ALTER ROLE ... SET search_path
        {"schema": None},  # search_path names no existing schema
        {"comment": None},
        {"comment": "another marker"},
    ],
    ids=lambda override: "-".join(f"{k}={v}" for k, v in override.items()),
)
def test_identity_mismatch_is_refused(override):
    with pytest.raises(UnsafeMigrationTarget):
        check_connected_identity(make_url(_url("db:5432")), **{**GOOD_IDENTITY, **override}, required_comment="marker")


def test_required_comment_setting():
    assert required_database_comment({}) is None
    assert required_database_comment({REQUIRE_COMMENT_ENV: " marker "}) == "marker"
    with pytest.raises(UnsafeMigrationTarget, match="empty"):
        required_database_comment({REQUIRE_COMMENT_ENV: "  "})


# --- the real CLI -------------------------------------------------------------------


def _refused_before_connecting(result) -> str:
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Migration target" not in output and "Connected to database" not in output
    assert "Running upgrade" not in output
    assert SECRET not in output
    return output


def test_cli_refuses_to_run_without_database_url():
    output = _refused_before_connecting(run_alembic("current"))
    assert "DATABASE_URL is not set" in output


@pytest.mark.parametrize("host_part", ["localhost:5432", "127.0.0.1:5432", "127.1:5432", "db,localhost:5432"])
def test_cli_refuses_unsafe_hosts_before_connecting(host_part):
    _refused_before_connecting(run_alembic("upgrade", "head", env={"DATABASE_URL": _url(host_part)}))


def test_cli_refuses_inherited_hostaddr_before_connecting():
    env = {"DATABASE_URL": _url("db:5432"), "PGHOSTADDR": "127.0.0.1"}
    output = _refused_before_connecting(run_alembic("upgrade", "head", env=env))
    assert "PGHOSTADDR" in output


def test_cli_refuses_an_empty_required_comment_before_connecting():
    env = {"DATABASE_URL": _url("db:5432"), REQUIRE_COMMENT_ENV: ""}
    output = _refused_before_connecting(run_alembic("downgrade", "base", env=env))
    assert REQUIRE_COMMENT_ENV in output


# --- offline SQL and the revision graph ------------------------------------------------


def test_offline_baseline_sql_renders_without_any_database_url():
    """Historical: exactly what revision 0001 creates."""
    result = run_alembic("upgrade", BASELINE_REVISION, "--sql")
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert sql.index("CREATE EXTENSION IF NOT EXISTS vector") < sql.index("CREATE TABLE chunks")
    assert "embedding VECTOR(384)" in sql
    assert sql.count("CREATE TABLE ") == 12  # 11 baseline tables + alembic_version
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{BASELINE_REVISION}')" in sql


def test_offline_sql_to_head_renders_without_any_database_url():
    result = run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, result.stderr
    assert f"'{head_revision()}'" in result.stdout


def test_offline_baseline_downgrade_sql_keeps_the_vector_extension():
    result = run_alembic("downgrade", f"{BASELINE_REVISION}:base", "--sql")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("DROP TABLE ") == 11
    assert f"DELETE FROM alembic_version WHERE alembic_version.version_num = '{BASELINE_REVISION}'" in result.stdout
    assert "DROP EXTENSION" not in result.stdout


def test_offline_stamp_sql_touches_only_the_bookkeeping_table():
    """`stamp` records a revision; it must not emit any of the revision's DDL."""
    result = run_alembic("stamp", BASELINE_REVISION, "--sql")
    assert result.returncode == 0, result.stderr
    statements = [s.strip() for s in result.stdout.split(";") if s.strip() and not s.strip().startswith("--")]
    statements = [s for s in statements if s not in ("BEGIN", "COMMIT")]
    assert statements, result.stdout
    assert all("alembic_version" in s for s in statements), statements
    assert "CREATE EXTENSION" not in result.stdout


def test_revision_history_has_a_single_head():
    heads = script_directory().get_heads()
    assert len(heads) == 1, heads
    result = run_alembic("heads")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{heads[0]} (head)"


def test_baseline_is_the_root_revision():
    assert script_directory().get_revision(BASELINE_REVISION).down_revision is None


def test_legacy_init_db_refuses_and_points_to_alembic():
    result = subprocess.run(
        [sys.executable, "init_db.py"],
        cwd=CORE_API_DIR,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "deprecated" in result.stderr
    assert "alembic upgrade head" in result.stderr
