"""The parse-job state machine, in SQL. PostgreSQL is the only authority on job state.

    queued --claim--> running --complete--> succeeded   (+ sections, page_count, queued index job)
                         |----permanent / exhausted----> failed (document failed if pending)
                         |----transient, attempts left--> queued (available_at = now() + backoff)
    running, lease expired --recovery--> queued (attempts left) | failed (exhausted)

Concurrency model (PostgreSQL, READ COMMITTED):

- Every transition is a conditional UPDATE (or a SELECT ... FOR UPDATE with
  the same condition). When two sessions race for a row, the second waits for
  the first's row lock, then re-evaluates its WHERE clause against the new row
  version: exactly one claim of a queued job succeeds.
- A claim stores a fresh attempt_token (gen_random_uuid()). Everything done
  on behalf of an attempt afterwards (complete, fail, requeue) first locks the
  job row with `status = 'running' AND attempt_token = :token`. Recovery and
  requeueing clear the token, a new claim replaces it, so a stale attempt
  (a worker that was frozen past its lease) finds no row and writes nothing:
  no sections, no job state, no document state. Fencing is by token, not by
  time: an attempt whose lease expired but which was not recovered yet may
  still complete, because no newer attempt exists.
- Lock order is the same everywhere: the parse job row(s) first, then the
  document row, then (completion only) the document's active index job.
  Recovery and dispatch take job rows with SKIP LOCKED and never wait for a
  row another session holds.
- No transaction or row lock is held while a PDF is being parsed: claiming
  commits at once; completion and failure are short transactions.
- All times come from the database clock (now()).
"""

from __future__ import annotations

import enum
import logging
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import DocumentSection
from .normalize import Section

log = logging.getLogger(__name__)

OPERATIONAL_MESSAGE_LIMIT = 2000

# Shown to users (documents.error_message, through the public `error` field).
# Never contains paths, keys, exception text or other internals.
GENERIC_FAILURE = "This document could not be processed because of a server problem."
PUBLIC_MESSAGES = {
    "encrypted_pdf": "This PDF is password-protected or encrypted. Upload a copy without protection.",
    "invalid_pdf": "This PDF could not be read. It may be damaged; export it again and upload the new file.",
    "no_text": "No selectable text was found in this PDF. Scanned documents are not supported yet.",
    "too_many_pages": "This PDF has more pages than the current processing limit allows.",
    "too_much_text": "This PDF contains more text than the current processing limit allows.",
    "processing_limit": "This PDF is too complex to process within the current limits.",
    "file_unavailable": "The stored file for this document is unavailable, so it could not be processed.",
    "retries_exhausted": "Processing failed repeatedly because of a temporary server problem.",
    "index_handoff_conflict": GENERIC_FAILURE,
}


@dataclass(frozen=True)
class Failure:
    """Why an attempt failed. `code` is stable and logged; `detail` is operational only."""

    code: str
    detail: str
    permanent: bool
    # False for failures that say nothing about the document (e.g. it is no
    # longer pending): the job fails, the document is left as it is.
    affects_document: bool = True

    @property
    def public_message(self) -> str:
        return PUBLIC_MESSAGES.get(self.code, GENERIC_FAILURE)

    @property
    def operational_message(self) -> str:
        return f"{self.code}: {self.detail}"[:OPERATIONAL_MESSAGE_LIMIT]


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    document_id: uuid.UUID
    token: uuid.UUID
    attempt: int
    max_attempts: int
    document_status: str
    storage_key: str | None
    size_bytes: int | None
    content_hash: str | None


class CompletionStatus(enum.Enum):
    COMPLETED = "completed"
    STALE = "stale"  # the attempt no longer owns the job; nothing written
    DOCUMENT_NOT_PENDING = "document_not_pending"  # job failed, document untouched
    INDEX_CONFLICT = "index_conflict"  # an index job was already active; nothing written


class FailureStatus(enum.Enum):
    REQUEUED = "requeued"
    FAILED = "failed"
    STALE = "stale"


class CompletionUnconfirmed(Exception):
    """COMMIT of a completion raised and the job does not show this attempt's success."""


@dataclass(frozen=True)
class RecoveryResult:
    requeued: tuple[uuid.UUID, ...]
    failed: tuple[uuid.UUID, ...]


