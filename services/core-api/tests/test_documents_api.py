"""Upload and list course documents through the real API, on the disposable database.

Each test builds its own application (create_app) with a verified session
factory, a temporary upload directory and an injected principal; see
tests/api_support.py.
"""

from __future__ import annotations

import hashlib
import re
import stat
import uuid
from datetime import datetime, timezone

import pytest

from tests.api_support import DOCUMENT_ITEM_KEYS
from tests.pdf_samples import make_pdf

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def professor(world):
    return world.user("professor", name="Dr. Ada Example")


@pytest.fixture
def course(world, professor):
    return world.course(professor)


# --- happy path ------------------------------------------------------------------------


def test_upload_returns_202_with_a_queued_frontend_document(api, world, professor, course):
    pdf = make_pdf("Lecture one")
    before = datetime.now(timezone.utc)

    response = api.as_user(professor).upload(course, pdf, filename="Lecture 01.pdf")

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == DOCUMENT_ITEM_KEYS | {"job_id"}
    assert UUID_RE.match(body["id"]) and UUID_RE.match(body["job_id"])
    assert {k: body[k] for k in DOCUMENT_ITEM_KEYS - {"id", "added_at"}} == {
        "title": "Lecture 01.pdf",
        "origin": "class",
        "kind": "notes",
        "status": "queued",
        "pages": None,
        "source_name": None,
        "url": None,
        "licence": None,
        "added_by": "Dr. Ada Example",
        "error": None,
    }
    added_at = datetime.fromisoformat(body["added_at"].replace("Z", "+00:00"))
    assert body["added_at"].endswith("Z")
    assert abs((added_at - before).total_seconds()) < 60


def test_upload_persists_metadata_file_and_exactly_one_queued_parse_job(api, world, professor, course):
    pdf = make_pdf("Stoichiometry")
    body = api.as_user(professor).upload(course, pdf, filename="Week 2.PDF").json()

    [doc] = world.rows("SELECT * FROM documents WHERE course_id = :c", c=course)
    assert str(doc["id"]) == body["id"]
    assert doc["origin"] == "class" and doc["status"] == "pending" and doc["kind"] == "notes"
    assert doc["title"] == doc["original_filename"] == "Week 2.PDF"
    assert doc["uploaded_by"] == professor.id
    assert doc["mime_type"] == "application/pdf"
    assert doc["size_bytes"] == len(pdf)
    assert doc["content_hash"] == hashlib.sha256(pdf).hexdigest()
    assert doc["storage_key"] == f"{course}/{doc['id']}.pdf"
    assert doc["page_count"] is None and doc["error_message"] is None

    stored = api.stored_files()
    assert stored == [api.upload_root / str(course) / f"{doc['id']}.pdf"]
    assert stored[0].read_bytes() == pdf
    assert stat.S_IMODE(stored[0].stat().st_mode) == 0o640
    assert api.incoming_files() == []

    jobs = world.rows("SELECT * FROM ingestion_jobs WHERE document_id = :d", d=doc["id"])
    assert len(jobs) == 1
    job = jobs[0]
    assert str(job["id"]) == body["job_id"]
    assert (job["kind"], job["status"], job["attempt_count"], job["max_attempts"]) == ("parse", "queued", 0, 3)
    assert job["started_at"] is None and job["finished_at"] is None and job["error_message"] is None


def test_list_returns_a_json_array_of_frontend_documents_newest_first(api, world, professor, course):
    api.as_user(professor)
    first = api.upload(course, make_pdf("first"), filename="first.pdf").json()
    second = api.upload(course, make_pdf("second"), filename="second.pdf").json()

    response = api.list(course)

    assert response.status_code == 200
    items = response.json()
    assert isinstance(items, list)
    assert [item["id"] for item in items] == [second["id"], first["id"]]
    for item in items:
        assert set(item) == DOCUMENT_ITEM_KEYS  # no job_id, nothing internal
        assert item["status"] == "queued"


def test_list_of_an_empty_course_is_an_empty_array(api, professor, course):
    response = api.as_user(professor).list(course)
    assert response.status_code == 200 and response.json() == []


def test_same_file_name_with_different_content_is_two_documents(api, world, professor, course):
    api.as_user(professor)
    a = api.upload(course, make_pdf("version a"), filename="Lecture.pdf")
    b = api.upload(course, make_pdf("version b"), filename="Lecture.pdf")
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.json()["id"] != b.json()["id"]
    assert len(api.stored_files()) == 2


def test_unsafe_and_long_file_names_are_stored_as_sanitized_metadata_only(api, world, professor, course):
    api.as_user(professor)
    response = api.upload(course, make_pdf("x"), filename="../../etc/‮passwd.pdf")
    assert response.status_code == 202
    assert response.json()["title"] == "passwd.pdf"

    long_name = "Chapter " + "x" * 400 + ".pdf"
    response = api.upload(course, make_pdf("y"), filename=long_name)
    assert response.status_code == 202
    assert len(response.json()["title"]) == 255 and response.json()["title"].endswith(".pdf")
    # Files are stored under server-generated names only.
    for path in api.stored_files():
        assert path.parent.name == str(course) and UUID_RE.match(path.stem)


