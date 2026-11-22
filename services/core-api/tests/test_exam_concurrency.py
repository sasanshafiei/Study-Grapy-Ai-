"""Races, lock waits, idempotent concurrency and rollback on faults, through the API on real PostgreSQL.

Interleavings are forced, not hoped for: a barrier is installed inside the first transaction (after it took
its locks) and the competing request runs on its own application instance and connection while it waits.
CONC-02/FP-06 (finalize vs question edit, both orders), CONC-03, CONC-04, CONC-05, CONC-06, BP-10, TX-01..03.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text

from app.exams import audit, bank, builder, finalize, idempotency
from tests import exam_support as ex
from tests.exam_support import ExamClient, key, match, mcq_body, row, tf_body
from tests.worker_support import wait_for


@pytest.fixture
def two(world, make_api):
    professor = world.user("professor")
    course = world.course(professor, code="CHEM 101")
    a = make_api(settings_overrides=ex.ENABLED).as_user(professor)
    b = make_api(settings_overrides=ex.ENABLED).as_user(professor)
    return {"world": world, "a": ExamClient(a, course), "b": ExamClient(b, course), "course": course, "professor": professor}


class Gate:
    """Pause the first caller of a patched function (after it ran) until released."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def wrap(self, original):
        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            self.calls += 1
            if self.calls == 1:
                self.reached.set()
                assert self.release.wait(10)
            return result

        return wrapped


def _in_thread(fn):
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def _pinned_revisions(world, exam_id) -> list:
    return [r["question_revision_no"] for r in world.rows(
        "SELECT question_revision_no FROM exam_items WHERE exam_id = :e ORDER BY position", e=uuid.UUID(exam_id))]  # fmt: skip


def test_conc02_fp06_finalize_first_pins_the_reviewed_revision(two, monkeypatch):
    a, b, world = two["a"], two["b"], two["world"]
    q = a.question(mcq_body(), approve=True)
    exam = a.add(a.exam(), q)
    fingerprint = a.preview(exam)["preview_fingerprint"]
    gate = Gate()
    monkeypatch.setattr(finalize, "_finalize_locked", gate.wrap(finalize._finalize_locked))
    thread, out = _in_thread(lambda: a.finalize(exam, fingerprint=fingerprint))
    assert gate.reached.wait(10)  # finalize holds exam FOR UPDATE and the question FOR SHARE
    edit_thread, edit = _in_thread(lambda: b.c.put(b.q_path(q["id"]), json=mcq_body(stem="Edited?"), headers=match(q["version"])))
    edit_thread.join(0.7)
    assert edit_thread.is_alive(), "the edit must wait for the finalization's FOR SHARE lock"
    gate.release.set()
    thread.join(10)
    edit_thread.join(10)
    assert out["value"].status_code == 200 and edit["value"].status_code == 200
    assert _pinned_revisions(world, exam["id"]) == [1]
    final = a.get(exam)
    assert final["items"][0]["question"]["stem"] == q["stem"] and final["items"][0]["question_status"] == "draft"


def test_conc02_fp06_edit_first_makes_the_finalize_stale(two, monkeypatch):
    a, b, world = two["a"], two["b"], two["world"]
    q = a.question(mcq_body(), approve=True)
    exam = a.add(a.exam(), q)
    fingerprint = a.preview(exam)["preview_fingerprint"]
    gate = Gate()
    monkeypatch.setattr(bank, "check_references", gate.wrap(bank.check_references))
    edit_thread, edit = _in_thread(lambda: b.c.put(b.q_path(q["id"]), json=mcq_body(stem="Edited?"), headers=match(q["version"])))
    assert gate.reached.wait(10)  # the edit holds the question FOR UPDATE
    thread, out = _in_thread(lambda: a.finalize(exam, fingerprint=fingerprint))
    thread.join(0.7)
    assert thread.is_alive(), "finalize must wait for the edit's row lock"
    gate.release.set()
    edit_thread.join(10)
    thread.join(10)
    assert edit["value"].status_code == 200
    assert out["value"].status_code == 412 and out["value"].json()["code"] == "exam_preview_stale"
    assert _pinned_revisions(world, exam["id"]) == [None] and a.get(exam)["status"] == "draft"


