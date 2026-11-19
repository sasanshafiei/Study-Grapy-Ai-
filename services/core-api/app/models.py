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
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))  # the professor
    name: Mapped[str] = mapped_column(String(150))
    join_code: Mapped[str] = mapped_column(String(12), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("user_id", "course_id"),)

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
        CheckConstraint(
            "CASE WHEN jsonb_typeof(locator) = 'object' AND jsonb_typeof(locator -> 'page') = 'number'"
            " AND (locator - 'page') = '{}'::jsonb"
            " THEN (locator ->> 'page')::numeric BETWEEN 1 AND 2147483647"
            " AND (locator ->> 'page')::numeric = trunc((locator ->> 'page')::numeric)"
            " ELSE false END",
            name="locator",
        ),
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
