"""The local file store, on a real (temporary) directory. No database needed."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

import pytest

from app import storage as storage_module
from app.storage import INCOMING_DIR, LocalFileStorage, StorageError, storage_key


@pytest.fixture
def store(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "uploads")


def _key() -> tuple[str, uuid.UUID, uuid.UUID]:
    course_id, document_id = uuid.uuid4(), uuid.uuid4()
    return storage_key(course_id, document_id), course_id, document_id


def _incoming(store: LocalFileStorage) -> list[str]:
    return sorted(os.listdir(store.root / INCOMING_DIR))


def test_nothing_is_created_until_first_use(tmp_path):
    LocalFileStorage(tmp_path / "uploads")
    assert not (tmp_path / "uploads").exists()


def test_key_layout_uses_server_generated_ids_only():
    key, course_id, document_id = _key()
    assert key == f"{course_id}/{document_id}.pdf"


@pytest.mark.parametrize(
    "bad_key",
    [
        "../x.pdf",
        "/etc/passwd",
        f"{uuid.uuid4()}/../../{uuid.uuid4()}.pdf",
        f"{uuid.uuid4()}/{uuid.uuid4()}.exe",
        f"{uuid.uuid4()}/Lecture 01.pdf",
        f"{str(uuid.uuid4()).upper()}/{uuid.uuid4()}.pdf",
        f"{uuid.uuid4()}/{uuid.uuid4()}.pdf/../x",
        f"{uuid.uuid4()}/{uuid.uuid4()}.pdf\n",  # `$` alone would accept the trailing newline
        f"\n{uuid.uuid4()}/{uuid.uuid4()}.pdf",
        f"{uuid.uuid4()}/{uuid.uuid4()}.pdf ",
    ],
)
def test_keys_that_are_not_two_canonical_uuids_are_refused(store, bad_key):
    with pytest.raises(StorageError):
        store.path_for(bad_key)
    with store.stage() as staged:
        with pytest.raises(StorageError):
            store.place(staged, bad_key)


def test_stage_and_place_stores_exact_bytes_with_restricted_modes(store):
    key, course_id, document_id = _key()
    with store.stage() as staged:
        staged.write(b"%PDF-1.4 hello")
        store.place(staged, key)
        staged.release()
    final = store.root / str(course_id) / f"{document_id}.pdf"
    assert final.read_bytes() == b"%PDF-1.4 hello"
    assert stat.S_IMODE(final.stat().st_mode) == 0o640
    assert stat.S_IMODE((store.root / str(course_id)).stat().st_mode) & 0o027 == 0  # no group write, no other access
    assert store.path_for(key) == final
    assert _incoming(store) == []


def test_temporary_file_is_removed_when_the_operation_fails(store):
    with pytest.raises(RuntimeError):
        with store.stage() as staged:
            staged.write(b"partial")
            assert _incoming(store) == [staged.temp_path.name]
            raise RuntimeError("client went away")
    assert _incoming(store) == []


def test_placement_never_overwrites_an_existing_file(store):
    key, course_id, document_id = _key()
    with store.stage() as first:
        first.write(b"first")
        store.place(first, key)
        first.release()
    with store.stage() as second:
        second.write(b"second")
        with pytest.raises(StorageError):
            store.place(second, key)
        assert second.placed_path is None  # nothing of this operation's at the final name
        store.remove_placed(second)  # a no-op: it must not touch the other upload's file
    assert (store.root / str(course_id) / f"{document_id}.pdf").read_bytes() == b"first"
    assert _incoming(store) == []


def test_remove_placed_removes_only_this_operations_file_and_never_after_release(store):
    key_a, course_id, doc_a = _key()
    key_b = storage_key(course_id, uuid.uuid4())
    with store.stage() as a:
        a.write(b"a")
        store.place(a, key_a)
        a.release()
        store.remove_placed(a)  # released: must be a no-op
    with store.stage() as b:
        b.write(b"b")
        store.place(b, key_b)
        store.remove_placed(b)
    assert (store.root / str(course_id) / f"{doc_a}.pdf").read_bytes() == b"a"
    assert sorted(p.name for p in (store.root / str(course_id)).iterdir()) == [f"{doc_a}.pdf"]


def test_symlinked_course_directory_is_refused(store, tmp_path):
    key, course_id, _ = _key()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.root.mkdir(parents=True, exist_ok=True)
    os.symlink(elsewhere, store.root / str(course_id))
    with store.stage() as staged:
        staged.write(b"data")
        with pytest.raises(StorageError):
            store.place(staged, key)
    with pytest.raises(StorageError):
        store.path_for(key)
    assert list(elsewhere.iterdir()) == []


def test_symlinked_incoming_directory_is_refused(store, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.root.mkdir(parents=True, exist_ok=True)
    os.symlink(elsewhere, store.root / INCOMING_DIR)
    with pytest.raises(StorageError):
        with store.stage():
            pass
    assert list(elsewhere.iterdir()) == []


def test_failure_after_link_leaves_the_final_name_owned_for_cleanup(store, monkeypatch):
    """If fsync of the directory fails after os.link, the caller can still remove exactly that file."""
    key, course_id, document_id = _key()
    root = store.root
    real = storage_module._fsync_directory

    def failing_fsync(path):  # the root syncs fine; the course directory (after the link) does not
        if Path(path) != root:
            raise OSError("simulated fsync failure")
        return real(path)

    monkeypatch.setattr(storage_module, "_fsync_directory", failing_fsync)
    with store.stage() as staged:
        staged.write(b"data")
        with pytest.raises(StorageError):
            store.place(staged, key)
        assert staged.placed_path == store.root / str(course_id) / f"{document_id}.pdf"
        assert staged.placed_path.exists()
        store.remove_placed(staged)
    assert not (store.root / str(course_id) / f"{document_id}.pdf").exists()
    assert _incoming(store) == []


def test_root_that_cannot_be_created_is_a_storage_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    store = LocalFileStorage(blocker / "uploads")
    with pytest.raises(StorageError):
        with store.stage():
            pass


# --- durability: what is fsynced, in which order, and failures -----------------------------


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, object]]:
    """Records file fsyncs, directory fsyncs and links, in order, then performs them."""
    recorded: list[tuple[str, object]] = []
    real_fsync, real_link = os.fsync, os.link

    def fsync(fd):
        recorded.append(("fsync-file", fd))
        return real_fsync(fd)

    def fsync_dir(path):
        recorded.append(("fsync-dir", Path(path)))
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            real_fsync(fd)  # the real call, not recorded as a file fsync
        finally:
            os.close(fd)

    def link(src, dst):
        recorded.append(("link", Path(dst)))
        return real_link(src, dst)

    monkeypatch.setattr(storage_module.os, "fsync", fsync)
    monkeypatch.setattr(storage_module, "_fsync_directory", fsync_dir)
    monkeypatch.setattr(storage_module.os, "link", link)
    return recorded


def _place(store: LocalFileStorage, key: str) -> None:
    with store.stage() as staged:
        staged.write(b"%PDF-1.4 data")
        store.place(staged, key)
        staged.release()


@pytest.mark.parametrize("course_dir_exists", [False, True], ids=["new-course-dir", "existing-course-dir"])
def test_placement_syncs_file_root_and_course_directory_in_order(store, calls, course_dir_exists):
    key, course_id, document_id = _key()
    root = store.root
    if course_dir_exists:  # e.g. created a moment ago by a concurrent upload that has not synced the root yet
        (root / str(course_id)).mkdir()
    calls.clear()

    _place(store, key)

    course_dir = root / str(course_id)
    kinds = [kind if kind != "fsync-dir" else ("fsync-dir", value) for kind, value in calls]
    assert kinds == [
        "fsync-file",  # the data
        ("fsync-dir", root),  # the course directory's entry, whoever created it
        "link",
        ("fsync-dir", course_dir),  # the file's name
    ]
    assert calls[2][1] == course_dir / f"{document_id}.pdf"


def test_root_created_by_the_store_syncs_every_new_level(tmp_path, calls):
    store = LocalFileStorage(tmp_path / "a" / "b" / "uploads")
    _ = store.root
    assert [value for kind, value in calls if kind == "fsync-dir"] == [
        tmp_path,
        tmp_path / "a",
        tmp_path / "a" / "b",
    ]


def test_existing_root_is_not_recreated(tmp_path, calls):
    (tmp_path / "uploads").mkdir()
    _ = LocalFileStorage(tmp_path / "uploads").root
    assert calls == []


@pytest.mark.parametrize("failing", ["file", "root"])
def test_sync_failure_before_the_link_places_nothing(store, monkeypatch, failing):
    key, course_id, document_id = _key()
    root = store.root
    if failing == "file":
        def fail(fd):
            raise OSError("simulated fsync failure")
        monkeypatch.setattr(storage_module.os, "fsync", fail)
    else:
        real = storage_module._fsync_directory

        def fail_root(path):
            if Path(path) == root:
                raise OSError("simulated fsync failure")
            return real(path)
        monkeypatch.setattr(storage_module, "_fsync_directory", fail_root)

    with store.stage() as staged:
        staged.write(b"data")
        with pytest.raises(StorageError):
            store.place(staged, key)
        assert staged.placed_path is None
    assert not (root / str(course_id) / f"{document_id}.pdf").exists()
    assert _incoming(store) == []


def test_temporary_file_that_cannot_be_removed_is_logged_not_raised(store, monkeypatch, caplog):
    real_unlink = os.unlink

    def refuse_temp(path, *args, **kwargs):
        if INCOMING_DIR in str(path):
            raise PermissionError("simulated")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "unlink", refuse_temp)
    with caplog.at_level("WARNING", logger="app.storage"):
        with store.stage() as staged:
            staged.write(b"partial")
            name = staged.temp_path.name
    assert "upload_reconcile" in caplog.text and name in caplog.text
    monkeypatch.undo()
    assert _incoming(store) == [name]  # left for reconciliation