def test_conc03_two_identical_finalizes(two, monkeypatch):
    a, b = two["a"], two["b"]
    exam = a.add(a.exam(), a.question(mcq_body(), approve=True))
    fingerprint = a.preview(exam)["preview_fingerprint"]
    gate = Gate()
    monkeypatch.setattr(finalize, "_finalize_locked", gate.wrap(finalize._finalize_locked))
    thread, out = _in_thread(lambda: a.finalize(exam, fingerprint=fingerprint))
    assert gate.reached.wait(10)
    second_thread, second = _in_thread(lambda: b.finalize(exam, fingerprint=fingerprint))
    second_thread.join(0.7)
    assert second_thread.is_alive()
    gate.release.set()
    thread.join(10)
    second_thread.join(10)
    assert out["value"].status_code == 200 and second["value"].status_code == 200
    assert second["value"].headers["idempotent-replayed"] == "true"
    stale = b.finalize(exam, fingerprint=fingerprint, version=exam["version"] - 1)
    assert stale.status_code == 412


@pytest.mark.parametrize("competitor", ["add_item", "update_item", "remove_item", "reorder"])
def test_bp10_blueprint_replacement_vs_item_mutation(two, monkeypatch, competitor):
    a, b, world = two["a"], two["b"], two["world"]
    q1, q2, q3 = (a.question(mcq_body(stem=f"R{i}?")) for i in range(3))
    exam = a.blueprint(a.exam(), [row(1), row(1)])["exam"]
    r1, r2 = (r["id"] for r in exam["blueprint"]["rows"])
    exam = a.add(a.add(exam, q1, row=r1), q2, row=r2)
    items = exam["items"]
    gate = Gate()
    monkeypatch.setattr(builder, "bump", gate.wrap(builder.bump))
    thread, out = _in_thread(lambda: a.c.put(a.e_path(exam["id"], "/blueprint"), headers=match(exam["version"]),
                                             json={"rows": [row(1, id=r2), row(1, id=r1)]}))  # fmt: skip
    assert gate.reached.wait(10)
    v = match(exam["version"])
    calls = {
        "add_item": lambda: b.c.post(b.e_path(exam["id"], "/items"), headers=v, json={"question_id": q3["id"], "points": 1,
                                                                                         "position": 1, "blueprint_row_id": None}),
        "update_item": lambda: b.c.patch(b.e_path(exam["id"], f"/items/{items[0]['id']}"), json={"blueprint_row_id": None}, headers=v),
        "remove_item": lambda: b.c.delete(b.e_path(exam["id"], f"/items/{items[0]['id']}"), headers=v),
        "reorder": lambda: b.c.put(b.e_path(exam["id"], "/items/order"), json={"item_ids": [items[1]["id"], items[0]["id"]]}, headers=v),
    }  # fmt: skip
    other_thread, other = _in_thread(calls[competitor])
    other_thread.join(0.7)
    assert other_thread.is_alive(), "the item mutation must wait for the exam row lock"
    gate.release.set()
    thread.join(10)
    other_thread.join(10)
    assert out["value"].status_code == 200
    assert other["value"].status_code == 412 and other["value"].json()["code"] == "version_mismatch"
    final = a.get(exam)
    assert final["version"] == exam["version"] + 1 and [r["id"] for r in final["blueprint"]["rows"]] == [r2, r1]
    assert sorted(i["position"] for i in final["items"]) == [1, 2]
    assert world.scalar("SELECT count(*) FROM exam_items i WHERE i.blueprint_row_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
                        "exam_blueprint_rows r WHERE r.id = i.blueprint_row_id AND r.exam_id = i.exam_id)") == 0  # fmt: skip


def test_bp10_different_exams_do_not_interfere(two, monkeypatch):
    a, b = two["a"], two["b"]
    one = a.blueprint(a.exam("One"), [row(1)])["exam"]
    other = a.blueprint(a.exam("Two"), [row(1)])["exam"]
    gate = Gate()
    monkeypatch.setattr(builder, "bump", gate.wrap(builder.bump))
    thread, out = _in_thread(lambda: a.c.put(a.e_path(one["id"], "/blueprint"), headers=match(one["version"]), json={"rows": [row(2)]}))
    assert gate.reached.wait(10)
    r = b.c.put(b.e_path(other["id"], "/blueprint"), headers=match(other["version"]), json={"rows": [row(3)]})
    assert r.status_code == 200  # did not wait for the other exam's lock
    gate.release.set()
    thread.join(10)
    assert out["value"].status_code == 200


