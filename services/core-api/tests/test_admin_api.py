"""WP-4: read-only admin observability API: authorization, contract, filters, pagination, safe errors."""

from __future__ import annotations

import uuid

import pytest

from app.auth import CurrentUser
from app.ingestion.jobs import GENERIC_FAILURE, PUBLIC_MESSAGES
from tests.harvest_support import opt_in

ENDPOINTS = ("/api/v1/admin/overview", "/api/v1/admin/sources", "/api/v1/admin/jobs")
FUTURE = "2999-01-01T00:00:00+00:00"  # rows created "later" than anything else sort first (newest first)


@pytest.fixture
def admin(world):
    return world.user("admin")


@pytest.fixture
def course(world):
    return world.course(world.user("professor"))


def _get(api, user, path, **params):
    return api.as_user(user).client.get(path, params=params)


def _job(world, course, kind, status, *, created_offset=0, message=None, job_id=None):
    document = world.document(course, title=f"doc-{uuid.uuid4().hex[:6]}")
    started = None if status == "queued" else FUTURE
    finished = FUTURE if status in ("succeeded", "failed") else None
    job_id = job_id or uuid.uuid4()
    world.execute(
        "INSERT INTO ingestion_jobs (id, document_id, kind, status, attempt_count, started_at, finished_at, "
        "error_message, created_at) VALUES (:id, :d, :k, :s, :a, :st, :f, :m, "
        "CAST(:t AS timestamptz) + :o * interval '1 second')",
        id=job_id, d=document, k=kind, s=status, a=0 if status == "queued" else 1, st=started, f=finished,
        m=message, t=FUTURE, o=created_offset,
    )  # fmt: skip
    return job_id, document


def _run(world, course, source, status, *, created_offset=0, error_code=None, run_id=None):
    opt_in(world, course, source)  # a run belongs to a harvest target (idempotent)
    run_id = run_id or uuid.uuid4()
    world.execute(
        "INSERT INTO harvest_runs (id, course_id, source_id, trigger, generation, max_claims, max_failures, status, "
        "finished_at, error_code, created_at) VALUES (:id, :c, :s, 'manual', 0, 4, 2, :st, :f, :e, "
        "CAST(:t AS timestamptz) + :o * interval '1 second')",
        id=run_id, c=course, s=source, st=status, f=None if status == "queued" else FUTURE, e=error_code,
        t=FUTURE, o=created_offset,
    )  # fmt: skip
    return run_id


# --- authorization --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ENDPOINTS)
def test_only_an_active_admin_row_is_authorized(api, world, admin, path):
    def check(user, status, code=None):
        response = _get(api, user, path)
        assert response.status_code == status, response.text
        assert response.headers["cache-control"] == "no-store"
        if code:
            assert response.json()["code"] == code
        return response

    check(None, 401, "not_authenticated")
    check(world.user("student"), 403, "admin_required")
    check(world.user("professor"), 403, "admin_required")
    disabled = world.user("admin", status="disabled")
    check(CurrentUser(id=disabled.id, role="admin", status="active"), 403, "account_disabled")  # the row decides
    check(CurrentUser(id=uuid.uuid4(), role="admin", status="active"), 401, "not_authenticated")  # vanished account
    # A token claiming admin for a student account is not enough: the users row is re-read.
    student = world.user("student")
    check(CurrentUser(id=student.id, role="admin", status="active"), 403, "admin_required")
    check(admin, 200)


def test_authorization_precedes_query_validation(api, world):
    response = _get(api, world.user("student"), "/api/v1/admin/jobs", kind="bogus")
    assert response.status_code == 403  # a non-admin learns nothing about accepted parameters


# --- overview -------------------------------------------------------------------------------------------------


