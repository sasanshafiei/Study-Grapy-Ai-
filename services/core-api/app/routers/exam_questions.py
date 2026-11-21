"""Question bank endpoints (core-api exams v1, docs/exam-api.md, E1-E7 and question history).

Order of checks, each before the next step: authentication (dependency; fails closed until CE-3), owner
authorization from the database rows (an enrolled student gets 403 before any id is looked up; everyone
else 404), EXAMS_ENABLED (503 after authorization), preconditions (Idempotency-Key / If-Match), the query
string or the strictly read body, then the work. Every response carries no-store; bodies are validated
through app/schemas/exams.py.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ..auth import CurrentUser, get_current_user
from ..errors import ApiError
from ..exams import bank
from ..exams.access import authorize_owner, question_not_found
from ..exams.http import (
    NoStoreRoute,
    choice,
    etag,
    guarded,
    idempotency_key,
    if_match,
    invalid_query,
    parse_limit,
    query_params,
    read_body,
    read_optional_body,
    require_empty_body,
    respond,
    uuid_param,
)
from ..exams.validation import BLOOM_LEVELS, DIFFICULTIES, QUESTION_TYPES, parse_question
from ..exams.views import load_question
from ..quiz.protocol import canonical_uuid
from ..schemas.documents import ErrorBody
from ..schemas.exams import AuditList, ExamQuestion, ExamQuestionReplay, QuestionList, ReplaceResponse, RevisionList

router = APIRouter(prefix="/api/v1", tags=["exam-questions"], route_class=NoStoreRoute)
NIL = uuid.UUID(int=0)  # a non-canonical id names nothing: it is answered like an unknown one
_ERRORS = {status: {"model": ErrorBody} for status in (400, 401, 403, 404, 408, 409, 412, 413, 415, 422, 428, 500, 503)}


def exams_unavailable() -> ApiError:
    return ApiError(503, "exams_unavailable", "Exams are not available yet.")


async def authorize(request: Request, user: CurrentUser, course_id: str) -> uuid.UUID:
    """Owner authorization from the database, then EXAMS_ENABLED (a switch never reveals whether a course exists)."""
    course = canonical_uuid(course_id) or NIL
    state = request.app.state
    await run_in_threadpool(authorize_owner, state.session_factory, user, course)
    if not state.settings.exams_enabled:
        raise exams_unavailable()
    return course


def question_id_of(raw: str) -> uuid.UUID:
    parsed = canonical_uuid(raw)
    if parsed is None:
        raise question_not_found()
    return parsed


@router.get("/courses/{course_id}/exam-questions", responses={**_ERRORS, 200: {"model": QuestionList}},
            summary="List the course's bank questions (no answer keys)", response_model=None)  # fmt: skip
async def list_questions(course_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        params = query_params(request, frozenset({"status", "type", "difficulty", "topic_id", "bloom_level", "q", "limit", "cursor"}))
        topic = uuid_param(params, "topic_id")
        q = params.get("q")
        if q is not None and (not 1 <= len(q) <= 100 or not q.isprintable()):
            raise invalid_query("q is 1 to 100 printable characters.")
        filters = {"status": choice(params, "status", ("draft", "approved", "retired")) or "",
                   "type": choice(params, "type", QUESTION_TYPES) or "",
                   "difficulty": choice(params, "difficulty", DIFFICULTIES) or "",
                   "bloom_level": choice(params, "bloom_level", BLOOM_LEVELS) or "",
                   "topic_id": "" if topic is None else str(topic), "q": q or ""}  # fmt: skip
        body = await run_in_threadpool(bank.list_questions, request.app.state.session_factory, user, course, filters,
                                       parse_limit(params), params.get("cursor"))  # fmt: skip
        return respond(200, body, QuestionList)

    return await guarded(run, "exam_questions.list")


@router.post("/courses/{course_id}/exam-questions", status_code=201, response_model=None,
             responses={**_ERRORS, 201: {"model": ExamQuestion}, 200: {"model": ExamQuestion, "description": "Replay"}},
             summary="Create a draft bank question (Idempotency-Key)")  # fmt: skip
async def create_question(course_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        key = idempotency_key(request)
        content = parse_question(await read_body(request, state.settings))
        answer = await run_in_threadpool(bank.create_question, state.session_factory, state.settings, user, course, key, content)
        return respond(answer.status, answer.body, ExamQuestion, {**answer.headers, **etag(answer.body["version"])})

    return await guarded(run, "exam_questions.create")


@router.get("/courses/{course_id}/exam-questions/{question_id}", response_model=None,
            responses={**_ERRORS, 200: {"model": ExamQuestion}}, summary="One bank question, with its answer key")  # fmt: skip
async def read_question(course_id: str, question_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        query_params(request, frozenset())
        body = await run_in_threadpool(bank.read_question, request.app.state.session_factory, user, course, question_id_of(question_id))
        return respond(200, body, ExamQuestion, etag(body["version"]))

    return await guarded(run, "exam_questions.read")


@router.put("/courses/{course_id}/exam-questions/{question_id}", response_model=None,
            responses={**_ERRORS, 200: {"model": ReplaceResponse}}, summary="Replace a question's content (If-Match)")  # fmt: skip
async def replace_question(course_id: str, question_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        content = parse_question(await read_body(request, state.settings))
        body = await run_in_threadpool(bank.replace_question, state.session_factory, user, course,
                                       question_id_of(question_id), expected, content)  # fmt: skip
        return respond(200, body, ReplaceResponse, etag(body["question"]["version"]))

    return await guarded(run, "exam_questions.replace")


async def _transition(request, user, course_id, question_id, operation, endpoint):
    async def run():
        course = await authorize(request, user, course_id)
        state = request.app.state
        expected = if_match(request)
        require_empty_body(await read_optional_body(request, state.settings))
        answer = await run_in_threadpool(operation, state.session_factory, user, course, question_id_of(question_id), expected)
        model = ExamQuestionReplay if "replayed_revision" in answer.body else ExamQuestion
        return respond(answer.status, answer.body, model, {**answer.headers, **etag(answer.body["version"])})

    return await guarded(run, endpoint)


@router.post("/courses/{course_id}/exam-questions/{question_id}/approve", response_model=None,
             responses={**_ERRORS, 200: {"model": ExamQuestion}},
             summary="Approve the current content as a new immutable revision (If-Match)")  # fmt: skip
async def approve_question(course_id: str, question_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    return await _transition(request, user, course_id, question_id, bank.approve_question, "exam_questions.approve")


@router.post("/courses/{course_id}/exam-questions/{question_id}/retire", response_model=None,
             responses={**_ERRORS, 200: {"model": ExamQuestion}}, summary="Retire a question (If-Match)")  # fmt: skip
async def retire_question(course_id: str, question_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    return await _transition(request, user, course_id, question_id, bank.retire_question, "exam_questions.retire")


@router.get("/courses/{course_id}/exam-questions/{question_id}/revisions", response_model=None,
            responses={**_ERRORS, 200: {"model": RevisionList}}, summary="Approved revisions, newest first")  # fmt: skip
async def list_revisions(course_id: str, question_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        params = query_params(request, frozenset({"limit", "cursor"}))
        body = await run_in_threadpool(bank.list_revisions, request.app.state.session_factory, user, course,
                                       question_id_of(question_id), parse_limit(params), params.get("cursor"))  # fmt: skip
        return respond(200, body, RevisionList)

    return await guarded(run, "exam_questions.revisions")


@router.get("/courses/{course_id}/exam-questions/{question_id}/history", response_model=None,
            responses={**_ERRORS, 200: {"model": AuditList}}, summary="Audit events of a question, newest first")  # fmt: skip
async def question_history(course_id: str, question_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        course = await authorize(request, user, course_id)
        params = query_params(request, frozenset({"limit", "cursor"}))
        qid = question_id_of(question_id)

        def exists(session):
            if load_question(session, course, qid) is None:
                raise question_not_found()

        body = await run_in_threadpool(bank.history, request.app.state.session_factory, user, course, "question", qid,
                                       parse_limit(params), params.get("cursor"), "exam_questions.history", exists)  # fmt: skip
        return respond(200, body, AuditList)

    return await guarded(run, "exam_questions.history")
