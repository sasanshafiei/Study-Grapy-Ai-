"""Admin audit events (WP-9): an append-only record of every committed admin mutation.

- admin_audit_events: actor (users.id), action, audited entity (kind + id, no FK so history
  survives the entity), the run or job the action created (operation_id), a small JSON object of
  ids, states and counts (<= 2 KiB), created_at from the database clock.
- admin_audit_events_guard (BEFORE INSERT/UPDATE/DELETE, row): an inserted row's actor must be an
  existing, active admin at that moment; UPDATE and DELETE are refused.
- admin_audit_events_truncate_guard (BEFORE TRUNCATE, statement): refused.

Additive only. Downgrade refuses while any audit event exists (history is never dropped silently);
an operator who really wants to downgrade removes them deliberately first (docs/database-migrations.md).

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Verbatim copy of app/schema_checks.ADMIN_AUDIT_SHAPE_CHECK (tests/test_migration_0012.py compares them).
ADMIN_AUDIT_ACTIONS = {
    "source_enabled": "source", "source_disabled": "source", "harvest_triggered": "course",
    "parse_retried": "document", "index_retried": "document",
}  # fmt: skip
ADMIN_AUDIT_SHAPE_CHECK = (
    "((" + " OR ".join(f"(action = '{a}' AND entity_kind = '{k}')" for a, k in ADMIN_AUDIT_ACTIONS.items()) + ")"
    " AND (operation_id IS NOT NULL) = (action IN ('harvest_triggered', 'parse_retried', 'index_retried'))"
    " AND jsonb_typeof(details) = 'object' AND octet_length(details::text) <= 2048) IS TRUE"
)

GUARDS = """
CREATE FUNCTION studygraph_admin_audit_events_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.actor_id AND role = 'admin' AND status = 'active') THEN
            RAISE EXCEPTION 'admin audit events need an active admin actor' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'admin audit events are append-only' USING ERRCODE = 'check_violation';
END
$fn$;

CREATE TRIGGER admin_audit_events_guard BEFORE INSERT OR UPDATE OR DELETE ON admin_audit_events
    FOR EACH ROW EXECUTE FUNCTION studygraph_admin_audit_events_guard();
CREATE TRIGGER admin_audit_events_truncate_guard BEFORE TRUNCATE ON admin_audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION studygraph_admin_audit_events_guard();
"""


def upgrade():
    op.create_table(
        "admin_audit_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("entity_kind", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(ADMIN_AUDIT_SHAPE_CHECK, name=op.f("admin_audit_events_shape_check")),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name=op.f("admin_audit_events_actor_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("admin_audit_events_pkey")),
    )
    op.create_index("admin_audit_events_entity_idx", "admin_audit_events", ["entity_kind", "entity_id", "created_at"])
    op.create_index("admin_audit_events_actor_idx", "admin_audit_events", ["actor_id", "created_at"])
    op.execute(GUARDS)


def downgrade():
    op.execute("""
        LOCK TABLE admin_audit_events IN ACCESS EXCLUSIVE MODE;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM admin_audit_events) THEN
                RAISE EXCEPTION 'downgrade to 0011 refused: admin audit events exist';
            END IF;
        END
        $$;
        DROP TRIGGER admin_audit_events_truncate_guard ON admin_audit_events;
        DROP TRIGGER admin_audit_events_guard ON admin_audit_events;
        DROP FUNCTION studygraph_admin_audit_events_guard();
    """)
    op.drop_index("admin_audit_events_actor_idx", table_name="admin_audit_events")
    op.drop_index("admin_audit_events_entity_idx", table_name="admin_audit_events")
    op.drop_table("admin_audit_events")