def test_overview_counts_and_contract(api, world, admin, course, make_api):
    harness = make_api(settings_overrides={"exams_enabled": True})
    before = _get(harness, admin, "/api/v1/admin/overview").json()

    enabled, disabled = world.source(enabled=True), world.source(enabled=False)
    world.document(course, status="indexed", source_id=enabled, origin="external")
    _job(world, course, "parse", "failed", message="invalid_pdf: details")
    _job(world, course, "index", "queued")
    _run(world, course, enabled, "succeeded")
    _run(world, course, disabled, "cancelled")

    response = _get(harness, admin, "/api/v1/admin/overview")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    after = response.json()
    assert set(after) == {"as_of", "features", "sources", "documents", "jobs"}
    assert after["features"]["exams_enabled"] is True and after["features"]["harvest_enabled"] is False
    assert after["sources"]["total"] - before["sources"]["total"] == 2
    assert after["sources"]["enabled"] - before["sources"]["enabled"] == 1
    assert after["documents"]["indexed"] - before["documents"]["indexed"] == 1
    assert after["documents"]["pending"] - before["documents"]["pending"] == 2  # the two jobs' documents
    delta = lambda kind, status: after["jobs"][kind][status] - before["jobs"][kind][status]  # noqa: E731
    assert (delta("parse", "failed"), delta("index", "queued")) == (1, 1)
    assert (delta("harvest", "succeeded"), delta("harvest", "cancelled")) == (1, 1)
    assert set(after["jobs"]["harvest"]) == {"queued", "running", "succeeded", "failed", "cancelled"}
    assert _get(harness, admin, "/api/v1/admin/overview", x="1").status_code == 422


# --- jobs -----------------------------------------------------------------------------------------------------


def test_jobs_are_paged_newest_first_without_gaps_or_repeats(api, world, admin, course):
    source = world.source()
    tie = sorted(uuid.uuid4() for _ in range(2))
    mine = [
        _job(world, course, "parse", "succeeded", created_offset=40)[0],
        _run(world, course, source, "failed", created_offset=30, error_code="provider_unavailable"),
        _job(world, course, "index", "queued", created_offset=20, job_id=tie[1])[0],  # same time: higher id first
        _run(world, course, source, "queued", created_offset=20, run_id=tie[0]),
        _job(world, course, "parse", "failed", created_offset=10)[0],
    ]
    seen, cursor = [], None
    for _ in range(100):
        params = {"limit": 2} if cursor is None else {"limit": 2, "cursor": cursor}
        body = _get(api, admin, "/api/v1/admin/jobs", **params).json()
        assert len(body["items"]) <= 2
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen[:5] == [str(i) for i in mine]
    assert len(seen) == len(set(seen))


def test_job_filters_and_subjects(api, world, admin, course):
    source = world.source()
    parse_id, document = _job(world, course, "parse", "failed", message="invalid_pdf: x")
    run_id = _run(world, course, source, "succeeded")

    parse_items = _get(api, admin, "/api/v1/admin/jobs", kind="parse").json()["items"]
    assert parse_items and all(i["kind"] == "parse" for i in parse_items)
    item = next(i for i in parse_items if i["id"] == str(parse_id))
    assert item["subject"] == {"course_id": str(course), "document_id": str(document), "source_id": None, "trigger": None}
    assert item["attempts"] == {"used": 1, "limit": 3}

    harvest = _get(api, admin, "/api/v1/admin/jobs", kind="harvest", status="succeeded").json()["items"]
    assert all(i["kind"] == "harvest" and i["status"] == "succeeded" for i in harvest)
    run = next(i for i in harvest if i["id"] == str(run_id))
    assert run["subject"] == {"course_id": str(course), "document_id": None, "source_id": str(source), "trigger": "manual"}
    assert run["error"] is None and run["attempts"] == {"used": 0, "limit": 4}
    assert all(i["status"] == "failed" for i in _get(api, admin, "/api/v1/admin/jobs", status="failed").json()["items"])


