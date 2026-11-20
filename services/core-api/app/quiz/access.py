"""Who may practise in a course, and the short transactions every quiz step runs in.

Practice policy (M7): only an active `student` account with a current
`student` enrollment in the course. The decision is read from the users,
courses and enrollments rows (never from the principal alone), with the same
error vocabulary as app/services/course_access.py:
- account disabled: 403 account_disabled;
- the professor who owns the course: 403 practice_not_allowed (they can read
  the course, practice is for students);
- anyone else without a student enrollment (other professors, admins,
  unenrolled students) and unknown courses: 404 course_not_found.

lock=True takes FOR SHARE locks on the user, course and enrollment rows, in
that order, so a concurrent deactivation, course deletion or unenrollment
waits until the write transaction that re-checked access commits. That
serializes authorization-sensitive writes; it cannot recall a response that
was already committed and is being delivered (docs/quiz-api.md, "Revocation").
quiz_sessions deliberately has no foreign key to enrollments: removing a
student keeps their history, only new access is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser, not_authenticated
from ..chat.db import apply_short_timeouts
from ..errors import ApiError
from ..services.course_access import account_disabled, course_not_found, require_active


def practice_not_allowed() -> ApiError:
    return ApiError(403, "practice_not_allowed", "Practice quizzes are for students enrolled in this course.")


def require_practice(session: Session, user: CurrentUser, course_id: uuid.UUID, *, lock: bool = False) -> None:
    require_active(user)
    share = " FOR SHARE" if lock else ""
    account = session.execute(text(f"SELECT role, status FROM users WHERE id = :u{share}"), {"u": user.id}).one_or_none()
    if account is None:
        raise not_authenticated()
    if account.status != "active":
        raise account_disabled()
    course = session.execute(text(f"SELECT owner_id FROM courses WHERE id = :c{share}"), {"c": course_id}).one_or_none()
    if course is None:
        raise course_not_found()
    if account.role == "student":
        enrolled = session.execute(
            text(f"SELECT 1 FROM enrollments WHERE user_id = :u AND course_id = :c AND role = 'student'{share}"),
            {"u": user.id, "c": course_id},
        ).first()
        if enrolled is None:
            raise course_not_found()
        return
    if account.role == "professor" and course.owner_id == user.id:
        raise practice_not_allowed()
    raise course_not_found()


@contextmanager
def short_tx(factory: sessionmaker[Session], *, read_only: bool = False, snapshot: bool = False) -> Iterator[Session]:
    """One short transaction with statement and lock timeouts; rolled back unless the body committed."""
    with factory() as session:
        try:
            if snapshot:
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            else:
                session.connection()
            if read_only:
                session.execute(text("SET TRANSACTION READ ONLY"))
            apply_short_timeouts(session)
            yield session
        finally:
            session.rollback()


def authorize_practice(factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID) -> None:
    """Practice access in its own short read-only transaction (ApiError when refused)."""
    with short_tx(factory, read_only=True) as session:
        require_practice(session, user, course_id)