@pytest.mark.parametrize("change", ["difficulty", "topic", "type", "bloom_level", "lose_approval", "still_matches"])
def test_a02_fill_rechecks_the_selected_question_under_lock(two, change):
    a, b, world = two["a"], two["b"], two["world"]
    topic = str(world.topic(two["course"], "Moles"))
    criteria = {"topic_id": topic, "difficulty": "medium", "bloom_level": "apply"}
    candidate = a.question(mcq_body(**criteria), approve=True)
    stable = a.question(mcq_body(stem="Stable match?", **criteria), approve=True)
    exam = a.blueprint(a.exam(), [row(2, type="mcq", **criteria)])["exam"]
    row_id = exam["blueprint"]["rows"][0]["id"]
    changed = mcq_body(stem="Revised question?", **criteria)
    if change == "difficulty":
        changed["difficulty"] = "hard"
    elif change == "topic":
        changed["topic_id"] = None
    elif change == "type":
        changed = tf_body(**criteria)
    elif change == "bloom_level":
        changed["bloom_level"] = "analyze"

    selected = []

    def edit_before_question_lock(query):
        sql = str(query.statement)
        if "FROM questions " in sql and "FOR SHARE" in sql and "ids" in (query.parameters or {}):
            selected.extend(str(qid) for qid in query.parameters["ids"])
            # The fill has its candidate IDs. A separate API instance commits the edit and optional
            # approval before the first instance sends its locking SELECT to PostgreSQL.
            edited = b.edit(candidate, changed)
            assert edited["status"] == "draft"
            if change != "lose_approval":
                assert b.approve(edited)["approved_revision"] == 2

    event.listen(a.api.session_factory, "do_orm_execute", edit_before_question_lock)
    try:
        response = a.c.post(a.e_path(exam["id"], "/fill-from-blueprint"),
                            json={"row_ids": [row_id]}, headers=match(exam["version"]))
    finally:
        event.remove(a.api.session_factory, "do_orm_execute", edit_before_question_lock)
    assert response.status_code == 200, response.text
    assert selected == [candidate["id"], stable["id"]]
    result = response.json()
    filled_exam = result["exam"]
    stored = world.rows("SELECT question_id FROM exam_items WHERE exam_id = :e ORDER BY position", e=uuid.UUID(exam["id"]))
    stored_ids = [str(item["question_id"]) for item in stored]
    assert [i["question_id"] for i in filled_exam["items"]] == stored_ids
    assert result["filled"][0]["added"] == len(stored_ids)
    expected_ids = selected if change == "still_matches" else [stable["id"]]
    added = len(expected_ids)
    assert result["filled"] == [{"blueprint_row_id": row_id, "added": added, "missing": 2 - added}]
    assert stored_ids == expected_ids
    assert filled_exam["version"] == exam["version"] + 1
    if change == "still_matches":
        final = a.finalized(filled_exam)
        assert final["blueprint_status"] == "fulfilled"
        assert [i["pinned_revision"] for i in final["items"]] == [2, 1]
    else:
        assert [i["code"] for i in filled_exam["readiness"]["issues"]] == ["blueprint_row_underfilled"]
        refused = a.finalize(filled_exam)
        assert refused.status_code == 409 and refused.json()["code"] == "exam_not_ready"


