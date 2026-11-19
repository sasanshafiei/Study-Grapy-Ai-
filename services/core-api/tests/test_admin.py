"""Operator commands: explicit targets, safety checks, init-storage and retry-parse."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.ingestion import admin, jobs
from app.ingestion.jobs import Failure
from app.storage import LocalFileStorage
from app.storage_identity import read_marker
from tests.pdf_samples import make_pdf
from tests.worker_support import add_stored_document, job_row


@pytest.fixture
def environ(disposable_target, tmp_path):
    store = tmp_path / "volume"
    store.mkdir()
    return {
        "DATABASE_URL": disposable_target.url.render_as_string(hide_password=False),
        "MIGRATIONS_REQUIRE_DATABASE_COMMENT": disposable_target.marker,
        "UPLOAD_DIR": str(store),
    }


def _run(environ, *argv, capsys):
    code = admin.main(list(argv), environ)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_storage_binds_the_store_and_prints_no_credentials(world, environ, disposable_target, capsys):
    code, out, err = _run(environ, "init-storage", "--database", disposable_target.url.database, capsys=capsys)
    assert code == 0, err
    storage_id = read_marker(LocalFileStorage(environ["UPLOAD_DIR"]))
    assert str(storage_id) in out and "initialized" in out
    assert disposable_target.url.password not in out + err
    assert world.scalar("SELECT storage_id FROM storage_identity") == storage_id


def test_mutating_commands_need_an_explicit_matching_target(world, environ, disposable_target, capsys):
    code, _, err = _run(environ, "init-storage", "--database", "some_other_db", capsys=capsys)
    assert code == 1 and "does not match" in err
    code, _, err = _run({k: v for k, v in environ.items() if k != "DATABASE_URL"}, "init-storage", "--database", "x", capsys=capsys)
    assert code == 1 and "DATABASE_URL is not set" in err
    code, _, err = _run({k: v for k, v in environ.items() if k != "UPLOAD_DIR"}, "init-storage", "--database",
                        disposable_target.url.database, capsys=capsys)  # fmt: skip
    assert code == 1 and "UPLOAD_DIR" in err
    wrong_marker = {**environ, "MIGRATIONS_REQUIRE_DATABASE_COMMENT": "studygraph-disposable-test:not-this-one"}
    code, _, err = _run(wrong_marker, "init-storage", "--database", disposable_target.url.database, capsys=capsys)
    assert code == 1 and "COMMENT" in err
    loopback = {**environ, "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/" + disposable_target.url.database}
    code, _, err = _run(loopback, "init-storage", "--database", disposable_target.url.database, capsys=capsys)
    assert code == 1 and "local machine" in err
    assert world.scalar("SELECT count(*) FROM storage_identity") == 0
    assert read_marker(LocalFileStorage(environ["UPLOAD_DIR"])) is None


def test_retry_parse_queues_a_new_job_for_a_failed_document(world, environ, disposable_target, capsys, tmp_path):
    owner = world.user("professor")
    course = world.course(owner)
    doc = add_stored_document(world.engine, world, LocalFileStorage(tmp_path / "s"), course, owner, make_pdf("r"))
    factory = sessionmaker(bind=world.engine)
    with factory() as session:
        claim = jobs.claim_parse(session, doc.job_id, 300)
        session.commit()
    jobs.record_failure(factory, claim, Failure("storage_error", "EIO", permanent=True), Settings())

    code, out, err = _run(environ, "retry-parse", "--database", disposable_target.url.database, str(doc.document_id), capsys=capsys)
    assert code == 0, err
    new_job = uuid.UUID(out.split()[3])
    assert job_row(world.engine, new_job)["status"] == "queued"

    code, _, err = _run(environ, "retry-parse", "--database", disposable_target.url.database, str(doc.document_id), capsys=capsys)
    assert code == 1 and "retry refused" in err
    code, _, err = _run(environ, "retry-parse", "--database", disposable_target.url.database, "not-a-uuid", capsys=capsys)
    assert code == 1 and "UUID" in err


def test_allow_missing_requires_adopt_existing(environ):
    with pytest.raises(SystemExit):
        admin.main(["init-storage", "--database", "x", "--allow-missing"], environ)
