"""Races, lock waits, idempotent concurrency and rollback on faults, through the API on real PostgreSQL.

Interleavings are forced, not hoped for: a barrier is installed inside the first transaction (after it took
its locks) and the competing request runs on its own application instance and connection while it waits.
CONC-02/FP-06 (finalize vs question edit, both orders), CONC-03, CONC-04, CONC-05, CONC-06, BP-10, TX-01..03.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from app.exams import audit, bank, builder, finalize, idempotency
from tests import exam_support as ex
from tests.exam_support import ExamClient, key, match, mcq_body, row, tf_body


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
    assert second_thread.is_alive(), "the second insert waits on the key"
    gate.release.set()
    thread.join(10)
    second_thread.join(10)
    assert out["value"].status_code == 201 and second["value"].status_code == 200
    assert second["value"].headers["idempotent-replayed"] == "true" and second["value"].json()["id"] == out["value"].json()["id"]
    assert world.scalar("SELECT count(*) FROM exams WHERE course_id = :c", c=two["course"]) == 1


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
