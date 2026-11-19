"""Document upload metadata and durable ingestion jobs (M2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19

documents gains the columns a stored upload needs (uploader, sanitized
original filename, server-generated storage key, detected MIME type, size)
plus the ones its processing will fill (page count, kind, user-readable
error). All of them are nullable and have no default, so adding them does not
rewrite the table and every legacy or external row stays valid: no file
metadata is invented for rows that have no stored file. CHECK constraints
require complete metadata and a canonical SHA-256 hash only for rows that do
have a storage_key, and every CHECK is written to evaluate to TRUE or FALSE,
never NULL (a NULL result would pass).

Course-scoped deduplication is a partial unique index over stored class
uploads only, so legacy hashes (whose format the scaffold never defined) are
neither rewritten nor compared.

ingestion_jobs records the work to do on a document, written in the same
transaction as the document. Nothing consumes it yet; the M3 worker will.

Data safety:
- upgrade first checks, inside the database, that the existing origin and
  status values fit the new CHECK constraints, and stops with counts (no
  values) before any change if they do not. It never modifies existing rows.
- downgrade refuses, before any change, while ingestion_jobs has rows or any
  document carries upload metadata: dropping the columns would silently lose
  that data and orphan the stored files. Remove them deliberately first.
Both checks are DO blocks, so they also run when the SQL is rendered with
`alembic upgrade --sql` and executed later.

Self-contained on purpose: it does not import app.models.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Columns added to documents, in this order (the ORM declares them in the same order).
NEW_DOCUMENT_COLUMNS = (
    "uploaded_by",
    "original_filename",
    "storage_key",
    "mime_type",
    "size_bytes",
    "page_count",
    "kind",
    "error_message",
)

DOCUMENT_CHECKS = {
    "documents_origin_check": "origin IN ('class', 'external', 'web_cache')",
    "documents_status_check": "status IN ('pending', 'indexed', 'failed', 'stale')",
    "documents_kind_check": "kind IS NULL OR kind IN ('slides', 'notes', 'syllabus', 'textbook', 'article')",
    "documents_size_bytes_check": "size_bytes IS NULL OR size_bytes > 0",
    "documents_page_count_check": "page_count IS NULL OR page_count > 0",
    "documents_stored_file_check": (
        "storage_key IS NULL OR ("
        "original_filename IS NOT NULL AND mime_type IS NOT NULL AND size_bytes IS NOT NULL "
        "AND content_hash IS NOT NULL AND content_hash ~ '^[0-9a-f]{64}$')"
    ),
    "documents_class_upload_check": (
        "storage_key IS NULL OR origin <> 'class' OR (course_id IS NOT NULL AND uploaded_by IS NOT NULL)"
    ),
}

JOB_CHECKS = {
    "ingestion_jobs_kind_check": "kind IN ('parse', 'index')",
    "ingestion_jobs_status_check": "status IN ('queued', 'running', 'succeeded', 'failed')",
    "ingestion_jobs_attempts_check": "max_attempts >= 1 AND attempt_count >= 0 AND attempt_count <= max_attempts",
    "ingestion_jobs_lifecycle_check": (
        "(status = 'queued' AND finished_at IS NULL)"
        " OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND attempt_count >= 1)"
        " OR (status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
        " AND finished_at >= started_at AND attempt_count >= 1)"
        " OR (status = 'failed' AND finished_at IS NOT NULL"
        " AND (started_at IS NULL OR finished_at >= started_at))"
    ),
}

# Runs inside the migration transaction, before any DDL. Reports counts only.
UPGRADE_PREFLIGHT = """
DO $migration_0002_preflight$
DECLARE
    bad_origin bigint;
    bad_status bigint;
