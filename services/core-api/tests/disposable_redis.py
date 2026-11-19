"""Safeguards for the tests that use a real Redis broker (M3).

Mirrors tests/disposable_db.py:

1. Explicit configuration: TEST_REDIS_URL and TEST_REDIS_TOKEN must both be
   set (scripts/test_migrations.sh sets them for the throwaway Redis it
   starts). Neither set: Redis tests are skipped. Only one: error. There is
   no fallback to REDIS_URL or the app settings.
2. Static checks on the URL: redis:// with an explicit password and database
   number, a host that is a separate server reached by name (never this
   machine, a Docker host alias or the docker-compose `redis` service), and a
   target different from REDIS_URL.
3. A check on a live connection: the key `studygraph:disposable-test` must
   hold `studygraph-disposable-test:<TEST_REDIS_TOKEN>`, which the runner
   writes outside the test process.

The tests never flush or purge anything: every test uses broker queues with
unique names, and the whole Redis container is destroyed by the runner.
Error messages never contain the password or the token.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import redis

from app.migration_safety import is_local_host

TEST_REDIS_URL_ENV = "TEST_REDIS_URL"
TEST_REDIS_TOKEN_ENV = "TEST_REDIS_TOKEN"
MARKER_KEY = "studygraph:disposable-test"
MARKER_PREFIX = "studygraph-disposable-test:"
MIN_TOKEN_LENGTH = 16
DEV_REDIS_HOSTS = frozenset({"redis"})


class MissingTestRedis(Exception):
    """No test broker is configured; broker tests are skipped."""


class NotDisposableRedis(RuntimeError):
    """The configured test broker failed a safety check; tests must stop."""


@dataclass(frozen=True)
class DisposableRedis:
    url: str
    token: str
    host: str

    @property
    def marker(self) -> str:
        return MARKER_PREFIX + self.token

    def __repr__(self) -> str:
        return f"DisposableRedis({self.host})"

    def client(self) -> redis.Redis:
        return redis.Redis.from_url(self.url, socket_connect_timeout=5, socket_timeout=5)


def redis_target_from_env(environ: Mapping[str, str]) -> DisposableRedis:
    raw = environ.get(TEST_REDIS_URL_ENV, "").strip()
    token = environ.get(TEST_REDIS_TOKEN_ENV, "").strip()
    if not raw and not token:
        raise MissingTestRedis(f"{TEST_REDIS_URL_ENV} and {TEST_REDIS_TOKEN_ENV} are not set")
    if not raw or not token:
        raise NotDisposableRedis(f"Set both {TEST_REDIS_URL_ENV} and {TEST_REDIS_TOKEN_ENV}, not only one of them.")
    if len(token) < MIN_TOKEN_LENGTH:
        raise NotDisposableRedis(f"{TEST_REDIS_TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters long.")
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        password = parts.password
        database = parts.path.lstrip("/")
    except ValueError:
        raise NotDisposableRedis(f"{TEST_REDIS_URL_ENV} is not a valid URL.") from None
    if parts.scheme != "redis":
        raise NotDisposableRedis(f"{TEST_REDIS_URL_ENV} must use redis://.")
    if not host or is_local_host(host) or host.lower() in DEV_REDIS_HOSTS:
        raise NotDisposableRedis(
            f"{TEST_REDIS_URL_ENV}: the test broker must be a separate server reached by name, "
            "not this machine and not the docker-compose development service."
        )
    if not password:
        raise NotDisposableRedis(f"{TEST_REDIS_URL_ENV} must carry a password.")
    if not database.isdigit():
        raise NotDisposableRedis(f"{TEST_REDIS_URL_ENV} must name a database number.")
    app_raw = environ.get("REDIS_URL", "").strip()
    if app_raw:
        try:
            app_host = urlsplit(app_raw).hostname or ""
        except ValueError:
            app_host = ""
        if app_host.lower() == host.lower():
            raise NotDisposableRedis(f"{TEST_REDIS_URL_ENV} points at the same server as REDIS_URL.")
    return DisposableRedis(url=raw, token=token, host=host)


def verify_redis(target: DisposableRedis) -> None:
    """The live check: the marker the runner wrote must be there."""
    client = target.client()
    try:
        value = client.get(MARKER_KEY)
    except redis.RedisError as exc:
        raise NotDisposableRedis(f"{target!r}: cannot verify the test broker ({type(exc).__name__}).") from None
    finally:
        client.close()
    if value is None or value.decode("utf-8", "replace") != target.marker:
        raise NotDisposableRedis(f"{target!r}: the broker does not carry this run's marker; refusing to use it.")
