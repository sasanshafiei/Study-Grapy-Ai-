"""Revision 0012 (WP-9): append-only admin audit events.

Runs only against the disposable test database (tests/disposable_db.py guards).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app import schema_checks
from tests.alembic_cli import head_revision
from tests.api_support import remove_admin_audit_state
from tests.harvest_support import wikipedia_source

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0012_admin_audit_events.py"


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0012_under_test", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_and_the_model_share_the_audit_check_verbatim():
    module = _migration()
    assert module.ADMIN_AUDIT_SHAPE_CHECK == schema_checks.ADMIN_AUDIT_SHAPE_CHECK
    assert module.ADMIN_AUDIT_ACTIONS == schema_checks.ADMIN_AUDIT_ACTIONS


def _objects(world) -> tuple:
    return (
        world.scalar("SELECT to_regclass('public.admin_audit_events') IS NOT NULL"),
        world.scalar("SELECT count(*) FROM pg_trigger WHERE tgname IN "
                     "('admin_audit_events_guard', 'admin_audit_events_truncate_guard')"),  # fmt: skip
        world.scalar("SELECT count(*) FROM pg_proc WHERE proname = 'studygraph_admin_audit_events_guard'"),
    )


def test_downgrade_refuses_while_audit_events_exist_and_round_trips_without_them(world, alembic):
    admin = world.user("admin")
    source = wikipedia_source(world)
    world.execute(
        "INSERT INTO admin_audit_events (actor_id, action, entity_kind, entity_id, details) "
        "VALUES (:a, 'source_enabled', 'source', :s, '{}')", a=admin.id, s=source,
    )  # fmt: skip
    try:
        with pytest.raises(pytest.fail.Exception, match="downgrade to 0011 refused"):
            alembic("downgrade", "0011")
        assert world.scalar("SELECT version_num FROM alembic_version") == head_revision()  # nothing was changed (one transaction)
        assert world.scalar("SELECT count(*) FROM admin_audit_events WHERE actor_id = :a", a=admin.id) == 1
        with world.engine.begin() as connection:  # the deliberate removal procedure (as the owner)
            remove_admin_audit_state(connection, [admin.id])
        alembic("downgrade", "0011")
        assert _objects(world) == (False, 0, 0)
    finally:
        alembic("upgrade", "head")
    assert world.scalar("SELECT version_num FROM alembic_version") == head_revision() == "0013"
    assert _objects(world) == (True, 2, 1)
