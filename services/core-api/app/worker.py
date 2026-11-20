"""Celery entry point for the ingestion worker and Beat.

    celery -A app.worker worker -Q ingestion.parse        (parse worker)
    celery -A app.worker worker -Q ingestion.maintenance  (maintenance worker)
    celery -A app.worker worker -Q ingestion.index        (index worker, M4; no upload volume)
    celery -A app.worker worker -Q harvest.run --concurrency 1   (harvest worker, M6; provider egress only)
    celery -A app.worker beat                              (scheduler, one instance)

Tasks are thin: they validate their message and call app.ingestion or
app.indexing. Parse and index messages are exactly {"job_id": "<canonical
uuid>"}; maintenance messages carry nothing. Anything else is logged and
dropped, never interpreted. The start-up checks depend on the worker's role,
derived from the queues it consumes (app.ingestion.runtime.worker_role).
"""

from __future__ import annotations

import logging
import os

from celery import signals

from .celery_app import (
    DISPATCH_TASK,
    HARVEST_MAINTAIN_TASK,
    HARVEST_TASK,
    INDEX_TASK,
    PARSE_TASK,
    RECONCILE_TASK,
    RECOVER_TASK,
    make_celery,
)
from .config import settings
from .harvest import executor as harvest_executor
from .harvest import maintenance as harvest_maintenance
from .indexing import pipeline as index_pipeline
from .ingestion import dispatch, jobs, pipeline, reconcile, runtime

log = logging.getLogger(__name__)

celery_app = make_celery(settings)


def _consumed_queues(worker) -> set[str]:
    consume_from = getattr(getattr(getattr(worker, "app", None), "amqp", None), "queues", None)
    selected = getattr(consume_from, "consume_from", None)
    return set(selected or ())


def _role_preflight(worker) -> None:
    role = runtime.worker_role(_consumed_queues(worker), settings)
    log.info("worker role: %s", role)
    runtime.preflight_for_role(role, settings, os.environ)


@signals.worker_init.connect
def _worker_preflight(sender=None, **_):
    runtime.exit_on_failure(_role_preflight, sender)


@signals.beat_init.connect
def _beat_preflight(**_):
    runtime.exit_on_failure(runtime.beat_preflight, os.environ)


@signals.worker_process_init.connect
def _forget_inherited_state(**_):
    runtime.reset_after_fork()


def _no_arguments(name: str, args: tuple, kwargs: dict) -> bool:
    if args or kwargs:
        log.error("task_rejected task=%s reason=unexpected_arguments", name)
        return False
    return True


@celery_app.task(
    name=PARSE_TASK,
    soft_time_limit=settings.parse_task_soft_time_limit_seconds,
    time_limit=settings.parse_task_time_limit_seconds,
)
def parse_document(*args, **kwargs):
    job_id = runtime.parse_job_id(args, kwargs)
    if job_id is None:
        log.error("task_rejected task=%s reason=invalid_message", PARSE_TASK)
        return None
    pipeline.run_parse_job(job_id, runtime.context(settings, celery_app))
    return None


@celery_app.task(
    name=INDEX_TASK,
    soft_time_limit=settings.index_task_soft_time_limit_seconds,
    time_limit=settings.index_task_time_limit_seconds,
)
def index_document(*args, **kwargs):
    job_id = runtime.parse_job_id(args, kwargs)
    if job_id is None:
        log.error("task_rejected task=%s reason=invalid_message", INDEX_TASK)
        return None
    index_pipeline.run_index_job(job_id, runtime.index_context(settings, celery_app))
    return None


_MAINTENANCE_LIMITS = {
    "soft_time_limit": settings.maintenance_task_soft_time_limit_seconds,
    "time_limit": settings.maintenance_task_time_limit_seconds,
}


@celery_app.task(name=DISPATCH_TASK, **_MAINTENANCE_LIMITS)
def dispatch_parse_jobs(*args, **kwargs):
    if _no_arguments(DISPATCH_TASK, args, kwargs):
        ctx = runtime.context(settings, celery_app)
        publisher = runtime.publisher(settings, celery_app)
        result = dispatch.sweep(ctx.session_factory, publisher, settings)
        log.info("dispatch_sweep published=%d released=%d skipped=%s", result.published, result.released, result.skipped)
        if settings.indexing_enabled:
            result = dispatch.sweep_index(ctx.session_factory, publisher, settings)
            log.info(
                "index_dispatch_sweep published=%d released=%d skipped=%s",
                result.published, result.released, result.skipped,
            )  # fmt: skip


@celery_app.task(name=RECOVER_TASK, **_MAINTENANCE_LIMITS)
def recover_stale_parse_jobs(*args, **kwargs):
    if _no_arguments(RECOVER_TASK, args, kwargs):
        ctx = runtime.context(settings, celery_app)
        result = jobs.recover_expired_leases(ctx.session_factory, settings)
        log.info("lease_recovery requeued=%d failed=%d", len(result.requeued), len(result.failed))
        # Index attempts are recovered whether or not indexing is enabled now: a
        # running attempt whose lease expired is dead either way.
        result = jobs.recover_expired_leases(ctx.session_factory, settings, kind="index")
        log.info("index_lease_recovery requeued=%d failed=%d", len(result.requeued), len(result.failed))


@celery_app.task(name=RECONCILE_TASK, **_MAINTENANCE_LIMITS)
def reconcile_files(*args, **kwargs):
    if _no_arguments(RECONCILE_TASK, args, kwargs):
        ctx = runtime.context(settings, celery_app)
        report = reconcile.reconcile(ctx.session_factory, ctx.storage, settings)
        removed = len(report.temp.removed) if report.temp else 0
        log.info(
            "reconcile skipped=%s temp_removed=%d orphans=%d missing=%d errors=%d",
            report.skipped, removed, len(report.orphans), len(report.missing), len(report.errors),
        )


@celery_app.task(
    name=HARVEST_TASK,
    soft_time_limit=settings.harvest_task_soft_time_limit_seconds,
    time_limit=settings.harvest_task_time_limit_seconds,
)
def harvest_run_slice(*args, **kwargs):
    run_id = runtime.run_id_of(args, kwargs)
    if run_id is None:
        log.error("task_rejected task=%s reason=invalid_message", HARVEST_TASK)
        return None
    harvest_executor.run_harvest(run_id, runtime.harvest_context(settings, celery_app))
    return None


@celery_app.task(name=HARVEST_MAINTAIN_TASK, **_MAINTENANCE_LIMITS)
def harvest_maintain(*args, **kwargs):
    if _no_arguments(HARVEST_MAINTAIN_TASK, args, kwargs):
        ctx = runtime.context(settings, celery_app)
        result = harvest_maintenance.maintain(ctx.session_factory, runtime.publisher(settings, celery_app), settings)
        log.info(
            "harvest_maintenance scheduled=%d published=%d released=%d recovered=%d failed=%d cleaned=%d skipped=%s",
            result.scheduled, result.published, result.released, result.recovered, result.failed, result.cleaned,
            result.skipped,
        )  # fmt: skip
