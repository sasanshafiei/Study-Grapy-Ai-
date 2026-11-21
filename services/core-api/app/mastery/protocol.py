"""studygraph.mastery/1: request building and strict, all-or-nothing response validation (no I/O).

PROPOSAL until AI-2 and CE approve it (docs/ai-mastery-integration.md); the shared
OpenAPI contract is not changed by M8.

Request (one student, one course, one immutable batch): pseudonymous learner and course
references, the batch id as request id, the pinned state epoch, and per event only the
attempt id (event id), topic id, the per-topic sequence number, question type, the
generator's difficulty label, correctness, and two untrusted telemetry values. Never a
user id, name, e-mail, answer, answer key, question text or course passage.

Response validation (any violation rejects the whole response; nothing is repaired or stored).
Identity fields are classified strictly: malformed values are `*_malformed` (an ordinary
per-batch failure), well-formed but different protocol, state epoch or model are `*_mismatch`,
and a literal `mock: true` is `mock_response` (both: identity change, global suspension).
exact key sets and types (a bool is never a number), echoed protocol / request id / state
epoch / learner and course references, mock literally false, the pinned model identity,
applied and duplicate event ids disjoint, without repeats, and together exactly the batch;
exactly one score per batch topic with mastery finite in [0, 1], through_seq equal to the
batch's last sequence number of that topic and attempts_seen equal to it. attempts_seen
below it means the AI service lost state (StateMismatch: delivery is blocked, nothing is
stored); above it is a protocol violation. next_review_at is null or RFC 3339 with an
explicit offset, bounded against database time at persistence.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .pins import CANONICAL_UUID, MODEL_FIELD, PROTOCOL, Pins

SERVICE = "ai-service"
CAPABILITIES_PATH = "/ai/v1/mastery/capabilities"
UPDATE_PATH = "/ai/v1/mastery/update"
ORDERING = "per_topic"
MAX_EVENTS_LIMIT = 1000
MAX_TIMESTAMP_CHARS = 40
REVIEW_PAST = timedelta(days=1)
REVIEW_FUTURE = timedelta(days=365)

CAPABILITY_KEYS = frozenset({"protocol", "service", "mock", "model", "state_epoch", "max_events", "ordering"})
MODEL_KEYS = frozenset({"name", "version"})
RESPONSE_KEYS = frozenset(
    {"protocol", "request_id", "state_epoch", "course_ref", "learner_ref", "mock", "model", "applied_event_ids",
     "duplicate_event_ids", "scores"}
)  # fmt: skip
SCORE_KEYS = frozenset({"topic_id", "mastery", "attempts_seen", "through_seq", "next_review_at"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL = re.compile(r"^studygraph\.[a-z][a-z0-9_]{0,30}/[1-9][0-9]{0,3}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


class ProtocolViolation(Exception):
    """The response does not follow the protocol; `reason` is a fixed code, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StateMismatch(Exception):
    """attempts_seen is below the events this core-api has delivered: the AI service lost learner state."""


@dataclass(frozen=True)
class Capabilities:
    status: str  # compatible | incompatible | unavailable
    reason: str | None
    service: str | None = None
    max_events: int = 0


@dataclass(frozen=True)
class BatchEvent:
    event_id: uuid.UUID
    topic_id: uuid.UUID
    topic_seq: int
    qtype: str
    difficulty: str
    correct: bool
    hint_used: int
    response_ms: int | None
    submitted_at: datetime


@dataclass(frozen=True)
class ValidatedScore:
    topic_id: uuid.UUID
    mastery: float
    through_seq: int
    next_review_at: datetime | None


@dataclass(frozen=True)
class ValidatedUpdate:
    applied: frozenset[uuid.UUID]
    duplicate: frozenset[uuid.UUID]
    scores: tuple[ValidatedScore, ...]


def _is_int(value: object) -> bool:
    return type(value) is int


def _uuid(value: object, reason: str) -> uuid.UUID:
    # fullmatch: `$` also matches before a terminal newline, and uuid.UUID() would then raise ValueError.
    if not isinstance(value, str) or not CANONICAL_UUID.fullmatch(value):
        raise ProtocolViolation(reason)
    return uuid.UUID(value)


def _model_matches(model: object, pins: Pins) -> bool:
    return (
        isinstance(model, dict)
        and set(model) == MODEL_KEYS
        and isinstance(model["name"], str)
        and isinstance(model["version"], str)
        and MODEL_FIELD.match(model["name"]) is not None
        and MODEL_FIELD.match(model["version"]) is not None
        and model["name"] == pins.model_name
        and model["version"] == pins.model_version
    )


