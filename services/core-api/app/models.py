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
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .schema_checks import (  # shared verbatim with migrations 0007, 0008 and 0012
    ADMIN_AUDIT_SHAPE_CHECK,
    AUDIT_SHAPE_CHECK,
    BLUEPRINT_VALUES_CHECK,
    EXAM_FIELDS_CHECK,
    EXAM_FINGERPRINT_CHECK,
    EXAM_ITEM_PURPOSE_CHECK,
    EXAM_ITEM_VALUES_CHECK,
    EXAM_STATE_CHECK,
    EXAM_STATUS_CHECK,
    IDEMPOTENCY_CHECK,
    MODEL_STATE_CHECK,
    OUTBOX_LIFECYCLE_CHECK,
    OUTBOX_STATUS_CHECK,
    QUESTION_BANK_CHECK,
    QUESTION_BLOOM_CHECK,
    RELEASE_PAYLOAD_CHECK,
    RELEASE_WEEK_CHECK,
    REVISION_CONTENT_CHECK,
    REVISION_DOMAIN_CHECK,
    REVISION_PURPOSE_CHECK,
    SCORE_PROVENANCE_CHECK,
    STREAM_BATCH_CHECK,
    STREAM_COUNTERS_CHECK,
    STREAM_ERROR_CHECK,
    STREAM_LEASE_CHECK,
    STREAM_STATE_CHECK,
    STUDY_EVENT_SHAPE_CHECK,
    STUDY_EVENT_TIME_CHECK,
    STUDY_EVENT_TYPE_CHECK,
)

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
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)  # wikipedia | openstax
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    licence_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- added by revision 0011 (WP-6): the allowlisted OpenStax book of an openstax source (else NULL).
    # A BEFORE INSERT trigger stores every new openstax row disabled; only enable-source activates it.
    provider_book: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(provider IS NULL AND provider_book IS NULL)"
            " OR (provider = 'wikipedia' AND language IS NOT NULL AND language ~ '^[a-z]{2,3}$'"
            " AND licence_url IS NOT NULL AND provider_book IS NULL)"
            " OR (provider = 'openstax' AND language IS NOT NULL AND language = 'en' AND licence_url IS NOT NULL"
            " AND provider_book IS NOT NULL AND provider_book ~ '^[a-z0-9]+(-[a-z0-9]+)*$')",
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
        # M8 (revision 0007): target of study_events' course-consistent document key.
        UniqueConstraint("id", "course_id"),
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
        CheckConstraint(
            "last_revision_key IS NULL OR (last_revision_key ~ '^[0-9a-f]{40}$' AND last_revision_id IS NULL)",
            name="last_revision_key",
        ),
        CheckConstraint("missing_checks >= 0 AND ((missing_checks = 0) = (missing_since IS NULL))", name="missing"),
        CheckConstraint(
            "duplicate_content_sha256 IS NULL OR (state = 'accepted' AND duplicate_content_sha256 ~ '^[0-9a-f]{64}$'"
            " AND duplicate_content_sha256 IS DISTINCT FROM content_sha256)",
            name="duplicate_content_sha256",
        ),
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
    # Accepted resources only (revision 0010): the digest of the newest checked revision (last_revision_id) when
    # its text duplicates content another accepted resource holds. content_sha256 stays the key of the visible
    # version; the revision is reconsidered once nobody holds this digest (app/harvest/persist.py).
    duplicate_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Revision 0011 (WP-6): a provider's non-numeric version identity (OpenStax: git blob SHA-1 of the module
    # file), set instead of last_revision_id; never both.
    last_revision_key: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ExternalDocumentVersion(Base):
    """Provenance of one harvested document version; immutable except for its stale marking."""

    __tablename__ = "external_document_versions"
    __table_args__ = (
        CheckConstraint(
            "version >= 1 AND page_id > 0 AND ((revision_id IS NOT NULL AND revision_id > 0 AND revision_key IS NULL)"
            " OR (revision_id IS NULL AND revision_key IS NOT NULL AND revision_key ~ '^[0-9a-f]{40}$'))",
            name="identity",
        ),
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
    revision_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # Wikipedia revision id
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
    # Revision 0011 (WP-6): OpenStax's version identity (git blob SHA-1 of the module file), set instead of
    # revision_id; exactly one of the two is set.
    revision_key: Mapped[str | None] = mapped_column(String(40), nullable=True)


# --- The one index --------------------------------------------------------

class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        ForeignKeyConstraint(["document_id", "course_id"], ["documents.id", "documents.course_id"],
                             name="index_generations_document_course_fkey", ondelete="CASCADE"),
        UniqueConstraint("document_id", "course_id", "idempotency_key", name="index_generations_identity_key"),
        CheckConstraint("section_count > 0 AND chunk_count >= section_count", name="counts"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'", name="hashes"),
        CheckConstraint("model_name ~ '^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$' AND "
                        "model_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$'", name="model"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    course_id: Mapped[uuid.UUID]
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    content_digest: Mapped[str] = mapped_column(String(64))
    section_count: Mapped[int]
    chunk_count: Mapped[int]
    model_name: Mapped[str] = mapped_column(String(100))
    model_version: Mapped[str] = mapped_column(String(100))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("id", "course_id"),
        ForeignKeyConstraint(["document_id", "course_id", "generation_key"],
                             ["index_generations.document_id", "index_generations.course_id",
                              "index_generations.idempotency_key"], name="chunks_generation_fkey", ondelete="CASCADE"),
        CheckConstraint("(generation_key IS NULL AND section_ordinal IS NULL) OR "
                        "(generation_key IS NOT NULL AND course_id IS NOT NULL AND section_ordinal IS NOT NULL "
                        "AND section_ordinal >= 0 AND embedding IS NOT NULL AND vector_norm(embedding) > 0 "
                        "AND locator IS NOT NULL AND text ~ '[^[:space:]]')", name="generation"),
        Index(None, "document_id", "generation_key"),
    )

    id: Mapped[uuid.UUID] = pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="class")
    text: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=True)  # {"page": 6} or {"heading": "4.4"}
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=True)
    generation_key: Mapped[str | None] = mapped_column(String(64))
    section_ordinal: Mapped[int | None]


