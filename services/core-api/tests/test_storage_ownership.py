"""M3 storage: temporary-file ownership, abandoned-file cleanup, safe reads and the identity marker.

No database needed. Locks are flock(2) locks; two descriptors opened
separately conflict even inside one process, which is what lets threads stand
in for concurrent uploads here.
"""

from __future__ import annotations

import fcntl
import os
import random
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from app import storage as storage_module
from app import storage_identity
from app.storage import (
    INCOMING_DIR,
    INCOMING_LOCK,
    LocalFileStorage,
    StorageError,
    StoredFileInvalid,
    StoredFileMissing,
    storage_key,
)
from app.storage_identity import MARKER_NAME, StorageIdentityError, parse_marker
from tests.worker_support import store_file

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="flock semantics checked on Linux")
OLD = 10 * 24 * 3600


@pytest.fixture
def store(tmp_path) -> LocalFileStorage:
    store = LocalFileStorage(tmp_path / "uploads")
    _ = store.root
    return store


def _age(path: Path, seconds: float = OLD) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


def _incoming(store: LocalFileStorage) -> Path:
    path = store.root / INCOMING_DIR
    path.mkdir(exist_ok=True)
    return path


def _clean(store: LocalFileStorage, grace: float = 60):
    scan = store.clean_abandoned_temp_files(grace_seconds=grace, limit=100, lock_wait_seconds=0.2)
    assert scan is not None
    return scan


# --- ownership protocol ---------------------------------------------------------------------


