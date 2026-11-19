"""Who may read or add materials in a course.

Course-level authorization is business logic on top of the authenticated
principal from app.auth (identity and role are CE-3's; ownership and
enrollment are checked here, in one place, for every course endpoint).

Policy (M2):
- read (list materials): the active professor who owns the course, or an
  active student with a `student` enrollment in it.
- upload: only the active professor who owns the course.
- No other role gets access through an enrollment, and admins get no implicit
  access to courses (a read-only admin exception is pending team approval).

Errors: a course that does not exist and a course the caller has no
relationship with both answer 404, so course ids cannot be probed. A caller
who can read the course but not perform the action gets 403.

The decision is made from the users, courses and enrollments rows, not only
from the principal, so it can be re-checked inside the write transaction
(lock=True takes FOR SHARE locks, holding off a concurrent ownership change
or account deactivation until the upload commits).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import CurrentUser, not_authenticated
from ..errors import ApiError
from ..models import Course, Enrollment, User

ACTIVE = "active"
PROFESSOR = "professor"
STUDENT = "student"


@dataclass(frozen=True)
class CourseAccess:
    course_id: uuid.UUID
    can_read: bool
    can_upload: bool
    display_name: str


def account_disabled() -> ApiError:
    return ApiError(403, "account_disabled", "This account is disabled.")


def course_not_found() -> ApiError:
    return ApiError(404, "course_not_found", "Course not found.")


def require_active(user: CurrentUser) -> None:
    """Defence in depth: the auth dependency should already have rejected this."""
    if user.status != ACTIVE:
        raise account_disabled()


def load_course_access(
    session: Session, user: CurrentUser, course_id: uuid.UUID, *, lock: bool = False
) -> CourseAccess:
    require_active(user)

    user_query = select(User.role, User.status, User.display_name).where(User.id == user.id)
    course_query = select(Course.owner_id).where(Course.id == course_id)
    if lock:
        user_query = user_query.with_for_update(read=True)
        course_query = course_query.with_for_update(read=True)

    account = session.execute(user_query).one_or_none()
    if account is None:
        raise not_authenticated()  # the principal no longer exists
    if account.status != ACTIVE:
        raise account_disabled()
    course = session.execute(course_query).one_or_none()
    if course is None:
        raise course_not_found()

    owner = account.role == PROFESSOR and course.owner_id == user.id
    enrolled_student = False
    if account.role == STUDENT:
        enrolled_student = (
            session.execute(
                select(Enrollment.id).where(
                    Enrollment.course_id == course_id,
                    Enrollment.user_id == user.id,
                    Enrollment.role == STUDENT,
                )
            ).first()
            is not None
        )
    return CourseAccess(
        course_id=course_id,
        can_read=owner or enrolled_student,
        can_upload=owner,
        display_name=account.display_name,
    )


def require_read(session: Session, user: CurrentUser, course_id: uuid.UUID) -> CourseAccess:
    access = load_course_access(session, user, course_id)
    if not access.can_read:
        raise course_not_found()
    return access


def require_upload(session: Session, user: CurrentUser, course_id: uuid.UUID, *, lock: bool = False) -> CourseAccess:
    access = load_course_access(session, user, course_id, lock=lock)
    if not access.can_read:
        raise course_not_found()
    if not access.can_upload:
        raise ApiError(403, "upload_not_allowed", "Only the professor who owns this course can upload materials.")
    return access
