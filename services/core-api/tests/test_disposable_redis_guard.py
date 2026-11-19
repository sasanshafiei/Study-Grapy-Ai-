"""The disposable-Redis guard itself (no broker needed): which configurations are refused."""

from __future__ import annotations

import pytest

from tests.disposable_redis import (
    MissingTestRedis,
    NotDisposableRedis,
    redis_target_from_env,
)

TOKEN = "0123456789abcdef0123"
GOOD = "redis://:s3cret@studygraph-migtest-x-redis:6379/0"


def test_nothing_configured_skips():
    with pytest.raises(MissingTestRedis):
        redis_target_from_env({})


@pytest.mark.parametrize(
    "environ",
    [
        {"TEST_REDIS_URL": GOOD},
        {"TEST_REDIS_TOKEN": TOKEN},
        {"TEST_REDIS_URL": GOOD, "TEST_REDIS_TOKEN": "short"},
        {"TEST_REDIS_URL": "rediss://:s3cret@h:6379/0", "TEST_REDIS_TOKEN": TOKEN},
        {"TEST_REDIS_URL": "redis://:s3cret@localhost:6379/0", "TEST_REDIS_TOKEN": TOKEN},
        {"TEST_REDIS_URL": "redis://:s3cret@127.0.0.1:6379/0", "TEST_REDIS_TOKEN": TOKEN},
        {"TEST_REDIS_URL": "redis://:s3cret@host.docker.internal:6379/0", "TEST_REDIS_TOKEN": TOKEN},
        {"TEST_REDIS_URL": "redis://:s3cret@redis:6379/0", "TEST_REDIS_TOKEN": TOKEN},  # the compose service
        {"TEST_REDIS_URL": "redis://studygraph-migtest-x-redis:6379/0", "TEST_REDIS_TOKEN": TOKEN},  # no password
        {"TEST_REDIS_URL": "redis://:s3cret@studygraph-migtest-x-redis:6379", "TEST_REDIS_TOKEN": TOKEN},  # no db
        {"TEST_REDIS_URL": GOOD, "TEST_REDIS_TOKEN": TOKEN, "REDIS_URL": "redis://studygraph-migtest-x-redis:6379/1"},
    ],
    ids=[
        "url-only", "token-only", "short-token", "other-scheme", "localhost", "loopback-ip", "docker-host-alias",
        "compose-redis", "no-password", "no-database-number", "same-as-REDIS_URL",
    ],
)  # fmt: skip
def test_unsafe_configurations_are_errors(environ):
    with pytest.raises(NotDisposableRedis) as caught:
        redis_target_from_env(environ)
    assert "s3cret" not in str(caught.value) and TOKEN not in str(caught.value)


def test_valid_configuration_hides_its_secrets():
    target = redis_target_from_env({"TEST_REDIS_URL": GOOD, "TEST_REDIS_TOKEN": TOKEN})
    assert target.marker == "studygraph-disposable-test:" + TOKEN
    assert "s3cret" not in repr(target) and TOKEN not in repr(target)
