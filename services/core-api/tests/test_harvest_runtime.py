"""Harvest feature flags, worker role, task registration and the operator CLI (M6)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.harvest import admin
from app.ingestion import runtime
from tests.harvest_support import USER_AGENT


def test_everything_external_is_off_by_default(monkeypatch):
    for name in ("HARVEST_ENABLED", "INDEXING_ENABLED", "CHAT_ENABLED", "HARVEST_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert (settings.harvest_enabled, settings.indexing_enabled, settings.chat_enabled) == (False, False, False)
    assert settings.harvest_user_agent is None


def test_harvest_settings_are_validated():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, harvest_user_agent="python-requests/2.0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, harvest_queue="ingestion.index")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, harvest_slice_max_requests=1)
    settings = Settings(_env_file=None, harvest_user_agent=USER_AGENT)
    assert settings.harvest_lease_seconds > settings.harvest_task_time_limit_seconds
    assert settings.harvest_slice_seconds < settings.harvest_task_soft_time_limit_seconds


def test_the_harvest_queue_needs_a_dedicated_worker():
    settings = Settings(_env_file=None)
    assert runtime.worker_role({"harvest.run"}, settings) == runtime.ROLE_HARVEST
    assert runtime.worker_role({"ingestion.parse", "ingestion.maintenance"}, settings) == runtime.ROLE_PROCESSING
    with pytest.raises(runtime.PreflightError, match="dedicated"):
        runtime.worker_role({"harvest.run", "ingestion.maintenance"}, settings)
    with pytest.raises(runtime.PreflightError, match="dedicated"):
        runtime.worker_role({"harvest.run", "ingestion.index"}, settings)


def test_harvest_worker_refuses_to_start_enabled_without_a_compliant_user_agent():
    settings = Settings(_env_file=None, harvest_enabled=True)
    environ = {"DATABASE_URL": "postgresql+psycopg://u:p@db:5432/x", "REDIS_URL": "redis://redis:6379/0"}
    with pytest.raises(runtime.PreflightError, match="HARVEST_USER_AGENT must be set"):
        runtime.harvest_worker_preflight(settings, environ)
    with pytest.raises(runtime.PreflightError, match="HARVEST_USER_AGENT"):
        runtime.harvest_worker_preflight(settings, {**environ, "HARVEST_USER_AGENT": "curl/8"})
    with pytest.raises(runtime.PreflightError, match="REDIS_URL"):
        runtime.harvest_worker_preflight(settings, {"DATABASE_URL": environ["DATABASE_URL"]})


@pytest.mark.parametrize(
    "args, kwargs, valid",
    [((), {"run_id": str(uuid.UUID(int=1))}, True), ((), {"run_id": str(uuid.UUID(int=0xABCDEF)).upper()}, False),
     ((), {"job_id": str(uuid.UUID(int=1))}, False), (("x",), {}, False), ((), {"run_id": 5}, False)],
)  # fmt: skip
def test_harvest_messages_are_validated(args, kwargs, valid):
    assert (runtime.run_id_of(args, kwargs) is not None) is valid


def test_harvest_task_is_registered_with_its_limits():
    from app.worker import celery_app, harvest_maintain, harvest_run_slice

    settings = Settings(_env_file=None)
    assert harvest_run_slice.soft_time_limit == settings.harvest_task_soft_time_limit_seconds
    assert harvest_run_slice.time_limit == settings.harvest_task_time_limit_seconds
    assert harvest_maintain.name in celery_app.tasks


# --- operator CLI (disposable database) ---------------------------------------------------------


@pytest.fixture
def environ(disposable_target):
    return {
        "DATABASE_URL": disposable_target.url.render_as_string(hide_password=False),
        "MIGRATIONS_REQUIRE_DATABASE_COMMENT": disposable_target.marker,
    }


def _run(environ, capsys, *argv):
    code = admin.main(list(argv), environ)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_operator_flow_is_explicit_at_every_step(world, environ, disposable_target, capsys):
    db = disposable_target.url.database
    owner = world.user("professor", name="Marie Curie")
    course = world.course(owner, code="PHYS 1")
    topic = world.topic(course, "Radioactive decay")
    world.topic(course, "Curie notes")
    code, out, _ = _run(environ, capsys, "init-source", "--database", db, "--provider", "wikipedia", "--language", "en")
    assert code == 0 and "disabled" in out
    source = uuid.UUID(out.strip().rsplit(" ", 1)[-1])
    world.sources.append(source)
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is False
    assert world.scalar("SELECT max_requests FROM harvest_provider_budgets WHERE source_id = :s", s=source) == 30
    code, _, err = _run(environ, capsys, "init-source", "--database", db, "--provider", "openalex", "--language", "en")
    assert code == 1 and "unavailable" in err
    code, _, err = _run(environ, capsys, "trigger", "--database", db, "--course", str(course), "--source", str(source))
    assert code == 1  # not opted in, source disabled
    assert _run(environ, capsys, "course-opt-in", "--database", db, "--course", str(course), "--source", str(source))[0] == 0
    code, out, _ = _run(environ, capsys, "propose-queries", "--database", db, "--course", str(course))
    assert "'Radioactive decay'\tok" in out and "member's name" in out
    assert world.scalar("SELECT count(*) FROM harvest_queries WHERE course_id = :c", c=course) == 0  # nothing auto-approved
    code, _, err = _run(environ, capsys, "approve-query", "--database", db, "--course", str(course), "--topic", str(topic),
                        "--text", "radioactive decay")  # fmt: skip
    assert code == 1 and "differs" in err
    code, out, _ = _run(environ, capsys, "approve-query", "--database", db, "--course", str(course), "--topic", str(topic),
                        "--text", "Radioactive decay")  # fmt: skip
    assert code == 0 and "approved" in out
    assert _run(environ, capsys, "trigger", "--database", db, "--course", str(course), "--source", str(source))[0] == 1
    assert _run(environ, capsys, "enable-source", "--database", db, "--source", str(source))[0] == 0
    code, out, _ = _run(environ, capsys, "trigger", "--database", db, "--course", str(course), "--source", str(source))
    assert code == 0 and "HARVEST_ENABLED is false" in out
    code, out, _ = _run(environ, capsys, "course-opt-out", "--database", db, "--course", str(course))
    assert code == 0 and "1 runs cancelled" in out
    code, out, _ = _run(environ, capsys, "status", "--database", db, "--course", str(course))
    assert "enabled=False" in out and "cancelled" in out and "u:p" not in out
    code, _, err = _run(environ, capsys, "status", "--database", "other_db", "--course", str(course))
    assert code == 1 and "does not match" in err


def test_openstax_sources_are_created_disabled_for_an_allowlisted_book_only(world, environ, disposable_target, capsys):
    db = disposable_target.url.database
    code, _, err = _run(environ, capsys, "init-source", "--database", db, "--provider", "openstax", "--book", "biology-2e")
    assert code == 1 and "allowlisted" in err
    code, _, err = _run(environ, capsys, "init-source", "--database", db, "--provider", "openstax", "--book",
                        "chemistry-2e", "--language", "de")  # fmt: skip
    assert code == 1 and "'en' only" in err
    code, _, err = _run(environ, capsys, "init-source", "--database", db, "--provider", "wikipedia", "--language", "en",
                        "--book", "chemistry-2e")  # fmt: skip
    assert code == 1 and "openstax provider only" in err
    code, out, _ = _run(environ, capsys, "init-source", "--database", db, "--provider", "openstax", "--book", "chemistry-2e")
    assert code == 0 and "disabled" in out
    source = uuid.UUID(out.strip().rsplit(" ", 1)[-1])
    world.sources.append(source)
    row = world.rows("SELECT * FROM sources WHERE id = :s", s=source)[0]
    assert (row["enabled"], row["provider"], row["language"], row["provider_book"], row["licence"]) == (
        False, "openstax", "en", "chemistry-2e", "CC BY-NC-SA 4.0")
    budget = world.rows("SELECT * FROM harvest_provider_budgets WHERE source_id = :s", s=source)[0]
    assert (budget["window_seconds"], budget["max_requests"], budget["min_interval_ms"]) == (3600, 50, 2000)
    code, out, _ = _run(environ, capsys, "init-source", "--database", db, "--provider", "openstax", "--book", "chemistry-2e")
    assert code == 0 and "unchanged" in out
    assert _run(environ, capsys, "enable-source", "--database", db, "--source", str(source))[0] == 0
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is True
