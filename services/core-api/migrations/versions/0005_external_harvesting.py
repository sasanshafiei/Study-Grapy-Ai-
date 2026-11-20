"""External harvesting: provenance, section locators, scheduling, approved queries, budgets (M6).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

Added (docs/external-harvesting.md):

- sources: provider, language, licence_url. NULL for every existing row, so
  legacy sources are untouched and never become harvest providers. A CHECK
  allows only provider 'wikipedia' (with a language code); a partial unique
  index keeps one source per (provider, language). Nothing is seeded: no
  provider exists, let alone an enabled one, until an operator creates it.
- documents.external_resource_id (NULL for every existing row) links a
  harvested document version to its durable resource identity. A CHECK
  limits it to fileless external documents with a course and a source. Two
  partial unique indexes allow at most one `indexed` and one `pending`
  version per resource.
- document_sections.locator: the CHECK accepts, besides {"page": n}, the
  genuine section locator {"anchor", "heading", "level"} (level 0 = lead
  section with null anchor and heading; levels 2..6 = a heading whose anchor
  and heading text come from the provider's section list). Every existing
  row satisfies the old check and therefore the new one.
- harvest_course_settings: explicit course opt-in (enabled defaults to false),
  per-course active-document quota, dirty generation (bumped by class
  indexing).
- harvest_targets: a course x source pair selected for harvesting, with its
  harvested generation, refresh schedule and failure backoff.
- harvest_queries: operator-approved public queries, bound to the exact text.
- harvest_provider_budgets: per-source request window and Retry-After block.
- harvest_runs: durable harvest jobs (claims, leases, attempt fencing,
  failures, dispatch reservations, persisted progress); one active run per
  target (partial unique index).
- external_resources: durable identity of a provider page within a course,
  its revision tracking, deduplication state and missing-page evidence.
  current_document_id is constrained by a composite foreign key
  (current_document_id, id) -> documents (id, external_resource_id): the
  document must exist AND belong to this very resource. The two tables
  reference each other, so that key is added after both exist (and dropped
  first on downgrade); a NULL current_document_id (no version yet) passes
  the key (MATCH SIMPLE). In the other direction, documents (external_resource_id,
  course_id, source_id) -> external_resources (id, course_id, source_id) means
  a harvested document can only reference a resource of its own course and
  source. Both keys are NO ACTION: nothing here is ever deleted one row at a time.
- external_document_versions: per-document provenance (revision, licence,
  attribution, content hash, fetch time, stale reason and evidence).

Uniqueness. Every new unique constraint or index covers only new tables or
new columns that are NULL for every existing row, so no legacy row can
violate it. The preflight below verifies that explicitly (and the locator
shape of every existing section) under lock, before any change.

Data safety:
- upgrade locks sources, documents and document_sections against concurrent
  writes, verifies the preconditions above and never modifies existing values.
- downgrade refuses, before any change, while any M6 state exists: rows in
  the new tables, provider-managed sources, documents linked to a resource or
  sections with a section locator. Remove that state deliberately first.
Both run as SQL, so they also apply to `alembic upgrade --sql` /
`downgrade --sql` output executed later.

Self-contained on purpose: it does not import app.models.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PAGE_LOCATOR = (
    "jsonb_typeof(locator) = 'object' AND jsonb_typeof(locator -> 'page') = 'number'"
    " AND (locator - 'page') = '{}'::jsonb"
)
PAGE_VALUE = (
    "(locator ->> 'page')::numeric BETWEEN 1 AND 2147483647"
    " AND (locator ->> 'page')::numeric = trunc((locator ->> 'page')::numeric)"
)
OLD_LOCATOR_CHECK = f"CASE WHEN {PAGE_LOCATOR} THEN {PAGE_VALUE} ELSE false END"
SECTION_LOCATOR = (
    "jsonb_typeof(locator) = 'object' AND (locator - 'anchor' - 'heading' - 'level') = '{}'::jsonb"
    " AND (locator -> 'anchor') IS NOT NULL AND (locator -> 'heading') IS NOT NULL"
    " AND jsonb_typeof(locator -> 'level') = 'number'"
)
SECTION_VALUE = (
    "CASE WHEN (locator ->> 'level')::numeric = 0"
    " THEN jsonb_typeof(locator -> 'anchor') = 'null' AND jsonb_typeof(locator -> 'heading') = 'null'"
    " WHEN (locator ->> 'level')::numeric IN (2, 3, 4, 5, 6)"
    " THEN jsonb_typeof(locator -> 'anchor') = 'string' AND jsonb_typeof(locator -> 'heading') = 'string'"
    " AND char_length(locator ->> 'anchor') BETWEEN 1 AND 255"
    " AND char_length(locator ->> 'heading') BETWEEN 1 AND 255"
    " AND (locator ->> 'anchor') !~ '[[:cntrl:][:space:]]'"
    " AND (locator ->> 'heading') !~ '[[:cntrl:]]'"
    " AND (locator ->> 'heading') = btrim(locator ->> 'heading')"
    " ELSE false END"
)
NEW_LOCATOR_CHECK = (
    f"CASE WHEN {PAGE_LOCATOR} THEN {PAGE_VALUE} WHEN {SECTION_LOCATOR} THEN {SECTION_VALUE} ELSE false END"
)

SOURCE_PROVIDER_CHECK = (
    "provider IS NULL OR (provider = 'wikipedia' AND language IS NOT NULL AND language ~ '^[a-z]{2,3}$'"
    " AND licence_url IS NOT NULL)"
)
DOCUMENT_EXTERNAL_RESOURCE_CHECK = (
    "external_resource_id IS NULL OR (origin = 'external' AND course_id IS NOT NULL"
    " AND source_id IS NOT NULL AND storage_key IS NULL)"
)
RUN_LIFECYCLE_CHECK = (
    "(status = 'queued' AND finished_at IS NULL AND attempt_token IS NULL AND lease_expires_at IS NULL)"
    " OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND claim_count >= 1"
    " AND attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_expires_at > started_at)"
    " OR (status IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL"
    " AND lease_expires_at IS NULL)"
)
HEX64 = "~ '^[0-9a-f]{64}$'"

UPGRADE_PREFLIGHT = """
DO $migration_0005_preflight$
DECLARE
    bad_sections bigint;
