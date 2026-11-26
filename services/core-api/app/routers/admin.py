"""Admin observability (WP-4) and admin mutation (WP-9) endpoints (docs/admin-api.md).

GET  /api/v1/admin/overview                                      sources, documents, jobs, feature switches
GET  /api/v1/admin/sources?enabled=&limit=&cursor=               external sources and their harvest state
GET  /api/v1/admin/jobs?kind=&status=&limit=&cursor=             parse/index jobs and harvest runs
PUT  /api/v1/admin/sources/{source_id}/enabled                   {"enabled": bool}: switch a provider source
POST /api/v1/admin/harvest-runs                                  {"course_id", "source_id"}: queue a manual run
POST /api/v1/admin/jobs/{job_id}/retry                           (no body): retry a failed parse/index job

Order of checks: authentication (CE-3 dependency), the caller's active admin row, the query
string, then one read-only snapshot that checks the admin row again. Every response, errors
included, carries Cache-Control: no-store; bodies pass strict schemas (app/schemas/admin.py).
An unexpected exception is logged as class + SQLSTATE only and answered with the generic 500.

Mutations: authentication, the admin row (read-only), an empty query string, the path id, the strict JSON
body, then one transaction (app/admin/mutations.py) that locks and re-checks the admin row, applies the
existing operator transition and writes its audit event before committing. A lock or statement timeout
is answered 503 admin_busy (Retry-After); nothing was committed.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..admin import mutations, observability
from ..auth import CurrentUser, get_current_user
from ..errors import ApiError
from ..exams.http import DEFAULT_LIMIT, MAX_LIMIT, NoStoreRoute, choice, parse_limit, query_params
from ..json_body import invalid_body, read_json_object, require_keys
from ..quiz.diagnostics import describe, internal_error, sqlstate_of
from ..quiz.protocol import canonical_uuid
from ..schemas.admin import (
    AdminHarvestRunCreated,
    AdminJobList,
    AdminJobRetry,
    AdminOverview,
    AdminSourceChange,
    AdminSourceList,
)
from ..schemas.documents import ErrorBody
from ..schemas.openapi import (
    BEARER,
    RETRY_AFTER,
    STRING,
    UUID,
    ResourceId,
    enum,
    exact,
    json_body,
    query,
    validation_error,
)

log = logging.getLogger(__name__)

# NoStoreRoute: no-store also on errors raised by dependencies (the authentication 401).
router = APIRouter(prefix="/api/v1/admin", tags=["admin"], route_class=NoStoreRoute)

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody, "description": "Not an active administrator."},
    500: {"model": ErrorBody},
    401: {"model": ErrorBody, "headers": BEARER},
    422: validation_error(),
}
_LIMIT = query("limit", {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT}, "Page size.")
_CURSOR = query("cursor", STRING, "next_cursor of the previous page, with the same filters.")


def _respond(body: dict, model, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=model.model_validate(body).model_dump(mode="json"),
                        headers=dict(NO_STORE))  # fmt: skip


async def _guarded(call, endpoint: str):
    try:
        return await call()
    except ApiError as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except StarletteHTTPException:
        raise
    except Exception as exc:
        log.error("admin_endpoint_error endpoint=%s %s", endpoint, describe(exc))
        raise internal_error() from None


@router.get(
    "/overview",
    responses={**_ERRORS, 200: {"model": AdminOverview}},
    summary="Source, document and job counts and the feature switches (admin; no parameters)",
    response_model=None,
)
async def admin_overview(request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        await run_in_threadpool(observability.authorize, state.session_factory, user)
        query_params(request, frozenset())
        body = await run_in_threadpool(observability.overview, state.session_factory, user, state.settings)
        return _respond(body, AdminOverview)

    return await _guarded(run, "admin.overview")


@router.get(
    "/sources",
    responses={**_ERRORS, 200: {"model": AdminSourceList}},
    openapi_extra={"parameters": [query("enabled", enum("true", "false"), "Only enabled or only disabled sources."),
                                  _LIMIT, _CURSOR]},  # fmt: skip
    summary="External sources with harvest targets, document counts and the latest run (admin), newest first",
    response_model=None,
)
async def admin_sources(request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        await run_in_threadpool(observability.authorize, state.session_factory, user)
        params = query_params(request, frozenset({"enabled", "limit", "cursor"}))
        enabled = choice(params, "enabled", ("true", "false"))
        body = await run_in_threadpool(
            observability.list_sources, state.session_factory, user,
            None if enabled is None else enabled == "true", parse_limit(params), params.get("cursor"),
        )  # fmt: skip
        return _respond(body, AdminSourceList)

    return await _guarded(run, "admin.sources")


@router.get(
    "/jobs",
    responses={**_ERRORS, 200: {"model": AdminJobList}},
    openapi_extra={"parameters": [query("kind", enum(*observability.JOB_KINDS), "Job kind."),
                                  query("status", enum(*observability.JOB_STATUSES), "Job status."),
                                  _LIMIT, _CURSOR]},  # fmt: skip
    summary="Document parse/index jobs and harvest runs (admin), newest first, with safe error summaries",
    response_model=None,
)
async def admin_jobs(request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        await run_in_threadpool(observability.authorize, state.session_factory, user)
        params = query_params(request, frozenset({"kind", "status", "limit", "cursor"}))
        body = await run_in_threadpool(
            observability.list_jobs, state.session_factory, user,
            choice(params, "kind", observability.JOB_KINDS), choice(params, "status", observability.JOB_STATUSES),
            parse_limit(params), params.get("cursor"),
        )  # fmt: skip
        return _respond(body, AdminJobList)

    return await _guarded(run, "admin.jobs")


# --- mutations (WP-9) ---------------------------------------------------------------------------------------

MAX_BODY_BYTES = 1024
BOOLEAN = {"type": "boolean"}
BODY_TIMEOUT_SECONDS = 10.0
_BUSY_SQLSTATES = frozenset({"55P03", "57014", "40P01", "40001"})  # lock/statement timeout, deadlock, serialization
_MUTATION_ERRORS: dict[int | str, dict[str, Any]] = {
    **_ERRORS,
    400: {"model": ErrorBody, "description": "Malformed JSON body."},
    404: {"model": ErrorBody, "description": "No such source, course or job."},
    408: {"model": ErrorBody},
    409: {"model": ErrorBody, "description": "Refused by the current state; nothing was changed."},
    413: {"model": ErrorBody},
    415: {"model": ErrorBody},
    503: {"model": ErrorBody, "headers": RETRY_AFTER, "description": "Busy; nothing was changed. Retry later."},
}


async def _guarded_mutation(call, endpoint: str):
    try:
        return await call()
    except ApiError as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except StarletteHTTPException:
        raise
    except Exception as exc:
        if sqlstate_of(exc) in _BUSY_SQLSTATES:
            log.warning("admin_mutation_busy endpoint=%s %s", endpoint, describe(exc))
            raise ApiError(503, "admin_busy", "This item is being changed right now. Try again in a moment.",
                           headers={**NO_STORE, "Retry-After": "2"}) from None  # fmt: skip
        log.error("admin_endpoint_error endpoint=%s %s", endpoint, describe(exc))
        raise internal_error() from None


async def _body(request: Request, keys: frozenset[str]) -> dict:
    """The strict JSON object with exactly `keys`; with no keys, an absent body or {}."""
    lengths = request.headers.getlist("content-length")
    if not keys and not request.headers.getlist("content-type") and (not lengths or lengths == ["0"]):
        if request.headers.getlist("transfer-encoding"):
            raise invalid_body("Send the request as JSON.")
        return {}
    body = await read_json_object(request, max_bytes=MAX_BODY_BYTES, timeout=BODY_TIMEOUT_SECONDS)
    if not keys and body:
        raise ApiError(422, "invalid_fields", "This request takes no fields.")
    if keys:
        require_keys(body, keys)
    return body


def _body_uuid(body: dict, name: str):
    value = canonical_uuid(body[name])
    if value is None:
        raise ApiError(422, "invalid_fields", f"{name} must be a lower-case UUID.")
    return value


@router.put(
    "/sources/{source_id}/enabled",
    responses={**_MUTATION_ERRORS, 200: {"model": AdminSourceChange}},
    openapi_extra=json_body(exact(enabled=BOOLEAN)),
    summary="Enable or disable a provider-managed source (admin, audited); disabling cancels its active runs",
    response_model=None,
)
async def admin_set_source(source_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        await run_in_threadpool(observability.authorize, state.session_factory, user)
        query_params(request, frozenset())
        parsed = canonical_uuid(source_id)
        if parsed is None:
            raise mutations.source_not_found()
        body = await _body(request, frozenset({"enabled"}))
        if not isinstance(body["enabled"], bool):
            raise ApiError(422, "invalid_fields", "enabled must be true or false.")
        result = await run_in_threadpool(mutations.set_source, state.session_factory, user, parsed, body["enabled"])
        return _respond(result, AdminSourceChange)

    return await _guarded_mutation(run, "admin.set_source")


@router.post(
    "/harvest-runs",
    status_code=201,
    responses={**_MUTATION_ERRORS, 201: {"model": AdminHarvestRunCreated}},
    openapi_extra=json_body(exact(course_id=UUID, source_id=UUID)),
    summary="Queue a manual harvest run for an opted-in course and enabled source (admin, audited)",
    response_model=None,
)
async def admin_trigger_harvest(request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        await run_in_threadpool(observability.authorize, state.session_factory, user)
        query_params(request, frozenset())
        body = await _body(request, frozenset({"course_id", "source_id"}))
        course_id, source_id = _body_uuid(body, "course_id"), _body_uuid(body, "source_id")
        result = await run_in_threadpool(
            mutations.trigger_harvest, state.session_factory, user, state.settings, course_id, source_id
        )
        return _respond(result, AdminHarvestRunCreated, 201)

    return await _guarded_mutation(run, "admin.trigger_harvest")


@router.post(
    "/jobs/{job_id}/retry",
    status_code=201,
    responses={**_MUTATION_ERRORS, 201: {"model": AdminJobRetry}},
    openapi_extra=json_body(exact(), required=False),
    summary="Queue a new job for a failed parse or index job's document (admin, audited); the failed job is kept",
    response_model=None,
)
async def admin_retry_job(job_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        await run_in_threadpool(observability.authorize, state.session_factory, user)
        query_params(request, frozenset())
        parsed = canonical_uuid(job_id)
        if parsed is None:
            raise mutations.job_not_found()
        await _body(request, frozenset())
        result = await run_in_threadpool(mutations.retry_job, state.session_factory, user, state.settings, parsed)
        return _respond(result, AdminJobRetry, 201)

    return await _guarded_mutation(run, "admin.retry_job")
