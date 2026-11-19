"""Reading the multipart body of an upload (app.uploads.read_upload_form). No database needed.

A tiny application exposes read_upload_form directly, and requests are sent
as raw ASGI messages where an HTTP client would normalize them (missing or
invalid Content-Length, streamed bodies, a client that disconnects).
"""

from __future__ import annotations

from tempfile import SpooledTemporaryFile

import pytest
from fastapi import FastAPI, Request
from starlette import formparsers
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser

from app.errors import ApiError, api_error_handler
from app.uploads import MULTIPART_OVERHEAD_BYTES, read_upload_form
from tests.api_support import asgi_call, multipart_body
from tests.pdf_samples import make_pdf

LIMIT = 4096  # max file bytes for these tests


def _app(max_file_bytes: int = LIMIT) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)

    @app.post("/upload")
    async def upload(request: Request):
        async with read_upload_form(request, max_file_bytes=max_file_bytes) as upload:
            data = await upload.read()
            return {"filename": upload.filename, "size": len(data), "content_type": upload.content_type}

    return app


@pytest.fixture
def spooled(monkeypatch) -> list[SpooledTemporaryFile]:
    """Every temporary file Starlette creates while parsing, to check they are all closed."""
    created: list[SpooledTemporaryFile] = []

    class Recording(SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Recording)
    return created


