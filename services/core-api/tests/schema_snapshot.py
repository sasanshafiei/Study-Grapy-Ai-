"""A comparable, credential-free description of the `public` schema.

Built from PostgreSQL catalog queries (not from SQLAlchemy metadata), so it is
an independent view of what really exists in a database. Used to:

- record the schema the scaffold's init_db.py produced
  (tests/fixtures/scaffold_schema.json), and
- check that migration 0001 reproduces it exactly
  (tests/test_migrations.py), or that an existing database matches it before
  it is adopted with `alembic stamp` (docs/database-migrations.md).

Coverage, `public` schema only:
- extensions and their schema (not their version, which depends on the image);
- tables;
- columns in physical order with types, NOT NULL and default expressions;
- constraints (pg_get_constraintdef, FK actions and match type);
- index definitions;
- the names and kinds of any other relations.

Not covered: other schemas, functions, triggers, rules, policies, view
definitions, sequence options, comments, collations, storage parameters,
identity/generated flags, ownership and privileges. This is enough for the
scaffold schema but is not a general equivalence proof. Adoption also
requires the normalized pg_dump comparison (scripts/normalize_schema_dump.py),
see docs/database-migrations.md.

Exclusions: only Alembic's bookkeeping table `alembic_version` and its
primary-key index `alembic_version_pkc`.

Command line (read-only; the session is opened with
default_transaction_read_only=on, so it cannot write even by mistake):

    python -m tests.schema_snapshot                       # print as JSON
    python -m tests.schema_snapshot --compare FILE.json   # exit 1 on any difference
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import URL, Connection, Engine

EXCLUDED_RELATIONS = ("alembic_version", "alembic_version_pkc")

_FK_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
_FK_MATCH = {"s": "SIMPLE", "f": "FULL", "p": "PARTIAL"}

_EXTENSIONS = text(
    """
    SELECT e.extname, n.nspname
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    ORDER BY e.extname
    """
)
_RELATIONS = text(
    """
    SELECT c.relname, c.relkind::text
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND NOT (c.relname = ANY(:excluded))
    ORDER BY c.relname
    """
)
_COLUMNS = text(
    """
    SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
           a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND a.attnum > 0 AND NOT a.attisdropped
      AND NOT (c.relname = ANY(:excluded))
    ORDER BY c.relname, a.attnum
    """
)
_CONSTRAINTS = text(
    """
    SELECT cl.relname, co.conname, co.contype::text, pg_get_constraintdef(co.oid),
           co.confupdtype::text, co.confdeltype::text, co.confmatchtype::text
    FROM pg_constraint co
    JOIN pg_class cl ON cl.oid = co.conrelid
    JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE n.nspname = 'public' AND NOT (cl.relname = ANY(:excluded))
    ORDER BY cl.relname, co.conname
    """
)
_INDEXES = text(
    """
    SELECT tablename, indexname, indexdef
    FROM pg_indexes
    WHERE schemaname = 'public' AND NOT (tablename = ANY(:excluded))
    ORDER BY tablename, indexname
    """
)


def read_only_engine(url: URL) -> Engine:
    """An engine whose sessions the server itself keeps read-only."""
    return create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def snapshot(connection: Connection) -> dict[str, Any]:
    """Describe the public schema reachable through `connection` (reads only)."""
    params = {"excluded": list(EXCLUDED_RELATIONS)}

    extensions = [f"{name} ({schema})" for name, schema in connection.execute(_EXTENSIONS)]

    relations = connection.execute(_RELATIONS, params).all()
    tables = [name for name, kind in relations if kind == "r"]
    other_relations = [f"{name} (relkind {kind})" for name, kind in relations if kind not in ("r", "i")]

    columns = []
    for table, column, type_, not_null, default in connection.execute(_COLUMNS, params):
        entry = f"{table}.{column}: {type_}"
        if not_null:
            entry += " NOT NULL"
        if default is not None:
            entry += f" DEFAULT {default}"
        columns.append(entry)

    constraints = []
    for table, name, kind, definition, on_update, on_delete, match in connection.execute(
        _CONSTRAINTS, params
    ):
        entry = f"{table}.{name}: {definition}"
        if kind == "f":
            entry += (
                f" [ON UPDATE {_FK_ACTIONS[on_update]}, ON DELETE {_FK_ACTIONS[on_delete]},"
                f" MATCH {_FK_MATCH[match]}]"
            )
        constraints.append(entry)

    indexes = [f"{table}.{name}: {definition}" for table, name, definition in connection.execute(_INDEXES, params)]

    return {
        "extensions": extensions,
        "tables": tables,
        "other_relations": other_relations,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
    }


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def diff(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """Unified diff of two snapshots; empty string when they are identical."""
    return "".join(
        difflib.unified_diff(
            dumps(expected).splitlines(keepends=True),
            dumps(actual).splitlines(keepends=True),
            fromfile="expected",
            tofile="actual",
        )
    )


def main(argv: list[str] | None = None) -> int:
    from app.migration_safety import UnsafeMigrationTarget, describe_target, resolve_migration_url

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare", type=Path, help="snapshot JSON file to compare against")
    args = parser.parse_args(argv)

    try:
        url = resolve_migration_url(os.environ)
    except UnsafeMigrationTarget as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    with read_only_engine(url).connect() as connection:
        actual = snapshot(connection)
        connection.rollback()

    if args.compare is None:
        sys.stdout.write(dumps(actual))
        return 0

    expected = json.loads(args.compare.read_text())
    difference = diff(expected, actual)
    if difference:
        print(f"Schema of {describe_target(url)} differs from {args.compare}:", file=sys.stderr)
        sys.stdout.write(difference)
        return 1
    print(f"Schema of {describe_target(url)} matches {args.compare}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