class Topic(Base):
    __tablename__ = "topics"
    # (id, course_id) is unique since revision 0006: the target of every course-consistent topic key.
    __table_args__ = (Index(None, "course_id"), UniqueConstraint("id", "course_id"))

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    name: Mapped[str] = mapped_column(String(150))


class ChunkTopic(Base):
    __tablename__ = "chunk_topics"
    __table_args__ = (
        UniqueConstraint("chunk_id", "topic_id"),
        ForeignKeyConstraint(["chunk_id", "course_id"], ["chunks.id", "chunks.course_id"],
                             name="chunk_topics_chunk_course_fkey", ondelete="CASCADE"),
        ForeignKeyConstraint(["topic_id", "course_id"], ["topics.id", "topics.course_id"],
                             name="chunk_topics_topic_course_fkey"),
    )

    id: Mapped[uuid.UUID] = pk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chunks.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"))
    course_id: Mapped[uuid.UUID | None]


# --- Practice & learner model --------------------------------------------


# --- Revision 0006 (M7, docs/quiz-api.md): quizzes, graded attempts, baseline statistics ---
QUESTION_PURPOSES = ("practice", "exam")
QUESTION_TYPES = ("mcq", "true_false", "short")
QUESTION_DIFFICULTIES = ("easy", "medium", "hard")
QUESTION_STATUSES = ("draft", "approved", "generated", "retired")
QUIZ_SESSION_STATUSES = ("generating", "ready", "failed")


