"""Unit tests for the disposable-database guard in tests/disposable_db.py.

These need no database: they prove the static and identity checks refuse
everything except an explicitly configured, correctly marked throwaway
database. tests/test_migrations.py repeats the identity checks against the
real disposable server.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from tests.disposable_db import (
    DisposableTarget,
    MissingTestDatabase,
    NotDisposable,
    check_identity,
    target_from_env,
)

SECRET = "Sup3r-Secret-Pw"
TOKEN = "0123456789abcdef0123456789abcdef"
GOOD_URL = f"postgresql+psycopg://sg_test_ab12:{SECRET}@studygraph-migtest-x-db:5432/sg_disposable_ab12cd34"


def _target_id(raw_url: str) -> str:
    """Test id without credentials."""
    url = make_url(raw_url)
    query = "?" + "&".join(sorted(url.query)) if url.query else ""
    user = "user" if url.username else "no-user"
    return f"{url.get_backend_name()}:{user}@{url.host or '-'}:{url.port or '-'}/{url.database}{query}"


def _env(url: str = GOOD_URL, token: str = TOKEN, **extra: str) -> dict[str, str]:
    return {"TEST_DATABASE_URL": url, "TEST_DATABASE_TOKEN": token, **extra}


def test_valid_configuration_is_accepted():
    target = target_from_env(_env())
    assert target.url.database == "sg_disposable_ab12cd34"
    assert target.marker == f"studygraph-disposable-test:{TOKEN}"
    assert SECRET not in repr(target) and TOKEN not in repr(target)


def test_no_configuration_means_skip_not_fallback():
    # DATABASE_URL alone must never be used for destructive tests.
    with pytest.raises(MissingTestDatabase):
        target_from_env({"DATABASE_URL": "postgresql+psycopg://u:p@db:5432/studygraph"})


@pytest.mark.parametrize(
    "environ",
    [
        {"TEST_DATABASE_URL": GOOD_URL},
        {"TEST_DATABASE_TOKEN": TOKEN},
        _env(token="too-short"),
    ],
    ids=["url-only", "token-only", "short-token"],
)
def test_partial_or_weak_configuration_is_an_error(environ):
    with pytest.raises(NotDisposable):
        target_from_env(environ)


@pytest.mark.parametrize(
    "url",
    [
        # wrong name: the development database or anything without the prefix
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@studygraph-migtest-x-db:5432/studygraph",
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@studygraph-migtest-x-db:5432/studygraph_test",
        # this machine, directly, in non-canonical form, or through Docker Desktop's host aliases
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@localhost:5432/sg_disposable_ab12cd34",
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@127.0.0.1:5433/sg_disposable_ab12cd34",
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@127.1:5432/sg_disposable_ab12cd34",
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@host.docker.internal:5432/sg_disposable_ab12cd34",
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@/sg_disposable_ab12cd34",
        # several hosts at once
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@studygraph-migtest-x-db,localhost:5432/sg_disposable_ab12cd34",
        # the docker-compose development database server
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@db:5432/sg_disposable_ab12cd34",
        # no explicit user, an invalid port, or a target hidden in query parameters
        "postgresql+psycopg://studygraph-migtest-x-db:5432/sg_disposable_ab12cd34",
        f"postgresql+psycopg://sg_test_ab12:{SECRET}@studygraph-migtest-x-db:0/sg_disposable_ab12cd34",
        f"{GOOD_URL}?host=localhost",
        f"{GOOD_URL}?options=-csearch_path%3Dother",
        # not PostgreSQL at all
        "sqlite:///sg_disposable_ab12cd34",
    ],
    ids=_target_id,
)
def test_unsafe_test_targets_are_refused(url):
    with pytest.raises(NotDisposable) as info:
        target_from_env(_env(url=url))
    assert SECRET not in str(info.value)
    assert TOKEN not in str(info.value)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS"])
def test_libpq_overrides_in_the_test_process_are_refused(variable):
    with pytest.raises(NotDisposable, match=variable):
        target_from_env(_env(**{variable: "127.0.0.1"}))


def test_test_target_gets_an_explicit_port():
    url_without_port = GOOD_URL.replace(":5432/", "/")
    assert target_from_env(_env(url=url_without_port, PGPORT="1")).url.port == 5432


def test_test_target_equal_to_the_application_database_is_refused():
    app_url = GOOD_URL.replace(SECRET, "other-password")
    with pytest.raises(NotDisposable, match="same database as DATABASE_URL"):
        target_from_env(_env(DATABASE_URL=app_url))
    # also when one of them leaves the default port implicit
    with pytest.raises(NotDisposable, match="same database as DATABASE_URL"):
        target_from_env(_env(DATABASE_URL=app_url.replace(":5432/", "/")))


def _target() -> DisposableTarget:
    return target_from_env(_env())


GOOD = {
    "database": "sg_disposable_ab12cd34",
    "current_user": "sg_test_ab12",
    "session_user": "sg_test_ab12",
    "schema": "public",
}


def test_identity_check_accepts_the_marked_database():
    target = _target()
    check_identity(target, **GOOD, comment=target.marker)


@pytest.mark.parametrize(
    ("override", "comment"),
    [
        ({"database": "studygraph"}, f"studygraph-disposable-test:{TOKEN}"),
        ({"current_user": "studygraph"}, f"studygraph-disposable-test:{TOKEN}"),
        ({"session_user": "studygraph"}, f"studygraph-disposable-test:{TOKEN}"),
        ({"schema": "other"}, f"studygraph-disposable-test:{TOKEN}"),
        ({}, None),
        ({}, "studygraph-disposable-test:another-token-value"),
        ({}, TOKEN),
    ],
    ids=["other-database", "other-current-user", "other-session-user", "other-schema", "no-marker", "other-token", "token-without-prefix"],
)
def test_identity_check_refuses_any_mismatch(override, comment):
    with pytest.raises(NotDisposable) as info:
        check_identity(_target(), **{**GOOD, **override}, comment=comment)
    assert TOKEN not in str(info.value)
