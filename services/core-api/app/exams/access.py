"""Who may use the professor exam builder (decided from the database rows, never from the principal alone).

Only the active professor who currently owns the course (courses.owner_id) may read or write exam data:
- unknown / deleted principal: 401 not_authenticated; disabled account: 403 account_disabled;
- an active student with a `student` enrollment in the course: 403 exams_not_allowed, decided before any
  exam or question id is looked up, so no id can be probed;
- other professors, admins (no implicit access, no break-glass in M9), unenrolled students and unknown
  courses: 404 course_not_found, with an identical body.

lock=True takes FOR SHARE locks on the user and course rows (in that order, the M9 lock order), so a
concurrent ownership transfer or deactivation waits until the write that re-checked access commits.
Every response that carries exam content or a file re-checks this in a fresh short read-only transaction
right before it is sent (authorize_owner).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser, not_authenticated
from ..errors import ApiError
from ..quiz.access import short_tx
from ..services.course_access import account_disabled, course_not_found, require_active


def exams_not_allowed() -> ApiError:
    return ApiError(403, "exams_not_allowed", "Exams and the question bank are available to the course's professor only.")


def exam_not_found() -> ApiError:
    return ApiError(404, "exam_not_found", "Exam not found.")


def question_not_found() -> ApiError:
    return ApiError(404, "question_not_found", "Question not found.")


def exam_item_not_found() -> ApiError:
    return ApiError(404, "exam_item_not_found", "Exam item not found.")


def require_exam_owner(session: Session, user: CurrentUser, course_id: uuid.UUID, *, lock: bool = False):
    """The course row (id, code, name) when the caller is its active owning professor; ApiError otherwise."""
    require_active(user)
    share = " FOR SHARE" if lock else ""
    account = session.execute(text(f"SELECT role, status FROM users WHERE id = :u{share}"), {"u": user.id}).one_or_none()
    if account is None:
        raise not_authenticated()
    if account.status != "active":
        raise account_disabled()
    course = session.execute(
        text(f"SELECT id, owner_id, code, name FROM courses WHERE id = :c{share}"), {"c": course_id}
    ).one_or_none()
    if course is None:
        raise course_not_found()
    if account.role == "professor" and course.owner_id == user.id:
        return course
    if account.role == "student":
        enrolled = session.execute(
            text("SELECT 1 FROM enrollments WHERE user_id = :u AND course_id = :c AND role = 'student'"),
            {"u": user.id, "c": course_id},
        ).first()
        if enrolled is not None:
            raise exams_not_allowed()
    raise course_not_found()


def authorize_owner(factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID) -> None:
    """Owner access in its own short read-only transaction (ApiError when refused)."""
    with short_tx(factory, read_only=True) as session:
        require_exam_owner(session, user, course_id)
