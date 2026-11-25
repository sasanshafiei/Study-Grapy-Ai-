"""Read-only admin observability endpoints (WP-4; docs/admin-api.md).

GET /api/v1/admin/overview                                       sources, documents, jobs, feature switches
GET /api/v1/admin/sources?enabled=&limit=&cursor=                external sources and their harvest state
GET /api/v1/admin/jobs?kind=&status=&limit=&cursor=              parse/index jobs and harvest runs

Order of checks: authentication (CE-3 dependency), the caller's active admin row, the query
string, then one read-only snapshot that checks the admin row again. Every response, errors
included, carries Cache-Control: no-store; bodies pass strict schemas (app/schemas/admin.py).
An unexpected exception is logged as class + SQLSTATE only and answered with the generic 500.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..admin import observability
from ..auth import CurrentUser, get_current_user
from ..errors import ApiError
from ..exams.http import DEFAULT_LIMIT, MAX_LIMIT, NoStoreRoute, choice, parse_limit, query_params
from ..quiz.diagnostics import describe, internal_error
from ..schemas.admin import AdminJobList, AdminOverview, AdminSourceList
from ..schemas.documents import ErrorBody
from ..schemas.openapi import BEARER, STRING, enum, query, validation_error

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


def _respond(body: dict, model) -> JSONResponse:
    return JSONResponse(status_code=200, content=model.model_validate(body).model_dump(mode="json"),
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
