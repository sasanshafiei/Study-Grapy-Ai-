"""Publishing parse and index jobs: bounded broker I/O and the dispatch sweeps over the job table.

Index jobs (M4) use the same reservation, release and broker rules on their
own queue, through sweep_index, which publishes nothing while
INDEXING_ENABLED is false (the jobs stay queued and keep their attempts).

ingestion_jobs is the durable outbox. A message is only a hint that a job may
be claimable; losing one costs latency, never a job, and a duplicate costs
one no-op claim.

Reservation. Before publishing, a job is reserved with a compare-and-set
UPDATE that sets last_dispatched_at = now() only if the job is still queued,
eligible (available_at reached, attempts left) and either never published
(last_dispatched_at IS NULL) or published long enough ago
(REDISPATCH_AFTER_SECONDS) that the message must be presumed lost. The upload
path and the sweep use the same reservation, so concurrent publishers do not
both publish a job within one interval. A failed publication releases the
reservation only if it is still the one this publisher made
(last_dispatched_at = the reserved value) and moves available_at forward by
PUBLISH_RETRY_DELAY_SECONDS: the job is retried soon, never in a tight loop.
An uncertain publication (an error after the broker may have accepted the
message) is treated the same way: a message that did arrive meets a job that
is not claimable yet and is dropped, and the job is published again later.

Broker I/O bounds, precisely (M3 review). Every publish uses a fresh broker
connection (no pool to wait for) that is closed on every path, makes at most
BROKER_PUBLISH_ATTEMPTS attempts, and starts no new attempt once its attempt
budget is spent. Within an attempt, the connect timeout bounds connecting and
the socket timeout bounds each read or write *between* bytes: a broker that
answers slowly but keeps sending can make one attempt last longer. There is
therefore no wall-clock guarantee inside this module. The absolute bound comes
from where it runs: the dispatch sweep runs on the maintenance worker under a
Celery hard time limit (the pool process is killed), and publishing from the
API process (DISPATCH_ON_UPLOAD) is off by default for exactly this reason.

Queue depth. Before reserving, the sweep asks the broker how many parse
messages are waiting and publishes nothing (or fewer) when the queue already
holds DISPATCH_MAX_QUEUE_DEPTH. This is advisory: uploads publish without
checking, and two sweeps can read the same depth, so it limits growth rather
than guaranteeing a bound. If the broker cannot be reached for that check,
the sweep reserves nothing (one connection attempt per sweep while the broker
is down).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..celery_app import HARVEST_TASK, INDEX_TASK, PARSE_TASK, broker_transport_options
from ..config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T")


class PublishError(Exception):
    """The broker did not confirm the operation (it may or may not have happened)."""


class Publisher(Protocol):
    def publish_parse(self, job_id: uuid.UUID) -> None: ...

    def parse_queue_depth(self) -> int: ...


class IndexPublisher(Protocol):
    def publish_index(self, job_id: uuid.UUID) -> None: ...

    def index_queue_depth(self) -> int: ...


class CeleryPublisher:
    """Publishes Celery task messages with bounded, pool-free broker I/O."""

    def __init__(self, celery_app: Celery, settings: Settings) -> None:
        self._app = celery_app
        self._settings = settings

    def _connection(self):
        s = self._settings
        return self._app.connection_for_write(
            connect_timeout=s.broker_connect_timeout_seconds,
            transport_options={
                **broker_transport_options(s),
                # One connection attempt per call; retries are counted here.
                "max_retries": 0,
                "interval_start": 0,
                "interval_step": 0,
                "interval_max": 0,
            },
        )

    def _with_connection(self, action: Callable[[object], T]) -> T:
        """Run `action` on a fresh connection, retrying a bounded number of times.

        The budget below only decides whether another attempt may *start*; it
        does not interrupt an attempt in progress (see the module docstring).
        """
        s = self._settings
        per_attempt = s.broker_connect_timeout_seconds + 2 * s.broker_socket_timeout_seconds
        attempt_budget_ends = time.monotonic() + s.broker_publish_attempts * per_attempt
        last: BaseException | None = None
        for _ in range(s.broker_publish_attempts):
            if time.monotonic() >= attempt_budget_ends:
                break
            connection = self._connection()
            try:
                return action(connection)
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - kombu, redis-py and socket errors alike
                last = exc
            finally:
                try:
                    connection.release()
                except Exception:  # noqa: BLE001
                    log.debug("closing a broker connection failed", exc_info=True)
        raise PublishError(f"broker operation failed: {type(last).__name__ if last else 'deadline'}") from last

    def _publish(self, task: str, queue: str, job_id: uuid.UUID, key: str = "job_id") -> None:
        def send(connection) -> None:
            channel = connection.default_channel  # connects: one attempt, bounded by the timeouts
            producer = connection.Producer(channel=channel)
            self._app.send_task(task, kwargs={key: str(job_id)}, queue=queue, producer=producer, retry=False)

        self._with_connection(send)

    def _depth(self, queue: str) -> int:
        def size(connection) -> int:
            return int(connection.default_channel.queue_declare(queue=queue, passive=False).message_count)

        return self._with_connection(size)

    def publish_parse(self, job_id: uuid.UUID) -> None:
        self._publish(PARSE_TASK, self._settings.parse_queue, job_id)

    def parse_queue_depth(self) -> int:
        return self._depth(self._settings.parse_queue)

    def publish_index(self, job_id: uuid.UUID) -> None:
        self._publish(INDEX_TASK, self._settings.index_queue, job_id)

    def index_queue_depth(self) -> int:
        return self._depth(self._settings.index_queue)

    def publish_harvest(self, run_id: uuid.UUID) -> None:
        self._publish(HARVEST_TASK, self._settings.harvest_queue, run_id, key="run_id")

    def harvest_queue_depth(self) -> int:
        return self._depth(self._settings.harvest_queue)


# --- reservations -----------------------------------------------------------------------------

_ELIGIBLE = (
    "kind = :kind AND status = 'queued' AND attempt_count < max_attempts AND available_at <= now() "
    "AND (last_dispatched_at IS NULL OR last_dispatched_at <= now() - :redispatch * interval '1 second')"
)


def reserve_job(session: Session, job_id: uuid.UUID, settings: Settings, kind: str = "parse") -> datetime | None:
    """Reserve one job for publication; the reservation timestamp, or None if not eligible."""
    return session.execute(
        text(
            f"UPDATE ingestion_jobs SET last_dispatched_at = now(), updated_at = now() "
            f"WHERE id = :job_id AND {_ELIGIBLE} RETURNING last_dispatched_at"
        ),
        {"job_id": job_id, "redispatch": settings.redispatch_after_seconds, "kind": kind},
    ).scalar_one_or_none()


def reserve_batch(
    session: Session, limit: int, settings: Settings, kind: str = "parse"
) -> tuple[list[uuid.UUID], datetime | None]:
    """Reserve up to `limit` eligible jobs (oldest first), skipping rows other sessions hold."""
    rows = session.execute(
        text(
            f"""
            WITH due AS (
                SELECT id FROM ingestion_jobs WHERE {_ELIGIBLE}
                ORDER BY available_at, created_at, id
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE ingestion_jobs j SET last_dispatched_at = now(), updated_at = now()
            FROM due WHERE j.id = due.id
            RETURNING j.id, j.last_dispatched_at
            """
        ),
        {"limit": limit, "redispatch": settings.redispatch_after_seconds, "kind": kind},
    ).all()
    if not rows:
        return [], None
    # One statement, one transaction timestamp: every reservation carries the same value.
    return sorted(row.id for row in rows), rows[0].last_dispatched_at


def release_reservations(
    session_factory: sessionmaker[Session], job_ids: list[uuid.UUID], reserved_at: datetime, settings: Settings
) -> int:
    """Undo this publisher's reservations after a failed publication (compare-and-set).

    A reservation made since (by another publisher) is left alone; so is a
    job that was claimed meanwhile. Returns the number released.
    """
    if not job_ids:
        return 0
    try:
        with session_factory() as session:
            released = session.execute(
                text(
                    "UPDATE ingestion_jobs SET last_dispatched_at = NULL, "
                    "available_at = greatest(available_at, now() + :delay * interval '1 second'), "
                    "updated_at = now() "
                    "WHERE id = ANY(:ids) AND status = 'queued' AND last_dispatched_at = :reserved_at"
                ),
                {"ids": job_ids, "reserved_at": reserved_at, "delay": settings.publish_retry_delay_seconds},
            ).rowcount
            session.commit()
            return released
    except SQLAlchemyError:
        # The reservations then simply age out (REDISPATCH_AFTER_SECONDS).
        log.warning("could not release %d dispatch reservations", len(job_ids), exc_info=True)
        return 0


# --- the two publication paths ------------------------------------------------------------------


def publish_new_job(
    session_factory: sessionmaker[Session], publisher: Publisher, job_id: uuid.UUID, settings: Settings
) -> bool:
    """Best-effort publication right after an upload committed (runs after the response).

    Never raises. True if this call published the job.
    """
    try:
        try:
            with session_factory() as session:
                reserved_at = reserve_job(session, job_id, settings)
                session.commit()
        except SQLAlchemyError:
            log.warning("dispatch_deferred job_id=%s reason=database", job_id, exc_info=True)
            return False
        if reserved_at is None:
            log.info("dispatch_skipped job_id=%s reason=not_eligible_or_reserved", job_id)
            return False
        try:
            publisher.publish_parse(job_id)
        except PublishError as exc:
            log.warning("dispatch_deferred job_id=%s reason=broker error=%s", job_id, exc)
            release_reservations(session_factory, [job_id], reserved_at, settings)
            return False
        log.info("dispatch_published job_id=%s source=upload", job_id)
        return True
    except Exception:  # noqa: BLE001 - a background task must not fail the request cycle
        log.exception("dispatch_failed job_id=%s", job_id)
        return False


@dataclass(frozen=True)
class SweepResult:
    published: int
    released: int
    skipped: str | None = None  # "broker_unavailable" | "queue_full" | None


def sweep(session_factory: sessionmaker[Session], publisher: Publisher, settings: Settings) -> SweepResult:
    """Publish eligible queued parse jobs (bounded batch). Database errors propagate."""
    try:
        depth = publisher.parse_queue_depth()
    except PublishError as exc:
        log.warning("dispatch_sweep_skipped reason=broker_unavailable error=%s", exc)
        return SweepResult(0, 0, "broker_unavailable")
    room = settings.dispatch_max_queue_depth - depth
    if room <= 0:
        log.info("dispatch_sweep_skipped reason=queue_full depth=%d", depth)
        return SweepResult(0, 0, "queue_full")

    with session_factory() as session:
        job_ids, reserved_at = reserve_batch(session, min(settings.dispatch_batch_size, room), settings)
        session.commit()
    published = 0
    for index, job_id in enumerate(job_ids):
        try:
            publisher.publish_parse(job_id)
        except PublishError as exc:
            # Stop at the first failure: the broker is not taking messages.
            released = release_reservations(session_factory, job_ids[index:], reserved_at, settings)
            log.warning(
                "dispatch_sweep_interrupted published=%d released=%d error=%s", published, released, exc
            )
            return SweepResult(published, released)
        published += 1
        log.info("dispatch_published job_id=%s source=sweep", job_id)
    return SweepResult(published, 0)


def sweep_index(session_factory: sessionmaker[Session], publisher: IndexPublisher, settings: Settings) -> SweepResult:
    """Publish eligible queued index jobs (bounded batch), only while indexing is enabled.

    The AI service is not contacted here: readiness is decided by the index
    worker before it claims, so a message for a job that cannot be indexed
    yet costs one cached readiness check and no attempt.
    """
    if not settings.indexing_enabled:
        return SweepResult(0, 0, "indexing_disabled")
    try:
        depth = publisher.index_queue_depth()
    except PublishError as exc:
        log.warning("index_dispatch_sweep_skipped reason=broker_unavailable error=%s", exc)
        return SweepResult(0, 0, "broker_unavailable")
    room = settings.dispatch_max_queue_depth - depth
    if room <= 0:
        log.info("index_dispatch_sweep_skipped reason=queue_full depth=%d", depth)
        return SweepResult(0, 0, "queue_full")
    with session_factory() as session:
        job_ids, reserved_at = reserve_batch(session, min(settings.dispatch_batch_size, room), settings, "index")
        session.commit()
    published = 0
    for index, job_id in enumerate(job_ids):
        try:
            publisher.publish_index(job_id)
        except PublishError as exc:
            released = release_reservations(session_factory, job_ids[index:], reserved_at, settings)
            log.warning("index_dispatch_sweep_interrupted published=%d released=%d error=%s", published, released, exc)
            return SweepResult(published, released)
        published += 1
        log.info("dispatch_published kind=index job_id=%s source=sweep", job_id)
    return SweepResult(published, 0)