def test_staged_file_is_locked_by_its_upload_until_it_finishes(store):
    with store.stage() as staged:
        staged.write(b"data")
        probe = os.open(staged.temp_path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        assert staged.temp_path.name.startswith("staged-")
    # after the upload: name gone and nothing locked
    assert list(_incoming(store).iterdir()) == []


def test_placed_file_stays_locked_until_the_upload_releases_it(store):
    key = storage_key(uuid.uuid4(), uuid.uuid4())
    with store.stage() as staged:
        staged.write(b"%PDF-1.4 placed")
        store.place(staged, key)
        assert store.stored_file_in_use(key)  # in flight: between placement and commit
        staged.release()
        assert store.stored_file_in_use(key)  # the lock goes when the upload ends, not at release()
    assert not store.stored_file_in_use(key)


def test_active_upload_survives_cleanup_however_old_its_file_looks(store):
    with store.stage() as staged:
        staged.write(b"slow client")
        _age(staged.temp_path)
        scan = _clean(store, grace=0)
        assert scan.active == [staged.temp_path.name] and scan.removed == []
        assert staged.temp_path.exists()


def test_abandoned_temporary_file_is_removed(store, tmp_path):
    """The owner process died (SIGKILL): its lock is gone, the file is old -> removed."""
    root = store.root
    script = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from app.storage import LocalFileStorage
        store = LocalFileStorage({str(root)!r})
        with store.stage() as staged:
            staged.write(b"partial")
            staged._file.flush()
            print(staged.temp_path.name, flush=True)
            time.sleep(600)
        """
    )
    owner = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        name = owner.stdout.readline().strip()
        path = _incoming(store) / name
        _age(path)
        assert _clean(store, grace=60).active == [name]  # alive: kept
        owner.kill()
        owner.wait(10)
        scan = _clean(store, grace=60)
        assert scan.removed == [name] and not path.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()


def test_recent_unlocked_file_is_kept_within_the_grace_period(store):
    path = _incoming(store) / "staged-abcdefgh.part"
    path.write_bytes(b"x")
    scan = _clean(store, grace=3600)
    assert scan.recent == [path.name] and path.exists()


def test_legacy_and_unexpected_entries_are_only_reported(store, tmp_path):
    incoming = _incoming(store)
    legacy = incoming / "upload-abcd1234.part"
    legacy.write_bytes(b"m2 upload, no ownership protocol")
    other = incoming / "notes.txt"
    other.write_bytes(b"?")
    target = tmp_path / "outside.txt"
    target.write_bytes(b"outside the store")
    os.symlink(target, incoming / "staged-symlink1.part")
    for path in (legacy, other):
        _age(path)
    scan = _clean(store, grace=0)
    assert scan.removed == []
    assert scan.legacy == [legacy.name]
    assert sorted(scan.unexpected) == ["notes.txt", "staged-symlink1.part"]
    assert legacy.exists() and other.exists() and target.read_bytes() == b"outside the store"


def test_cleanup_never_touches_the_final_file_of_a_crashed_upload(store):
    """A crash between link and unlink leaves the temporary name and the final name on one inode."""
    key = store_file(store, uuid.uuid4(), uuid.uuid4(), b"%PDF-1.4 committed")
    final = store.path_for(key)
    leftover = _incoming(store) / "staged-crash123.part"
    os.link(final, leftover)
    _age(leftover)
    scan = _clean(store, grace=60)
    assert scan.removed == [leftover.name]
    assert final.read_bytes() == b"%PDF-1.4 committed"


def test_upload_paused_between_creating_and_locking_its_file_is_safe(store, monkeypatch):
    """The window the shared .incoming.lock closes: the reconciler cannot look while it is open."""
    entered, release = threading.Event(), threading.Event()
    real_flock = fcntl.flock

    def paused_flock(fd, operation):
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and threading.current_thread().name == "uploader":
            entered.set()
            assert release.wait(30)
        return real_flock(fd, operation)

    monkeypatch.setattr(storage_module.fcntl, "flock", paused_flock)
    names: list[str] = []

    def upload():
        with store.stage() as staged:
            names.append(staged.temp_path.name)
            staged.write(b"data")

    thread = threading.Thread(target=upload, name="uploader")
    thread.start()
    try:
        assert entered.wait(30)
        [candidate] = list(_incoming(store).iterdir())  # created, not yet locked
        _age(candidate)
        assert store.clean_abandoned_temp_files(grace_seconds=0, limit=100, lock_wait_seconds=0.3) is None
        assert candidate.exists()
    finally:
        release.set()
        thread.join(30)
    assert names == [candidate.name]


def test_uploads_wait_for_the_reconciler_and_give_up_after_a_bound(store, monkeypatch):
    monkeypatch.setattr(storage_module, "INCOMING_LOCK_WAIT_SECONDS", 0.3)
    holder = os.open(store.root / INCOMING_LOCK, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(StorageError):
            with store.stage():
                pass
        assert time.monotonic() - started < 5
    finally:
        os.close(holder)
    with store.stage() as staged:  # free again
        staged.write(b"ok")


@linux_only
def test_concurrent_uploads_and_cleanup_lose_nothing(store):
    stop = threading.Event()
    errors: list[BaseException] = []
    stored: list[tuple[str, bytes]] = []

    def uploader(index: int):
        try:
            for n in range(15):
                key = storage_key(uuid.uuid4(), uuid.uuid4())
                data = f"%PDF-1.4 upload {index}-{n}".encode()
                with store.stage() as staged:
                    staged.write(data)
                    _age(staged.temp_path)  # look abandoned by age alone
                    time.sleep(0.005)
                    store.place(staged, key)
                    staged.release()
                stored.append((key, data))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reconciler():
        while not stop.is_set():
            scan = store.clean_abandoned_temp_files(grace_seconds=0, limit=1000, lock_wait_seconds=0.05)
            if scan is not None and scan.removed:
                errors.append(AssertionError(f"removed a live upload's file: {scan.removed}"))

    threads = [threading.Thread(target=uploader, args=(i,)) for i in range(4)]
    cleaner = threading.Thread(target=reconciler)
    cleaner.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    stop.set()
    cleaner.join(30)
    assert errors == []
    assert len(stored) == 60
    for key, data in stored:
        assert store.path_for(key).read_bytes() == data
    assert list(_incoming(store).iterdir()) == []


def test_symlinked_incoming_directory_is_refused_by_cleanup(tmp_path):
    store = LocalFileStorage(tmp_path / "uploads")
    store.root.mkdir(exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "staged-abcdefgh.part").write_bytes(b"not ours")
    os.symlink(elsewhere, store.root / INCOMING_DIR)
    with pytest.raises(StorageError):
        store.clean_abandoned_temp_files(grace_seconds=0, limit=10)
    assert (elsewhere / "staged-abcdefgh.part").exists()


# --- reading stored files -------------------------------------------------------------------


def test_open_stored_reads_only_plain_files_in_plain_directories(store, tmp_path):
    course, doc = uuid.uuid4(), uuid.uuid4()
    key = store_file(store, course, doc, b"%PDF-1.4 stored")
    fd = store.open_stored(key)
    try:
        assert os.read(fd, 100) == b"%PDF-1.4 stored"
    finally:
        os.close(fd)

    with pytest.raises(StoredFileMissing):
        store.open_stored(storage_key(course, uuid.uuid4()))
    with pytest.raises(StoredFileMissing):
        store.open_stored(storage_key(uuid.uuid4(), uuid.uuid4()))

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"secret")
    linked = storage_key(course, uuid.uuid4())
    os.symlink(outside, store.root / linked)
    with pytest.raises(StoredFileInvalid):
        store.open_stored(linked)

    other_course = uuid.uuid4()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", store.root / str(other_course))
    with pytest.raises(StoredFileInvalid):
        store.open_stored(storage_key(other_course, uuid.uuid4()))

    directory = storage_key(course, uuid.uuid4())
    os.mkdir(store.root / directory)
    with pytest.raises(StoredFileInvalid):
        store.open_stored(directory)
    with pytest.raises(StorageError):
        store.open_stored("../etc/passwd")


def test_readers_never_create_a_missing_store(tmp_path):
    store = LocalFileStorage(tmp_path / "not-mounted")
    with pytest.raises(StorageError):
        store.open_stored(storage_key(uuid.uuid4(), uuid.uuid4()))
    with pytest.raises(StorageError):
        store.clean_abandoned_temp_files(grace_seconds=0, limit=1)
    assert not (tmp_path / "not-mounted").exists()


def test_iter_stored_files_lists_only_originals(store, tmp_path):
    course = uuid.uuid4()
    key = store_file(store, course, uuid.uuid4(), b"a")
    (store.root / str(course) / "notes.txt").write_bytes(b"x")
    (store.root / "not-a-course").mkdir()
    os.symlink(tmp_path, store.root / str(uuid.uuid4()))
    assert [entry.key for entry in store.iter_stored_files(100)] == [key]


# --- identity marker --------------------------------------------------------------------------


def test_marker_round_trip_and_never_overwritten(store):
    storage_id = uuid.uuid4()
    assert storage_identity.read_marker(store) is None
    storage_identity.write_marker(store, storage_id)
    assert storage_identity.read_marker(store) == storage_id
    with pytest.raises(StorageIdentityError):
        storage_identity.write_marker(store, uuid.uuid4())
    assert storage_identity.read_marker(store) == storage_id
    assert [p.name for p in store.root.iterdir() if p.name.startswith(MARKER_NAME)] == [MARKER_NAME]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b'{"kind": "studygraph-upload-store", "version": 1}',
        b'{"kind": "other", "version": 1, "storage_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}',
        b'{"kind": "studygraph-upload-store", "version": 2, "storage_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}',
        b'{"kind": "studygraph-upload-store", "version": true, "storage_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}',
        b'{"kind": "studygraph-upload-store", "version": 1, "storage_id": "7C9E6679-7425-40DE-944B-E07FC1F90AE7"}',
        b'{"kind": "studygraph-upload-store", "version": 1, "storage_id": "not-a-uuid"}',
        b'{"kind": "studygraph-upload-store", "version": 1, "storage_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "x": 1}',
    ],
)
def test_malformed_markers_are_refused(store, content):
    (store.root / MARKER_NAME).write_bytes(content)
    with pytest.raises(StorageIdentityError):
        storage_identity.read_marker(store)


def test_unsafe_markers_are_refused(store, tmp_path):
    good = tmp_path / "good-marker"
    good.write_bytes(
        b'{"kind": "studygraph-upload-store", "storage_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "version": 1}'
    )
    assert parse_marker(good.read_bytes()) == uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")
    os.symlink(good, store.root / MARKER_NAME)
    with pytest.raises(StorageIdentityError):
        storage_identity.read_marker(store)
    os.unlink(store.root / MARKER_NAME)
    (store.root / MARKER_NAME).mkdir()
    with pytest.raises(StorageIdentityError):
        storage_identity.read_marker(store)
    os.rmdir(store.root / MARKER_NAME)
    (store.root / MARKER_NAME).write_bytes(b" " * 5000)
    with pytest.raises(StorageIdentityError):
        storage_identity.read_marker(store)


def test_missing_store_is_not_established(tmp_path):
    with pytest.raises(StorageIdentityError):
        storage_identity.read_marker(LocalFileStorage(tmp_path / "absent"))
    assert not (tmp_path / "absent").exists()


# --- M3 review H2: one pinned root for every operation ------------------------------------------


def _retarget(link: Path, target: Path) -> None:
    replacement = link.with_name(link.name + ".new")
    os.symlink(target, replacement)
    os.replace(replacement, link)


def test_retargeted_root_symlink_cannot_split_verification_and_writes(tmp_path):
    """The review's reproduction: root cached in A, symlink moved to B, marker used through the
    same instance, then stage(). Before the fix the marker was read from B and the file staged in A."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "uploads"
    os.symlink(a, link)
    store = LocalFileStorage(link)
    assert store.root == a.resolve()  # pinned at first use
    storage_identity.write_marker(store, uuid.uuid4())  # in A

    _retarget(link, b)
    with pytest.raises(StorageIdentityError):
        storage_identity.write_marker(store, uuid.uuid4())
    with pytest.raises(StorageIdentityError):
        storage_identity.read_marker(store)
    with pytest.raises(StorageError):
        with store.stage():
            pass
    with pytest.raises(StorageError):
        store.open_stored(storage_key(uuid.uuid4(), uuid.uuid4()))
    with pytest.raises(StorageError):
        store.clean_abandoned_temp_files(grace_seconds=0, limit=10)
    with pytest.raises(StorageError):
        _ = store.root
    assert os.listdir(b) == []  # nothing verified or written in B
    assert not (a / INCOMING_DIR).exists() or list((a / INCOMING_DIR).iterdir()) == []

    _retarget(link, a)  # pointing back at the pinned directory works again
    with store.stage() as staged:
        staged.write(b"ok")


def test_replaced_root_directory_is_refused(tmp_path):
    """Same path, different directory (a remounted or recreated volume): refused by identity."""
    root = tmp_path / "uploads"
    store = LocalFileStorage(root)
    _ = store.root
    storage_identity.write_marker(store, uuid.uuid4())
    marker = storage_identity.read_marker(store)
    os.rename(root, tmp_path / "old-volume")
    root.mkdir()
    storage_identity.write_marker(LocalFileStorage(root), marker)  # even carrying the same id
    with pytest.raises(StorageError):
        _ = store.root
    with pytest.raises(StorageIdentityError):
        storage_identity.read_marker(store)
    with pytest.raises(StorageError):
        with store.stage():
            pass
    assert sorted(os.listdir(root)) == [MARKER_NAME]


def test_a_root_given_through_a_stable_symlink_keeps_working(tmp_path):
    target = tmp_path / "volume"
    target.mkdir()
    link = tmp_path / "uploads"
    os.symlink(target, link)
    store = LocalFileStorage(link)
    key = store_file(store, uuid.uuid4(), uuid.uuid4(), b"%PDF-1.4 stable")
    fd = store.open_stored(key)
    os.close(fd)
    assert (target / key).read_bytes() == b"%PDF-1.4 stable"


# --- fair bounded scans ------------------------------------------------------------------------


def test_bounded_scans_cover_a_large_store_over_runs(store):
    keys = {store_file(store, uuid.uuid4(), uuid.uuid4(), b"x") for _ in range(10)}
    first_three = {entry.key for entry in store.iter_stored_files(3)}
    assert {entry.key for entry in store.iter_stored_files(3)} == first_three  # without rng: always the same
    rng = random.Random(7)
    seen: set[str] = set()
    for _ in range(30):
        window = [entry.key for entry in store.iter_stored_files(3, rng)]
        assert len(window) == 3
        seen.update(window)
    assert seen == keys


def test_bounded_temp_scan_covers_every_entry_over_runs(store):
    """Report-only entries stay in place, so they show which window each run examined."""
    incoming = _incoming(store)
    names = {f"upload-{n:08d}.part" for n in range(10)}  # M2 names: reported, never removed
    for name in names:
        (incoming / name).write_bytes(b"x")

    def window(rng):
        scan = store.clean_abandoned_temp_files(grace_seconds=60, limit=3, lock_wait_seconds=0.2, rng=rng)
        assert scan.truncated and len(scan.legacy) == 3
        return set(scan.legacy)

    assert window(None) == window(None)  # without rng: always the same first entries
    rng = random.Random(11)
    seen: set[str] = set()
    for _ in range(30):
        seen |= window(rng)
    assert seen == names and len(list(incoming.iterdir())) == 10
