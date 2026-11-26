"""Harvest runs: the durable job state, in SQL (PostgreSQL is the authority; Redis only carries hints).

    queued --claim--> running --complete--> succeeded   (target: harvested generation, next refresh)
                         |--yield (bounded progress, provider throttle)--> queued (claim_count kept)
                         |--failure, failures left--> queued (failure_count + 1, backoff)
                         |--failure, exhausted / permanent--> failed (target backoff)
                         |--opt-out / source disabled--> cancelled
    running, lease expired --recovery--> queued | failed  (counts as a failed attempt)
    queued, no claims or failures left --recovery--> failed

Counters. claim_count is incremented by every claim and bounded by
max_claims: every slice of work, whether it ends in a yield, a failure or
completion, uses one, so a run is finite even if it only ever yields.
failure_count is incremented only by actual failed attempts (an error, a
lost lease) and bounded by max_failures. Neither is ever decremented or
refunded; progress (`progress`, cursors and tallies) is persisted under the
attempt token, so a yielded or retried run continues where it stopped.

Fencing. A claim stores a fresh attempt_token; every write on behalf of the
attempt (progress, persistence of documents, yield, failure, completion) is
conditional on `status = 'running' AND attempt_token = :token`. Recovery,
cancellation and requeueing clear or replace the token, so a stale attempt
writes nothing.

Scheduling. One active (queued or running) run per course x source, enforced
by a partial unique index; the scheduler inserts with ON CONFLICT DO NOTHING.
A run records the course's dirty_generation when it is created; completion
raises the target's harvested_generation only to that value. A class indexing
that completes during the run raises dirty_generation above it, so the
target stays dirty and the next maintenance tick schedules another run: no
notification is lost, whatever the interleaving.

Lock order (app/models.py): harvest_course_settings -> sources ->
harvest_targets -> harvest_runs -> external_resources -> ingestion_jobs ->
documents. No transaction is held across a provider request.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings

log = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed", "cancelled")


@dataclass
class RunClaim:
    run_id: uuid.UUID
    course_id: uuid.UUID
    source_id: uuid.UUID
    token: uuid.UUID
    claim_count: int
    max_claims: int
    failure_count: int
    max_failures: int
    generation: int
    trigger: str
    progress: dict = field(default_factory=dict)


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --- gates ---------------------------------------------------------------------------------------


_GATES = text(
    """
    SELECT g.enabled AS course_enabled, s.enabled AS source_enabled, s.provider, s.language,
           (t.course_id IS NOT NULL) AS targeted
    FROM courses c
    LEFT JOIN harvest_course_settings g ON g.course_id = c.id
    LEFT JOIN harvest_targets t ON t.course_id = c.id AND t.source_id = :source
    LEFT JOIN sources s ON s.id = :source
    WHERE c.id = :course
    """
)


def closed_gate(session: Session, settings: Settings, course_id: uuid.UUID, source_id: uuid.UUID) -> str | None:
    """Why harvesting this course x source must not proceed now; None if every gate is open."""
    if not settings.harvest_enabled:
        return "harvest_disabled"
    row = session.execute(_GATES, {"course": course_id, "source": source_id}).one_or_none()
    if row is None:
        return "course_missing"
    if row.course_enabled is not True:
        return "course_not_opted_in"
    if row.provider is None or row.source_enabled is not True:
        return "source_disabled"
    if not row.targeted:
        return "target_missing"
    return None


# --- scheduling ------------------------------------------------------------------------------------


_DUE_TARGETS = text(
    """
    SELECT t.course_id, t.source_id FROM harvest_targets t
    JOIN harvest_course_settings g ON g.course_id = t.course_id
    JOIN sources s ON s.id = t.source_id
    WHERE g.enabled AND s.enabled IS TRUE AND s.provider IS NOT NULL AND t.not_before <= now()
      AND (g.dirty_generation > t.harvested_generation OR t.next_refresh_at <= now())
      AND NOT EXISTS (SELECT 1 FROM harvest_runs r WHERE r.course_id = t.course_id AND r.source_id = t.source_id
                      AND r.status IN ('queued', 'running'))
    ORDER BY least(t.next_refresh_at, t.updated_at), t.course_id, t.source_id
    LIMIT :limit
    """
)


def create_run(
    session: Session, settings: Settings, course_id: uuid.UUID, source_id: uuid.UUID, *, manual: bool = False
) -> uuid.UUID | None:
    """Create the target's run if it is due (or `manual`) and none is active; the caller commits.

    Locks in order: course settings (shared), source (shared), target. A
    concurrent scheduler either waits and then sees the active run, or loses
    the ON CONFLICT race; a concurrent dirty bump is ordered before or after
    the generation read here, never lost.
    """
    gate = session.execute(
        text("SELECT enabled, dirty_generation FROM harvest_course_settings WHERE course_id = :c FOR SHARE"),
        {"c": course_id},
    ).one_or_none()
    source = session.execute(
        text("SELECT enabled, provider FROM sources WHERE id = :s FOR SHARE"), {"s": source_id}
    ).one_or_none()
    if gate is None or gate.enabled is not True or source is None or source.enabled is not True or source.provider is None:
        return None
    target = session.execute(
        text(
            "SELECT harvested_generation, (next_refresh_at <= now()) AS refresh_due, (not_before <= now()) AS allowed "
            "FROM harvest_targets WHERE course_id = :c AND source_id = :s FOR NO KEY UPDATE"
        ),
        {"c": course_id, "s": source_id},
    ).one_or_none()
    if target is None:
        return None
    dirty = gate.dirty_generation > target.harvested_generation
    if not manual and (not target.allowed or not (dirty or target.refresh_due)):
        return None
    trigger = "manual" if manual else ("dirty" if dirty else "refresh")
    return session.execute(
        text(
            "INSERT INTO harvest_runs (course_id, source_id, trigger, generation, max_claims, max_failures) "
            "VALUES (:c, :s, :trigger, :generation, :max_claims, :max_failures) "
            "ON CONFLICT (course_id, source_id) WHERE status IN ('queued', 'running') DO NOTHING RETURNING id"
        ),
        {
            "c": course_id,
            "s": source_id,
            "trigger": trigger,
            "generation": gate.dirty_generation,
            "max_claims": settings.harvest_run_max_claims,
            "max_failures": settings.harvest_run_max_failures,
        },
    ).scalar_one_or_none()


def schedule(session_factory: sessionmaker[Session], settings: Settings) -> list[uuid.UUID]:
    """Create runs for due targets (bounded per tick). Nothing while HARVEST_ENABLED is false."""
    if not settings.harvest_enabled:
        return []
    with session_factory() as session:
        due = session.execute(_DUE_TARGETS, {"limit": settings.harvest_dispatch_batch_size}).all()
        session.rollback()
    created = []
    for row in due:
        with session_factory() as session:
            run_id = create_run(session, settings, row.course_id, row.source_id)
            session.commit()
        if run_id is not None:
            created.append(run_id)
            log.info("harvest_run_scheduled run_id=%s course_id=%s", run_id, row.course_id)
    return created


# --- claim, progress, yield, failure, completion -----------------------------------------------------


_CLAIM = text(
    """
    UPDATE harvest_runs
    SET status = 'running', claim_count = claim_count + 1, started_at = now(),
        attempt_token = gen_random_uuid(), lease_expires_at = now() + :lease * interval '1 second',
        updated_at = now()
    WHERE id = :id AND status = 'queued' AND available_at <= now()
      AND claim_count < max_claims AND failure_count < max_failures
    RETURNING course_id, source_id, attempt_token, claim_count, max_claims, failure_count, max_failures,
              generation, trigger, progress
    """
)


def claim(session: Session, run_id: uuid.UUID, settings: Settings) -> RunClaim | None:
    """Claim a queued run for one slice; None if not claimable. The caller commits."""
    row = session.execute(_CLAIM, {"id": run_id, "lease": settings.harvest_lease_seconds}).one_or_none()
    if row is None:
        return None
    return RunClaim(
        run_id=run_id,
        course_id=row.course_id,
        source_id=row.source_id,
        token=row.attempt_token,
        claim_count=row.claim_count,
        max_claims=row.max_claims,
        failure_count=row.failure_count,
        max_failures=row.max_failures,
        generation=row.generation,
        trigger=row.trigger,
        progress=dict(row.progress or {}),
    )


def lock_owned(session: Session, run: RunClaim) -> bool:
    """Lock the run row if this attempt still owns it (FOR NO KEY UPDATE)."""
    return (
        session.execute(
            text(
                "SELECT 1 FROM harvest_runs WHERE id = :id AND status = 'running' AND attempt_token = :token "
                "FOR NO KEY UPDATE"
            ),
            {"id": run.run_id, "token": run.token},
        ).first()
        is not None
    )


def write_progress(session: Session, run: RunClaim) -> bool:
    """Persist run.progress under the token (inside the caller's transaction)."""
    return bool(
        session.execute(
            text(
                "UPDATE harvest_runs SET progress = CAST(:p AS jsonb), updated_at = now() "
                "WHERE id = :id AND status = 'running' AND attempt_token = :token"
            ),
            {"id": run.run_id, "token": run.token, "p": _json(run.progress)},
        ).rowcount
    )


def save_progress(session_factory: sessionmaker[Session], run: RunClaim) -> bool:
    with session_factory() as session:
        ok = write_progress(session, run)
        session.commit()
    return ok


def _lock_target(session: Session, run: RunClaim) -> None:
    session.execute(
        text("SELECT 1 FROM harvest_targets WHERE course_id = :c AND source_id = :s FOR NO KEY UPDATE"),
        {"c": run.course_id, "s": run.source_id},
    )


def _finish(session: Session, run_id: uuid.UUID, status: str, code: str | None) -> None:
    session.execute(
        text(
            "UPDATE harvest_runs SET status = :status, finished_at = now(), lease_expires_at = NULL, "
            "error_code = :code, updated_at = now() WHERE id = :id"
        ),
        {"id": run_id, "status": status, "code": code},
    )


def _target_backoff(session: Session, course_id: uuid.UUID, source_id: uuid.UUID, settings: Settings) -> None:
    session.execute(
        text(
            "UPDATE harvest_targets SET not_before = now() + :delay * interval '1 second', updated_at = now() "
            "WHERE course_id = :c AND source_id = :s"
        ),
        {"c": course_id, "s": source_id, "delay": settings.harvest_failed_run_backoff_seconds},
    )


def yield_run(
    session_factory: sessionmaker[Session], run: RunClaim, settings: Settings, delay_seconds: float, reason: str
) -> str:
    """End this slice without a failure: progress kept, run queued again after `delay_seconds`.

    With no claims left the run cannot continue: it fails as
    `work_limit_exhausted` (its progress and documents stay). Returns
    "yielded", "exhausted" or "stale".
    """
    with session_factory() as session:
        _lock_target(session, run)
        if not lock_owned(session, run):
            session.rollback()
            return "stale"
        write_progress(session, run)
        if run.claim_count >= run.max_claims:
            _finish(session, run.run_id, "failed", "work_limit_exhausted")
            _target_backoff(session, run.course_id, run.source_id, settings)
            session.commit()
            log.warning("harvest_run_failed run_id=%s code=work_limit_exhausted", run.run_id)
            return "exhausted"
        session.execute(
            text(
                "UPDATE harvest_runs SET status = 'queued', attempt_token = NULL, lease_expires_at = NULL, "
                "last_dispatched_at = NULL, available_at = now() + :delay * interval '1 second', "
                "error_code = :reason, updated_at = now() WHERE id = :id"
            ),
            {"id": run.run_id, "delay": max(float(delay_seconds), 0.0), "reason": reason},
        )
        session.commit()
    log.info("harvest_run_yielded run_id=%s reason=%s delay=%.0fs", run.run_id, reason, delay_seconds)
    return "yielded"


def fail_attempt(
    session_factory: sessionmaker[Session], run: RunClaim, settings: Settings, code: str, *, permanent: bool
) -> str:
    """Record a failed attempt (failure_count + 1, never refunded). Returns "requeued", "failed" or "stale"."""
    with session_factory() as session:
        _lock_target(session, run)
        if not lock_owned(session, run):
            session.rollback()
            return "stale"
        write_progress(session, run)
        failures = session.execute(
            text("UPDATE harvest_runs SET failure_count = failure_count + 1 WHERE id = :id RETURNING failure_count"),
            {"id": run.run_id},
        ).scalar_one()
        if permanent or failures >= run.max_failures or run.claim_count >= run.max_claims:
            _finish(session, run.run_id, "failed", code[:64])
            _target_backoff(session, run.course_id, run.source_id, settings)
            session.commit()
            log.warning("harvest_run_failed run_id=%s code=%s failures=%d", run.run_id, code, failures)
            return "failed"
        delay = settings.harvest_retry_backoff_seconds * (2 ** (failures - 1))
        session.execute(
            text(
                "UPDATE harvest_runs SET status = 'queued', attempt_token = NULL, lease_expires_at = NULL, "
                "last_dispatched_at = NULL, available_at = now() + :delay * interval '1 second', "
                "error_code = :code, updated_at = now() WHERE id = :id"
            ),
            {"id": run.run_id, "delay": delay, "code": code[:64]},
        )
        session.commit()
    log.warning("harvest_run_requeued run_id=%s code=%s failures=%d", run.run_id, code, failures)
    return "requeued"


def complete(session_factory: sessionmaker[Session], run: RunClaim, settings: Settings) -> str:
    """Mark the run succeeded and record the generation it covered. Returns "succeeded" or "stale"."""
    with session_factory() as session:
        _lock_target(session, run)
        if not lock_owned(session, run):
            session.rollback()
            return "stale"
        write_progress(session, run)
        _finish(session, run.run_id, "succeeded", None)
        session.execute(
            text(
                "UPDATE harvest_targets SET harvested_generation = greatest(harvested_generation, :generation), "
                "next_refresh_at = now() + refresh_interval_seconds * interval '1 second', not_before = now(), "
                "updated_at = now() WHERE course_id = :c AND source_id = :s"
            ),
            {"c": run.course_id, "s": run.source_id, "generation": run.generation},
        )
        session.commit()
    log.info("harvest_run_succeeded run_id=%s progress=%s", run.run_id, _json(run.progress))
    return "succeeded"


# --- cancellation (opt-out, source disable) ----------------------------------------------------------


def cancel_active(session: Session, *, course_id: uuid.UUID | None = None, source_id: uuid.UUID | None = None,
                  code: str) -> int:  # fmt: skip
    """Cancel queued and running runs of a course or a source (the caller holds the gate lock and commits)."""
    if (course_id is None) == (source_id is None):
        raise ValueError("cancel by course or by source")
    column = "course_id" if course_id is not None else "source_id"
    ids = session.execute(
        text(f"SELECT id FROM harvest_runs WHERE {column} = :v AND status IN ('queued', 'running') ORDER BY id FOR NO KEY UPDATE"),
        {"v": course_id or source_id},
    ).scalars().all()
    for run_id in ids:
        _finish(session, run_id, "cancelled", code)
    return len(ids)


@dataclass(frozen=True)
class SourceChange:
    was_enabled: bool
    cancelled_runs: int


def set_source_enabled(session: Session, source_id: uuid.UUID, enabled: bool) -> SourceChange | None:
    """Switch a provider-managed source on or off (the caller commits); None if there is no such source.

    Locks the source row (global order: sources -> harvest_runs). Enabling only sets the flag: course
    opt-in, approved queries, HARVEST_ENABLED and the provider budget still gate every run. Disabling
    also cancels the source's queued and running runs (their attempts are fenced out by the token).
    """
    row = session.execute(
        text("SELECT provider, enabled FROM sources WHERE id = :s FOR NO KEY UPDATE"), {"s": source_id}
    ).one_or_none()
    if row is None or row.provider is None:
        return None
    session.execute(text("UPDATE sources SET enabled = :e WHERE id = :s"), {"s": source_id, "e": enabled})
    cancelled = 0 if enabled else cancel_active(session, source_id=source_id, code="source_disabled")
    return SourceChange(was_enabled=row.enabled is True, cancelled_runs=cancelled)


# --- recovery and history -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryResult:
    requeued: tuple[uuid.UUID, ...]
    failed: tuple[uuid.UUID, ...]


def recover(session_factory: sessionmaker[Session], settings: Settings) -> RecoveryResult:
    """Expired leases count as failed attempts; runs that can never be claimed again fail.

    Runs regardless of HARVEST_ENABLED (a dead attempt is dead either way).
    Rows are taken with SKIP LOCKED; every update is conditional on the token
    read under the lock.
    """
    requeued: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    with session_factory() as session:
        expired = session.execute(
            text(
                "SELECT id, course_id, source_id, attempt_token FROM harvest_runs "
                "WHERE status = 'running' AND lease_expires_at < now() ORDER BY lease_expires_at, id LIMIT :limit"
            ),
            {"limit": settings.recovery_batch_size},
        ).all()
        session.rollback()
    for row in expired:
        with session_factory() as session:
            target = session.execute(
                text("SELECT 1 FROM harvest_targets WHERE course_id = :c AND source_id = :s FOR NO KEY UPDATE SKIP LOCKED"),
                {"c": row.course_id, "s": row.source_id},
            ).first()
            if target is None:  # held by a completing attempt (or gone): next tick decides
                session.rollback()
                continue
            locked = session.execute(
                text(
                    "SELECT claim_count, max_claims, failure_count, max_failures FROM harvest_runs "
                    "WHERE id = :id AND status = 'running' AND attempt_token = :token AND lease_expires_at < now() "
                    "FOR NO KEY UPDATE SKIP LOCKED"
                ),
                {"id": row.id, "token": row.attempt_token},
            ).one_or_none()
            if locked is None:
                session.rollback()
                continue
            failures = locked.failure_count + 1
            if failures >= locked.max_failures or locked.claim_count >= locked.max_claims:
                session.execute(
                    text("UPDATE harvest_runs SET failure_count = :f WHERE id = :id"), {"id": row.id, "f": failures}
                )
                _finish(session, row.id, "failed", "lease_expired")
                _target_backoff(session, row.course_id, row.source_id, settings)
                failed.append(row.id)
            else:
                session.execute(
                    text(
                        "UPDATE harvest_runs SET status = 'queued', failure_count = :f, attempt_token = NULL, "
                        "lease_expires_at = NULL, last_dispatched_at = NULL, error_code = 'lease_expired', "
                        "available_at = now() + :delay * interval '1 second', updated_at = now() WHERE id = :id"
                    ),
                    {"id": row.id, "f": failures, "delay": settings.harvest_retry_backoff_seconds * (2 ** (failures - 1))},
                )
                requeued.append(row.id)
            session.commit()
    with session_factory() as session:
        stuck = session.execute(
            text(
                "SELECT id, course_id, source_id FROM harvest_runs WHERE status = 'queued' "
                "AND (claim_count >= max_claims OR failure_count >= max_failures) ORDER BY id LIMIT :limit "
                "FOR NO KEY UPDATE SKIP LOCKED"
            ),
            {"limit": settings.recovery_batch_size},
        ).all()
        for row in stuck:
            _finish(session, row.id, "failed", "work_limit_exhausted")
            failed.append(row.id)
        session.commit()
    for run_id in requeued:
        log.warning("harvest_lease_recovered run_id=%s action=requeued", run_id)
    for run_id in failed:
        log.warning("harvest_lease_recovered run_id=%s action=failed", run_id)
    return RecoveryResult(tuple(requeued), tuple(failed))


def cleanup_history(session_factory: sessionmaker[Session], settings: Settings) -> int:
    """Delete finished runs older than the retention, a bounded batch per call (provenance keeps NULL)."""
    with session_factory() as session:
        deleted = session.execute(
            text(
                """
                WITH victims AS MATERIALIZED (
                    SELECT id FROM harvest_runs
                    WHERE status IN ('succeeded', 'failed', 'cancelled')
                      AND finished_at < now() - :days * interval '1 day'
                    ORDER BY finished_at, id LIMIT :limit FOR UPDATE SKIP LOCKED
                )
                DELETE FROM harvest_runs AS r USING victims WHERE r.id = victims.id
                """
            ),
            {"days": settings.harvest_history_retention_days, "limit": settings.harvest_history_cleanup_batch},
        ).rowcount
        session.commit()
    return deleted


# --- dispatch reservations ------------------------------------------------------------------------------

_ELIGIBLE = (
    "status = 'queued' AND available_at <= now() AND claim_count < max_claims AND failure_count < max_failures "
    "AND (last_dispatched_at IS NULL OR last_dispatched_at <= now() - :redispatch * interval '1 second')"
)


def reserve_batch(session: Session, limit: int, settings: Settings) -> tuple[list[uuid.UUID], object]:
    rows = session.execute(
        text(
            f"""
            WITH due AS (
                SELECT id FROM harvest_runs WHERE {_ELIGIBLE}
                ORDER BY available_at, created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED
            )
            UPDATE harvest_runs r SET last_dispatched_at = now(), updated_at = now()
            FROM due WHERE r.id = due.id RETURNING r.id, r.last_dispatched_at
            """
        ),
        {"limit": limit, "redispatch": settings.harvest_redispatch_after_seconds},
    ).all()
    if not rows:
        return [], None
    return sorted(row.id for row in rows), rows[0].last_dispatched_at


def release_reservations(
    session_factory: sessionmaker[Session], run_ids: list[uuid.UUID], reserved_at, settings: Settings
) -> int:
    if not run_ids:
        return 0
    with session_factory() as session:
        released = session.execute(
            text(
                "UPDATE harvest_runs SET last_dispatched_at = NULL, "
                "available_at = greatest(available_at, now() + :delay * interval '1 second'), updated_at = now() "
                "WHERE id = ANY(:ids) AND status = 'queued' AND last_dispatched_at = :reserved_at"
            ),
            {"ids": run_ids, "reserved_at": reserved_at, "delay": settings.publish_retry_delay_seconds},
        ).rowcount
        session.commit()
    return released
