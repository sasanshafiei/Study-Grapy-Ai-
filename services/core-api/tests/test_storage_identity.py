"""Binding the database to an upload store (explicit init-storage) and verifying it, on real PostgreSQL."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import sessionmaker

from app import storage_identity
from app.storage import LocalFileStorage
from app.storage_identity import MARKER_NAME, StorageIdentityError
from tests.pdf_samples import make_pdf
from tests.worker_support import add_stored_document, store_file


@pytest.fixture
def factory(world):
    return sessionmaker(bind=world.engine)


@pytest.fixture
def init(world):
    def init(storage, **flags):
        return storage_identity.initialize(world.engine, storage, **flags)

    return init


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "volume"
    path.mkdir()
    return path


def _verify(factory, store):
    with factory() as session:
        return storage_identity.verify(session, store)


def _recorded(world):
    return world.scalar("SELECT storage_id FROM storage_identity")


def test_fresh_store_and_database_are_bound_explicitly(world, factory, init, root):
    store = LocalFileStorage(root)
    with pytest.raises(StorageIdentityError):
        _verify(factory, store)  # nothing bound yet: fail closed
    result = init(store)
    assert result.action == "initialized"
    assert _verify(factory, store) == result.storage_id == storage_identity.read_marker(store) == _recorded(world)
    again = init(store)
    assert (again.action, again.storage_id) == ("already-initialized", result.storage_id)


def test_a_different_or_empty_directory_is_refused_once_bound(world, factory, init, root, tmp_path):
    init(LocalFileStorage(root))
    owner = world.user("professor")
    add_stored_document(world.engine, world, LocalFileStorage(root), world.course(owner), owner, make_pdf("held"))
    empty = tmp_path / "typo-volume"
    empty.mkdir()
    with pytest.raises(StorageIdentityError, match="no marker"):
        init(LocalFileStorage(empty))
    with pytest.raises(StorageIdentityError):
        _verify(factory, LocalFileStorage(empty))
    other = tmp_path / "other-deployment"
    other.mkdir()
    storage_identity.write_marker(LocalFileStorage(other), uuid.uuid4())
    with pytest.raises(StorageIdentityError, match="different upload store"):
        init(LocalFileStorage(other))
    with pytest.raises(StorageIdentityError, match="another store"):
        _verify(factory, LocalFileStorage(other))
    assert os.listdir(empty) == []  # nothing written into the wrong directory


def test_a_missing_mount_point_is_never_created(world, init, tmp_path):
    with pytest.raises(StorageIdentityError):
        init(LocalFileStorage(tmp_path / "absent"))
    assert not (tmp_path / "absent").exists()
    assert world.scalar("SELECT count(*) FROM storage_identity") == 0


def test_existing_uploads_need_adopt_existing_and_all_their_files(world, init, root):
    owner = world.user("professor")
    course = world.course(owner)
    staging = LocalFileStorage(root)
    add_stored_document(world.engine, world, staging, course, owner, make_pdf("present"))
    add_stored_document(world.engine, world, staging, course, owner, make_pdf("missing"), with_file=False)
    store = LocalFileStorage(root)

    with pytest.raises(StorageIdentityError, match="adopt-existing"):
        init(store)
    with pytest.raises(StorageIdentityError, match="1 of 2 stored documents have no file"):
        init(store, adopt_existing=True)
    assert storage_identity.read_marker(store) is None and world.scalar("SELECT count(*) FROM storage_identity") == 0

    result = init(store, adopt_existing=True, allow_missing=True)
    assert (result.action, result.stored_documents, result.missing_files) == ("adopted", 2, 1)
    assert storage_identity.read_marker(store) == _recorded(world) == result.storage_id


def test_a_store_with_files_but_no_documents_is_not_adopted_silently(world, init, root):
    store_file(LocalFileStorage(root), uuid.uuid4(), uuid.uuid4(), b"%PDF-1.4 someone else's")
    with pytest.raises(StorageIdentityError, match="adopt-existing"):
        init(LocalFileStorage(root))


@pytest.mark.parametrize("contents", ["empty", "with-files"])
def test_a_store_initialized_for_another_database_is_never_bound_silently(world, init, root, contents):
    """The M3 review case: a marker this database never recorded, even on an empty store."""
    foreign = uuid.uuid4()
    storage_identity.write_marker(LocalFileStorage(root), foreign)  # initialized by some other database
    if contents == "with-files":
        store_file(LocalFileStorage(root), uuid.uuid4(), uuid.uuid4(), b"%PDF-1.4 theirs")

    with pytest.raises(StorageIdentityError, match="initialized for another database"):
        init(LocalFileStorage(root))
    assert world.scalar("SELECT count(*) FROM storage_identity") == 0

    result = init(LocalFileStorage(root), adopt_existing=True)  # explicit, deliberate
    assert (result.action, result.storage_id) == ("adopted", foreign)
    assert _recorded(world) == foreign


def test_a_store_bound_to_one_database_cannot_silently_join_another(world, init, root):
    first = init(LocalFileStorage(root)).storage_id
    world.execute("DELETE FROM storage_identity")  # what a second, unrelated database looks like
    with pytest.raises(StorageIdentityError, match="initialized for another database"):
        init(LocalFileStorage(root))
    assert storage_identity.read_marker(LocalFileStorage(root)) == first


def test_an_interrupted_initialization_is_completed(world, init, root):
    """Record committed, marker never written (the only state an interrupted run can leave)."""
    recorded = uuid.uuid4()
    world.execute("INSERT INTO storage_identity (id, storage_id) VALUES (1, :s)", s=recorded)
    result = init(LocalFileStorage(root))
    assert (result.action, result.storage_id) == ("completed", recorded)
    assert storage_identity.read_marker(LocalFileStorage(root)) == recorded


def test_completing_into_a_directory_that_holds_uploads_needs_adopt_existing(world, init, root):
    recorded = uuid.uuid4()
    world.execute("INSERT INTO storage_identity (id, storage_id) VALUES (1, :s)", s=recorded)
    owner = world.user("professor")
    add_stored_document(world.engine, world, LocalFileStorage(root), world.course(owner), owner, make_pdf("x"))
    with pytest.raises(StorageIdentityError, match="no marker"):
        init(LocalFileStorage(root))
    assert init(LocalFileStorage(root), adopt_existing=True).action == "completed"


def test_the_record_is_committed_before_the_marker_is_written(world, init, root, monkeypatch):
    def fail(storage, storage_id):
        raise StorageIdentityError("simulated: the marker could not be written")

    monkeypatch.setattr(storage_identity, "write_marker", fail)
    with pytest.raises(StorageIdentityError, match="simulated"):
        init(LocalFileStorage(root))
    recorded = _recorded(world)
    assert recorded is not None and storage_identity.read_marker(LocalFileStorage(root)) is None
    monkeypatch.undo()
    assert init(LocalFileStorage(root)).action == "completed"  # the re-run finishes the job
    assert storage_identity.read_marker(LocalFileStorage(root)) == recorded


def test_concurrent_initializations_bind_one_identity(world, init, root):
    store = LocalFileStorage(root)
    barrier = threading.Barrier(4, timeout=30)

    def run():
        barrier.wait()
        return init(LocalFileStorage(root))

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: run(), range(4)))
    assert sorted(r.action for r in results) == ["already-initialized"] * 3 + ["initialized"]
    assert {r.storage_id for r in results} == {storage_identity.read_marker(store)} == {_recorded(world)}
    assert [name for name in os.listdir(root) if name.startswith(MARKER_NAME)] == [MARKER_NAME]