def test_octet_stream_part_with_pdf_content_is_accepted(api, professor, course):
    response = api.as_user(professor).upload(course, make_pdf(), content_type="application/octet-stream")
    assert response.status_code == 202


# --- validation ----------------------------------------------------------------------------


def _assert_rejected(api, world, course, response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code and isinstance(body["detail"], str) and body["detail"]
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 0
    assert world.scalar("SELECT count(*) FROM ingestion_jobs") == 0
    assert api.stored_files() == [] and api.incoming_files() == []
    return body


def test_missing_file_field(api, world, professor, course):
    response = api.as_user(professor).client.post(api.path(course), files={"document": ("a.pdf", make_pdf())})
    _assert_rejected(api, world, course, response, 422, "file_required")


def test_empty_file(api, world, professor, course):
    response = api.as_user(professor).upload(course, b"", filename="Empty.pdf")
    body = _assert_rejected(api, world, course, response, 422, "empty_file")
    assert body["detail"] == "Empty.pdf is empty."


@pytest.mark.parametrize(
    "filename, content_type",
    [
        ("Slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("Notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("Notes.txt", "text/plain"),
        ("Notes.md", "text/markdown"),
    ],
)
def test_formats_other_than_pdf_are_unsupported(api, world, professor, course, filename, content_type):
    response = api.as_user(professor).upload(course, b"some content", filename=filename, content_type=content_type)
    body = _assert_rejected(api, world, course, response, 415, "unsupported_file_type")
    assert body["detail"] == f"{filename} is not a supported type. Upload a PDF file."


def test_pdf_name_with_a_mismatching_declared_type_is_unsupported(api, world, professor, course):
    response = api.as_user(professor).upload(course, make_pdf(), content_type="text/html")
    _assert_rejected(api, world, course, response, 415, "unsupported_file_type")


@pytest.mark.parametrize(
    "content",
    [b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, b"<html>not a pdf</html>", make_pdf()[:-200]],
    ids=["png-renamed", "html-renamed", "truncated-pdf"],
)
def test_content_that_is_not_a_valid_pdf_is_rejected(api, world, professor, course, content):
    response = api.as_user(professor).upload(course, content, filename="Spoof.pdf")
    body = _assert_rejected(api, world, course, response, 422, "invalid_pdf")
    assert body["detail"] == "Spoof.pdf is not a valid PDF file."


def test_file_over_the_configured_limit_is_rejected(make_api, world, professor, course):
    pdf = make_pdf(padding=8000)
    small = make_api(max_upload_bytes=len(pdf) - 1).as_user(professor)
    response = small.upload(course, pdf, filename="Big.pdf")
    _assert_rejected(small, world, course, response, 413, "file_too_large")

    exact = make_api(max_upload_bytes=len(pdf)).as_user(professor)
    assert exact.upload(course, pdf, filename="Big.pdf").status_code == 202


def test_body_far_over_the_limit_is_rejected_before_parsing(make_api, world, professor, course):
    small = make_api(max_upload_bytes=1024).as_user(professor)
    response = small.upload(course, make_pdf(padding=200_000), filename="Huge.pdf")
    body = _assert_rejected(small, world, course, response, 413, "file_too_large")
    assert body["detail"] == "The file is larger than 0.000976562 MB."


@pytest.mark.parametrize("filename", [".pdf", "‮.pdf", "..."])
def test_file_without_a_usable_name_is_rejected(api, world, professor, course, filename):
    response = api.as_user(professor).upload(course, make_pdf(), filename=filename)
    _assert_rejected(api, world, course, response, 422, "invalid_filename")


def test_part_with_an_empty_file_name_is_not_a_file(api, world, professor, course):
    # Without a filename the multipart part is a text field, so no file was sent.
    response = api.as_user(professor).upload(course, make_pdf(), filename="")
    _assert_rejected(api, world, course, response, 422, "file_required")


# --- duplicates ------------------------------------------------------------------------------


def test_duplicate_in_the_same_course_is_409_with_the_existing_document(api, world, professor, course):
    api.as_user(professor)
    pdf = make_pdf("dup")
    first = api.upload(course, pdf, filename="Original.pdf").json()

    response = api.upload(course, pdf, filename="Copy with another name.pdf")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "This file is already in this course as Original.pdf.",
        "code": "duplicate_document",
        "document_id": first["id"],
    }
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 1
    assert world.scalar("SELECT count(*) FROM ingestion_jobs") == 1
    assert len(api.stored_files()) == 1 and api.incoming_files() == []


def test_same_file_in_another_course_is_accepted_without_revealing_the_first(api, world, professor, course):
    other_professor = world.user("professor", name="Dr. Other")
    other_course = world.course(other_professor, name="Organic Chemistry")
    pdf = make_pdf("shared")
    api.as_user(other_professor).upload(other_course, pdf, filename="Secret title.pdf")

    response = api.as_user(professor).upload(course, pdf, filename="Mine.pdf")

    assert response.status_code == 202
    assert "Secret" not in response.text and str(other_course) not in response.text
    assert len(api.stored_files()) == 2


# --- listing: mappings and scope -------------------------------------------------------------


def test_list_maps_internal_states_to_public_ones(api, world, professor, course):
    api.as_user(professor)
    running_id = api.upload(course, make_pdf("running"), filename="running.pdf").json()["id"]
    world.execute(
        "UPDATE ingestion_jobs SET status = 'running', attempt_count = 1, started_at = now() WHERE document_id = :d",
        d=running_id,
    )
    ready = world.document(course, title="ready.pdf", status="indexed", kind="slides", page_count=12)
    failed = world.document(course, title="failed.pdf", status="failed", error_message="This PDF is encrypted.")
    stale = world.document(
        course, origin="web_cache", title="Cached page", status="stale", url="https://example.org/a", licence="CC BY"
    )
    external = world.document(course, origin="external", title="OpenStax 4.4", status="indexed")
    legacy = world.document(course, title="legacy.pdf")  # no upload metadata, no job

    items = {item["id"]: item for item in api.list(course).json()}

    assert items[running_id]["status"] == "processing"
    assert (items[str(ready)]["status"], items[str(ready)]["kind"], items[str(ready)]["pages"]) == ("ready", "slides", 12)
    assert (items[str(failed)]["status"], items[str(failed)]["error"]) == ("failed", "This PDF is encrypted.")
    assert (items[str(stale)]["origin"], items[str(stale)]["status"]) == ("web", "failed")
    assert items[str(stale)]["error"] == "The original source is no longer available."
    assert (items[str(stale)]["url"], items[str(stale)]["licence"]) == ("https://example.org/a", "CC BY")
    assert (items[str(external)]["origin"], items[str(external)]["kind"]) == ("external", "article")
    assert (items[str(legacy)]["status"], items[str(legacy)]["kind"], items[str(legacy)]["added_by"]) == (
        "queued",
        "notes",
        None,
    )


def test_list_only_returns_this_courses_documents(api, world, professor, course):
    other_course = world.course(professor, name="Other")
    world.document(other_course, title="elsewhere.pdf")
    world.document(None, origin="external", title="Global page", status="indexed")
    mine = world.document(course, title="mine.pdf")

    items = api.as_user(professor).list(course).json()

    assert [item["id"] for item in items] == [str(mine)]


def test_legacy_naive_created_at_is_presented_as_utc(api, world, professor, course):
    world.document(course, title="legacy.pdf", created_at=datetime(2026, 11, 1, 9, 30, 0))
    [item] = api.as_user(professor).list(course).json()
    assert item["added_at"] == "2026-11-01T09:30:00Z"


def test_responses_never_expose_storage_details(api, world, professor, course):
    api.as_user(professor)
    upload = api.upload(course, make_pdf("private"), filename="private.pdf")
    listing = api.list(course)
    stored = api.stored_files()[0]
    sha = hashlib.sha256(make_pdf("private")).hexdigest()
    for text in (upload.text, listing.text):
        for secret in (str(api.upload_root), stored.name, sha, "storage_key", "mime_type", "size_bytes"):
            assert secret not in text


# --- API surface -----------------------------------------------------------------------------


def test_document_routes_and_openapi_contract(api):
    schema = api.app.openapi()
    path = schema["paths"]["/api/v1/courses/{course_id}/documents"]
    assert set(path) == {"get", "post"}
    body = path["post"]["requestBody"]
    assert body["required"] is True
    multipart = body["content"]["multipart/form-data"]["schema"]
    assert multipart["required"] == ["file"]
    assert multipart["properties"]["file"] == {
        "type": "string",
        "format": "binary",
        "description": "One PDF file (25 MiB by default, see MAX_UPLOAD_BYTES).",
    }
    assert "202" in path["post"]["responses"]
    assert path["get"]["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
    assert path["post"]["security"] == path["get"]["security"] == [{"HTTPBearer": []}]
    # No legacy /documents/upload route and nothing outside /api/v1 besides health.
    paths = set(schema["paths"])
    assert "/documents/upload" not in paths
    assert {p for p in paths if not p.startswith("/api/v1/")} == {"/health", "/health/db"}


def test_unknown_course_id_formats(api, professor):
    api.as_user(professor)
    assert api.list(uuid.uuid4()).status_code == 404
    assert api.client.get("/api/v1/courses/not-a-uuid/documents").status_code == 422