def test_a02_concurrent_fills_share_one_exam_version_and_never_reuse_a_question(two, monkeypatch):
    a, b, world = two["a"], two["b"], two["world"]
    other_course = world.course(two["professor"])
    ExamClient(b.api, other_course).question(approve=True)  # older eligible content in another course
    local = a.question(approve=True)
    exam = a.blueprint(a.exam(), [row(1), row(1)])["exam"]
    eid = uuid.UUID(exam["id"])
    path = a.e_path(eid, "/fill-from-blueprint")
    both_ready = threading.Barrier(2)
    lock_exam = builder.lock_exam

    def contend_for_exam(*args, **kwargs):
        both_ready.wait(timeout=5)
        return lock_exam(*args, **kwargs)

    monkeypatch.setattr(builder, "lock_exam", contend_for_exam)
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(client.c.post, path, json={"row_ids": None}, headers=match(exam["version"]))
                    for client in (a, b)]
        responses = [request.result(timeout=10) for request in requests]
    winner, stale = sorted(responses, key=lambda r: r.status_code)
    assert winner.status_code == 200 and stale.status_code == 412, [r.text for r in responses]
    assert stale.json()["code"] == "version_mismatch"
    result = winner.json()
    assert result["exam"]["version"] == stale.json()["current_version"] == exam["version"] + 1
    assert [(entry["added"], entry["missing"]) for entry in result["filled"]] == [(1, 0), (0, 1)]
    assert world.rows("SELECT question_id FROM exam_items WHERE exam_id = :e", e=eid) == [
        {"question_id": uuid.UUID(local["id"])},
    ]
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'exam_filled_from_blueprint'",
                        e=eid) == 1


def test_conc04_retire_vs_add_and_add_vs_finalize(two, monkeypatch):
    a, b = two["a"], two["b"]
    q = a.question(mcq_body(), approve=True)
    exam = a.exam()
    gate = Gate()
    monkeypatch.setattr(audit, "record", gate.wrap(audit.record))
    thread, out = _in_thread(lambda: a.c.post(a.q_path(q["id"], "/retire"), headers=match(q["version"])))
    assert gate.reached.wait(10)  # the retirement holds the question FOR UPDATE
    add_thread, added = _in_thread(lambda: b.c.post(b.e_path(exam["id"], "/items"), headers=match(exam["version"]),
                                                    json={"question_id": q["id"], "points": 1, "position": None,
                                                          "blueprint_row_id": None}))  # fmt: skip
    add_thread.join(0.7)
    assert add_thread.is_alive()
    gate.release.set()
    thread.join(10)
    add_thread.join(10)
    assert out["value"].status_code == 200
    assert added["value"].status_code == 409 and added["value"].json()["code"] == "question_retired"


def test_conc05_concurrent_creates_with_one_key_make_one_resource(two, monkeypatch):
    a, b, world = two["a"], two["b"], two["world"]
    headers = key()
    gate = Gate()
    monkeypatch.setattr(idempotency, "store", gate.wrap(idempotency.store))
    thread, out = _in_thread(lambda: a.c.post(a.e_path(), json={"title": "T", "instructions": None, "duration_minutes": None},
                                              headers=headers))  # fmt: skip
    assert gate.reached.wait(10)
    second_thread, second = _in_thread(lambda: b.c.post(b.e_path(), json={"title": "T", "instructions": None,
                                                                          "duration_minutes": None}, headers=headers))  # fmt: skip
    second_thread.join(0.7)
    assert second_thread.is_alive(), "the second create waits for the first transaction to commit"
    gate.release.set()
    thread.join(10)
    second_thread.join(10)
    assert out["value"].status_code == 201 and second["value"].status_code == 200
    assert second["value"].headers["idempotent-replayed"] == "true" and second["value"].json()["id"] == out["value"].json()["id"]
    assert world.scalar("SELECT count(*) FROM exams WHERE course_id = :c", c=two["course"]) == 1


