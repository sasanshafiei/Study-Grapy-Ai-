"""One index attempt, from the pre-claim gate to the confirmed completion.

    indexing enabled? -> AI service ready and compatible? (cached probe)   } nothing claimed,
                                                                         } no attempt consumed
    -> claim (short transaction, committed; attempt_count + 1, never decremented)
    -> build the canonical request from the stored metadata and sections
    -> POST it (bounded HTTP client) with its content-based idempotency key
    -> confirmed 200 only -> complete (one transaction, fenced by the token,
       current content digest, committed generation and chunk evidence verified) -> document indexed

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

External (harvested, section-based) documents (M6) pass one more pre-claim
gate: the service must have advertised the "section" locator scheme, the
document's source must be enabled and its course opted in. Otherwise the job
is deferred (available_at moved forward by EXTERNAL_INDEX_DEFER_SECONDS,
dispatch reservation released) without being claimed: no attempt is
consumed, and since the dispatch sweep takes jobs in available_at order, a
deferred external job never holds back page-based (PDF) jobs.

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
from sqlalchemy import text
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
        deferred = _defer_unsupported_external(job_id, ctx, readiness)
    except SQLAlchemyError:
        log.warning("index_not_claimed job_id=%s reason=database", job_id, exc_info=True)
        return AttemptResult("database_unavailable", job_id)
    if deferred is not None:
        log.info("index_not_claimed job_id=%s reason=%s action=deferred", job_id, deferred)
        return AttemptResult("deferred", job_id, deferred)
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
        status = _attempt(claim, ctx, ctx.client.max_request_bytes(readiness), readiness.locators)
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
    if status is CompletionStatus.SUPERSEDED:
        log.warning("index_failed job_id=%s code=index_superseded", job_id)
        return AttemptResult("failed", job_id, "index_superseded")
    if status is CompletionStatus.EXTERNAL_INACTIVE:
        log.warning("index_failed job_id=%s code=external_inactive", job_id)
        return AttemptResult("failed", job_id, "external_inactive")
    log.warning("index_failed job_id=%s code=document_not_pending", job_id)
    return AttemptResult("failed", job_id, "document_not_pending")


_EXTERNAL_GATE = text(
    """
    SELECT d.external_resource_id, s.enabled AS source_enabled, g.enabled AS course_enabled
    FROM ingestion_jobs j JOIN documents d ON d.id = j.document_id
    LEFT JOIN sources s ON s.id = d.source_id
    LEFT JOIN harvest_course_settings g ON g.course_id = d.course_id
    WHERE j.id = :job AND j.kind = 'index' AND j.status = 'queued'
    """
)


def _defer_unsupported_external(job_id: uuid.UUID, ctx: IndexContext, readiness) -> str | None:
    """Defer (never claim) a queued external job that cannot be sent now; the reason, or None."""
    with ctx.session_factory() as session:
        row = session.execute(_EXTERNAL_GATE, {"job": job_id}).one_or_none()
        if row is None or row.external_resource_id is None:
            session.rollback()
            return None  # not an external job (or not claimable anyway): the M4 path, unchanged
        if "section" not in readiness.locators:
            reason = "section_locators_unsupported"
        elif row.source_enabled is not True:
            reason = "source_disabled"
        elif row.course_enabled is not True:
            reason = "course_not_opted_in"
        else:
            session.rollback()
            return None
        session.execute(
            text(
                "UPDATE ingestion_jobs SET available_at = greatest(available_at, now() + :delay * interval '1 second'), "
                "last_dispatched_at = NULL, updated_at = now() "
                "WHERE id = :job AND kind = 'index' AND status = 'queued'"
            ),
            {"job": job_id, "delay": ctx.settings.external_index_defer_seconds},
        )
        session.commit()
    return reason


def _attempt(claim: Claim, ctx: IndexContext, max_bytes: int, locators=frozenset({"page"})) -> CompletionStatus:
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
    if request.locator_scheme not in locators:
        # Belt and braces after the claim (the pre-claim gate already deferred such
        # jobs): a document is never sent in a scheme the service did not advertise.
        raise _AttemptFailed(Failure("index_locators_unsupported", request.locator_scheme, permanent=False))
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
        status = jobs.complete_index(ctx.session_factory, claim, request.content_digest, result.chunk_count)
        if status is CompletionStatus.ROUTING_CHANGED:
            raise _AttemptFailed(Failure(status.value, "document routing changed during completion", permanent=False))
        if status is CompletionStatus.PROOF_MISSING:
            raise _AttemptFailed(Failure("index_proof_missing", "committed generation does not match receipt", permanent=False))
        return status
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
