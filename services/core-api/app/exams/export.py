"""Official exports (E22) and watermarked preview files (E23).

Flow: owner authorization -> EXAMS_ENABLED -> EXAM_EXPORT_ENABLED -> query (variant, format) -> the render
model from one REPEATABLE READ READ ONLY snapshot -> per-professor rate limit and render slot -> the
bounded renderer subprocess -> the audit event (with a fresh FOR SHARE re-authorization) committed BEFORE
the first byte is sent -> the file.

Variant-specific sanitized models (H6): the student model is assembled from a query that selects only header
fields, numbering, points, type, stem and choice texts; answer keys, explanations, model answers, rubrics and
sources are never loaded for it, and its schema (extra="forbid") has no field that could carry them. The
child receives exactly one variant's model. A finalized exam's models read only frozen exam columns and
immutable revisions, so its content never changes; its bytes are identical for one renderer version.
"""

from __future__ import annotations

import logging
import math
from typing import Literal

from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ..errors import ApiError
from ..quiz.access import short_tx
from . import audit
from . import snapshot as snap
from .access import exam_not_found, require_exam_owner
from .builder import budget_exceeded
from .filenames import content_disposition
from .render import RENDERER_VERSION, RenderFailed, RenderLimits, render  # noqa: F401  (re-exported for the router)
from .views import ts

log = logging.getLogger("app.exams.export")