@pytest.mark.parametrize("operation", ["exam", "question", "duplicate"])
@pytest.mark.parametrize("competitor", ["replay", "different_key", "different_payload"])
def test_f06_creates_compete_for_the_last_course_slot(world, make_api, operation, competitor):
    owner = world.user("professor")
    course = world.course(owner)
    exam_limit = 2 if operation == "duplicate" else 1
    settings = {**ex.ENABLED, "exam_max_exams": exam_limit, "exam_max_bank_questions": 1}
    a, b = (ExamClient(make_api(settings_overrides=settings).as_user(owner), course) for _ in range(2))
    if operation == "question":
        path, body, change = a.q_path(), mcq_body(), {"stem": "Different question?"}
        lock, limit_error, action = "exam-bank-limit", "question_limit_reached", "question_created"
    else:
        lock, limit_error = "exam-limit", "exam_limit_reached"
        body, change = {"title": "Midterm"}, {"title": "Different exam"}
        if operation == "duplicate":
            with world.engine.begin() as conn:
                source = ex.exam(conn, course, owner.id)
                question = ex.bank_question(conn, course, owner.id)
                blueprint = ex.blueprint_row(conn, source, course, 1)
                ex.item(conn, source, course, question, 1, row=blueprint)
            path, action = a.e_path(source, "/duplicate"), "exam_duplicated"
        else:
            path, action = a.e_path(), "exam_created"
            body.update(instructions=None, duration_minutes=None)

    headers = key()
    other_headers = key() if competitor == "different_key" else headers
    other_body = {**body, **change} if competitor == "different_payload" else body
    with ThreadPoolExecutor(max_workers=2) as pool:
        with world.engine.begin() as holder:
            holder.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{lock}:{course}"})
            pid = holder.scalar(text("SELECT pg_backend_pid()"))
            first = pool.submit(a.c.post, path, json=body, headers=headers)
            second = pool.submit(b.c.post, path, json=other_body, headers=other_headers)
            # Release only after PostgreSQL confirms both requests are waiting on the capacity lock.
            wait_for(lambda: world.scalar("SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))",
                                          pid=pid) == 2, timeout=5, interval=0.02, what="both capacity lock waiters")
        responses = [first.result(timeout=10), second.result(timeout=10)]

    assert sum(r.status_code == 201 for r in responses) == 1, [r.text for r in responses]
    created = next(r for r in responses if r.status_code == 201)
    other = next(r for r in responses if r is not created)
    resource = created.json()["exam"] if operation == "duplicate" else created.json()
    created_id = uuid.UUID(resource["id"])
    assert created.headers["location"].endswith(f"/{created_id}")
    if competitor == "replay":
        assert other.status_code == 200, other.text
        assert other.headers["idempotent-replayed"] == "true" and other.json() == created.json()
    else:
        expected = limit_error if competitor == "different_key" else "idempotency_key_reused"
        assert other.status_code == 409 and other.json()["code"] == expected, other.text

    if operation == "question":
        assert world.scalar("SELECT count(*) FROM questions WHERE course_id = :c AND version IS NOT NULL", c=course) == 1
    else:
        assert world.scalar("SELECT count(*) FROM exams WHERE course_id = :c", c=course) == exam_limit
    if operation == "duplicate":
        assert len(resource["items"]) == len(resource["blueprint"]["rows"]) == len(created.json()["duplicate_report"]) == 1
    assert world.rows("SELECT action, entity_id FROM exam_audit_events WHERE course_id = :c", c=course) == [
        {"action": action, "entity_id": created_id},
    ]
    assert world.rows("SELECT resource_id FROM exam_idempotency_keys WHERE course_id = :c", c=course) == [
        {"resource_id": created_id},
    ]


def test_f06_same_user_key_racing_in_two_owned_courses_rolls_back_the_loser(two, monkeypatch):
    world, a = two["world"], two["a"]
    b = ExamClient(two["b"].api, world.course(two["professor"]))
    both_inserting = threading.Barrier(2)
    store = idempotency.store

    def store_together(*args):
        both_inserting.wait(timeout=5)
        return store(*args)

    monkeypatch.setattr(idempotency, "store", store_together)
    headers = key()
    body = {"title": "Midterm", "instructions": None, "duration_minutes": None}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.c.post, client.e_path(), json=body, headers=headers)
                   for client in (a, b)]
        responses = [f.result(timeout=10) for f in futures]
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    for client, response in zip((a, b), responses):
        expected = int(response.status_code == 201)
        if not expected:
            assert response.json()["code"] == "idempotency_key_reused"
        for table in ("exams", "exam_idempotency_keys", "exam_audit_events"):
            assert world.scalar(f"SELECT count(*) FROM {table} WHERE course_id = :c", c=client.course) == expected


def test_conc06_lock_timeout_is_503_and_writes_nothing(two):
    a, world = two["a"], two["world"]
    exam = a.exam()
    with world.engine.connect() as holder:
        transaction = holder.begin()
        holder.execute(text("SELECT 1 FROM exams WHERE id = :e FOR UPDATE"), {"e": uuid.UUID(exam["id"])})
        r = a.c.patch(a.e_path(exam["id"]), json={"title": "Blocked"}, headers=match(1))
        transaction.rollback()
    assert r.status_code == 503 and r.json()["code"] == "exam_busy" and r.headers["retry-after"] == "2"
    assert r.headers["cache-control"] == "no-store"
    assert a.get(exam)["title"] == "Midterm 1" and a.get(exam)["version"] == 1
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'exam_updated'",
                        e=uuid.UUID(exam["id"])) == 0


