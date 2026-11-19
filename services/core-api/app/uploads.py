"""Receiving and checking an uploaded file.

Two halves:

- read_upload_form() (async) reads the multipart body of an upload request,
  after authentication and authorization have already passed. It enforces the
  request-body limit while the bytes arrive (Content-Length is checked first,
  without ever converting an oversized number, but never trusted alone),
  requires the whole multipart message up to its closing boundary, allows
  exactly one file part, closes every temporary file Starlette spooled on
  every path, and maps every failure to a clear 4xx response.
- normalize_filename(), check_declared_type() and copy_and_inspect() (sync,
  run in a worker thread) validate the file name, the declared type and the
  content while copying it into storage and hashing it.

Only PDF is accepted for now. The checks are deliberately cheap: a PDF header
at offset 0 and an end-of-file marker near the end. They reject files that
are not PDFs at all and truncated uploads; a damaged or encrypted PDF can
still pass and is detected by the real parser (M3).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from fastapi import Request
from python_multipart.exceptions import FormParserError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from .errors import ApiError

FILE_FIELD = "file"
MAX_FILENAME_CHARS = 255
CHUNK_SIZE = 1024 * 1024
# Room for the multipart framing around the file: boundaries and part headers.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_FORM_FIELDS = 4  # text fields tolerated (and ignored) next to the file
SIGNATURE_WINDOW = 1024  # bytes inspected at the start and at the end of the file

PDF_EXTENSION = ".pdf"
PDF_MEDIA_TYPE = "application/pdf"
# Declared part types accepted for a .pdf file. Browsers send application/pdf;
# application/octet-stream is what they send when they do not know the type.
# The content check below is what actually decides.
ACCEPTED_DECLARED_TYPES = frozenset({"application/pdf", "application/x-pdf", "application/octet-stream", ""})

_PDF_HEADER = re.compile(rb"^%PDF-[12]\.[0-9]")
_PDF_EOF = b"%%EOF"
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})  # control, format (bidi), surrogate, private, unassigned


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):g} MB"


def file_too_large(max_bytes: int, name: str | None = None) -> ApiError:
    subject = name or "The file"
    return ApiError(413, "file_too_large", f"{subject} is larger than {_mb(max_bytes)}.")


def invalid_multipart() -> ApiError:
    return ApiError(
        400, "invalid_multipart", "The upload could not be read. Send one PDF file in a form field named file."
    )


# --- multipart body ---------------------------------------------------------------


class _UploadStreamError(MultiPartException):
    """Raised from inside the body stream. Subclassing MultiPartException makes
    Starlette close the files it has spooled before re-raising."""

    def __init__(self, error: ApiError) -> None:
        super().__init__(error.message)
        self.error = error


class _CompleteMultiPartParser(MultiPartParser):
    """Starlette's parser, recording whether the message reached its closing boundary.

    python-multipart calls `on_end` exactly when it has read the closing
    `--boundary--`; its finalize() does not check this, and Starlette returns
    whatever parts were finished before the stream stopped. A body cut off
    after the file part (an announced next part, a half-sent text field, a
    closing boundary without its hyphens) is therefore detected here.
    """

    complete = False

    def on_end(self) -> None:
        super().on_end()
        self.complete = True


def _check_declared_length(request: Request, body_limit: int, max_file_bytes: int) -> None:
    """Reject an invalid or too large Content-Length before reading the body.

    Content-Length is 1*DIGIT (leading zeros allowed). The value is compared
    by its number of significant digits first, so a huge value is refused
    without converting it (int() refuses strings over 4300 digits). The header
    is only a hint for early rejection: the body is counted as it arrives.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return
    value = raw.strip()
    if not value or not value.isascii() or not value.isdigit():
        raise ApiError(400, "invalid_content_length", "The request has an invalid Content-Length header.")
    significant = value.lstrip("0") or "0"
    if len(significant) > len(str(body_limit)) or int(significant) > body_limit:
        raise file_too_large(max_file_bytes)


async def _limited_stream(request: Request, limit: int, max_file_bytes: int) -> AsyncIterator[bytes]:
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise _UploadStreamError(file_too_large(max_file_bytes))
            yield chunk
    except ClientDisconnect:
        raise _UploadStreamError(
            ApiError(400, "upload_interrupted", "The upload was interrupted before it finished.")
        ) from None


