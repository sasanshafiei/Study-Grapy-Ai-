"""Which upload store a database's stored files live in.

A store carries a marker file at its root, `.studygraph-storage`:

    {"kind": "studygraph-upload-store", "version": 1, "storage_id": "<uuid>"}

and the database records the same id in the single-row `storage_identity`
table. verify() requires both to exist and match. The API checks it before it
stores an upload and the worker before it claims a job, so a missing, empty,
wrong or unreadable mount (a typo in a volume name creates a new empty volume)
fails closed for every job instead of making each document look like it lost
its file, and the API and the worker cannot silently use different stores.

The binding is only ever created by initialize(), which the operator runs
explicitly (`python -m app.ingestion.admin init-storage`). The database
record is always committed before the marker is written, so a marker that
exists without a matching record was not written by this database's
initialization: it belongs to a store initialized for another database (or
another deployment) and is never adopted without adopt_existing. An
interrupted initialization leaves the opposite state (record, no marker),
which a re-run completes when the store and the database hold no uploads.
Nothing is bound to a store or database that already holds uploads unless
told to (adopt_existing), and then only if every stored document's file is
present (or allow_missing is given as well).

The marker is read through a descriptor of the root, without following
symlinks, must be a small regular file and must parse exactly; anything else
is "not established".
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from .storage import (
    FILE_MODE,
    INCOMING_DIR,
    INCOMING_LOCK,
    LocalFileStorage,
    StorageError,
    StoredFileInvalid,
    _open_plain_file,
)

MARKER_NAME = ".studygraph-storage"
MARKER_KIND = "studygraph-upload-store"
MARKER_VERSION = 1
MAX_MARKER_BYTES = 4096
# Root entries that do not make a store "in use".
_HOUSEKEEPING_ENTRIES = frozenset({INCOMING_DIR, INCOMING_LOCK, MARKER_NAME})

log = logging.getLogger(__name__)


class StorageIdentityError(StorageError):
    """The identity of the upload store could not be established (details for the log)."""


def _marker_bytes(storage_id: uuid.UUID) -> bytes:
    body = {"kind": MARKER_KIND, "version": MARKER_VERSION, "storage_id": str(storage_id)}
    return (json.dumps(body, sort_keys=True) + "\n").encode("ascii")


def parse_marker(raw: bytes) -> uuid.UUID:
    """The storage id in a marker file's content; StorageIdentityError if it is not exactly a marker."""
    try:
        body = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise StorageIdentityError("the storage marker is not valid JSON") from None
    if not isinstance(body, dict) or set(body) != {"kind", "version", "storage_id"}:
        raise StorageIdentityError("the storage marker has unexpected content")
    if body["kind"] != MARKER_KIND or body["version"] != MARKER_VERSION or type(body["version"]) is not int:
        raise StorageIdentityError("the storage marker is of an unknown kind or version")
    value = body["storage_id"]
    try:
        storage_id = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        storage_id = None
    if storage_id is None or str(storage_id) != value:
        raise StorageIdentityError("the storage marker id is not a canonical UUID")
    return storage_id


