"""StudyGraph schema v1 (draft).

This is CE-1's core deliverable. It covers the backbone tables the whole
team builds on. It is intentionally a starting point, not the final schema:
extend it as features land, and move to Alembic migrations (see the README)
before anyone has real data they care about.

Design notes:
- UUID primary keys everywhere (not serial ints) so ids are safe to expose.
- `chunks` is the single retrieval index: class files, external pages and
  web-cache pages all live here, told apart by `origin`.
- Embedding dimension is 384. If you ever switch embedding models you must
  re-embed everything, so pick the model before indexing anything real.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

EMBED_DIM = 384


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


# --- Identity -------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="student")  # student | professor | admin
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Course(Base):
    """A course, owned by one professor.

    Revision 0004 added `code` (the short course code shown next to the name,
    e.g. "CHEM 101"). Courses created before it carry the empty string, the
    documented fallback for "no code recorded" (docs/courses-api.md).

    join_code keeps the baseline's case-sensitive unique constraint. New codes
    are generated in upper case; a join prefers an exact match and otherwise
    tries the upper-case form of what the student typed (app/services/courses.py).
    """

    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint("code = btrim(code) AND code !~ '[[:cntrl:]]'", name="code"),
        Index(None, "owner_id"),
    )

    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))  # the professor
    name: Mapped[str] = mapped_column(String(150))
    join_code: Mapped[str] = mapped_column(String(12), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # --- added by revision 0004 ---
    code: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("''::character varying"))


class Enrollment(Base):
    """Membership of a user in a course; role `student` grants student access (M5 joins)."""

    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("user_id", "course_id"), Index(None, "course_id"))

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    role: Mapped[str] = mapped_column(String(20), default="student")


# --- Sources & documents --------------------------------------------------

class Source(Base):
    """A place external content can come from (an allowlisted domain, an API)."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = pk()
    kind: Mapped[str] = mapped_column(String(30))  # openstax | wikipedia | openalex | web
    domain: Mapped[str] = mapped_column(String(255), nullable=True)
    licence: Mapped[str] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # --- added by revision 0005 (M6 harvesting; NULL for every legacy source) ---
    # A provider-managed source: created disabled by the operator CLI, never seeded.
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)  # wikipedia
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    licence_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "provider IS NULL OR (provider = 'wikipedia' AND language IS NOT NULL AND language ~ '^[a-z]{2,3}$'"
            " AND licence_url IS NOT NULL)",
            name="provider",
        ),
        Index(None, "provider", "language", unique=True, postgresql_where=text("provider IS NOT NULL")),
    )


DOCUMENT_ORIGINS = ("class", "external", "web_cache")
DOCUMENT_STATUSES = ("pending", "indexed", "failed", "stale")
DOCUMENT_KINDS = ("slides", "notes", "syllabus", "textbook", "article")


