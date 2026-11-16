"""Dev-only: create all tables from the SQLAlchemy models.

Run once after the stack is up:
    docker compose exec core-api python init_db.py

This is a shortcut for getting started. Before anyone stores data they care
about, replace it with Alembic migrations (see README) so schema changes are
versioned and reversible.
"""

from app import models  # noqa: F401  (registers the tables on Base)
from app.db import Base, engine


def main():
    Base.metadata.create_all(engine)
    print("Tables created:", ", ".join(sorted(Base.metadata.tables)))


if __name__ == "__main__":
    main()
