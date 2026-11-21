"""POST /courses/{id}/study-events: allowlisted, bounded, idempotent client telemetry (docs/study-events.md).

Accepted event types (exact keys; anything else rejects the whole batch with 422):
  document_viewed           client_event_id, type, occurred_at, document_id
  recommendation_shown|opened|dismissed
                            client_event_id, type, occurred_at, recommendation_id (16 hex),
                            recommendation_type, topic_id (or null), target_document_id (or null)
  focus_interval            client_event_id, type, started_at, ended_at, surface
                            (document | quiz | dashboard | chat), topic_id (or null), document_id (or null)
No free text, URL, user agent or browser data is accepted or stored.

Time: RFC 3339 with an explicit offset; every timestamp given (occurred_at, and for a focus
interval started_at AND ended_at independently) must lie within [now - 24 h, now + skew]
in database time (clock_timestamp()); a focus interval must have ended_at > started_at and
last at most STUDY_EVENTS_MAX_INTERVAL_SECONDS. Stored occurred_at of an interval is ended_at.

Identity: (student, client_event_id) is unique; the fingerprint is the SHA-256 of the
canonical event (course included, timestamps normalized to UTC microseconds). The same
id with the same fingerprint is a duplicate (reported, nothing written, no quota used);
the same id with another fingerprint is 409 for the whole batch. Validation is atomic:
nothing of a batch is stored unless all of it is valid.

Quota: per student and UTC day (received_at), across every API process: the transaction
takes a PostgreSQL advisory transaction lock on the student and counts that day's rows
(index study_events (user_id, received_at)) before inserting; over the quota the whole
batch is refused (429). Duplicates never count.

Retention: maintenance deletes rows received more than STUDY_EVENTS_RETENTION_DAYS ago
(>= 2 days, longer than the 24-hour acceptance window, so a replay that is still
acceptable always finds its original). After that, the id could be reused by a client.

These events are untrusted: they never affect grading, mastery, streaks, recommendations
or analytics. A recommendation event is not evidence that the recommendation was issued.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser
from ..config import Settings
from ..errors import ApiError
from ..quiz.access import short_tx
from ..quiz.protocol import canonical_uuid
from . import materials
from .access import require_student, study_events_not_allowed

log = logging.getLogger(__name__)

PAST_WINDOW = timedelta(hours=24)
RECOMMENDATION_TYPES = ("review_topic", "practice_quiz", "class_material", "external_resource")
RECOMMENDATION_EVENTS = ("recommendation_shown", "recommendation_opened", "recommendation_dismissed")
SURFACES = ("document", "quiz", "dashboard", "chat")
KEYS = {
    "document_viewed": frozenset({"client_event_id", "type", "occurred_at", "document_id"}),
    **{
        kind: frozenset({"client_event_id", "type", "occurred_at", "recommendation_id", "recommendation_type",
                         "topic_id", "target_document_id"})  # fmt: skip
        for kind in RECOMMENDATION_EVENTS
    },
    "focus_interval": frozenset({"client_event_id", "type", "started_at", "ended_at", "surface", "topic_id", "document_id"}),
}
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
_REC_ID = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class StudyEvent:
    client_event_id: uuid.UUID
    kind: str
    occurred_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    surface: str | None = None
    topic_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    recommendation_id: str | None = None
    recommendation_type: str | None = None

    def fingerprint(self, course_id: uuid.UUID) -> str:
        def ts(value: datetime | None) -> str | None:
            return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        canonical = {
            "course_id": str(course_id), "type": self.kind, "occurred_at": ts(self.occurred_at),
            "started_at": ts(self.started_at), "ended_at": ts(self.ended_at), "surface": self.surface,
            "topic_id": None if self.topic_id is None else str(self.topic_id),
            "document_id": None if self.document_id is None else str(self.document_id),
            "recommendation_id": self.recommendation_id, "recommendation_type": self.recommendation_type,
        }  # fmt: skip
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _invalid(index: int, message: str) -> ApiError:
    return ApiError(422, "invalid_study_event", f"Event {index}: {message}", extra={"index": index})


def _timestamp(value: object, index: int, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not _RFC3339.match(value):
        raise _invalid(index, f"{field} must be an RFC 3339 time with an explicit offset.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid(index, f"{field} must be an RFC 3339 time with an explicit offset.") from None
    if parsed.utcoffset() is None:
        raise _invalid(index, f"{field} must carry an offset.")
    return parsed.astimezone(timezone.utc)


def _optional_uuid(value: object, index: int, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    parsed = canonical_uuid(value)
    if parsed is None:
        raise _invalid(index, f"{field} must be a lower-case UUID or null.")
    return parsed


def parse_batch(body: dict, settings: Settings) -> list[StudyEvent]:
    if set(body) != {"events"} or not isinstance(body["events"], list):
        raise ApiError(422, "invalid_fields", "The request must contain exactly one field, events (a list).")
    raw = body["events"]
    if not 1 <= len(raw) <= settings.study_events_max_batch:
        raise ApiError(422, "invalid_batch_size", f"Send between 1 and {settings.study_events_max_batch} events.")
    events: list[StudyEvent] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or item["type"] not in KEYS:
            raise _invalid(index, "unknown or missing event type.")
        kind = item["type"]
        if set(item) != KEYS[kind]:
            raise _invalid(index, f"a {kind} event has exactly these fields: {', '.join(sorted(KEYS[kind]))}.")
        event_id = canonical_uuid(item["client_event_id"])
        if event_id is None:
            raise _invalid(index, "client_event_id must be a lower-case UUID.")
        if kind == "document_viewed":
            document = _optional_uuid(item["document_id"], index, "document_id")
            if document is None:
                raise _invalid(index, "document_id is required.")
            events.append(StudyEvent(event_id, kind, _timestamp(item["occurred_at"], index, "occurred_at"),
                                     document_id=document))  # fmt: skip
        elif kind in RECOMMENDATION_EVENTS:
            rec_id, rec_type = item["recommendation_id"], item["recommendation_type"]
            # fullmatch: `$` also matches before a terminal newline (17 characters, VARCHAR(16) column).
            if not isinstance(rec_id, str) or not _REC_ID.fullmatch(rec_id):
                raise _invalid(index, "recommendation_id must be 16 lower-case hex characters.")
            if rec_type not in RECOMMENDATION_TYPES:
                raise _invalid(index, "unknown recommendation_type.")
            events.append(StudyEvent(
                event_id, kind, _timestamp(item["occurred_at"], index, "occurred_at"),
                topic_id=_optional_uuid(item["topic_id"], index, "topic_id"),
                document_id=_optional_uuid(item["target_document_id"], index, "target_document_id"),
                recommendation_id=rec_id, recommendation_type=rec_type,
            ))  # fmt: skip
        else:
            started = _timestamp(item["started_at"], index, "started_at")
            ended = _timestamp(item["ended_at"], index, "ended_at")
            if item["surface"] not in SURFACES:
                raise _invalid(index, "surface must be document, quiz, dashboard or chat.")
            if not ended > started:
                raise _invalid(index, "ended_at must be after started_at.")
            if ended - started > timedelta(seconds=settings.study_events_max_interval_seconds):
                raise _invalid(index, f"a focus interval lasts at most {settings.study_events_max_interval_seconds} s.")
            events.append(StudyEvent(
                event_id, kind, ended, started_at=started, ended_at=ended, surface=item["surface"],
                topic_id=_optional_uuid(item["topic_id"], index, "topic_id"),
                document_id=_optional_uuid(item["document_id"], index, "document_id"),
            ))  # fmt: skip
    ids = [e.client_event_id for e in events]
    if len(set(ids)) != len(ids):
        raise ApiError(422, "duplicate_event_ids", "Each event in a batch needs its own client_event_id.")
    return events


def _check_times(events: list[StudyEvent], now: datetime, settings: Settings) -> None:
    low, high = now - PAST_WINDOW, now + timedelta(seconds=settings.study_events_future_skew_seconds)
    for index, event in enumerate(events):
        for field, value in (("occurred_at", event.occurred_at), ("started_at", event.started_at),
                             ("ended_at", event.ended_at)):  # fmt: skip
            if value is not None and not low <= value <= high:
                raise _invalid(index, f"{field} must lie within the last 24 hours (and not in the future).")


def _check_references(session: Session, course_id: uuid.UUID, events: list[tuple[int, StudyEvent]]) -> None:
    topics = {e.topic_id for _, e in events if e.topic_id is not None}
    if topics:
        found = set(
            session.execute(
                text("SELECT id FROM topics WHERE course_id = :c AND id = ANY(:ids)"), {"c": course_id, "ids": list(topics)}
            ).scalars()
        )
        for index, event in events:
            if event.topic_id is not None and event.topic_id not in found:
                raise _invalid(index, "topic_id is not a topic of this course.")
    checked: dict[uuid.UUID, bool] = {}
    for index, event in events:
        if event.document_id is None:
            continue
        if event.document_id not in checked:
            checked[event.document_id] = materials.visible_document(session, course_id, event.document_id)
        if not checked[event.document_id]:
            raise _invalid(index, "the document is not available in this course.")


def record(
    factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID, events: list[StudyEvent], settings: Settings
) -> dict:
    with short_tx(factory) as session:
        require_student(session, user, course_id, study_events_not_allowed, lock=True)
        now: datetime = session.execute(text("SELECT clock_timestamp()")).scalar_one()
        _check_times(events, now, settings)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('studygraph.study_events:' || CAST(:u AS text), 0))"),
            {"u": user.id},
        )
        existing = dict(
            session.execute(
                text("SELECT client_event_id, fingerprint FROM study_events WHERE user_id = :u AND client_event_id = ANY(:ids)"),
                {"u": user.id, "ids": [e.client_event_id for e in events]},
            ).all()
        )
        fresh: list[tuple[int, StudyEvent, str]] = []
        results = []
        for index, event in enumerate(events):
            fingerprint = event.fingerprint(course_id)
            stored = existing.get(event.client_event_id)
            if stored is None:
                fresh.append((index, event, fingerprint))
                results.append({"client_event_id": str(event.client_event_id), "status": "accepted"})
            elif stored == fingerprint:
                results.append({"client_event_id": str(event.client_event_id), "status": "duplicate"})
            else:
                raise ApiError(
                    409, "event_id_conflict", "A client_event_id was already used for a different event.",
                    extra={"index": index},
                )  # fmt: skip
        _check_references(session, course_id, [(i, e) for i, e, _ in fresh])
        if fresh:
            used = session.execute(
                text(
                    "SELECT count(*) FROM study_events WHERE user_id = :u "
                    "AND received_at >= (date_trunc('day', CAST(:now AS timestamptz) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
                ),
                {"u": user.id, "now": now},
            ).scalar_one()
            if used + len(fresh) > settings.study_events_daily_quota:
                raise ApiError(429, "study_event_quota_exceeded", "Too many study events today. Try again tomorrow.")
            for _, event, fingerprint in fresh:
                session.execute(
                    text(
                        "INSERT INTO study_events (user_id, course_id, client_event_id, fingerprint, event_type, "
                        "occurred_at, started_at, ended_at, surface, topic_id, document_id, recommendation_id, "
                        "recommendation_type, received_at) VALUES (:u, :c, :id, :fp, :kind, :occurred, :started, :ended, "
                        ":surface, :topic, :doc, :rec, :rec_type, :now)"
                    ),
                    {"u": user.id, "c": course_id, "id": event.client_event_id, "fp": fingerprint, "kind": event.kind,
                     "occurred": event.occurred_at, "started": event.started_at, "ended": event.ended_at,
                     "surface": event.surface, "topic": event.topic_id, "doc": event.document_id,
                     "rec": event.recommendation_id, "rec_type": event.recommendation_type, "now": now},
                )  # fmt: skip
        session.commit()
    accepted = len(fresh)
    return {"accepted": accepted, "duplicates": len(events) - accepted, "results": results,
            "measurement": "client_reported"}  # fmt: skip


def authorize(factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID) -> None:
    with short_tx(factory, read_only=True) as session:
        require_student(session, user, course_id, study_events_not_allowed)


def cleanup(session_factory: sessionmaker[Session], settings: Settings) -> int:
    """Delete events past their retention, a bounded batch per tick (runs whatever STUDY_EVENTS_ENABLED says)."""
    with session_factory() as session:
        deleted = session.execute(
            text(
                "DELETE FROM study_events WHERE id IN (SELECT id FROM study_events "
                "WHERE received_at < now() - :days * interval '1 day' ORDER BY received_at, id LIMIT :limit "
                "FOR UPDATE SKIP LOCKED)"
            ),
            {"days": settings.study_events_retention_days, "limit": settings.study_events_cleanup_batch},
        ).rowcount
        session.commit()
    return deleted
