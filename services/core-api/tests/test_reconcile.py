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


class _ScriptedRandom(random.Random):
    """Scan starting points chosen by the test instead of by chance.

    reconcile() draws one start per scan: a position among the entries of the
    store root (stored-file scan, `randrange`) and a 128-bit document id
    (missing-file scan, `getrandbits(128)`). Here the root scan starts at the
    next scripted course directory, wherever it sits among the housekeeping
    entries, and the document scan at the next scripted id. Once a script is
    used up, the root scan starts at position 0 and the id scan at the lowest id.
    """

    def __init__(self, root, course_starts=(), id_starts=()):
        super().__init__(0)
        self._root = root
        self._course_starts = iter(course_starts)
        self._id_starts = iter(id_starts)

    def randrange(self, start, stop=None, step=1):
        course = next(self._course_starts, None)
        if course is None:
            return 0
        return sorted(os.listdir(self._root)).index(course)

    def getrandbits(self, k):
        assert k == 128
        return next(self._id_starts, 0)


def _uuid_at(slot: int, offset: int = 0) -> uuid.UUID:
    """A fixed uuid at slot/16 of the id space (slot 1, 3, 5 ... 15), plus `offset`."""
    return uuid.UUID(int=(slot << 123) + offset)


def test_bounded_runs_cover_more_orphans_and_missing_files_than_one_batch(world, store, factory):
    """M3 review: with more entries than the batch size, later entries are not starved.

    The ids and the scan starting points are fixed, so every run has one exact
    expected outcome (no dependence on random uuids or on how many runs happen
    to be enough): 8 orphans and 8 missing files, 3 per scan, three runs cover
    all of them, one of which wraps around the end, and a fourth starts past the
    last document, so that the whole window comes from the wrapped part.
    """
    owner = world.user("professor")
    course = world.course(owner)
    courses = [str(uuid.UUID(int=(n + 1) << 120)) for n in range(8)]  # sorted order == numeric order
    orphan_of = {c: store_file(store, uuid.UUID(c), uuid.uuid4(), b"%PDF-1.4 orphan") for c in courses}
    orphans = set(orphan_of.values())
    document_ids = [_uuid_at(2 * n + 1) for n in range(8)]
    missing_of = {
        d: str(add_stored_document(world.engine, world, store, course, owner, make_pdf(f"m{n}"), with_file=False,
                                   document_id=d).document_id)
        for n, d in enumerate(document_ids)
    }
    missing = set(missing_of.values())
    contents = {key: store.path_for(key).read_bytes() for key in orphans}
    doc = lambda *slots: [str(document_ids[n]) for n in slots]  # noqa: E731
    orphan = lambda *cs: {orphan_of[courses[n]] for n in cs}  # noqa: E731

    runs = [
        # (first course scanned, id the document scan starts after, expected orphans, expected missing, in this order)
        (0, _uuid_at(1, -1), orphan(0, 1, 2), doc(0, 1, 2)),  # from the start
        (3, _uuid_at(7, -1), orphan(3, 4, 5), doc(3, 4, 5)),  # the middle
        (6, _uuid_at(13, -1), orphan(6, 7, 0), doc(6, 7, 0)),  # the end, then wrapped around to the beginning
        (1, _uuid_at(15, 1), orphan(1, 2, 3), doc(0, 1, 2)),  # start after the last id: only the wrapped part
    ]
    settings = Settings(reconcile_batch_size=3, reconcile_orphan_grace_seconds=0)
    rng = _ScriptedRandom(
        store.root, [courses[c] for c, *_ in runs], [int(start) for _, start, *_ in runs]
    )
    seen_orphans: set[str] = set()
    seen_missing: set[str] = set()
    for _, _, expected_orphans, expected_missing in runs:
        report = reconcile(factory, store, settings, rng)
        assert report.errors == [] and report.skipped is None
        assert set(report.orphans) == expected_orphans  # a bounded window: 3 of 8, never more
        assert report.missing == expected_missing  # in id order, wrapping around at the end
        assert report.invalid == []
        seen_orphans.update(report.orphans)
        seen_missing.update(report.missing)
    assert seen_orphans == orphans and seen_missing == missing  # more than one batch, covered across runs

    # Report-only: nothing was removed, changed or "repaired".
    for key in orphans:
        assert store.path_for(key).read_bytes() == contents[key]
    assert world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=course) == 8

    # The unscripted path (no rng given: the production default) stays bounded and only reports real findings.
    report = reconcile(factory, store, settings)
    assert len(report.orphans) <= 3 and set(report.orphans) <= orphans
    assert len(report.missing) <= 3 and set(report.missing) <= missing


def test_missing_scan_wraps_around_and_ends_at_its_starting_id(world, store, factory):
    """Starting exactly at a document id: the ids after it come first, it is the last one checked."""
    owner = world.user("professor")
    course = world.course(owner)
    ids = [_uuid_at(2 * n + 1) for n in range(8)]
    for n, d in enumerate(ids):
        add_stored_document(world.engine, world, store, course, owner, make_pdf(f"w{n}"), with_file=False, document_id=d)
    settings = Settings(reconcile_batch_size=8, reconcile_orphan_grace_seconds=0)
    report = reconcile(factory, store, settings, _ScriptedRandom(store.root, id_starts=[int(ids[3])]))
    assert report.missing == [str(ids[n]) for n in (4, 5, 6, 7, 0, 1, 2, 3)]  # each once; nothing beyond the batch
    smaller = Settings(reconcile_batch_size=5, reconcile_orphan_grace_seconds=0)
    report = reconcile(factory, store, smaller, _ScriptedRandom(store.root, id_starts=[int(ids[3])]))
    assert report.missing == [str(ids[n]) for n in (4, 5, 6, 7, 0)]
