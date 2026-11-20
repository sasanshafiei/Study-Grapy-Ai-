"""Harvest maintenance (one Beat-driven task on the maintenance worker): schedule, dispatch, recover, clean up.

- schedule and dispatch only while HARVEST_ENABLED (nothing is created or
  published otherwise); recovery and history cleanup always run.
- dispatch publishes {"run_id": "<uuid>"} to the harvest queue with the same
  reservation / release rules as ingestion jobs (app/ingestion/dispatch.py):
  harvest_runs is the outbox, a message is only a hint, a lost message is
  re-published after HARVEST_REDISPATCH_AFTER_SECONDS, a failed publication
  releases its own reservations only. The queue depth check is advisory.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..ingestion.dispatch import PublishError
from . import runs

log = logging.getLogger(__name__)


class HarvestPublisher(Protocol):
    def publish_harvest(self, run_id: uuid.UUID) -> None: ...
    def harvest_queue_depth(self) -> int: ...


@dataclass(frozen=True)
class MaintenanceResult:
    scheduled: int
    published: int
    released: int
    recovered: int
    failed: int
    cleaned: int
    skipped: str | None = None


def dispatch(session_factory: sessionmaker[Session], publisher: HarvestPublisher, settings: Settings) -> tuple[int, int, str | None]:
    if not settings.harvest_enabled:
        return 0, 0, "harvest_disabled"
    try:
        depth = publisher.harvest_queue_depth()
    except PublishError as exc:
        log.warning("harvest_dispatch_skipped reason=broker_unavailable error=%s", exc)
        return 0, 0, "broker_unavailable"
    room = settings.dispatch_max_queue_depth - depth
    if room <= 0:
        return 0, 0, "queue_full"
    with session_factory() as session:
        run_ids, reserved_at = runs.reserve_batch(session, min(settings.harvest_dispatch_batch_size, room), settings)
        session.commit()
    published = 0
    for index, run_id in enumerate(run_ids):
        try:
            publisher.publish_harvest(run_id)
        except PublishError as exc:
            released = runs.release_reservations(session_factory, run_ids[index:], reserved_at, settings)
            log.warning("harvest_dispatch_interrupted published=%d released=%d error=%s", published, released, exc)
            return published, released, None
        published += 1
        log.info("dispatch_published kind=harvest run_id=%s", run_id)
    return published, 0, None


def maintain(session_factory: sessionmaker[Session], publisher: HarvestPublisher, settings: Settings) -> MaintenanceResult:
    recovered = runs.recover(session_factory, settings)
    cleaned = runs.cleanup_history(session_factory, settings)
    scheduled = runs.schedule(session_factory, settings)
    published, released, skipped = dispatch(session_factory, publisher, settings)
    return MaintenanceResult(
        scheduled=len(scheduled),
        published=published,
        released=released,
        recovered=len(recovered.requeued),
        failed=len(recovered.failed),
        cleaned=cleaned,
        skipped=skipped,
    )
