"""Document sections, job scheduling and attempt fencing, storage identity (M3).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19

ingestion_jobs gains four columns used by the asynchronous pipeline
(docs/document-processing.md):

- attempt_token: identity of the attempt that holds a running job, new for
  every claim. Every write on behalf of an attempt is conditional on it, so a
  stale worker can never overwrite the result of a newer attempt.
- lease_expires_at: when a running attempt is presumed dead and recovered.
- available_at: retry backoff; a queued job is neither dispatched nor claimed
  before it. NOT NULL DEFAULT now(): existing rows get the migration time
  (PostgreSQL stores the value once, the table is not rewritten), so jobs
  queued before M3 become dispatchable at once.
- last_dispatched_at: when a message was last reserved for publication
  (NULL = never published), so the dispatch sweep neither floods the broker
  nor waits long for a job whose first publication failed.

A CHECK requires a running job to carry its token and a lease after its start.

document_sections holds the normalized text of each source page that has
text: ordinal 0-based and unique per document, locator exactly {"page": n}
with the 1-based page number, non-blank text, ON DELETE CASCADE from
documents (sections are derived data of their document).

storage_identity is a single row binding this database to one upload store
(the id in <UPLOAD_DIR>/.studygraph-storage); it is only written by the
explicit init-storage command.

Data safety:
- upgrade locks ingestion_jobs against concurrent writes and stops, before any
  change, if a job is already running: such a job has no attempt token or
  lease to give it after the fact. It never modifies existing rows.
- downgrade refuses, before any change, while document_sections has rows, a
  job carries attempt or dispatch state (running, attempt_token,
  lease_expires_at, last_dispatched_at), a queued job is scheduled for later
  (available_at in the future, a retry backoff that would be lost), or a
  storage identity is recorded. Remove that state deliberately first.
Both checks are DO blocks, so they also run when the SQL is rendered with
`alembic upgrade --sql` / `downgrade --sql` and executed later.

Self-contained on purpose: it does not import app.models.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JOB_LEASE_CHECK = (
    "status <> 'running' OR (attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL"
    " AND lease_expires_at > started_at)"
)

SECTION_CHECKS = {
    "document_sections_ordinal_check": "ordinal >= 0",
    "document_sections_text_check": "btrim(text, E' \\t\\n\\r') <> ''",
    "document_sections_locator_check": (
        "CASE WHEN jsonb_typeof(locator) = 'object' AND jsonb_typeof(locator -> 'page') = 'number'"
        " AND (locator - 'page') = '{}'::jsonb"
        " THEN (locator ->> 'page')::numeric BETWEEN 1 AND 2147483647"
        " AND (locator ->> 'page')::numeric = trunc((locator ->> 'page')::numeric)"
        " ELSE false END"
    ),
}

# Runs inside the migration transaction, before any DDL. The lock (released at
# COMMIT) keeps a job from starting between the check and the new constraint.
UPGRADE_PREFLIGHT = """
DO $migration_0003_preflight$
DECLARE
    running_jobs bigint;
BEGIN
    LOCK TABLE ingestion_jobs IN SHARE ROW EXCLUSIVE MODE;
    SELECT count(*) INTO running_jobs FROM ingestion_jobs WHERE status = 'running';
    IF running_jobs > 0 THEN
        RAISE EXCEPTION 'Migration 0003 stopped before any change. % ingestion_jobs rows are running; revision 0003 requires every running job to carry an attempt token and a lease, which cannot be assigned after the fact. Set them back to queued (or failed) deliberately, then run the upgrade again (docs/database-migrations.md).', running_jobs;
    END IF;
END
$migration_0003_preflight$
"""

# The tables are locked first so no row can appear between the check and the drops.
DOWNGRADE_GUARD = """
DO $migration_0003_downgrade_guard$
DECLARE
    section_rows bigint;
    attempt_rows bigint;
    scheduled_rows bigint;
    identity_rows bigint;
BEGIN
    LOCK TABLE document_sections, storage_identity, ingestion_jobs IN ACCESS EXCLUSIVE MODE;
    SELECT count(*) INTO section_rows FROM document_sections;
    SELECT count(*) INTO attempt_rows FROM ingestion_jobs
    WHERE status = 'running' OR attempt_token IS NOT NULL OR lease_expires_at IS NOT NULL
       OR last_dispatched_at IS NOT NULL;
    SELECT count(*) INTO scheduled_rows FROM ingestion_jobs WHERE status = 'queued' AND available_at > now();
    SELECT count(*) INTO identity_rows FROM storage_identity;
    IF section_rows > 0 OR attempt_rows > 0 OR scheduled_rows > 0 OR identity_rows > 0 THEN
        RAISE EXCEPTION 'Downgrade below 0003 refused before any change. % document_sections rows, % ingestion_jobs rows with attempt or dispatch state, % queued jobs scheduled for a later retry and % storage identity rows would be lost. Remove that state deliberately first (docs/database-migrations.md).', section_rows, attempt_rows, scheduled_rows, identity_rows;
    END IF;
END
$migration_0003_downgrade_guard$
"""


def upgrade() -> None:
    op.execute(UPGRADE_PREFLIGHT)

    op.add_column("ingestion_jobs", sa.Column("attempt_token", sa.UUID(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "ingestion_jobs",
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column("ingestion_jobs", sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(op.f("ingestion_jobs_lease_check"), "ingestion_jobs", JOB_LEASE_CHECK)

    op.create_table(
        "document_sections",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *(sa.CheckConstraint(condition, name=op.f(name)) for name, condition in SECTION_CHECKS.items()),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("document_sections_document_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("document_sections_pkey")),
        sa.UniqueConstraint("document_id", "ordinal", name=op.f("document_sections_document_id_ordinal_key")),
    )

    op.create_table(
        "storage_identity",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("storage_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("storage_identity_single_row_check")),
        sa.PrimaryKeyConstraint("id", name=op.f("storage_identity_pkey")),
    )


def downgrade() -> None:
    """Remove everything upgrade() added, only if that loses no data or schedule."""
    op.execute(DOWNGRADE_GUARD)

    op.drop_table("storage_identity")
    op.drop_table("document_sections")
    op.drop_constraint(op.f("ingestion_jobs_lease_check"), "ingestion_jobs", type_="check")
    op.drop_column("ingestion_jobs", "last_dispatched_at")
    op.drop_column("ingestion_jobs", "available_at")
    op.drop_column("ingestion_jobs", "lease_expires_at")
    op.drop_column("ingestion_jobs", "attempt_token")
