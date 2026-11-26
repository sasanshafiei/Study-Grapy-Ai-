"""Read-only checks that infra/dev/smoke.sh runs inside the running containers (WP-13).

    python -m scripts.dev_smoke api [EXPECTED_USER]      # in core-api
    python -m scripts.dev_smoke worker ROLE SERVICE      # in a worker; ROLE as in app.ingestion.runtime

`api` uses the application's own database connection (DATABASE_URL, the runtime login) in one READ ONLY
transaction with a statement timeout, each check in its own savepoint: the identity, the schema revision,
the grants of the StudyGraph groups (services/core-api/scripts/db_privileges.sql), the logins in the AI
groups, other superuser sessions and the upload store. `worker` runs the worker's own start-up checks
(app.ingestion.runtime.preflight_for_role, which change nothing) and, for the processing role, whose
preflight only logs a missing upload-store binding, verifies the store.

Neither connects as anything else or changes anything. Output: one `STATUS|check|detail` line per result
(PASS, FAIL, SKIP, NOTE), never a URL, password, token or path. The evaluation functions take plain rows,
and the module imports the application and SQLAlchemy only when a command runs.
"""

from __future__ import annotations

import logging
import os
import sys

GROUPS = ("studygraph_ce_runtime", "studygraph_ai_indexer", "studygraph_ai_retriever")
RUNTIME_GROUP = GROUPS[0]
AI_GROUPS = GROUPS[1:]
# The one routine each group may execute (db_privileges.sql, section 5).
ROUTINE_OF = {
    "studygraph_ce_runtime": "verify",
    "studygraph_ai_indexer": "commit",
    "studygraph_ai_retriever": "retrieve",
}
# Sub-switches that take effect only while their parent switch is on (app/config.py).
PARENT_FLAG = {
    "quiz_generation_enabled": "quiz_enabled",
    "quiz_attempts_enabled": "quiz_enabled",
    "exam_export_enabled": "exams_enabled",
    "exam_generation_enabled": "exams_enabled",
}

# Per StudyGraph group, and per login that is a member of an AI group: the routines it can execute, how
# many public relations it holds any privilege on, which it cannot read, and how many there are.
PRIVILEGES_SQL = """
WITH subject(name, grp) AS (
    SELECT rolname, rolname FROM pg_catalog.pg_roles
    WHERE rolname IN ('studygraph_ce_runtime', 'studygraph_ai_indexer', 'studygraph_ai_retriever')
    UNION
    SELECT m.rolname, g.rolname
    FROM pg_catalog.pg_auth_members am
    JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
    JOIN pg_catalog.pg_roles m ON m.oid = am.member
    WHERE g.rolname IN ('studygraph_ai_indexer', 'studygraph_ai_retriever') AND m.rolcanlogin
), routine(key, sig) AS (
    VALUES ('commit', 'public.commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb)'),
           ('retrieve', 'public.retrieve_index_chunks(uuid,text[])'),
           ('verify', 'public.verified_index_generation(uuid,uuid,text,text,integer,integer)')
), relation AS (
    SELECT oid, relname FROM pg_catalog.pg_class
    WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p', 'v', 'm')
)
SELECT s.name, s.grp,
       (SELECT coalesce(string_agg(r.key, ',' ORDER BY r.key), '') FROM routine r
        WHERE pg_catalog.has_function_privilege(s.name, r.sig, 'EXECUTE')) AS executes,
       (SELECT count(*) FROM relation t
        WHERE pg_catalog.has_table_privilege(s.name, t.oid,
              'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')) AS any_table,
       (SELECT coalesce(string_agg(t.relname, ', ' ORDER BY t.relname), '') FROM relation t
        WHERE NOT pg_catalog.has_table_privilege(s.name, t.oid, 'SELECT')) AS unreadable,
       (SELECT count(*) FROM relation) AS relations
FROM subject s
ORDER BY s.grp, s.name
"""

# Logins in an AI group: role attributes or predefined roles, and membership of another StudyGraph group.
LOGINS_SQL = """
SELECT m.rolname, g.rolname,
       m.rolsuper OR m.rolcreatedb OR m.rolcreaterole OR m.rolreplication OR m.rolbypassrls
           OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles p WHERE starts_with(p.rolname, 'pg_')
                      AND pg_catalog.pg_has_role(m.oid, p.oid, 'MEMBER')) AS privileged,
       (SELECT coalesce(string_agg(o.rolname, ', ' ORDER BY o.rolname), '') FROM pg_catalog.pg_roles o
        WHERE o.rolname IN ('studygraph_ce_runtime', 'studygraph_ai_indexer', 'studygraph_ai_retriever')
          AND o.oid <> g.oid AND pg_catalog.pg_has_role(m.oid, o.oid, 'MEMBER')) AS other_groups
FROM pg_catalog.pg_auth_members am
JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
JOIN pg_catalog.pg_roles m ON m.oid = am.member
WHERE g.rolname IN ('studygraph_ai_indexer', 'studygraph_ai_retriever') AND m.rolcanlogin
ORDER BY 1, 2
"""

