"""POST /api/v1/courses/{course_id}/chat (CE-2's contract; protocol in docs/ai-chat-integration.md).

Order of checks, each before the next step:
1. authenticate (dependency) and authorize course read access (database);
2. CHAT_ENABLED and a configured AI client, else 503 (nothing is read or sent);
3. read the body (actual bytes counted, time bounded) and validate it
   strictly: exactly {question, scope}, the question text, a known scope that
   is enabled (never substituted);
4. take a global and a per-user concurrency slot, else 429 at once;
5. answer with the event stream (app/chat/stream.py), which re-authorizes
   before each upstream request and before verifying and emitting the answer.
Errors up to step 4 are ordinary JSON errors with an HTTP status; once the
stream is open, a failure is a single `error` event.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from ..auth import CurrentUser, get_current_user
from ..chat.db import authorize_read
from ..chat.protocol import Limits, has_forbidden_controls
from ..chat.scopes import SCOPES, UNAVAILABLE_SCOPES, parse_enabled_scopes
from ..chat.stream import ChatJob, ChatStream, run_chat
from ..errors import ApiError
from ..json_body import read_json_object, require_keys
from ..schemas.documents import ErrorBody
from ..schemas.openapi import BEARER, RETRY_AFTER, enum, header, validation_error

router = APIRouter(prefix="/api/v1", tags=["chat"])

CHAT_KEYS = frozenset({"question", "scope"})
DEADLINE_MARGIN_SECONDS = 10.0
BUSY_RETRY_AFTER_SECONDS = 5

_DOC = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["question", "scope"],
                    "additionalProperties": False,
                    "properties": {
                        "question": {"type": "string", "minLength": 1, "description": "Trimmed text. Limit defaults to 2000 characters."},
                        "scope": {"type": "string", "enum": [scope for scope in SCOPES if scope not in UNAVAILABLE_SCOPES],
                                  "description": "Availability depends on enabled course chat scopes."},
                    },
                }
            }
        },
    },
    "responses": {
        "200": {
            "description": (
                "UTF-8 server-sent events. Ignore comment lines. Each data field is JSON. "
                "A token carries {text}. The terminal done carries id, role, text, citations, grounded, "
                "used_web, scope, topic_ids and created_at. A terminal error carries {message, code}. "
                "After the stream opens an error is an event within the HTTP 200 response."
            ),
            "headers": {"Cache-Control": header("Stream cache policy.", enum("no-store")),
                        "X-Accel-Buffering": header("Disable proxy buffering.", enum("no"))},
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
}
_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorBody} for status in (400, 401, 403, 404, 408, 413, 415, 422, 429, 503)
}


def validate_question(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ApiError(422, "invalid_question", "The question must be text.")
    question = value.strip()
    if not question:
        raise ApiError(422, "invalid_question", "Type a question first.")
    if len(question) > max_chars:
        raise ApiError(422, "question_too_long", f"Questions can be at most {max_chars} characters.")
    if has_forbidden_controls(question):
        raise ApiError(422, "invalid_question", "The question contains characters that are not allowed.")
    return question


def validate_scope(value: object, enabled: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in SCOPES:
        raise ApiError(422, "invalid_scope", "Choose which sources the answer may use.")
    if value in UNAVAILABLE_SCOPES:
        raise ApiError(422, "scope_unavailable", "Answers from the live web are not available yet.")
    if value not in enabled:
        raise ApiError(422, "scope_unavailable", "Answers from these sources are not available yet.")
    return value


@router.post(
    "/courses/{course_id}/chat",
    response_class=Response,
    responses={**_ERRORS, 401: {"model": ErrorBody, "headers": BEARER}, 422: validation_error(),
               429: {"model": ErrorBody, "headers": RETRY_AFTER}, 500: {"model": ErrorBody}},
    summary="Ask a question about a course (validated server-sent events)",
    openapi_extra=_DOC,
    response_model=None,
)
async def chat(course_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)):
    state = request.app.state
    settings = state.settings
    await run_in_threadpool(authorize_read, state.session_factory, user, course_id)
    client = state.chat_client
    if not settings.chat_enabled or client is None or not client.configured:
        raise ApiError(503, "chat_unavailable", "Asking questions is not available yet.")

    body = await read_json_object(
        request, max_bytes=settings.chat_max_body_bytes, timeout=settings.chat_body_timeout_seconds
    )
    require_keys(body, CHAT_KEYS)
    question = validate_question(body["question"], settings.chat_max_question_chars)
    scope = validate_scope(body["scope"], parse_enabled_scopes(settings.chat_enabled_scopes))

    lease = state.chat_limiter.try_acquire(user.id)
    if lease is None:
        raise ApiError(
            429,
            "chat_busy",
            "Too many questions are being answered right now. Wait a moment and ask again.",
            headers={"Retry-After": str(BUSY_RETRY_AFTER_SECONDS)},
        )
    try:
        job = ChatJob(
            session_factory=state.session_factory,
            client=client,
            user=user,
            course_id=course_id,
            scope=scope,
            question=question,
            limits=Limits(settings.chat_max_answer_chars, settings.chat_max_citations, settings.chat_max_topics),
            total_deadline=settings.chat_probe_deadline_seconds
            + settings.chat_answer_deadline_seconds
            + DEADLINE_MARGIN_SECONDS,
        )
        return ChatStream(lambda: run_chat(job), lease, settings.chat_delivery_deadline_seconds)
    except BaseException:
        lease.release()
        raise
