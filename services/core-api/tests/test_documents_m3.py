"""The public API with M3 processing: publication after a confirmed commit, upload store checks,
status mapping of parsed documents, and unchanged response shapes."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.ingestion import jobs
from app.ingestion.jobs import Failure
from app.ingestion.normalize import Section
from app.main import create_app
from app.storage_identity import MARKER_NAME
from tests.api_support import DOCUMENT_ITEM_KEYS, RecordingPublisher
from tests.pdf_samples import make_pdf


@pytest.fixture
def owner(world):
    return world.user("professor", name="Owner")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


def test_upload_publishes_its_job_after_the_commit(api, world, owner, course):
    response = api.as_user(owner).upload(course, make_pdf("published"))
    assert response.status_code == 202
    body = response.json()
    assert set(body) == DOCUMENT_ITEM_KEYS | {"job_id"} and body["status"] == "queued"
    assert api.publisher.published == [uuid.UUID(body["job_id"])]
    reserved = world.scalar("SELECT last_dispatched_at FROM ingestion_jobs WHERE id = :j", j=body["job_id"])
    assert reserved is not None


def test_broker_outage_does_not_fail_the_upload(make_api, world, owner, course):
    api = make_api(publisher=RecordingPublisher(fail=True))
    response = api.as_user(owner).upload(course, make_pdf("outage"))
    assert response.status_code == 202
    row = world.rows("SELECT status, last_dispatched_at FROM ingestion_jobs WHERE id = :j", j=response.json()["job_id"])
    assert (row[0]["status"], row[0]["last_dispatched_at"]) == ("queued", None)  # the sweep will publish it


def test_publishing_can_be_switched_off(make_api, world, owner, course):
    api = make_api(publisher=None)
    assert api.as_user(owner).upload(course, make_pdf("off")).status_code == 202


def test_rolled_back_upload_is_never_published(api, world, owner, course):
    name = f"sg_test_{uuid.uuid4().hex[:8]}"
    world.execute(
        f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'simulated commit-time failure'; END $$"
    )
    world.execute(
        f"CREATE CONSTRAINT TRIGGER {name} AFTER INSERT ON ingestion_jobs DEFERRABLE INITIALLY DEFERRED "
        f"FOR EACH ROW EXECUTE FUNCTION {name}()"
    )
    try:
        response = api.as_user(owner).upload(course, make_pdf("rolled back"))
    finally:
        world.execute(f"DROP TRIGGER {name} ON ingestion_jobs")
        world.execute(f"DROP FUNCTION {name}()")
    assert response.status_code == 500
    assert api.publisher.published == []


def test_upload_with_an_unknown_commit_outcome_is_never_published(api, world, owner, course):
    class CommitLostInTransit(Session):
        def commit(self):
            raise ConnectionError("simulated: COMMIT never reached the server")

    real = api.app.state.session_factory
    api.app.state.session_factory = sessionmaker(bind=world.engine, autoflush=False, class_=CommitLostInTransit)
    try:
        response = api.as_user(owner).upload(course, make_pdf("unknown"))
    finally:
        api.app.state.session_factory = real
    assert response.status_code == 503 and response.json()["code"] == "upload_unconfirmed"
    assert api.publisher.published == []
    # the kept file is reconciliation's business; remove the leftover document if the commit did land
    world.execute("DELETE FROM documents WHERE course_id = :c", c=course)


@pytest.mark.parametrize("problem", ["unbound", "missing-marker", "other-store"])
def test_upload_store_problems_answer_503_before_the_body_is_read(make_api, world, owner, course, problem):
    api = make_api(bind=problem != "unbound")
    if problem == "unbound":
        api.upload_root.mkdir(parents=True, exist_ok=True)
    elif problem == "missing-marker":
        (api.upload_root / MARKER_NAME).unlink()
    else:
        (api.upload_root / MARKER_NAME).unlink()
        (api.upload_root / MARKER_NAME).write_text(
            f'{{"kind": "studygraph-upload-store", "storage_id": "{uuid.uuid4()}", "version": 1}}'
        )
    response = api.as_user(owner).upload(course, make_pdf("refused"))
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Uploads are temporarily unavailable. Try again later.",
        "code": "storage_unavailable",
    }
    assert api.stored_files() == [] and api.incoming_files() == []
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 0


def test_store_problems_are_not_revealed_to_callers_without_access(make_api, world, course):
    api = make_api(bind=False)
    stranger = world.user("professor", name="Stranger")
    assert api.as_user(stranger).upload(course, make_pdf("x")).status_code == 404
    assert api.as_user(None).upload(course, make_pdf("x")).status_code == 401


def test_listing_shows_parsed_documents_as_processing_never_ready(api, world, owner, course):
    api.as_user(owner)
    parsed_id = uuid.UUID(api.upload(course, make_pdf("parsed")).json()["id"])
    failed_id = uuid.UUID(api.upload(course, make_pdf("failed")).json()["id"])
    queued_id = uuid.UUID(api.upload(course, make_pdf("queued")).json()["id"])
    factory = sessionmaker(bind=world.engine)
    settings = api.settings

    def job_of(document_id):
        return world.scalar("SELECT id FROM ingestion_jobs WHERE document_id = :d AND kind = 'parse'", d=document_id)

    with factory() as session:
        parsed_claim = jobs.claim_parse(session, job_of(parsed_id), 300)
        failed_claim = jobs.claim_parse(session, job_of(failed_id), 300)
        session.commit()
    jobs.complete_parse(factory, parsed_claim, 7, [Section(0, 1, "Text")])
    jobs.record_failure(factory, failed_claim, Failure("encrypted_pdf", "x", permanent=True), settings)

    items = {item["id"]: item for item in api.list(course).json()}
    assert set(items[str(parsed_id)]) == DOCUMENT_ITEM_KEYS
    assert (items[str(parsed_id)]["status"], items[str(parsed_id)]["pages"]) == ("processing", 7)
    assert items[str(parsed_id)]["error"] is None
    assert (items[str(failed_id)]["status"], items[str(failed_id)]["error"]) == (
        "failed",
        jobs.PUBLIC_MESSAGES["encrypted_pdf"],
    )
    assert items[str(queued_id)]["status"] == "queued"
    assert world.scalar("SELECT status FROM documents WHERE id = :d", d=parsed_id) == "pending"


def test_no_endpoint_exposes_parsed_content():
    paths = create_app().openapi()["paths"]
    assert {path: sorted(methods) for path, methods in paths.items()} == {
        "/api/v1/courses/{course_id}/documents": ["get", "post"],
        # M5: course list/create/join and topics (no document content), and chat, whose
        # snippets come from verified retrieval chunks, never from document_sections.
        "/api/v1/courses": ["get", "post"],
        "/api/v1/courses/join": ["post"],
        "/api/v1/courses/{course_id}/topics": ["get"],
        "/api/v1/courses/{course_id}/chat": ["post"],
        # M7: quizzes and attempts; their citation snippets, too, come only from verified chunks.
        "/api/v1/courses/{course_id}/quizzes": ["post"],
        "/api/v1/courses/{course_id}/quizzes/{quiz_id}": ["get"],
        "/api/v1/courses/{course_id}/attempts": ["post"],
        "/health": ["get"],
        "/health/db": ["get"],
    }


def test_listing_never_includes_section_text(api, world, owner, course):
    api.as_user(owner)
    document_id = uuid.UUID(api.upload(course, make_pdf("secret section")).json()["id"])
    world.execute(
        "INSERT INTO document_sections (document_id, ordinal, text, locator) "
        "VALUES (:d, 0, 'private parsed text', '{\"page\": 1}')",
        d=document_id,
    )
    listing = api.list(course)
    assert "private parsed text" not in listing.text
    with world.engine.begin() as connection:
        connection.execute(text("DELETE FROM document_sections WHERE document_id = :d"), {"d": document_id})


@pytest.mark.parametrize("change", ["symlink-retargeted", "directory-replaced"])
def test_an_upload_cannot_verify_one_store_and_write_into_another(world, owner, course, tmp_path, change):
    """M3 review H2, through the API: after the store's root moves, uploads are refused (503)."""
    from tests.api_support import ApiHarness, bind_storage

    volume = tmp_path / "volume-a"
    bind_storage(world.engine, volume)
    (tmp_path / "api").mkdir()
    link = tmp_path / "api" / "uploads"
    os.symlink(volume, link)
    api = ApiHarness(world, tmp_path / "api")
    try:
        api.as_user(owner)
        first = api.upload(course, make_pdf("before the change"))
        assert first.status_code == 202
        if change == "symlink-retargeted":
            other = tmp_path / "volume-b"
            bind_storage(world.engine, other)  # same identity id: the marker check alone would pass
            replacement = tmp_path / "api" / "uploads.new"
            os.symlink(other, replacement)
            os.replace(replacement, link)
            watched = other
        else:
            os.rename(volume, tmp_path / "volume-a.old")
            bind_storage(world.engine, volume)  # a new directory at the same path, same id
            watched = volume
        second = api.upload(course, make_pdf("after the change"))
        assert second.status_code == 503 and second.json()["code"] == "storage_unavailable"
        stored = [p for p in watched.rglob("*.pdf")]
        assert stored == []  # nothing written into the other store
        assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 1
    finally:
        api.client.close()