@pytest.fixture
def response_read_timeout(world):
    """Force PostgreSQL to cancel a statement inside the real read-only delivery transaction."""
    with world.engine.begin() as holder:
        lock_id = holder.scalar(text("SELECT pg_backend_pid()"))
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_id})

        def timeout(session, *args, **kwargs):
            assert session.scalar(text("SHOW transaction_read_only")) == "on"
            session.execute(text("SET LOCAL statement_timeout = '50ms'"))
            session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_id})
            pytest.fail("the response query acquired the held lock")

        yield timeout


def _assert_response_unavailable(response):
    assert response.status_code == 503 and response.json()["code"] == "exam_response_unavailable", response.text
    assert set(response.json()) == {"code", "detail"}
    assert "may already be saved" in response.json()["detail"]
    assert response.headers["cache-control"] == "no-store" and response.headers["pragma"] == "no-cache"
    assert not {"location", "etag", "retry-after", "idempotent-replayed"}.intersection(response.headers)


@pytest.mark.parametrize("changed", [True, False], ids=["committed", "no_op"])
def test_f14_update_response_timeout_and_reconciliation(two, monkeypatch, response_read_timeout, changed):
    x, world = two["a"], two["world"]
    exam = x.exam()
    eid = uuid.UUID(exam["id"])
    title = "Private revised title" if changed else exam["title"]
    with monkeypatch.context() as patch:
        patch.setattr(builder.snap, "load_exam", response_read_timeout)
        r = x.c.patch(x.e_path(eid), json={"title": title}, headers=match(1))
        read = x.c.get(x.e_path(eid))
    version = 2 if changed else 1
    assert world.rows("SELECT title, version FROM exams WHERE id = :e", e=eid) == [{"title": title, "version": version}]
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'exam_updated'",
                        e=eid) == int(changed)
    _assert_response_unavailable(r)
    assert title not in r.text and str(eid) not in r.text
    assert read.status_code == 503 and read.json()["code"] == "exam_busy" and read.headers["retry-after"] == "2"
    current = x.get(exam)
    assert (current["title"], current["version"]) == (title, version)
    if changed:
        stale = x.c.patch(x.e_path(eid), json={"title": title}, headers=match(1))
        assert stale.status_code == 412 and stale.json()["code"] == "version_mismatch"
        assert stale.json()["current_version"] == version


@pytest.mark.parametrize("operation", ["question", "exam", "duplicate"])
def test_f14_create_response_timeout_replays_once(two, monkeypatch, response_read_timeout, operation):
    x, world = two["a"], two["world"]
    if operation == "question":
        path, body = x.q_path(), mcq_body(stem="Private question?")
        module, reader, action = bank, "question_view", "question_created"
    elif operation == "exam":
        path, body = x.e_path(), {"title": "Private exam", "instructions": None, "duration_minutes": None}
        module, reader, action = builder.snap, "load_exam", "exam_created"
    else:
        path, body = x.e_path(x.exam()["id"], "/duplicate"), {"title": "Private copy"}
        module, reader, action = finalize, "duplicate_report", "exam_duplicated"
    headers = key()
    with monkeypatch.context() as patch:
        patch.setattr(module, reader, response_read_timeout)
        created = x.c.post(path, json=body, headers=headers)
        replay = x.c.post(path, json=body, headers=headers)
    resource_id = world.scalar("SELECT resource_id FROM exam_idempotency_keys WHERE user_id = :u AND idempotency_key = :k",
                               u=two["professor"].id, k=uuid.UUID(headers["Idempotency-Key"]))
    assert resource_id is not None
    recovered = x.c.post(path, json=body, headers=headers)
    assert recovered.status_code == 200 and recovered.headers["idempotent-replayed"] == "true"
    resource = recovered.json()["exam"] if operation == "duplicate" else recovered.json()
    assert resource["id"] == str(resource_id)
    table = "questions" if operation == "question" else "exams"
    expected_count = 2 if operation == "duplicate" else 1
    assert world.scalar(f"SELECT count(*) FROM {table} WHERE course_id = :c", c=two["course"]) == expected_count
    assert world.rows("SELECT action FROM exam_audit_events WHERE entity_id = :r", r=resource_id) == [{"action": action}]
    for response in (created, replay):
        _assert_response_unavailable(response)
        assert str(resource_id) not in response.text and "Private" not in response.text