def read_marker(storage: LocalFileStorage) -> uuid.UUID | None:
    """The id in the store's marker, or None if there is no marker file.

    StorageIdentityError if the store is unavailable or the marker is unsafe
    (a symlink, not a regular file, too large) or malformed.
    """
    try:
        root_fd = storage.open_root()
    except StorageError as exc:
        raise StorageIdentityError("the upload directory is not available") from exc
    try:
        try:
            fd = _open_plain_file(MARKER_NAME, root_fd, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except StoredFileInvalid:
            raise StorageIdentityError("the storage marker is not a regular file") from None
        except OSError as exc:
            raise StorageIdentityError("the storage marker could not be opened") from exc
        try:
            raw = os.read(fd, MAX_MARKER_BYTES + 1)
        except OSError as exc:
            raise StorageIdentityError("the storage marker could not be read") from exc
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    if len(raw) > MAX_MARKER_BYTES:
        raise StorageIdentityError("the storage marker is too large")
    return parse_marker(raw)


def recorded_identity(session: Session) -> uuid.UUID | None:
    return session.execute(text("SELECT storage_id FROM storage_identity WHERE id = 1")).scalar_one_or_none()


def verify(session: Session, storage: LocalFileStorage) -> uuid.UUID:
    """The established storage id; StorageIdentityError if the store is not the database's store.

    Database errors propagate unchanged (the caller decides how to report them).
    """
    recorded = recorded_identity(session)
    marker = read_marker(storage)
    if recorded is None:
        raise StorageIdentityError("no upload store is recorded in the database (run init-storage)")
    if marker is None:
        raise StorageIdentityError("the upload directory has no storage marker (wrong or empty mount?)")
    if marker != recorded:
        raise StorageIdentityError("the upload directory belongs to another store (marker mismatch)")
    return recorded


def write_marker(storage: LocalFileStorage, storage_id: uuid.UUID) -> None:
    """Create the marker file, never replacing an existing one, and make it durable."""
    try:
        root_fd = storage.open_root()
    except StorageError as exc:
        raise StorageIdentityError("the upload directory is not available") from exc
    temp_name = f"{MARKER_NAME}.tmp-{secrets.token_hex(8)}"
    try:
        try:
            fd = os.open(
                temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE, dir_fd=root_fd
            )
        except OSError as exc:
            raise StorageIdentityError("could not create the storage marker") from exc
        try:
            try:
                os.write(fd, _marker_bytes(storage_id))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temp_name, MARKER_NAME, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            except FileExistsError:
                raise StorageIdentityError("a storage marker already exists") from None
        except OSError as exc:
            raise StorageIdentityError("could not write the storage marker") from exc
        finally:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
        try:
            os.fsync(root_fd)
        except OSError as exc:
            raise StorageIdentityError("could not sync the storage marker") from exc
    finally:
        os.close(root_fd)


@dataclass(frozen=True)
class InitResult:
    storage_id: uuid.UUID
    action: str  # "already-initialized" | "initialized" | "adopted" | "completed"
    stored_documents: int
    missing_files: int


# Session-level advisory lock serializing initialize() runs, held across the
# COMMIT of the record and the writing of the marker ("STG_STOR").
INIT_LOCK_KEY = 0x5354475F53544F52


def _root_in_use(storage: LocalFileStorage) -> bool:
    """True when the store holds anything besides housekeeping entries."""
    try:
        root_fd = storage.open_root()
    except StorageError as exc:
        raise StorageIdentityError("the upload directory is not available") from exc
    try:
        return any(name not in _HOUSEKEEPING_ENTRIES for name in os.listdir(root_fd))
    finally:
        os.close(root_fd)


def initialize(
    engine: Engine,
    storage: LocalFileStorage,
    *,
    adopt_existing: bool = False,
    allow_missing: bool = False,
) -> InitResult:
    """Bind the database to this upload store (explicit operator action).

    Order: the record is committed first, the marker written second, under a
    session-level advisory lock held across both, so concurrent runs
    serialize and a marker never exists without having been recorded by
    *this* database's initialization. Outcomes:

    - record and marker match: already initialized, nothing changes;
    - record and a different marker: refused (another store);
    - marker without a record: the store was initialized for another
      database; refused unless adopt_existing (then its id is recorded);
    - record without a marker: an interrupted initialization (or a wrong,
      empty mount). Completed automatically only when neither the store nor
      the database holds uploads; otherwise adopt_existing is required;
    - neither: a new id is recorded and written, if neither side holds
      uploads, or with adopt_existing.

    Whenever stored documents exist, every one's file must be present here,
    unless allow_missing. A refusal changes nothing (StorageIdentityError).
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": INIT_LOCK_KEY})
        connection.commit()
        try:
            result = _initialize_locked(connection, storage, adopt_existing, allow_missing)
        finally:
            connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INIT_LOCK_KEY})
            connection.commit()
    if result.action != "already-initialized":
        log.warning(
            "storage_identity %s storage_id=%s stored_documents=%s missing_files=%s",
            result.action, result.storage_id, result.stored_documents, result.missing_files,
        )  # fmt: skip
    return result


def _check_files(connection: Connection, storage: LocalFileStorage, stored: int, allow_missing: bool) -> int:
    if not stored:
        return 0
    keys = connection.execute(text("SELECT storage_key FROM documents WHERE storage_key IS NOT NULL")).scalars().all()
    missing = sum(1 for key in keys if storage.stored_file_state(key) != "present")
    if missing and not allow_missing:
        raise StorageIdentityError(
            f"{missing} of {stored} stored documents have no file in this directory; refusing to bind it. "
            "Investigate, or run again with --adopt-existing --allow-missing."
        )
    return missing


def _initialize_locked(
    connection: Connection, storage: LocalFileStorage, adopt_existing: bool, allow_missing: bool
) -> InitResult:
    recorded = connection.execute(text("SELECT storage_id FROM storage_identity WHERE id = 1")).scalar_one_or_none()
    marker = read_marker(storage)
    stored = connection.execute(text("SELECT count(*) FROM documents WHERE storage_key IS NOT NULL")).scalar_one()
    in_use = _root_in_use(storage)
    empty = stored == 0 and not in_use

    if recorded is not None and marker == recorded:
        return InitResult(recorded, "already-initialized", stored, 0)
    if recorded is not None and marker is not None:
        raise StorageIdentityError("the database is bound to a different upload store (marker mismatch)")
    if marker is not None:  # and no record
        if not adopt_existing:
            raise StorageIdentityError(
                "this upload directory was initialized for another database (it has a marker this database "
                "never recorded). Check that it is the right store, then run again with --adopt-existing."
            )
        missing = _check_files(connection, storage, stored, allow_missing)
        connection.execute(text("INSERT INTO storage_identity (id, storage_id) VALUES (1, :sid)"), {"sid": marker})
        connection.commit()
        return InitResult(marker, "adopted", stored, missing)
    if recorded is not None:  # and no marker
        if not empty and not adopt_existing:
            raise StorageIdentityError(
                "the database is bound to an upload store, but this directory has no marker: wrong or empty "
                "mount? If this really is the store, run again with --adopt-existing."
            )
        missing = _check_files(connection, storage, stored, allow_missing)
        write_marker(storage, recorded)
        return InitResult(recorded, "completed", stored, missing)
    if not empty and not adopt_existing:
        raise StorageIdentityError(
            f"refusing to bind a store automatically: {stored} stored documents in the database, "
            f"upload directory {'in use' if in_use else 'empty'}. Check that this is their store, then "
            "run again with --adopt-existing."
        )
    missing = _check_files(connection, storage, stored, allow_missing)
    storage_id = uuid.uuid4()
    connection.execute(text("INSERT INTO storage_identity (id, storage_id) VALUES (1, :sid)"), {"sid": storage_id})
    connection.commit()  # recorded first ...
    write_marker(storage, storage_id)  # ... then written: an interruption leaves "record, no marker"
    return InitResult(storage_id, "initialized" if empty else "adopted", stored, missing)
