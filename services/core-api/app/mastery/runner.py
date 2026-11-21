"""One delivery attempt of one stream (the mastery task's body) and the mastery maintenance tick.

deliver_stream():
  1. MASTERY_DELIVERY_ENABLED and complete pins, else nothing (no claim, no request);
  2. capability probe (cached per process, outside any transaction), recorded in
     mastery_model_state with a database-time fence; a mismatch blocks the model, an
     unavailable service is left alone (nothing is claimed);
  3. claim (app/mastery/delivery.py): the stream's immutable batch, created first if needed;
  4. access re-check in a fresh transaction right before sending: revoked -> paused, nothing sent;
  5. one POST, no retry here; then strict validation and one persistence transaction. A response
     that names another protocol, model, version or state epoch than the probe confirmed (or a
     mock) suspends the model globally (sticky, operator reconciliation) and keeps the batch;
     any other invalid response is a retried failure of this batch only.
Every outcome is logged with fixed codes and the batch id only: never a user or course id,
pseudonym, key, request or response body, or exception message (SQLAlchemy errors carry
their parameters; app/quiz/diagnostics.py).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..ingestion.dispatch import PublishError
from ..quiz.diagnostics import describe
from . import delivery
from .client import AiMasteryClient, MasteryUpstreamError, encode_request
from .pins import pins_of
from .protocol import ProtocolViolation, StateMismatch, build_request, validate_update

log = logging.getLogger(__name__)

# validate_update() reasons that mean the AI service explicitly changed identity after a compatible probe: a
# well-formed but different protocol, model name/version or state epoch, or a literal mock: true. A model
# problem, handled globally. Every other violation, including a malformed identity field (*_malformed: null,
# wrong type, missing key, bad syntax, non-boolean mock), establishes no identity: an ordinary failure of this
# batch, retried.
IDENTITY_VIOLATIONS = frozenset({"protocol_mismatch", "model_mismatch", "state_epoch_mismatch", "mock_response"})


@dataclass
class MasteryContext:
    session_factory: sessionmaker[Session]
    settings: Settings
    client: AiMasteryClient


def deliver_stream(user_id: uuid.UUID, course_id: uuid.UUID, ctx: MasteryContext) -> str:
    settings = ctx.settings
    if not settings.mastery_delivery_enabled:
        log.info("mastery_delivery_skipped reason=disabled")
        return "disabled"
    pins = pins_of(settings)
    if pins is None or not ctx.client.configured:
        log.error("mastery_delivery_skipped reason=not_configured")
        return "not_configured"
    if delivery.model_blocked(ctx.session_factory):
        return "model_blocked"

    started = delivery.probe_started_at(ctx.session_factory)
    caps = ctx.client.probe_sync(pins)
    delivery.record_probe(ctx.session_factory, started, caps.status, caps.reason, pins, caps.service)
    if caps.status != "compatible":
        return f"probe_{caps.status}"

    max_events = min(settings.mastery_max_batch_events, caps.max_events)
    claim = delivery.claim(ctx.session_factory, user_id, course_id, settings, pins, max_events)
    if isinstance(claim, delivery.NoClaim):
        return claim.reason

    with ctx.session_factory() as session:
        allowed = delivery.has_access(session, user_id, course_id)
        session.rollback()
    if not allowed:
        return delivery.pause(ctx.session_factory, claim)

    request = build_request(claim.batch_id, user_id, course_id, list(claim.events), pins)
    body = encode_request(request)
    if len(body) > settings.mastery_max_request_bytes:
        return delivery.block(ctx.session_factory, claim, "request_too_large")

    try:
        response = ctx.client.update_sync(body)
    except MasteryUpstreamError as exc:
        return delivery.fail(ctx.session_factory, claim, settings, f"upstream_{exc.code}")

    try:
        update = validate_update(response, request, list(claim.events), pins)
    except StateMismatch:
        # The AI service has fewer events than were delivered: its learner state is gone. Nothing is
        # stored; the model is blocked for every stream and predictions are hidden until reconciliation.
        return delivery.block(ctx.session_factory, claim, "ai_state_mismatch", model_reason="ai_state_mismatch")
    except ProtocolViolation as exc:
        if exc.reason in IDENTITY_VIOLATIONS:
            return delivery.suspend_for_model(ctx.session_factory, claim, f"response_{exc.reason}")
        return delivery.fail(ctx.session_factory, claim, settings, f"protocol_{exc.reason}")

    try:
        return delivery.complete(ctx.session_factory, claim, update, pins)
    except ProtocolViolation as exc:
        return delivery.fail(ctx.session_factory, claim, settings, f"protocol_{exc.reason}")
    except Exception as exc:  # noqa: BLE001 - commit uncertainty: the lease decides (same batch resent)
        log.error("mastery_completion_uncertain batch_id=%s %s", claim.batch_id, describe(exc))
        return "uncertain"


# --- maintenance tick (maintenance worker, Beat-driven; database work only) -----------------------------


class MasteryPublisher(Protocol):
    def publish_mastery(self, user_id: uuid.UUID, course_id: uuid.UUID) -> None: ...
    def mastery_queue_depth(self) -> int: ...


@dataclass(frozen=True)
class MaintenanceResult:
    requeued: int
    blocked: int
    resumed: int
    published: int
    released: int
    skipped: str | None


def dispatch(session_factory: sessionmaker[Session], publisher: MasteryPublisher, settings: Settings) -> tuple[int, int, str | None]:
    if not settings.mastery_delivery_enabled:
        return 0, 0, "delivery_disabled"
    if pins_of(settings) is None:
        return 0, 0, "not_configured"
    if delivery.model_blocked(session_factory):
        return 0, 0, "model_blocked"
    try:
        depth = publisher.mastery_queue_depth()
    except PublishError as exc:
        log.warning("mastery_dispatch_skipped reason=broker_unavailable error=%s", exc)
        return 0, 0, "broker_unavailable"
    room = settings.mastery_dispatch_max_queue_depth - depth
    if room <= 0:
        return 0, 0, "queue_full"
    with session_factory() as session:
        streams, reserved_at = delivery.reserve(session, settings, min(settings.mastery_dispatch_batch_size, room))
        session.commit()
    published = 0
    for index, (user_id, course_id) in enumerate(streams):
        try:
            publisher.publish_mastery(user_id, course_id)
        except PublishError as exc:
            released = delivery.release_reservations(session_factory, streams[index:], reserved_at)
            log.warning("mastery_dispatch_interrupted published=%d released=%d error=%s", published, released, exc)
            return published, released, None
        published += 1
    return published, 0, None


def maintain(session_factory: sessionmaker[Session], publisher: MasteryPublisher, settings: Settings) -> MaintenanceResult:
    requeued, blocked = delivery.recover(session_factory, settings, settings.recovery_batch_size)
    resumed = delivery.resume_paused(session_factory, settings.recovery_batch_size)
    published, released, skipped = dispatch(session_factory, publisher, settings)
    return MaintenanceResult(requeued, blocked, resumed, published, released, skipped)