def backoff_seconds(attempt: int, settings: Settings, rng: random.Random | None = None) -> int:
    """Delay before retry number `attempt` (1-based): base * 2**(attempt-1), +-20 %, capped."""
    rng = rng or random
    base = settings.retry_backoff_base_seconds * (2 ** max(attempt - 1, 0))
    jittered = base * rng.uniform(0.8, 1.2)
    return int(min(settings.retry_backoff_max_seconds, round(jittered)))


# --- claim ----------------------------------------------------------------------------------

_CLAIM = text(
    """
    UPDATE ingestion_jobs
    SET status = 'running', attempt_count = attempt_count + 1, started_at = now(),
        attempt_token = gen_random_uuid(), lease_expires_at = now() + :lease * interval '1 second',
        updated_at = now()
    WHERE id = :job_id AND kind = 'parse' AND status = 'queued'
      AND available_at <= now() AND attempt_count < max_attempts
    RETURNING document_id, attempt_token, attempt_count, max_attempts
    """
)


def claim_parse(session: Session, job_id: uuid.UUID, lease_seconds: int) -> Claim | None:
    """Claim a queued parse job for a new attempt; None if it is not claimable. The caller commits."""
    row = session.execute(_CLAIM, {"job_id": job_id, "lease": lease_seconds}).one_or_none()
    if row is None:
        return None
    document = session.execute(
        text("SELECT status, storage_key, size_bytes, content_hash FROM documents WHERE id = :id"),
        {"id": row.document_id},
    ).one()
    return Claim(
        job_id=job_id,
        document_id=row.document_id,
        token=row.attempt_token,
        attempt=row.attempt_count,
        max_attempts=row.max_attempts,
        document_status=document.status,
        storage_key=document.storage_key,
        size_bytes=document.size_bytes,
        content_hash=document.content_hash,
    )


# --- shared pieces --------------------------------------------------------------------------

_LOCK_OWNED = text(
    "SELECT document_id, attempt_count, max_attempts FROM ingestion_jobs "
    "WHERE id = :job_id AND kind = 'parse' AND status = 'running' AND attempt_token = :token FOR UPDATE"
)


def _finish_failed(session: Session, job_id: uuid.UUID, message: str) -> None:
    session.execute(
        text(
            "UPDATE ingestion_jobs SET status = 'failed', finished_at = now(), lease_expires_at = NULL, "
            "error_message = :message, updated_at = now() WHERE id = :job_id"
        ),
        {"job_id": job_id, "message": message},
    )


def _requeue(session: Session, job_id: uuid.UUID, message: str, delay_seconds: int) -> None:
    session.execute(
        text(
            "UPDATE ingestion_jobs SET status = 'queued', attempt_token = NULL, lease_expires_at = NULL, "
            "last_dispatched_at = NULL, available_at = now() + :delay * interval '1 second', "
            "error_message = :message, updated_at = now() WHERE id = :job_id"
        ),
        {"job_id": job_id, "message": message, "delay": delay_seconds},
    )


def _fail_document(session: Session, document_id: uuid.UUID, public_message: str) -> bool:
    """Mark the document failed, but only while it is pending (never overwrite indexed or stale)."""
    status = session.execute(
        text("SELECT status FROM documents WHERE id = :id FOR UPDATE"), {"id": document_id}
    ).scalar_one_or_none()
    if status != "pending":
        return False
    session.execute(
        text("UPDATE documents SET status = 'failed', error_message = :message WHERE id = :id"),
        {"id": document_id, "message": public_message},
    )
    return True


# --- completion -----------------------------------------------------------------------------


def complete_parse(
    session_factory: sessionmaker[Session], claim: Claim, page_count: int, sections: Sequence[Section]
) -> CompletionStatus:
    """Persist the result of a successful attempt atomically, if the attempt still owns the job.

    One transaction: replace the document's sections, set page_count, mark the
    job succeeded and create the queued index job (the M4 hand-off). The
    document stays `pending`: parsed is not indexed.
    """
    if not sections:
        raise ValueError("a successful parse has at least one section")
    session = session_factory()
    try:
        try:
            status = _write_completion(session, claim, page_count, sections)
        except BaseException:
            _rollback_quietly(session)
            raise
        if status is not CompletionStatus.COMPLETED:
            return status
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 - the outcome is established on a fresh connection
            if _completion_committed(session_factory, claim):
                log.warning("parse completion commit raised but is committed job_id=%s", claim.job_id)
                return CompletionStatus.COMPLETED
            raise CompletionUnconfirmed(str(claim.job_id)) from exc
        return CompletionStatus.COMPLETED
    finally:
        session.close()


