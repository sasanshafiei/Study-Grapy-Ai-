"""Admin mutations with a durable audit trail (WP-9; docs/admin-api.md).

Each mutation is one short READ COMMITTED transaction (app/quiz/access.short_tx: statement and lock
timeouts) that, in this order:
1. locks the caller's users row FOR SHARE and requires an existing, active admin. A concurrent demotion
   or disablement either committed first and is seen here, or waits until this transaction ends;
2. applies the existing operator transition, unchanged (the same functions the CLIs call):
   runs.set_source_enabled, runs.create_run(manual=True), jobs.queue_parse_retry / queue_index_retry;
3. inserts one admin_audit_events row whose actor is the caller (never a value from the request);
4. commits. The change and its audit row commit together or not at all.

A request that changes nothing (a source already in the requested state with no run to cancel) commits
nothing and writes no audit row. A refused operation raises ApiError before anything commits.

Concurrency and repeats rely on the transitions' own locks and constraints: the source row lock, the
partial unique index of one active run per course x source, and the job/document row locks plus the
one-active-job indexes. The loser of a race, or a repeated request, is refused (409) or reports
`changed: false`; it never creates a second run or job.

Audit details hold ids, states, counts and stable error codes only: no titles, text, URLs, stored
operational messages, tokens or exception text.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser
from ..config import Settings
from ..errors import ApiError
from ..harvest import runs
from ..ingestion import jobs
from ..quiz.access import short_tx
from .observability import require_admin, stored_error_code

log = logging.getLogger(__name__)

RETRYABLE_KINDS = ("parse", "index")

_GATE_ERRORS = {
    "course_missing": (404, "course_not_found", "There is no course with this id."),
    "course_not_opted_in": (409, "course_not_opted_in", "This course is not opted in to harvesting."),
    "source_disabled": (409, "source_disabled", "This source is disabled."),
    "target_missing": (409, "target_missing", "This course is not opted in for this source."),
}


def source_not_found() -> ApiError:
    return ApiError(404, "source_not_found", "There is no provider-managed source with this id.")


def job_not_found() -> ApiError:
    return ApiError(404, "job_not_found", "There is no parse or index job with this id.")


def harvest_disabled() -> ApiError:
    return ApiError(409, "harvest_disabled", "Harvesting is switched off on this server.")


def _iso(value: datetime) -> str:
    return value.isoformat()


def _audit(session: Session, actor: CurrentUser, action: str, entity_kind: str, entity_id: uuid.UUID,
           operation_id: uuid.UUID | None, details: dict) -> tuple[uuid.UUID, datetime]:  # fmt: skip
    row = session.execute(
        text(
            "INSERT INTO admin_audit_events (actor_id, action, entity_kind, entity_id, operation_id, details) "
            "VALUES (:actor, :action, :kind, :entity, :operation, CAST(:details AS jsonb)) RETURNING id, created_at"
        ),
        {"actor": actor.id, "action": action, "kind": entity_kind, "entity": entity_id, "operation": operation_id,
         "details": json.dumps(details, sort_keys=True, separators=(",", ":"))},
    ).one()  # fmt: skip
    return row.id, row.created_at


def _committed(action: str, actor: CurrentUser, entity_id: uuid.UUID, event_id: uuid.UUID) -> None:
    log.info("admin_mutation action=%s actor_id=%s entity_id=%s audit_event_id=%s", action, actor.id, entity_id, event_id)


# --- sources ------------------------------------------------------------------------------------------------


def set_source(factory: sessionmaker[Session], user: CurrentUser, source_id: uuid.UUID, enabled: bool) -> dict:
    action = "source_enabled" if enabled else "source_disabled"
    with short_tx(factory) as session:
        require_admin(session, user, lock=True)
        change = runs.set_source_enabled(session, source_id, enabled)
        if change is None:
            raise source_not_found()
        event_id = None
        if change.was_enabled != enabled or change.cancelled_runs:
            event_id, _ = _audit(session, user, action, "source", source_id, None, {
                "before": {"enabled": change.was_enabled}, "after": {"enabled": enabled},
                "cancelled_runs": change.cancelled_runs,
            })  # fmt: skip
            session.commit()
        # else: nothing changed; short_tx rolls the same-value UPDATE back and no audit row is written.
    if event_id is not None:
        _committed(action, user, source_id, event_id)
    return {"source_id": str(source_id), "enabled": enabled, "changed": event_id is not None,
            "cancelled_runs": change.cancelled_runs,
            "audit_event_id": None if event_id is None else str(event_id)}  # fmt: skip


# --- harvest runs -------------------------------------------------------------------------------------------


def trigger_harvest(
    factory: sessionmaker[Session], user: CurrentUser, settings: Settings, course_id: uuid.UUID, source_id: uuid.UUID
) -> dict:
    """Queue a manual run (the CLI's `trigger`); the harvest worker performs every provider request later."""
    with short_tx(factory) as session:
        require_admin(session, user, lock=True)
        if not settings.harvest_enabled:
            raise harvest_disabled()  # a run queued now would only wait; nothing is written while switched off
        if session.execute(
            text("SELECT 1 FROM sources WHERE id = :s AND provider IS NOT NULL"), {"s": source_id}
        ).first() is None:
            raise source_not_found()
        run_id = runs.create_run(session, settings, course_id, source_id, manual=True)
        if run_id is None:
            # create_run holds the course settings and source rows FOR SHARE when they exist, so this
            # explanation is consistent with its decision.
            reason = runs.closed_gate(session, settings, course_id, source_id)
            if reason in _GATE_ERRORS:
                raise ApiError(*_GATE_ERRORS[reason])
            if reason is not None:
                raise harvest_disabled()
            active = session.execute(
                text("SELECT id FROM harvest_runs WHERE course_id = :c AND source_id = :s "
                     "AND status IN ('queued', 'running') LIMIT 1"),
                {"c": course_id, "s": source_id},
            ).scalar_one_or_none()  # fmt: skip
            raise ApiError(409, "harvest_run_active", "A harvest run for this course and source is already queued or running.",
                           extra={"run_id": None if active is None else str(active)})  # fmt: skip
        run = session.execute(
            text("SELECT generation, created_at FROM harvest_runs WHERE id = :r"), {"r": run_id}
        ).one()
        event_id, _ = _audit(session, user, "harvest_triggered", "course", course_id, run_id, {
            "source_id": str(source_id), "run_id": str(run_id), "trigger": "manual", "generation": run.generation,
            "max_claims": settings.harvest_run_max_claims, "max_failures": settings.harvest_run_max_failures,
        })  # fmt: skip
        session.commit()
    _committed("harvest_triggered", user, course_id, event_id)
    return {"run_id": str(run_id), "course_id": str(course_id), "source_id": str(source_id), "status": "queued",
            "trigger": "manual", "created_at": _iso(run.created_at), "audit_event_id": str(event_id)}  # fmt: skip


# --- job retries --------------------------------------------------------------------------------------------


def retry_job(factory: sessionmaker[Session], user: CurrentUser, settings: Settings, job_id: uuid.UUID) -> dict:
    """Queue a new job for the failed parse or index job's document (the CLIs' retry-parse / retry-index).

    The kind and the document come from the failed job itself, so the request cannot pair a job with
    another document. The failed job is not touched: it stays as history with its attempts and error.
    """
    with short_tx(factory) as session:
        require_admin(session, user, lock=True)
        # Not locked: `failed` is terminal. The retry below locks the document's resource, jobs and document
        # in the global order and re-checks every rule under those locks.
        job = session.execute(
            text("SELECT kind, status, document_id, error_message FROM ingestion_jobs WHERE id = :j"), {"j": job_id}
        ).one_or_none()
        if job is None or job.kind not in RETRYABLE_KINDS:
            raise job_not_found()
        if job.status != "failed":
            raise ApiError(409, "job_not_failed", "Only a failed job can be retried.")
        if job.kind == "index" and not settings.indexing_enabled:
            raise ApiError(409, "indexing_disabled", "Indexing is switched off on this server.")
        queue = jobs.queue_parse_retry if job.kind == "parse" else jobs.queue_index_retry
        try:
            new_job_id = queue(session, job.document_id)
        except jobs.RetryRefused as exc:
            # The messages are fixed operator texts (ids and statuses only), reviewed for display.
            raise ApiError(409, "retry_refused", f"This job cannot be retried: {exc}.") from None
        action = f"{job.kind}_retried"
        event_id, _ = _audit(session, user, action, "document", job.document_id, new_job_id, {
            "retried_job_id": str(job_id), "job_id": str(new_job_id), "kind": job.kind,
            "before": {"document_status": "failed", "error_code": stored_error_code(job.error_message)},
            "after": {"document_status": "pending", "job_status": "queued"},
        })  # fmt: skip
        created_at = session.execute(
            text("SELECT created_at FROM ingestion_jobs WHERE id = :j"), {"j": new_job_id}
        ).scalar_one()
        session.commit()
    _committed(action, user, job.document_id, event_id)
    return {"job_id": str(new_job_id), "retried_job_id": str(job_id), "document_id": str(job.document_id),
            "kind": job.kind, "status": "queued", "created_at": _iso(created_at), "audit_event_id": str(event_id)}  # fmt: skip
