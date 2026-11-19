"""Course documents: listing and accepting an upload.

Accepting an upload creates two kinds of state that share no transaction: a
file in storage and, in PostgreSQL, the document row plus its first ingestion
job. ingest_upload() orders the steps so that a committed document always
points to a stored file, and so that a failure only ever removes files this
operation created:

1. Validate the name and declared type; check that the upload directory is
   the store this database is bound to (storage identity,
   app/storage_identity.py), else 503 before any file is written; then
   stream the body into a staged temporary file (size limit, SHA-256, PDF
   signature) and fsync it.
2. Open the write transaction: record this connection's backend identity,
   re-check course access with FOR SHARE locks, look for a duplicate, insert
   the document and its queued parse job. A unique violation of the
   course-scoped dedup index (and only that one) becomes 409.
3. Link the staged file to its final name (never overwriting).
4. COMMIT, with the response already built so that nothing after a
   successful commit touches the database.
   - Commit succeeded: release the file to the database and return 202.
   - Commit raised: the outcome is established, not assumed. A fresh
     connection checks whether the original backend has ended its transaction
     and then whether the document row exists. Committed: keep the file and
     return 202. Rolled back: remove the file this operation placed. Not
     determinable: keep the file, log a reconciliation event and answer 503.

Publishing the parse job is not part of this function: once it returns (the
commit is confirmed), the router schedules app.ingestion.dispatch.
publish_new_job to run after the response. If that publication fails or never
happens (broker down, process crash), the dispatch sweep publishes the job
from the table; an unconfirmed (503) upload is never published here.
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import exists, func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser
from ..errors import ApiError
from ..models import Document, IngestionJob, User
from ..schemas.documents import (
    DocumentItem,
    DocumentUploadResponse,
    as_utc,
    public_error,
    public_kind,
    public_origin,
    public_status,
)
from .. import storage_identity
from ..storage import LocalFileStorage, StagedFile, StorageError, storage_key
from ..storage_identity import StorageIdentityError
from ..uploads import FileFacts, check_declared_type, copy_and_inspect, normalize_filename
from . import course_access

log = logging.getLogger(__name__)

DEDUP_INDEX = "documents_course_id_content_hash_idx"
UPLOAD_KIND = "notes"  # a default, not inferred from the file
UPLOAD_LANGUAGE = "en"  # the baseline default; language detection belongs to parsing (M3)
# SQLSTATE classes whose errors say nothing reliable about a COMMIT's outcome:
# connection exception, operator intervention (e.g. admin shutdown),
# insufficient resources, system error, internal error.
_INDETERMINATE_SQLSTATE_CLASSES = ("08", "57", "53", "58", "XX")


@dataclass(frozen=True)
class IncomingFile:
    filename: str | None
    content_type: str | None
    file: BinaryIO


class CommitOutcome(enum.Enum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _Backend:
    pid: int
    started: object  # backend_start, compared as returned by the server


def upload_failed() -> ApiError:
    return ApiError(500, "upload_failed", "The upload could not be completed. Try again later.")


def storage_failed() -> ApiError:
    return ApiError(500, "storage_error", "The file could not be saved. Try again later.")


def storage_unavailable() -> ApiError:
    return ApiError(503, "storage_unavailable", "Uploads are temporarily unavailable. Try again later.")


def upload_unconfirmed() -> ApiError:
    return ApiError(
        503,
        "upload_unconfirmed",
        "The upload could not be confirmed. Check the course materials before uploading the file again.",
    )


def read_failed(exc: SQLAlchemyError) -> ApiError:
    """A database read failed: 503 when the database could not be reached, 500 otherwise."""
    if isinstance(exc, OperationalError) or (isinstance(exc, DBAPIError) and exc.connection_invalidated):
        return ApiError(503, "database_unavailable", "Course materials are temporarily unavailable. Try again later.")
    return ApiError(500, "server_error", "Something went wrong on the server. Try again later.")


# --- listing ---------------------------------------------------------------------------


def list_documents(session_factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID) -> list[DocumentItem]:
    job_running = exists().where(IngestionJob.document_id == Document.id, IngestionJob.status == "running")
    # Parsing finished; indexing (M4) has not: still processing, never ready.
    parsed = exists().where(
        IngestionJob.document_id == Document.id, IngestionJob.kind == "parse", IngestionJob.status == "succeeded"
    )
    query = (
        select(
            Document.id,
            Document.title,
            Document.origin,
            Document.kind,
            Document.status,
            Document.page_count,
            Document.url,
            Document.licence,
            Document.created_at,
            Document.error_message,
            User.display_name,
            job_running.label("job_running"),
            parsed.label("parsed"),
        )
        .outerjoin(User, User.id == Document.uploaded_by)
        .where(Document.course_id == course_id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    try:
        with session_factory() as session:
            course_access.require_read(session, user, course_id)
            rows = session.execute(query).all()
    except SQLAlchemyError as exc:
        log.exception("listing documents failed course_id=%s", course_id)
        raise read_failed(exc) from None
    return [
        DocumentItem(
            id=row.id,
            title=row.title,
            origin=public_origin(row.origin),
            kind=public_kind(row.kind, row.origin),
            status=public_status(row.status, job_running=row.job_running, parsed=row.parsed),
            pages=row.page_count,
            source_name=None,  # filled from sources once harvesting exists
            url=row.url,
            licence=row.licence,
            added_by=row.display_name,
            added_at=as_utc(row.created_at),
            error=public_error(row.status, row.error_message),
        )
        for row in rows
    ]


# --- upload --------------------------------------------------------------------------------


def authorize_upload(
    session_factory: sessionmaker[Session],
    user: CurrentUser,
    course_id: uuid.UUID,
    storage: LocalFileStorage | None = None,
) -> None:
    """Checked before the request body is read; re-checked inside the write transaction.

    With `storage`, also checks (after access, so callers without access
    learn nothing about storage) that the upload store is available.
    """
    try:
        with session_factory() as session:
            course_access.require_upload(session, user, course_id)
            if storage is not None:
                _verify_store(session, storage)
    except SQLAlchemyError as exc:
        log.exception("upload authorization failed course_id=%s", course_id)
        raise read_failed(exc) from None


def _verify_store(session: Session, storage: LocalFileStorage) -> None:
    try:
        storage_identity.verify(session, storage)
    except StorageIdentityError as exc:
        log.error("upload_storage_unavailable reason=%s", exc)
        raise storage_unavailable() from None


def require_upload_store(session_factory: sessionmaker[Session], storage: LocalFileStorage) -> None:
    """503 unless the upload directory is the store this database is bound to."""
    try:
        with session_factory() as session:
            _verify_store(session, storage)
    except SQLAlchemyError as exc:
        log.exception("upload storage check failed")
        raise read_failed(exc) from None


def ingest_upload(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    user: CurrentUser,
    course_id: uuid.UUID,
    incoming: IncomingFile,
    *,
    max_bytes: int,
) -> DocumentUploadResponse:
    name = normalize_filename(incoming.filename)
    mime_type = check_declared_type(name, incoming.content_type)
    require_upload_store(session_factory, storage)  # authoritative check, right before writing
    document_id = uuid.uuid4()
    key = storage_key(course_id, document_id)
    try:
        with storage.stage() as staged:
            try:
                facts = copy_and_inspect(incoming.file, staged, name=name, max_bytes=max_bytes)
            except OSError as exc:
                raise StorageError("could not write the staged upload") from exc
            return _persist(
                session_factory,
                storage,
                staged,
                user=user,
                course_id=course_id,
                document_id=document_id,
                key=key,
                name=name,
                mime_type=mime_type,
                facts=facts,
            )
    except StorageError:
        # Includes a failure to remove this upload's own file after a confirmed
        # rollback; the log line carries the key for reconciliation.
        log.exception("upload storage failure course_id=%s document_id=%s storage_key=%s", course_id, document_id, key)
        raise storage_failed() from None


def _persist(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    staged: StagedFile,
    *,
    user: CurrentUser,
    course_id: uuid.UUID,
    document_id: uuid.UUID,
    key: str,
    name: str,
    mime_type: str,
    facts: FileFacts,
) -> DocumentUploadResponse:
    job_id = uuid.uuid4()
    session = session_factory()
    try:
        try:
            backend = _backend_identity(session)
            access = course_access.require_upload(session, user, course_id, lock=True)
            duplicate = _find_duplicate(session, course_id, facts.sha256)
            if duplicate is not None:
                raise _duplicate_error(duplicate)
            created_at = session.execute(
                insert(Document)
                .values(
                    id=document_id,
                    course_id=course_id,
                    origin="class",
                    title=name,
                    language=UPLOAD_LANGUAGE,
                    content_hash=facts.sha256,
                    status="pending",
                    # Naive UTC, matching how the baseline column is interpreted,
                    # whatever the session's TimeZone setting is.
                    created_at=func.timezone("UTC", func.now()),
                    uploaded_by=user.id,
                    original_filename=name,
                    storage_key=key,
                    mime_type=mime_type,
                    size_bytes=facts.size_bytes,
                    kind=UPLOAD_KIND,
                )
                .returning(Document.created_at)
            ).scalar_one()
            session.execute(
                insert(IngestionJob).values(id=job_id, document_id=document_id, kind="parse", status="queued")
            )
        except IntegrityError as exc:
            _rollback_quietly(session)
            if _violated_constraint(exc) == DEDUP_INDEX:
                # A concurrent upload of the same file committed first.
                raise _duplicate_error(_existing_duplicate(session, course_id, facts.sha256)) from None
            log.exception("upload insert failed course_id=%s document_id=%s", course_id, document_id)
            raise upload_failed() from None
        except SQLAlchemyError:
            log.exception("upload insert failed course_id=%s document_id=%s", course_id, document_id)
            _rollback_quietly(session)
            raise upload_failed() from None

        # Built before the file is placed: between placement and COMMIT nothing else can fail.
        response = DocumentUploadResponse(
            id=document_id,
            title=name,
            origin="class",
            kind=UPLOAD_KIND,
            status="queued",
            pages=None,
            source_name=None,
            url=None,
            licence=None,
            added_by=access.display_name,
            added_at=as_utc(created_at),
            error=None,
            job_id=job_id,
        )

        try:
            storage.place(staged, key)
        except StorageError:
            log.exception("upload placement failed course_id=%s document_id=%s", course_id, document_id)
            _rollback_quietly(session)
            storage.remove_placed(staged)  # only if the final name was already created by this operation
            raise storage_failed() from None

        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 - every commit failure is classified below
            outcome = _commit_outcome(exc, session_factory, document_id, backend)
            if outcome is CommitOutcome.COMMITTED:
                staged.release()
                log.warning("commit raised but document %s is committed; keeping its file", document_id)
                return response
            if outcome is CommitOutcome.ROLLED_BACK:
                log.warning("commit failed and was rolled back document_id=%s", document_id, exc_info=exc)
                storage.remove_placed(staged)
                raise upload_failed() from None
            staged.release()  # it may be referenced by a committed row: never delete it here
            log.error(
                "upload_reconcile document_id=%s course_id=%s storage_key=%s: commit outcome unknown; "
                "file kept for reconciliation",
                document_id,
                course_id,
                key,
                exc_info=exc,
            )
            raise upload_unconfirmed() from None
        staged.release()
        return response
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - closing a broken connection must not mask the outcome
            log.warning("closing the upload session failed", exc_info=True)


def _backend_identity(session: Session) -> _Backend:
    row = session.execute(
        text("SELECT pid, backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()")
    ).one()
    return _Backend(pid=row.pid, started=row.backend_start)


def _find_duplicate(session: Session, course_id: uuid.UUID, sha256: str) -> tuple[uuid.UUID, str] | None:
    row = session.execute(
        select(Document.id, Document.title).where(
            Document.course_id == course_id,
            Document.origin == "class",
            Document.storage_key.is_not(None),
            Document.content_hash == sha256,
        )
    ).first()
    return None if row is None else (row.id, row.title)


def _existing_duplicate(session: Session, course_id: uuid.UUID, sha256: str) -> tuple[uuid.UUID, str] | None:
    try:
        return _find_duplicate(session, course_id, sha256)
    except SQLAlchemyError:
        log.warning("could not look up the duplicate document", exc_info=True)
        return None


def _duplicate_error(duplicate: tuple[uuid.UUID, str] | None) -> ApiError:
    if duplicate is None:
        return ApiError(409, "duplicate_document", "This file is already in this course.")
    document_id, title = duplicate
    return ApiError(
        409,
        "duplicate_document",
        f"This file is already in this course as {title}.",
        extra={"document_id": str(document_id)},
    )


def _violated_constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _rollback_quietly(session: Session) -> None:
    try:
        session.rollback()
    except Exception:  # noqa: BLE001
        log.warning("rollback failed", exc_info=True)


def _server_rejected(exc: BaseException) -> bool:
    """True when PostgreSQL itself answered the COMMIT with an error.

    Then the transaction is known to be rolled back. Errors without an
    SQLSTATE (raised client-side) and connection-level or operator errors are
    not proof of anything.
    """
    if not isinstance(exc, DBAPIError) or exc.connection_invalidated:
        return False
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return bool(sqlstate) and not sqlstate.startswith(_INDETERMINATE_SQLSTATE_CLASSES)


def _commit_outcome(
    exc: BaseException, session_factory: sessionmaker[Session], document_id: uuid.UUID, backend: _Backend
) -> CommitOutcome:
    """Establish what a COMMIT that raised actually did, on a fresh connection.

    The original backend is checked first: while it still has a transaction
    open (or is still processing the COMMIT), the row being absent proves
    nothing. Once it has ended its transaction or exited, the presence of the
    document row is the outcome.
    """
    try:
        with session_factory() as probe:
            busy = probe.execute(
                text(
                    # coalesce: a hidden state (NULL) counts as busy, never as finished.
                    "SELECT coalesce(state, '') <> 'idle' OR xact_start IS NOT NULL FROM pg_stat_activity "
                    "WHERE pid = :pid AND backend_start = :started"
                ),
                {"pid": backend.pid, "started": backend.started},
            ).scalar_one_or_none()
            if busy:
                return CommitOutcome.UNKNOWN
            committed = probe.execute(select(Document.id).where(Document.id == document_id)).first() is not None
            return CommitOutcome.COMMITTED if committed else CommitOutcome.ROLLED_BACK
    except Exception:  # noqa: BLE001 - verification is best effort; fall back to what the error proves
        log.warning("could not verify the commit outcome of document %s", document_id, exc_info=True)
    return CommitOutcome.ROLLED_BACK if _server_rejected(exc) else CommitOutcome.UNKNOWN
