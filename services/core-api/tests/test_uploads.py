"""Upload checks and public mappings that need no database."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest

from app.errors import ApiError
from app.schemas.documents import (
    STALE_MESSAGE,
    as_utc,
    public_error,
    public_kind,
    public_origin,
    public_status,
)
from app.uploads import (
    MAX_FILENAME_CHARS,
    check_declared_type,
    copy_and_inspect,
    normalize_filename,
)
from tests.pdf_samples import make_pdf


class Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.writes = 0

    def write(self, chunk: bytes) -> None:
        self.data += chunk
        self.writes += 1


def _error(call) -> ApiError:
    with pytest.raises(ApiError) as caught:
        call()
    return caught.value


# --- file names ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Lecture 01.pdf", "Lecture 01.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\prof\\Week 3.PDF", "Week 3.PDF"),
        ("  spaced \t  out   name.pdf  ", "spaced out name.pdf"),
        ("evil\u202efdp.exe.pdf", "evilfdp.exe.pdf"),  # bidi override removed
        ("tab\x00null\x1fbell.pdf", "tabnullbell.pdf"),  # control characters removed
        ("e\u0301tude.pdf", "\u00e9tude.pdf"),  # NFC
        ("zero\u200bwidth.pdf", "zerowidth.pdf"),  # format character removed
    ],
)
def test_filename_is_normalized_to_safe_metadata(raw, expected):
    assert normalize_filename(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", ".pdf", "...pdf", "folder/", "\u202e\u202e.pdf", "\x00\x01"])
def test_filename_without_a_usable_name_is_rejected(raw):
    error = _error(lambda: normalize_filename(raw))
    assert (error.status_code, error.code) == (422, "invalid_filename")


def test_long_filename_is_shortened_keeping_the_extension():
    name = normalize_filename("a" * 400 + ".pdf")
    assert len(name) == MAX_FILENAME_CHARS
    assert name.endswith(".pdf") and name.startswith("aaaa")


# --- declared type ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, declared",
    [
        ("Notes.pdf", "application/pdf"),
        ("Notes.PDF", "application/pdf"),
        ("Notes.pdf", "Application/PDF; charset=binary"),
        ("Notes.pdf", "application/x-pdf"),
        ("Notes.pdf", "application/octet-stream"),
        ("Notes.pdf", None),
    ],
)
def test_pdf_by_extension_and_declared_type_is_accepted(name, declared):
    assert check_declared_type(name, declared) == "application/pdf"


@pytest.mark.parametrize(
    "name, declared",
    [
        ("Slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("Notes.txt", "text/plain"),
        ("Notes.pdf.exe", "application/pdf"),
        ("Notes", "application/pdf"),
        ("Notes.pdf", "text/html"),  # MIME type does not match the extension
        ("Notes.pdf", "image/png"),
    ],
)
def test_other_types_are_unsupported(name, declared):
    error = _error(lambda: check_declared_type(name, declared))
    assert (error.status_code, error.code) == (415, "unsupported_file_type")
    assert name in error.message


# --- content ---------------------------------------------------------------------------


def test_copy_hashes_counts_and_copies_in_bounded_chunks():
    pdf = make_pdf(padding=3 * 1024 * 1024)
    sink = Sink()
    facts = copy_and_inspect(io.BytesIO(pdf), sink, name="Big.pdf", max_bytes=10 * 1024 * 1024)
    assert bytes(sink.data) == pdf
    assert facts.size_bytes == len(pdf)
    assert facts.sha256 == hashlib.sha256(pdf).hexdigest()
    assert sink.writes >= 3  # streamed, not read in one piece


def test_file_at_the_limit_is_accepted_and_one_byte_more_is_not():
    pdf = make_pdf(padding=500)
    facts = copy_and_inspect(io.BytesIO(pdf), Sink(), name="Edge.pdf", max_bytes=len(pdf))
    assert facts.size_bytes == len(pdf)
    error = _error(lambda: copy_and_inspect(io.BytesIO(pdf), Sink(), name="Edge.pdf", max_bytes=len(pdf) - 1))
    assert (error.status_code, error.code) == (413, "file_too_large")


def test_empty_file_is_rejected():
    error = _error(lambda: copy_and_inspect(io.BytesIO(b""), Sink(), name="Empty.pdf", max_bytes=100))
    assert (error.status_code, error.code) == (422, "empty_file")
    assert "Empty.pdf" in error.message


@pytest.mark.parametrize(
    "content",
    [
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,  # an image renamed to .pdf
        b"<html><body>%PDF-1.4</body></html>%%EOF",  # PDF marker not at the start
        b"%PDF",  # too short
        b"%PDF-9.9\n%%EOF\n",  # not a PDF version
        b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n",  # truncated: no end-of-file marker
        b"%PDF-1.4\n" + b"x" * 5000,  # end marker missing from the tail
    ],
)
def test_content_that_is_not_a_pdf_is_rejected(content):
    error = _error(lambda: copy_and_inspect(io.BytesIO(content), Sink(), name="Fake.pdf", max_bytes=10**6))
    assert (error.status_code, error.code) == (422, "invalid_pdf")


def test_non_pdf_content_is_rejected_without_copying_the_rest():
    sink = Sink()
    content = b"MZ" + b"\x00" * (4 * 1024 * 1024)
    _error(lambda: copy_and_inspect(io.BytesIO(content), sink, name="Fake.pdf", max_bytes=10**8))
    assert sink.writes == 0


# --- public mappings ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, running, expected",
    [
        ("pending", False, "queued"),
        ("pending", True, "processing"),
        ("indexed", False, "ready"),
        ("failed", False, "failed"),
        ("stale", False, "failed"),
    ],
)
def test_status_mapping(status, running, expected):
    assert public_status(status, job_running=running) == expected


def test_stale_is_never_ready_and_carries_an_explanation():
    assert public_status("stale", job_running=False) != "ready"
    assert public_error("stale", None) == STALE_MESSAGE
    assert public_error("stale", "Removed upstream.") == "Removed upstream."
    assert public_error("failed", None) is None


def test_unknown_internal_values_are_not_silently_mapped():
    with pytest.raises(KeyError):
        public_status("archived", job_running=False)
    with pytest.raises(KeyError):
        public_origin("upload")


def test_origin_and_kind_mapping():
    assert [public_origin(o) for o in ("class", "external", "web_cache")] == ["class", "external", "web"]
    assert public_kind("slides", "class") == "slides"
    assert public_kind(None, "class") == "notes"
    assert public_kind(None, "external") == "article"
    assert public_kind(None, "web_cache") == "article"


def test_naive_timestamps_are_read_as_utc():
    naive = datetime(2026, 11, 19, 12, 0, 0)
    assert as_utc(naive) == datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    assert as_utc(aware) == datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
