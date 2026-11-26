"""Read-only operational views for administrators (WP-4; docs/admin-api.md).

Everything here reads existing tables in one short REPEATABLE READ, READ ONLY transaction
(app/quiz/access.short_tx: statement and lock timeouts) and writes nothing.

Authorization: the caller's `users` row is re-read in that same transaction; only an
existing, active `admin` passes (401 for a vanished account, 403 otherwise). The CE-3
dependency authenticates; nothing here trusts a role from the token alone.

Disclosure: ids, statuses, counts, timestamps and stable error codes only. No document
text, titles, URLs of class material, learner data or credentials. A job's stored
operational message ("code: detail", which may contain exception text) is never returned:
only its code and the reviewed public message for that code (app/ingestion/jobs.py).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser, not_authenticated
from ..config import Settings
from ..errors import ApiError
from ..exams.http import decode_cursor, encode_cursor
from ..ingestion.jobs import GENERIC_FAILURE, PUBLIC_MESSAGES
from ..models import DOCUMENT_STATUSES
from ..quiz.access import short_tx
from ..services.course_access import ACTIVE, account_disabled, require_active

ADMIN = "admin"
INGESTION_STATUSES = ("queued", "running", "succeeded", "failed")
HARVEST_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
JOB_KINDS = ("parse", "index", "harvest")
JOB_STATUSES = HARVEST_STATUSES
FEATURES = (
    "dispatch_on_upload", "indexing_enabled", "chat_enabled", "harvest_enabled", "quiz_enabled",
    "dashboard_enabled", "analytics_enabled", "study_events_enabled", "mastery_delivery_enabled",
    "exams_enabled", "exam_export_enabled",
)  # fmt: skip

_STORED_CODE = re.compile(r"^([a-z][a-z0-9_]{0,63}):")


def admin_required() -> ApiError:
    return ApiError(403, "admin_required", "Only administrators can see this.")


def require_admin(session: Session, user: CurrentUser, *, lock: bool = False) -> None:
    """The caller's users row must exist and be an active admin. `lock` (mutations, WP-9) holds the row
    FOR SHARE until the transaction ends, so a concurrent demotion or disablement cannot interleave."""
    require_active(user)
    account = session.execute(
        text("SELECT role, status FROM users WHERE id = :u" + (" FOR SHARE" if lock else "")), {"u": user.id}
    ).one_or_none()
    if account is None:
        raise not_authenticated()
    if account.status != ACTIVE:
        raise account_disabled()
    if account.role != ADMIN:
        raise admin_required()


def authorize(factory: sessionmaker[Session], user: CurrentUser) -> None:
    with short_tx(factory, read_only=True) as session:
        require_admin(session, user)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _str(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def stored_error_code(stored_message: str | None) -> str | None:
    """The stable code of a document job's stored message ("code: detail"); never the detail itself."""
    if stored_message is None:
        return None
    match = _STORED_CODE.match(stored_message)
    return "unknown" if match is None else match.group(1)


def _job_error(stored_message: str | None, error_code: str | None) -> dict | None:
    """A safe summary: the stable code and, for document jobs, the reviewed public message."""
    if error_code is not None:  # harvest runs store a code only
        return {"code": error_code, "message": None}
    code = stored_error_code(stored_message)
    if code is None:
        return None
    if code == "unknown":
        return {"code": code, "message": GENERIC_FAILURE}
    return {"code": code, "message": PUBLIC_MESSAGES.get(code, GENERIC_FAILURE)}


def _zeroed(rows, statuses: tuple[str, ...]) -> dict[str, int]:
    counts = dict.fromkeys(statuses, 0)
    for status, count in rows:
        counts[status] = count  # a status outside the CHECK list cannot exist
    return counts


# --- overview -----------------------------------------------------------------------------------------------


def overview(factory: sessionmaker[Session], user: CurrentUser, settings: Settings) -> dict:
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_admin(session, user)
        as_of = session.execute(text("SELECT now()")).scalar_one()
        sources = session.execute(
            text("SELECT count(*) AS total, count(*) FILTER (WHERE enabled IS TRUE) AS enabled FROM sources")
        ).one()
        documents = session.execute(text("SELECT status, count(*) FROM documents GROUP BY status")).all()
        ingestion = session.execute(text("SELECT kind, status, count(*) FROM ingestion_jobs GROUP BY kind, status")).all()
        harvest = session.execute(text("SELECT status, count(*) FROM harvest_runs GROUP BY status")).all()
    return {
        "as_of": _iso(as_of),
        "features": {name: bool(getattr(settings, name)) for name in FEATURES},
        "sources": {"total": sources.total, "enabled": sources.enabled},
        "documents": _zeroed(documents, DOCUMENT_STATUSES),
        "jobs": {
            "parse": _zeroed([(s, n) for k, s, n in ingestion if k == "parse"], INGESTION_STATUSES),
            "index": _zeroed([(s, n) for k, s, n in ingestion if k == "index"], INGESTION_STATUSES),
            "harvest": _zeroed(harvest, HARVEST_STATUSES),
        },
    }


# --- sources ------------------------------------------------------------------------------------------------

