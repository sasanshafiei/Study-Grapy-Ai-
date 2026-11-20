"""Courses, topics and enrollment (M5).

Every decision is made from the users, courses and enrollments rows read in
the same transaction, never from the principal's role alone (a role change
or a disabled account takes effect at once):

- list: an active professor sees the courses they own; an active student the
  courses they hold a `student` enrollment in; any other role sees none
  (admins get no implicit course access, as in app/services/course_access.py).
- topics: the course readers of course_access.require_read.
- create: active professors only. The new course gets a random join code.
- join: active students only, with the code of a course whose owner is an
  active professor. Joining twice is idempotent; an existing enrollment with
  another role is a conflict, never silently accepted or changed.

Join codes: JOIN_CODE_LENGTH characters from an alphabet without look-alikes
(no 0/O, 1/I/L), drawn with `secrets`; about 8.5e11 combinations. Creation
retries a bounded number of times on a collision (exact or case-insensitive)
and fails cleanly after that. Lookups prefer the code exactly as typed (legacy
codes may be lower case), then its upper-case form without spaces or hyphens.

student_count counts enrollments with role `student` whose user is an active
student, computed in the same statement as the course list (no N+1). Lists
are complete: there is no silent cap (ordering: name, then id).
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import CurrentUser, not_authenticated
from ..errors import ApiError
from .course_access import (
    ACTIVE,
    PROFESSOR,
    STUDENT,
    account_disabled,
    require_active,
    require_read,
)

JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 8
JOIN_CODE_ATTEMPTS = 5
NAME_MAX_CHARS = 150
CODE_MAX_CHARS = 32
JOIN_CODE_MAX_CHARS = 12  # courses.join_code is VARCHAR(12)

_JOIN_SEPARATORS = re.compile(r"[\s-]+")

# Active students enrolled as students, per course (a correlated subquery: one statement).
_STUDENT_COUNT = (
    "(SELECT count(*) FROM enrollments se JOIN users su ON su.id = se.user_id "
    "WHERE se.course_id = c.id AND se.role = 'student' AND su.role = 'student' AND su.status = 'active')"
)


@dataclass(frozen=True)
class Account:
    role: str
    status: str
    display_name: str


def load_account(session: Session, user: CurrentUser, *, lock: bool = False) -> Account:
    """The caller's users row (FOR SHARE when lock): 401 if gone, 403 if disabled."""
    require_active(user)
    sql = "SELECT role, status, display_name FROM users WHERE id = :id"
    row = session.execute(text(sql + (" FOR SHARE" if lock else "")), {"id": user.id}).one_or_none()
    if row is None:
        raise not_authenticated()
    if row.status != ACTIVE:
        raise account_disabled()
    return Account(role=row.role, status=row.status, display_name=row.display_name)


def _course_item(row, *, show_join_code: bool) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "professor_name": row.professor_name,
        "join_code": row.join_code if show_join_code else "",
        "student_count": int(row.student_count),
    }


def list_courses(session: Session, user: CurrentUser) -> list[dict]:
    account = load_account(session, user)
    if account.role == PROFESSOR:
        rows = session.execute(
            text(
                f"SELECT c.id, c.code, c.name, c.join_code, o.display_name AS professor_name, "
                f"{_STUDENT_COUNT} AS student_count "
                "FROM courses c JOIN users o ON o.id = c.owner_id "
                "WHERE c.owner_id = :me ORDER BY c.name, c.id"
            ),
            {"me": user.id},
        ).all()
        return [_course_item(r, show_join_code=True) for r in rows]
    if account.role == STUDENT:
        rows = session.execute(
            text(
                f"SELECT c.id, c.code, c.name, c.join_code, o.display_name AS professor_name, "
                f"{_STUDENT_COUNT} AS student_count "
                "FROM enrollments me JOIN courses c ON c.id = me.course_id JOIN users o ON o.id = c.owner_id "
                "WHERE me.user_id = :me AND me.role = 'student' ORDER BY c.name, c.id"
            ),
            {"me": user.id},
        ).all()
        return [_course_item(r, show_join_code=False) for r in rows]
    return []


def list_topics(session: Session, user: CurrentUser, course_id: uuid.UUID) -> list[dict]:
    require_read(session, user, course_id)
    rows = session.execute(
        text("SELECT id, name FROM topics WHERE course_id = :c ORDER BY name, id"), {"c": course_id}
    ).all()
    return [{"id": r.id, "name": r.name} for r in rows]


# --- validation -------------------------------------------------------------------------------


def _has_control(value: str) -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp") for ch in value)