@asynccontextmanager
async def read_upload_form(request: Request, *, max_file_bytes: int) -> AsyncIterator[UploadFile]:
    """Parse the body and yield the single UploadFile sent as `file`.

    Every temporary file created while parsing is closed when the context
    exits, whatever happened.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise ApiError(415, "unsupported_media_type", "Send the file as multipart/form-data in a field named file.")

    body_limit = max_file_bytes + MULTIPART_OVERHEAD_BYTES
    _check_declared_length(request, body_limit, max_file_bytes)  # before reading any of the body

    parser = _CompleteMultiPartParser(
        request.headers,
        _limited_stream(request, body_limit, max_file_bytes),
        max_files=1,
        max_fields=MAX_FORM_FIELDS,
    )
    try:
        try:
            form = await parser.parse()
        except _UploadStreamError as exc:
            raise exc.error from None
        except (MultiPartException, FormParserError):
            # Malformed body (python-multipart's own errors are not wrapped by
            # Starlette), a second file part, too many fields, an oversized text field.
            raise invalid_multipart() from None
        if not parser.complete:
            raise ApiError(400, "upload_incomplete", "The upload ended before it was complete. Try again.")
        values = form.getlist(FILE_FIELD)
        if len(values) > 1:
            raise invalid_multipart()
        if not values or not isinstance(values[0], UploadFile):
            # No `file` part, or a plain text field instead of a file part.
            raise ApiError(422, "file_required", "Choose a PDF file to upload.")
        yield values[0]
    finally:
        # Starlette closes its spooled files only for MultiPartException; this
        # also covers success, truncated bodies and any other exception.
        for spooled in getattr(parser, "_files_to_close_on_error", ()):
            spooled.close()


# --- name, type and content checks -------------------------------------------------------


def normalize_filename(raw: str | None) -> str:
    """The client's file name, made safe to store and display.

    Keeps only the last path component, normalizes Unicode (NFC), drops
    control, format (including bidirectional overrides), surrogate, private-use
    and unassigned characters, collapses whitespace and shortens long names
    while keeping the extension. The result is metadata only; it is never used
    as a path.
    """
    if not raw:
        raise ApiError(422, "invalid_filename", "The file has no name. Rename it and upload it again.")
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _DROPPED_CATEGORIES)
    name = " ".join(name.split())
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    if not stem.strip(" ."):
        raise ApiError(422, "invalid_filename", "The file name is not valid. Rename the file and upload it again.")
    if len(name) > MAX_FILENAME_CHARS:
        suffix = f".{extension}" if dot and len(extension) < 16 else ""
        name = stem[: MAX_FILENAME_CHARS - len(suffix)].rstrip() + suffix
    return name


def check_declared_type(name: str, declared_content_type: str | None) -> str:
    """Accept PDF by extension and declared type; return the stored MIME type."""
    declared = (declared_content_type or "").split(";", 1)[0].strip().lower()
    if not name.lower().endswith(PDF_EXTENSION) or declared not in ACCEPTED_DECLARED_TYPES:
        raise ApiError(415, "unsupported_file_type", f"{name} is not a supported type. Upload a PDF file.")
    return PDF_MEDIA_TYPE


class _Sink(Protocol):
    def write(self, data: bytes) -> None: ...


@dataclass(frozen=True)
class FileFacts:
    size_bytes: int
    sha256: str


def copy_and_inspect(source: BinaryIO, sink: _Sink, *, name: str, max_bytes: int) -> FileFacts:
    """Stream source into sink in bounded chunks, counting, hashing and checking it is a PDF."""
    digest = hashlib.sha256()
    size = 0
    head = b""
    tail = b""
    while chunk := source.read(CHUNK_SIZE):
        size += len(chunk)
        if size > max_bytes:
            raise file_too_large(max_bytes, name)
        if len(head) < SIGNATURE_WINDOW:
            head += chunk[: SIGNATURE_WINDOW - len(head)]
            if len(head) >= 8 and not _PDF_HEADER.match(head):
                raise not_a_pdf(name)  # fail fast instead of copying the rest
        tail = (tail + chunk)[-SIGNATURE_WINDOW:]
        digest.update(chunk)
        sink.write(chunk)
    if size == 0:
        raise ApiError(422, "empty_file", f"{name} is empty.")
    if not _PDF_HEADER.match(head) or _PDF_EOF not in tail:
        raise not_a_pdf(name)
    return FileFacts(size_bytes=size, sha256=digest.hexdigest())


def not_a_pdf(name: str) -> ApiError:
    return ApiError(422, "invalid_pdf", f"{name} is not a valid PDF file.")