def _post(body: bytes, content_type: str, *, length: str | None = "auto", chunk_size: int | None = None, **kwargs):
    headers = {"content-type": content_type}
    if length == "auto":
        headers["content-length"] = str(len(body))
    elif length is not None:
        headers["content-length"] = length
    chunks = [body] if chunk_size is None else [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
    return asgi_call(_app(), "POST", "/upload", headers, chunks, **kwargs)


def test_single_file_is_read(spooled):
    pdf = make_pdf()
    body, content_type = multipart_body([("file", "Lecture.pdf", pdf, "application/pdf")])
    result = _post(body, content_type)
    assert result.status == 200, result.body
    assert result.json() == {"filename": "Lecture.pdf", "size": len(pdf), "content_type": "application/pdf"}
    assert spooled and all(f.closed for f in spooled)


def test_declared_length_over_the_limit_is_refused_before_reading_the_body(spooled):
    body, content_type = multipart_body([("file", "Big.pdf", b"x" * 10, "application/pdf")])
    result = _post(body, content_type, length=str(LIMIT + MULTIPART_OVERHEAD_BYTES + 1))
    assert result.status == 413 and result.json()["code"] == "file_too_large"
    assert result.receive_calls == 0
    assert spooled == []


def test_streamed_body_without_length_is_counted_and_refused_with_413(spooled):
    big = b"%PDF-1.4\n" + b"x" * (LIMIT + MULTIPART_OVERHEAD_BYTES)
    body, content_type = multipart_body([("file", "Big.pdf", big, "application/pdf")])
    result = _post(body, content_type, length=None, chunk_size=8192)
    assert result.status == 413, result.body
    assert result.json() == {"detail": f"The file is larger than {LIMIT / 1048576:g} MB.", "code": "file_too_large"}
    assert spooled and all(f.closed for f in spooled)


def test_understated_length_does_not_bypass_the_limit(spooled):
    """Content-Length is a hint for early rejection, never the limit itself."""
    big = b"x" * (LIMIT + MULTIPART_OVERHEAD_BYTES)
    body, content_type = multipart_body([("file", "Big.pdf", big, "application/pdf")])
    result = _post(body, content_type, length="100", chunk_size=8192)
    assert result.status == 413
    assert all(f.closed for f in spooled)


@pytest.mark.parametrize("length", ["abc", "-1", "1e3", "12 34", "١٢", "", "+12", "12,12", "0x10"])
def test_malformed_content_length_is_refused_before_reading(length):
    body, content_type = multipart_body([("file", "a.pdf", b"x", "application/pdf")])
    result = _post(body, content_type, length=length)
    assert result.status == 400 and result.json()["code"] == "invalid_content_length"
    assert result.receive_calls == 0


@pytest.mark.parametrize("digits", [21, 4300, 4500, 100_000])
def test_huge_numeric_content_length_is_413_without_converting_it(digits):
    """Python refuses int() on more than 4300 digits; the length is compared without converting it."""
    body, content_type = multipart_body([("file", "a.pdf", b"x", "application/pdf")])
    result = _post(body, content_type, length="9" * digits)
    assert result.status == 413 and result.json()["code"] == "file_too_large"
    assert result.receive_calls == 0


def test_leading_zeros_in_content_length_are_read_as_the_number(spooled):
    pdf = make_pdf()
    body, content_type = multipart_body([("file", "a.pdf", pdf, "application/pdf")])
    for length in ("0" * 5000 + str(len(body)), "000" + str(len(body))):
        result = _post(body, content_type, length=length)
        assert result.status == 200, result.body
        assert result.json()["size"] == len(pdf)
    over = "0" * 5000 + str(LIMIT + MULTIPART_OVERHEAD_BYTES + 1)
    result = _post(body, content_type, length=over)
    assert result.status == 413 and result.receive_calls == 0


@pytest.mark.parametrize("content_type", ["application/json", "application/pdf", "text/plain", ""])
def test_body_that_is_not_multipart_is_refused_unread(content_type):
    result = _post(b'{"file": "x"}', content_type)
    assert result.status == 415 and result.json()["code"] == "unsupported_media_type"
    assert result.receive_calls == 0


def test_missing_boundary_is_a_400():
    result = _post(b"whatever", "multipart/form-data")
    assert result.status == 400 and result.json()["code"] == "invalid_multipart"


@pytest.mark.parametrize(
    "body",
    [
        b"--sgboundary\r\nNoColonHere\r\n\r\ndata\r\n--sgboundary--\r\n",  # malformed part header
        b"--sgboundary\r\nContent-Disposition: form-data\r\n\r\ndata\r\n--sgboundary--\r\n",  # part without a name
        b"--wrongboundary-and-then-garbage\x00\xff",
    ],
)
def test_malformed_multipart_is_a_400_not_a_500(body, spooled):
    result = _post(body, "multipart/form-data; boundary=sgboundary")
    assert result.status in (400, 422), result.body
    assert result.json()["code"] in ("invalid_multipart", "file_required")
    assert all(f.closed for f in spooled)


def test_second_file_part_is_rejected(spooled):
    body, content_type = multipart_body(
        [("file", "a.pdf", make_pdf("a"), "application/pdf"), ("file", "b.pdf", make_pdf("b"), "application/pdf")]
    )
    result = _post(body, content_type)
    assert result.status == 400 and result.json()["code"] == "invalid_multipart"
    assert all(f.closed for f in spooled)


def test_file_sent_as_a_text_field_is_rejected():
    body, content_type = multipart_body([("file", None, b"%PDF-1.4 not a file part", None)])
    result = _post(body, content_type)
    assert result.status == 422 and result.json()["code"] == "file_required"


def test_text_field_and_file_part_both_named_file_are_rejected():
    body, content_type = multipart_body(
        [("file", None, b"text", None), ("file", "a.pdf", make_pdf(), "application/pdf")]
    )
    result = _post(body, content_type)
    assert result.status == 400 and result.json()["code"] == "invalid_multipart"


@pytest.mark.parametrize(
    "parts",
    [
        [("title", None, b"Lecture", None)],
        [("document", "a.pdf", b"%PDF-1.4", "application/pdf")],  # the web client's field name is `file`
    ],
)
def test_missing_file_field_is_a_422(parts, spooled):
    body, content_type = multipart_body(parts)
    result = _post(body, content_type)
    assert result.status == 422 and result.json()["code"] == "file_required"
    assert all(f.closed for f in spooled)


def test_too_many_text_fields_are_rejected():
    parts = [(f"f{i}", None, b"v", None) for i in range(5)] + [("file", "a.pdf", make_pdf(), "application/pdf")]
    body, content_type = multipart_body(parts)
    assert _post(body, content_type).status == 400


def test_interrupted_upload_closes_spooled_files(spooled):
    body, content_type = multipart_body([("file", "a.pdf", make_pdf(padding=3000), "application/pdf")])
    result = _post(body[: len(body) // 2], content_type, length=None, disconnect_after_chunks=True)
    assert result.status == 400 and result.json()["code"] == "upload_interrupted"
    assert spooled and all(f.closed for f in spooled)


def test_truncated_body_closes_spooled_files(spooled):
    body, content_type = multipart_body([("file", "a.pdf", make_pdf(padding=3000), "application/pdf")])
    result = _post(body[: len(body) // 2], content_type)  # ends in the middle of the file part
    assert result.status == 400 and result.json()["code"] == "upload_incomplete"
    assert spooled and all(f.closed for f in spooled)


def _file_part(pdf: bytes, boundary: str = "sgboundary") -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf + b"\r\n"


@pytest.mark.parametrize(
    "ending",
    [
        b"--sgboundary\r\n",  # a next part was announced and never sent
        b'--sgboundary\r\nContent-Disposition: form-data; name="title"\r\n\r\nLectu',  # incomplete text field
        b'--sgboundary\r\nContent-Disposition: form-da',  # incomplete part header
        b"--sgboundary-",  # closing boundary missing its second hyphen
        b"--sgboundary",  # closing boundary missing its hyphens
        b"",  # no boundary at all after the file data
    ],
    ids=["announced-part", "incomplete-text-field", "incomplete-header", "one-hyphen", "no-hyphens", "nothing"],
)
@pytest.mark.parametrize("chunked", [False, True], ids=["one-chunk", "chunked"])
def test_multipart_message_must_be_complete(spooled, ending, chunked):
    """A complete PDF part is not enough: the message must reach its closing boundary."""
    body = _file_part(make_pdf()) + ending
    result = _post(body, "multipart/form-data; boundary=sgboundary", chunk_size=7 if chunked else None)
    assert result.status == 400, result.body
    assert result.json()["code"] == "upload_incomplete"
    assert spooled and all(f.closed for f in spooled)


@pytest.mark.parametrize(
    "ending",
    [b"--sgboundary--\r\n", b"--sgboundary--", b"--sgboundary--\r\nepilogue that is ignored\r\n"],
    ids=["crlf", "no-crlf", "epilogue"],
)
@pytest.mark.parametrize("chunked", [False, True], ids=["one-chunk", "chunked"])
def test_complete_multipart_messages_are_accepted(spooled, ending, chunked):
    pdf = make_pdf()
    body = _file_part(pdf) + ending
    result = _post(body, "multipart/form-data; boundary=sgboundary", chunk_size=5 if chunked else None)
    assert result.status == 200, result.body
    assert result.json()["size"] == len(pdf)
    assert all(f.closed for f in spooled)


def test_starlette_still_tracks_spooled_files_where_read_upload_form_expects():
    """read_upload_form closes Starlette's spooled files through this attribute;
    fail loudly if a Starlette upgrade renames it."""

    async def empty():
        yield b""

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=x"}), empty())
    assert isinstance(parser._files_to_close_on_error, list)
