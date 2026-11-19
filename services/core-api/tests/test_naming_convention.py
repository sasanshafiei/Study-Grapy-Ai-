"""The constraint/index naming convention on Base.metadata (app/db.py).

These tests use scratch tables, so they stay valid as the models grow.
Whether the names the ORM emits match the migrated database is checked
against the current head in tests/test_migrations.py; the names of the
historical baseline are pinned by tests/fixtures/scaffold_schema.json.
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db import NAMING_CONVENTION, Base

PG = postgresql.dialect()


def _ddl(element) -> str:
    return str(element.compile(dialect=PG))


def _scratch_metadata() -> MetaData:
    return MetaData(naming_convention=NAMING_CONVENTION)


def test_base_metadata_uses_the_project_convention():
    assert Base.metadata.naming_convention == NAMING_CONVENTION


def test_pk_fk_and_unique_names_match_postgresql_defaults():
    """The names PostgreSQL itself gives unnamed constraints (as init_db.py relied on)."""
    metadata = _scratch_metadata()
    Table("owners", metadata, Column("id", Integer, primary_key=True))
    pets = Table(
        "pets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner_id", Integer, ForeignKey("owners.id")),
        Column("tag", String(10), unique=True),
        Column("name", String(20)),
        UniqueConstraint("owner_id", "name"),
    )
    ddl = _ddl(CreateTable(pets))
    assert "CONSTRAINT pets_pkey PRIMARY KEY (id)" in ddl
    assert "CONSTRAINT pets_owner_id_fkey FOREIGN KEY(owner_id)" in ddl
    assert "CONSTRAINT pets_tag_key UNIQUE (tag)" in ddl
    assert "CONSTRAINT pets_owner_id_name_key UNIQUE (owner_id, name)" in ddl


def test_project_convention_for_indexes_and_named_checks():
    metadata = _scratch_metadata()
    parent = Table("parents", metadata, Column("id", Integer, primary_key=True))
    widgets = Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey(parent.c.id)),
        Column("code", String(5)),
        Column("qty", Integer),
        CheckConstraint("qty >= 0", name="qty_non_negative"),
    )
    single = Index(None, widgets.c.code)
    multi = Index(None, widgets.c.parent_id, widgets.c.code)

    assert "CONSTRAINT widgets_qty_non_negative_check CHECK (qty >= 0)" in _ddl(CreateTable(widgets))
    assert _ddl(CreateIndex(single)).startswith("CREATE INDEX widgets_code_idx ON widgets")
    assert _ddl(CreateIndex(multi)).startswith("CREATE INDEX widgets_parent_id_code_idx ON widgets")


def test_unnamed_check_constraint_is_rejected():
    """The ck pattern uses %(constraint_name)s, so a CHECK must be named explicitly.

    SQLAlchemy fails as soon as the table is defined, long before any DDL runs.
    """
    with pytest.raises(InvalidRequestError, match="constraint_name"):
        Table(
            "gadgets",
            _scratch_metadata(),
            Column("id", Integer, primary_key=True),
            Column("qty", Integer),
            CheckConstraint("qty >= 0"),
        )
