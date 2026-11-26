"""Revision 0011 (WP-6): OpenStax provider identity, git version keys, insert-disabled OpenStax sources.

Runs only against the disposable test database (tests/disposable_db.py guards).
"""

from __future__ import annotations

import pytest

from tests.alembic_cli import head_revision
from tests.harvest_support import wikipedia_source


def _columns(world, table: str, column: str) -> int:
    return world.scalar(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = :t AND column_name = :c", t=table, c=column
    )


def test_downgrade_refuses_while_openstax_state_exists_and_round_trips_without_it(world, alembic):
    wiki = wikipedia_source(world)
    openstax = world._one(
        "INSERT INTO sources (kind, provider, language, licence_url, provider_book) "
        "VALUES ('openstax', 'openstax', 'en', 'https://creativecommons.org/licenses/by-nc-sa/4.0/', 'chemistry-2e') "
        "RETURNING id"
    )
    world.sources.append(openstax)
    try:
        with pytest.raises(pytest.fail.Exception, match="downgrade to 0010 refused"):
            alembic("downgrade", "0010")
        assert world.scalar("SELECT version_num FROM alembic_version") == head_revision()  # nothing was changed
        world.execute("DELETE FROM harvest_provider_budgets WHERE source_id = :s", s=openstax)
        world.execute("DELETE FROM sources WHERE id = :s", s=openstax)
        world.sources.remove(openstax)
        alembic("downgrade", "0010")
        assert _columns(world, "sources", "provider_book") == 0
        assert _columns(world, "external_resources", "last_revision_key") == 0
        assert _columns(world, "external_document_versions", "revision_key") == 0
        assert world.scalar("SELECT is_nullable FROM information_schema.columns WHERE table_name = "
                            "'external_document_versions' AND column_name = 'revision_id'") == "NO"  # fmt: skip
        assert world.scalar("SELECT provider FROM sources WHERE id = :s", s=wiki) == "wikipedia"
    finally:
        alembic("upgrade", "head")
    assert world.scalar("SELECT version_num FROM alembic_version") == head_revision() == "0012"
    assert world.scalar("SELECT count(*) FROM pg_trigger WHERE tgname = 'sources_openstax_insert_disabled'") == 1
