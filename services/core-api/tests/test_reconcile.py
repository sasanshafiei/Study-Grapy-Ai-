"""File reconciliation on a real database and store: what is removed, what is only reported."""

from __future__ import annotations

import os
import random
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, pool
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.ingestion.reconcile import reconcile
from app.storage import INCOMING_DIR, LocalFileStorage, storage_key
from tests.api_support import bind_storage
from tests.pdf_samples import make_pdf
from tests.worker_support import add_stored_document, store_file

OLD = 10 * 24 * 3600
SETTINGS = Settings(reconcile_temp_grace_seconds=60, reconcile_orphan_grace_seconds=60)


@pytest.fixture
def store(world, tmp_path) -> LocalFileStorage:
    bind_storage(world.engine, tmp_path / "store")
    return LocalFileStorage(tmp_path / "store")


@pytest.fixture
def factory(world):
    return sessionmaker(bind=world.engine)


def _age(path, seconds=OLD):
    past = time.time() - seconds
    os.utime(path, (past, past))


def _abandoned_temp(store: LocalFileStorage, name: str = "staged-abandon1.part"):
    incoming = store.root / INCOMING_DIR
    incoming.mkdir(exist_ok=True)
    path = incoming / name
    path.write_bytes(b"partial upload")
    _age(path)
    return path


def test_reconcile_removes_abandoned_temps_and_reports_orphans_and_missing_files(world, store, factory, monkeypatch):
    owner = world.user("professor")
    course = world.course(owner)
    kept = add_stored_document(world.engine, world, store, course, owner, make_pdf("kept"))
    gone = add_stored_document(world.engine, world, store, course, owner, make_pdf("gone"))
    os.unlink(store.path_for(gone.key))
    orphan_key = store_file(store, uuid.uuid4(), uuid.uuid4(), b"%PDF-1.4 crash between place and commit")
    temp = _abandoned_temp(store)
    legacy = _abandoned_temp(store, "upload-legacy01.part")
    # Files are judged by max(mtime, ctime) for orphans; ctime cannot be set back, so use a zero grace here.
    report = reconcile(factory, store, Settings(reconcile_temp_grace_seconds=60, reconcile_orphan_grace_seconds=0))

    assert report.skipped is None and report.errors == []
    assert report.temp.removed == [temp.name] and not temp.exists()
    assert report.temp.legacy == [legacy.name] and legacy.exists()
    assert report.orphans == [orphan_key] and store.path_for(orphan_key).exists()  # reported, never removed
    assert report.missing == [str(gone.document_id)]
    assert store.path_for(kept.key).exists()
    assert world.scalar("SELECT status FROM documents WHERE id = :d", d=gone.document_id) == "pending"  # untouched


def test_reconcile_does_nothing_without_an_established_store_identity(world, store, factory):
    temp = _abandoned_temp(store)
    world.execute("DELETE FROM storage_identity")
    report = reconcile(factory, store, SETTINGS)
    assert report.skipped and report.temp is None and temp.exists()


def test_database_outage_causes_no_cleanup(world, store):
    temp = _abandoned_temp(store)
    unreachable = create_engine(
        "postgresql+psycopg://nobody:nothing@sg-unreachable.invalid:5432/none",
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 2},
    )
    report = reconcile(sessionmaker(bind=unreachable), store, SETTINGS)
    assert report.skipped and temp.exists()


def test_in_flight_upload_is_neither_removed_nor_reported(world, store, factory):
    key = storage_key(uuid.uuid4(), uuid.uuid4())
    with store.stage() as staged:
        staged.write(b"%PDF-1.4 in flight")
        _age(staged.temp_path)
        report = reconcile(factory, store, Settings(reconcile_temp_grace_seconds=0, reconcile_orphan_grace_seconds=0))
        assert report.temp.removed == [] and report.temp.active == [staged.temp_path.name]
        store.place(staged, key)  # placed, not yet committed: no document row
        report = reconcile(factory, store, Settings(reconcile_temp_grace_seconds=0, reconcile_orphan_grace_seconds=0))
        assert report.orphans == []  # its upload still owns it
        staged.release()
    assert store.path_for(key).exists()


def test_reconciliation_running_during_uploads_damages_nothing(world, make_api, store):
    """Real uploads through the API while reconciliation runs in a loop with zero grace periods."""
    owner = world.user("professor")
    course = world.course(owner)
    api = make_api()
    api.as_user(owner)
    stop = threading.Event()
    removed: list[str] = []
    zero = Settings(reconcile_temp_grace_seconds=0, reconcile_orphan_grace_seconds=3600)
    factory = sessionmaker(bind=world.engine)

    def loop():
        while not stop.is_set():
            report = reconcile(factory, api.storage, zero)
            if report.temp:
                removed.extend(report.temp.removed)

    cleaner = threading.Thread(target=loop)
    cleaner.start()
    try:
        responses = [api.upload(course, make_pdf(f"lecture {n}"), filename=f"L{n}.pdf") for n in range(12)]
    finally:
        stop.set()
        cleaner.join(60)
    assert [r.status_code for r in responses] == [202] * 12
    assert removed == []
    for item in api.list(course).json():
        assert world.scalar("SELECT storage_key FROM documents WHERE id = :d", d=item["id"])
    assert len(api.stored_files()) == 12 and api.incoming_files() == []


def test_bounded_runs_cover_more_orphans_and_missing_files_than_one_batch(world, store, factory):
    """M3 review: with more entries than the batch size, later entries are not starved."""
    owner = world.user("professor")
    course = world.course(owner)
    orphans = {store_file(store, uuid.uuid4(), uuid.uuid4(), b"%PDF-1.4 orphan") for _ in range(8)}
    missing = {
        str(add_stored_document(world.engine, world, store, course, owner, make_pdf(f"m{n}"), with_file=False).document_id)
        for n in range(8)
    }
    settings = Settings(reconcile_batch_size=3, reconcile_orphan_grace_seconds=0)
    rng = random.Random(5)
    seen_orphans: set[str] = set()
    seen_missing: set[str] = set()
    for _ in range(25):
        report = reconcile(factory, store, settings, rng)
        assert len(report.missing) <= 3 and len(report.orphans) <= 3
        seen_orphans.update(report.orphans)
        seen_missing.update(report.missing)
    assert seen_orphans == orphans and seen_missing == missing
    for key in orphans:
        assert store.path_for(key).exists()  # still report-only
