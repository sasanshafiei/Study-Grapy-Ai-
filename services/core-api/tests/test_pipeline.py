"""One parse attempt end to end, in process: real PostgreSQL, real store, real extractor subprocess.

Covers the classification of failures (permanent / transient / not claimed)
and the storage identity rule: a worker whose store cannot be established
claims nothing and consumes no attempt.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.ingestion import pipeline
from app.ingestion.extraction import extract_pdf
from app.ingestion.jobs import PUBLIC_MESSAGES
from app.ingestion.pipeline import WorkerContext
from app.storage import LocalFileStorage
from app.storage_identity import MARKER_NAME
from tests.api_support import bind_storage
from tests.pdf_samples import Blank, ImageOnly, build_pdf, encrypt_pdf, malformed_pdf, text_page
from tests.worker_support import add_stored_document, document_row, index_jobs, job_row, sections_of

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the extractor needs Linux")
FAKE = Path(__file__).with_name("fake_extractor.py")


@pytest.fixture
def owner(world):
    return world.user("professor")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


@pytest.fixture
def store(world, tmp_path) -> LocalFileStorage:
    bind_storage(world.engine, tmp_path / "store")
    return LocalFileStorage(tmp_path / "store")


@pytest.fixture
def ctx(world, store) -> WorkerContext:
    settings = Settings(upload_dir=store.configured_root, retry_backoff_base_seconds=0, retry_backoff_max_seconds=0)
    return WorkerContext(session_factory=sessionmaker(bind=world.engine), storage=store, settings=settings)


@pytest.fixture
def add(world, owner, course, store):
    def add(data: bytes, **kwargs):
        return add_stored_document(world.engine, world, store, course, owner, data, **kwargs)

    return add


def test_successful_attempt_produces_page_aware_sections(ctx, add, world):
    data = build_pdf(
        [text_page("Stoichiometry", "H₂O → 2H₂ + O₂"), Blank(), ImageOnly(),
         text_page("ﬁnal  page  text"), Blank()]
    )  # fmt: skip
    doc = add(data)
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert result.outcome == "succeeded"
    sections = sections_of(world.engine, doc.document_id)
    assert [(s["ordinal"], s["locator"], s["text"]) for s in sections] == [
        (0, {"page": 1}, "Stoichiometry\nH₂O → 2H₂ + O₂"),
        (1, {"page": 4}, "final page text"),
    ]
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["page_count"]) == ("pending", 5)  # parsed, not indexed
    assert [j["status"] for j in index_jobs(world.engine, doc.document_id)] == ["queued"]
    # A duplicate delivery after success claims nothing and changes nothing.
    assert pipeline.run_parse_job(doc.job_id, ctx).outcome == "not_claimed"
    assert sections_of(world.engine, doc.document_id) == sections


@pytest.mark.parametrize(
    "data, code",
    [
        (encrypt_pdf(build_pdf([text_page("secret")])), "encrypted_pdf"),
        (malformed_pdf(), "invalid_pdf"),
        (build_pdf([ImageOnly(), Blank()]), "no_text"),
    ],
    ids=["encrypted", "malformed", "scanned-like"],
)
def test_document_problems_fail_permanently_with_a_safe_message(ctx, add, world, data, code):
    doc = add(data)
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert (result.outcome, result.code) == ("failed", code)
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_count"]) == ("failed", 1) and row["error_message"].startswith(code)
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["error_message"]) == ("failed", PUBLIC_MESSAGES[code])
    assert sections_of(world.engine, doc.document_id) == [] and index_jobs(world.engine, doc.document_id) == []


def test_page_limit_is_a_permanent_failure(add, world, store):
    ctx = WorkerContext(
        session_factory=sessionmaker(bind=world.engine),
        storage=store,
        settings=Settings(upload_dir=store.configured_root, parse_max_pages=2),
    )
    doc = add(build_pdf([text_page("p")] * 3))
    assert pipeline.run_parse_job(doc.job_id, ctx).code == "too_many_pages"


def test_missing_file_in_a_verified_store_is_a_permanent_failure(ctx, add, world, store):
    doc = add(build_pdf([text_page("gone")]))
    os.unlink(store.path_for(doc.key))
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert (result.outcome, result.code) == ("failed", "file_unavailable")
    assert document_row(world.engine, doc.document_id)["error_message"] == PUBLIC_MESSAGES["file_unavailable"]


def test_changed_file_fails_the_integrity_check(ctx, add, world, store):
    doc = add(build_pdf([text_page("original")]))
    path = store.path_for(doc.key)
    os.chmod(path, 0o640)
    path.write_bytes(build_pdf([text_page("tampered")]))
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert (result.outcome, result.code) == ("failed", "file_unavailable")
    assert "SHA-256" in job_row(world.engine, doc.job_id)["error_message"] or "size" in job_row(world.engine, doc.job_id)["error_message"]


def test_symlinked_stored_file_is_never_followed(ctx, add, world, store, tmp_path):
    doc = add(build_pdf([text_page("real")]))
    path = store.path_for(doc.key)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(path.read_bytes())
    os.unlink(path)
    os.symlink(outside, path)
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert (result.outcome, result.code) == ("failed", "file_unavailable")
    assert "not a regular file" in job_row(world.engine, doc.job_id)["error_message"]


@pytest.mark.parametrize("problem", ["missing-marker", "other-store", "malformed-marker", "not-mounted", "unbound-database"])
def test_store_identity_problems_claim_nothing_and_consume_no_attempt(ctx, add, world, store, tmp_path, problem):
    doc = add(build_pdf([text_page("fine")]))
    marker = store.configured_root / MARKER_NAME
    if problem == "missing-marker":
        os.unlink(marker)
    elif problem == "other-store":
        os.unlink(marker)
        marker.write_text(f'{{"kind": "studygraph-upload-store", "storage_id": "{uuid.uuid4()}", "version": 1}}')
    elif problem == "malformed-marker":
        os.chmod(marker, 0o640)
        marker.write_text("{")
    elif problem == "not-mounted":
        ctx.storage = LocalFileStorage(tmp_path / "empty-volume")
    else:
        world.execute("DELETE FROM storage_identity")
    for _ in range(5):  # however often the message arrives
        result = pipeline.run_parse_job(doc.job_id, ctx)
        assert result.outcome == "storage_unavailable"
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_count"]) == ("queued", 0)
    assert document_row(world.engine, doc.document_id)["status"] == "pending"


def test_store_disappearing_after_the_claim_is_transient(ctx, add, world, store, monkeypatch):
    doc = add(build_pdf([text_page("x")]))
    real_open = store.open_stored

    def unmount_then_open(key):
        os.unlink(store.configured_root / MARKER_NAME)  # the mount went away mid-attempt
        os.unlink(store.path_for(key))
        return real_open(key)

    monkeypatch.setattr(store, "open_stored", unmount_then_open)
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert (result.outcome, result.code) == ("requeued", "storage_unavailable")
    assert document_row(world.engine, doc.document_id)["status"] == "pending"


def test_extractor_crash_is_retried_then_succeeds(ctx, add, world, tmp_path):
    doc = add(build_pdf([text_page("eventually")]))
    command = (sys.executable, "-I", str(FAKE), "crash:1", str(tmp_path))
    ctx.extract = lambda fd, limits: extract_pdf(fd, limits, command=command)
    first = pipeline.run_parse_job(doc.job_id, ctx)
    assert (first.outcome, first.code) == ("requeued", "extractor_crashed")
    second = pipeline.run_parse_job(doc.job_id, ctx)
    assert second.outcome == "succeeded"
    assert job_row(world.engine, doc.job_id)["attempt_count"] == 2
    assert [s["text"] for s in sections_of(world.engine, doc.document_id)] == ["eventually"]


def test_repeated_transient_failures_exhaust_the_attempts(ctx, add, world, tmp_path):
    doc = add(build_pdf([text_page("never")]))
    command = (sys.executable, "-I", str(FAKE), "crash:99", str(tmp_path))
    ctx.extract = lambda fd, limits: extract_pdf(fd, limits, command=command)
    outcomes = [pipeline.run_parse_job(doc.job_id, ctx).outcome for _ in range(4)]
    assert outcomes == ["requeued", "requeued", "failed", "not_claimed"]
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["error_message"]) == ("failed", PUBLIC_MESSAGES["retries_exhausted"])


def test_unexpected_error_is_retried_not_fatal(ctx, add, world):
    doc = add(build_pdf([text_page("x")]))

    def broken(fd, limits):
        raise RuntimeError("a bug")

    ctx.extract = broken
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert (result.outcome, result.code) == ("requeued", "unexpected_error")


def test_document_that_is_no_longer_pending_is_left_alone(ctx, add, world):
    doc = add(build_pdf([text_page("x")]))
    world.execute("UPDATE documents SET status = 'indexed' WHERE id = :d", d=doc.document_id)
    result = pipeline.run_parse_job(doc.job_id, ctx)
    assert (result.outcome, result.code) == ("failed", "document_not_pending")
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["error_message"]) == ("indexed", None)


def test_a_store_whose_root_moved_after_first_use_claims_nothing(add, world, store, tmp_path):
    """M3 review H2: verification and file access always use the pinned root."""
    link = tmp_path / "configured-root"
    os.symlink(store.configured_root, link)
    pinned = LocalFileStorage(link)
    _ = pinned.root
    other = tmp_path / "other-volume"
    bind_storage(world.engine, other)  # a second directory carrying the same identity
    replacement = tmp_path / "configured-root.new"
    os.symlink(other, replacement)
    os.replace(replacement, link)
    doc = add(build_pdf([text_page("x")]))
    ctx = WorkerContext(
        session_factory=sessionmaker(bind=world.engine), storage=pinned, settings=Settings(upload_dir=link)
    )
    assert pipeline.run_parse_job(doc.job_id, ctx).outcome == "storage_unavailable"
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_count"]) == ("queued", 0)