# sources.created_at is a timestamp without time zone (baseline schema); it is read as UTC everywhere here,
# so the page order and the cursor key compare the same value.
_SOURCES = text(
    """
    SELECT s.id, s.kind, s.provider, s.language, s.domain, s.licence, s.licence_url, s.enabled,
           s.created_at AT TIME ZONE 'UTC' AS created_at,
           (SELECT count(*) FROM harvest_targets t WHERE t.source_id = s.id) AS courses,
           dc.pending, dc.indexed, dc.failed, dc.stale,
           lr.id AS run_id, lr.status AS run_status, lr.created_at AS run_created_at,
           lr.finished_at AS run_finished_at, lr.error_code AS run_error_code
    FROM sources s
    LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE d.status = 'pending') AS pending,
               count(*) FILTER (WHERE d.status = 'indexed') AS indexed,
               count(*) FILTER (WHERE d.status = 'failed') AS failed,
               count(*) FILTER (WHERE d.status = 'stale') AS stale
        FROM documents d WHERE d.source_id = s.id
    ) dc ON TRUE
    LEFT JOIN LATERAL (
        SELECT r.id, r.status, r.created_at, r.finished_at, r.error_code FROM harvest_runs r
        WHERE r.source_id = s.id ORDER BY r.created_at DESC, r.id DESC LIMIT 1
    ) lr ON TRUE
    WHERE (CAST(:enabled AS boolean) IS NULL OR s.enabled = CAST(:enabled AS boolean))
      AND (CAST(:key AS timestamptz) IS NULL
           OR (s.created_at AT TIME ZONE 'UTC', s.id) < (CAST(:key AS timestamptz), CAST(:last AS uuid)))
    ORDER BY s.created_at DESC, s.id DESC
    LIMIT :limit
    """
)


def _source(row) -> dict:
    last_run = None
    if row.run_id is not None:
        last_run = {"id": str(row.run_id), "status": row.run_status, "created_at": _iso(row.run_created_at),
                    "finished_at": _iso(row.run_finished_at), "error_code": row.run_error_code}  # fmt: skip
    return {
        "id": str(row.id), "kind": row.kind, "provider": row.provider, "language": row.language,
        "domain": row.domain, "licence": row.licence, "licence_url": row.licence_url, "enabled": bool(row.enabled),
        "created_at": _iso(row.created_at), "courses": row.courses,
        "documents": {"pending": row.pending, "indexed": row.indexed, "failed": row.failed, "stale": row.stale},
        "last_run": last_run,
    }  # fmt: skip


def list_sources(
    factory: sessionmaker[Session], user: CurrentUser, enabled: bool | None, limit: int, cursor: str | None
) -> dict:
    filters = {"enabled": enabled}
    after = decode_cursor(cursor, "admin.sources", user.id, None, None, filters)
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_admin(session, user)
        rows = session.execute(_SOURCES, {
            "enabled": enabled, "key": None if after is None else after[0],
            "last": None if after is None else str(after[1]), "limit": limit + 1,
        }).all()  # fmt: skip
    return _page(rows, limit, _source, "admin.sources", user.id, filters)


# --- jobs ---------------------------------------------------------------------------------------------------

_JOBS = text(
    """
    WITH jobs AS (
        SELECT j.id, j.kind, j.status, j.created_at, j.started_at, j.finished_at,
               j.attempt_count AS attempts, j.max_attempts AS attempt_limit,
               j.error_message AS stored_message, CAST(NULL AS varchar) AS error_code,
               d.course_id, j.document_id, CAST(NULL AS uuid) AS source_id, CAST(NULL AS varchar) AS trigger
        FROM ingestion_jobs j JOIN documents d ON d.id = j.document_id
        WHERE CAST(:kind AS varchar) IS NULL OR j.kind = CAST(:kind AS varchar)
        UNION ALL
        SELECT r.id, 'harvest', r.status, r.created_at, r.started_at, r.finished_at,
               r.claim_count, r.max_claims, CAST(NULL AS text), r.error_code,
               r.course_id, CAST(NULL AS uuid), r.source_id, r.trigger
        FROM harvest_runs r
        WHERE CAST(:kind AS varchar) IS NULL OR CAST(:kind AS varchar) = 'harvest'
    )
    SELECT * FROM jobs
    WHERE (CAST(:status AS varchar) IS NULL OR status = CAST(:status AS varchar))
      AND (CAST(:key AS timestamptz) IS NULL OR (created_at, id) < (CAST(:key AS timestamptz), CAST(:last AS uuid)))
    ORDER BY created_at DESC, id DESC
    LIMIT :limit
    """
)


def _job(row) -> dict:
    return {
        "id": str(row.id), "kind": row.kind, "status": row.status, "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at), "finished_at": _iso(row.finished_at),
        "attempts": {"used": row.attempts, "limit": row.attempt_limit},
        "subject": {"course_id": _str(row.course_id), "document_id": _str(row.document_id),
                    "source_id": _str(row.source_id), "trigger": row.trigger},
        "error": _job_error(row.stored_message, row.error_code),
    }  # fmt: skip


def list_jobs(
    factory: sessionmaker[Session], user: CurrentUser, kind: str | None, status: str | None, limit: int,
    cursor: str | None,
) -> dict:  # fmt: skip
    filters = {"kind": kind, "status": status}
    after = decode_cursor(cursor, "admin.jobs", user.id, None, None, filters)
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_admin(session, user)
        rows = session.execute(_JOBS, {
            "kind": kind, "status": status, "key": None if after is None else after[0],
            "last": None if after is None else str(after[1]), "limit": limit + 1,
        }).all()  # fmt: skip
    return _page(rows, limit, _job, "admin.jobs", user.id, filters)


def _page(rows, limit: int, shape, endpoint: str, user_id: uuid.UUID, filters: dict) -> dict:
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = encode_cursor(endpoint, user_id, None, None, filters, last.created_at, last.id)
    return {"items": [shape(row) for row in items], "next_cursor": next_cursor}
