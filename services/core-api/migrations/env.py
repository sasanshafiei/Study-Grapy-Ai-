"""Alembic environment for core-api.

Online commands (upgrade, downgrade, stamp, current, check, revision
--autogenerate) connect to the database named by the DATABASE_URL environment
variable, and only after the checks in app.migration_safety pass: the target
is validated before connecting, and the identity of the database, role and
schema (plus the COMMENT required by MIGRATIONS_REQUIRE_DATABASE_COMMENT, if
set) is verified on the migration's own connection before any DDL runs.
Offline mode (`--sql`) just renders PostgreSQL DDL and needs no URL at all.
"""

import logging
import os
from logging.config import fileConfig

from alembic import context
from alembic.util import CommandError
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import create_engine, pool

from app import models  # noqa: F401  (importing it registers every table on Base.metadata)
from app.db import Base
from app.migration_safety import (
    UnsafeMigrationTarget,
    describe_target,
    required_database_comment,
    resolve_migration_url,
    verify_connection,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
log = logging.getLogger("alembic.env")

target_metadata = Base.metadata

# Used by `alembic check` and `alembic revision --autogenerate`.
# tests/test_migrations.py passes the same options to compare_metadata().
COMPARE_OPTIONS = {"compare_type": True, "compare_server_default": True}


def render_item(type_, obj, autogen_context):
    """Render pgvector columns as VECTOR(dim=N), adding the import they need."""
    if type_ == "type" and isinstance(obj, VECTOR):
        autogen_context.imports.add("from pgvector.sqlalchemy import VECTOR")
        return f"VECTOR(dim={obj.dim})"
    return False


def run_migrations_offline() -> None:
    """Render the migration SQL for PostgreSQL without connecting anywhere."""
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        **COMPARE_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the checked DATABASE_URL target."""
    try:
        # The returned URL carries an explicit port; it is what the driver gets.
        url = resolve_migration_url(os.environ)
        required_comment = required_database_comment(os.environ)
    except UnsafeMigrationTarget as exc:
        raise CommandError(str(exc)) from None

    log.info("Migration target: %s", describe_target(url))
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        # Verified on this same connection, which then runs the migration.
        try:
            server_version = verify_connection(connection, url, required_comment)
        except UnsafeMigrationTarget as exc:
            raise CommandError(f"{exc} Stopped before any change.") from None
        finally:
            # End the implicit transaction opened by the identity query, so the
            # migration below runs (and commits) in a transaction of its own.
            connection.rollback()
        log.info(
            "Connected to database %r (PostgreSQL %s); identity verified%s",
            url.database,
            server_version,
            ", required database comment present" if required_comment else "",
        )

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_item=render_item,
            **COMPARE_OPTIONS,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
