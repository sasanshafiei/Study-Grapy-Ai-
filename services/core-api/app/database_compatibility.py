"""Database requirements shared by API readiness and worker preflight."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .migration_safety import EXPECTED_SCHEMA

REQUIRED_SCHEMA_REVISION = "0011"


class IncompatibleDatabase(RuntimeError):
    pass


def check_database_compatibility(connection: Connection) -> None:
    """Check the caller's live connection. Never stamp, migrate or open another engine."""
    schema, vector_installed, versioned = connection.execute(text("""
        SELECT current_schema(),
               EXISTS (SELECT 1 FROM pg_catalog.pg_extension
                       WHERE extname = 'vector' AND extnamespace = to_regnamespace(:schema)),
               to_regclass('public.alembic_version') IS NOT NULL
    """), {"schema": EXPECTED_SCHEMA}).one()
    if schema != EXPECTED_SCHEMA:
        raise IncompatibleDatabase(f"the application requires the {EXPECTED_SCHEMA} schema")
    if not vector_installed:
        raise IncompatibleDatabase(f"pgvector is required in the {EXPECTED_SCHEMA} schema")
    revisions = []
    if versioned:
        revisions = connection.scalars(text("SELECT version_num FROM public.alembic_version LIMIT 2")).all()
    if revisions != [REQUIRED_SCHEMA_REVISION]:
        observed = ", ".join(map(str, revisions)) or "no revision"
        raise IncompatibleDatabase(
            f"database schema is at {observed}, expected {REQUIRED_SCHEMA_REVISION}; run the migration first"
        )
