"""Who may read a dashboard, send study events, or read course analytics (decided from the database rows).

- dashboard and study events: an active `student` account with a current `student`
  enrollment (the M7 practice rule). The owning professor gets 403 (dashboard_not_allowed /
  study_events_not_allowed); anyone else, and unknown courses, 404 course_not_found.
- analytics: only the active professor who owns the course. An enrolled student gets 403
  analytics_not_allowed; other professors, admins, unenrolled users and unknown courses 404.
Every endpoint checks this inside its data transaction and again, in a fresh transaction,
right before the response body is sent (a revocation in between is honoured).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import CurrentUser, not_authenticated
from ..errors import ApiError
from ..services.course_access import account_disabled, course_not_found, require_active


def dashboard_not_allowed() -> ApiError:
    return ApiError(403, "dashboard_not_allowed", "The learning dashboard is for students enrolled in this course.")


def study_events_not_allowed() -> ApiError:
    return ApiError(403, "study_events_not_allowed", "Study activity is recorded for enrolled students only.")


def analytics_not_allowed() -> ApiError:
    return ApiError(403, "analytics_not_allowed", "Course analytics are available to the course's professor only.")


def _account_and_course(session: Session, user: CurrentUser, course_id: uuid.UUID, share: str):
    require_active(user)
    account = session.execute(text(f"SELECT role, status FROM users WHERE id = :u{share}"), {"u": user.id}).one_or_none()
    if account is None:
        raise not_authenticated()
    if account.status != "active":
        raise account_disabled()
    course = session.execute(text(f"SELECT owner_id FROM courses WHERE id = :c{share}"), {"c": course_id}).one_or_none()
    if course is None:
        raise course_not_found()
    return account, course


def require_student(
    session: Session, user: CurrentUser, course_id: uuid.UUID, forbidden, *, lock: bool = False
) -> None:
    share = " FOR SHARE" if lock else ""
    account, course = _account_and_course(session, user, course_id, share)
    if account.role == "student":
        enrolled = session.execute(
            text(f"SELECT 1 FROM enrollments WHERE user_id = :u AND course_id = :c AND role = 'student'{share}"),
            {"u": user.id, "c": course_id},
        ).first()
        if enrolled is None:
            raise course_not_found()
        return
    if account.role == "professor" and course.owner_id == user.id:
        raise forbidden()
    raise course_not_found()


def require_owner(session: Session, user: CurrentUser, course_id: uuid.UUID) -> None:
    account, course = _account_and_course(session, user, course_id, "")
    if account.role == "professor" and course.owner_id == user.id:
        return
    if account.role == "student":
        enrolled = session.execute(
            text("SELECT 1 FROM enrollments WHERE user_id = :u AND course_id = :c AND role = 'student'"),
            {"u": user.id, "c": course_id},
        ).first()
        if enrolled is not None:
            raise analytics_not_allowed()
    raise course_not_found()
