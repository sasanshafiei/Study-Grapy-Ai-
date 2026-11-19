"""Celery entry point for the ingestion worker and Beat.

    celery -A app.worker worker -Q ingestion.parse        (parse worker)
    celery -A app.worker worker -Q ingestion.maintenance  (maintenance worker)
    celery -A app.worker beat                              (scheduler, one instance)

Tasks are thin: they validate their message and call app.ingestion. A parse
message is exactly {"job_id": "<canonical uuid>"}; maintenance messages carry
nothing. Anything else is logged and dropped, never interpreted.
"""

from __future__ import annotations

import logging
import os

from celery import signals

from .celery_app import DISPATCH_TASK, PARSE_TASK, RECONCILE_TASK, RECOVER_TASK, make_celery
from .config import settings
from .ingestion import dispatch, jobs, pipeline, reconcile, runtime

log = logging.getLogger(__name__)

celery_app = make_celery(settings)


@signals.worker_init.connect
def _worker_preflight(**_):
    runtime.exit_on_failure(runtime.worker_preflight, settings, os.environ)


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


_MAINTENANCE_LIMITS = {
    "soft_time_limit": settings.maintenance_task_soft_time_limit_seconds,
    "time_limit": settings.maintenance_task_time_limit_seconds,
}


@celery_app.task(name=DISPATCH_TASK, **_MAINTENANCE_LIMITS)
def dispatch_parse_jobs(*args, **kwargs):
    if _no_arguments(DISPATCH_TASK, args, kwargs):
        ctx = runtime.context(settings, celery_app)
        result = dispatch.sweep(ctx.session_factory, runtime.publisher(settings, celery_app), settings)
        log.info("dispatch_sweep published=%d released=%d skipped=%s", result.published, result.released, result.skipped)


@celery_app.task(name=RECOVER_TASK, **_MAINTENANCE_LIMITS)
def recover_stale_parse_jobs(*args, **kwargs):
    if _no_arguments(RECOVER_TASK, args, kwargs):
        ctx = runtime.context(settings, celery_app)
        result = jobs.recover_expired_leases(ctx.session_factory, settings)
        log.info("lease_recovery requeued=%d failed=%d", len(result.requeued), len(result.failed))


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