class Document(Base):
    """A class upload, an external page or a web-cache page.

    Revision 0002 added the upload columns (uploaded_by .. error_message). They
    are nullable because legacy rows and external documents have no stored
    file; a row with a storage_key must carry complete file metadata
    (stored_file and class_upload checks). Every condition below is written so
    that it is TRUE or FALSE, never NULL: a CHECK that evaluates to NULL passes.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("origin IN ('class', 'external', 'web_cache')", name="origin"),
        CheckConstraint("status IN ('pending', 'indexed', 'failed', 'stale')", name="status"),
        CheckConstraint(
            "kind IS NULL OR kind IN ('slides', 'notes', 'syllabus', 'textbook', 'article')", name="kind"
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes > 0", name="size_bytes"),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="page_count"),
        CheckConstraint(
            "storage_key IS NULL OR ("
            "original_filename IS NOT NULL AND mime_type IS NOT NULL AND size_bytes IS NOT NULL "
            "AND content_hash IS NOT NULL AND content_hash ~ '^[0-9a-f]{64}$')",
            name="stored_file",
        ),
        CheckConstraint(
            "storage_key IS NULL OR origin <> 'class' OR (course_id IS NOT NULL AND uploaded_by IS NOT NULL)",
            name="class_upload",
        ),
        Index(None, "course_id"),
        # Course-scoped deduplication of stored class files. Legacy rows have no
        # storage_key, so their (possibly non-canonical) hashes are not part of it.
        Index(
            None,
            "course_id",
            "content_hash",
            unique=True,
            postgresql_where=text("origin = 'class' AND storage_key IS NOT NULL"),
        ),
        # M6: a harvested version is a fileless external document of a course and a source.
        CheckConstraint(
            "external_resource_id IS NULL OR (origin = 'external' AND course_id IS NOT NULL"
            " AND source_id IS NOT NULL AND storage_key IS NULL)",
            name="external_resource",
        ),
        # M6: a harvested document can only reference an external resource of its own course and source
        # (MATCH SIMPLE: class documents have no resource and are not checked).
        ForeignKeyConstraint(
            ["external_resource_id", "course_id", "source_id"],
            ["external_resources.id", "external_resources.course_id", "external_resources.source_id"],
            name="documents_external_resource_id_fkey",
        ),
        # Target of external_resources.current_document_id: that document must belong to the same resource.
        UniqueConstraint("id", "external_resource_id", name="documents_id_external_resource_id_key"),
        # At most one indexed and one pending version per external resource. A
        # replacement switches old indexed -> stale first, then new -> indexed.
        Index(
            "documents_external_resource_indexed_idx",
            "external_resource_id",
            unique=True,
            postgresql_where=text("status = 'indexed' AND external_resource_id IS NOT NULL"),
        ),
        Index(
            "documents_external_resource_pending_idx",
            "external_resource_id",
            unique=True,
            postgresql_where=text("status = 'pending' AND external_resource_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="class")  # class | external | web_cache
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text, nullable=True)
    licence: Mapped[str] = mapped_column(String(100), nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="en")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | indexed | failed | stale
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # Naive timestamp from the baseline schema, interpreted as UTC (see docs/document-ingestion.md).
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # --- added by revision 0002 (stored uploads) ---
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(20), nullable=True)  # slides | notes | syllabus | textbook | article
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # user-readable, shown by the web app
    # --- added by revision 0005 (M6): the resource this harvested version belongs to ---
    # The key is composite (see __table_args__): the resource must belong to this document's course and source.
    external_resource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


JOB_KINDS = ("parse", "index")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")


class IngestionJob(Base):
    """Durable record of one unit of ingestion work on a document.

    Written in the same transaction as the document it belongs to, so a job is
    never lost between the API and the queue: the row itself is the outbox the
    dispatcher publishes from and the record a worker claims. Transitions,
    applied with conditional UPDATEs (app/ingestion/jobs.py): queued -> running
    (claim: new attempt_token and lease) -> succeeded | failed | queued (retry
    while attempts remain, with backoff in available_at). A failed job stays
    failed; an operator retry creates a new job row.

    Revision 0003 added the scheduling and fencing columns (attempt_token ..
    last_dispatched_at). A running job always carries its attempt token and
    lease (lease check); every write made on behalf of an attempt is
    conditional on that token still being current.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('parse', 'index')", name="kind"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="status"),
        CheckConstraint(
            "max_attempts >= 1 AND attempt_count >= 0 AND attempt_count <= max_attempts", name="attempts"
        ),
        CheckConstraint(
            "(status = 'queued' AND finished_at IS NULL)"
            " OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND attempt_count >= 1)"
            " OR (status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
            " AND finished_at >= started_at AND attempt_count >= 1)"
            " OR (status = 'failed' AND finished_at IS NOT NULL"
            " AND (started_at IS NULL OR finished_at >= started_at))",
            name="lifecycle",
        ),
        CheckConstraint(
            "status <> 'running' OR (attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL"
            " AND lease_expires_at > started_at)",
            name="lease",
        ),
        Index(None, "document_id"),
        # At most one queued or running job of each kind per document.
        Index(None, "document_id", "kind", unique=True, postgresql_where=text("status IN ('queued', 'running')")),
    )

    id: Mapped[uuid.UUID] = pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'queued'::character varying")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # operational detail, never public
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- added by revision 0003 (dispatch, retry scheduling, attempt fencing) ---
    # Identity of the current (or last finishing) attempt; new for every claim.
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # A running attempt not finished by then is presumed dead (recovery).
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Neither claimed nor dispatched before this time: retry backoff.
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # When a message for the job was last reserved for publication; NULL = not published.
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Exactly {"page": <integer 1 .. 2^31-1>} (PDF pages, M3), or since revision 0005
# exactly {"anchor", "heading", "level"} (M6 section locators): level 0 is the
# lead section (anchor and heading null); levels 2..6 carry the provider's
# section anchor and heading text. The CASE makes every branch TRUE or FALSE.
SECTION_LOCATOR_CHECK = (
    "CASE WHEN jsonb_typeof(locator) = 'object' AND jsonb_typeof(locator -> 'page') = 'number'"
    " AND (locator - 'page') = '{}'::jsonb"
    " THEN (locator ->> 'page')::numeric BETWEEN 1 AND 2147483647"
    " AND (locator ->> 'page')::numeric = trunc((locator ->> 'page')::numeric)"
    " WHEN jsonb_typeof(locator) = 'object' AND (locator - 'anchor' - 'heading' - 'level') = '{}'::jsonb"
    " AND (locator -> 'anchor') IS NOT NULL AND (locator -> 'heading') IS NOT NULL"
    " AND jsonb_typeof(locator -> 'level') = 'number'"
    " THEN CASE WHEN (locator ->> 'level')::numeric = 0"
    " THEN jsonb_typeof(locator -> 'anchor') = 'null' AND jsonb_typeof(locator -> 'heading') = 'null'"
    " WHEN (locator ->> 'level')::numeric IN (2, 3, 4, 5, 6)"
    " THEN jsonb_typeof(locator -> 'anchor') = 'string' AND jsonb_typeof(locator -> 'heading') = 'string'"
    " AND char_length(locator ->> 'anchor') BETWEEN 1 AND 255"
    " AND char_length(locator ->> 'heading') BETWEEN 1 AND 255"
    " AND (locator ->> 'anchor') !~ '[[:cntrl:][:space:]]'"
    " AND (locator ->> 'heading') !~ '[[:cntrl:]]'"
    " AND (locator ->> 'heading') = btrim(locator ->> 'heading')"
    " ELSE false END"
    " ELSE false END"
)


