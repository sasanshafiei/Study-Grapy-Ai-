"""Course endpoints (public API, /api/v1).

GET /courses and GET /courses/{id}/topics are CE-2's (contracts/core-api.web.md
on the CE-2 branch). POST /courses and POST /courses/join are CE-side
additions, documented in docs/courses-api.md.

The POST handlers take the raw Request: the caller is authenticated (the
dependency) and authorized against the database before the body is read,
under the size and time limits of app/json_body.py. Joins are rate limited per
user before anything else is done (app/limits.py; single process only).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..errors import ApiError
from ..json_body import read_json_object, require_keys
from ..schemas.courses import CourseCreateBody, CourseItem, CourseJoinBody, TopicItem
from ..schemas.documents import ErrorBody
from ..services import courses

router = APIRouter(prefix="/api/v1", tags=["courses"])

MAX_BODY_BYTES = 4096
BODY_TIMEOUT_SECONDS = 10.0
CREATE_KEYS = frozenset({"name", "code"})
JOIN_KEYS = frozenset({"join_code"})

T = TypeVar("T")

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorBody, "description": "Not signed in"},
    403: {"model": ErrorBody, "description": "Account disabled, or the role may not do this"},
}


def _json_body(model) -> dict[str, Any]:
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}


def in_transaction(factory, fn: Callable[..., T], *args) -> T:
    """Run fn(session, *args) in its own short transaction (commit on success, rollback otherwise)."""
    with factory() as session:
        try:
            result = fn(session, *args)
            session.commit()
            return result
        except BaseException:
            session.rollback()
            raise


@router.get("/courses", response_model=list[CourseItem], responses=_ERRORS, summary="List my courses")
def list_courses(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return courses.list_courses(db, user)


@router.get(
    "/courses/{course_id}/topics",
    response_model=list[TopicItem],
    responses={**_ERRORS, 404: {"model": ErrorBody, "description": "Course not found (or not visible)"}},
    summary="List a course's topics",
)
def list_topics(
    course_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[dict]:
    return courses.list_topics(db, user, course_id)


@router.post(
    "/courses",
    status_code=201,
    response_model=CourseItem,
    responses={**_ERRORS, 413: {"model": ErrorBody}, 415: {"model": ErrorBody}, 422: {"model": ErrorBody}},
    summary="Create a course (professors)",
    openapi_extra=_json_body(CourseCreateBody),
)
async def create_course(request: Request, user: CurrentUser = Depends(get_current_user)) -> dict:
    factory = request.app.state.session_factory
    await run_in_threadpool(in_transaction, factory, courses.require_can_create, user)
    body = await read_json_object(request, max_bytes=MAX_BODY_BYTES, timeout=BODY_TIMEOUT_SECONDS)
    require_keys(body, CREATE_KEYS)
    name = courses.clean_text_field(body["name"], "name", courses.NAME_MAX_CHARS)
    code = courses.clean_text_field(body["code"], "code", courses.CODE_MAX_CHARS)
    return await run_in_threadpool(in_transaction, factory, courses.create_course, user, name, code)


@router.post(
    "/courses/join",
    response_model=CourseItem,
    responses={
        **_ERRORS,
        201: {"model": CourseItem, "description": "Enrolled now"},
        404: {"model": ErrorBody, "description": "No joinable course has this code"},
        409: {"model": ErrorBody, "description": "Already in the course with another role"},
        429: {"model": ErrorBody, "description": "Too many join attempts"},
    },
    summary="Join a course with its join code (students)",
    description="200 when the caller was already enrolled as a student, 201 when enrolled now.",
    openapi_extra=_json_body(CourseJoinBody),
)
async def join_course(request: Request, user: CurrentUser = Depends(get_current_user)):
    retry_after = request.app.state.join_rate_limiter.hit(user.id)
    if retry_after is not None:
        raise ApiError(
            429,
            "too_many_join_attempts",
            "Too many join attempts. Wait a few minutes and try again.",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )
    factory = request.app.state.session_factory
    await run_in_threadpool(in_transaction, factory, courses.require_can_join, user)
    body = await read_json_object(request, max_bytes=MAX_BODY_BYTES, timeout=BODY_TIMEOUT_SECONDS)
    require_keys(body, JOIN_KEYS)
    candidates = courses.join_code_candidates(body["join_code"])
    item, created = await run_in_threadpool(in_transaction, factory, courses.join_course, user, candidates)
    return JSONResponse(status_code=201 if created else 200, content=CourseItem(**item).model_dump(mode="json"))
