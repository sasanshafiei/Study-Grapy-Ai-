"""Celery configuration shared by the workers, Beat and the publishers (API and dispatch sweep).

Building the app connects to nothing. Choices (docs/document-processing.md):

- PostgreSQL is the source of truth for job state; messages carry a job id
  only. No result backend: task results are ignored, nothing is written back
  to Redis.
- JSON only (no pickle): a broker message can never deserialize code.
- task_acks_late, prefetch 1: a message is acknowledged after the task has
  run, so one taken by a worker that dies is delivered again (after the
  visibility timeout, or at once on a warm shutdown). Duplicate deliveries are
  harmless: claiming a job is a conditional UPDATE.
- task_reject_on_worker_lost off: a pool process that dies on a document is
  not handed the same message again (no redelivery loop on a poison PDF); the
  job's lease expires and recovery decides, counting the attempt.
- Remote control, events, gossip and mingle off: nothing but tasks is read
  from the broker.
"""

from __future__ import annotations

from celery import Celery

from .config import Settings

PARSE_TASK = "ingestion.parse_document"
# M4: published to the index queue only while INDEXING_ENABLED; consumed by the index worker.
INDEX_TASK = "ingestion.index_document"
DISPATCH_TASK = "ingestion.dispatch_parse_jobs"
RECOVER_TASK = "ingestion.recover_stale_parse_jobs"
RECONCILE_TASK = "ingestion.reconcile_files"
# M6: one bounded slice of a harvest run (harvest queue, dedicated harvest worker),
# and the harvest maintenance tick (schedule, dispatch, recover, clean up).
HARVEST_TASK = "harvest.run_slice"
HARVEST_MAINTAIN_TASK = "harvest.maintain"
MAINTENANCE_TASKS = (DISPATCH_TASK, RECOVER_TASK, RECONCILE_TASK, HARVEST_MAINTAIN_TASK)


def broker_transport_options(settings: Settings) -> dict:
    return {
        "visibility_timeout": settings.broker_visibility_timeout_seconds,
        "socket_connect_timeout": settings.broker_connect_timeout_seconds,
        "socket_timeout": settings.broker_socket_timeout_seconds,
        "socket_keepalive": True,
    }


def beat_schedule(settings: Settings) -> dict:
    """Periodic maintenance. Each message expires after one interval, so a backlog
    built while no maintenance worker runs is dropped instead of replayed."""

    def entry(task: str, every: float) -> dict:
        return {"task": task, "schedule": every, "options": {"queue": settings.maintenance_queue, "expires": every}}

    return {
        "dispatch-parse-jobs": entry(DISPATCH_TASK, settings.dispatch_interval_seconds),
        "recover-stale-parse-jobs": entry(RECOVER_TASK, settings.recovery_interval_seconds),
        "reconcile-files": entry(RECONCILE_TASK, settings.reconcile_interval_seconds),
        "harvest-maintenance": entry(HARVEST_MAINTAIN_TASK, settings.harvest_maintenance_interval_seconds),
    }


def make_celery(settings: Settings) -> Celery:
    app = Celery("studygraph", set_as_current=False)
    app.conf.update(
        broker_url=settings.redis_url.get_secret_value(),
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=settings.broker_connect_timeout_seconds,
        broker_transport_options=broker_transport_options(settings),
        result_backend=None,
        task_ignore_result=True,
        task_store_errors_even_if_ignored=False,
        task_serializer="json",
        accept_content=["json"],
        result_accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=False,
        task_acks_on_failure_or_timeout=True,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=100,
        worker_max_memory_per_child=400_000,  # KiB, checked after each task
        worker_cancel_long_running_tasks_on_connection_loss=False,
        worker_enable_remote_control=False,
        worker_send_task_events=False,
        task_send_sent_event=False,
        task_default_queue=settings.maintenance_queue,
        task_create_missing_queues=True,
        task_routes={
            PARSE_TASK: {"queue": settings.parse_queue},
            INDEX_TASK: {"queue": settings.index_queue},
            HARVEST_TASK: {"queue": settings.harvest_queue},
            **{name: {"queue": settings.maintenance_queue} for name in MAINTENANCE_TASKS},
        },
        timezone="UTC",
        enable_utc=True,
        beat_schedule=beat_schedule(settings),
    )
    return app