class DocumentSection(Base):
    """Normalized text of one source page of a parsed document (M3, CE-1).

    Sections are what parsing produces and what indexing (AI-1) consumes; they
    are not retrieval chunks. One section per page that has text: `ordinal` is
    0-based and contiguous within a document, in page order; `locator` is
    {"page": n} with the 1-based physical page number, so pages without text
    leave gaps in page numbers but never in ordinals. A document's sections
    are replaced as one set, in the transaction that completes its parse job.
    """

    __tablename__ = "document_sections"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal"),
        CheckConstraint("btrim(text, E' \\t\\n\\r') <> ''", name="text"),
        # Exactly {"page": <integer 1 .. 2^31-1>}. The CASE keeps the cast from
        # running on non-numbers and makes a missing key FALSE, never NULL.
        CheckConstraint(SECTION_LOCATOR_CHECK, name="locator"),
        UniqueConstraint("document_id", "ordinal"),
    )

    id: Mapped[uuid.UUID] = pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StorageIdentity(Base):
    """Which upload store this database's stored files live in (single row, M3).

    `storage_id` must equal the id in the store's marker file
    (<UPLOAD_DIR>/.studygraph-storage) before the API stores an upload or a
    worker claims a job, so a wrong, empty or missing mount fails closed
    instead of making documents look like they lost their files. Written only
    by the explicit `init-storage` command (app/storage_identity.py).
    """

    __tablename__ = "storage_identity"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    storage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# --- External harvesting (M6, revision 0005, docs/external-harvesting.md) ---------
#
# Lock order, used by every transaction that takes more than one of these rows
# (harvest persistence, index completion, scheduling, opt-out, source disable):
#   harvest_course_settings -> sources -> harvest_targets -> harvest_runs
#   -> external_resources -> ingestion_jobs -> documents (new version, then old)


