"""Quiz endpoints (core-api quiz v1, docs/quiz-api.md).

POST /api/v1/courses/{course_id}/quizzes          generate (Idempotency-Key required)
GET  /api/v1/courses/{course_id}/quizzes/{quiz_id} the quiz, its state and answered feedback
POST /api/v1/courses/{course_id}/attempts          answer one assigned question

Order of checks, each before the next step: authentication (dependency),
practice authorization from the database (active student, current student
enrollment), QUIZ_ENABLED (and for attempts QUIZ_ATTEMPTS_ENABLED; for new
generations QUIZ_GENERATION_ENABLED, checked only after an existing outcome
could not be replayed), the Idempotency-Key header, then the strictly read
body. Every response, error responses included, carries Cache-Control:
no-store; bodies are validated through explicit schemas (app/schemas/quiz.py).
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
from ..errors import ApiError
from ..json_body import read_json_object
from ..quiz.access import authorize_practice
from ..quiz.attempts import AttemptRequest, submit_attempt
from ..quiz.diagnostics import describe, internal_error
from ..quiz.generation import QuizRequest, create_quiz, read_quiz
from ..quiz.protocol import REQUEST_DIFFICULTIES, canonical_uuid, parse_quiz_scopes
from ..schemas.documents import ErrorBody
from ..schemas.openapi import BEARER, LOCATION, REPLAYED, RETRY_AFTER, STRING, UUID, enum, exact, validation_error
from ..schemas.quiz import AttemptResponse, QuizResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["quizzes"])

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
QUIZ_REQUIRED = frozenset({"topic_ids", "count", "difficulty"})
QUIZ_OPTIONAL = frozenset({"scope"})
ATTEMPT_KEYS = frozenset({"quiz_id", "question_id", "answer", "hint_count", "response_ms"})
MAX_ANSWER_CHARS = 16
MAX_HINTS = 1  # M7 shows one hint per question
MAX_RESPONSE_MS = 3_600_000

_ERRORS: dict[int | str, dict[str, Any]] = {
    **{status: {"model": ErrorBody} for status in (403, 404, 500, 503)},
    401: {"model": ErrorBody, "headers": BEARER},
}
_WRITE_ERRORS = {**{s: {"model": ErrorBody} for s in (400, 408, 413, 415)}, 422: validation_error()}
_GENERATION_ERROR = {"description": "Quiz generation unavailable or failed. A failed reservation may include recovery fields.",
                     "content": {"application/json": {"schema": {
                         "type": "object", "required": ["code", "detail"],
                         "properties": {"code": STRING, "detail": STRING, "quiz_id": UUID,
                                        "status": enum("failed"), "retry": enum("new_idempotency_key")}}}}}
_QUIZ_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["topic_ids", "count", "difficulty"],
                    "additionalProperties": False,
                    "properties": {
                        "topic_ids": {
                            "type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True,
                            "items": UUID, "description": "Deployment may configure a lower topic limit.",
                        },
                        "count": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Deployment may configure a lower question limit."},
                        "difficulty": {"type": "string", "enum": list(REQUEST_DIFFICULTIES)},
                        "scope": {"type": "string", "enum": ["class_only"], "default": "class_only"},
                    },
                }
            }
        },
    },
    "parameters": [
        {"name": "Idempotency-Key", "in": "header", "required": True, "schema": UUID}
    ],
}  # fmt: skip
_ATTEMPT_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": sorted(ATTEMPT_KEYS),
                    "additionalProperties": False,
                    "properties": {
                        "quiz_id": UUID,
                        "question_id": UUID,
                        "answer": {"type": "string", "minLength": 1, "maxLength": MAX_ANSWER_CHARS},
                        "hint_count": {"type": "integer", "minimum": 0, "maximum": MAX_HINTS},
                        "response_ms": {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_RESPONSE_MS},
                    },
                }
            }
        },
    }
}


def _invalid(code: str, message: str) -> ApiError:
    return ApiError(422, code, message)


def _int(value: object, low: int, high: int, code: str, message: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _invalid(code, message)
    return value


def _quiz_unavailable() -> ApiError:
    return ApiError(503, "quiz_unavailable", "Practice quizzes are not available yet.")


def idempotency_key(request: Request) -> uuid.UUID:
    values = request.headers.getlist("idempotency-key")
    if not values:
        raise ApiError(400, "idempotency_key_required", "Send an Idempotency-Key header (a new UUID per quiz).")
    key = canonical_uuid(values[0]) if len(values) == 1 else None
    if key is None:
        raise ApiError(400, "invalid_idempotency_key", "The Idempotency-Key must be one lower-case UUID.")
    return key


def parse_quiz_request(body: dict, course_id: uuid.UUID, key: uuid.UUID, settings) -> QuizRequest:
    keys = set(body)
    if not QUIZ_REQUIRED <= keys or not keys <= QUIZ_REQUIRED | QUIZ_OPTIONAL:
        raise _invalid("invalid_fields", "The request must contain topic_ids, count and difficulty (and optionally scope).")
    raw = body["topic_ids"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= settings.quiz_max_topics:
        raise _invalid("invalid_topics", f"Choose between 1 and {settings.quiz_max_topics} topics.")
    topics = [canonical_uuid(t) for t in raw]
    if any(t is None for t in topics) or len(set(topics)) != len(topics):
        raise _invalid("invalid_topics", "Topics must be distinct topic ids.")
    count = _int(body["count"], 1, settings.quiz_max_questions, "invalid_count",
                 f"Ask for between 1 and {settings.quiz_max_questions} questions.")  # fmt: skip
    difficulty = body["difficulty"]
    if not isinstance(difficulty, str) or difficulty not in REQUEST_DIFFICULTIES:
        raise _invalid("invalid_difficulty", "Choose easy, medium, hard or mixed.")
    scope = body.get("scope", "class_only")
    if not isinstance(scope, str) or scope not in parse_quiz_scopes(settings.quiz_enabled_scopes):
        raise _invalid("scope_unavailable", "Quizzes can only use this course's materials.")
    return QuizRequest(course_id, key, tuple(sorted(topics)), count, difficulty, scope)


def parse_attempt_request(body: dict) -> AttemptRequest:
    if set(body) != ATTEMPT_KEYS:
        raise _invalid("invalid_fields", f"The request must contain exactly these fields: {', '.join(sorted(ATTEMPT_KEYS))}.")
    quiz_id, question_id = canonical_uuid(body["quiz_id"]), canonical_uuid(body["question_id"])
    if quiz_id is None or question_id is None:
        raise _invalid("invalid_ids", "quiz_id and question_id must be lower-case UUIDs.")
    answer = body["answer"]
    if not isinstance(answer, str) or not 1 <= len(answer) <= MAX_ANSWER_CHARS or not answer.isascii() or not answer.isprintable():
        raise _invalid("invalid_answer", "Choose one of the answers offered.")
    hints = _int(body["hint_count"], 0, MAX_HINTS, "invalid_hint_count", f"hint_count must be between 0 and {MAX_HINTS}.")
    response_ms = body["response_ms"]
    if response_ms is not None:
        response_ms = _int(response_ms, 0, MAX_RESPONSE_MS, "invalid_response_ms",
                           f"response_ms must be between 0 and {MAX_RESPONSE_MS}.")  # fmt: skip
    return AttemptRequest(quiz_id, question_id, answer, hints, response_ms)


def _respond(status: int, body: dict, model, headers: dict[str, str] | None = None) -> JSONResponse:
    content = model.model_validate(body).model_dump(mode="json")
    return JSONResponse(status_code=status, content=content, headers={**NO_STORE, **(headers or {})})


async def _guarded(call, endpoint: str):
    """Every error from these endpoints carries no-store as well.

    An unexpected exception never leaves this function: SQLAlchemy and Pydantic errors render the failing statement's
    parameters or the offending input (answer keys, explanations, course passages) in their message, and the server's
    generic traceback logging would print them. It is logged as class + SQLSTATE only (app/quiz/diagnostics.py) and
    answered with the generic 500 body; `from None` also keeps the original out of the traceback chain.
    """
    try:
        return await call()
    except ApiError as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except StarletteHTTPException:
        raise
    except Exception as exc:
        log.error("quiz_endpoint_error endpoint=%s %s", endpoint, describe(exc))
        raise internal_error() from None


async def _authorized_delivery(state, user: CurrentUser, course_id: uuid.UUID) -> None:
    """Authorize the student again, in a fresh short transaction, right before a sensitive body is sent."""
    await run_in_threadpool(authorize_practice, state.session_factory, user, course_id)


@router.post(
    "/courses/{course_id}/quizzes",
    responses={**_ERRORS, **_WRITE_ERRORS, **{s: _GENERATION_ERROR for s in (502, 503, 504)},
               409: {"description": "Key reused or quiz_generation_in_progress (with Retry-After).", "headers": RETRY_AFTER,
                     "content": {"application/json": {"schema": {"oneOf": [
                         exact(code=enum("idempotency_key_reused"), detail=STRING),
                         exact(code=enum("quiz_generation_in_progress"), detail=STRING, quiz_id=UUID, status=enum("generating")),
                     ]}}}},
               429: {"model": ErrorBody, "headers": RETRY_AFTER},
               200: {"model": QuizResponse, "description": "Replay of an existing quiz", "headers": {**LOCATION, **REPLAYED}},
               201: {"model": QuizResponse, "description": "Generated", "headers": LOCATION}},
    summary="Generate a practice quiz (idempotent)",
    openapi_extra=_QUIZ_BODY,
    response_model=None,
    status_code=201,
)  # fmt: skip
async def create(course_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        settings = state.settings
        await run_in_threadpool(authorize_practice, state.session_factory, user, course_id)
        if not settings.quiz_enabled:
            raise _quiz_unavailable()
        key = idempotency_key(request)
        body = await read_json_object(
            request, max_bytes=settings.quiz_max_body_bytes, timeout=settings.quiz_body_timeout_seconds
        )
        answer = await create_quiz(state, user, parse_quiz_request(body, course_id, key, settings))
        return _respond(answer.status, answer.body, QuizResponse, answer.headers)

    return await _guarded(run, "create")


@router.get(
    "/courses/{course_id}/quizzes/{quiz_id}",
    responses={**_ERRORS, 200: {"model": QuizResponse}},
    summary="A quiz of the caller: state, questions and feedback for answered questions",
    response_model=None,
)
async def read(course_id: uuid.UUID, quiz_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        await run_in_threadpool(authorize_practice, state.session_factory, user, course_id)
        if not state.settings.quiz_enabled:
            raise _quiz_unavailable()
        view = await run_in_threadpool(read_quiz, state.session_factory, user, course_id, quiz_id)
        await _authorized_delivery(state, user, course_id)
        return _respond(200, view, QuizResponse)

    return await _guarded(run, "read")


@router.post(
    "/courses/{course_id}/attempts",
    responses={**_ERRORS, **_WRITE_ERRORS,
               409: {"description": "Already answered with a different choice.", "content": {"application/json": {
                   "schema": exact(code=enum("already_answered"), detail=STRING, attempt_id=UUID)}}},
               200: {"model": AttemptResponse, "description": "Replay of the same answer"},
               201: {"model": AttemptResponse, "description": "Graded"}},
    summary="Answer one question of a practice quiz (graded locally, idempotent per question)",
    openapi_extra=_ATTEMPT_BODY,
    response_model=None,
    status_code=201,
)  # fmt: skip
async def attempt(course_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)):
    async def run():
        state = request.app.state
        settings = state.settings
        await run_in_threadpool(authorize_practice, state.session_factory, user, course_id)
        if not settings.quiz_enabled or not settings.quiz_attempts_enabled:
            raise ApiError(503, "quiz_attempts_unavailable", "Answering practice questions is not available right now.")
        body = await read_json_object(
            request, max_bytes=settings.quiz_max_body_bytes, timeout=settings.quiz_body_timeout_seconds
        )
        status, view = await run_in_threadpool(
            submit_attempt, state.session_factory, user, course_id, parse_attempt_request(body)
        )
        await _authorized_delivery(state, user, course_id)
        return _respond(status, view, AttemptResponse)

    return await _guarded(run, "attempt")
