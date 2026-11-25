"""Development/demo seed command (WP-2). Run inside a core-api or worker container:

    python -m app.seed.admin demo --database NAME [--topics FILE|-] [--dry-run]

Refused unless ENVIRONMENT is explicitly set to `development` or `test` in the
process environment (the application's own default does not count), the
database target passes the same checks as the other operator commands
(DATABASE_URL, migration safety rules, --database must repeat its name), and
the schema is at the revision this code requires. What it writes, and why it
never modifies existing data, is described in app/seed/demo.py and
docs/demo-seed.md. Without --topics the packaged four-topic development sample
is imported.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping

from sqlalchemy.exc import OperationalError

from ..database_compatibility import IncompatibleDatabase, check_database_compatibility
from ..ingestion.admin import AdminError, guarded_session_factory
from ..migration_safety import UnsafeMigrationTarget
from ..topics import catalogue
from ..topics.admin import LOCK_NOT_AVAILABLE, _read
from . import demo

ALLOWED_ENVIRONMENTS = frozenset({"development", "test"})


def _require_environment(environ: Mapping[str, str]) -> None:
    value = environ.get("ENVIRONMENT")
    if value not in ALLOWED_ENVIRONMENTS:
        shown = "not set" if value is None else repr(value)
        raise AdminError(f"the demo seed runs only with ENVIRONMENT=development or test (ENVIRONMENT is {shown})")


def _topic_names(path: str | None) -> list[str]:
    try:
        return catalogue.parse_catalogue(demo.sample_topics() if path is None else _read(path))
    except catalogue.CatalogueError as exc:
        raise AdminError(f"invalid topic file: {exc}") from None


def cmd_demo(args, environ: Mapping[str, str]) -> int:
    _require_environment(environ)
    names = _topic_names(args.topics)
    factory, engine, target = guarded_session_factory(args.database, environ)
    try:
        with factory() as session:
            try:
                check_database_compatibility(session.connection())
            except IncompatibleDatabase as exc:
                raise AdminError(f"{target}: {exc}") from None
            session.rollback()  # the seed must start its own transaction (SET TRANSACTION, advisory lock)
            result = demo.seed_demo(session, names, dry_run=args.dry_run)
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
    except (demo.SeedConflict, catalogue.CatalogueError) as exc:
        raise AdminError(f"{target}: {exc}; nothing was written") from None
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
            raise AdminError(f"{target}: another seed run or transaction holds a demo row; nothing was written") from None
        raise
    finally:
        engine.dispose()
    _report(result, target, args.dry_run)
    return 0


def _report(result: demo.SeedResult, target: str, dry_run: bool) -> None:
    for line in result.lines:
        print(line)
    if result.topics is None:
        topics = f"{result.topics_planned} to import"
    else:
        created = f"{len(result.topics.created)} {'to create' if dry_run else 'created'}"
        topics = f"{created}, {len(result.topics.matched)} already present, {len(result.topics.kept)} kept"
    print(f"{target} demo seed: topics {topics}" + (" (dry run: nothing written)" if dry_run else ""))


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("demo", help="create the missing parts of the General Chemistry demo dataset")
    seed.add_argument("--database", required=True, help="the database name DATABASE_URL must point to")
    seed.add_argument("--topics", help="topic file (app.topics format) or - for stdin; default: packaged sample")
    seed.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = parser.parse_args(argv)
    try:
        return cmd_demo(args, environ)
    except (AdminError, UnsafeMigrationTarget) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
