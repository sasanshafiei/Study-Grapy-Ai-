"""Idempotent creates (Idempotency-Key, table exam_idempotency_keys), contract §1.6.

The key row is written in the same transaction as the resource it created. A replay with the same
fingerprint (course, operation and canonical body) returns the resource's current representation; a
different fingerprint is 409 idempotency_key_reused. After an initial miss, creates recheck the key
under the course capacity lock. The per-user primary key also handles concurrent uses of a key across
different capacity locks.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..errors import ApiError
from .http import sha256_hex


class KeyTaken(Exception):
    """Another transaction committed this key first; roll back and replay."""


def fingerprint(operation: str, course_id: uuid.UUID, body: object) -> str:
    return sha256_hex({"operation": operation, "course_id": str(course_id), "body": body})


def lookup(session: Session, user_id: uuid.UUID, key: uuid.UUID, operation: str, print_: str):
    row = session.execute(
        text("SELECT operation, request_fingerprint, course_id, resource_id FROM exam_idempotency_keys "
             "WHERE user_id = :u AND idempotency_key = :k"),  # fmt: skip
        {"u": user_id, "k": key},
    ).first()
    if row is None:
        return None
    if row.operation != operation or row.request_fingerprint != print_:
        raise ApiError(409, "idempotency_key_reused", "This Idempotency-Key was already used for a different request.")
    return row.resource_id


def lock_capacity_for_create(session: Session, course_id: uuid.UUID, user_id: uuid.UUID, key: uuid.UUID,
                             operation: str, print_: str) -> None:
    scope = "exam-bank-limit" if operation == "question_create" else "exam-limit"
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{scope}:{course_id}"})
    # A separate READ COMMITTED statement sees the creator that committed while we waited.
    if lookup(session, user_id, key, operation, print_) is not None:
        raise KeyTaken()


def store(session: Session, user_id, key, operation, print_, course_id, resource_id) -> None:
    inserted = session.execute(
        text("INSERT INTO exam_idempotency_keys (user_id, idempotency_key, operation, request_fingerprint, course_id, "
             "resource_id) VALUES (:u, :k, :op, :f, :c, :r) ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING 1"),  # fmt: skip
        {"u": user_id, "k": key, "op": operation, "f": print_, "c": course_id, "r": resource_id},
    ).first()
    if inserted is None:
        raise KeyTaken()
