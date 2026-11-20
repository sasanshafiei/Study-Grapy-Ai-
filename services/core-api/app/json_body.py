"""Strict, bounded JSON request bodies for the M5 endpoints.

Handlers that use this take the raw Request (no body parameter), so FastAPI
does not read the body before the caller is authenticated and authorized;
the handler calls read_json_object() only afterwards.

Checks, in order, before and while reading:
- Content-Type must be application/json (an optional charset must be utf-8);
- Content-Encoding is refused unless absent or `identity` (no decompression);
- a declared Content-Length must be a single decimal number within the limit;
- the bytes actually received are counted while streaming and the read stops
  as soon as they exceed the limit (a missing or false Content-Length does
  not help), and a body shorter or longer than its Content-Length is refused;
- the whole read must finish within `timeout` seconds (slow senders).
Parsing: strict UTF-8, no repeated object keys, no NaN/Infinity, and the top
level must be an object. The caller then checks the exact key set and types.
"""

from __future__ import annotations

import json

import anyio
from fastapi import Request
from starlette.requests import ClientDisconnect

from .errors import ApiError

_DIGITS = frozenset("0123456789")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def strict_loads(data: bytes) -> object:
    """Parse JSON strictly. Raises ValueError (UnicodeDecodeError is one) on any problem."""
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None


def invalid_body(message: str = "The request body is not valid JSON.") -> ApiError:
    return ApiError(400, "invalid_body", message)


def _check_headers(request: Request, max_bytes: int) -> int | None:
    media = request.headers.getlist("content-type")
    if len(media) != 1:
        raise ApiError(415, "unsupported_media_type", "Send the request as JSON.")
    media_type, _, params = media[0].partition(";")
    if media_type.strip().lower() != "application/json":
        raise ApiError(415, "unsupported_media_type", "Send the request as JSON.")
    for param in filter(None, (p.strip() for p in params.split(";"))):
        name, _, value = param.partition("=")
        if name.strip().lower() != "charset" or value.strip().strip('"').lower() != "utf-8":
            raise ApiError(415, "unsupported_media_type", "Send the request as UTF-8 JSON.")
    encodings = request.headers.getlist("content-encoding")
    if any(e.strip().lower() not in ("", "identity") for e in encodings):
        raise ApiError(415, "unsupported_encoding", "Compressed request bodies are not accepted.")
    lengths = request.headers.getlist("content-length")
    if not lengths:
        return None
    if len(lengths) != 1 or not lengths[0] or not set(lengths[0]) <= _DIGITS or len(lengths[0]) > 12:
        raise invalid_body("The request has an invalid Content-Length.")
    declared = int(lengths[0])
    if declared > max_bytes:
        raise ApiError(413, "body_too_large", "The request is too large.")
    return declared


async def read_json_object(request: Request, *, max_bytes: int, timeout: float) -> dict:
    declared = _check_headers(request, max_bytes)
    body = bytearray()
    try:
        with anyio.fail_after(timeout):
            async for chunk in request.stream():
                body += chunk
                if len(body) > max_bytes:
                    raise ApiError(413, "body_too_large", "The request is too large.")
    except TimeoutError:
        raise ApiError(408, "request_timeout", "The request took too long to arrive.") from None
    except ClientDisconnect:
        raise invalid_body("The request ended before its body was complete.") from None
    if declared is not None and len(body) != declared:
        raise invalid_body("The request body does not match its Content-Length.")
    try:
        value = strict_loads(bytes(body))
    except ValueError:
        raise invalid_body() from None
    if not isinstance(value, dict):
        raise invalid_body("The request body must be a JSON object.")
    return value


def require_keys(body: dict, keys: frozenset[str]) -> None:
    if set(body) != keys:
        raise ApiError(
            422, "invalid_fields", f"The request must contain exactly these fields: {', '.join(sorted(keys))}."
        )
