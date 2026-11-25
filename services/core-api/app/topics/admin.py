"""Course topic operator commands (WP-1). Run inside a core-api or worker container:

    python -m app.topics.admin import --database NAME --course COURSE_ID --file topics.json [--dry-run]
    python -m app.topics.admin import --database NAME --course COURSE_ID --file - [--dry-run] < topics.json

The file format, identity and locking rules are in app/topics/catalogue.py and
docs/course-topics.md. An import only adds topics: existing topics are never
renamed or deleted, and re-importing a file changes nothing. The file is
validated completely before any database connection is opened.

The database target is taken and verified exactly as for app.ingestion.admin
(DATABASE_URL from the environment, migration safety rules, --database must
repeat its name). Output contains topic ids and names, never credentials.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping

from sqlalchemy.exc import OperationalError

from ..ingestion.admin import AdminError, guarded_session_factory
from ..migration_safety import UnsafeMigrationTarget
from . import catalogue

LOCK_NOT_AVAILABLE = "55P03"


def _uuid(value: str, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AdminError(f"{name} is not a UUID") from None


def _read(path: str) -> bytes:
    limit = catalogue.MAX_FILE_BYTES + 1  # one byte more, so an oversized file is refused, not truncated
    try:
        if path == "-":
            return sys.stdin.buffer.read(limit)
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError as exc:
        raise AdminError(f"cannot read --file: {exc.strerror or type(exc).__name__}") from None


def cmd_import(args, environ: Mapping[str, str]) -> int:
    course_id = _uuid(args.course, "--course")
    try:
        names = catalogue.parse_catalogue(_read(args.file))
    except catalogue.CatalogueError as exc:
        raise AdminError(f"invalid topic file: {exc}") from None
    factory, engine, target = guarded_session_factory(args.database, environ)
    try:
        with factory() as session:
            result = catalogue.import_topics(session, course_id, names, dry_run=args.dry_run)
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
    except catalogue.CatalogueError as exc:
        raise AdminError(f"course {course_id}: {exc}") from None
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
            raise AdminError(f"course {course_id} is locked by another topic import; nothing was written") from None
        raise
    finally:
        engine.dispose()
    _report(result, f"{target} course {course_id}", args.dry_run)
    return 0


def _report(result: catalogue.ImportResult, subject: str, dry_run: bool) -> None:
    for topic_id, name in result.created:
        print(f"{'would create' if dry_run else 'created'}\t{topic_id or '-'}\t{name}")
    for name, topic in result.matched:
        note = "" if topic.name == name else f"\t(stored as {topic.name!r}; not renamed)"
        print(f"exists\t{topic.topic_id}\t{name}{note}")
    for topic in result.kept:
        print(f"kept\t{topic.topic_id}\t{topic.name}\t(not in the file; never deleted)")
    summary = (
        f"{subject}: {len(result.created)} {'to create' if dry_run else 'created'}, "
        f"{len(result.matched)} already present, {len(result.kept)} kept"
    )
    print(summary + (" (dry run: nothing written)" if dry_run else ""))


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.topics.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="add a course's topics from a JSON file (never renames or deletes)")
    imp.add_argument("--database", required=True, help="the database name DATABASE_URL must point to")
    imp.add_argument("--course", required=True, help="course id")
    imp.add_argument("--file", required=True, help='JSON file {"topics": [{"name": ...}]}, or - for stdin')
    imp.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = parser.parse_args(argv)
    try:
        return cmd_import(args, environ)
    except (AdminError, UnsafeMigrationTarget) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