def validate_capabilities(status: int, body: object, pins: Pins) -> Capabilities:
    """compatible only for the exact pinned identity of a non-mock service; transport problems are 'unavailable'."""
    if status != 200 or not isinstance(body, dict):
        return Capabilities("unavailable", f"probe_http_{status}" if status != 200 else "probe_invalid_body")
    if set(body) != CAPABILITY_KEYS:
        return Capabilities("incompatible", "capabilities_shape")
    if body["protocol"] != PROTOCOL:
        return Capabilities("incompatible", "protocol_mismatch")
    if body["mock"] is not False:  # literally false: a missing, null, "false" or 0 mock flag is refused
        return Capabilities("incompatible", "mock_service")
    if not isinstance(body["service"], str) or not re.match(r"^[a-z][a-z0-9-]{0,39}$", body["service"]):
        return Capabilities("incompatible", "capabilities_shape")
    if not _model_matches(body["model"], pins):
        return Capabilities("incompatible", "model_mismatch")
    if not isinstance(body["state_epoch"], str) or body["state_epoch"] != str(pins.state_epoch):
        return Capabilities("incompatible", "epoch_mismatch")
    if body["ordering"] != ORDERING:
        return Capabilities("incompatible", "ordering_unsupported")
    if not _is_int(body["max_events"]) or not 1 <= body["max_events"] <= MAX_EVENTS_LIMIT:
        return Capabilities("incompatible", "capabilities_shape")
    return Capabilities("compatible", None, body["service"], body["max_events"])


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ordered_events(events: list[BatchEvent]) -> list[BatchEvent]:
    """Approximate time order across topics, exact topic_seq order within each topic (deterministic).

    The list is first sorted by (submitted_at, event_id); then each topic's events are put
    back, in topic_seq order, into the positions that topic occupies. submitted_at is the
    attempt transaction's start time, which can invert two attempts of one topic, never
    their topic_seq order.
    """
    by_time = sorted(events, key=lambda e: (e.submitted_at, str(e.event_id)))
    per_topic: dict[uuid.UUID, list[BatchEvent]] = {}
    for event in sorted(events, key=lambda e: (str(e.topic_id), e.topic_seq)):
        per_topic.setdefault(event.topic_id, []).append(event)
    cursor = {topic: 0 for topic in per_topic}
    ordered = []
    for slot in by_time:
        topic = slot.topic_id
        ordered.append(per_topic[topic][cursor[topic]])
        cursor[topic] += 1
    return ordered


def build_request(
    batch_id: uuid.UUID, user_id: uuid.UUID, course_id: uuid.UUID, events: list[BatchEvent], pins: Pins
) -> dict:
    return {
        "protocol": PROTOCOL,
        "request_id": str(batch_id),
        "state_epoch": str(pins.state_epoch),
        "pseudonym_namespace": pins.namespace,
        "course_ref": pins.course_ref(course_id),
        "learner_ref": pins.learner_ref(user_id, course_id),
        "events": [
            {
                "event_id": str(e.event_id),
                "topic_id": str(e.topic_id),
                "topic_seq": e.topic_seq,
                "qtype": e.qtype,
                "difficulty": e.difficulty,
                "correct": bool(e.correct),
                "hint_used": 1 if e.hint_used else 0,
                "response_ms": e.response_ms,
                "telemetry_trusted": False,
                "submitted_at": _timestamp(e.submitted_at),
            }
            for e in ordered_events(events)
        ],
    }


def parse_review_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_CHARS or not _RFC3339.match(value):
        raise ProtocolViolation("review_time_format")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProtocolViolation("review_time_format") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolViolation("review_time_format")
    return parsed.astimezone(timezone.utc)


def check_review_bounds(scores: tuple[ValidatedScore, ...], now: datetime) -> None:
    """Called with database time inside the persistence transaction."""
    for score in scores:
        if score.next_review_at is not None and not (now - REVIEW_PAST <= score.next_review_at <= now + REVIEW_FUTURE):
            raise ProtocolViolation("review_time_out_of_range")


