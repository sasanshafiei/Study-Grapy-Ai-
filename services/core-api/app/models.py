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
    never lost between the API and the queue: the row itself is what a worker
    (M3) dispatches and claims. Allowed transitions, applied with conditional
    UPDATEs: queued -> running -> succeeded | failed | queued (retry while
    attempts remain); failed -> queued only by an explicit retry.
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
