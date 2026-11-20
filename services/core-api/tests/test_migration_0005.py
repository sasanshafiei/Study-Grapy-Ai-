"""Revision 0005: external provenance, section locators, harvesting state (M6).

Database tests run only through the guarded fixtures (disposable database);
the offline-SQL tests need no database.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.alembic_cli import BASELINE_REVISION, run_alembic
from tests.test_migration_0002 import _base_rows, _run_script
from tests.test_migration_0003 import (
    _failed,
    _m2_dataset,
    scratch3,  # noqa: F401  (fixture used by scratch5)
)
from tests.test_migrations import BASELINE_TABLES, _current, _relation_oids, _scaffold_snapshot, _snapshot, _table_contents

REVISION = "0005"
PREVIOUS = "0004"
NEW_TABLES = {
    "harvest_course_settings", "harvest_targets", "harvest_queries", "harvest_provider_budgets", "harvest_runs",
    "external_resources", "external_document_versions",
}  # fmt: skip
UNCHANGED_TABLES = BASELINE_TABLES - {"sources", "documents"} | {"ingestion_jobs", "document_sections"}


@pytest.fixture
def scratch5(scratch3, committed_transaction):  # noqa: F811
    """scratch3 (empty database at base before and after) plus removal of any M6 state."""
    yield
    with committed_transaction() as connection:
        if connection.execute(text("SELECT to_regclass('public.harvest_runs') IS NOT NULL")).scalar_one():
            connection.execute(text("DELETE FROM external_document_versions"))
            connection.execute(text("UPDATE external_resources SET current_document_id = NULL"))
            connection.execute(text("DELETE FROM document_sections WHERE (locator -> 'page') IS NULL"))
            connection.execute(text("DELETE FROM documents WHERE external_resource_id IS NOT NULL"))
            for table in ("external_resources", "harvest_runs", "harvest_queries", "harvest_targets",
                          "harvest_provider_budgets", "harvest_course_settings"):  # fmt: skip
                connection.execute(text(f"DELETE FROM {table}"))
            connection.execute(text("DELETE FROM sources WHERE provider IS NOT NULL"))


def _legacy_sources(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO sources (kind, domain, licence, enabled) VALUES "
            "('wikipedia', 'en.wikipedia.org', 'CC BY-SA', true), ('openstax', 'openstax.org', NULL, false)"
        )
    )


def _m6_rows(connection, ids) -> dict:
    source = connection.execute(
        text(
            "INSERT INTO sources (kind, enabled, provider, language, licence_url) "
            "VALUES ('wikipedia', false, 'wikipedia', 'en', 'https://creativecommons.org/licenses/by-sa/4.0/') RETURNING id"
        )
    ).scalar_one()
    connection.execute(text("INSERT INTO harvest_course_settings (course_id) VALUES (:c)"), {"c": ids["course"]})
    connection.execute(text("INSERT INTO harvest_targets (course_id, source_id) VALUES (:c, :s)"), {"c": ids["course"], "s": source})
    resource = connection.execute(
        text(
            "INSERT INTO external_resources (course_id, source_id, page_id, title, canonical_url, state, content_sha256) "
            "VALUES (:c, :s, 7, 'T', 'https://en.wikipedia.org/wiki/T', 'accepted', :h) RETURNING id"
        ),
        {"c": ids["course"], "s": source, "h": "a" * 64},
    ).scalar_one()
    return {"source": source, "resource": resource}


def test_upgrade_keeps_every_row_and_leaves_legacy_sources_untouched(scratch5, disposable_engine, alembic, committed_transaction):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        _m2_dataset(connection)
        _legacy_sources(connection)
    contents = _table_contents(disposable_engine, UNCHANGED_TABLES)
    oids = _relation_oids(disposable_engine)
    query = "SELECT id, kind, domain, licence, enabled, created_at FROM sources ORDER BY id"
    with disposable_engine.connect() as connection:
        sources_before = [tuple(r) for r in connection.execute(text(query))]
    assert any(row[1] == "wikipedia" and row[4] is True for row in sources_before)  # a legacy, enabled row
    alembic("upgrade", REVISION)
    with disposable_engine.connect() as connection:
        assert [tuple(r) for r in connection.execute(text(query))] == sources_before
        assert connection.execute(
            text("SELECT count(*) FROM sources WHERE provider IS NOT NULL OR language IS NOT NULL OR licence_url IS NOT NULL")
        ).scalar_one() == 0  # legacy sources never become harvest providers
        assert connection.execute(text("SELECT count(*) FROM documents WHERE external_resource_id IS NOT NULL")).scalar_one() == 0
        for table in NEW_TABLES:
            assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0, table  # nothing seeded
    assert _table_contents(disposable_engine, UNCHANGED_TABLES) == contents
    new_oids = _relation_oids(disposable_engine)
    assert all(new_oids[name] == oid for name, oid in oids.items())
    assert _current(alembic).split()[0] == REVISION


@pytest.mark.parametrize(
    "locator, ok",
    [({"page": 3}, True), ({"anchor": None, "heading": None, "level": 0}, True),
     ({"anchor": "History", "heading": "History", "level": 2}, True),
     ({"anchor": "History", "heading": "History", "level": 1}, False),
     ({"anchor": "Two words", "heading": "H", "level": 2}, False),
     ({"anchor": "H", "heading": " padded", "level": 2}, False),
     ({"anchor": None, "heading": "H", "level": 0}, False), ({"anchor": "H", "heading": "H"}, False),
     ({"anchor": "H", "heading": "H", "level": 2, "page": 1}, False), ({"page": 1, "level": 0}, False),
     ({"anchor": "H", "heading": "H", "level": 2.5}, False), ({"anchor": "", "heading": "H", "level": 2}, False)],
)  # fmt: skip
def test_section_locator_check(head_database, rolled_back_transaction, locator, ok):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        document = connection.execute(
            text("INSERT INTO documents (course_id, origin, title, language, status) VALUES (:c, 'external', 'X', 'en', 'pending') RETURNING id"),
            {"c": ids["course"]},
        ).scalar_one()
        statement = text("INSERT INTO document_sections (document_id, ordinal, text, locator) VALUES (:d, 0, 'text', CAST(:l AS jsonb))")
        if ok:
            connection.execute(statement, {"d": document, "l": json.dumps(locator)})
        else:
            with pytest.raises(IntegrityError, match="document_sections_locator_check"):
                connection.execute(statement, {"d": document, "l": json.dumps(locator)})


def test_one_indexed_and_one_pending_version_per_resource(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        m6 = _m6_rows(connection, ids)
        insert = text(
            "INSERT INTO documents (course_id, source_id, origin, title, language, status, external_resource_id) "
            "VALUES (:c, :s, 'external', 'T', 'en', :status, :r)"
        )
        values = {"c": ids["course"], "s": m6["source"], "r": m6["resource"]}
        connection.execute(insert, {**values, "status": "indexed"})
        connection.execute(insert, {**values, "status": "pending"})
        connection.execute(insert, {**values, "status": "stale"})
        connection.execute(insert, {**values, "status": "failed"})
        with pytest.raises(IntegrityError, match="documents_external_resource_indexed_idx"):
            with connection.begin_nested():
                connection.execute(insert, {**values, "status": "indexed"})
        with pytest.raises(IntegrityError, match="documents_external_resource_pending_idx"):
            with connection.begin_nested():
                connection.execute(insert, {**values, "status": "pending"})
        with pytest.raises(IntegrityError, match="documents_external_resource_check"):
            connection.execute(
                text("INSERT INTO documents (course_id, origin, title, language, status, external_resource_id) "
                     "VALUES (:c, 'class', 'T', 'en', 'pending', :r)"),
                {"c": ids["course"], "r": m6["resource"]},
            )  # fmt: skip


def test_resource_and_document_references_agree_on_resource_course_and_source(head_database, rolled_back_transaction):
    """M1: current_document_id must be a document of the same resource; documents only reference their own course/source."""
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        m6 = _m6_rows(connection, ids)
        other_source = connection.execute(
            text(
                "INSERT INTO sources (kind, enabled, provider, language, licence_url) "
                "VALUES ('wikipedia', false, 'wikipedia', 'de', 'https://creativecommons.org/licenses/by-sa/4.0/') RETURNING id"
            )
        ).scalar_one()
        second = connection.execute(
            text(
                "INSERT INTO external_resources (course_id, source_id, page_id, title, canonical_url, state, content_sha256) "
                "VALUES (:c, :s, 8, 'U', 'https://en.wikipedia.org/wiki/U', 'accepted', :h) RETURNING id"
            ),
            {"c": ids["course"], "s": m6["source"], "h": "b" * 64},
        ).scalar_one()
        insert = text(
            "INSERT INTO documents (course_id, source_id, origin, title, language, status, external_resource_id) "
            "VALUES (:c, :s, 'external', 'T', 'en', 'pending', :r) RETURNING id"
        )
        document = connection.execute(insert, {"c": ids["course"], "s": m6["source"], "r": m6["resource"]}).scalar_one()
        # A harvested document cannot reference a resource of another course or another source.
        stale = text(insert.text.replace("'pending'", "'stale'"))  # not subject to the one-pending-version index
        for course, source in ((ids["course2"], m6["source"]), (ids["course"], other_source)):
            with pytest.raises(IntegrityError, match="documents_external_resource_id_fkey"):
                with connection.begin_nested():
                    connection.execute(stale, {"c": course, "s": source, "r": m6["resource"]})
        point = text("UPDATE external_resources SET current_document_id = :d WHERE id = :r")
        # The current document must exist and belong to this very resource.
        with pytest.raises(IntegrityError, match="external_resources_current_document_id_fkey"):
            with connection.begin_nested():
                connection.execute(point, {"d": document, "r": second})  # a document of another resource
        with pytest.raises(IntegrityError, match="external_resources_current_document_id_fkey"):
            with connection.begin_nested():
                connection.execute(point, {"d": uuid.uuid4(), "r": m6["resource"]})  # no such document
        connection.execute(point, {"d": document, "r": m6["resource"]})
        connection.execute(point, {"d": None, "r": m6["resource"]})


def test_provider_sources_are_unique_and_restricted(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        insert = text(
            "INSERT INTO sources (kind, provider, language, licence_url, enabled) VALUES ('wikipedia', :p, :l, 'https://x.org/l', false)"
        )
        connection.execute(insert, {"p": "wikipedia", "l": "en"})
        with pytest.raises(IntegrityError, match="sources_provider_language_idx"):
            with connection.begin_nested():
                connection.execute(insert, {"p": "wikipedia", "l": "en"})
        for provider, language in (("openalex", "en"), ("openstax", "en"), ("wikipedia", "EN"), ("wikipedia", None)):
            with pytest.raises(IntegrityError, match="sources_provider_check"):
                with connection.begin_nested():
                    connection.execute(insert, {"p": provider, "l": language})


def test_run_lifecycle_and_one_active_run_per_target(head_database, rolled_back_transaction):
    with rolled_back_transaction() as connection:
        ids = _base_rows(connection)
        m6 = _m6_rows(connection, ids)
        insert = text(
            "INSERT INTO harvest_runs (course_id, source_id, trigger, generation, max_claims, max_failures, status, finished_at) "
            "VALUES (:c, :s, 'manual', 0, 5, 3, :status, :finished)"
        )
        values = {"c": ids["course"], "s": m6["source"]}
        connection.execute(insert, {**values, "status": "queued", "finished": None})
        connection.execute(insert, {**values, "status": "succeeded", "finished": "2026-01-01"})
        with pytest.raises(IntegrityError, match="harvest_runs_course_id_source_id_idx"):
            with connection.begin_nested():
                connection.execute(insert, {**values, "status": "queued", "finished": None})
        with pytest.raises(IntegrityError, match="harvest_runs_lifecycle_check"):
            with connection.begin_nested():
                connection.execute(insert, {**values, "status": "failed", "finished": None})
        with pytest.raises(IntegrityError, match="harvest_runs_counters_check"):
            connection.execute(text("UPDATE harvest_runs SET claim_count = 6 WHERE status = 'queued'"))


@pytest.mark.parametrize("state", ["source", "course_settings", "section_locator"])
def test_downgrade_refuses_while_m6_state_exists(scratch5, disposable_engine, alembic, alembic_env, committed_transaction, state):
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        if state == "source":
            connection.execute(
                text("INSERT INTO sources (kind, provider, language, licence_url, enabled) VALUES ('wikipedia', 'wikipedia', 'en', 'https://x.org/l', false)")
            )
        elif state == "course_settings":
            connection.execute(text("INSERT INTO harvest_course_settings (course_id) VALUES (:c)"), {"c": ids["course"]})
        else:
            document = connection.execute(
                text("INSERT INTO documents (course_id, origin, title, language, status) VALUES (:c, 'external', 'X', 'en', 'pending') RETURNING id"),
                {"c": ids["course"]},
            ).scalar_one()
            connection.execute(
                text("INSERT INTO document_sections (document_id, ordinal, text, locator) VALUES (:d, 0, 't', CAST(:l AS jsonb))"),
                {"d": document, "l": json.dumps({"anchor": None, "heading": None, "level": 0})},
            )
    before = _snapshot(disposable_engine)
    for target in (PREVIOUS, BASELINE_REVISION):
        output = _failed(run_alembic("downgrade", target, env=alembic_env))
        assert "Downgrade below 0005 refused before any change." in output
    assert _snapshot(disposable_engine) == before
    assert _current(alembic).split()[0] == REVISION
    with committed_transaction() as connection:
        connection.execute(text("DELETE FROM document_sections"))
        connection.execute(text("DELETE FROM harvest_course_settings"))
        connection.execute(text("DELETE FROM documents WHERE origin = 'external'"))


def test_downgrade_without_m6_state_restores_0004_exactly_and_upgrades_again(scratch5, disposable_engine, alembic, committed_transaction):
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as connection:
        _m2_dataset(connection)
        _legacy_sources(connection)
    snapshot_0004 = _snapshot(disposable_engine)
    contents = _table_contents(disposable_engine, UNCHANGED_TABLES | {"sources", "documents"})
    alembic("upgrade", REVISION)
    result = alembic("downgrade", PREVIOUS)
    assert f"Running downgrade {REVISION} -> {PREVIOUS}" in result.stderr
    assert _snapshot(disposable_engine) == snapshot_0004
    assert _table_contents(disposable_engine, UNCHANGED_TABLES | {"sources", "documents"}) == contents
    alembic("upgrade", "head")
    assert _current(alembic).endswith("(head)")


def _offline(*args: str) -> str:
    result = run_alembic(*args, "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_offline_sql_carries_the_preflight_and_the_guard():
    upgrade = _offline("upgrade", f"{PREVIOUS}:{REVISION}")
    assert upgrade.index("DO $migration_0005_preflight$") < upgrade.index("ALTER TABLE sources ADD COLUMN provider")
    downgrade = _offline("downgrade", f"{REVISION}:{PREVIOUS}")
    assert downgrade.index("DO $migration_0005_downgrade_guard$") < downgrade.index("DROP TABLE external_document_versions")


def test_offline_downgrade_sql_enforces_the_guard_when_executed(scratch5, disposable_engine, alembic, committed_transaction):
    sql = _offline("downgrade", f"{REVISION}:{PREVIOUS}")
    alembic("upgrade", REVISION)
    with committed_transaction() as connection:
        ids = _base_rows(connection)
        connection.execute(text("INSERT INTO harvest_course_settings (course_id) VALUES (:c)"), {"c": ids["course"]})
    before = _snapshot(disposable_engine)
    with pytest.raises(Exception, match="Downgrade below 0005 refused"):
        with committed_transaction() as connection:
            _run_script(connection, sql)
    assert _snapshot(disposable_engine) == before
    with committed_transaction() as connection:
        connection.execute(text("DELETE FROM harvest_course_settings"))


def test_historical_baseline_is_untouched_by_0005(scratch5, disposable_engine, alembic):
    alembic("upgrade", "head")
    alembic("downgrade", BASELINE_REVISION)
    assert _snapshot(disposable_engine) == _scaffold_snapshot()
