"""Append-only exam audit events (exam_audit_events): ids, hashes and counts only, never content.

Written in the same transaction as the change they describe, so an event exists exactly when the change
committed; transition replays (contract §1.7) rely on that. `details` is bounded (<= 4 KiB, also a CHECK).
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

MAX_DETAILS_BYTES = 4096


def record(session: Session, *, course_id: uuid.UUID, actor_id: uuid.UUID, entity_kind: str, entity_id: uuid.UUID,
           action: str, entity_version: int | None, details: dict | None = None) -> None:  # fmt: skip
    payload = json.dumps(details or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_DETAILS_BYTES:
        raise ValueError("audit details too large")  # a defect: the caller bounds what it records
    session.execute(
        # clock_timestamp(): several events of one transaction keep their order in the history.
        text("INSERT INTO exam_audit_events (course_id, actor_id, entity_kind, entity_id, action, entity_version, details, "
             "created_at) VALUES (:c, :a, :k, :e, :act, :v, CAST(:d AS jsonb), clock_timestamp())"),  # fmt: skip
        {"c": course_id, "a": actor_id, "k": entity_kind, "e": entity_id, "act": action, "v": entity_version, "d": payload},
    )


def transition_event(session: Session, entity_id: uuid.UUID, action: str, entity_version: int):
    """The event of this transition that produced `entity_version`, or None."""
    return session.execute(
        text("SELECT details FROM exam_audit_events WHERE entity_id = :e AND action = :a AND entity_version = :v "
             "ORDER BY created_at LIMIT 1"),  # fmt: skip
        {"e": entity_id, "a": action, "v": entity_version},
    ).first()
