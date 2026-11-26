"""Exam endpoints (core-api exams v1, docs/exam-api.md, E8-E24).

Same order of checks as the bank router: authentication, owner authorization from the database,
EXAMS_ENABLED (then EXAM_EXPORT_ENABLED for the file endpoints), preconditions, query/body, work.
Item and blueprint endpoints are versioned on the exam (If-Match carries the exam version).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ..auth import CurrentUser, get_current_user
from ..errors import ApiError
from ..exams import bank, builder, finalize
from ..exams import openapi as wire
from ..exams.access import exam_item_not_found, exam_not_found
from ..exams.http import (
    NoStoreRoute,
    choice,
    etag,
    guarded,
    idempotency_key,
    if_match,
    parse_limit,
    query_params,
    read_body,
    read_optional_body,
    require_empty_body,
    respond,
)
from ..exams.validation import (
    parse_blueprint,
    parse_duplicate_title,
    parse_exam_fields,
    parse_points,
    parse_reason,
    require_exact_keys,
)
from ..quiz.protocol import canonical_uuid
from ..schemas.openapi import ResourceId
from ..schemas.exams import (
    AuditList,
    BlueprintResponse,
    DuplicateResponse,
    Exam,
    ExamList,
    FillResponse,
    Preview,
)
from .exam_questions import authorize

router = APIRouter(prefix="/api/v1", tags=["exams"], route_class=NoStoreRoute)
_FINGERPRINT_CHARS = frozenset("0123456789abcdef")


def exam_id_of(raw: str) -> uuid.UUID:
    parsed = canonical_uuid(raw)
    if parsed is None:
        raise exam_not_found()
    return parsed


def _exam_response(status: int, body: dict, headers: dict | None = None):
    return respond(status, body, Exam, {**(headers or {}), **etag(body["version"])})


@router.get(
    "/courses/{course_id}/exams", response_model=None,
    summary="List the course's exams",
    **wire.read(ExamList, wire.EXAMS),
)
async def list_exams(course_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        params = query_params(request, frozenset({"status", "limit", "cursor"}))
        status = choice(params, "status", ("draft", "finalized", "archived"))
        body = await run_in_threadpool(builder.list_exams, request.app.state.session_factory, user, course, status,
                                       parse_limit(params), params.get("cursor"))  # fmt: skip
        return respond(200, body, ExamList)

    return await guarded(run, "exams.list")


@router.post(
    "/courses/{course_id}/exams", response_model=None, status_code=201,
    summary="Create a draft exam (Idempotency-Key)",
    **wire.mutation(Exam, wire.EXAM, keyed=True, invalid="invalid_exam",
                    conflicts="idempotency_key_reused exam_limit_reached"),
)
async def create_exam(course_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        key = idempotency_key(request)
        fields = parse_exam_fields(await read_body(request, state.settings), partial=False)
        answer = await run_in_threadpool(builder.create_exam, state.session_factory, state.settings, user, course, key, fields)
        return _exam_response(answer.status, answer.body, answer.headers)

    return await guarded(run, "exams.create")


@router.get(
    "/courses/{course_id}/exams/{exam_id}", response_model=None,
    summary="One exam with blueprint, items, readiness and preview fingerprint",
    **wire.read(Exam, etag=True),
)
async def read_exam(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        query_params(request, frozenset())
        state = request.app.state
        body = await run_in_threadpool(builder.read_exam, state.session_factory, state.settings, user, course, exam_id_of(exam_id))
        return _exam_response(200, body)

    return await guarded(run, "exams.read")


@router.patch(
    "/courses/{course_id}/exams/{exam_id}", response_model=None,
    summary="Change title, instructions or duration (If-Match)",
    **wire.mutation(Exam, wire.patch(wire.EXAM), invalid="invalid_exam", conflicts="exam_not_draft"),
)
async def update_exam(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        fields = parse_exam_fields(await read_body(request, state.settings), partial=True)
        body = await run_in_threadpool(builder.update_exam, state.session_factory, state.settings, user, course,
                                       exam_id_of(exam_id), expected, fields)  # fmt: skip
        return _exam_response(200, body)

    return await guarded(run, "exams.update")


@router.put(
    "/courses/{course_id}/exams/{exam_id}/blueprint", response_model=None,
    summary="Replace the blueprint (If-Match)",
    **wire.mutation(BlueprintResponse, wire.BLUEPRINT, invalid="invalid_blueprint", conflicts="exam_not_draft"),
)
async def replace_blueprint(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        rows = parse_blueprint(await read_body(request, state.settings))
        body = await run_in_threadpool(builder.replace_blueprint, state.session_factory, state.settings, user, course,
                                       exam_id_of(exam_id), expected, rows)  # fmt: skip
        return respond(200, body, BlueprintResponse, etag(body["exam"]["version"]))

    return await guarded(run, "exams.blueprint")


def _optional_uuid(value, error: ApiError):
    if value is None:
        return None
    parsed = canonical_uuid(value)
    if parsed is None:
        raise error
    return parsed


@router.post(
    "/courses/{course_id}/exams/{exam_id}/items", response_model=None, status_code=201,
    summary="Add a bank question to the exam (If-Match)",
    **wire.mutation(Exam, wire.ITEM, item=True, invalid="invalid_position invalid_blueprint_row invalid_points",
                    conflicts="exam_not_draft exam_item_limit question_retired question_already_in_exam export_budget_exceeded"),
)
async def add_item(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        body = await read_body(request, state.settings)
        require_exact_keys(body, frozenset({"question_id", "points", "position", "blueprint_row_id"}))
        question = canonical_uuid(body["question_id"])
        points = parse_points(body["points"])
        position = body["position"]
        if position is not None and (type(position) is not int or position < 1):
            raise ApiError(422, "invalid_position", "position is a positive whole number, or null.")
        row = _optional_uuid(body["blueprint_row_id"], ApiError(422, "invalid_blueprint_row", "This blueprint row does not exist in this exam.",
                                                                 extra={"reason": "unknown_row"}))  # fmt: skip
        view, item_id = await run_in_threadpool(builder.add_item, state.session_factory, state.settings, user, course,
                                                exam_id_of(exam_id), expected, question, points, position, row)  # fmt: skip
        return _exam_response(201, view, {"X-Exam-Item-Id": item_id})

    return await guarded(run, "exams.add_item")


def item_id_of(raw: str) -> uuid.UUID:
    parsed = canonical_uuid(raw)
    if parsed is None:
        raise exam_item_not_found()
    return parsed


@router.put(
    "/courses/{course_id}/exams/{exam_id}/items/order", response_model=None,
    summary="Reorder all items (If-Match)",
    **wire.mutation(Exam, wire.ORDER, invalid="invalid_order", conflicts="exam_not_draft"),
)
async def reorder_items(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        body = await read_body(request, state.settings)
        require_exact_keys(body, frozenset({"item_ids"}))
        if not isinstance(body["item_ids"], list) or len(body["item_ids"]) > 1000:
            raise ApiError(422, "invalid_order", "item_ids is the list of every item id of the exam.", extra={"reason": "missing"})
        order = [canonical_uuid(i) for i in body["item_ids"]]
        view = await run_in_threadpool(builder.reorder_items, state.session_factory, state.settings, user, course,
                                       exam_id_of(exam_id), expected, order)  # fmt: skip
        return _exam_response(200, view)

    return await guarded(run, "exams.reorder")


@router.patch(
    "/courses/{course_id}/exams/{exam_id}/items/{item_id}", response_model=None,
    summary="Change an item's points or blueprint row (If-Match)",
    **wire.mutation(Exam, wire.ITEM_PATCH, invalid="invalid_points invalid_blueprint_row", conflicts="exam_not_draft"),
)
async def update_item(course_id: ResourceId, exam_id: ResourceId, item_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        body = await read_body(request, state.settings)
        if not body or not set(body) <= {"points", "blueprint_row_id"}:
            raise ApiError(422, "invalid_fields", "Send a non-empty subset of: blueprint_row_id, points.")
        changes = {}
        if "points" in body:
            changes["points"] = parse_points(body["points"])
        if "blueprint_row_id" in body:
            changes["row_id"] = _optional_uuid(body["blueprint_row_id"], ApiError(422, "invalid_blueprint_row",
                                               "This blueprint row does not exist in this exam.", extra={"reason": "unknown_row"}))  # fmt: skip
        view = await run_in_threadpool(lambda: builder.update_item(state.session_factory, state.settings, user, course,
                                                                   exam_id_of(exam_id), item_id_of(item_id), expected, **changes))  # fmt: skip
        return _exam_response(200, view)

    return await guarded(run, "exams.update_item")


@router.delete(
    "/courses/{course_id}/exams/{exam_id}/items/{item_id}", response_model=None,
    summary="Remove an item (If-Match)",
    **wire.mutation(Exam, wire.EMPTY, conflicts="exam_not_draft"),
)
async def remove_item(course_id: ResourceId, exam_id: ResourceId, item_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        require_empty_body(await read_optional_body(request, state.settings))
        view = await run_in_threadpool(builder.remove_item, state.session_factory, state.settings, user, course,
                                       exam_id_of(exam_id), item_id_of(item_id), expected)  # fmt: skip
        return _exam_response(200, view)

    return await guarded(run, "exams.remove_item")


@router.post(
    "/courses/{course_id}/exams/{exam_id}/fill-from-blueprint", response_model=None,
    summary="Add matching approved questions (If-Match)",
    **wire.mutation(FillResponse, wire.FILL, invalid="invalid_blueprint", conflicts="exam_not_draft export_budget_exceeded"),
)
async def fill(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        body = await read_body(request, state.settings)
        require_exact_keys(body, frozenset({"row_ids"}))
        raw = body["row_ids"]
        if raw is not None and (not isinstance(raw, list) or len(raw) > 50):
            raise ApiError(422, "invalid_blueprint", "row_ids is null or a list of blueprint row ids.",
                           extra={"row": None, "reason": "unknown_row"})  # fmt: skip
        row_ids = None if raw is None else [canonical_uuid(r) for r in raw]
        result = await run_in_threadpool(builder.fill_from_blueprint, state.session_factory, state.settings, user, course,
                                         exam_id_of(exam_id), expected, row_ids)  # fmt: skip
        return respond(200, result, FillResponse, etag(result["exam"]["version"]))

    return await guarded(run, "exams.fill")


@router.get(
    "/courses/{course_id}/exams/{exam_id}/preview", response_model=None,
    summary="The print model the renderer would use, with readiness and preview fingerprint",
    **wire.read(Preview, wire.PREVIEW),
)
async def preview(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        params = query_params(request, frozenset({"variant"}))
        variant = choice(params, "variant", ("student", "answer_key")) or "student"
        state = request.app.state
        body = await run_in_threadpool(finalize.preview, state.session_factory, state.settings, user, course,
                                       exam_id_of(exam_id), variant)  # fmt: skip
        return respond(200, body, Preview)

    return await guarded(run, "exams.preview")


@router.post(
    "/courses/{course_id}/exams/{exam_id}/finalize", response_model=None,
    summary="Finalize the reviewed draft (If-Match + preview_fingerprint)",
    **wire.mutation(Exam, wire.FINALIZE, replay=True, invalid="invalid_blueprint_override blueprint_override_not_needed",
                    conflicts="exam_not_draft exam_not_ready finalize_intent_mismatch export_budget_exceeded"),
)
async def finalize_exam(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        body = await read_optional_body(request, state.settings)
        keys = frozenset({"preview_fingerprint", "blueprint_deviation_reason"})
        if body is None:
            raise ApiError(422, "invalid_fields", "The request must contain exactly these fields: blueprint_deviation_reason, preview_fingerprint.")
        require_exact_keys(body, keys)
        fingerprint = body["preview_fingerprint"]
        if fingerprint is None:
            raise ApiError(428, "preview_fingerprint_required", "Send the preview_fingerprint of the preview you reviewed.")
        if not isinstance(fingerprint, str) or len(fingerprint) != 68 or not fingerprint.startswith("pf1:") \
                or not set(fingerprint[4:]) <= _FINGERPRINT_CHARS:  # fmt: skip
            raise ApiError(400, "invalid_preview_fingerprint", "preview_fingerprint must be the value a preview returned.")
        reason = parse_reason(body["blueprint_deviation_reason"])
        answer = await run_in_threadpool(finalize.finalize, state.session_factory, state.settings, user, course,
                                         exam_id_of(exam_id), expected, fingerprint, reason)  # fmt: skip
        return _exam_response(answer.status, answer.body, answer.headers)

    return await guarded(run, "exams.finalize")


@router.post(
    "/courses/{course_id}/exams/{exam_id}/archive", response_model=None,
    summary="Archive a draft or finalized exam (If-Match)",
    **wire.mutation(Exam, wire.EMPTY, replay=True),
)
async def archive_exam(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        require_empty_body(await read_optional_body(request, state.settings))
        answer = await run_in_threadpool(finalize.archive, state.session_factory, state.settings, user, course,
                                         exam_id_of(exam_id), expected)  # fmt: skip
        return _exam_response(answer.status, answer.body, answer.headers)

    return await guarded(run, "exams.archive")


@router.post(
    "/courses/{course_id}/exams/{exam_id}/duplicate", response_model=None, status_code=201,
    summary="Duplicate as a new draft (Idempotency-Key)",
    **wire.mutation(DuplicateResponse, wire.DUPLICATE, keyed=True, invalid="invalid_exam",
                    conflicts="idempotency_key_reused exam_limit_reached export_budget_exceeded"),
)
async def duplicate_exam(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        key = idempotency_key(request)
        title = parse_duplicate_title(await read_body(request, state.settings))
        answer = await run_in_threadpool(finalize.duplicate, state.session_factory, state.settings, user, course,
                                         exam_id_of(exam_id), key, title)  # fmt: skip
        return respond(answer.status, answer.body, DuplicateResponse, {**answer.headers, **etag(answer.body["exam"]["version"])})

    return await guarded(run, "exams.duplicate")


@router.get(
    "/courses/{course_id}/exams/{exam_id}/history", response_model=None,
    summary="Audit events of the exam, its items and exports, newest first",
    **wire.read(AuditList, wire.PAGE),
)
async def exam_history(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        params = query_params(request, frozenset({"limit", "cursor"}))
        eid = exam_id_of(exam_id)
        body = await run_in_threadpool(bank.history, request.app.state.session_factory, user, course, "exam", eid,
                                       parse_limit(params), params.get("cursor"), "exams.history",
                                       finalize.exam_exists(course, eid))  # fmt: skip
        return respond(200, body, AuditList)

    return await guarded(run, "exams.history")


async def _file(request: Request, user: CurrentUser, course_id: str, exam_id: str, *, preview_file: bool):
    from ..exams import export

    course = await authorize(request, user, course_id)
    state = request.app.state
    settings = state.settings
    if not settings.exam_export_enabled:
        raise ApiError(503, "exam_export_unavailable", "Exam files are not available yet.")
    params = query_params(request, frozenset({"variant", "format"}))
    variant, fmt = params.get("variant"), params.get("format")
    if variant not in ("student", "answer_key") or fmt not in ("pdf", "docx"):
        raise export.invalid_export_option()
    eid = exam_id_of(exam_id)
    # Admission (A03): the rate limit and the render slot come before the snapshot, so a refused request
    # never reads the exam. Only the owner of the course gets this far, so a 429 discloses nothing.
    retry = state.exam_export_rate_limiter.hit(user.id)
    if retry is not None:
        raise export.rate_limited(retry)
    lease = state.exam_export_limiter.try_acquire(user.id)
    if lease is None:
        raise export.busy()
    # Nothing between try_acquire and `try` can raise or await. The slot covers preparation and rendering
    # and is released exactly once on every path: errors, timeouts, unexpected exceptions, cancellation.
    try:
        meta = await run_in_threadpool(export.prepare, state.session_factory, settings, user, course, eid, variant, fmt,
                                       preview_file=preview_file)  # fmt: skip
        rendered = await run_in_threadpool(export.render, fmt, meta["model"], export.limits_for(settings, fmt),
                                           timestamp=meta["timestamp"])  # fmt: skip
    except export.RenderFailed as exc:
        raise export.render_error(exc) from None
    finally:
        lease.release()
    action = "preview_file_downloaded" if preview_file else "export_downloaded"
    details = {"variant": variant, "format": fmt, "bytes": len(rendered.data), "file_sha256": rendered.sha256,
               "renderer_version": export.RENDERER_VERSION}  # fmt: skip
    if not meta["finalized"]:
        details["draft_version"] = meta["version"]
    await run_in_threadpool(export.record, state.session_factory, user, course, eid, action, meta["version"], details)
    return export.file_response(meta, rendered, variant, fmt)


@router.get(
    "/courses/{course_id}/exams/{exam_id}/export", response_model=None,
    summary="Official PDF or DOCX of a finalized exam (student version or answer key)",
    **wire.download(),
)
async def export_exam(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    return await guarded(lambda: _file(request, user, course_id, exam_id, preview_file=False), "exams.export")


@router.get(
    "/courses/{course_id}/exams/{exam_id}/preview-file", response_model=None,
    summary="Watermarked draft file (or the finalized file) for review",
    **wire.download(draft=True),
)
async def preview_file(course_id: ResourceId, exam_id: ResourceId, request: Request, user: CurrentUser = Depends(get_current_user)):
    return await guarded(lambda: _file(request, user, course_id, exam_id, preview_file=True), "exams.preview_file")