class QuizSession(Base):
    """One quiz generation request of one student in one course (M7, revision 0006).

    generating -> ready | failed; ready and failed are terminal (database trigger). There is
    deliberately no foreign key to enrollments: practice history outlives course membership,
    which authorization checks on every request instead.
    """

    __tablename__ = "quiz_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('generating', 'ready', 'failed')", name="status"),
        CheckConstraint(
            "(status = 'generating' AND attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL"
            " AND failure_code IS NULL AND completed_at IS NULL AND protocol IS NULL AND service IS NULL)"
            " OR (status = 'ready' AND attempt_token IS NULL AND lease_expires_at IS NULL AND failure_code IS NULL"
            " AND completed_at IS NOT NULL AND protocol IS NOT NULL AND service IS NOT NULL)"
            " OR (status = 'failed' AND attempt_token IS NULL AND lease_expires_at IS NULL AND failure_code IS NOT NULL"
            " AND failure_code ~ '^[a-z][a-z0-9_]{0,39}$' AND completed_at IS NOT NULL)",
            name="state",
        ),
        CheckConstraint(
            "requested_count BETWEEN 1 AND 10 AND difficulty IN ('easy', 'medium', 'hard', 'mixed')"
            " AND scope = 'class_only' AND request_fingerprint ~ '^[0-9a-f]{64}$'"
            " AND cardinality(requested_topic_ids) BETWEEN 1 AND 5"
            " AND array_position(requested_topic_ids, NULL) IS NULL",
            name="request",
        ),
        UniqueConstraint("user_id", "idempotency_key"),
        UniqueConstraint("id", "user_id", "course_id"),
        Index("quiz_sessions_one_generating_idx", "user_id", unique=True, postgresql_where=text("status = 'generating'")),
        Index("quiz_sessions_lease_idx", "lease_expires_at", postgresql_where=text("status = 'generating'")),
        Index(None, "user_id", "course_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_topic_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    requested_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    difficulty: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(30), nullable=False)
    protocol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    service: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuizGenerationQuota(Base):
    """Generations reserved per user and UTC day (atomic upsert before any upstream request)."""

    __tablename__ = "quiz_generation_quotas"
    __table_args__ = (CheckConstraint("used >= 0", name="used"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    quota_day: Mapped[date] = mapped_column(Date, primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Question(Base):
    """A question. Revision 0006 added CHECKs, the generation columns and the composite keys.

    A generated question (generated_by_session_id set, status 'generated') carries complete
    metadata (questions_generated_check) and is immutable (database trigger); legacy and
    non-generated questions keep their old freedom.

    Revision 0008 (M9) added the professor question bank: `version IS NOT NULL` marks a bank
    row (always purpose 'exam', status draft | approved | retired, questions_bank_check), with
    the author, optimistic-concurrency version, last change and latest approved revision
    (composite key to exam_question_revisions; use_alter: the tables reference each other).
    Trigger questions_bank_guard keeps bank identity immutable, versions +1 per change, the
    documented transitions and no DELETE. bloom_level is optional for any row.
    """

    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint("purpose IN ('practice', 'exam')", name="purpose"),
        CheckConstraint("qtype IN ('mcq', 'true_false', 'short')", name="qtype"),
        CheckConstraint("difficulty IN ('easy', 'medium', 'hard')", name="difficulty"),
        CheckConstraint("status IN ('draft', 'approved', 'generated', 'retired')", name="status"),
        CheckConstraint(
            "(generated_by_session_id IS NULL AND generated_for_user_id IS NULL AND status <> 'generated'"
            " AND generation IS NULL)"
            " OR ((generated_by_session_id IS NOT NULL AND generated_for_user_id IS NOT NULL AND status = 'generated'"
            " AND purpose = 'practice' AND qtype IN ('mcq', 'true_false') AND topic_id IS NOT NULL"
            " AND hint IS NOT NULL AND explanation IS NOT NULL AND grading_method = 'choice_id/1'"
            " AND content_hash IS NOT NULL AND content_hash ~ '^[0-9a-f]{64}$'"
            " AND generation IS NOT NULL AND jsonb_typeof(generation) = 'object'"
            " AND answer_key IS NOT NULL AND jsonb_typeof(answer_key) = 'object'"
            " AND jsonb_typeof(answer_key -> 'choice_id') = 'string'"
            " AND (answer_key ->> 'choice_id') ~ '^[a-z0-9_-]{1,16}$'"
            " AND (answer_key - 'choice_id') = '{}'::jsonb"
            " AND choices IS NOT NULL AND jsonb_typeof(choices) = 'array' AND jsonb_array_length(choices) BETWEEN 2 AND 6"
            " AND choices @> jsonb_build_array(jsonb_build_object('id', answer_key ->> 'choice_id'))"
            " AND citations IS NOT NULL AND jsonb_typeof(citations) = 'array'"
            " AND jsonb_array_length(citations) BETWEEN 1 AND 4) IS TRUE)",
            name="generated",
        ),
        UniqueConstraint("id", "course_id"),
        UniqueConstraint("id", "course_id", "purpose"),
        UniqueConstraint(
            "id", "generated_by_session_id", "generated_for_user_id", "topic_id",
            name="questions_generated_assignment_key",
        ),  # fmt: skip
        ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="questions_topic_course_fkey"
        ),
        ForeignKeyConstraint(
            ["generated_by_session_id", "generated_for_user_id", "course_id"],
            ["quiz_sessions.id", "quiz_sessions.user_id", "quiz_sessions.course_id"],
            name="questions_generated_session_fkey",
        ),
        Index(None, "course_id", "purpose", "status"),
        # --- revision 0008 (M9) ---
        CheckConstraint(QUESTION_BLOOM_CHECK, name="bloom_level"),
        CheckConstraint(QUESTION_BANK_CHECK, name="bank"),
        ForeignKeyConstraint(
            ["id", "approved_revision_no"],
            ["exam_question_revisions.question_id", "exam_question_revisions.revision_no"],
            name="questions_approved_revision_fkey",
            use_alter=True,
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(20), default="practice")  # practice | exam
    qtype: Mapped[str] = mapped_column(String(20), default="mcq")  # mcq | true_false | short
    difficulty: Mapped[str] = mapped_column(String(20), default="medium")
    text: Mapped[str] = mapped_column(Text)
    choices: Mapped[dict] = mapped_column(JSONB, nullable=True)  # generated: [{"id", "text"}] in delivery order
    answer: Mapped[str] = mapped_column(Text, nullable=True)  # legacy free text; generated questions use answer_key
    explanation: Mapped[str] = mapped_column(Text, nullable=True)
    citations: Mapped[dict] = mapped_column(JSONB, nullable=True)  # generated: verified evidence snapshots
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | approved | generated | retired
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # --- added by revision 0006 (M7; NULL for every legacy row) ---
    hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_key: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {"choice_id": "..."}; never delivered
    grading_method: Mapped[str | None] = mapped_column(String(30), nullable=True)  # choice_id/1
    generated_by_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    generated_for_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    generation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # protocol, service, model, request_id
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # --- added by revision 0008 (M9 question bank; NULL for every legacy and M7 row) ---
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the M9 marker; +1 per change
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # historical
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bloom_level: Mapped[str | None] = mapped_column(String(20), nullable=True)


class QuizItem(Base):
    """A generated question assigned to its own session and student at one position (M7)."""

    __tablename__ = "quiz_items"
    __table_args__ = (
        CheckConstraint("purpose = 'practice'", name="purpose"),
        CheckConstraint("position BETWEEN 1 AND 10", name="position"),
        ForeignKeyConstraint(
            ["session_id", "user_id", "course_id"],
            ["quiz_sessions.id", "quiz_sessions.user_id", "quiz_sessions.course_id"],
            name="quiz_items_session_fkey",
        ),
        ForeignKeyConstraint(
            ["question_id", "course_id", "purpose"],
            ["questions.id", "questions.course_id", "questions.purpose"],
            name="quiz_items_question_fkey",
        ),
        ForeignKeyConstraint(
            ["question_id", "session_id", "user_id", "topic_id"],
            [
                "questions.id",
                "questions.generated_by_session_id",
                "questions.generated_for_user_id",
                "questions.topic_id",
            ],
            name="quiz_items_generated_question_fkey",
        ),
        ForeignKeyConstraint(["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="quiz_items_topic_fkey"),
        UniqueConstraint("session_id", "position"),
        UniqueConstraint("question_id"),
        UniqueConstraint(
            "id", "user_id", "question_id", "topic_id", "course_id", name="quiz_items_attempt_target_key"
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    question_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'practice'::character varying")
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Attempt(Base):
    """An answer. Revision 0006 added course_id (backfilled) and the M7 grading columns.

    An M7 attempt (quiz_item_id set) carries the complete grading snapshot, is unique per
    assignment, checked against the answer key on insert and append-only (database trigger).
    """

    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint(
            "(quiz_item_id IS NULL AND answer IS NULL AND grading_method IS NULL AND feedback IS NULL"
            " AND mastery_before IS NULL AND mastery_after IS NULL AND mastery_method IS NULL"
            " AND stats_attempts IS NULL AND stats_correct IS NULL AND submitted_at IS NULL)"
            " OR ((quiz_item_id IS NOT NULL AND topic_id IS NOT NULL AND answer IS NOT NULL"
            " AND grading_method = 'choice_id/1' AND feedback IS NOT NULL AND jsonb_typeof(feedback) = 'object'"
            " AND mastery_method = 'cumulative_accuracy/1' AND mastery_after IS NOT NULL"
            " AND mastery_after BETWEEN 0 AND 1"
            " AND (mastery_before IS NULL OR mastery_before BETWEEN 0 AND 1)"
            " AND stats_attempts IS NOT NULL AND stats_correct IS NOT NULL"
            " AND stats_attempts >= 1 AND stats_correct BETWEEN 0 AND stats_attempts"
            " AND submitted_at IS NOT NULL AND hint_count BETWEEN 0 AND 1"
            " AND (response_ms IS NULL OR response_ms BETWEEN 0 AND 3600000)) IS TRUE)",
            name="practice",
        ),
        ForeignKeyConstraint(
            ["question_id", "course_id"], ["questions.id", "questions.course_id"], name="attempts_question_course_fkey"
        ),
        ForeignKeyConstraint(["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="attempts_topic_course_fkey"),
        ForeignKeyConstraint(
            ["quiz_item_id", "user_id", "question_id", "topic_id", "course_id"],
            ["quiz_items.id", "quiz_items.user_id", "quiz_items.question_id", "quiz_items.topic_id", "quiz_items.course_id"],
            name="attempts_quiz_item_fkey",
        ),
        UniqueConstraint("quiz_item_id"),
        Index(None, "user_id", "course_id", "submitted_at"),
        Index(None, "course_id", "topic_id", "submitted_at"),
        # --- revision 0007 (M8) ---
        # Target of mastery_outbox's composite key: an event names its attempt's own sequence number.
        UniqueConstraint("id", "user_id", "course_id", "topic_id", "stats_attempts", name="attempts_mastery_event_key"),
        # The per-topic sequence of graded attempts never repeats (the trigger also forbids gaps).
        Index(
            "attempts_topic_sequence_idx", "user_id", "topic_id", "stats_attempts", unique=True,
            postgresql_where=text("quiz_item_id IS NOT NULL"),
        ),  # fmt: skip
        Index("attempts_graded_course_submitted_idx", "course_id", "submitted_at",
              postgresql_where=text("quiz_item_id IS NOT NULL")),  # fmt: skip
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"), nullable=True)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    hint_count: Mapped[int] = mapped_column(Integer, default=0)  # client telemetry, untrusted
    response_ms: Mapped[int] = mapped_column(Integer, nullable=True)  # client telemetry, untrusted
    attempted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # --- added by revision 0006 (M7) ---
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    quiz_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    answer: Mapped[str | None] = mapped_column(String(16), nullable=True)  # the choice id submitted
    grading_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    feedback: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # immutable grading snapshot
    mastery_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    mastery_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    mastery_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stats_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats_correct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TopicPracticeStat(Base):
    """Deterministic baseline counters of committed M7 attempts per (user, topic). Not AI mastery."""

    __tablename__ = "topic_practice_stats"
    __table_args__ = (
        CheckConstraint("attempts >= 0 AND correct >= 0 AND correct <= attempts", name="counters"),
        ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="topic_practice_stats_topic_fkey"
        ),
        Index(None, "course_id", "topic_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MasteryScore(Base):
    """One model prediction per (student, topic), written only by core-api from a validated AI-2 response (M8).

    Revision 0007 added course_id, the composite topic key and complete provenance. A legacy
    row (every provenance column NULL) is preserved but never shown as model output; an M8
    row carries the protocol, model, version, state epoch, pseudonym key id, the topic
    sequence it covers (through_seq == attempts_seen == attempts), the batch and the time it
    was received, and mastery in [0, 1] (docs/ai-mastery-integration.md).
    """

    __tablename__ = "mastery_scores"
    __table_args__ = (
        UniqueConstraint("user_id", "topic_id"),
        ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="mastery_scores_topic_course_fkey"
        ),
        CheckConstraint(SCORE_PROVENANCE_CHECK, name="provenance"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id"))
    mastery: Mapped[float] = mapped_column(Float, default=0.0)  # output of the Model 2 classifier
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    # --- added by revision 0007 (M8) ---
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    protocol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_epoch: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    pseudonym_key_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    through_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --- Revision 0007 (M8): mastery delivery, study telemetry, analytics releases -------------
#
# Lock order of every transaction that takes more than one of these rows (app/mastery/):
#   users -> courses -> enrollments -> mastery_model_state -> mastery_streams -> mastery_outbox
#   -> mastery_scores
# A graded attempt (M7) only INSERTs new outbox rows and inserts its stream row ON CONFLICT DO
# NOTHING; it never waits for an existing stream or outbox row lock, and the delivery transactions
# lock streams FOR NO KEY UPDATE, which does not block the outbox key's KEY SHARE check.

class MasteryStream(Base):
    """Delivery state of one student's mastery events in one course: at most one active batch."""

    __tablename__ = "mastery_streams"
    __table_args__ = (
        CheckConstraint(STREAM_STATE_CHECK, name="state"),
        CheckConstraint(STREAM_BATCH_CHECK, name="batch"),
        CheckConstraint(STREAM_LEASE_CHECK, name="lease"),
        CheckConstraint(STREAM_ERROR_CHECK, name="error"),
        CheckConstraint(STREAM_COUNTERS_CHECK, name="counters"),
        Index(None, "state", "next_attempt_at"),
        Index(None, "course_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'ready'::character varying"))
    # The active batch: immutable identity (also the request id) and membership (mastery_outbox.batch_id).
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    batch_epoch: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    batch_size: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    batch_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    batch_claims: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Renewable claim (fencing token) and lease of the attempt delivering the batch now.
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    claim_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    operator_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_operator_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_operator_actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MasteryOutbox(Base):
    """One mastery event per graded M7 attempt, created by a trigger in the attempt's own transaction."""

    __tablename__ = "mastery_outbox"
    __table_args__ = (
        CheckConstraint("topic_seq >= 1", name="topic_seq"),
        CheckConstraint("origin IN ('trigger', 'backfill')", name="origin"),
        CheckConstraint(OUTBOX_STATUS_CHECK, name="status"),
        CheckConstraint(OUTBOX_LIFECYCLE_CHECK, name="lifecycle"),
        ForeignKeyConstraint(
            ["event_id", "user_id", "course_id", "topic_id", "topic_seq"],
            ["attempts.id", "attempts.user_id", "attempts.course_id", "attempts.topic_id", "attempts.stats_attempts"],
            name="mastery_outbox_attempt_fkey",
        ),
        ForeignKeyConstraint(
            ["user_id", "course_id"],
            ["mastery_streams.user_id", "mastery_streams.course_id"],
            name="mastery_outbox_stream_fkey",
        ),
        ForeignKeyConstraint(["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="mastery_outbox_topic_fkey"),
        UniqueConstraint("user_id", "topic_id", "topic_seq"),
        Index("mastery_outbox_pending_idx", "user_id", "course_id", "topic_seq", "topic_id",
              postgresql_where=text("status = 'pending'")),  # fmt: skip
        Index("mastery_outbox_batch_idx", "batch_id", postgresql_where=text("batch_id IS NOT NULL")),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # the attempt id
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic_seq: Mapped[int] = mapped_column(Integer, nullable=False)  # attempts.stats_attempts
    origin: Mapped[str] = mapped_column(String(10), nullable=False)  # trigger | backfill
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'::character varying"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)  # applied | duplicate
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    batched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MasteryModelState(Base):
    """Outcome of the last capability probe against the pinned mastery model (single row)."""

    __tablename__ = "mastery_model_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("status IN ('unknown', 'compatible', 'incompatible')", name="status"),
        CheckConstraint(MODEL_STATE_CHECK, name="state"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'unknown'::character varying"))
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    protocol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    service: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state_epoch: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    pseudonym_key_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    probe_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StudyEvent(Base):
    """Client-reported study telemetry (allowlisted, bounded, untrusted; docs/study-events.md)."""

    __tablename__ = "study_events"
    __table_args__ = (
        CheckConstraint(STUDY_EVENT_TYPE_CHECK, name="event_type"),
        CheckConstraint(STUDY_EVENT_SHAPE_CHECK, name="shape"),
        CheckConstraint(STUDY_EVENT_TIME_CHECK, name="time"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint"),
        ForeignKeyConstraint(["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="study_events_topic_fkey"),
        ForeignKeyConstraint(
            ["document_id", "course_id"], ["documents.id", "documents.course_id"],
            name="study_events_document_fkey", ondelete="CASCADE",
        ),  # fmt: skip
        UniqueConstraint("user_id", "client_event_id"),
        Index(None, "user_id", "received_at"),
        Index(None, "user_id", "course_id", "occurred_at"),
        Index(None, "received_at"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    client_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    surface: Mapped[str | None] = mapped_column(String(20), nullable=True)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recommendation_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AnalyticsWeeklyRelease(Base):
    """One immutable, privacy-filtered analytics release per course and completed UTC ISO week."""

    __tablename__ = "analytics_weekly_releases"
    __table_args__ = (
        CheckConstraint(RELEASE_WEEK_CHECK, name="week"),
        CheckConstraint(RELEASE_PAYLOAD_CHECK, name="payload"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    policy: Mapped[str] = mapped_column(String(40), nullable=False)
    min_group_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# --- Revision 0008 (M9): professor question bank, exams, blueprint, audit, idempotency --------------
#
# Lock order of every M9 transaction that takes several rows (app/exams/):
#   users -> courses -> exams -> exam_blueprint_rows -> exam_items -> questions (ascending id)
#   -> exam_question_revisions -> exam_idempotency_keys -> exam_audit_events (insert)
# Bank-only operations take users -> courses -> questions -> ... and never lock an exam afterwards.


class ExamQuestionRevision(Base):
    """Immutable copy of a bank question's content at one approval (revision_no 1, 2, ... per question).

    Inserted only as an exact copy of the current draft working copy (trigger, which also locks the
    question FOR UPDATE and requires contiguous numbers); never updated or deleted. source_refs holds
    frozen display data {document_id, locator, title, locator_label} read at approval.
    """

    __tablename__ = "exam_question_revisions"
    __table_args__ = (
        CheckConstraint(REVISION_PURPOSE_CHECK, name="purpose"),
        CheckConstraint(REVISION_DOMAIN_CHECK, name="domain"),
        CheckConstraint(REVISION_CONTENT_CHECK, name="content"),
        ForeignKeyConstraint(
            ["question_id", "course_id", "purpose"],
            ["questions.id", "questions.course_id", "questions.purpose"],
            name="exam_question_revisions_question_fkey",
        ),
        ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="exam_question_revisions_topic_fkey"
        ),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'exam'::character varying"))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    qtype: Mapped[str] = mapped_column(String(20), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(20), nullable=False)
    bloom_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    choices: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    answer_key: Mapped[dict] = mapped_column(JSONB, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Exam(Base):
    """A professor's exam: draft (editable) -> finalized (immutable, exportable) -> archived; never deleted.

    Finalization freezes the course header, totals, content hash and size, the blueprint decision and
    the preview fingerprint it was accepted with; exams_guard verifies items, pinned revisions and the
    blueprint in the database at that transition.
    """

    __tablename__ = "exams"
    __table_args__ = (
        CheckConstraint(EXAM_STATUS_CHECK, name="status"),
        CheckConstraint(EXAM_FIELDS_CHECK, name="fields"),
        CheckConstraint(EXAM_FINGERPRINT_CHECK, name="fingerprint"),
        CheckConstraint(EXAM_STATE_CHECK, name="state"),
        UniqueConstraint("id", "course_id"),
        ForeignKeyConstraint(
            ["derived_from_exam_id", "course_id"], ["exams.id", "exams.course_id"], name="exams_derived_from_fkey"
        ),
        Index(None, "course_id", "status", "updated_at", "id"),
    )

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    finalized_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    course_code_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    course_name_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    item_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    total_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blueprint_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # none | fulfilled | deviation_accepted
    blueprint_deviation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    finalize_preview_fingerprint: Mapped[str | None] = mapped_column(String(68), nullable=True)
    derived_from_exam_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExamBlueprintRow(Base):
    """One blueprint requirement of an exam (criteria NULL = any); writable only while the exam is a draft.

    (exam_id, position) is unique DEFERRABLE INITIALLY DEFERRED so rows can be reordered in one
    transaction; (id, exam_id) is a separate non-deferrable key, the target of exam_items' row key.
    """

    __tablename__ = "exam_blueprint_rows"
    __table_args__ = (
        CheckConstraint(BLUEPRINT_VALUES_CHECK, name="values"),
        ForeignKeyConstraint(["exam_id", "course_id"], ["exams.id", "exams.course_id"], name="exam_blueprint_rows_exam_fkey"),
        ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="exam_blueprint_rows_topic_fkey"
        ),
        UniqueConstraint("id", "exam_id"),
        UniqueConstraint(
            "exam_id", "position", name="exam_blueprint_rows_exam_id_position_key", deferrable=True, initially="DEFERRED"
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    exam_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    qtype: Mapped[str | None] = mapped_column(String(20), nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bloom_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    question_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    points_each: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class ExamItem(Base):
    """A bank question placed in an exam at a position with points; pinned to a revision at finalization."""

    __tablename__ = "exam_items"
    __table_args__ = (
        CheckConstraint(EXAM_ITEM_PURPOSE_CHECK, name="purpose"),
        CheckConstraint(EXAM_ITEM_VALUES_CHECK, name="values"),
        ForeignKeyConstraint(["exam_id", "course_id"], ["exams.id", "exams.course_id"], name="exam_items_exam_fkey"),
        ForeignKeyConstraint(
            ["question_id", "course_id", "purpose"],
            ["questions.id", "questions.course_id", "questions.purpose"],
            name="exam_items_question_fkey",
        ),
        ForeignKeyConstraint(
            ["question_id", "question_revision_no"],
            ["exam_question_revisions.question_id", "exam_question_revisions.revision_no"],
            name="exam_items_revision_fkey",
        ),
        ForeignKeyConstraint(
            ["blueprint_row_id", "exam_id"],
            ["exam_blueprint_rows.id", "exam_blueprint_rows.exam_id"],
            name="exam_items_blueprint_row_fkey",
        ),
        UniqueConstraint("exam_id", "position", name="exam_items_exam_id_position_key", deferrable=True, initially="DEFERRED"),
        UniqueConstraint("exam_id", "question_id"),
        Index(None, "question_id"),
    )

    id: Mapped[uuid.UUID] = pk()
    exam_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    question_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'exam'::character varying"))
    question_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    points: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    blueprint_row_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExamAuditEvent(Base):
    """Append-only, course-scoped record of every M9 change and export: ids, hashes and counts only."""

    __tablename__ = "exam_audit_events"
    __table_args__ = (
        CheckConstraint(AUDIT_SHAPE_CHECK, name="shape"),
        Index("exam_audit_events_entity_idx", "course_id", "entity_kind", "entity_id", "created_at"),
        Index("exam_audit_events_replay_idx", "entity_id", "action", "entity_version"),
    )

    id: Mapped[uuid.UUID] = pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # no FK: history survives
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExamIdempotencyKey(Base):
    """The resource an M9 create (question, exam, duplicate) made for one user's Idempotency-Key."""

    __tablename__ = "exam_idempotency_keys"
    __table_args__ = (CheckConstraint(IDEMPOTENCY_CHECK, name="shape"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# --- Revision 0012 (WP-9): admin audit events -----------------------------------------------------------------


class AdminAuditEvent(Base):
    """Append-only record of every committed admin mutation (app/admin/mutations.py; docs/admin-api.md).

    Written in the same transaction as the change it records, never by an API client. actor_id is the
    authenticated administrator whose users row was locked and re-checked in that transaction; a trigger
    refuses a row whose actor is not an active admin, and refuses UPDATE, DELETE and TRUNCATE. entity_id and
    operation_id (the run or job the action created) have no foreign key, so history survives the entity.
    details holds ids, states and counts only.
    """

    __tablename__ = "admin_audit_events"
    __table_args__ = (
        CheckConstraint(ADMIN_AUDIT_SHAPE_CHECK, name="shape"),
        Index("admin_audit_events_entity_idx", "entity_kind", "entity_id", "created_at"),
        Index("admin_audit_events_actor_idx", "actor_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = pk()
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