def clean_text_field(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ApiError(422, "invalid_fields", f"{field} must be text.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_chars or _has_control(cleaned):
        raise ApiError(
            422, "invalid_fields", f"{field} must be 1 to {max_chars} characters without control characters."
        )
    return cleaned


def join_code_candidates(value: object) -> list[str]:
    """What to look up for a typed join code: exactly as typed (trimmed), then canonical upper case."""
    if not isinstance(value, str):
        raise ApiError(422, "invalid_join_code", "Enter the join code your professor gave you.")
    typed = value.strip()
    if not typed or len(typed) > 32 or _has_control(typed):
        raise ApiError(422, "invalid_join_code", "Enter the join code your professor gave you.")
    canonical = _JOIN_SEPARATORS.sub("", typed).upper()
    candidates = [c for c in dict.fromkeys([typed, canonical]) if 0 < len(c) <= JOIN_CODE_MAX_CHARS]
    if not candidates:
        raise ApiError(422, "invalid_join_code", "Enter the join code your professor gave you.")
    return candidates


def new_join_code() -> str:
    return "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))


# --- create -----------------------------------------------------------------------------------


def creation_forbidden() -> ApiError:
    return ApiError(403, "course_create_not_allowed", "Only professors can create courses.")


def require_can_create(session: Session, user: CurrentUser) -> None:
    if load_account(session, user).role != PROFESSOR:
        raise creation_forbidden()


def create_course(
    session: Session, user: CurrentUser, name: str, code: str, *, generate=new_join_code
) -> dict:
    account = load_account(session, user, lock=True)  # held until commit: no role change in between
    if account.role != PROFESSOR:
        raise creation_forbidden()
    for _ in range(JOIN_CODE_ATTEMPTS):
        join_code = generate()
        row = session.execute(
            text(
                "INSERT INTO courses (owner_id, name, code, join_code) "
                "SELECT :owner, :name, :code, :join WHERE NOT EXISTS "
                "(SELECT 1 FROM courses WHERE upper(join_code) = :join_upper) "
                "ON CONFLICT DO NOTHING RETURNING id"
            ),
            {"owner": user.id, "name": name, "code": code, "join": join_code, "join_upper": join_code.upper()},
        ).one_or_none()
        if row is not None:
            return {
                "id": row.id,
                "code": code,
                "name": name,
                "professor_name": account.display_name,
                "join_code": join_code,
                "student_count": 0,
            }
    raise ApiError(503, "join_code_unavailable", "The course could not be created right now. Try again.")


# --- join -------------------------------------------------------------------------------------


def join_forbidden() -> ApiError:
    return ApiError(403, "join_not_allowed", "Only student accounts can join a course with a code.")


def invalid_join_code() -> ApiError:
    return ApiError(404, "join_code_not_found", "That join code does not match any course.")


def require_can_join(session: Session, user: CurrentUser) -> None:
    if load_account(session, user).role != STUDENT:
        raise join_forbidden()


def join_course(session: Session, user: CurrentUser, candidates: list[str]) -> tuple[dict, bool]:
    """(course item, created). Locks: caller, course, owner (FOR SHARE, held until commit)."""
    account = load_account(session, user, lock=True)
    if account.role != STUDENT:
        raise join_forbidden()
    rows = session.execute(
        text("SELECT id, owner_id, join_code FROM courses WHERE join_code = ANY(:codes) FOR SHARE"),
        {"codes": candidates},
    ).all()
    if not rows:
        raise invalid_join_code()
    by_code = {r.join_code: r for r in rows}
    course = next(by_code[c] for c in candidates if c in by_code)  # exact typed form first
    owner = session.execute(
        text("SELECT role, status FROM users WHERE id = :id FOR SHARE"), {"id": course.owner_id}
    ).one_or_none()
    if owner is None or owner.role != PROFESSOR or owner.status != ACTIVE:
        raise invalid_join_code()  # not joinable; indistinguishable from an unknown code

    inserted = session.execute(
        text(
            "INSERT INTO enrollments (user_id, course_id, role) VALUES (:u, :c, 'student') "
            "ON CONFLICT (user_id, course_id) DO NOTHING RETURNING id"
        ),
        {"u": user.id, "c": course.id},
    ).one_or_none()
    created = inserted is not None
    if not created:
        # The conflicting row is committed (ON CONFLICT waited for it); it must grant student access.
        existing = session.execute(
            text("SELECT role FROM enrollments WHERE user_id = :u AND course_id = :c FOR SHARE"),
            {"u": user.id, "c": course.id},
        ).one_or_none()
        if existing is None:
            raise ApiError(409, "enrollment_changed", "Your enrollment changed while joining. Try again.")
        if existing.role != STUDENT:
            raise ApiError(409, "enrollment_conflict", "You already belong to this course in another role.")

    item = session.execute(
        text(
            f"SELECT c.id, c.code, c.name, c.join_code, o.display_name AS professor_name, "
            f"{_STUDENT_COUNT} AS student_count FROM courses c JOIN users o ON o.id = c.owner_id WHERE c.id = :c"
        ),
        {"c": course.id},
    ).one()
    return _course_item(item, show_join_code=False), created
