"""Database requirements shared by API readiness and worker preflight."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .migration_safety import EXPECTED_SCHEMA

REQUIRED_SCHEMA_REVISION = "0011"

# F05: only these ENVIRONMENT values may run the API and workers as a privileged database role (the
# development compose bootstrap user, the disposable test superuser). Every other value, unknown ones
# included, requires an unprivileged runtime identity (docs/database-security.md).
PRIVILEGED_ROLE_ENVIRONMENTS = frozenset({"development", "test"})

# What the connected role can do, directly or through any role it is a member of.
_RUNTIME_IDENTITY_PROBLEMS = text("""
    SELECT array_remove(ARRAY[
        CASE WHEN bool_or(r.rolsuper) THEN 'superuser' END,
        CASE WHEN bool_or(r.rolbypassrls) THEN 'bypassrls' END,
        CASE WHEN bool_or(r.rolcreaterole) THEN 'createrole' END,
        CASE WHEN bool_or(r.rolcreatedb) THEN 'createdb' END,
        CASE WHEN bool_or(r.rolreplication) THEN 'replication' END,
        CASE WHEN pg_catalog.has_database_privilege(pg_catalog.current_database(), 'CREATE')
             THEN 'database create' END,
        CASE WHEN pg_catalog.has_schema_privilege(:schema, 'CREATE') THEN 'schema create' END,
        CASE WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace = to_regnamespace(:schema)
                          AND pg_catalog.pg_has_role(c.relowner, 'MEMBER')) THEN 'table owner' END,
        -- predefined roles such as pg_read_server_files or pg_write_all_data confer power without attributes
        CASE WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_roles p WHERE starts_with(p.rolname, 'pg_')
                          AND pg_catalog.pg_has_role(p.oid, 'MEMBER')) THEN 'predefined role' END
    ], NULL)
    FROM pg_catalog.pg_roles r WHERE pg_catalog.pg_has_role(r.oid, 'MEMBER')
""")


class IncompatibleDatabase(RuntimeError):
    pass


def requires_unprivileged_role(environment: str) -> bool:
    return environment not in PRIVILEGED_ROLE_ENVIRONMENTS


def runtime_identity_problems(connection: Connection) -> list[str]:
    """Attributes and ownership that the application's own database role must not have."""
    return list(connection.execute(_RUNTIME_IDENTITY_PROBLEMS, {"schema": EXPECTED_SCHEMA}).scalar_one())


def check_database_compatibility(connection: Connection, *, unprivileged: bool = False) -> None:
    """Check the caller's live connection. Never stamp, migrate or open another engine.

    With `unprivileged`, also refuse a role that is (or can become) a superuser, owner or other
    privileged identity: outside development and test the API and workers use the runtime role only.
    """
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
    if unprivileged and (problems := runtime_identity_problems(connection)):
        raise IncompatibleDatabase(
            f"the database role is too privileged for the application ({', '.join(problems)}); "
            "connect with the runtime identity (docs/database-security.md)"
        )
