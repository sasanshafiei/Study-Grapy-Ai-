"""Baseline schema: the 11 tables of the original scaffold.

Revision ID: 0001
Revises:
Create Date: 2026-11-19

This revision reproduces exactly the schema that the scaffold's init_db.py
(Base.metadata.create_all) built at commit 8fa94ea: the same tables, column
order, types, nullability, server defaults and constraint names (PostgreSQL's
default names, e.g. users_pkey, courses_owner_id_fkey, users_email_key). That
is what allows a database created by init_db.py to be adopted with
`alembic stamp 0001` instead of being rebuilt; tests/fixtures/
scaffold_schema.json records that schema and the tests compare against it.

Generated with `alembic revision --autogenerate` against an empty database
and then edited by hand: the pgvector extension step was added (autogenerate
does not manage extensions) and the comments were written.

This file is self-contained on purpose: it does not import app.models. The
models will keep changing; every later change is a new revision, never an
edit to this one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the baseline tables (and the pgvector extension if missing)."""
    # chunks.embedding needs the `vector` type, so the extension must exist
    # first. IF NOT EXISTS makes this a no-op where infra/postgres/init.sql or a
    # database administrator already installed it. Installing it requires a
    # superuser: pgvector is not marked as a trusted extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sources",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("licence", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("sources_pkey")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("users_pkey")),
        sa.UniqueConstraint("email", name=op.f("users_email_key")),
    )
    op.create_table(
        "courses",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("join_code", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name=op.f("courses_owner_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("courses_pkey")),
        sa.UniqueConstraint("join_code", name=op.f("courses_join_code_key")),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("licence", sa.String(length=100), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("documents_course_id_fkey")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("documents_source_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("documents_pkey")),
    )
    op.create_table(
        "enrollments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("enrollments_course_id_fkey")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("enrollments_user_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("enrollments_pkey")),
        sa.UniqueConstraint("user_id", "course_id", name=op.f("enrollments_user_id_course_id_key")),
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("topics_course_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("topics_pkey")),
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=True),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # 384 is fixed by this revision. A different embedding size means a new
        # revision and re-embedding every chunk; coordinate it with AI-1.
        sa.Column("embedding", VECTOR(dim=384), nullable=True),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("chunks_course_id_fkey")),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name=op.f("chunks_document_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("chunks_pkey")),
    )
    op.create_table(
        "mastery_scores",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=False),
        sa.Column("mastery", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_review_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], name=op.f("mastery_scores_topic_id_fkey")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("mastery_scores_user_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("mastery_scores_pkey")),
        sa.UniqueConstraint("user_id", "topic_id", name=op.f("mastery_scores_user_id_topic_id_key")),
    )
    op.create_table(
        "questions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=True),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("qtype", sa.String(length=20), nullable=False),
        sa.Column("difficulty", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("choices", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("questions_course_id_fkey")),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], name=op.f("questions_topic_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("questions_pkey")),
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("hint_count", sa.Integer(), nullable=False),
        sa.Column("response_ms", sa.Integer(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], name=op.f("attempts_question_id_fkey")),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], name=op.f("attempts_topic_id_fkey")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("attempts_user_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("attempts_pkey")),
    )
    op.create_table(
        "chunk_topics",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], name=op.f("chunk_topics_chunk_id_fkey")),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], name=op.f("chunk_topics_topic_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("chunk_topics_pkey")),
        sa.UniqueConstraint("chunk_id", "topic_id", name=op.f("chunk_topics_chunk_id_topic_id_key")),
    )


def downgrade() -> None:
    """Drop the baseline tables, dependants first. Deletes all their data.

    The `vector` extension is deliberately left installed: it is shared by the
    whole database, may be used by objects outside this schema, and was not
    necessarily created by this revision (init.sql or an administrator may
    have installed it).
    """
    op.drop_table("chunk_topics")
    op.drop_table("attempts")
    op.drop_table("questions")
    op.drop_table("mastery_scores")
    op.drop_table("chunks")
    op.drop_table("topics")
    op.drop_table("enrollments")
    op.drop_table("documents")
    op.drop_table("courses")
    op.drop_table("users")
    op.drop_table("sources")