BEGIN
    LOCK TABLE sources, documents, document_sections IN SHARE ROW EXCLUSIVE MODE;
    SELECT count(*) INTO bad_sections FROM document_sections
    WHERE NOT (CASE WHEN jsonb_typeof(locator) = 'object' AND jsonb_typeof(locator -> 'page') = 'number'
                     AND (locator - 'page') = '{}'::jsonb
                    THEN (locator ->> 'page')::numeric BETWEEN 1 AND 2147483647
                     AND (locator ->> 'page')::numeric = trunc((locator ->> 'page')::numeric)
                    ELSE false END);
    IF bad_sections > 0 THEN
        RAISE EXCEPTION 'Migration 0005 stopped before any change. % document_sections rows do not carry a valid page locator. Inspect them first (docs/database-migrations.md).', bad_sections;
    END IF;
END
$migration_0005_preflight$
"""

DOWNGRADE_GUARD = """
DO $migration_0005_downgrade_guard$
DECLARE
    m6_rows bigint;
    managed_sources bigint;
    linked_documents bigint;
    section_locators bigint;
BEGIN
    LOCK TABLE sources, documents, document_sections, harvest_course_settings, harvest_targets,
        harvest_queries, harvest_provider_budgets, harvest_runs, external_resources,
        external_document_versions IN ACCESS EXCLUSIVE MODE;
    SELECT (SELECT count(*) FROM harvest_course_settings) + (SELECT count(*) FROM harvest_targets)
         + (SELECT count(*) FROM harvest_queries) + (SELECT count(*) FROM harvest_provider_budgets)
         + (SELECT count(*) FROM harvest_runs) + (SELECT count(*) FROM external_resources)
         + (SELECT count(*) FROM external_document_versions)
      INTO m6_rows;
    SELECT count(*) INTO managed_sources FROM sources
    WHERE provider IS NOT NULL OR language IS NOT NULL OR licence_url IS NOT NULL;
    SELECT count(*) INTO linked_documents FROM documents WHERE external_resource_id IS NOT NULL;
    SELECT count(*) INTO section_locators FROM document_sections WHERE (locator -> 'page') IS NULL;
    IF m6_rows > 0 OR managed_sources > 0 OR linked_documents > 0 OR section_locators > 0 THEN
        RAISE EXCEPTION 'Downgrade below 0005 refused before any change. % harvesting rows, % provider-managed sources, % documents linked to an external resource and % sections with a section locator would be lost. Remove that state deliberately first (docs/database-migrations.md).', m6_rows, managed_sources, linked_documents, section_locators;
    END IF;