# Other sessions on this database that a superuser opened. usesysid and application_name are visible to
# every role, so the unprivileged runtime login can see them.
SUPERUSER_SESSIONS_SQL = """
SELECT count(*),
       coalesce(string_agg(DISTINCT coalesce(nullif(a.application_name, ''), 'unnamed'), ', '), '')
FROM pg_catalog.pg_stat_activity a
JOIN pg_catalog.pg_roles r ON r.oid = a.usesysid
WHERE r.rolsuper AND a.datname = pg_catalog.current_database() AND a.pid <> pg_catalog.pg_backend_pid()
"""


def short(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0] if lines else ''}"[:240]


def identity_result(user, expected, problems):
    if user != expected:
        return ("FAIL", "identity", f"connected as {user}, expected {expected} (docs/database-security.md)")
    if problems:
        return ("FAIL", "identity", f"{user} is too privileged: {', '.join(problems)}")
    return ("PASS", "identity", f"{user}: no superuser, owner, CREATE or predefined-role power")


def schema_result(revisions, required):
    revisions = list(revisions)
    if revisions == [required]:
        return ("PASS", "schema", f"revision {required}, the one this code requires")
    seen = ", ".join(revisions) or "no revision"
    return ("FAIL", "schema", f"database at {seen}, code requires {required}: "
                              "docker compose run --rm migrate, then docker compose run --rm db-init")


def grant_results(rows):
    """rows: (name, group, executes, any_table, unreadable, relations) from PRIVILEGES_SQL."""
    groups = {row[0]: row for row in rows if row[0] == row[1]}
    missing = [name for name in GROUPS if name not in groups]
    if missing:
        return [("FAIL", "grants", f"group roles missing ({', '.join(missing)}): docker compose run --rm db-init")]
    problems = []
    for name in GROUPS:
        _, _, executes, any_table, unreadable, _ = groups[name]
        if executes != ROUTINE_OF[name]:
            problems.append(f"{name} executes [{executes or 'nothing'}], expected [{ROUTINE_OF[name]}]")
        if name in AI_GROUPS and int(any_table):
            problems.append(f"{name} holds privileges on {any_table} relation(s)")
        if name == RUNTIME_GROUP and unreadable:
            problems.append(f"{name} cannot read {unreadable}")
    if problems:
        return [("FAIL", "grants", "; ".join(problems) + " (db-init re-applies them; run it after every migration)")]
    relations = groups[RUNTIME_GROUP][5]
    return [("PASS", "grants", f"runtime group reads all {relations} relations; AI groups hold no table "
                               "privilege; each group executes only its own routine")]


def ai_login_results(login_rows, privilege_rows):
    """login_rows from LOGINS_SQL, privilege_rows from PRIVILEGES_SQL."""
    if not login_rows:
        return [("SKIP", "ai-logins", "no login is a member of an AI group yet (WP-13 PR-2 provisions them)")]
    attributes = {(row[0], row[1]): row for row in login_rows}
    problems, names = [], []
    for name, group, executes, any_table, _, _ in privilege_rows:
        if name == group:
            continue
        names.append(f"{name} ({group})")
        _, _, privileged, other_groups = attributes.get((name, group), (name, group, False, ""))
        if privileged:
            problems.append(f"{name} holds role attributes or a predefined role")
        if other_groups:
            problems.append(f"{name} is also a member of {other_groups}")
        if executes != ROUTINE_OF[group]:
            problems.append(f"{name} executes [{executes or 'nothing'}], expected [{ROUTINE_OF[group]}]")
        if int(any_table):
            problems.append(f"{name} holds privileges on {any_table} relation(s)")
    if problems:
        return [("FAIL", "ai-logins", "; ".join(problems))]
    return [("PASS", "ai-logins", f"{', '.join(names)}: EXECUTE on their routine only, no table access")]


