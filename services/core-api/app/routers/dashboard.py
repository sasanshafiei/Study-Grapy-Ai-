"""Dashboard, analytics and study-event endpoints (M8; docs/dashboard-api.md, docs/analytics-privacy.md,
docs/study-events.md).

GET  /api/v1/courses/{course_id}/dashboard?tz=<IANA zone>   the caller's own learning data (student)
GET  /api/v1/courses/{course_id}/analytics                  privacy-filtered weekly releases (owner professor)
POST /api/v1/courses/{course_id}/study-events               client-reported study telemetry (student)

Order of checks: authentication (dependency), authorization from the database rows, the
feature flag (503 after authorization, so the flag does not reveal course existence), the
query string / body, then the work. The data is read in one snapshot; the caller is
authorized again in a fresh transaction right before the body is sent. Every response,
errors included, carries Cache-Control: no-store; bodies pass explicit schemas
(app/schemas/dashboard.py). An unexpected exception is logged as class + SQLSTATE only and
answered with the generic 500 (the M7 diagnostics), never with its message.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..auth import CurrentUser, get_current_user
from ..dashboard import analytics, student, study_events
from ..dashboard.timezones import InvalidTimezone, parse_timezone
from ..errors import ApiError
from ..json_body import read_json_object
from ..quiz.diagnostics import describe, internal_error
from ..schemas.dashboard import AnalyticsResponse, DashboardResponse, StudyEventsResponse
from ..schemas.documents import ErrorBody

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["dashboard"])

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorBody} for status in (400, 401, 403, 404, 408, 409, 413, 415, 422, 429, 503)
}


def _respond(status: int, body: dict, model) -> JSONResponse:
    content = model.model_validate(body).model_dump(mode="json")
    return JSONResponse(status_code=status, content=content, headers=dict(NO_STORE))


async def _guarded(call, endpoint: str):
    try:
        return await call()
    except ApiError as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except StarletteHTTPException:
        raise
    except Exception as exc:
        log.error("dashboard_endpoint_error endpoint=%s %s", endpoint, describe(exc))
        raise internal_error() from None


def _query(request: Request, allowed: frozenset[str]) -> dict[str, str]:
    items = request.query_params.multi_items()
    keys = [k for k, _ in items]
    if any(k not in allowed for k in keys) or len(keys) != len(set(keys)):
        raise ApiError(422, "invalid_query", "Unsupported query parameter.")
    return dict(items)


@router.get(
    "/courses/{course_id}/dashboard",
    responses={**_ERRORS, 200: {"model": DashboardResponse}},
    summary="The caller's learning dashboard for one course (student)",
    response_model=None,
)
async def dashboard(course_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        settings = state.settings
        await run_in_threadpool(student.authorize_dashboard, state.session_factory, user, course_id)
        if not settings.dashboard_enabled:
            raise ApiError(503, "dashboard_unavailable", "The learning dashboard is not available right now.")
        query = _query(request, frozenset({"tz"}))
        try:
            zone_name, zone = parse_timezone(query.get("tz"))
        except InvalidTimezone:
            raise ApiError(422, "invalid_timezone", "tz must be an IANA time zone name such as Europe/Istanbul.") from None
        view = await run_in_threadpool(
            student.read_dashboard, state.session_factory, user, course_id, zone_name, zone, settings
        )
        await run_in_threadpool(student.authorize_dashboard, state.session_factory, user, course_id)
        return _respond(200, view, DashboardResponse)

    return await _guarded(run, "dashboard")


@router.get(
    "/courses/{course_id}/analytics",
    responses={**_ERRORS, 200: {"model": AnalyticsResponse}},
    summary="Privacy-filtered weekly class analytics (owner professor; no parameters)",
    response_model=None,
)
async def course_analytics(course_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        settings = state.settings
        await run_in_threadpool(analytics.authorize_analytics, state.session_factory, user, course_id)
        if not settings.analytics_enabled:
            raise ApiError(503, "analytics_unavailable", "Course analytics are not available yet (privacy review pending).")
        _query(request, frozenset())  # no filters, ranges or subsets: by design
        view = await run_in_threadpool(analytics.read_analytics, state.session_factory, user, course_id, settings)
        await run_in_threadpool(analytics.authorize_analytics, state.session_factory, user, course_id)
        return _respond(200, view, AnalyticsResponse)

    return await _guarded(run, "analytics")


_STUDY_EVENTS_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {
            "type": "object", "required": ["events"], "additionalProperties": False,
            "properties": {"events": {"type": "array", "minItems": 1, "maxItems": 25, "items": {"type": "object"}}},
        }}},
    }
}  # fmt: skip


@router.post(
    "/courses/{course_id}/study-events",
    responses={**_ERRORS, 200: {"model": StudyEventsResponse}},
    summary="Record client-reported study activity (student; idempotent per client_event_id)",
    openapi_extra=_STUDY_EVENTS_BODY,
    response_model=None,
)
async def post_study_events(course_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        settings = state.settings
        await run_in_threadpool(study_events.authorize, state.session_factory, user, course_id)
        if not settings.study_events_enabled:
            raise ApiError(503, "study_events_unavailable", "Study activity tracking is not enabled.")
        _query(request, frozenset())
        body = await read_json_object(
            request, max_bytes=settings.study_events_max_body_bytes, timeout=settings.study_events_body_timeout_seconds
        )
        events = study_events.parse_batch(body, settings)
        view = await run_in_threadpool(study_events.record, state.session_factory, user, course_id, events, settings)
        return _respond(200, view, StudyEventsResponse)

    return await _guarded(run, "study_events")
