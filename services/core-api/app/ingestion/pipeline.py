"""One parse attempt, from claim to completion, and the classification of its failures.

    storage identity check -> claim (short transaction, committed)
    -> open the stored file through the storage abstraction (no symlinks)
    -> verify size and SHA-256 against the document row
    -> extract in the limited subprocess -> normalize -> sections
    -> complete (one transaction, fenced by the attempt token)

Failure classes (docs/document-processing.md):

- Not claimed: the job is not claimable (duplicate message, already done,
  backoff not over) or the store's identity cannot be established. Nothing
  is written; in particular no attempt is consumed because the worker's
  storage is misconfigured.
- Permanent (the document): encrypted, unreadable, no text, over a limit,
  file missing from a verified store, not a regular file, size or hash
  mismatch. The job and the document fail; no retry.
- Transient (the environment): database or storage I/O errors, a store
  that stops verifying after the claim, an extractor crash, a soft time
  limit, anything unexpected. The job is requeued with backoff while
  attempts remain, then fails as exhausted.

If even recording the failure is impossible (database down), the attempt
leaves the job running and its lease expiry makes recovery decide.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .. import storage_identity
from ..config import Settings
from ..storage import LocalFileStorage, StorageError, StoredFileInvalid, StoredFileMissing
from ..storage_identity import StorageIdentityError
from . import jobs
from .extraction import Extraction, ExtractionFailed, ExtractionLimits, extract_pdf
from .jobs import Claim, CompletionStatus, CompletionUnconfirmed, Failure, FailureStatus
from .normalize import build_sections

log = logging.getLogger(__name__)

READ_CHUNK = 1024 * 1024


@dataclass
class WorkerContext:
    session_factory: sessionmaker[Session]
    storage: LocalFileStorage
    settings: Settings
    extract: Callable[[int, ExtractionLimits], Extraction] = extract_pdf
    rng: random.Random | None = None

    @property
    def limits(self) -> ExtractionLimits:
        s = self.settings
        return ExtractionLimits(
            max_pages=s.parse_max_pages,
            max_text_chars=s.parse_max_text_chars,
            timeout_seconds=s.parse_timeout_seconds,
            memory_bytes=s.parse_memory_limit_mb * 1024 * 1024,
        )


@dataclass(frozen=True)
class AttemptResult:
    outcome: str  # not_claimed | storage_unavailable | database_unavailable | succeeded | failed | requeued | stale | unconfirmed | unrecorded
    job_id: uuid.UUID
    code: str | None = None


class _AttemptFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)
        self.failure = failure


def _permanent(code: str, detail: str, *, affects_document: bool = True) -> _AttemptFailed:
    return _AttemptFailed(Failure(code, detail, permanent=True, affects_document=affects_document))


def _transient(code: str, detail: str) -> _AttemptFailed:
    return _AttemptFailed(Failure(code, detail, permanent=False))


def run_parse_job(job_id: uuid.UUID, ctx: WorkerContext) -> AttemptResult:
    try:
        with ctx.session_factory() as session:
            storage_identity.verify(session, ctx.storage)
            claim = jobs.claim_parse(session, job_id, ctx.settings.job_lease_seconds)
            session.commit()
    except StorageIdentityError as exc:
        log.error("parse_not_claimed job_id=%s reason=storage_identity detail=%s", job_id, exc)
        return AttemptResult("storage_unavailable", job_id)
    except SQLAlchemyError:
        log.warning("parse_not_claimed job_id=%s reason=database", job_id, exc_info=True)
        return AttemptResult("database_unavailable", job_id)
    if claim is None:
        log.info("parse_not_claimed job_id=%s reason=not_claimable", job_id)
        return AttemptResult("not_claimed", job_id)

    log.info("parse_claimed job_id=%s document_id=%s attempt=%d", job_id, claim.document_id, claim.attempt)
    try:
        status, page_count = _attempt(claim, ctx)
    except _AttemptFailed as exc:
        return _record(claim, exc.failure, ctx)
    except CompletionUnconfirmed:
        log.error("parse_completion_unconfirmed job_id=%s attempt=%d; lease recovery decides", job_id, claim.attempt)
        return AttemptResult("unconfirmed", job_id)
    except SoftTimeLimitExceeded:
        return _record(claim, Failure("soft_time_limit", "the task reached its soft time limit", permanent=False), ctx)
    except SQLAlchemyError as exc:
        return _record(claim, Failure("database_error", type(exc).__name__, permanent=False), ctx)
    except Exception as exc:  # noqa: BLE001 - anything unexpected is retried, within max_attempts
        log.exception("parse_attempt_error job_id=%s", job_id)
        return _record(claim, Failure("unexpected_error", type(exc).__name__, permanent=False), ctx)

    if status is CompletionStatus.COMPLETED:
        log.info("parse_succeeded job_id=%s document_id=%s pages=%d", job_id, claim.document_id, page_count)
        return AttemptResult("succeeded", job_id)
    if status is CompletionStatus.STALE:
        log.warning("stale_attempt_discarded job_id=%s attempt=%d", job_id, claim.attempt)
        return AttemptResult("stale", job_id)
    if status is CompletionStatus.DOCUMENT_NOT_PENDING:
        log.warning("parse_failed job_id=%s code=document_not_pending", job_id)
        return AttemptResult("failed", job_id, "document_not_pending")
    return _record(
        claim, Failure("index_handoff_conflict", "an index job was already active", permanent=True), ctx
    )


def _record(claim: Claim, failure: Failure, ctx: WorkerContext) -> AttemptResult:
    try:
        status = jobs.record_failure(ctx.session_factory, claim, failure, ctx.settings, ctx.rng)
    except SQLAlchemyError:
        log.error(
            "parse_failure_unrecorded job_id=%s code=%s; lease recovery decides", claim.job_id, failure.code, exc_info=True
        )
        return AttemptResult("unrecorded", claim.job_id, failure.code)
    if status is FailureStatus.STALE:
        log.warning("stale_attempt_discarded job_id=%s attempt=%d code=%s", claim.job_id, claim.attempt, failure.code)
        return AttemptResult("stale", claim.job_id, failure.code)
    if status is FailureStatus.REQUEUED:
        log.warning("parse_requeued job_id=%s attempt=%d code=%s", claim.job_id, claim.attempt, failure.code)
        return AttemptResult("requeued", claim.job_id, failure.code)
    level = logging.WARNING if failure.permanent else logging.ERROR
    log.log(level, "parse_failed job_id=%s attempt=%d code=%s detail=%s", claim.job_id, claim.attempt, failure.code, failure.detail)
    return AttemptResult("failed", claim.job_id, failure.code)


def _attempt(claim: Claim, ctx: WorkerContext) -> tuple[CompletionStatus, int]:
    if claim.document_status != "pending":
        raise _permanent("document_not_pending", f"document status is {claim.document_status}", affects_document=False)
    if claim.storage_key is None or claim.size_bytes is None or claim.content_hash is None:
        raise _permanent("file_unavailable", "the document has no stored file")
    fd = _open_verified(claim, ctx)
    try:
        try:
            extraction = ctx.extract(fd, ctx.limits)
        except ExtractionFailed as exc:
            if exc.permanent:
                raise _permanent(exc.code, exc.detail) from None
            raise _transient(exc.code, exc.detail) from None
    finally:
        os.close(fd)
    sections = build_sections(extraction.pages)
    if not sections:
        raise _permanent("no_text", f"no text on any of {extraction.page_count} pages")
    return jobs.complete_parse(ctx.session_factory, claim, extraction.page_count, sections), extraction.page_count


def _open_verified(claim: Claim, ctx: WorkerContext) -> int:
    """Open the stored file and check it is exactly what was uploaded; returns the open descriptor."""
    try:
        fd = ctx.storage.open_stored(claim.storage_key)
    except StoredFileMissing:
        if _store_still_verified(ctx):
            raise _permanent("file_unavailable", "the stored file is missing from the verified store") from None
        raise _transient("storage_unavailable", "the store stopped verifying after the claim") from None
    except StoredFileInvalid:
        raise _permanent("file_unavailable", "the stored name is not a regular file") from None
    except StorageError as exc:
        raise _transient("storage_error", type(exc.__cause__ or exc).__name__) from None
    try:
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, READ_CHUNK):
            size += len(chunk)
            if size > claim.size_bytes:
                break
            digest.update(chunk)
        if size != claim.size_bytes:
            raise _permanent("file_unavailable", f"stored size {size} differs from the recorded size")
        if digest.hexdigest() != claim.content_hash:
            raise _permanent("file_unavailable", "stored content does not match the recorded SHA-256")
    except OSError as exc:
        os.close(fd)
        raise _transient("storage_error", type(exc).__name__) from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _store_still_verified(ctx: WorkerContext) -> bool:
    try:
        with ctx.session_factory() as session:
            storage_identity.verify(session, ctx.storage)
        return True
    except (StorageIdentityError, SQLAlchemyError):
        return False
