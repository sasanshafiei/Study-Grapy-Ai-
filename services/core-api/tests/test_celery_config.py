"""Celery configuration, settings invariants and task message validation (no database, no broker)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.celery_app import (
    ANALYTICS_MAINTAIN_TASK,
    DISPATCH_TASK,
    HARVEST_MAINTAIN_TASK,
    HARVEST_TASK,
    MASTERY_MAINTAIN_TASK,
    MASTERY_TASK,
    MAINTENANCE_TASKS,
    PARSE_TASK,
    RECONCILE_TASK,
    RECOVER_TASK,
    make_celery,
)
from app.config import Settings
from app.ingestion import runtime
from app.ingestion.jobs import backoff_seconds
from app.main import create_app
from tests.alembic_cli import head_revision


@pytest.fixture
def settings() -> Settings:
    return Settings(parse_queue="q.parse", maintenance_queue="q.maint")


def test_broker_settings_and_no_result_backend(settings):
    conf = make_celery(settings).conf
    assert conf.result_backend is None and conf.task_ignore_result is True
    assert conf.task_serializer == "json" and list(conf.accept_content) == ["json"]
    assert conf.task_acks_late is True and conf.task_reject_on_worker_lost is False
    assert conf.worker_prefetch_multiplier == 1
    assert conf.worker_enable_remote_control is False
    assert conf.worker_send_task_events is False and conf.task_send_sent_event is False
    assert conf.worker_cancel_long_running_tasks_on_connection_loss is False
    assert conf.broker_connection_retry_on_startup is True
    options = conf.broker_transport_options
    assert options["visibility_timeout"] == settings.broker_visibility_timeout_seconds
    assert options["socket_timeout"] == settings.broker_socket_timeout_seconds
    assert options["socket_connect_timeout"] == settings.broker_connect_timeout_seconds


def test_routes_and_beat_schedule(settings):
    conf = make_celery(settings).conf
    assert conf.task_routes[PARSE_TASK] == {"queue": "q.parse"}
    for name in MAINTENANCE_TASKS:
        assert conf.task_routes[name] == {"queue": "q.maint"}
    schedule = {entry["task"]: entry for entry in conf.beat_schedule.values()}
    assert set(schedule) == {
        DISPATCH_TASK, RECOVER_TASK, RECONCILE_TASK, HARVEST_MAINTAIN_TASK, MASTERY_MAINTAIN_TASK,
        ANALYTICS_MAINTAIN_TASK,
    }  # fmt: skip
    assert conf.task_routes[HARVEST_TASK] == {"queue": settings.harvest_queue}
    assert HARVEST_TASK not in schedule
    assert conf.task_routes[MASTERY_TASK] == {"queue": settings.mastery_queue}
    assert MASTERY_TASK not in schedule
    for entry in schedule.values():
        assert entry["options"]["queue"] == "q.maint"
        assert entry["options"]["expires"] == entry["schedule"]  # no replay of a missed backlog
    assert PARSE_TASK not in schedule


def test_worker_tasks_are_registered_with_their_limits():
    from app.worker import celery_app, parse_document

    assert {PARSE_TASK, *MAINTENANCE_TASKS} <= set(celery_app.tasks)
    assert parse_document.soft_time_limit < parse_document.time_limit


@pytest.mark.parametrize(
    "overrides",
    [
        {"parse_timeout_seconds": 150},  # not below the soft limit
        {"parse_task_soft_time_limit_seconds": 180},  # not below the hard limit
        {"broker_visibility_timeout_seconds": 300},  # not above twice the hard limit
        {"maintenance_task_soft_time_limit_seconds": 120},
        {"retry_backoff_base_seconds": 700},
        {"parse_queue": "same", "maintenance_queue": "same"},
        {"parse_queue": "bad queue name"},
    ],
)
def test_inconsistent_limits_are_refused(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_lease_outlasts_the_hard_time_limit():
    s = Settings()
    assert s.parse_timeout_seconds < s.parse_task_soft_time_limit_seconds < s.parse_task_time_limit_seconds
    assert s.job_lease_seconds > s.parse_task_time_limit_seconds
    assert s.broker_visibility_timeout_seconds > 2 * s.parse_task_time_limit_seconds


def test_backoff_is_exponential_jittered_and_capped():
    s = Settings(retry_backoff_base_seconds=30, retry_backoff_max_seconds=600)

    class Fixed:
        def __init__(self, value):
            self.value = value

        def uniform(self, low, high):
            return self.value

    assert [backoff_seconds(n, s, Fixed(1.0)) for n in (1, 2, 3, 4, 10)] == [30, 60, 120, 240, 600]
    assert backoff_seconds(1, s, Fixed(0.8)) == 24 and backoff_seconds(1, s, Fixed(1.2)) == 36
    assert backoff_seconds(1, Settings(retry_backoff_base_seconds=0, retry_backoff_max_seconds=0)) == 0


@pytest.mark.parametrize(
    "args, kwargs, valid",
    [
        ((), {"job_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}, True),
        ((), {"job_id": "7C9E6679-7425-40DE-944B-E07FC1F90AE7"}, False),
        ((), {"job_id": "not-a-uuid"}, False),
        ((), {"job_id": 5}, False),
        ((), {"job_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "path": "/etc/passwd"}, False),
        (("7c9e6679-7425-40de-944b-e07fc1f90ae7",), {}, False),
        ((), {}, False),
    ],
)
def test_parse_messages_are_validated(args, kwargs, valid):
    result = runtime.parse_job_id(args, kwargs)
    assert (result == uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")) if valid else result is None


def test_required_schema_revision_is_the_migration_head():
    assert runtime.REQUIRED_SCHEMA_REVISION == head_revision()


def test_worker_environment_must_be_explicit():
    with pytest.raises(runtime.PreflightError) as caught:
        runtime.require_environment({"DATABASE_URL": "x", "REDIS_URL": " "}, runtime.WORKER_ENVIRONMENT)
    assert "REDIS_URL" in str(caught.value) and "UPLOAD_DIR" in str(caught.value)
    runtime.require_environment({"REDIS_URL": "redis://r:6379/0"}, runtime.BEAT_ENVIRONMENT)


def test_building_the_app_connects_to_no_broker():
    app = create_app(Settings(redis_url="redis://unreachable.invalid:6379/0", dispatch_on_upload=True))
    assert app.state.publisher is not None  # created, not connected
    assert create_app(Settings(dispatch_on_upload=False)).state.publisher is None


def test_api_process_does_not_load_the_pdf_parser():
    import subprocess
    import sys

    code = "import sys, app.main; print('pypdf' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# --- M3 review M3: no credentials in settings representations -----------------------------------


def test_settings_never_show_credentials(monkeypatch):
    db, broker = "fake-db-secret-41", "fake-redis-secret-42"
    explicit = Settings(
        database_url=f"postgresql+psycopg://u:{db}@h:5432/d", redis_url=f"redis://:{broker}@r:6379/0"
    )
    monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg://u:{db}@h:5432/d")
    monkeypatch.setenv("REDIS_URL", f"redis://:{broker}@r:6379/0")
    from_env = Settings()
    for settings in (explicit, from_env):
        for shown in (repr(settings), str(settings), settings.model_dump_json(), repr(settings.model_dump())):
            assert db not in shown and broker not in shown
        assert settings.database_url.get_secret_value() == f"postgresql+psycopg://u:{db}@h:5432/d"
        assert make_celery(settings).conf.broker_url == f"redis://:{broker}@r:6379/0"  # still usable


@pytest.mark.parametrize("bad", [{"parse_timeout_seconds": 500}, {"max_upload_bytes": -1}, {"parse_queue": "x y"}])
def test_validation_errors_never_echo_credentials_or_inputs(bad):
    with pytest.raises(ValidationError) as caught:
        Settings(database_url="postgresql+psycopg://u:fake-db-secret-43@h/d", redis_url="redis://:fake-redis-secret-44@r/0", **bad)
    message = str(caught.value)
    assert "fake-db-secret-43" not in message and "fake-redis-secret-44" not in message
    assert "input_value" not in message


# --- M3 review M1: no broker I/O from the API by default; honest publisher bounds --------------


def test_the_api_does_not_publish_by_default():
    assert Settings().dispatch_on_upload is False
    assert create_app(Settings()).state.publisher is None
    assert Settings().dispatch_interval_seconds == 10  # the sweep is the pick-up path


def test_publisher_starts_no_new_attempt_once_its_budget_is_spent(monkeypatch):
    from app.ingestion import dispatch
    from app.ingestion.dispatch import CeleryPublisher, PublishError

    settings = Settings(broker_publish_attempts=3, broker_connect_timeout_seconds=1, broker_socket_timeout_seconds=1)
    publisher = CeleryPublisher(make_celery(settings), settings)
    clock = [0.0]
    monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock[0])

    class Connection:
        released = 0

        def release(self):
            Connection.released += 1

    monkeypatch.setattr(publisher, "_connection", Connection)
    attempts = []

    def slow_then_fails(connection):
        attempts.append(1)
        clock[0] += 100  # a broker that keeps an attempt busy far past the budget
        raise OSError("simulated slow broker")

    with pytest.raises(PublishError):
        publisher._with_connection(slow_then_fails)
    # The running attempt is not interrupted (only socket inactivity timeouts can end it),
    # but no further attempt starts, and its connection is closed.
    assert attempts == [1] and Connection.released == 1
