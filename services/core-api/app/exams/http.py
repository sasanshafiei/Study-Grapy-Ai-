"""HTTP conventions of the exam endpoints: preconditions, query strings, cursors, no-store responses.

- Idempotency-Key: exactly one lower-case UUID (400 idempotency_key_required / invalid_idempotency_key).
- If-Match: exactly one strong entity tag of digits, `"7"` (428 precondition_required when missing,
  400 invalid_precondition for weak tags, lists, `*` or anything else).
- Query strings: only the documented parameters, each at most once (422 invalid_query).
- Cursors (contract §1.8): base64url (no padding, <= 512 characters) of a strict JSON object
  {"v": 1, "k": <sort key>, "id": <last id>, "b": <binding>}; the binding is the first 32 hex characters of
  SHA-256 over (endpoint, caller, course, exam, normalized filters, sort, version). A cursor only positions
  a keyset inside an already authorization-scoped query; it is not a capability and needs no signing key.
- Every response, errors included, carries Cache-Control: no-store and Pragma: no-cache; unexpected
  exceptions are logged as class + SQLSTATE only and answered with the generic 500.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import ApiError
from ..json_body import invalid_body, read_json_object
from ..quiz.diagnostics import describe, internal_error
from ..quiz.protocol import canonical_uuid

log = logging.getLogger("app.exams")

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_IF_MATCH = re.compile(r'^"(0|[1-9][0-9]{0,8})"$')
_CURSOR_CHARS = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
CURSOR_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def canonical_json(value: Any) -> bytes:
    """UTF-8, sorted keys, no insignificant whitespace (the canonical form of every M9 digest)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def idempotency_key(request: Request) -> uuid.UUID:
    values = request.headers.getlist("idempotency-key")
    if not values:
        raise ApiError(400, "idempotency_key_required", "Send an Idempotency-Key header (a new UUID per create).")
    key = canonical_uuid(values[0]) if len(values) == 1 else None
    if key is None:
        raise ApiError(400, "invalid_idempotency_key", "The Idempotency-Key must be one lower-case UUID.")
    return key


def if_match(request: Request) -> int:
    values = request.headers.getlist("if-match")
    if not values:
        raise ApiError(428, "precondition_required", 'Send If-Match: "<version>" with the version you last read.')
    match = _IF_MATCH.match(values[0].strip()) if len(values) == 1 else None
    if match is None:
        raise ApiError(400, "invalid_precondition", 'If-Match must be exactly one version, for example "7".')
    return int(match.group(1))


def version_mismatch(current: int) -> ApiError:
    return ApiError(412, "version_mismatch", "This was changed in the meantime. Reload and try again.",
                    extra={"current_version": current})  # fmt: skip


def invalid_query(message: str = "The query string has an unknown, repeated or malformed parameter.") -> ApiError:
    return ApiError(422, "invalid_query", message)


def query_params(request: Request, allowed: frozenset[str]) -> dict[str, str]:
    """Each parameter at most once, none outside `allowed`."""
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in params:
            raise invalid_query()
        params[key] = value
    return params


def parse_limit(params: dict[str, str]) -> int:
    raw = params.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    if not re.fullmatch(r"[1-9][0-9]{0,2}", raw) or not 1 <= int(raw) <= MAX_LIMIT:
        raise invalid_query(f"limit must be between 1 and {MAX_LIMIT}.")
    return int(raw)


def choice(params: dict[str, str], name: str, values: tuple[str, ...]) -> str | None:
    raw = params.get(name)
    if raw is not None and raw not in values:
        raise invalid_query(f"{name} must be one of: {', '.join(values)}.")
    return raw


def uuid_param(params: dict[str, str], name: str) -> uuid.UUID | None:
    raw = params.get(name)
    if raw is None:
        return None
    parsed = canonical_uuid(raw)
    if parsed is None:
        raise invalid_query(f"{name} must be a lower-case UUID.")
    return parsed


def _binding(endpoint: str, user_id: uuid.UUID, course_id: uuid.UUID, exam_id: uuid.UUID | None, filters: dict) -> str:
    material = [endpoint, str(user_id), str(course_id), None if exam_id is None else str(exam_id),
                {k: filters[k] for k in sorted(filters)}, "desc", CURSOR_VERSION]  # fmt: skip
    return hashlib.sha256(canonical_json(material)).hexdigest()[:32]


def invalid_cursor() -> ApiError:
    return ApiError(422, "invalid_cursor", "The cursor is not valid for this list. Start again from the first page.")


