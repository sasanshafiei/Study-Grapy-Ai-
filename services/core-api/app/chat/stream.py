"""The chat pipeline and its server-sent-event response.

VALIDATED SSE DELIVERY, NOT MODEL-TOKEN STREAMING. The AI protocol is a
synchronous JSON exchange. core-api waits for the complete answer, validates
and verifies it, and only then sends it to the browser as SSE `token` events
(the verified text cut into pieces) followed by one `done` event with the full
message. Nothing unverified is ever sent. On failure exactly one `error`
event is sent instead, with a message written for the user.

Framing (CE-2's parser in apps/web/src/lib/api/live.ts): LF line endings,
one `event:` and one single-line `data:` (compact JSON, ASCII-escaped, so no
raw CR/LF can occur inside it), frames separated by a blank line. The stream
opens with an SSE comment line, which the parser skips.

Resources. ChatStream owns the request's concurrency lease from the moment
the handler creates it:
- released in __call__'s `finally` on every exit: completion, error, client
  disconnect (a watcher task cancels the pipeline when http.disconnect
  arrives), server-side cancellation, and a stalled ASGI `send` (the opening
  and the frames each have a delivery deadline, CHAT_DELIVERY_DEADLINE_SECONDS,
  so a connected client that stops reading cannot hold the slot);
- released by a weakref finalizer if the response object is discarded without
  ever being called (the "generator never starts" case);
- the handler releases it itself if building the response fails.
The pipeline itself holds no database session across awaits (every database
step runs in its own short transaction in a worker thread) and every HTTP
client lives inside an `async with`.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import anyio
import anyio.lowlevel
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from ..auth import CurrentUser
from ..errors import ApiError
from ..limits import Lease
from .client import AiChatClient, ChatUpstreamError
from .db import authorize_read
from .protocol import Limits, ProtocolViolation, build_request, validate_answer
from .verify import VerificationFailed, verify_answer

log = logging.getLogger(__name__)

TOKEN_PIECE_CHARS = 48
OPENING = b": studygraph validated answer stream\n\n"
DEFAULT_DELIVERY_DEADLINE = 15.0
_PIECES = re.compile(r"\S+\s*|\s+")

MESSAGES = {
    "ai_unavailable": "The assistant is not available right now. Try again later.",
    "scope_unavailable": "Answers from these sources are not available right now. Choose class materials only.",
    "question_too_long": "The question is too long. Shorten it and ask again.",
    "timeout": "The assistant took too long to answer. Try again.",
    "rejected": "The answer could not be checked against your course materials, so it was not shown. Try asking again.",
    "access": "You no longer have access to this course.",
    "server_error": "Something went wrong while answering. Try again later.",
}


class ChatFailure(Exception):
    def __init__(self, code: str, log_reason: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.log_reason = log_reason or code


def frame(event: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("ascii")


def token_pieces(text: str, size: int = TOKEN_PIECE_CHARS) -> list[str]:
    """Cut text at whitespace into pieces of about `size` characters; "".join(pieces) == text."""
    pieces: list[str] = []
    current = ""
    for match in _PIECES.finditer(text):
        part = match.group(0)
        if current and len(current) + len(part) > size:
            pieces.append(current)
            current = ""
        current += part
    if current:
        pieces.append(current)
    return pieces


def error_frame(code: str) -> bytes:
    return frame("error", {"message": MESSAGES[code], "code": code})


@dataclass(frozen=True)
class ChatJob:
    session_factory: object
    client: AiChatClient
    user: CurrentUser
    course_id: uuid.UUID
    scope: str
    question: str
    limits: Limits
    total_deadline: float


def _verify(factory, user, course_id, scope, proposed):
    with factory() as session:
        try:
            return verify_answer(session, user, course_id, scope, proposed)  # sets its own timeouts
        finally:
            session.rollback()  # read-only snapshot: nothing to commit


def message_frames(verified, scope: str) -> list[bytes]:
    message = {
        "id": f"msg_{uuid.uuid4().hex}",
        "role": "assistant",
        "text": verified.text,
        "citations": list(verified.citations),
        "grounded": verified.grounded,
        "used_web": False,
        "scope": scope,
        "topic_ids": [str(t) for t in verified.topic_ids],
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return [frame("token", {"text": piece}) for piece in token_pieces(verified.text)] + [frame("done", message)]


async def _checkpoint() -> None:
    """Cancellation point after a threadpool call, inside the deadline scope.

    run_in_threadpool does not abandon its thread, so a deadline or a
    disconnect that arrives meanwhile is deferred until the thread returns and
    may not be delivered by the return itself. Raise it here, before anything
    else (another upstream request, an answer) can happen.
    """
    await anyio.lowlevel.checkpoint()
    if anyio.current_effective_deadline() <= anyio.current_time():
        raise TimeoutError("pipeline deadline expired")


async def run_chat(job: ChatJob) -> list[bytes]:
    """Authorize, probe, ask once, validate, re-authorize and verify; the frames to send."""
    request_id = uuid.uuid4()
    try:
        with anyio.fail_after(job.total_deadline):
            await run_in_threadpool(authorize_read, job.session_factory, job.user, job.course_id)
            await _checkpoint()
            capabilities = await job.client.capabilities()
            if not capabilities.ready:
                raise ChatFailure("ai_unavailable", capabilities.reason)
            if job.scope not in capabilities.scopes:
                raise ChatFailure("scope_unavailable", "scope_not_offered")
            if len(job.question) > capabilities.max_question_chars:
                raise ChatFailure("question_too_long")
            payload = build_request(request_id, job.course_id, job.scope, job.question, job.limits)
            await run_in_threadpool(authorize_read, job.session_factory, job.user, job.course_id)
            await _checkpoint()
            try:
                body = await job.client.answer(payload)
            except ChatUpstreamError as exc:
                code = "timeout" if exc.code in ("deadline", "timeout") else "ai_unavailable"
                raise ChatFailure(code, exc.code) from None
            try:
                proposed = validate_answer(body, payload, job.limits)
            except ProtocolViolation as exc:
                raise ChatFailure("rejected", f"protocol_{exc.reason}") from None
            try:
                verified = await run_in_threadpool(
                    _verify, job.session_factory, job.user, job.course_id, job.scope, proposed
                )
            except VerificationFailed as exc:
                raise ChatFailure("rejected", f"verification_{exc.reason}") from None
            await _checkpoint()
    except ChatFailure as failure:
        log.warning("chat_failed request_id=%s code=%s reason=%s", request_id, failure.code, failure.log_reason)
        return [error_frame(failure.code)]
    except ApiError as exc:  # access re-check failed (removed, disabled, course gone)
        log.info("chat_access_lost request_id=%s code=%s", request_id, exc.code)
        return [error_frame("access")]
    except TimeoutError:
        log.warning("chat_failed request_id=%s code=timeout reason=total_deadline", request_id)
        return [error_frame("timeout")]
    except Exception:
        log.exception("chat_failed request_id=%s code=server_error", request_id)
        return [error_frame("server_error")]
    log.info("chat_answered request_id=%s grounded=%s", request_id, verified.grounded)
    return message_frames(verified, job.scope)


STREAM_HEADERS = (
    (b"content-type", b"text/event-stream; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-accel-buffering", b"no"),
)


class ChatStream(Response):
    """ASGI response: open the event stream, run the pipeline, send its frames.

    A Response subclass (FastAPI passes Response instances through unchanged),
    but without a body or Content-Length: the stream ends when the frames do.

    Lifetime of the lease is bounded by three things, none of which needs the
    client's cooperation: the opening (start + comment) must be accepted by
    ASGI `send` within `delivery_deadline`; the pipeline has its own deadline
    (ChatJob.total_deadline); and the verified frames plus the final empty
    body must be accepted within another `delivery_deadline`. A client that
    stays connected but stops reading therefore cannot hold the slot: when a
    send stalls past its deadline the delivery is abandoned, the connection is
    left for the server to close and the lease is released. (`send` returning
    means the server accepted the bytes into its own bounded write buffer.)
    """

    def __init__(
        self,
        produce: Callable[[], Awaitable[list[bytes]]],
        lease: Lease,
        delivery_deadline: float = DEFAULT_DELIVERY_DEADLINE,
    ) -> None:
        self._lease = lease
        self._finalizer = weakref.finalize(self, lease.release)
        super().__init__(status_code=200)
        self.raw_headers = list(STREAM_HEADERS)
        self._produce = produce
        self._delivery_deadline = delivery_deadline

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with anyio.create_task_group() as group:

                async def watch_disconnect() -> None:
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            log.info("chat_client_disconnected")
                            group.cancel_scope.cancel()
                            return

                group.start_soon(watch_disconnect)
                try:
                    await self._deliver(send)
                finally:
                    group.cancel_scope.cancel()
        finally:
            self._lease.release()
            self._finalizer.detach()

    async def _deliver(self, send: Send) -> None:
        with anyio.move_on_after(self._delivery_deadline) as opening:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            await send({"type": "http.response.body", "body": OPENING, "more_body": True})
        if opening.cancelled_caught:
            log.warning("chat_delivery_timeout phase=opening")
            return
        frames = await self._produce()
        await anyio.lowlevel.checkpoint()  # a disconnect during the pipeline must not still send the answer
        with anyio.move_on_after(self._delivery_deadline) as closing:
            for chunk in frames:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        if closing.cancelled_caught:
            log.warning("chat_delivery_timeout phase=frames")
