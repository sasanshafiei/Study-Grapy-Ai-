"""Authorization of every M9 endpoint (AUTH-01 ... AUTH-05) and the default-off switches (REG-02 ... REG-04)."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.auth import CurrentUser
from app.config import Settings
from app.exams import bank
from tests import exam_support as ex
from tests.api_support import asgi_call
from tests.exam_support import ExamClient, key, match, mcq_body

# (method, path suffix under /api/v1/courses/{course}, needs body, needs Idempotency-Key, needs If-Match)
ENDPOINTS = [
    ("GET", "/exam-questions", None), ("POST", "/exam-questions", "question"), ("GET", "/exam-questions/{q}", None),
    ("PUT", "/exam-questions/{q}", "question"), ("POST", "/exam-questions/{q}/approve", None),
    ("POST", "/exam-questions/{q}/retire", None), ("GET", "/exam-questions/{q}/revisions", None),
    ("GET", "/exam-questions/{q}/history", None), ("GET", "/exams", None), ("POST", "/exams", "exam"),
    ("GET", "/exams/{e}", None), ("PATCH", "/exams/{e}", "patch"), ("PUT", "/exams/{e}/blueprint", "blueprint"),
    ("POST", "/exams/{e}/items", "item"), ("PATCH", "/exams/{e}/items/{i}", "points"), ("DELETE", "/exams/{e}/items/{i}", None),
    ("PUT", "/exams/{e}/items/order", "order"), ("POST", "/exams/{e}/fill-from-blueprint", "fill"),
    ("GET", "/exams/{e}/preview", None), ("POST", "/exams/{e}/finalize", "finalize"), ("POST", "/exams/{e}/archive", None),
    ("POST", "/exams/{e}/duplicate", "duplicate"), ("GET", "/exams/{e}/history", None),
    ("GET", "/exams/{e}/export?variant=student&format=pdf", None), ("GET", "/exams/{e}/preview-file?variant=student&format=pdf", None),
]  # fmt: skip


def _body(kind, ids):
    return {
        None: None, "question": mcq_body(), "exam": {"title": "T", "instructions": None, "duration_minutes": None},
        "patch": {"title": "T2"}, "blueprint": {"rows": []}, "points": {"points": 3}, "order": {"item_ids": [ids["i"]]},
        "item": {"question_id": ids["q"], "points": 1, "position": None, "blueprint_row_id": None},
        "fill": {"row_ids": None}, "finalize": {"preview_fingerprint": "pf1:" + "0" * 64, "blueprint_deviation_reason": None},
        "duplicate": {"title": None},
    }[kind]  # fmt: skip


@pytest.fixture
def matrix(world, make_api):
    owner = world.user("professor")
    course = world.course(owner, code="CHEM 101")
    setup = make_api(settings_overrides=ex.EXPORT_ENABLED).as_user(owner)
    x = ExamClient(setup, course)
    q = x.question()
    exam = x.add(x.exam(), q)
    ids = {"q": q["id"], "e": exam["id"], "i": exam["items"][0]["id"]}
    student = world.user("student")
    world.enroll(student, course)
    disabled = CurrentUser(id=owner.id, role="professor", status="disabled")
    callers = {
        "owner": owner, "other_professor": world.user("professor"), "admin": world.user("admin"), "student": student,
        "unenrolled_student": world.user("student"), "disabled": disabled, "anonymous": None,
    }  # fmt: skip
    return {"world": world, "make_api": make_api, "course": course, "ids": ids, "callers": callers, "owner": owner}


def _call(api, method, course, suffix, kind, ids, *, headers=None):
    path = f"/api/v1/courses/{course}" + suffix.format(**ids)
    body = _body(kind, ids)
    h = {**key(), **match(1), **(headers or {})}
    return api.client.request(method, path, json=body, headers=h) if body is not None else api.client.request(method, path, headers=h)


@pytest.mark.parametrize("method,suffix,kind", ENDPOINTS)
def test_auth01_every_endpoint_answers_by_role(matrix, method, suffix, kind):
    api = matrix["make_api"](settings_overrides=ex.EXPORT_ENABLED)
    expected = {"anonymous": (401, "not_authenticated"), "disabled": (403, "account_disabled"),
                "student": (403, "exams_not_allowed"), "other_professor": (404, "course_not_found"),
                "admin": (404, "course_not_found"), "unenrolled_student": (404, "course_not_found")}  # fmt: skip
    bodies_404 = []
    for name, (status, code) in expected.items():
        r = _call(api.as_user(matrix["callers"][name]), method, matrix["course"], suffix, kind, matrix["ids"])
        assert (r.status_code, r.json()["code"]) == (status, code), name
        assert r.headers["cache-control"] == "no-store"
        if status == 404:
            bodies_404.append(r.json())
    unknown = _call(api.as_user(matrix["owner"]), method, uuid.uuid4(), suffix, kind, matrix["ids"])
    assert unknown.status_code == 404 and unknown.json() in bodies_404
    non_canonical = _call(api.as_user(matrix["owner"]), method, str(matrix["course"]).upper(), suffix, kind, matrix["ids"])
    assert non_canonical.status_code == 404 and non_canonical.json() == bodies_404[0]
    assert all(b == bodies_404[0] for b in bodies_404)
    owner = _call(api.as_user(matrix["owner"]), method, matrix["course"], suffix, kind, matrix["ids"])
    assert owner.status_code not in (401, 403), owner.text
    if owner.headers.get("content-type", "").startswith("application/json"):
        assert owner.json().get("code") != "course_not_found"


@pytest.mark.parametrize("method,suffix,kind", ENDPOINTS)
def test_reg02_auth03_switches_default_off_and_checked_after_authorization(matrix, method, suffix, kind):
    api = matrix["make_api"]()  # default settings: every M9 switch off
    r = _call(api.as_user(matrix["owner"]), method, matrix["course"], suffix, kind, matrix["ids"])
    assert r.status_code == 503 and r.json()["code"] == "exams_unavailable" and r.headers["cache-control"] == "no-store"
    for name, status in (("student", 403), ("other_professor", 404), ("anonymous", 401)):
        r = _call(api.as_user(matrix["callers"][name]), method, matrix["course"], suffix, kind, matrix["ids"])
        assert r.status_code == status, name
    if "export" in suffix or "preview-file" in suffix:
        exams_only = matrix["make_api"](settings_overrides=ex.ENABLED).as_user(matrix["owner"])
        r = _call(exams_only, method, matrix["course"], suffix, kind, matrix["ids"])
        assert r.status_code == 503 and r.json()["code"] == "exam_export_unavailable"


def test_auth03_body_is_never_read_before_authorization(matrix):
    api = matrix["make_api"](settings_overrides=ex.ENABLED)
    payload = json.dumps(mcq_body()).encode()
    for user in (matrix["callers"]["student"], matrix["callers"]["other_professor"], matrix["callers"]["anonymous"]):
        api.as_user(user)
        result = asgi_call(api.app, "POST", f"/api/v1/courses/{matrix['course']}/exam-questions",
                           {"content-type": "application/json", "content-length": str(len(payload)), **key()}, [payload])  # fmt: skip
        assert result.status in (401, 403, 404) and result.receive_calls == 0
    closed = matrix["make_api"]().as_user(matrix["owner"])
    result = asgi_call(closed.app, "POST", f"/api/v1/courses/{matrix['course']}/exam-questions",
                       {"content-type": "application/json", "content-length": str(len(payload)), **key()}, [payload])  # fmt: skip
    assert result.status == 503 and result.receive_calls == 0


def test_auth04_revocation_between_the_write_and_the_delivery(matrix, monkeypatch):
    world = matrix["world"]
    api = matrix["make_api"](settings_overrides=ex.ENABLED).as_user(matrix["owner"])
    x = ExamClient(api, matrix["course"])
    original = bank.deliver
    successor = world.user("professor")

    def transfer_then_deliver(*args, **kwargs):
        world.execute("UPDATE courses SET owner_id = :p WHERE id = :c", p=successor.id, c=matrix["course"])
        return original(*args, **kwargs)

    monkeypatch.setattr(bank, "deliver", transfer_then_deliver)
    r = api.client.post(x.q_path(), json=mcq_body(stem="Secret stem SENTINEL-AUTH04?"), headers=key())
    assert r.status_code == 404 and "SENTINEL-AUTH04" not in r.text
    world.execute("UPDATE courses SET owner_id = :p WHERE id = :c", p=matrix["owner"].id, c=matrix["course"])
    monkeypatch.undo()
    disabled_after = ExamClient(api, matrix["course"])
    q = disabled_after.question(mcq_body(stem="Visible?"))
    world.execute("UPDATE users SET status = 'disabled' WHERE id = :u", u=matrix["owner"].id)
    r = api.client.get(x.q_path(q["id"]))
    assert r.status_code == 403 and r.json()["code"] == "account_disabled"
    world.execute("UPDATE users SET status = 'active' WHERE id = :u", u=matrix["owner"].id)


def test_auth05_ownership_change_waits_for_the_write(matrix):
    import threading

    from sqlalchemy import text

    world = matrix["world"]
    api = matrix["make_api"](settings_overrides=ex.ENABLED).as_user(matrix["owner"])
    x = ExamClient(api, matrix["course"])
    exam = x.exam("Locked")
    successor = world.user("professor")
    with world.engine.connect() as writer:
        transaction = writer.begin()
        writer.execute(text("SELECT 1 FROM courses WHERE id = :c FOR SHARE"), {"c": matrix["course"]})  # as a write would
        done = threading.Event()

        def transfer():
            with world.engine.begin() as c:
                c.execute(text("SET LOCAL lock_timeout = '10s'"))
                c.execute(text("UPDATE courses SET owner_id = :p WHERE id = :c"), {"p": successor.id, "c": matrix["course"]})
            done.set()

        thread = threading.Thread(target=transfer, daemon=True)
        thread.start()
        assert not done.wait(0.7), "the ownership change must wait for the FOR SHARE lock"
        transaction.commit()
        thread.join(10)
    assert done.is_set()
    r = api.client.get(x.e_path(exam["id"]))
    assert r.status_code == 404 and r.json()["code"] == "course_not_found"
    world.execute("UPDATE courses SET owner_id = :p WHERE id = :c", p=matrix["owner"].id, c=matrix["course"])


def test_reg02_reg03_reg04_settings():
    s = Settings()
    assert (s.exams_enabled, s.exam_export_enabled, s.exam_generation_enabled) == (False, False, False)
    assert (s.quiz_enabled, s.analytics_enabled, s.study_events_enabled, s.mastery_delivery_enabled,
            s.ai_recommendations_enabled, s.dashboard_enabled) == (False, False, False, False, False, True)  # fmt: skip
    assert s.exam_export_max_content_chars == 300_000 and s.exam_export_timeout_seconds == 30
    assert (s.exam_export_cpu_seconds, s.exam_export_memory_mb) == (25, 512)
    for bad in ({"exam_generation_enabled": True}, {"exam_export_enabled": True},
                {"exams_enabled": True, "exam_export_cpu_seconds": 30, "exam_export_timeout_seconds": 30},
                {"exam_export_max_content_chars": 300_001}, {"exam_max_items": 201}):  # fmt: skip
        with pytest.raises(ValidationError):
            Settings(**bad)
    assert Settings(exams_enabled=True, exam_export_enabled=True).exam_export_enabled is True
