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
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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


class Document(Base):
    __tablename__ = "documents"

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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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