def test_job_errors_are_summarized_without_operational_detail(api, world, admin, course):
    secret = "Traceback (most recent call last): /srv/app/secret.py password=hunter2"
    known, _ = _job(world, course, "parse", "failed", message=f"invalid_pdf: {secret}")
    unknown_code, _ = _job(world, course, "index", "failed", message=f"weird_new_code: {secret}")
    unparsable, _ = _job(world, course, "parse", "failed", message=secret)
    run = _run(world, course, world.source(), "failed", error_code="provider_licence_mismatch")

    response = _get(api, admin, "/api/v1/admin/jobs", status="failed", limit=100)
    assert "hunter2" not in response.text and "Traceback" not in response.text and "/srv/" not in response.text
    errors = {i["id"]: i["error"] for i in response.json()["items"]}
    assert errors[str(known)] == {"code": "invalid_pdf", "message": PUBLIC_MESSAGES["invalid_pdf"]}
    assert errors[str(unknown_code)] == {"code": "weird_new_code", "message": GENERIC_FAILURE}
    assert errors[str(unparsable)] == {"code": "unknown", "message": GENERIC_FAILURE}
    assert errors[str(run)] == {"code": "provider_licence_mismatch", "message": None}


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"kind": "bogus"}, "invalid_query"),
        ({"status": "done"}, "invalid_query"),
        ({"limit": "0"}, "invalid_query"),
        ({"limit": "101"}, "invalid_query"),
        ({"limit": "abc"}, "invalid_query"),
        ({"page": "2"}, "invalid_query"),
        ({"cursor": "not-a-cursor"}, "invalid_cursor"),
        ({"cursor": "eyJ2IjoxfQ"}, "invalid_cursor"),
    ],
)
def test_invalid_job_queries_are_refused(api, admin, params, code):
    response = _get(api, admin, "/api/v1/admin/jobs", **params)
    assert response.status_code == 422 and response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"


def test_repeated_parameters_and_foreign_cursors_are_refused(api, world, admin, course):
    for offset in range(3):
        _job(world, course, "parse", "queued", created_offset=offset)
    client = api.as_user(admin).client
    assert client.get("/api/v1/admin/jobs?kind=parse&kind=index").status_code == 422
    cursor = _get(api, admin, "/api/v1/admin/jobs", kind="parse", limit=1).json()["next_cursor"]
    assert cursor is not None
    assert _get(api, admin, "/api/v1/admin/jobs", kind="parse", limit=1, cursor=cursor).status_code == 200
    changed = _get(api, admin, "/api/v1/admin/jobs", kind="index", limit=1, cursor=cursor)  # other filters
    assert changed.status_code == 422 and changed.json()["code"] == "invalid_cursor"
    other_admin = _get(api, world.user("admin"), "/api/v1/admin/jobs", kind="parse", limit=1, cursor=cursor)
    assert other_admin.status_code == 422 and other_admin.json()["code"] == "invalid_cursor"


def test_empty_results(api, admin):
    body = _get(api, admin, "/api/v1/admin/jobs", kind="parse", status="running").json()
    assert body == {"items": [], "next_cursor": None}


# --- sources --------------------------------------------------------------------------------------------------


def test_sources_contract_counts_filter_and_paging(api, world, admin, course):
    first, second = world.source(enabled=True), world.source(enabled=False)
    world.execute("UPDATE sources SET created_at = '2999-01-01 00:00:02' WHERE id = :s", s=first)
    world.execute("UPDATE sources SET created_at = '2999-01-01 00:00:01' WHERE id = :s", s=second)
    opt_in(world, course, first)
    world.document(course, status="indexed", source_id=first, origin="external")
    world.document(course, status="stale", source_id=first, origin="external")
    _run(world, course, first, "failed", created_offset=1, error_code="provider_unavailable")
    latest = _run(world, course, first, "succeeded", created_offset=5)

    page = _get(api, admin, "/api/v1/admin/sources", limit=1).json()
    assert [s["id"] for s in page["items"]] == [str(first)] and page["next_cursor"]
    item = page["items"][0]
    assert set(item) == {"id", "kind", "provider", "language", "domain", "licence", "licence_url", "enabled",
                         "created_at", "courses", "documents", "last_run"}  # fmt: skip
    assert item["enabled"] is True and item["courses"] == 1
    assert item["documents"] == {"pending": 0, "indexed": 1, "failed": 0, "stale": 1}
    assert item["last_run"]["id"] == str(latest) and item["last_run"]["status"] == "succeeded"
    assert item["created_at"].startswith("2999-01-01T00:00:02")

    page2 = _get(api, admin, "/api/v1/admin/sources", limit=1, cursor=page["next_cursor"]).json()
    assert [s["id"] for s in page2["items"]] == [str(second)]
    assert page2["items"][0]["last_run"] is None and page2["items"][0]["courses"] == 0

    only_disabled = _get(api, admin, "/api/v1/admin/sources", enabled="false", limit=100).json()["items"]
    assert str(second) in {s["id"] for s in only_disabled} and all(s["enabled"] is False for s in only_disabled)
    assert _get(api, admin, "/api/v1/admin/sources", enabled="yes").status_code == 422