def superuser_result(count, names):
    if int(count) == 0:
        return ("PASS", "superuser", "no other session on this database is a superuser's")
    return ("FAIL", "superuser", f"{count} other session(s) connected as a superuser ({names}): an old container, "
                                 "a tool or an open psql; no service may use the superuser")


def flags_result(settings):
    """The feature switches in effect: on, and (for a sub-switch) its parent on too."""
    values = settings.model_dump()
    on = sorted(name.upper() for name, value in values.items()
                if name.endswith("_enabled") and value is True and values.get(PARENT_FLAG.get(name, name)) is True)
    return ("NOTE", "flags", f"in effect in core-api: {', '.join(on) or 'none'}")


def database_results(conn, expected_user):
    """Every database check on `conn`, an open connection inside a transaction, each in a savepoint."""
    from sqlalchemy import text

    from app.database_compatibility import REQUIRED_SCHEMA_REVISION, runtime_identity_problems

    def rows(sql):
        return [tuple(row) for row in conn.execute(text(sql))]

    checks = (
        ("identity", lambda: [identity_result(
            conn.execute(text("SELECT current_user")).scalar_one(), expected_user, runtime_identity_problems(conn))]),
        ("schema", lambda: [schema_result(
            conn.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all(),
            REQUIRED_SCHEMA_REVISION)]),
        ("grants", lambda: grant_results(rows(PRIVILEGES_SQL))),
        ("ai-logins", lambda: ai_login_results(rows(LOGINS_SQL), rows(PRIVILEGES_SQL))),
        ("superuser", lambda: [superuser_result(*conn.execute(text(SUPERUSER_SESSIONS_SQL)).one())]),
    )
    results = []
    for name, check in checks:
        try:
            with conn.begin_nested():
                results.extend(check())
        except Exception as exc:  # noqa: BLE001 - reported as a result line
            results.append(("FAIL", name, short(exc)))
    return results


def verify_upload_store(conn, upload_dir):
    from sqlalchemy.orm import Session

    from app import storage_identity
    from app.storage import LocalFileStorage

    with Session(bind=conn) as session:
        return storage_identity.verify(session, LocalFileStorage(upload_dir))


def api_command(expected_user):
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import NullPool

        from app.config import Settings

        settings = Settings()
        engine = create_engine(settings.database_url.get_secret_value(), poolclass=NullPool,
                               connect_args={"connect_timeout": 5})
    except Exception as exc:  # noqa: BLE001 - reported as a result line
        return [("FAIL", "core-api", f"cannot load the application settings: {short(exc)}")]
    results = []
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            results.extend(database_results(conn, expected_user))
            try:
                with conn.begin_nested():
                    storage_id = verify_upload_store(conn, settings.upload_dir)
                results.append(("PASS", "upload-store", f"store {str(storage_id)[:8]}... is the one this database recorded"))
            except Exception as exc:  # noqa: BLE001 - reported as a result line
                results.append(("FAIL", "upload-store", short(exc)))
            conn.rollback()
    except Exception as exc:  # noqa: BLE001 - reported as a result line
        results.append(("FAIL", "database", f"core-api cannot query its database: {short(exc)}"))
    finally:
        engine.dispose()
    results.append(flags_result(settings))
    return results


def worker_command(role, service):
    logging.disable(logging.CRITICAL)  # the preflight logs; this command reports
    try:
        from app.config import Settings
        from app.ingestion import runtime

        settings = Settings()
        runtime.preflight_for_role(role, settings, os.environ)
    except Exception as exc:  # noqa: BLE001 - reported as a result line
        return [("FAIL", "preflight", f"{service}: {short(exc)}")]
    if role != runtime.ROLE_PROCESSING:
        return [("PASS", "preflight", f"{service}: start-up checks pass (role {role})")]
    try:
        from sqlalchemy import text

        engine = runtime.verified_engine(runtime.database_target(os.environ))
        try:
            with engine.connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                verify_upload_store(conn, settings.upload_dir)
                conn.rollback()
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - reported as a result line
        return [("FAIL", "preflight", f"{service}: start-up checks pass, but the upload store does not: {short(exc)}")]
    return [("PASS", "preflight", f"{service}: start-up checks pass (role processing), upload store verified")]


def main(argv):
    if len(argv) >= 1 and argv[0] == "api" and len(argv) <= 2:
        results = api_command(argv[1] if len(argv) == 2 else "studygraph_app")
    elif len(argv) == 3 and argv[0] == "worker":
        results = worker_command(argv[1], argv[2])
    else:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    for result in results:
        print("|".join(str(part) for part in result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