def _write_completion(
    session: Session, claim: Claim, page_count: int, sections: Sequence[Section]
) -> CompletionStatus:
    """The statements of complete_parse. Returns COMPLETED with the transaction still open;
    any other status has already committed or rolled back."""
    owned = session.execute(_LOCK_OWNED, {"job_id": claim.job_id, "token": claim.token}).one_or_none()
    if owned is None:
        session.rollback()
        return CompletionStatus.STALE
    document_status = session.execute(
        text("SELECT status FROM documents WHERE id = :id FOR UPDATE"), {"id": owned.document_id}
    ).scalar_one()
    if document_status != "pending":
        _finish_failed(session, claim.job_id, f"document_not_pending: document status is {document_status}")
        session.commit()
        return CompletionStatus.DOCUMENT_NOT_PENDING
    active_index = session.execute(
        text(
            "SELECT id FROM ingestion_jobs WHERE document_id = :id AND kind = 'index' "
            "AND status IN ('queued', 'running') FOR UPDATE"
        ),
        {"id": owned.document_id},
    ).first()
    if active_index is not None:
        session.rollback()
        return CompletionStatus.INDEX_CONFLICT

    session.execute(text("DELETE FROM document_sections WHERE document_id = :id"), {"id": owned.document_id})
    session.execute(
        insert(DocumentSection),
        [
            {"document_id": owned.document_id, "ordinal": s.ordinal, "text": s.text, "locator": s.locator}
            for s in sections
        ],
    )
    session.execute(
        text("UPDATE documents SET page_count = :pages, error_message = NULL WHERE id = :id"),
        {"id": owned.document_id, "pages": page_count},
    )
    session.execute(
        text(
            "UPDATE ingestion_jobs SET status = 'succeeded', finished_at = now(), lease_expires_at = NULL, "
            "error_message = NULL, updated_at = now() WHERE id = :job_id AND attempt_token = :token"
        ),
        {"job_id": claim.job_id, "token": claim.token},
    )
    try:
        with session.begin_nested():
            session.execute(
                text("INSERT INTO ingestion_jobs (document_id, kind) VALUES (:id, 'index')"),
                {"id": owned.document_id},
            )
    except IntegrityError:
        # Only the one-active-job-per-kind index can be violated here: a
        # concurrent writer created an index job. Not a successful hand-off.
        session.rollback()
        return CompletionStatus.INDEX_CONFLICT
    return CompletionStatus.COMPLETED


def _completion_committed(session_factory: sessionmaker[Session], claim: Claim) -> bool:
    try:
        with session_factory() as probe:
            row = probe.execute(
                text("SELECT status, attempt_token FROM ingestion_jobs WHERE id = :id"), {"id": claim.job_id}
            ).one_or_none()
    except SQLAlchemyError:
        log.warning("could not verify the completion of job %s", claim.job_id, exc_info=True)
        return False
    return row is not None and row.status == "succeeded" and row.attempt_token == claim.token


def _rollback_quietly(session: Session) -> None:
    try:
        session.rollback()
    except Exception:  # noqa: BLE001
        log.warning("rollback failed", exc_info=True)


# --- failure --------------------------------------------------------------------------------


def record_failure(
    session_factory: sessionmaker[Session],
    claim: Claim,
    failure: Failure,
    settings: Settings,
    rng: random.Random | None = None,
) -> FailureStatus:
    """Record a failed attempt, if it still owns the job.

    Permanent failures, and transient ones on the last attempt, fail the job
    (and the document, if pending and the failure is about it). Other transient
    failures put the job back in the queue after a backoff.
    """
    with session_factory() as session:
        owned = session.execute(_LOCK_OWNED, {"job_id": claim.job_id, "token": claim.token}).one_or_none()
        if owned is None:
            session.rollback()
            return FailureStatus.STALE
        if failure.permanent or owned.attempt_count >= owned.max_attempts:
            _finish_failed(session, claim.job_id, failure.operational_message)
            if failure.affects_document:
                public = failure.public_message if failure.permanent else PUBLIC_MESSAGES["retries_exhausted"]
                _fail_document(session, owned.document_id, public)
            session.commit()
            return FailureStatus.FAILED
        _requeue(session, claim.job_id, failure.operational_message, backoff_seconds(owned.attempt_count, settings, rng))
        session.commit()
        return FailureStatus.REQUEUED


# --- recovery -------------------------------------------------------------------------------