def format_key(value: datetime | int) -> str:
    if isinstance(value, int):
        return str(value)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def encode_cursor(endpoint, user_id, course_id, exam_id, filters: dict, key: datetime | int, last_id) -> str:
    document = {"v": CURSOR_VERSION, "k": format_key(key), "id": str(last_id),
                "b": _binding(endpoint, user_id, course_id, exam_id, filters)}  # fmt: skip
    return base64.urlsafe_b64encode(canonical_json(document)).decode("ascii").rstrip("=")


def decode_cursor(raw: str | None, endpoint, user_id, course_id, exam_id, filters: dict, *, integer_key: bool = False):
    """None, or (key, last_id) for a cursor issued for exactly this list."""
    if raw is None:
        return None
    if not _CURSOR_CHARS.match(raw):
        raise invalid_cursor()
    try:
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise invalid_cursor() from None
    if not isinstance(document, dict) or set(document) != {"v", "k", "id", "b"} or document["v"] != CURSOR_VERSION:
        raise invalid_cursor()
    if type(document["v"]) is not int or not all(isinstance(document[k], str) for k in ("k", "id", "b")):
        raise invalid_cursor()
    last_id = canonical_uuid(document["id"])
    if last_id is None or document["b"] != _binding(endpoint, user_id, course_id, exam_id, filters):
        raise invalid_cursor()
    if integer_key:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", document["k"]):
            raise invalid_cursor()
        return int(document["k"]), last_id
    if not _TIMESTAMP.match(document["k"]):
        raise invalid_cursor()
    try:
        key = datetime.strptime(document["k"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise invalid_cursor() from None
    return key, last_id


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


async def read_body(request: Request, settings) -> dict:
    return await read_json_object(request, max_bytes=settings.exam_max_body_bytes, timeout=settings.exam_body_timeout_seconds)


async def read_optional_body(request: Request, settings) -> dict | None:
    """None for a request without a body (no Content-Type, no or zero Content-Length); else the strict object."""
    lengths = request.headers.getlist("content-length")
    if not request.headers.getlist("content-type") and (not lengths or lengths == ["0"]):
        if request.headers.getlist("transfer-encoding"):
            raise invalid_body("Send the request as JSON.")
        return None
    return await read_body(request, settings)


def require_empty_body(body: dict | None) -> None:
    if body not in (None, {}):
        raise ApiError(422, "invalid_fields", "This request takes no fields.")


def respond(status: int, body: Any, model, headers: dict[str, str] | None = None) -> JSONResponse:
    content = model.model_validate(body).model_dump(mode="json")
    return JSONResponse(status_code=status, content=content, headers={**NO_STORE, **(headers or {})})


def etag(version: int) -> dict[str, str]:
    return {"ETag": f'"{version}"'}


async def guarded(call, endpoint: str):
    """Every error carries no-store; an unexpected exception is logged as class + SQLSTATE only.

    SQLAlchemy and Pydantic errors render statement parameters or input (stems, answer keys, explanations)
    in their message; nothing here logs a message, a traceback or a body (`from None` drops the chain).
    A lock or statement timeout is 503 exam_busy: the transaction rolled back, nothing was written.
    """
    try:
        return await call()
    except ApiError as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except StarletteHTTPException:
        raise
    except Exception as exc:
        state = describe(exc)
        if any(code in state for code in ("sqlstate=55P03", "sqlstate=57014", "sqlstate=40P01", "sqlstate=40001")):
            log.warning("exam_endpoint_busy endpoint=%s %s", endpoint, state)
            raise ApiError(503, "exam_busy", "This exam is being changed right now. Try again in a moment.",
                           headers={**NO_STORE, "Retry-After": "2"}) from None  # fmt: skip
        log.error("exam_endpoint_error endpoint=%s %s", endpoint, state)
        raise internal_error() from None


class NoStoreRoute(APIRoute):
    """Every response of an exam route carries no-store, including errors raised by dependencies (the
    authentication dependency's 401 is raised before the endpoint body runs)."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store_handler(request):
            try:
                response = await handler(request)
            except ApiError as exc:
                exc.headers = {**(exc.headers or {}), **NO_STORE}
                raise
            for name, value in NO_STORE.items():
                if name.lower() == "cache-control" and response.headers.get("cache-control", "").startswith("no-store"):
                    continue
                response.headers[name] = value
            return response

        return no_store_handler
