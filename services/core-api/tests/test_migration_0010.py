"""Revision 0010 (WP-5 / F10): the digest of a duplicated newest revision of an accepted resource.

Runs only against the disposable test database (tests/disposable_db.py guards).
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from tests.alembic_cli import head_revision
from tests.harvest_support import wikipedia_source

X, Y, Z = "a" * 64, "b" * 64, "c" * 64


def _resource(world, course, source, page: int, state: str, digest: str, **columns):
    names = ", ".join(["course_id", "source_id", "page_id", "title", "canonical_url", "state", "content_sha256",
                       "last_revision_id", *columns])  # fmt: skip
    values = ", ".join([":c", ":s", ":p", "'T'", "'https://en.wikipedia.org/wiki/T'", ":state", ":h", ":p",
                        *(f":{name}" for name in columns)])  # fmt: skip
    return world._one(
        f"INSERT INTO external_resources ({names}) VALUES ({values}) RETURNING id",
        c=course, s=source, p=page, state=state, h=digest, **columns,
    )  # fmt: skip


@pytest.fixture
def rows(world):
    course = world.course(world.user("professor"))
    source = wikipedia_source(world)
    holder = _resource(world, course, source, 8, "accepted", Y)
    return world, course, source, holder


def test_the_duplicate_digest_is_only_for_accepted_resources_and_never_their_own_content(rows):
    world, course, source, holder = rows
    marked = _resource(world, course, source, 7, "accepted", X, duplicate_content_sha256=Y, duplicate_of_id=holder)
    assert world.scalar("SELECT duplicate_content_sha256 FROM external_resources WHERE id = :r", r=marked) == Y
    for page, state, digest, duplicate in ((9, "duplicate", Y, Y), (10, "missing", Z, Y), (11, "accepted", Z, Z),
                                           (12, "accepted", Z, "B" * 64), (13, "accepted", Z, "b" * 63)):  # fmt: skip
        with pytest.raises(IntegrityError, match="external_resources_duplicate_content_sha256_check"):
            _resource(world, course, source, page, state, digest, duplicate_content_sha256=duplicate)


def test_downgrade_forgets_recorded_duplicates_so_the_older_code_fetches_them_again(rows, alembic):
    world, course, source, holder = rows
    marked = _resource(world, course, source, 7, "accepted", X, duplicate_content_sha256=Y, duplicate_of_id=holder)
    duplicate = _resource(world, course, source, 9, "duplicate", Y, duplicate_of_id=holder)
    world.execute("UPDATE external_resources SET next_check_at = now() + interval '7 days' WHERE course_id = :c", c=course)
    try:
        alembic("downgrade", "0009")
        assert world.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'external_resources' "
            "AND column_name = 'duplicate_content_sha256'"
        ) == 0
        after = {r["id"]: r for r in world.rows("SELECT * FROM external_resources WHERE course_id = :c", c=course)}
        assert (after[marked]["state"], after[marked]["content_sha256"]) == ("accepted", X)  # visible version kept
        assert (after[marked]["last_revision_id"], after[marked]["duplicate_of_id"]) == (None, None)
        assert world.scalar("SELECT next_check_at <= now() FROM external_resources WHERE id = :r", r=marked)
        for untouched in (holder, duplicate):
            assert after[untouched]["last_revision_id"] is not None
            assert world.scalar("SELECT next_check_at > now() FROM external_resources WHERE id = :r", r=untouched)
        assert after[duplicate]["duplicate_of_id"] == holder
    finally:
        alembic("upgrade", "head")
    assert world.scalar("SELECT version_num FROM alembic_version") == head_revision() == "0013"


def test_core_api_and_workers_require_the_head_revision():
    from app.database_compatibility import REQUIRED_SCHEMA_REVISION

    assert REQUIRED_SCHEMA_REVISION == head_revision() == "0013"