BEGIN
    SELECT count(*) INTO bad_origin FROM documents WHERE origin NOT IN ('class', 'external', 'web_cache');
    SELECT count(*) INTO bad_status FROM documents WHERE status NOT IN ('pending', 'indexed', 'failed', 'stale');
    IF bad_origin > 0 OR bad_status > 0 THEN
        RAISE EXCEPTION 'Migration 0002 stopped before any change. % documents rows have an origin outside (class, external, web_cache) and % have a status outside (pending, indexed, failed, stale). Correct or remove those rows deliberately, then run the upgrade again (docs/database-migrations.md).', bad_origin, bad_status;
    END IF;
END
$migration_0002_preflight$
"""

# The tables are locked first so no row can appear between the check and the drops.
DOWNGRADE_GUARD = """
DO $migration_0002_downgrade_guard$
DECLARE
    job_rows bigint;
    upload_rows bigint;
BEGIN
    LOCK TABLE ingestion_jobs, documents IN ACCESS EXCLUSIVE MODE;
    SELECT count(*) INTO job_rows FROM ingestion_jobs;
    SELECT count(*) INTO upload_rows FROM documents
    WHERE uploaded_by IS NOT NULL OR original_filename IS NOT NULL OR storage_key IS NOT NULL
       OR mime_type IS NOT NULL OR size_bytes IS NOT NULL OR page_count IS NOT NULL
       OR kind IS NOT NULL OR error_message IS NOT NULL;
    IF job_rows > 0 OR upload_rows > 0 THEN
        RAISE EXCEPTION 'Downgrade below 0002 refused before any change. % ingestion_jobs rows and % documents rows with upload metadata would be lost, and their stored files would no longer be referenced. Remove that data deliberately first (docs/database-migrations.md).', job_rows, upload_rows;
    END IF;
END
$migration_0002_downgrade_guard$
"""


def upgrade() -> None:
    op.execute(UPGRADE_PREFLIGHT)

    op.add_column("documents", sa.Column("uploaded_by", sa.UUID(), nullable=True))
    op.add_column("documents", sa.Column("original_filename", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("storage_key", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("mime_type", sa.String(length=100), nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("documents", sa.Column("page_count", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("kind", sa.String(length=20), nullable=True))
    op.add_column("documents", sa.Column("error_message", sa.Text(), nullable=True))

    op.create_foreign_key(op.f("documents_uploaded_by_fkey"), "documents", "users", ["uploaded_by"], ["id"])
    op.create_unique_constraint(op.f("documents_storage_key_key"), "documents", ["storage_key"])
    for name, condition in DOCUMENT_CHECKS.items():
        op.create_check_constraint(op.f(name), "documents", condition)
    op.create_index(op.f("documents_course_id_idx"), "documents", ["course_id"])
    op.create_index(
        op.f("documents_course_id_content_hash_idx"),
        "documents",
        ["course_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("origin = 'class' AND storage_key IS NOT NULL"),
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'queued'::character varying"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *(sa.CheckConstraint(condition, name=op.f(name)) for name, condition in JOB_CHECKS.items()),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("ingestion_jobs_document_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("ingestion_jobs_pkey")),
    )
    op.create_index(op.f("ingestion_jobs_document_id_idx"), "ingestion_jobs", ["document_id"])
    op.create_index(
        op.f("ingestion_jobs_document_id_kind_idx"),
        "ingestion_jobs",
        ["document_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    """Remove everything upgrade() added, only if that loses no data."""
    op.execute(DOWNGRADE_GUARD)

    op.drop_index(op.f("ingestion_jobs_document_id_kind_idx"), table_name="ingestion_jobs")
    op.drop_index(op.f("ingestion_jobs_document_id_idx"), table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")

    op.drop_index(op.f("documents_course_id_content_hash_idx"), table_name="documents")
    op.drop_index(op.f("documents_course_id_idx"), table_name="documents")
    for name in reversed(DOCUMENT_CHECKS):
        op.drop_constraint(op.f(name), "documents", type_="check")
    op.drop_constraint(op.f("documents_storage_key_key"), "documents", type_="unique")
    op.drop_constraint(op.f("documents_uploaded_by_fkey"), "documents", type_="foreignkey")
    for column in reversed(NEW_DOCUMENT_COLUMNS):
        op.drop_column("documents", column)
