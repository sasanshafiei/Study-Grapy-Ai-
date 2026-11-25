"""Index worker role, start-up checks, task registration and message validation. No infrastructure."""

from __future__ import annotations

import logging

import pytest

from app.celery_app import INDEX_TASK, PARSE_TASK, make_celery
from app.config import Settings
from app.ingestion import runtime
from app.ingestion.runtime import PreflightError

SETTINGS = Settings(parse_queue="q.parse", maintenance_queue="q.maint", index_queue="q.index")
TOKEN = "t" * 40


@pytest.mark.parametrize(
    "queues, role",
    [
        ({"q.parse"}, "processing"),
        ({"q.maint"}, "processing"),
        ({"q.parse", "q.maint"}, "processing"),
        (set(), "processing"),  # no -Q: the M3 default, unchanged
        ({"q.index"}, "index"),
    ],
)
def test_worker_role_follows_the_consumed_queues(queues, role):
    assert runtime.worker_role(queues, SETTINGS) == role


@pytest.mark.parametrize("queues", [{"q.index", "q.parse"}, {"q.index", "q.maint"}, {"q.index", "other"}])
def test_the_index_queue_needs_a_dedicated_worker(queues):
    with pytest.raises(PreflightError, match="dedicated worker"):
        runtime.worker_role(queues, SETTINGS)


def _env(**overrides):
    env = {
        "DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/studygraph",
        "REDIS_URL": "redis://redis:6379/0",
        "AI_SERVICE_URL": "http://ai-service:8000",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def test_index_preflight_does_not_require_the_upload_dir(monkeypatch):
    checked = []
    monkeypatch.setattr(runtime, "check_database", lambda target, **_: checked.append(target))
    runtime.index_worker_preflight(Settings(indexing_enabled=False), _env())  # no UPLOAD_DIR
    assert len(checked) == 1


def test_processing_preflight_still_requires_the_upload_dir():
    with pytest.raises(PreflightError, match="UPLOAD_DIR"):
        runtime.preflight_for_role("processing", SETTINGS, _env())


@pytest.mark.parametrize(
    "env, enabled, message",
    [
        (_env(AI_SERVICE_URL=None), False, "AI_SERVICE_URL"),
        (_env(AI_SERVICE_URL="  "), False, "AI_SERVICE_URL"),
        (_env(AI_SERVICE_URL="ftp://ai"), False, "http or https"),
        (_env(AI_SERVICE_URL="http://u:p@ai:8000"), False, "credentials"),
        (_env(), True, "AI_SERVICE_TOKEN must be set"),
        (_env(AI_SERVICE_TOKEN="short"), True, "at least"),
        (_env(AI_SERVICE_TOKEN=" " + TOKEN), True, "without spaces"),
        (_env(DATABASE_URL=None), False, "DATABASE_URL"),
    ],
)
def test_index_preflight_refusals(env, enabled, message, monkeypatch):
    monkeypatch.setattr(runtime, "check_database", lambda target, **_: None)
    with pytest.raises(PreflightError, match=message):
        runtime.index_worker_preflight(Settings(indexing_enabled=enabled, ai_service_url=env.get("AI_SERVICE_URL", "x")), env)


def test_index_preflight_makes_no_network_call_to_the_ai_service(monkeypatch, caplog):
    """Start-up is outside every Celery time limit: no probe, even with indexing enabled."""
    monkeypatch.setattr(runtime, "check_database", lambda target, **_: None)
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("no connection at start-up"))
    caplog.set_level(logging.INFO)
    settings = Settings(indexing_enabled=True, ai_service_url="http://127.0.0.1:9", ai_service_token=TOKEN)
    runtime.index_worker_preflight(settings, _env(AI_SERVICE_URL="http://127.0.0.1:9", AI_SERVICE_TOKEN=TOKEN))
    assert "checked per claim" in caplog.text
    assert TOKEN not in caplog.text


def test_index_task_is_routed_to_the_index_queue_and_registered_with_limits():
    conf = make_celery(SETTINGS).conf
    assert conf.task_routes[INDEX_TASK] == {"queue": "q.index"}
    assert conf.task_routes[PARSE_TASK] == {"queue": "q.parse"}
    from app.worker import celery_app, index_document

    assert INDEX_TASK in celery_app.tasks
    assert index_document.soft_time_limit < index_document.time_limit


@pytest.mark.parametrize(
    "args, kwargs",
    [((), {}), (("x",), {}), ((), {"job_id": "not-a-uuid"}), ((), {"job_id": "1", "extra": 1}), ((), {"job_id": 5})],
)
def test_invalid_index_messages_are_dropped_without_touching_anything(args, kwargs, monkeypatch, caplog):
    from app import worker

    monkeypatch.setattr(runtime, "index_context", lambda *a, **k: pytest.fail("no context for an invalid message"))
    caplog.set_level(logging.ERROR)
    assert worker.index_document.run(*args, **kwargs) is None
    assert "reason=invalid_message" in caplog.text


def test_worker_init_uses_the_selected_queues(monkeypatch):
    from app import worker

    class Queues:
        consume_from = {"ingestion.index": object()}

    class Amqp:
        queues = Queues()

    class App:
        amqp = Amqp()

    class Sender:
        app = App()

    seen = []
    monkeypatch.setattr(runtime, "preflight_for_role", lambda role, *a: seen.append(role))
    worker._role_preflight(Sender())
    assert seen == ["index"]