END
$migration_0005_downgrade_guard$
"""


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.execute(UPGRADE_PREFLIGHT)

    # --- sources: provider identity (NULL for every legacy row) ---------------------------
    op.add_column("sources", sa.Column("provider", sa.String(length=30), nullable=True))
    op.add_column("sources", sa.Column("language", sa.String(length=10), nullable=True))
    op.add_column("sources", sa.Column("licence_url", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("sources_provider_check"), "sources", SOURCE_PROVIDER_CHECK)
    op.create_index(
        op.f("sources_provider_language_idx"),
        "sources",
        ["provider", "language"],
        unique=True,
        postgresql_where=sa.text("provider IS NOT NULL"),
    )

    # --- section locators -------------------------------------------------------------------
    op.drop_constraint(op.f("document_sections_locator_check"), "document_sections", type_="check")
    op.create_check_constraint(op.f("document_sections_locator_check"), "document_sections", NEW_LOCATOR_CHECK)

    # --- course opt-in and targets ------------------------------------------------------------
    op.create_table(
        "harvest_course_settings",
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_active_documents", sa.Integer(), server_default=sa.text("20"), nullable=False),
        sa.Column("dirty_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("max_active_documents BETWEEN 1 AND 500", name=op.f("harvest_course_settings_quota_check")),
        sa.CheckConstraint("dirty_generation >= 0", name=op.f("harvest_course_settings_generation_check")),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("harvest_course_settings_course_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("course_id", name=op.f("harvest_course_settings_pkey")),
    )
    op.create_table(
        "harvest_targets",
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("harvested_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("refresh_interval_seconds", sa.Integer(), server_default=sa.text("604800"), nullable=False),
        sa.Column("next_refresh_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("harvested_generation >= 0", name=op.f("harvest_targets_generation_check")),
        sa.CheckConstraint(
            "refresh_interval_seconds BETWEEN 3600 AND 31536000", name=op.f("harvest_targets_refresh_check")
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["harvest_course_settings.course_id"],
            name=op.f("harvest_targets_course_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("harvest_targets_source_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("course_id", "source_id", name=op.f("harvest_targets_pkey")),
    )
    op.create_index(op.f("harvest_targets_source_id_idx"), "harvest_targets", ["source_id"])

    # --- approved queries -------------------------------------------------------------------
    op.create_table(
        "harvest_queries",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=False),
        sa.Column("query_text", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'approved'::character varying"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "query_text = btrim(query_text) AND char_length(query_text) BETWEEN 2 AND 120"
            " AND query_text !~ '[[:cntrl:]]'",
            name=op.f("harvest_queries_text_check"),
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name=op.f("harvest_queries_status_check"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["harvest_course_settings.course_id"],
            name=op.f("harvest_queries_course_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"], ["topics.id"], name=op.f("harvest_queries_topic_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("harvest_queries_pkey")),
    )
    op.create_index(
        op.f("harvest_queries_course_id_topic_id_idx"),
        "harvest_queries",
        ["course_id", "topic_id"],
        unique=True,
        postgresql_where=sa.text("status = 'approved'"),
    )
    op.create_index(op.f("harvest_queries_topic_id_idx"), "harvest_queries", ["topic_id"])

    # --- provider budgets -------------------------------------------------------------------
    op.create_table(
        "harvest_provider_budgets",
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("max_requests", sa.Integer(), nullable=False),
        sa.Column("min_interval_ms", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("used_requests", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "window_seconds BETWEEN 1 AND 86400 AND max_requests BETWEEN 1 AND 100000"
            " AND min_interval_ms BETWEEN 0 AND 600000 AND used_requests BETWEEN 0 AND max_requests",
            name=op.f("harvest_provider_budgets_limits_check"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("harvest_provider_budgets_source_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("harvest_provider_budgets_pkey")),
    )

    # --- harvest runs -------------------------------------------------------------------------
    op.create_table(
        "harvest_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'queued'::character varying"), nullable=False),
        sa.Column("claim_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_claims", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_failures", sa.Integer(), nullable=False),
        sa.Column("attempt_token", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("trigger IN ('dirty', 'refresh', 'manual')", name=op.f("harvest_runs_trigger_check")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name=op.f("harvest_runs_status_check")
        ),
        sa.CheckConstraint(
            "generation >= 0 AND max_claims >= 1 AND claim_count BETWEEN 0 AND max_claims"
            " AND max_failures >= 1 AND failure_count BETWEEN 0 AND max_failures",
            name=op.f("harvest_runs_counters_check"),
        ),
        sa.CheckConstraint(RUN_LIFECYCLE_CHECK, name=op.f("harvest_runs_lifecycle_check")),
        sa.CheckConstraint("jsonb_typeof(progress) = 'object'", name=op.f("harvest_runs_progress_check")),
        sa.ForeignKeyConstraint(
            ["course_id", "source_id"],
            ["harvest_targets.course_id", "harvest_targets.source_id"],
            name=op.f("harvest_runs_course_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("harvest_runs_pkey")),
    )
    op.create_index(
        op.f("harvest_runs_course_id_source_id_idx"),
        "harvest_runs",
        ["course_id", "source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(op.f("harvest_runs_status_available_at_idx"), "harvest_runs", ["status", "available_at"])

    # --- external resources and document provenance -------------------------------------------
    op.create_table(
        "external_resources",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("page_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("duplicate_of_id", sa.UUID(), nullable=True),
        sa.Column("current_document_id", sa.UUID(), nullable=True),
        sa.Column("last_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("discovered_by_query_id", sa.UUID(), nullable=True),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing_checks", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("missing_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("state IN ('accepted', 'duplicate', 'missing')", name=op.f("external_resources_state_check")),
        sa.CheckConstraint("page_id > 0", name=op.f("external_resources_page_id_check")),
        sa.CheckConstraint(
            f"content_sha256 IS NULL OR content_sha256 {HEX64}", name=op.f("external_resources_content_sha256_check")
        ),
        sa.CheckConstraint(
            "missing_checks >= 0 AND ((missing_checks = 0) = (missing_since IS NULL))",
            name=op.f("external_resources_missing_check"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("external_resources_course_id_fkey"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("external_resources_source_id_fkey")),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"],
            ["external_resources.id"],
            name=op.f("external_resources_duplicate_of_id_fkey"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["discovered_by_query_id"],
            ["harvest_queries.id"],
            name=op.f("external_resources_discovered_by_query_id_fkey"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("external_resources_pkey")),
        sa.UniqueConstraint(
            "course_id", "source_id", "page_id", name=op.f("external_resources_course_id_source_id_page_id_key")
        ),
        # Target of documents' composite key: a document can only reference a resource of its own course and source.
        sa.UniqueConstraint("id", "course_id", "source_id", name=op.f("external_resources_id_course_id_source_id_key")),
    )
    op.create_index(
        op.f("external_resources_course_id_content_sha256_idx"),
        "external_resources",
        ["course_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text("state = 'accepted'"),
    )
    op.create_index(
        op.f("external_resources_source_id_next_check_at_idx"), "external_resources", ["source_id", "next_check_at"]
    )

    op.add_column("documents", sa.Column("external_resource_id", sa.UUID(), nullable=True))
    op.create_unique_constraint(
        op.f("documents_id_external_resource_id_key"), "documents", ["id", "external_resource_id"]
    )  # target of external_resources.current_document_id's composite key
    op.create_foreign_key(
        op.f("documents_external_resource_id_fkey"),
        "documents",
        "external_resources",
        ["external_resource_id", "course_id", "source_id"],
        ["id", "course_id", "source_id"],
    )
    op.create_check_constraint(op.f("documents_external_resource_check"), "documents", DOCUMENT_EXTERNAL_RESOURCE_CHECK)
    op.create_index(
        "documents_external_resource_indexed_idx",
        "documents",
        ["external_resource_id"],
        unique=True,
        postgresql_where=sa.text("status = 'indexed' AND external_resource_id IS NOT NULL"),
    )
    op.create_index(
        "documents_external_resource_pending_idx",
        "documents",
        ["external_resource_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND external_resource_id IS NOT NULL"),
    )

    # The cycle external_resources <-> documents: this key can only be created now that both exist.
    op.create_foreign_key(
        op.f("external_resources_current_document_id_fkey"),
        "external_resources",
        "documents",
        ["current_document_id", "id"],
        ["id", "external_resource_id"],
    )

    op.create_table(
        "external_document_versions",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.BigInteger(), nullable=False),
        sa.Column("revision_id", sa.BigInteger(), nullable=False),
        sa.Column("revision_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("revision_url", sa.Text(), nullable=False),
        sa.Column("licence", sa.String(length=100), nullable=False),
        sa.Column("licence_url", sa.Text(), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("harvest_run_id", sa.UUID(), nullable=True),
        sa.Column("stale_reason", sa.String(length=20), nullable=True),
        sa.Column("stale_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("staled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "version >= 1 AND page_id > 0 AND revision_id > 0", name=op.f("external_document_versions_identity_check")
        ),
        sa.CheckConstraint(
            f"content_sha256 {HEX64}", name=op.f("external_document_versions_content_sha256_check")
        ),
        sa.CheckConstraint(
            "(stale_reason IS NULL AND staled_at IS NULL AND stale_evidence IS NULL)"
            " OR (stale_reason = 'superseded' AND staled_at IS NOT NULL)"
            " OR (stale_reason = 'missing' AND staled_at IS NOT NULL AND stale_evidence IS NOT NULL)",
            name=op.f("external_document_versions_stale_check"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("external_document_versions_document_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["external_resources.id"],
            name=op.f("external_document_versions_resource_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["harvest_run_id"],
            ["harvest_runs.id"],
            name=op.f("external_document_versions_harvest_run_id_fkey"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("document_id", name=op.f("external_document_versions_pkey")),
        sa.UniqueConstraint(
            "resource_id", "version", name=op.f("external_document_versions_resource_id_version_key")
        ),
    )
    op.create_index(
        op.f("external_document_versions_harvest_run_id_idx"), "external_document_versions", ["harvest_run_id"]
    )


def downgrade() -> None:
    """Remove everything upgrade() added, only if no M6 state would be lost."""
    op.execute(DOWNGRADE_GUARD)
    op.drop_table("external_document_versions")
    op.drop_constraint(op.f("external_resources_current_document_id_fkey"), "external_resources", type_="foreignkey")
    op.drop_index("documents_external_resource_pending_idx", table_name="documents")
    op.drop_index("documents_external_resource_indexed_idx", table_name="documents")
    op.drop_constraint(op.f("documents_external_resource_check"), "documents", type_="check")
    op.drop_constraint(op.f("documents_external_resource_id_fkey"), "documents", type_="foreignkey")
    op.drop_constraint(op.f("documents_id_external_resource_id_key"), "documents", type_="unique")
    op.drop_column("documents", "external_resource_id")
    op.drop_table("external_resources")
    op.drop_table("harvest_runs")
    op.drop_table("harvest_provider_budgets")
    op.drop_table("harvest_queries")
    op.drop_table("harvest_targets")
    op.drop_table("harvest_course_settings")
    op.drop_constraint(op.f("document_sections_locator_check"), "document_sections", type_="check")
    op.create_check_constraint(op.f("document_sections_locator_check"), "document_sections", OLD_LOCATOR_CHECK)
    op.drop_index(op.f("sources_provider_language_idx"), table_name="sources", postgresql_where=sa.text("provider IS NOT NULL"))
    op.drop_constraint(op.f("sources_provider_check"), "sources", type_="check")
    op.drop_column("sources", "licence_url")
    op.drop_column("sources", "language")
    op.drop_column("sources", "provider")
