"""Deprecated: the schema is now managed by Alembic migrations.

This script used to run Base.metadata.create_all(), which builds tables
without recording any migration revision. On a database that Alembic manages
that silently produces an untracked schema, so it no longer creates anything.
It is kept only so the old command fails with directions instead.

See docs/database-migrations.md.
"""

import sys

MESSAGE = """\
init_db.py is deprecated and no longer creates tables.

Create or update the schema with Alembic, inside the core-api container:

    docker compose exec core-api alembic upgrade head

If your database was created earlier by init_db.py, do not run `upgrade` on it
yet: follow "Adopting a database created by init_db.py" in
docs/database-migrations.md.
"""


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