class HarvestCourseSettings(Base):
    """Explicit course opt-in (enabled is false until an operator turns it on), quota, dirty generation."""

    __tablename__ = "harvest_course_settings"
    __table_args__ = (
        CheckConstraint("max_active_documents BETWEEN 1 AND 500", name="quota"),
        CheckConstraint("dirty_generation >= 0", name="generation"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    max_active_documents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    # Bumped by every confirmed class indexing of the course (never by external indexing).
    dirty_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HarvestTarget(Base):
    """A course x provider source selected for harvesting, with its schedule."""

    __tablename__ = "harvest_targets"
    __table_args__ = (
        CheckConstraint("harvested_generation >= 0", name="generation"),
        CheckConstraint("refresh_interval_seconds BETWEEN 3600 AND 31536000", name="refresh"),
        Index(None, "source_id"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("harvest_course_settings.course_id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    harvested_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    refresh_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("604800"))
    next_refresh_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # No run is scheduled before this (backoff after a failed run).
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HarvestQuery(Base):
    """A public query an operator approved for a course topic, bound to its exact text."""

    __tablename__ = "harvest_queries"
    __table_args__ = (
        CheckConstraint(
            "query_text = btrim(query_text) AND char_length(query_text) BETWEEN 2 AND 120"
            " AND query_text !~ '[[:cntrl:]]'",
            name="text",
        ),
        CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name="status",
        ),
        Index(None, "course_id", "topic_id", unique=True, postgresql_where=text("status = 'approved'")),
        Index(None, "topic_id"),
    )

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("harvest_course_settings.course_id", ondelete="CASCADE"), nullable=False
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), nullable=False)
    query_text: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'approved'::character varying")
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HarvestProviderBudget(Base):
    """Request budget of one source: a fixed window, a minimum spacing and a Retry-After block."""

    __tablename__ = "harvest_provider_budgets"
    __table_args__ = (
        CheckConstraint(
            "window_seconds BETWEEN 1 AND 86400 AND max_requests BETWEEN 1 AND 100000"
            " AND min_interval_ms BETWEEN 0 AND 600000 AND used_requests BETWEEN 0 AND max_requests",
            name="limits",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    min_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    used_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # No request to this provider before this time (spacing, Retry-After, maxlag).
    blocked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


HARVEST_RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class HarvestRun(Base):
    """One durable harvest of a course x source (app/harvest/runs.py).

    claim_count counts every claim (bounded slices of work, max_claims makes
    the run finite); failure_count counts only failed attempts (errors, lost
    leases). Neither is ever decremented. Progress is persisted in `progress`
    under the attempt token, so a yield or a failure never repeats work.
    """

    __tablename__ = "harvest_runs"
    __table_args__ = (
        CheckConstraint("trigger IN ('dirty', 'refresh', 'manual')", name="trigger"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="status"),
        CheckConstraint(
            "generation >= 0 AND max_claims >= 1 AND claim_count BETWEEN 0 AND max_claims"
            " AND max_failures >= 1 AND failure_count BETWEEN 0 AND max_failures",
            name="counters",
        ),
        CheckConstraint(
            "(status = 'queued' AND finished_at IS NULL AND attempt_token IS NULL AND lease_expires_at IS NULL)"
            " OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND claim_count >= 1"
            " AND attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_expires_at > started_at)"
            " OR (status IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL"
            " AND lease_expires_at IS NULL)",
            name="lifecycle",
        ),
        CheckConstraint("jsonb_typeof(progress) = 'object'", name="progress"),
        ForeignKeyConstraint(
            ["course_id", "source_id"],
            ["harvest_targets.course_id", "harvest_targets.source_id"],
            ondelete="CASCADE",
        ),
        # One active run per target: the scheduler's race guard.
        Index(None, "course_id", "source_id", unique=True, postgresql_where=text("status IN ('queued', 'running')")),
        Index(None, "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'queued'::character varying"))
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_claims: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExternalResource(Base):
    """Durable identity of one provider page within a course: revision tracking, dedup, missing evidence."""

    __tablename__ = "external_resources"
    __table_args__ = (
        CheckConstraint("state IN ('accepted', 'duplicate', 'missing')", name="state"),
        CheckConstraint("page_id > 0", name="page_id"),
        CheckConstraint("content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256"),
        CheckConstraint("missing_checks >= 0 AND ((missing_checks = 0) = (missing_since IS NULL))", name="missing"),
        UniqueConstraint("course_id", "source_id", "page_id"),
        UniqueConstraint("id", "course_id", "source_id", name="external_resources_id_course_id_source_id_key"),
        ForeignKeyConstraint(
            ["current_document_id", "id"],
            ["documents.id", "documents.external_resource_id"],
            name="external_resources_current_document_id_fkey",
            use_alter=True,
        ),
        # Concurrency-safe course-scoped deduplication of accepted content.
        Index(None, "course_id", "content_sha256", unique=True, postgresql_where=text("state = 'accepted'")),
        Index(None, "source_id", "next_check_at"),
    )

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    page_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_resources.id", ondelete="SET NULL"), nullable=True
    )
    # The latest accepted version (pending, indexed or failed); the visible
    # version is the one whose document is `indexed`. The composite key in
    # __table_args__ (use_alter: the tables reference each other) makes the
    # document exist and belong to this resource. Written only under this row's lock.
    current_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_revision_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discovered_by_query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("harvest_queries.id", ondelete="SET NULL"), nullable=True
    )
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missing_checks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    missing_evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExternalDocumentVersion(Base):
    """Provenance of one harvested document version; immutable except for its stale marking."""

    __tablename__ = "external_document_versions"
    __table_args__ = (
        CheckConstraint("version >= 1 AND page_id > 0 AND revision_id > 0", name="identity"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256"),
        CheckConstraint(
            "(stale_reason IS NULL AND staled_at IS NULL AND stale_evidence IS NULL)"
            " OR (stale_reason = 'superseded' AND staled_at IS NOT NULL)"
            " OR (stale_reason = 'missing' AND staled_at IS NOT NULL AND stale_evidence IS NOT NULL)",
            name="stale",
        ),
        UniqueConstraint("resource_id", "version"),
        Index(None, "harvest_run_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("external_resources.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    page_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    revision_url: Mapped[str] = mapped_column(Text, nullable=False)
    licence: Mapped[str] = mapped_column(String(100), nullable=False)
    licence_url: Mapped[str] = mapped_column(Text, nullable=False)
    attribution: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    harvest_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("harvest_runs.id", ondelete="SET NULL"), nullable=True
    )
    stale_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)  # superseded | missing
    stale_evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    staled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# --- The one index --------------------------------------------------------

class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="class")
    text: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=True)  # {"page": 6} or {"heading": "4.4"}
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=True)
    # A generated tsvector for sparse/keyword search is added via migration;
    # keeping it out of the ORM keeps this file simple. See README.


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (Index(None, "course_id"),)

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    name: Mapped[str] = mapped_column(String(150))


class ChunkTopic(Base):
    __tablename__ = "chunk_topics"
    __table_args__ = (UniqueConstraint("chunk_id", "topic_id"),)

    id: Mapped[uuid.UUID] = pk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chunks.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"))


# --- Practice & learner model --------------------------------------------

class Question(Base):
    __tablename__ = "questions"

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(20), default="practice")  # practice | exam
    qtype: Mapped[str] = mapped_column(String(20), default="mcq")
    difficulty: Mapped[str] = mapped_column(String(20), default="medium")
    text: Mapped[str] = mapped_column(Text)
    choices: Mapped[dict] = mapped_column(JSONB, nullable=True)
    answer: Mapped[str] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=True)
    citations: Mapped[dict] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | approved
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"), nullable=True)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    hint_count: Mapped[int] = mapped_column(Integer, default=0)
    response_ms: Mapped[int] = mapped_column(Integer, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MasteryScore(Base):
    __tablename__ = "mastery_scores"
    __table_args__ = (UniqueConstraint("user_id", "topic_id"),)

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"))
    mastery: Mapped[float] = mapped_column(Float, default=0.0)  # output of the Model 2 classifier
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_review_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
