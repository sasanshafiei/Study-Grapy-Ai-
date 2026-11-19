"""Normalize `pg_dump --schema-only --no-owner --no-privileges` output for comparison.

Usage:  python scripts/normalize_schema_dump.py [DUMP_FILE]   (reads stdin without a file)

The flags --no-owner and --no-privileges already leave out ownership and ACL
statements (roles differ between databases and are not part of the schema we
migrate). This script then removes only what is not schema:

1. `--` comment lines (dump header, pg_dump/server versions, object headers);
2. the psql meta-commands `\\restrict <key>` / `\\unrestrict <key>`, whose key
   is random for every dump;
3. session settings: `SET ...;` and `SELECT pg_catalog.set_config(...)`;
4. every statement that mentions public.alembic_version (Alembic bookkeeping);
5. blank lines (statements are re-joined with one empty line between them).

Everything else (extensions, tables, columns, defaults, constraints, indexes,
sequences, views, functions, triggers, comments on objects, ...) is kept
verbatim, so any real schema difference shows up in a diff.

tests/fixtures/scaffold_schema.sql is this script's output for the scaffold
database created by init_db.py, captured read-only before M1.
"""

from __future__ import annotations

import sys


def normalize(dump: str) -> str:
    kept = [
        line
        for line in dump.splitlines()
        if not line.startswith("--")
        and not line.startswith("\\restrict ")
        and not line.startswith("\\unrestrict ")
        and not line.startswith("SET ")
        and not line.startswith("SELECT pg_catalog.set_config(")
    ]
    statements: list[str] = []
    current: list[str] = []
    for line in kept:
        if not line.strip():
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(current))
            current = []
    if current:
        statements.append("\n".join(current))
    statements = [s for s in statements if "public.alembic_version" not in s]
    return "\n\n".join(statements) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    dump = open(argv[1], encoding="utf-8").read() if len(argv) == 2 else sys.stdin.read()
    sys.stdout.write(normalize(dump))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
