"""File reconciliation: clean up what is provably garbage, report everything else.

Runs periodically on the maintenance worker. Before anything else it
establishes the store's identity (marker == database record); without that,
including whenever the database is unreachable, it does nothing at all.

1. Temporary files in .incoming (storage.clean_abandoned_temp_files): a
   `staged-*.part` file is removed only when no upload owns it any more (its
   owner lock can be taken while .incoming.lock is held exclusively), it is
   older than RECONCILE_TEMP_GRACE_SECONDS, and its name still refers to the
   inode that was checked. Removing a temporary name never removes a stored
   file: a committed document references its final name, which is a separate
   link to the inode. M2-era `upload-*.part` names, symlinks and anything else
   are reported, never touched.
2. Stored files without a document row ("orphans", left by a crash between
   placing a file and committing): reported only, never moved or deleted. A
   file whose upload still holds its lock is in flight and not reported, and
   the database is asked again after that check. Automatic removal is not
   done because an upload whose COMMIT outcome was unknown may still have
   committed, and nothing short of that transaction's end proves otherwise.
3. Documents whose stored file is missing: reported only. A queued document
   fails through its parse job (pipeline.py); a parsed or indexed document is
   left as it is.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .. import storage_identity
from ..config import Settings
from ..storage import LocalFileStorage, StorageError, TempScan
from ..storage_identity import StorageIdentityError

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    skipped: str | None = None  # why nothing was done
    temp: TempScan | None = None  # None: .incoming was busy or the step failed
    orphans: list[str] = field(default_factory=list)  # storage keys
    missing: list[str] = field(default_factory=list)  # document ids
    invalid: list[str] = field(default_factory=list)  # document ids whose stored name is not a plain file
    errors: list[str] = field(default_factory=list)


def reconcile(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    settings: Settings,
    rng: random.Random | None = None,
) -> ReconcileReport:
    """One bounded reconciliation run.

    Each of the three scans examines at most RECONCILE_BATCH_SIZE entries,
    starting at a random point and wrapping around, so a store larger than
    the batch is covered across runs instead of always the same first entries
    (coverage is probabilistic, not a guaranteed sweep; see the docs).
    """
    rng = rng or random.Random()
    report = ReconcileReport()
    try:
        with session_factory() as session:
            storage_identity.verify(session, storage)
    except (StorageIdentityError, SQLAlchemyError) as exc:
        report.skipped = f"storage identity not established: {type(exc).__name__}"
        log.error("reconcile_skipped reason=%s", report.skipped)
        return report

    try:
        report.temp = storage.clean_abandoned_temp_files(
            grace_seconds=settings.reconcile_temp_grace_seconds, limit=settings.reconcile_batch_size, rng=rng
        )
    except StorageError as exc:
        report.errors.append(f"temp: {exc}")
        log.error("reconcile_temp_failed error=%s", exc)
    _log_temp(report.temp)

    try:
        _report_orphans(session_factory, storage, settings, report, rng)
    except (SQLAlchemyError, StorageError) as exc:
        report.errors.append(f"orphans: {type(exc).__name__}")
        log.error("reconcile_orphans_failed error=%s", type(exc).__name__)

    try:
        _report_missing(session_factory, storage, settings, report, rng)
    except (SQLAlchemyError, StorageError) as exc:
        report.errors.append(f"missing: {type(exc).__name__}")
        log.error("reconcile_missing_failed error=%s", type(exc).__name__)
    return report


def _log_temp(scan: TempScan | None) -> None:
    if scan is None:
        log.info("reconcile_temp_skipped reason=incoming_busy")
        return
    for name in scan.removed:
        log.warning("reconcile_temp_removed name=%s", name)
    for name in scan.legacy:
        log.warning("reconcile_legacy_temp name=%s action=report_only", name)
    for name in scan.unexpected:
        log.warning("reconcile_unexpected_incoming_entry name=%s action=report_only", name)


def _referenced(session: Session, keys: list[str]) -> set[str]:
    return set(
        session.execute(
            text("SELECT storage_key FROM documents WHERE storage_key = ANY(:keys)"), {"keys": keys}
        ).scalars()
    )


def _report_orphans(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    settings: Settings,
    report: ReconcileReport,
    rng: random.Random,
) -> None:
    cutoff = time.time() - settings.reconcile_orphan_grace_seconds
    candidates = [
        entry.key
        for entry in storage.iter_stored_files(settings.reconcile_batch_size, rng)
        if entry.changed_at <= cutoff
    ]
    if not candidates:
        return
    with session_factory() as session:
        unreferenced = [key for key in candidates if key not in _referenced(session, candidates)]
    settled = [key for key in unreferenced if not storage.stored_file_in_use(key)]
    if not settled:
        return
    with session_factory() as session:  # asked again after the in-flight check
        still = _referenced(session, settled)
    for key in settled:
        if key not in still:
            report.orphans.append(key)
            log.warning("reconcile_orphan_found storage_key=%s action=report_only", key)


_MISSING_PAGE = text(
    "SELECT id, storage_key FROM documents WHERE storage_key IS NOT NULL "
    "AND id > CAST(:after AS uuid) AND (CAST(:until AS uuid) IS NULL OR id <= CAST(:until AS uuid)) "
    "ORDER BY id LIMIT :limit"
)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _report_missing(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    settings: Settings,
    report: ReconcileReport,
    rng: random.Random,
) -> None:
    """Check stored files by document id: from a random id to the end, then from the start up to it."""
    start = str(uuid.UUID(int=rng.getrandbits(128)))
    checked = 0
    for after, until in ((start, None), (_ZERO_UUID, start)):
        while checked < settings.reconcile_batch_size:
            with session_factory() as session:
                rows = session.execute(
                    _MISSING_PAGE,
                    {"after": after, "until": until, "limit": min(200, settings.reconcile_batch_size - checked)},
                ).all()
            if not rows:
                break
            for row in rows:
                state = storage.stored_file_state(row.storage_key)
                if state == "missing":
                    report.missing.append(str(row.id))
                    log.error("reconcile_missing_file document_id=%s action=report_only", row.id)
                elif state == "invalid":
                    report.invalid.append(str(row.id))
                    log.error("reconcile_invalid_file document_id=%s action=report_only", row.id)
            checked += len(rows)
            after = str(rows[-1].id)