@pytest.mark.parametrize("fault", ["timeout", "unexpected_db_error"])
def test_f14_finalize_reconciles_with_original_intent(two, monkeypatch, response_read_timeout, fault):
    x, world = two["a"], two["world"]
    exam = x.add(x.exam(), x.question(approve=True))
    fingerprint = x.preview(exam)["preview_fingerprint"]
    authorize = builder.require_exam_owner

    def fail_response_read(session, user, course_id):
        authorize(session, user, course_id)
        if fault == "timeout":
            return response_read_timeout(session)
        session.execute(text("SELECT 1 / 0"))

    # Finalization does its write under finalize.require_exam_owner, then calls builder's fresh exam read.
    with monkeypatch.context() as patch:
        patch.setattr(builder, "require_exam_owner", fail_response_read)
        failed = x.finalize(exam, fingerprint=fingerprint)
    if fault == "timeout":
        _assert_response_unavailable(failed)
    else:
        assert failed.status_code == 500 and failed.json()["code"] == "server_error"
    replay = x.finalize(exam, fingerprint=fingerprint)
    assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
    assert (replay.json()["status"], replay.json()["version"]) == ("finalized", exam["version"] + 1)
    conflict = x.finalize(exam, fingerprint=fingerprint, reason="A different reason")
    assert conflict.status_code == 409 and conflict.json()["code"] == "finalize_intent_mismatch"
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'exam_finalized'",
                        e=uuid.UUID(exam["id"])) == 1


def test_tx01_fault_after_pinning_rolls_everything_back(two, monkeypatch):
    a, world = two["a"], two["world"]
    exam = a.add(a.add(a.exam(), a.question(mcq_body(), approve=True)), a.question(tf_body(), approve=True))

    def boom(*args, **kwargs):
        raise RuntimeError("injected fault after pinning")

    from app.exams import snapshot

    monkeypatch.setattr(snapshot, "content_sha256", boom)
    r = a.finalize(exam)
    assert r.status_code == 500 and r.json()["code"] == "server_error"
    assert _pinned_revisions(world, exam["id"]) == [None, None] and a.get(exam)["status"] == "draft"
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'exam_finalized'",
                        e=uuid.UUID(exam["id"])) == 0  # fmt: skip


def test_tx02_audit_failure_rolls_back_the_mutation(two, monkeypatch):
    a = two["a"]
    exam = a.exam()

    def boom(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit, "record", boom)
    r = a.c.patch(a.e_path(exam["id"]), json={"title": "Changed"}, headers=match(1))
    assert r.status_code == 500
    monkeypatch.undo()
    assert a.get(exam)["title"] == "Midterm 1" and a.get(exam)["version"] == 1
    created = a.c.post(a.e_path(), json={"title": "X", "instructions": None, "duration_minutes": None}, headers=key())
    assert created.status_code == 201


def test_tx03_revision_failure_leaves_the_question_draft(two, monkeypatch):
    a, world = two["a"], two["world"]
    document = world.document(two["course"], title="L.pdf", page_count=9)
    q = a.question(mcq_body(source_refs=[{"document_id": str(document), "locator": {"page": 1}}]))
    original = bank._frozen_refs

    def tampered(session, course_id, row):
        refs = original(session, course_id, row)
        refs[0]["locator"] = {"page": 2}  # not a copy of the working copy: the revision trigger refuses it
        return refs

    monkeypatch.setattr(bank, "_frozen_refs", tampered)
    r = a.c.post(a.q_path(q["id"], "/approve"), headers=match(1))
    assert r.status_code == 500
    after = a.c.get(a.q_path(q["id"])).json()
    assert (after["status"], after["version"], after["approved_revision"]) == ("draft", 1, None)
    assert world.scalar("SELECT count(*) FROM exam_question_revisions WHERE question_id = :q", q=uuid.UUID(q["id"])) == 0