MEDIA_TYPES = {"pdf": "application/pdf",
               "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}  # fmt: skip
FILE_HEADERS = {
    "Cache-Control": "no-store, private", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox", "Referrer-Policy": "no-referrer",
}  # fmt: skip


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenderHeader(_Strict):
    course_code: str
    course_name: str
    title: str
    instructions: str | None
    duration_minutes: int | None
    question_count: int
    total_points: int


class RenderOption(_Strict):
    label: str | None
    text: str


class RenderQuestion(_Strict):
    number: int
    points: int
    type: Literal["mcq", "true_false", "short"]
    stem: str
    options: list[RenderOption] | None
    answer_lines: int | None


class StudentRenderModel(_Strict):
    """No field can carry an answer, an explanation, a source, a topic, an id or approval data."""

    variant: Literal["student"]
    header: RenderHeader
    questions: list[RenderQuestion]
    draft_version: int | None
    content_sha12: None


class RenderAnswer(_Strict):
    number: int
    points: int
    answer: str | None
    answer_text: str | None
    model_answer: str | None
    rubric: str | None
    explanation: str | None
    sources: list[str]


class AnswerKeyRenderModel(_Strict):
    variant: Literal["answer_key"]
    header: RenderHeader
    questions: list[RenderQuestion]
    answer_key: list[RenderAnswer]
    draft_version: int | None
    content_sha12: str


def invalid_export_option() -> ApiError:
    return ApiError(422, "invalid_export_option", "Choose variant=student|answer_key and format=pdf|docx.")


def not_finalized() -> ApiError:
    return ApiError(409, "exam_not_finalized", "Only a finalized exam can be exported. Finalize it first.")


def frozen_header(exam) -> dict:
    return {"course_code": exam.course_code_snapshot, "course_name": exam.course_name_snapshot, "title": exam.title,
            "instructions": exam.instructions, "duration_minutes": exam.duration_minutes,
            "question_count": exam.item_count, "total_points": exam.total_points}  # fmt: skip


def prepare(factory, settings, user, course_id, exam_id, variant: str, fmt: str, *, preview_file: bool) -> dict:
    """The validated render model and the response metadata, from one consistent read-only snapshot."""
    with short_tx(factory, read_only=True, snapshot=True) as session:
        course = require_exam_owner(session, user, course_id)
        exam = session.execute(text(f"SELECT {snap.EXAM_COLUMNS} FROM exams WHERE id = :e AND course_id = :c"),
                               {"e": exam_id, "c": course_id}).first()  # fmt: skip
        if exam is None:
            raise exam_not_found()
        finalized = exam.finalized_at is not None
        if not finalized and (not preview_file or exam.status != "draft"):
            raise not_finalized()
        meta: dict = {"finalized": finalized, "version": exam.version, "finalized_version": exam.finalized_version,
                      "content_sha256": exam.content_sha256, "title": exam.title}  # fmt: skip
        if finalized:
            header = frozen_header(exam)
            sha12 = exam.content_sha256[:12]
            meta["course_code"] = exam.course_code_snapshot
            meta["timestamp"] = ts(exam.finalized_at)
            state = snap.load_exam(session, course_id, exam_id, course, with_source=False) if variant == "answer_key" else None
        else:
            state = snap.load_exam(session, course_id, exam_id, course, with_source=False)
            size = snap.content_chars(state)
            if size > settings.exam_export_max_content_chars:
                raise budget_exceeded(size, settings.exam_export_max_content_chars)
            header = snap.header_values(state)
            sha12 = snap.content_sha256(state)[:12]
            meta["course_code"] = course.code or ""
            meta["timestamp"] = ts(exam.updated_at)
            meta["fingerprint"] = snap.digest(snap.fingerprint_input(session, exam, course))
        draft_version = None if finalized else exam.version
        if variant == "student":
            rows = snap.student_print_rows(session, exam)  # selects no answer data
            model = StudentRenderModel.model_validate({
                "variant": "student", "header": header, "draft_version": draft_version, "content_sha12": None,
                "questions": [snap.student_question(n, r.points, r.qtype, r.stem, r.choices) for n, r in enumerate(rows, 1)],
            }).model_dump(mode="json")  # fmt: skip
        else:
            full = snap.answer_key_print_model(state)
            model = AnswerKeyRenderModel.model_validate({
                "variant": "answer_key", "header": header if finalized else full["header"], "questions": full["questions"],
                "answer_key": full["answer_key"], "draft_version": draft_version, "content_sha12": sha12,
            }).model_dump(mode="json")  # fmt: skip
    meta["model"] = model
    return meta


def record(factory, user, course_id, exam_id, action: str, version: int, details: dict) -> None:
    """The export audit event, committed before the body is sent; re-authorizes with FOR SHARE locks."""
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        if session.execute(text("SELECT 1 FROM exams WHERE id = :e AND course_id = :c FOR SHARE"),
                           {"e": exam_id, "c": course_id}).first() is None:  # fmt: skip
            raise exam_not_found()
        audit.record(session, course_id=course_id, actor_id=user.id, entity_kind="exam", entity_id=exam_id, action=action,
                     entity_version=version, details=details)  # fmt: skip
        session.commit()


def limits_for(settings, fmt: str) -> RenderLimits:
    return RenderLimits.from_settings(settings, fmt)


def rate_limited(retry_after: float) -> ApiError:
    return ApiError(429, "too_many_exports", "Too many exports in a short time. Try again later.",
                    headers={"Retry-After": str(max(1, math.ceil(retry_after)))})  # fmt: skip


def busy() -> ApiError:
    return ApiError(429, "export_busy", "Another export is being prepared. Try again in a moment.", headers={"Retry-After": "2"})


def render_error(exc: RenderFailed) -> ApiError:
    log.warning("exam_export_failed kind=%s reason=%s", exc.kind, exc.reason)
    if exc.kind == "timeout":
        return ApiError(504, "export_timeout", "Preparing the file took too long.")
    return ApiError(500, "export_failed", "The file could not be prepared.")


def file_response(meta: dict, rendered, variant: str, fmt: str) -> Response:
    finalized = meta["finalized"]
    version = meta["finalized_version"] if finalized else meta["version"]
    headers = {
        **FILE_HEADERS,
        "Content-Disposition": content_disposition(meta["course_code"], meta["title"], version, variant, fmt, draft=not finalized),
        "X-Exam-Renderer-Version": RENDERER_VERSION,
    }  # fmt: skip
    if finalized:
        headers["X-Exam-Content-SHA256"] = meta["content_sha256"]
        headers["X-Exam-Finalized-Version"] = str(meta["finalized_version"])
    else:
        headers["X-Exam-Version"] = str(meta["version"])
        headers["X-Exam-Preview-Fingerprint"] = meta["fingerprint"]
    return Response(content=rendered.data, media_type=MEDIA_TYPES[fmt], headers=headers)
