"""Failure paths of an upload: storage, database, COMMIT outcomes and concurrency.

Real PostgreSQL behaviour is used wherever it can be produced (triggers that
fail inside the transaction or at COMMIT, a backend terminated before COMMIT,
a second connection holding the unique index entry). Only the "COMMIT
succeeded but the client saw an error" and "outcome cannot be established"
cases replace Session.commit, since a real network failure at that exact
moment cannot be produced on demand.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, pool, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import storage as storage_module
from app.services import documents
from tests.pdf_samples import make_pdf

SERVICE_LOGGER = "app.services.documents"


@pytest.fixture
def owner(world):
    return world.user("professor", name="Owner")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


def _assert_no_trace(api, world, course) -> None:
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 0
    assert world.scalar("SELECT count(*) FROM ingestion_jobs") == 0
    assert api.stored_files() == []
    assert api.incoming_files() == []


def _assert_safe_error(api, response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    for secret in (str(api.upload_root), "Traceback", "psycopg", "simulated", "sqlalchemy", ".part"):
        assert secret not in response.text


def _session_factory(api, session_class) -> None:
    api.app.state.session_factory = sessionmaker(bind=api.world.engine, autoflush=False, class_=session_class)


class _Trigger:
    """A trigger on the disposable database that exists only inside a `with` block."""

    def __init__(self, world, table: str, body: str, *, deferred: bool = False) -> None:
        self.world = world
        self.table = table
        self.name = f"sg_test_{uuid.uuid4().hex[:10]}"
        self.body = body
        self.deferred = deferred

    def __enter__(self):
        self.world.execute(
            f"CREATE FUNCTION {self.name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {self.body} END $$"
        )
        if self.deferred:
            self.world.execute(
                f"CREATE CONSTRAINT TRIGGER {self.name} AFTER INSERT ON {self.table} "
                f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {self.name}()"
            )
        else:
            self.world.execute(
                f"CREATE TRIGGER {self.name} BEFORE INSERT ON {self.table} "
                f"FOR EACH ROW EXECUTE FUNCTION {self.name}()"
            )
        return self

    def __exit__(self, *exc):
        self.world.execute(f"DROP TRIGGER IF EXISTS {self.name} ON {self.table}")
        self.world.execute(f"DROP FUNCTION IF EXISTS {self.name}()")


# --- storage ---------------------------------------------------------------------------------


def test_storage_failure_leaves_no_document_and_no_file(api, world, owner, course, monkeypatch):
    def failing_link(src, dst):
        raise OSError(28, "simulated: no space left on device")

    monkeypatch.setattr(storage_module.os, "link", failing_link)
    response = api.as_user(owner).upload(course, make_pdf())

    _assert_safe_error(api, response, 500, "storage_error")
    assert response.json()["detail"] == "The file could not be saved. Try again later."
    _assert_no_trace(api, world, course)


@pytest.mark.parametrize("failing_directory", ["root", "course"])
def test_directory_sync_failure_never_commits_and_removes_only_this_uploads_file(
    api, world, owner, course, monkeypatch, failing_directory
):
    """root: before the link (nothing placed). course: after the link (the placed file is removed).

    Either way the document is not committed, so no row can reference a file
    whose directory entry was not confirmed durable.
    """
    api.as_user(owner)
    kept = api.upload(course, make_pdf("already stored")).json()
    [kept_file] = api.stored_files()
    real = storage_module._fsync_directory

    def failing_fsync(path):
        is_root = Path(path) == api.storage.root
        if is_root == (failing_directory == "root"):
            raise OSError("simulated fsync failure")
        return real(path)

    monkeypatch.setattr(storage_module, "_fsync_directory", failing_fsync)
    response = api.upload(course, make_pdf("second"))

    _assert_safe_error(api, response, 500, "storage_error")
    assert api.stored_files() == [kept_file] and kept_file.read_bytes() == make_pdf("already stored")
    assert api.incoming_files() == []
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 1
    assert str(world.scalar("SELECT id FROM documents WHERE course_id = :c", c=course)) == kept["id"]


# --- database, before COMMIT --------------------------------------------------------------------


def test_job_insert_failure_rolls_back_the_document_too(api, world, owner, course):
    with _Trigger(world, "ingestion_jobs", "RAISE EXCEPTION 'simulated job insert failure';"):
        response = api.as_user(owner).upload(course, make_pdf())
    _assert_safe_error(api, response, 500, "upload_failed")
    _assert_no_trace(api, world, course)


def test_other_integrity_errors_are_not_reported_as_duplicates(api, world, owner, course):
    body = "RAISE EXCEPTION USING ERRCODE = 'unique_violation', CONSTRAINT = 'some_other_key', MESSAGE = 'simulated';"
    with _Trigger(world, "ingestion_jobs", body):
        response = api.as_user(owner).upload(course, make_pdf())
    _assert_safe_error(api, response, 500, "upload_failed")
    _assert_no_trace(api, world, course)


# --- COMMIT outcomes -------------------------------------------------------------------------


def test_commit_rejected_by_the_server_is_a_confirmed_rollback(api, world, owner, course, caplog):
    """A deferred constraint fails at COMMIT: PostgreSQL rolled back, so the placed file is removed."""
    body = "RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'simulated commit-time failure';"
    with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
        with _Trigger(world, "documents", body, deferred=True):
            response = api.as_user(owner).upload(course, make_pdf())
    _assert_safe_error(api, response, 500, "upload_failed")
    _assert_no_trace(api, world, course)
    assert "commit failed and was rolled back" in caplog.text


def test_backend_lost_before_commit_is_verified_as_rolled_back(api, world, owner, course):
    """The connection dies before COMMIT reaches the server; a fresh connection proves nothing was committed."""
    terminator = world.engine

    class TerminatedBeforeCommit(Session):
        def commit(self):
            pid = self.execute(text("SELECT pg_backend_pid()")).scalar_one()
            with terminator.begin() as connection:
                assert connection.execute(text("SELECT pg_terminate_backend(:pid, 10000)"), {"pid": pid}).scalar_one()
            super().commit()

    _session_factory(api, TerminatedBeforeCommit)
    response = api.as_user(owner).upload(course, make_pdf())

    _assert_safe_error(api, response, 500, "upload_failed")
    _assert_no_trace(api, world, course)


def test_commit_that_succeeded_but_raised_keeps_the_file_and_succeeds(api, world, owner, course, caplog):
    """The commit reached the database, then the client saw a connection error."""

    class CommitThenConnectionError(Session):
        def commit(self):
            super().commit()
            raise OperationalError(
                "COMMIT", {}, psycopg.OperationalError("simulated: server closed the connection unexpectedly")
            )

    _session_factory(api, CommitThenConnectionError)
    pdf = make_pdf("committed")
    with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
        response = api.as_user(owner).upload(course, pdf)

    assert response.status_code == 202, response.text  # persistence was verified, so success is reported
    document_id = response.json()["id"]
    assert str(world.scalar("SELECT id FROM documents WHERE course_id = :c", c=course)) == document_id
    assert world.scalar("SELECT count(*) FROM ingestion_jobs WHERE document_id = :d", d=document_id) == 1
    [stored] = api.stored_files()
    assert stored.name == f"{document_id}.pdf" and stored.read_bytes() == pdf
    assert "is committed; keeping its file" in caplog.text


def test_commit_with_an_undeterminable_outcome_keeps_the_file_and_reports_503(api, world, owner, course, caplog):
    """While the original backend still holds its transaction open, absence of the row proves nothing."""

    class CommitLostInTransit(Session):
        def commit(self):  # the COMMIT never got through; the server-side transaction is still open
            raise OperationalError("COMMIT", {}, psycopg.OperationalError("simulated: connection timed out"))

    _session_factory(api, CommitLostInTransit)
    with caplog.at_level(logging.ERROR, logger=SERVICE_LOGGER):
        response = api.as_user(owner).upload(course, make_pdf())

    _assert_safe_error(api, response, 503, "upload_unconfirmed")
    [orphan] = api.stored_files()  # kept for reconciliation, never deleted on an unknown outcome
    assert "upload_reconcile" in caplog.text and orphan.stem in caplog.text
    # In this simulation the transaction was finally rolled back when the session closed.
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 0
    assert api.incoming_files() == []


def test_nothing_touches_the_database_after_a_successful_commit(api, world, owner, course):
    """A failure after COMMIT must not trigger rollback-style cleanup: there is no such step."""
    statements: list[str] = []

    class Recording(Session):
        committed = False

        def commit(self):
            super().commit()
            self.committed = True

        def execute(self, *args, **kwargs):
            if self.committed:
                statements.append(str(args[0]))
            return super().execute(*args, **kwargs)

    _session_factory(api, Recording)
    assert api.as_user(owner).upload(course, make_pdf()).status_code == 202
    assert statements == []
    assert len(api.stored_files()) == 1


# --- authorization re-checked in the write transaction ------------------------------------------


@pytest.mark.parametrize(
    "change, status, code",
    [
        ("UPDATE users SET status = 'disabled' WHERE id = :owner", 403, "account_disabled"),
        ("UPDATE courses SET owner_id = :other WHERE id = :course", 404, "course_not_found"),
    ],
)
def test_access_lost_while_the_file_was_uploading_is_refused(
    api, world, owner, course, monkeypatch, change, status, code
):
    other = world.user("professor")
    original = documents.copy_and_inspect

    def copy_then_change(*args, **kwargs):
        facts = original(*args, **kwargs)
        world.execute(change, owner=owner.id, other=other.id, course=course)
        return facts

    monkeypatch.setattr(documents, "copy_and_inspect", copy_then_change)
    response = api.as_user(owner).upload(course, make_pdf())

    _assert_safe_error(api, response, status, code)
    _assert_no_trace(api, world, course)


# --- concurrency -------------------------------------------------------------------------------


def _wait_for_lock_wait(world, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if world.scalar(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ):
            return
        time.sleep(0.05)
    raise AssertionError("the upload never waited on the other transaction")


@pytest.mark.parametrize("other_outcome", ["commit", "rollback"])
def test_upload_waits_for_a_concurrent_identical_insert(api, world, owner, course, other_outcome):
    """Deterministic race: another transaction holds the dedup index entry for the same content."""
    pdf = make_pdf("race")
    other_id = uuid.uuid4()
    holder = world.engine.connect()
    holder.execute(
        text(
            "INSERT INTO documents (id, course_id, origin, title, language, status, content_hash, uploaded_by, "
            "original_filename, storage_key, mime_type, size_bytes, kind) VALUES (:id, :c, 'class', 'First.pdf', "
            "'en', 'pending', :h, :u, 'First.pdf', :k, 'application/pdf', :s, 'notes')"
        ),
        {
            "id": other_id,
            "c": course,
            "h": hashlib.sha256(pdf).hexdigest(),
            "u": owner.id,
            "k": f"{course}/{other_id}.pdf",
            "s": len(pdf),
        },
    )
    result: dict = {}
    api.as_user(owner)
    worker = threading.Thread(target=lambda: result.setdefault("response", api.upload(course, pdf)))
    worker.start()
    try:
        _wait_for_lock_wait(world)
        holder.commit() if other_outcome == "commit" else holder.rollback()
    finally:
        holder.close()
        worker.join(30)
    response = result["response"]

    if other_outcome == "commit":
        assert response.status_code == 409
        assert response.json()["document_id"] == str(other_id)
        assert api.stored_files() == []  # the losing upload never placed its file
    else:
        assert response.status_code == 202
        assert len(api.stored_files()) == 1
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 1
    assert api.incoming_files() == []


def test_concurrent_identical_uploads_create_one_document_one_job_and_one_file(api, world, owner, course, monkeypatch):
    """Both uploads pass the duplicate pre-check before either inserts; the unique index decides."""
    barrier = threading.Barrier(2, timeout=30)
    local = threading.local()
    original = documents._find_duplicate

    def checked_then_wait(*args):
        found = original(*args)
        if not getattr(local, "waited", False):
            local.waited = True
            barrier.wait()  # both have seen "no duplicate" at this point
        return found

    monkeypatch.setattr(documents, "_find_duplicate", checked_then_wait)
    api.as_user(owner)
    pdf = make_pdf("simultaneous")
    responses: list = []

    def upload(name: str) -> None:
        with TestClient(api.app) as client:  # one client per thread
            responses.append(client.post(api.path(course), files={"file": (name, pdf, "application/pdf")}))

    threads = [threading.Thread(target=upload, args=(name,)) for name in ("a.pdf", "b.pdf")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [202, 409], [r.text for r in responses]
    winner = next(r for r in responses if r.status_code == 202).json()
    loser = next(r for r in responses if r.status_code == 409).json()
    assert loser["document_id"] == winner["id"]
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 1
    assert world.scalar("SELECT count(*) FROM ingestion_jobs") == 1
    [stored] = api.stored_files()
    assert stored.name == f"{winner['id']}.pdf" and stored.read_bytes() == pdf  # the winner's file is intact
    assert api.incoming_files() == []


# --- database read failures and unexpected errors ---------------------------------------------


def _unreachable_database(api) -> None:
    """A session factory whose database cannot be reached (TEST-NET-1, RFC 5737: never routed)."""
    engine = create_engine(
        "postgresql+psycopg://nobody:none@192.0.2.1:5432/sg_disposable_unreachable",
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 2},
    )
    api.app.state.session_factory = sessionmaker(bind=engine)


def test_unreachable_database_gives_json_503_when_listing(api, owner, course):
    _unreachable_database(api)
    response = api.as_user(owner).list(course)
    _assert_safe_error(api, response, 503, "database_unavailable")
    assert "192.0.2.1" not in response.text


def test_unreachable_database_refuses_an_upload_before_reading_it(api, owner, course, monkeypatch):
    from tests.api_support import asgi_call, multipart_body

    _unreachable_database(api)
    stage_calls = []
    monkeypatch.setattr(api.storage, "stage", lambda: stage_calls.append(1))
    body, content_type = multipart_body([("file", "a.pdf", make_pdf(), "application/pdf")])
    api.as_user(owner)

    result = asgi_call(
        api.app, "POST", api.path(course), {"content-type": content_type, "content-length": str(len(body))}, [body]
    )

    assert result.status == 503 and result.json()["code"] == "database_unavailable"
    assert result.receive_calls == 0 and stage_calls == []


def test_other_database_read_errors_give_json_500(api, owner, course, monkeypatch):
    def broken(*args, **kwargs):
        raise ProgrammingError("SELECT", {}, Exception("simulated: column does not exist"))

    monkeypatch.setattr(documents.course_access, "require_read", broken)
    response = api.as_user(owner).list(course)
    _assert_safe_error(api, response, 500, "server_error")


def test_unexpected_errors_still_answer_with_the_json_error_shape(api, owner, course, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("simulated programming error with /secret/path")

    monkeypatch.setattr(documents, "list_documents", crash)
    client = TestClient(api.app, raise_server_exceptions=False)
    api.as_user(owner)
    response = client.get(api.path(course))
    _assert_safe_error(api, response, 500, "server_error")
    assert response.json() == {"detail": "Something went wrong on the server. Try again later.", "code": "server_error"}
    assert "/secret/path" not in response.text
    client.close()