def recover_expired_leases(
    session_factory: sessionmaker[Session], settings: Settings, rng: random.Random | None = None
) -> RecoveryResult:
    """Requeue (or fail, when exhausted) running parse jobs whose lease expired.

    Also fails queued parse jobs that have no attempts left (only possible
    after manual edits), so they cannot wait forever. Safe to run in several
    processes at once: rows are taken with SKIP LOCKED, and each update is
    conditional on the token read under the row lock.
    """
    requeued: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    with session_factory() as session:
        expired = session.execute(
            text(
                "SELECT id, document_id, attempt_count, max_attempts, attempt_token FROM ingestion_jobs "
                "WHERE kind = 'parse' AND status = 'running' AND lease_expires_at < now() "
                "ORDER BY lease_expires_at, id LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"limit": settings.recovery_batch_size},
        ).all()
        for row in expired:
            message = f"lease_expired: attempt {row.attempt_count} did not finish before its lease expired"
            if row.attempt_count < row.max_attempts:
                _requeue(session, row.id, message, backoff_seconds(row.attempt_count, settings, rng))
                requeued.append(row.id)
            else:
                _finish_failed(session, row.id, message)
                _fail_document(session, row.document_id, PUBLIC_MESSAGES["retries_exhausted"])
                failed.append(row.id)
        exhausted = session.execute(
            text(
                "SELECT id, document_id FROM ingestion_jobs "
                "WHERE kind = 'parse' AND status = 'queued' AND attempt_count >= max_attempts "
                "ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"limit": settings.recovery_batch_size},
        ).all()
        for row in exhausted:
            _finish_failed(session, row.id, "retries_exhausted: queued without attempts left")
            _fail_document(session, row.document_id, PUBLIC_MESSAGES["retries_exhausted"])
            failed.append(row.id)
        session.commit()
    for job_id in requeued:
        log.warning("lease_recovered job_id=%s action=requeued", job_id)
    for job_id in failed:
        log.warning("lease_recovered job_id=%s action=failed", job_id)
    return RecoveryResult(tuple(requeued), tuple(failed))


# --- operator retry -------------------------------------------------------------------------


class RetryRefused(Exception):
    """The document cannot be retried (reason in the message, safe to show an operator)."""


def retry_parse(session_factory: sessionmaker[Session], document_id: uuid.UUID) -> uuid.UUID:
    """Queue a new parse job for a failed document; returns the new job's id.

    A new job row (new id, attempt count 0, new tokens): the failed job stays
    as history and no attempt identity is ever reused. Only for a document
    that is `failed`, has a stored file, and has neither an active nor a
    succeeded parse job (successful documents are never reparsed here).
    Concurrent retries of the same document serialize on the job and document
    row locks (taken in the usual order); the loser is refused.
    """
    with session_factory() as session:
        jobs = session.execute(
            text(
                "SELECT id, status FROM ingestion_jobs WHERE document_id = :id AND kind = 'parse' "
                "ORDER BY id FOR UPDATE"
            ),
            {"id": document_id},
        ).all()
        document = session.execute(
            text("SELECT status, storage_key FROM documents WHERE id = :id FOR UPDATE"), {"id": document_id}
        ).one_or_none()
        if document is None:
            raise RetryRefused("no such document")
        if document.storage_key is None:
            raise RetryRefused("the document has no stored file")
        if document.status != "failed":
            raise RetryRefused(f"the document is {document.status}, not failed")
        # Re-read: a job inserted by a concurrent retry is not in `jobs`.
        statuses = set(
            session.execute(
                text("SELECT status FROM ingestion_jobs WHERE document_id = :id AND kind = 'parse'"),
                {"id": document_id},
            ).scalars()
        ) | {row.status for row in jobs}
        if statuses & {"queued", "running"}:
            raise RetryRefused("a parse job is already queued or running")
        if "succeeded" in statuses:
            raise RetryRefused("the document was already parsed; reparsing is not supported")
        try:
            job_id = session.execute(
                text("INSERT INTO ingestion_jobs (document_id, kind) VALUES (:id, 'parse') RETURNING id"),
                {"id": document_id},
            ).scalar_one()
        except IntegrityError:
            raise RetryRefused("a parse job is already queued or running") from None
        session.execute(
            text("UPDATE documents SET status = 'pending', error_message = NULL WHERE id = :id"),
            {"id": document_id},
        )
        session.commit()
    log.warning("parse_retry_queued document_id=%s job_id=%s", document_id, job_id)
    return job_id