def validate_update(
    body: object, request: dict, events: list[BatchEvent], pins: Pins
) -> ValidatedUpdate:
    if not isinstance(body, dict) or set(body) != RESPONSE_KEYS:
        raise ProtocolViolation("response_shape")
    # Identity fields. A malformed value (wrong type, shape or syntax, null, a non-boolean mock) is an ordinary
    # malformed response (*_malformed, retried per batch): it establishes no identity. Only a WELL-FORMED value
    # that explicitly differs, or a literal mock: true, is an identity change (*_mismatch, mock_response), which
    # the runner treats as a sticky global incompatibility (app/mastery/runner.py, IDENTITY_VIOLATIONS).
    # fullmatch, not match: `$` also matches before a terminal newline, so "studygraph.mastery/2\n" would
    # otherwise pass as well-formed and be misread as a deliberate identity change.
    if not isinstance(body["protocol"], str) or not _PROTOCOL.fullmatch(body["protocol"]):
        raise ProtocolViolation("protocol_malformed")
    if not isinstance(body["state_epoch"], str) or not CANONICAL_UUID.fullmatch(body["state_epoch"]):
        raise ProtocolViolation("state_epoch_malformed")
    if type(body["mock"]) is not bool:
        raise ProtocolViolation("mock_malformed")
    model = body["model"]
    if not (
        isinstance(model, dict) and set(model) == MODEL_KEYS
        and all(isinstance(model[k], str) and MODEL_FIELD.fullmatch(model[k]) for k in MODEL_KEYS)
    ):  # fmt: skip
        raise ProtocolViolation("model_malformed")
    if body["protocol"] != request["protocol"]:
        raise ProtocolViolation("protocol_mismatch")
    if body["state_epoch"] != request["state_epoch"]:
        raise ProtocolViolation("state_epoch_mismatch")
    if body["mock"] is True:
        raise ProtocolViolation("mock_response")
    if not _model_matches(model, pins):
        raise ProtocolViolation("model_mismatch")
    for key in ("request_id", "course_ref", "learner_ref"):
        if not isinstance(body[key], str) or body[key] != request[key]:
            raise ProtocolViolation(f"{key}_mismatch")
    if not _HEX64.match(body["learner_ref"]) or not _HEX64.match(body["course_ref"]):
        raise ProtocolViolation("reference_shape")

    expected = {e.event_id for e in events}
    accounted: list[uuid.UUID] = []
    lists = []
    for key in ("applied_event_ids", "duplicate_event_ids"):
        raw = body[key]
        if not isinstance(raw, list) or len(raw) > len(events):
            raise ProtocolViolation("event_accounting")
        ids = [_uuid(v, "event_accounting") for v in raw]
        lists.append(frozenset(ids))
        accounted.extend(ids)
    if len(accounted) != len(set(accounted)) or set(accounted) != expected:
        raise ProtocolViolation("event_accounting")  # repeats, overlap, missing or foreign events

    last_seq: dict[uuid.UUID, int] = {}
    for event in events:
        last_seq[event.topic_id] = max(last_seq.get(event.topic_id, 0), event.topic_seq)
    raw_scores = body["scores"]
    if not isinstance(raw_scores, list) or len(raw_scores) != len(last_seq):
        raise ProtocolViolation("score_topics")
    scores: list[ValidatedScore] = []
    seen: set[uuid.UUID] = set()
    lost_state = False
    for item in raw_scores:
        if not isinstance(item, dict) or set(item) != SCORE_KEYS:
            raise ProtocolViolation("score_shape")
        topic = _uuid(item["topic_id"], "score_topics")
        if topic not in last_seq or topic in seen:
            raise ProtocolViolation("score_topics")
        seen.add(topic)
        mastery = item["mastery"]
        if type(mastery) not in (int, float) or not math.isfinite(mastery) or not 0.0 <= mastery <= 1.0:
            raise ProtocolViolation("mastery_range")
        through, attempts_seen = item["through_seq"], item["attempts_seen"]
        if not _is_int(through) or not _is_int(attempts_seen) or attempts_seen < 0:
            raise ProtocolViolation("score_sequence")
        if through != last_seq[topic]:
            raise ProtocolViolation("score_sequence")
        if attempts_seen > through:
            raise ProtocolViolation("score_sequence")
        if attempts_seen < through:
            lost_state = True
        scores.append(ValidatedScore(topic, float(mastery), through, parse_review_time(item["next_review_at"])))
    if lost_state:
        raise StateMismatch()
    return ValidatedUpdate(lists[0], lists[1], tuple(sorted(scores, key=lambda s: str(s.topic_id))))
