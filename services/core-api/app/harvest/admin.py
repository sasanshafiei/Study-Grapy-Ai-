"""Harvesting operator commands (M6). Run inside a core-api or worker container:

    python -m app.harvest.admin init-source    --database NAME --provider wikipedia --language en
    python -m app.harvest.admin init-source    --database NAME --provider openstax --book chemistry-2e
    python -m app.harvest.admin enable-source  --database NAME --source SOURCE_ID
    python -m app.harvest.admin disable-source --database NAME --source SOURCE_ID
    python -m app.harvest.admin course-opt-in  --database NAME --course COURSE_ID --source SOURCE_ID [--max-documents N]
    python -m app.harvest.admin course-opt-out --database NAME --course COURSE_ID
    python -m app.harvest.admin propose-queries --database NAME --course COURSE_ID
    python -m app.harvest.admin approve-query  --database NAME --course COURSE_ID --topic TOPIC_ID --text "EXACT TEXT"
    python -m app.harvest.admin revoke-query   --database NAME --query QUERY_ID
    python -m app.harvest.admin trigger        --database NAME --course COURSE_ID --source SOURCE_ID
    python -m app.harvest.admin status         --database NAME --course COURSE_ID

The database target is taken and verified exactly as for app.ingestion.admin
(DATABASE_URL from the environment, migration safety rules, --database must
repeat its name). Nothing here contacts a provider. Sources are created
disabled; enabling a source, opting a course in and approving each query are
separate explicit steps; HARVEST_ENABLED must also be true before anything is
scheduled, dispatched or claimed (docs/external-harvesting.md).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping

from sqlalchemy import text

from ..config import Settings
from ..ingestion.admin import AdminError, guarded_session_factory
from ..migration_safety import UnsafeMigrationTarget
from . import openstax, queries, runs, wikipedia


def _uuid(value: str, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AdminError(f"{name} is not a UUID") from None


def _factory(args, environ):
    factory, engine, target = guarded_session_factory(args.database, environ)
    return factory, engine


def _source_values(args, settings: Settings) -> dict:
    """The row an operator creates: provider identity, licence and request budget (validated here)."""
    if args.provider == wikipedia.PROVIDER:
        if args.book is not None:
            raise AdminError("--book applies to the openstax provider only")
        if args.language not in wikipedia.LANGUAGES:
            raise AdminError(f"language must be one of {', '.join(sorted(wikipedia.LANGUAGES))}")
        return {"provider": wikipedia.PROVIDER, "l": args.language, "book": None,
                "domain": wikipedia.endpoint(args.language).host, "licence": wikipedia.LICENCE,
                "licence_url": wikipedia.LICENCE_URL, "w": settings.harvest_budget_window_seconds,
                "m": settings.harvest_budget_max_requests, "i": settings.harvest_budget_min_interval_ms}  # fmt: skip
    if args.provider == openstax.PROVIDER:
        if args.book not in openstax.BOOKS:
            raise AdminError(f"--book must be one of {', '.join(sorted(openstax.BOOKS))} (the allowlisted books)")
        book = openstax.BOOKS[args.book]
        if args.language not in (None, book.language):
            raise AdminError(f"{book.slug} is published in '{book.language}' only")
        return {"provider": openstax.PROVIDER, "l": book.language, "book": book.slug, "domain": openstax.API_HOST,
                "licence": openstax.LICENCE, "licence_url": openstax.LICENCE_URL,
                "w": openstax.BUDGET_WINDOW_SECONDS, "m": openstax.BUDGET_MAX_REQUESTS,
                "i": openstax.BUDGET_MIN_INTERVAL_MS}  # fmt: skip
    raise AdminError("provider must be wikipedia or openstax (OpenAlex is unavailable)")


def cmd_init_source(args, environ, settings: Settings) -> int:
    values = _source_values(args, settings)
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            existing = session.execute(
                text("SELECT id, provider_book FROM sources WHERE provider = :provider AND language = :l"), values
            ).one_or_none()
            if existing is not None:
                if existing.provider_book != values["book"]:
                    raise AdminError(f"source {existing.id} already harvests another book ({existing.provider_book})")
                print(f"source exists: {existing.id} (unchanged)")
                return 0
            source_id = session.execute(
                text(
                    "INSERT INTO sources (kind, domain, licence, enabled, provider, language, licence_url, provider_book) "
                    "VALUES (:provider, :domain, :licence, false, :provider, :l, :licence_url, :book) RETURNING id"
                ),
                values,
            ).scalar_one()
            session.execute(
                text(
                    "INSERT INTO harvest_provider_budgets (source_id, window_seconds, max_requests, min_interval_ms) "
                    "VALUES (:s, :w, :m, :i)"
                ),
                {"s": source_id, "w": values["w"], "m": values["m"], "i": values["i"]},
            )
            session.commit()
    finally:
        engine.dispose()
    print(f"source created (disabled): {source_id}")
    return 0


def cmd_set_source(args, environ, settings: Settings, enabled: bool) -> int:
    source_id = _uuid(args.source, "--source")
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            row = session.execute(
                text("SELECT provider FROM sources WHERE id = :s FOR NO KEY UPDATE"), {"s": source_id}
            ).one_or_none()
            if row is None or row.provider is None:
                raise AdminError("no provider-managed source with this id")
            session.execute(text("UPDATE sources SET enabled = :e WHERE id = :s"), {"s": source_id, "e": enabled})
            cancelled = 0 if enabled else runs.cancel_active(session, source_id=source_id, code="source_disabled")
            session.commit()
    finally:
        engine.dispose()
    print(f"source {source_id} {'enabled' if enabled else 'disabled'}" + ("" if enabled else f"; {cancelled} runs cancelled"))
    return 0


def cmd_opt_in(args, environ, settings: Settings) -> int:
    course_id, source_id = _uuid(args.course, "--course"), _uuid(args.source, "--source")
    quota = args.max_documents or settings.harvest_default_max_active_documents
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            if session.execute(text("SELECT 1 FROM courses WHERE id = :c"), {"c": course_id}).first() is None:
                raise AdminError("no such course")
            if session.execute(
                text("SELECT 1 FROM sources WHERE id = :s AND provider IS NOT NULL"), {"s": source_id}
            ).first() is None:
                raise AdminError("no provider-managed source with this id")
            session.execute(
                text(
                    "INSERT INTO harvest_course_settings (course_id, enabled, max_active_documents) VALUES (:c, true, :q) "
                    "ON CONFLICT (course_id) DO UPDATE SET enabled = true, max_active_documents = :q, updated_at = now()"
                ),
                {"c": course_id, "q": quota},
            )
            session.execute(
                text(
                    "INSERT INTO harvest_targets (course_id, source_id) VALUES (:c, :s) "
                    "ON CONFLICT (course_id, source_id) DO NOTHING"
                ),
                {"c": course_id, "s": source_id},
            )
            session.commit()
    finally:
        engine.dispose()
    print(f"course {course_id} opted in for source {source_id} (max {quota} active documents)")
    return 0


def cmd_opt_out(args, environ, settings: Settings) -> int:
    course_id = _uuid(args.course, "--course")
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            found = session.execute(
                text("SELECT 1 FROM harvest_course_settings WHERE course_id = :c FOR NO KEY UPDATE"), {"c": course_id}
            ).first()
            if found is None:
                raise AdminError("the course was never opted in")
            session.execute(
                text("UPDATE harvest_course_settings SET enabled = false, updated_at = now() WHERE course_id = :c"),
                {"c": course_id},
            )
            cancelled = runs.cancel_active(session, course_id=course_id, code="course_opted_out")
            session.commit()
    finally:
        engine.dispose()
    print(f"course {course_id} opted out; {cancelled} runs cancelled (existing documents are kept)")
    return 0


def cmd_propose(args, environ, settings: Settings) -> int:
    course_id = _uuid(args.course, "--course")
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            proposals = queries.propose(session, course_id)
            session.rollback()
    except queries.QueryRefused as exc:
        raise AdminError(str(exc)) from None
    finally:
        engine.dispose()
    for item in proposals:
        verdict = "ok" if item.refused is None else f"refused: {item.refused}"
        print(f"{item.topic_id}\t{item.query_text!r}\t{verdict}")
    print(f"{len(proposals)} topics; approve each text exactly with approve-query (nothing is approved automatically)")
    return 0


def cmd_approve(args, environ, settings: Settings) -> int:
    course_id, topic_id = _uuid(args.course, "--course"), _uuid(args.topic, "--topic")
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            query_id = queries.approve(session, course_id, topic_id, args.text, settings.harvest_max_queries_per_course)
            session.commit()
    except queries.QueryRefused as exc:
        raise AdminError(f"approval refused: {exc}") from None
    finally:
        engine.dispose()
    print(f"query approved: {query_id}")
    return 0


def cmd_revoke(args, environ, settings: Settings) -> int:
    query_id = _uuid(args.query, "--query")
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            revoked = queries.revoke(session, query_id)
            session.commit()
    finally:
        engine.dispose()
    print("query revoked" if revoked else "no approved query with this id")
    return 0 if revoked else 1


def cmd_trigger(args, environ, settings: Settings) -> int:
    course_id, source_id = _uuid(args.course, "--course"), _uuid(args.source, "--source")
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            run_id = runs.create_run(session, settings, course_id, source_id, manual=True)
            session.commit()
    finally:
        engine.dispose()
    if run_id is None:
        raise AdminError("no run created: the course is not opted in, the source is disabled, or a run is active")
    note = "" if settings.harvest_enabled else " (HARVEST_ENABLED is false: it stays queued)"
    print(f"run queued: {run_id}{note}")
    return 0


def cmd_status(args, environ, settings: Settings) -> int:
    course_id = _uuid(args.course, "--course")
    factory, engine = _factory(args, environ)
    try:
        with factory() as session:
            gate = session.execute(
                text("SELECT enabled, max_active_documents, dirty_generation FROM harvest_course_settings WHERE course_id = :c"),
                {"c": course_id},
            ).one_or_none()
            targets = session.execute(
                text(
                    "SELECT t.source_id, s.provider, s.language, s.enabled, t.harvested_generation, t.next_refresh_at, "
                    "t.not_before FROM harvest_targets t JOIN sources s ON s.id = t.source_id WHERE t.course_id = :c"
                ),
                {"c": course_id},
            ).all()
            run_rows = session.execute(
                text(
                    "SELECT id, status, trigger, claim_count, failure_count, error_code FROM harvest_runs "
                    "WHERE course_id = :c ORDER BY created_at DESC LIMIT 5"
                ),
                {"c": course_id},
            ).all()
            states = session.execute(
                text("SELECT state, count(*) FROM external_resources WHERE course_id = :c GROUP BY state ORDER BY state"),
                {"c": course_id},
            ).all()
            session.rollback()
    finally:
        engine.dispose()
    print(f"HARVEST_ENABLED={settings.harvest_enabled}")
    print("course: not configured" if gate is None else
          f"course: enabled={gate.enabled} quota={gate.max_active_documents} dirty_generation={gate.dirty_generation}")  # fmt: skip
    for t in targets:
        print(f"target: source={t.source_id} {t.provider}/{t.language} enabled={t.enabled} "
              f"harvested_generation={t.harvested_generation} next_refresh={t.next_refresh_at} not_before={t.not_before}")  # fmt: skip
    for r in run_rows:
        print(f"run: {r.id} {r.status} trigger={r.trigger} claims={r.claim_count} failures={r.failure_count} code={r.error_code}")
    print("resources: " + (", ".join(f"{s}={n}" for s, n in states) or "none"))
    return 0


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.harvest.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, *options: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--database", required=True, help="the database name DATABASE_URL must point to")
        for option in options:
            p.add_argument(f"--{option}", required=option not in ("max-documents",))
        return p

    init = sub.add_parser("init-source", help="create a provider source (disabled) and its request budget")
    init.add_argument("--database", required=True, help="the database name DATABASE_URL must point to")
    init.add_argument("--provider", required=True, help="wikipedia or openstax")
    init.add_argument("--language", help="wikipedia: required; openstax: the book's language (optional)")
    init.add_argument("--book", help="openstax: the allowlisted book slug, e.g. chemistry-2e")
    command("enable-source", "activate a provider source", "source")
    command("disable-source", "deactivate a source; cancels its runs, hides its documents", "source")
    opt_in = sub.add_parser("course-opt-in", help="opt a course in for a provider source")
    opt_in.add_argument("--database", required=True)
    opt_in.add_argument("--course", required=True)
    opt_in.add_argument("--source", required=True)
    opt_in.add_argument("--max-documents", type=int, choices=range(1, 501), metavar="1..500")
    command("course-opt-out", "stop harvesting for a course; cancels its runs", "course")
    command("propose-queries", "show the query derived from each course topic (read only)", "course")
    command("approve-query", "approve one exact query text for a topic", "course", "topic", "text")
    command("revoke-query", "revoke an approved query", "query")
    command("trigger", "queue a manual run for a course x source", "course", "source")
    command("status", "show a course's harvesting state", "course")
    args = parser.parse_args(argv)
    settings = Settings()
    commands = {
        "init-source": cmd_init_source,
        "enable-source": lambda a, e, s: cmd_set_source(a, e, s, True),
        "disable-source": lambda a, e, s: cmd_set_source(a, e, s, False),
        "course-opt-in": cmd_opt_in,
        "course-opt-out": cmd_opt_out,
        "propose-queries": cmd_propose,
        "approve-query": cmd_approve,
        "revoke-query": cmd_revoke,
        "trigger": cmd_trigger,
        "status": cmd_status,
    }
    try:
        return commands[args.command](args, environ, settings)
    except (AdminError, UnsafeMigrationTarget) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
