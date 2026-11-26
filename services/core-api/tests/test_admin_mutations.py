"""WP-9: admin mutations (source switch, manual harvest trigger, parse/index retry) and their audit events.

Runs only against the disposable test database (tests/disposable_db.py guards). Nothing here contacts a
provider: a triggered run is only queued.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.admin import mutations
from app.auth import CurrentUser
from app.errors import ApiError
from app.harvest import admin as harvest_cli
from app.harvest import runs
from tests.harvest_support import USER_AGENT, claimed_run, harvest_settings, opt_in, run_row, wikipedia_source

HARVEST_ON = {"harvest_enabled": True, "harvest_user_agent": USER_AGENT}
SECRET_MESSAGE = "storage_error: EIO reading /srv/uploads/private-notes.pdf (Traceback: token=abc123)"
SECRET_TITLE = "Private lecture notes.pdf"
_NO_BODY = object()


@pytest.fixture
def admin(world):
    return world.user("admin")


@pytest.fixture
def owner(world):
    return world.user("professor")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


@pytest.fixture
def factory(world):
    return sessionmaker(bind=world.engine, autoflush=False)


def _send(api, user, method, path, body=_NO_BODY, **options):
    client = api.as_user(user).client
    if body is _NO_BODY:
        return client.request(method, path, **options)
    return client.request(method, path, json=body, **options)


def _audit(world, **where) -> list:
    clause = " AND ".join(f"{name} = :{name}" for name in where) or "TRUE"
    return world.rows(f"SELECT * FROM admin_audit_events WHERE {clause} ORDER BY created_at, id", **where)


def _source_path(source_id) -> str:
    return f"/api/v1/admin/sources/{source_id}/enabled"


def _retry_path(job_id) -> str:
    return f"/api/v1/admin/jobs/{job_id}/retry"


def _provider_source(world, language: str, *, enabled: bool = True) -> uuid.UUID:
    """Another Wikipedia source (one per language): provider-managed, with its request budget."""
    source_id = world._one(
        "INSERT INTO sources (kind, domain, licence, enabled, provider, language, licence_url) "
        "VALUES ('wikipedia', :domain, 'CC BY-SA 4.0', :e, 'wikipedia', :l, "
        "'https://creativecommons.org/licenses/by-sa/4.0/') RETURNING id",
        domain=f"{language}.wikipedia.org", e=enabled, l=language,
    )  # fmt: skip
    world.sources.append(source_id)
    world.execute("INSERT INTO harvest_provider_budgets (source_id, window_seconds, max_requests, min_interval_ms) "
                  "VALUES (:s, 60, 10, 0)", s=source_id)  # fmt: skip
    return source_id


def _queued_run(world, course_id, source_id) -> uuid.UUID:
    opt_in(world, course_id, source_id)
    return world._one(
        "INSERT INTO harvest_runs (course_id, source_id, trigger, generation, max_claims, max_failures) "
        "VALUES (:c, :s, 'manual', 0, 4, 2) RETURNING id",
        c=course_id, s=source_id,
    )  # fmt: skip


def _failed_upload(world, course_id, owner) -> tuple[uuid.UUID, uuid.UUID]:
    """A failed class upload with one failed parse job whose stored message holds operational detail."""
    document = world.document(
        course_id, status="failed", title=SECRET_TITLE, storage_key=f"k/{uuid.uuid4().hex}",
        original_filename="notes.pdf", mime_type="application/pdf", size_bytes=10, content_hash=uuid.uuid4().hex * 2,
        uploaded_by=owner.id, error_message="This document could not be processed because of a server problem.",
    )  # fmt: skip
    job = world._one(
        "INSERT INTO ingestion_jobs (document_id, kind, status, attempt_count, started_at, finished_at, error_message) "
        "VALUES (:d, 'parse', 'failed', 3, now(), now(), :m) RETURNING id",
        d=document, m=SECRET_MESSAGE,
    )  # fmt: skip
    return document, job


def _failed_index(world, course_id, *, parsed: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    """A failed document whose parse succeeded (sections stored) and whose index job failed."""
    document = world.document(course_id, status="failed", title=SECRET_TITLE)
    if parsed:
        world.execute("INSERT INTO ingestion_jobs (document_id, kind, status, attempt_count, started_at, finished_at) "
                      "VALUES (:d, 'parse', 'succeeded', 1, now(), now())", d=document)  # fmt: skip
        world.execute("INSERT INTO document_sections (document_id, ordinal, text, locator) "
                      "VALUES (:d, 0, 'Le Chatelier', '{\"page\": 1}')", d=document)  # fmt: skip
    job = world._one(
        "INSERT INTO ingestion_jobs (document_id, kind, status, attempt_count, started_at, finished_at, error_message) "
        "VALUES (:d, 'index', 'failed', 1, now(), now(), 'index_rejected: upstream said no') RETURNING id",
        d=document,
    )  # fmt: skip
    return document, job


def _race(count: int, call) -> list:
    """Run `call` from `count` threads at once; each result is the return value or the ApiError."""
    barrier, results = threading.Barrier(count), [None] * count

    def worker(index):
        barrier.wait()
        try:
            results[index] = call()
        except ApiError as exc:
            results[index] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


# --- authorization ------------------------------------------------------------------------------------------


def test_only_an_existing_active_admin_row_may_mutate_and_refusals_change_nothing(make_api, world, admin, course):
    api = make_api(settings_overrides=HARVEST_ON)
    source = wikipedia_source(world)
    opt_in(world, course, source)
    _, parse_job = _failed_upload(world, course, world.user("professor"))
    calls = (
        ("PUT", _source_path(source), {"enabled": False}),
        ("POST", "/api/v1/admin/harvest-runs", {"course_id": str(course), "source_id": str(source)}),
        ("POST", _retry_path(parse_job), _NO_BODY),
    )
    student, professor = world.user("student"), world.user("professor")
    disabled = world.user("admin", status="disabled")
    refused = (
        (None, 401, "not_authenticated"),
        (student, 403, "admin_required"),
        (professor, 403, "admin_required"),
        (CurrentUser(id=disabled.id, role="admin", status="active"), 403, "account_disabled"),  # the row decides
        (CurrentUser(id=uuid.uuid4(), role="admin", status="active"), 401, "not_authenticated"),  # deleted account
        (CurrentUser(id=student.id, role="admin", status="active"), 403, "admin_required"),  # forged role claim
    )
    for method, path, body in calls:
        for user, status, code in refused:
            response = _send(api, user, method, path, body)
            assert (response.status_code, response.json()["code"]) == (status, code), (path, response.text)
            assert response.headers["cache-control"] == "no-store"
        # Authorization precedes body validation: a non-admin learns nothing about accepted input.
        assert _send(api, student, method, path, {"bogus": 1}).status_code == 403
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is True
    assert world.scalar("SELECT count(*) FROM harvest_runs WHERE course_id = :c", c=course) == 0
    assert world.scalar("SELECT status FROM ingestion_jobs WHERE id = :j", j=parse_job) == "failed"
    assert world.scalar("SELECT count(*) FROM ingestion_jobs WHERE kind = 'parse' AND document_id = "
                        "(SELECT document_id FROM ingestion_jobs WHERE id = :j)", j=parse_job) == 1  # fmt: skip
    assert _audit(world) == []


def test_a_concurrent_demotion_is_seen_inside_the_mutation_transaction(world, admin, factory):
    source = wikipedia_source(world)
    holder = world.engine.connect()
    holder.execute(text("UPDATE users SET role = 'student' WHERE id = :u"), {"u": admin.id})  # uncommitted, row locked
    outcome = {}
    disable = lambda: outcome.update(r=_capture(lambda: mutations.set_source(factory, admin, source, False)))  # noqa: E731
    thread = threading.Thread(target=disable)
    thread.start()
    time.sleep(0.5)  # the mutation now waits for the users row (FOR SHARE)
    holder.commit()
    holder.close()
    thread.join(30)
    assert isinstance(outcome["r"], ApiError) and outcome["r"].code == "admin_required"
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is True and _audit(world) == []


def _capture(call):
    try:
        return call()
    except ApiError as exc:
        return exc


def test_a_lock_timeout_is_answered_busy_without_any_change(api, world, admin):
    source = wikipedia_source(world)
    holder = world.engine.connect()
    holder.execute(text("SELECT 1 FROM users WHERE id = :u FOR UPDATE"), {"u": admin.id})
    try:
        response = _send(api, admin, "PUT", _source_path(source), {"enabled": False})
    finally:
        holder.rollback()
        holder.close()
    assert (response.status_code, response.json()["code"]) == (503, "admin_busy")
    assert response.headers["retry-after"] == "2" and response.headers["cache-control"] == "no-store"
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is True and _audit(world) == []


# --- sources ------------------------------------------------------------------------------------------------


def test_disabling_a_source_cancels_its_active_runs_fences_the_running_attempt_and_is_audited(
    api, world, admin, owner, factory
):
    source = wikipedia_source(world)
    running_course, queued_course = world.course(owner), world.course(owner)
    opt_in(world, running_course, source)
    claim = claimed_run(factory, harvest_settings(), running_course, source)
    queued = _queued_run(world, queued_course, source)

    response = _send(api, admin, "PUT", _source_path(source), {"enabled": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert {k: body[k] for k in ("source_id", "enabled", "changed", "cancelled_runs")} == {
        "source_id": str(source), "enabled": False, "changed": True, "cancelled_runs": 2}  # fmt: skip
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is False
    for run_id in (claim.run_id, queued):
        assert (run_row(world, run_id)["status"], run_row(world, run_id)["error_code"]) == ("cancelled", "source_disabled")
    with factory() as session:
        assert runs.lock_owned(session, claim) is False  # the running attempt writes nothing any more
    [event] = _audit(world, entity_id=source)
    assert str(event["id"]) == body["audit_event_id"]
    assert (event["actor_id"], event["action"], event["entity_kind"], event["operation_id"]) == (
        admin.id, "source_disabled", "source", None)  # fmt: skip
    assert event["details"] == {"before": {"enabled": True}, "after": {"enabled": False}, "cancelled_runs": 2}

    # Repeating the request changes nothing and records nothing.
    again = _send(api, admin, "PUT", _source_path(source), {"enabled": False}).json()
    assert (again["changed"], again["cancelled_runs"], again["audit_event_id"]) == (False, 0, None)
    assert len(_audit(world, entity_id=source)) == 1

    # Enabling only sets the flag: no run is created, opt-in and HARVEST_ENABLED still gate everything.
    enabled = _send(api, admin, "PUT", _source_path(source), {"enabled": True}).json()
    assert (enabled["changed"], enabled["enabled"]) == (True, True)
    assert world.scalar("SELECT count(*) FROM harvest_runs WHERE source_id = :s AND status IN ('queued', 'running')",
                        s=source) == 0  # fmt: skip
    events = _audit(world, entity_id=source)
    assert [e["action"] for e in events] == ["source_disabled", "source_enabled"]
    assert events[1]["details"] == {"before": {"enabled": False}, "after": {"enabled": True}, "cancelled_runs": 0}


def test_source_requests_are_validated_without_side_effects(api, world, admin):
    source = wikipedia_source(world)
    legacy = world.source()  # not provider-managed: never switched here

    def check(path, body, status, code, **options):
        response = _send(api, admin, "PUT", path, body, **options)
        assert (response.status_code, response.json()["code"]) == (status, code), response.text
        assert response.headers["cache-control"] == "no-store"

    check(_source_path(legacy), {"enabled": False}, 404, "source_not_found")
    check(_source_path(uuid.uuid4()), {"enabled": False}, 404, "source_not_found")
    check(_source_path(str(source).upper()), {"enabled": False}, 404, "source_not_found")
    check(_source_path("not-a-uuid"), {"enabled": False}, 404, "source_not_found")
    check(_source_path(source), {"enabled": "false"}, 422, "invalid_fields")
    check(_source_path(source), {"enabled": 0}, 422, "invalid_fields")
    check(_source_path(source), {}, 422, "invalid_fields")
    check(_source_path(source), {"enabled": False, "actor_id": str(uuid.uuid4())}, 422, "invalid_fields")
    check(_source_path(source), _NO_BODY, 415, "unsupported_media_type")
    check(_source_path(source), _NO_BODY, 400, "invalid_body", content=b"{", headers={"content-type": "application/json"})
    check(_source_path(source), _NO_BODY, 413, "body_too_large", content=b"{" + b" " * 2048 + b"}",
          headers={"content-type": "application/json"})  # fmt: skip
    check(_source_path(source) + "?force=1", {"enabled": False}, 422, "invalid_query")
    assert _send(api, admin, "PATCH", _source_path(source), {"enabled": False}).status_code == 405
    assert _send(api, admin, "DELETE", "/api/v1/admin/sources/" + str(source)).status_code in (404, 405)
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=legacy) is True
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is True and _audit(world) == []


def test_concurrent_disables_change_and_audit_once(world, admin, factory):
    source = wikipedia_source(world)
    results = _race(6, lambda: mutations.set_source(factory, admin, source, False))
    assert sorted(r["changed"] for r in results) == [False] * 5 + [True]
    assert len(_audit(world, entity_id=source)) == 1


def test_the_operator_cli_uses_the_same_transition_and_writes_no_http_audit(world, owner, disposable_target, capsys):
    source = wikipedia_source(world)
    queued = _queued_run(world, world.course(owner), source)
    environ = {"DATABASE_URL": disposable_target.url.render_as_string(hide_password=False),
               "MIGRATIONS_REQUIRE_DATABASE_COMMENT": disposable_target.marker}  # fmt: skip
    assert harvest_cli.main(["disable-source", "--database", disposable_target.url.database, "--source", str(source)],
                            environ) == 0  # fmt: skip
    assert "1 runs cancelled" in capsys.readouterr().out
    assert run_row(world, queued)["status"] == "cancelled" and _audit(world) == []
    code = harvest_cli.main(["enable-source", "--database", disposable_target.url.database, "--source",
                             str(world.source())], environ)  # fmt: skip
    assert code == 1 and "no provider-managed source" in capsys.readouterr().err


# --- harvest trigger ----------------------------------------------------------------------------------------


def test_trigger_respects_every_gate_and_writes_nothing_when_refused(api, make_api, world, admin, owner):
    source = wikipedia_source(world)
    course = world.course(owner)
    opt_in(world, course, source)
    payload = {"course_id": str(course), "source_id": str(source)}

    def check(harness, body, status, code):
        response = _send(harness, admin, "POST", "/api/v1/admin/harvest-runs", body)
        assert (response.status_code, response.json()["code"]) == (status, code), response.text
        assert response.headers["cache-control"] == "no-store"

    check(api, payload, 409, "harvest_disabled")  # HARVEST_ENABLED is false by default
    harvest = make_api(settings_overrides=HARVEST_ON)
    other_source, disabled_source = _provider_source(world, "de"), _provider_source(world, "fr", enabled=False)
    not_opted, opted_out = world.course(owner), world.course(owner)
    opt_in(world, opted_out, source, enabled=False)
    check(harvest, {"course_id": str(not_opted), "source_id": str(source)}, 409, "course_not_opted_in")
    check(harvest, {"course_id": str(opted_out), "source_id": str(source)}, 409, "course_not_opted_in")
    check(harvest, {"course_id": str(course), "source_id": str(other_source)}, 409, "target_missing")
    opt_in(world, course, disabled_source)
    check(harvest, {"course_id": str(course), "source_id": str(disabled_source)}, 409, "source_disabled")
    check(harvest, {"course_id": str(uuid.uuid4()), "source_id": str(source)}, 404, "course_not_found")
    check(harvest, {"course_id": str(course), "source_id": str(uuid.uuid4())}, 404, "source_not_found")
    check(harvest, {"course_id": str(course), "source_id": str(world.source())}, 404, "source_not_found")
    check(harvest, {"course_id": str(course).upper(), "source_id": str(source)}, 422, "invalid_fields")
    check(harvest, {"course_id": 7, "source_id": str(source)}, 422, "invalid_fields")
    check(harvest, {"course_id": str(course)}, 422, "invalid_fields")
    check(harvest, {**payload, "trigger": "dirty"}, 422, "invalid_fields")
    check(harvest, [payload], 400, "invalid_body")
    assert world.scalar("SELECT count(*) FROM harvest_runs") == 0 and _audit(world) == []


def test_trigger_queues_one_manual_run_with_the_configured_limits_and_audits_it(make_api, world, admin, course):
    api = make_api(settings_overrides={**HARVEST_ON, "harvest_run_max_claims": 7, "harvest_run_max_failures": 3})
    source = wikipedia_source(world)
    opt_in(world, course, source)
    payload = {"course_id": str(course), "source_id": str(source)}

    response = _send(api, admin, "POST", "/api/v1/admin/harvest-runs", payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["status"], body["trigger"]) == ("queued", "manual")
    assert (body["course_id"], body["source_id"]) == (str(course), str(source))
    run = run_row(world, uuid.UUID(body["run_id"]))
    assert (run["status"], run["trigger"], run["claim_count"], run["max_claims"], run["max_failures"]) == (
        "queued", "manual", 0, 7, 3)  # fmt: skip  (queued only: the worker makes every provider request)
    [event] = _audit(world, entity_id=course)
    assert (str(event["id"]), event["actor_id"], event["action"], event["entity_kind"], str(event["operation_id"])) == (
        body["audit_event_id"], admin.id, "harvest_triggered", "course", body["run_id"])  # fmt: skip
    assert event["details"] == {"source_id": str(source), "run_id": body["run_id"], "trigger": "manual",
                                "generation": 0, "max_claims": 7, "max_failures": 3}  # fmt: skip

    duplicate = _send(api, admin, "POST", "/api/v1/admin/harvest-runs", payload)
    assert (duplicate.status_code, duplicate.json()["code"], duplicate.json()["run_id"]) == (
        409, "harvest_run_active", body["run_id"])  # fmt: skip
    assert world.scalar("SELECT count(*) FROM harvest_runs WHERE course_id = :c", c=course) == 1
    assert len(_audit(world)) == 1


def test_concurrent_triggers_create_one_run_and_one_audit_event(world, admin, course, factory):
    source = wikipedia_source(world)
    opt_in(world, course, source)
    settings = harvest_settings()
    results = _race(6, lambda: mutations.trigger_harvest(factory, admin, settings, course, source))
    created = [r for r in results if isinstance(r, dict)]
    assert len(created) == 1
    assert sorted({r.code for r in results if isinstance(r, ApiError)}) == ["harvest_run_active"]
    assert world.scalar("SELECT count(*) FROM harvest_runs WHERE course_id = :c", c=course) == 1
    assert len(_audit(world)) == 1


# --- job retries --------------------------------------------------------------------------------------------


def test_parse_retry_queues_a_new_job_keeps_the_failure_and_leaks_nothing(api, world, admin, owner, course, caplog):
    document, failed_job = _failed_upload(world, course, owner)
    caplog.set_level(logging.INFO)

    response = _send(api, admin, "POST", _retry_path(failed_job))
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["retried_job_id"], body["document_id"], body["kind"], body["status"]) == (
        str(failed_job), str(document), "parse", "queued")  # fmt: skip
    new_job = world.rows("SELECT * FROM ingestion_jobs WHERE id = :j", j=body["job_id"])[0]
    assert (new_job["kind"], new_job["status"], new_job["attempt_count"]) == ("parse", "queued", 0)
    old = world.rows("SELECT * FROM ingestion_jobs WHERE id = :j", j=failed_job)[0]
    assert (old["status"], old["attempt_count"], old["error_message"]) == ("failed", 3, SECRET_MESSAGE)  # history
    assert world.scalar("SELECT status FROM documents WHERE id = :d", d=document) == "pending"
    [event] = _audit(world, entity_id=document)
    assert (event["actor_id"], event["action"], event["entity_kind"], str(event["operation_id"])) == (
        admin.id, "parse_retried", "document", body["job_id"])  # fmt: skip
    assert event["details"] == {
        "retried_job_id": str(failed_job), "job_id": body["job_id"], "kind": "parse",
        "before": {"document_status": "failed", "error_code": "storage_error"},
        "after": {"document_status": "pending", "job_status": "queued"},
    }  # fmt: skip
    for leaked in ("/srv", "EIO", "Traceback", "abc123", SECRET_TITLE):
        assert leaked not in response.text and leaked not in json.dumps(event["details"]) and leaked not in caplog.text

    # The read-only jobs view still lists the failed job with its safe summary.
    listed = _send(api, admin, "GET", "/api/v1/admin/jobs", params={"kind": "parse", "status": "failed"}).json()
    assert [item["error"]["code"] for item in listed["items"] if item["id"] == str(failed_job)] == ["storage_error"]

    # Repeats and other ineligible jobs are refused without a change or an audit event.
    def check(job, status, code):
        refused = _send(api, admin, "POST", _retry_path(job))
        assert (refused.status_code, refused.json()["code"]) == (status, code), refused.text

    check(failed_job, 409, "retry_refused")  # a parse job is already queued
    check(body["job_id"], 409, "job_not_failed")  # queued, not failed
    check(uuid.uuid4(), 404, "job_not_found")
    check("NOT-A-UUID", 404, "job_not_found")
    harvest_run = _queued_run(world, course, wikipedia_source(world))
    check(harvest_run, 404, "job_not_found")  # harvest runs are triggered, never retried here
    assert _send(api, admin, "POST", _retry_path(failed_job), {"kind": "index"}).json()["code"] == "invalid_fields"
    assert world.scalar("SELECT count(*) FROM ingestion_jobs WHERE document_id = :d", d=document) == 2
    assert len(_audit(world)) == 1


def test_parse_retry_refuses_a_document_that_is_not_failed(api, world, admin, owner, course):
    document, failed_job = _failed_upload(world, course, owner)
    world.execute("UPDATE documents SET status = 'indexed' WHERE id = :d", d=document)
    response = _send(api, admin, "POST", _retry_path(failed_job), {})
    assert (response.status_code, response.json()["code"]) == (409, "retry_refused")
    assert "indexed" in response.json()["detail"]
    assert world.scalar("SELECT count(*) FROM ingestion_jobs WHERE document_id = :d", d=document) == 1
    assert _audit(world) == []


def test_index_retry_honours_indexing_enabled_and_the_retry_rules(api, make_api, world, admin, course):
    document, failed_job = _failed_index(world, course)
    refused = _send(api, admin, "POST", _retry_path(failed_job))
    assert (refused.status_code, refused.json()["code"]) == (409, "indexing_disabled")
    assert world.scalar("SELECT status FROM documents WHERE id = :d", d=document) == "failed"

    indexing = make_api(settings_overrides={"indexing_enabled": True})
    response = _send(indexing, admin, "POST", _retry_path(failed_job))
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["kind"], body["document_id"]) == ("index", str(document))
    assert world.scalar("SELECT status FROM ingestion_jobs WHERE id = :j", j=failed_job) == "failed"
    [event] = _audit(world, entity_id=document)
    assert (event["action"], event["details"]["before"]["error_code"]) == ("index_retried", "index_rejected")

    never_parsed, never_parsed_job = _failed_index(world, course, parsed=False)
    again = _send(indexing, admin, "POST", _retry_path(never_parsed_job))
    assert (again.status_code, again.json()["code"]) == (409, "retry_refused")
    assert len(_audit(world)) == 1


def test_concurrent_retries_of_one_job_create_one_job_and_one_audit_event(world, admin, owner, course, factory):
    document, failed_job = _failed_upload(world, course, owner)
    settings = harvest_settings()
    results = _race(6, lambda: mutations.retry_job(factory, admin, settings, failed_job))
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert {r.code for r in results if isinstance(r, ApiError)} == {"retry_refused"}
    assert world.scalar("SELECT count(*) FROM ingestion_jobs WHERE document_id = :d AND status = 'queued'", d=document) == 1
    assert len(_audit(world)) == 1


# --- atomicity and the audit table --------------------------------------------------------------------------


def test_a_failing_audit_insert_rolls_the_mutation_back(api, world, admin, owner, course, monkeypatch):
    source = wikipedia_source(world)
    queued = _queued_run(world, course, source)
    document, failed_job = _failed_upload(world, course, owner)
    original = mutations._audit

    def oversized(session, actor, action, kind, entity, operation, details):
        return original(session, actor, action, kind, entity, operation, {**details, "pad": "x" * 4096})  # CHECK fails

    monkeypatch.setattr(mutations, "_audit", oversized)
    for method, path, body in (("PUT", _source_path(source), {"enabled": False}), ("POST", _retry_path(failed_job), _NO_BODY)):
        response = _send(api, admin, method, path, body)
        assert (response.status_code, response.json()["code"]) == (500, "server_error")
        assert "pad" not in response.text and "check" not in response.text.lower()
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is True
    assert run_row(world, queued)["status"] == "queued"
    assert world.scalar("SELECT status FROM documents WHERE id = :d", d=document) == "failed"
    assert world.scalar("SELECT count(*) FROM ingestion_jobs WHERE document_id = :d", d=document) == 1
    assert _audit(world) == []


def test_audit_events_are_append_only_and_need_an_active_admin_actor(world, admin):
    source = wikipedia_source(world)
    insert = ("INSERT INTO admin_audit_events (actor_id, action, entity_kind, entity_id, details) "
              "VALUES (:a, 'source_enabled', 'source', :s, '{}')")  # fmt: skip
    for actor in (world.user("student").id, world.user("admin", status="disabled").id):
        with pytest.raises(DBAPIError, match="active admin actor"):
            world.execute(insert, a=actor, s=source)
    with pytest.raises(DBAPIError, match="shape_check"):
        world.execute("INSERT INTO admin_audit_events (actor_id, action, entity_kind, entity_id, details) "
                      "VALUES (:a, 'source_enabled', 'document', :s, '{}')", a=admin.id, s=source)  # fmt: skip
    world.execute(insert, a=admin.id, s=source)
    for statement in ("UPDATE admin_audit_events SET details = '{}' WHERE actor_id = :a",
                      "DELETE FROM admin_audit_events WHERE actor_id = :a", "TRUNCATE admin_audit_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            world.execute(statement, a=admin.id)
    assert len(_audit(world, actor_id=admin.id)) == 1
    with pytest.raises(DBAPIError):  # the actor cannot be deleted while history names them
        world.execute("DELETE FROM users WHERE id = :a", a=admin.id)
