"""One index attempt, from the pre-claim gate to the confirmed completion.

    indexing enabled? -> AI service ready and compatible? (cached probe)   } nothing claimed,
                                                                         } no attempt consumed
    -> claim (short transaction, committed; attempt_count + 1, never decremented)
    -> build the canonical request from the stored metadata and sections
    -> POST it (bounded HTTP client) with its content-based idempotency key
    -> confirmed 200 only -> complete (one transaction, fenced by the token,
       content digest re-verified against what is stored now) -> document indexed

Failure classes (docs/ai-indexing-integration.md):

- Not claimed: indexing disabled, AI service unreachable, mock, wrong
  protocol, refusing our credentials, or not ready; job not claimable
  (duplicate message, done, backoff). Nothing is written.
- Permanent (the document): no course, invalid stored content, request
  larger than the limit, refused by the service per protocol (non-retryable
  400/409/413/422 protocol error), content changed during indexing. Job and document fail;
  an operator can retry-index.
- Transient: timeouts, lost responses, 5xx/408/409/429, 202 without a
  confirmation, responses that do not validate, database errors. The job is
  requeued with backoff and the *same* idempotency key is sent again;
  after the last attempt it fails as exhausted. A response showing the
  service is unreachable, refuses our credentials or does not speak the
  protocol also marks it not ready, so no further job is claimed in this
  process until a later probe succeeds; the attempt already claimed counts.

Indexing deadlines are per attempt and start when the request starts; they
never derive from the job's created_at (a job may wait for a long time while
indexing is disabled).
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..ingestion import jobs
from ..ingestion.jobs import Claim, CompletionStatus, CompletionUnconfirmed, Failure, FailureStatus
from ..ingestion.pipeline import AttemptResult
from .client import AiIndexClient, IndexResult, Outcome
from .payload import IndexRequest, InvalidIndexContent, request_for

log = logging.getLogger(__name__)


class CompletionAdapter(Protocol):
    """How an HTTP 202 (accepted, not yet indexed) is handled. Never as success."""

    def on_accepted(self, claim: Claim, request: IndexRequest, result: IndexResult) -> Failure: ...


class SyncOnlyCompletion:
    """The only adapter until AI-1 and CE-1 agree on an asynchronous completion protocol.

    A 202 is a transient failure: the job is retried later with the same
    idempotency key, so an idempotent service that has finished meanwhile
    answers 200 for the replay. If it never does, the job fails as exhausted.
    """

    def on_accepted(self, claim: Claim, request: IndexRequest, result: IndexResult) -> Failure:
        return Failure("index_not_confirmed", "202 Accepted without a confirmation (sync protocol)", permanent=False)


@dataclass
class IndexContext:
    session_factory: sessionmaker[Session]
    settings: Settings
    client: AiIndexClient
    completion: CompletionAdapter = field(default_factory=SyncOnlyCompletion)
    rng: random.Random | None = None


class _AttemptFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)
        self.failure = failure


def run_index_job(job_id: uuid.UUID, ctx: IndexContext) -> AttemptResult:
    if not ctx.settings.indexing_enabled:
        log.info("index_not_claimed job_id=%s reason=indexing_disabled", job_id)
        return AttemptResult("indexing_disabled", job_id)
    readiness = ctx.client.readiness()
    if not readiness.ready:
        log.warning("index_not_claimed job_id=%s reason=ai_not_ready detail=%s", job_id, readiness.reason)
        return AttemptResult("ai_not_ready", job_id, readiness.reason)
    try:
        with ctx.session_factory() as session:
            claim = jobs.claim_index(session, job_id, ctx.settings.index_lease_seconds)
            session.commit()
    except SQLAlchemyError:
        log.warning("index_not_claimed job_id=%s reason=database", job_id, exc_info=True)
        return AttemptResult("database_unavailable", job_id)
    if claim is None:
        log.info("index_not_claimed job_id=%s reason=not_claimable", job_id)
        return AttemptResult("not_claimed", job_id)

    log.info("index_claimed job_id=%s document_id=%s attempt=%d", job_id, claim.document_id, claim.attempt)
    try:
        status = _attempt(claim, ctx, ctx.client.max_request_bytes(readiness))
    except _AttemptFailed as exc:
        return _record(claim, exc.failure, ctx)
    except CompletionUnconfirmed:
        log.error("index_completion_unconfirmed job_id=%s attempt=%d; lease recovery decides", job_id, claim.attempt)
        return AttemptResult("unconfirmed", job_id)
    except SoftTimeLimitExceeded:
        return _record(claim, Failure("soft_time_limit", "the task reached its soft time limit", permanent=False), ctx)
    except SQLAlchemyError as exc:
        return _record(claim, Failure("database_error", type(exc).__name__, permanent=False), ctx)
    except Exception as exc:  # noqa: BLE001 - anything unexpected is retried, within max_attempts
        log.exception("index_attempt_error job_id=%s", job_id)
        return _record(claim, Failure("unexpected_error", type(exc).__name__, permanent=False), ctx)

    if status is CompletionStatus.COMPLETED:
        log.info("index_succeeded job_id=%s document_id=%s", job_id, claim.document_id)
        return AttemptResult("succeeded", job_id)
    if status is CompletionStatus.STALE:
        log.warning("stale_attempt_discarded job_id=%s attempt=%d", job_id, claim.attempt)
        return AttemptResult("stale", job_id)
    if status is CompletionStatus.CONTENT_CHANGED:
        log.warning("index_failed job_id=%s code=index_content_changed", job_id)
        return AttemptResult("failed", job_id, "index_content_changed")
    log.warning("index_failed job_id=%s code=document_not_pending", job_id)
    return AttemptResult("failed", job_id, "document_not_pending")


def _attempt(claim: Claim, ctx: IndexContext, max_bytes: int) -> CompletionStatus:
    if claim.document_status != "pending":
        raise _AttemptFailed(
            Failure(
                "document_not_pending",
                f"document status is {claim.document_status}",
                permanent=True,
                affects_document=False,
            )
        )
    try:
        with ctx.session_factory() as session:
            request = request_for(session, claim.document_id)
            session.rollback()
    except InvalidIndexContent as exc:
        raise _AttemptFailed(Failure("index_invalid_content", str(exc), permanent=True)) from None
    if len(request.body) > max_bytes:
        raise _AttemptFailed(
            Failure("index_payload_too_large", f"{len(request.body)} bytes, limit {max_bytes}", permanent=True)
        )
    log.info(
        "index_request job_id=%s document_id=%s sections=%d bytes=%d key=%s",
        claim.job_id, claim.document_id, request.section_count, len(request.body), request.idempotency_key[:16],
    )  # fmt: skip
    result = ctx.client.index(request)
    if result.outcome is Outcome.INDEXED:
        log.info(
            "index_confirmed job_id=%s chunks=%s replayed=%s", claim.job_id, result.chunk_count, result.replayed
        )
        return jobs.complete_index(ctx.session_factory, claim, request.content_digest)
    if result.outcome is Outcome.ACCEPTED:
        raise _AttemptFailed(ctx.completion.on_accepted(claim, request, result))
    if result.outcome is Outcome.REJECTED:
        raise _AttemptFailed(Failure("index_rejected", result.detail or "rejected", permanent=True))
    if result.outcome is Outcome.TOO_LARGE:
        raise _AttemptFailed(Failure("index_payload_too_large", result.detail or "rejected as too large", permanent=True))
    if result.outcome in (Outcome.INCOMPATIBLE, Outcome.UNAVAILABLE):
        ctx.client.mark_unready(result.code)
    raise _AttemptFailed(Failure(result.code, result.detail or result.outcome.value, permanent=False))


def _record(claim: Claim, failure: Failure, ctx: IndexContext) -> AttemptResult:
    try:
        status = jobs.record_failure(ctx.session_factory, claim, failure, ctx.settings, ctx.rng)
    except SQLAlchemyError:
        log.error(
            "index_failure_unrecorded job_id=%s code=%s; lease recovery decides", claim.job_id, failure.code, exc_info=True
        )
        return AttemptResult("unrecorded", claim.job_id, failure.code)
    if status is FailureStatus.STALE:
        log.warning("stale_attempt_discarded job_id=%s attempt=%d code=%s", claim.job_id, claim.attempt, failure.code)
        return AttemptResult("stale", claim.job_id, failure.code)
    if status is FailureStatus.REQUEUED:
        log.warning("index_requeued job_id=%s attempt=%d code=%s", claim.job_id, claim.attempt, failure.code)
        return AttemptResult("requeued", claim.job_id, failure.code)
    level = logging.WARNING if failure.permanent else logging.ERROR
    log.log(
        level, "index_failed job_id=%s attempt=%d code=%s detail=%s",
        claim.job_id, claim.attempt, failure.code, failure.detail,
    )  # fmt: skip
    return AttemptResult("failed", claim.job_id, failure.code)
