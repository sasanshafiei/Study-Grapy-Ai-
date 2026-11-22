"""Exam drafts, blueprint replacement, items, order and fill (E8-E17) on real PostgreSQL.

BP-06 ... BP-09 and BP-11 (V3 blueprint replacement), VAL-02/VAL-08 (bounds, export budget), CUR-01 for exams.
"""

from __future__ import annotations

import uuid

import pytest

from tests import exam_support as ex
from tests.exam_support import ExamClient, key, match, mcq_body, row, short_body, tf_body


@pytest.fixture
def ws(world, make_api):
    professor = world.user("professor", name="Prof. Ada")
    course = world.course(professor, code="CHEM 101")
    topic = world.topic(course, "Stoichiometry")
    other_topic = world.topic(world.course(professor), "Elsewhere")
    api = make_api(settings_overrides=ex.ENABLED).as_user(professor)
    return {"world": world, "api": api, "x": ExamClient(api, course), "c": api.client, "course": course, "topic": topic,
            "other_topic": other_topic, "professor": professor}  # fmt: skip


def _foreign_rows(world) -> int:
    return world.scalar(
        "SELECT count(*) FROM exam_items i WHERE i.blueprint_row_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM exam_blueprint_rows r WHERE r.id = i.blueprint_row_id AND r.exam_id = i.exam_id)"
    )


def test_create_list_patch_and_validation(ws):
    x, c = ws["x"], ws["c"]
    exam = x.exam()
    assert (exam["status"], exam["version"], exam["items"], exam["readiness"]["ready"]) == ("draft", 1, [], False)
    assert [i["code"] for i in exam["readiness"]["issues"]] == ["no_items"]
    assert exam["readiness"]["preview_fingerprint"].startswith("pf1:") and len(exam["readiness"]["preview_fingerprint"]) == 68
    assert exam["export_budget"] == 300000 and exam["content_chars"] == len("CHEM 101General ChemistryMidterm 1Answer all questions.")
    for body, field in (({"title": "", "instructions": None, "duration_minutes": None}, "title"),
                        ({"title": "a\nb", "instructions": None, "duration_minutes": None}, "title"),
                        ({"title": "t" * 201, "instructions": None, "duration_minutes": None}, "title"),
                        ({"title": "T", "instructions": "i" * 4001, "duration_minutes": None}, "instructions"),
                        ({"title": "T", "instructions": None, "duration_minutes": 0}, "duration_minutes"),
                        ({"title": "T", "instructions": None, "duration_minutes": 601}, "duration_minutes"),
                        ({"title": "T", "instructions": None, "duration_minutes": True}, "duration_minutes")):  # fmt: skip
        r = c.post(x.e_path(), json=body, headers=key())
        assert r.status_code == 422 and r.json() == {"detail": r.json()["detail"], "code": "invalid_exam", "field": field}
    r = c.patch(x.e_path(exam["id"]), json={"title": "Midterm 1 — Gases"}, headers=match(1))
    assert r.status_code == 200 and r.json()["title"] == "Midterm 1 — Gases" and r.json()["version"] == 2
    same = c.patch(x.e_path(exam["id"]), json={"title": "Midterm 1 — Gases"}, headers=match(2))
    assert same.status_code == 200 and same.json()["version"] == 2
    for body in ({}, {"status": "finalized"}):
        r = c.patch(x.e_path(exam["id"]), json=body, headers=match(2))
        assert r.status_code == 422 and r.json()["code"] == "invalid_fields"
    second = x.exam("Final")
    listed = c.get(x.e_path(), params={"limit": 1}).json()
    assert [i["id"] for i in listed["items"]] == [second["id"]] and listed["next_cursor"]
    rest = c.get(x.e_path(), params={"limit": 1, "cursor": listed["next_cursor"]}).json()
    assert [i["id"] for i in rest["items"]] == [exam["id"]]
    bad = c.get(x.e_path(), params={"limit": 1, "status": "draft", "cursor": listed["next_cursor"]})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_cursor"
    assert c.get(x.e_path(str(uuid.uuid4()))).json()["code"] == "exam_not_found"


def test_items_add_insert_remove_reorder(ws):
    x, c = ws["x"], ws["c"]
    q1, q2, q3 = (x.question(mcq_body(stem=f"Q{i}?")) for i in range(3))
    exam = x.exam()
    exam = x.add(exam, q1)
    exam = x.add(exam, q2, points=5)
    r = c.post(x.e_path(exam["id"], "/items"), headers=match(exam["version"]),
               json={"question_id": q3["id"], "points": 1, "position": 1, "blueprint_row_id": None})  # fmt: skip
    assert r.status_code == 201 and r.headers["x-exam-item-id"]
    exam = r.json()
    assert [i["question_id"] for i in exam["items"]] == [q3["id"], q1["id"], q2["id"]]
    assert [i["position"] for i in exam["items"]] == [1, 2, 3] and exam["total_points"] == 8 and exam["version"] == 4
    dup = c.post(x.e_path(exam["id"], "/items"), headers=match(4), json={"question_id": q1["id"], "points": 1, "position": None,
                                                                          "blueprint_row_id": None})  # fmt: skip
    assert dup.status_code == 409 and dup.json()["code"] == "question_already_in_exam" and dup.json()["item_id"]
    for body, code in (({"question_id": q1["id"], "points": 0, "position": None, "blueprint_row_id": None}, "invalid_points"),
                       ({"question_id": q1["id"], "points": 101, "position": None, "blueprint_row_id": None}, "invalid_points"),
                       ({"question_id": q1["id"], "points": 1.5, "position": None, "blueprint_row_id": None}, "invalid_points"),
                       ({"question_id": q1["id"], "points": 1, "position": 9, "blueprint_row_id": None}, "invalid_position"),
                       ({"question_id": q1["id"], "points": 1}, "invalid_fields")):  # fmt: skip
        r = c.post(x.e_path(exam["id"], "/items"), headers=match(4), json=body)
        assert r.status_code == 422 and r.json()["code"] == code, body
    missing = c.post(x.e_path(exam["id"], "/items"), headers=match(4),
                     json={"question_id": str(uuid.uuid4()), "points": 1, "position": None, "blueprint_row_id": None})  # fmt: skip
    assert missing.status_code == 404 and missing.json()["code"] == "question_not_found"
    ids = [i["id"] for i in exam["items"]]
    for order, reason in ((ids[:2], "missing"), (ids + [ids[0]], "duplicate"), (ids[:2] + [str(uuid.uuid4())], "unknown")):
        r = c.put(x.e_path(exam["id"], "/items/order"), json={"item_ids": order}, headers=match(4))
        assert r.status_code == 422 and r.json()["code"] == "invalid_order" and r.json()["reason"] == reason
    reversed_ = c.put(x.e_path(exam["id"], "/items/order"), json={"item_ids": list(reversed(ids))}, headers=match(4))
    assert reversed_.status_code == 200 and [i["id"] for i in reversed_.json()["items"]] == list(reversed(ids))
    exam = reversed_.json()
    r = c.patch(x.e_path(exam["id"], f"/items/{ids[0]}"), json={"points": 7}, headers=match(exam["version"]))
    assert r.status_code == 200 and next(i for i in r.json()["items"] if i["id"] == ids[0])["points"] == 7
    exam = r.json()
    removed = c.delete(x.e_path(exam["id"], f"/items/{ids[1]}"), headers=match(exam["version"]))
    assert removed.status_code == 200 and [i["position"] for i in removed.json()["items"]] == [1, 2]
    assert c.delete(x.e_path(exam["id"], f"/items/{ids[1]}"), headers=match(removed.json()["version"])).json()["code"] == "exam_item_not_found"
    retired = c.post(x.q_path(q2["id"], "/retire"), headers=match(q2["version"]))
    assert retired.status_code == 200
    re_add = c.post(x.e_path(exam["id"], "/items"), headers=match(removed.json()["version"]), json={
        "question_id": q2["id"], "points": 1, "position": None, "blueprint_row_id": None})  # fmt: skip
    assert re_add.status_code in (201, 409)
    history = c.get(x.e_path(exam["id"], "/history")).json()["items"]
    assert {h["action"] for h in history} >= {"exam_created", "item_added", "items_reordered", "item_updated", "item_removed"}


def test_bp06_swap_preserves_ids_assignments_and_bumps_once(ws):
    x, world = ws["x"], ws["world"]
    exam = x.blueprint(x.exam(), [row(1), row(1, 3)])["exam"]
    a, b = (r["id"] for r in exam["blueprint"]["rows"])
    q1, q2 = x.question(mcq_body(stem="A?")), x.question(mcq_body(stem="B?"))
    exam = x.add(x.add(exam, q1, row=a), q2, row=b)
    version = exam["version"]
    swapped = x.blueprint(exam, [row(1, 3, id=b), row(1, id=a)])
    rows = swapped["exam"]["blueprint"]["rows"]
    assert [(r["id"], r["position"]) for r in rows] == [(b, 1), (a, 2)]
    assert swapped["exam"]["version"] == version + 1
    assert swapped["blueprint_change"] == {"kept": 2, "inserted": 0, "removed": 0, "unassigned_item_ids": []}
    assert {i["question_id"]: i["blueprint_row_id"] for i in swapped["exam"]["items"]} == {q1["id"]: a, q2["id"]: b}
    assert world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = 'blueprint_replaced'",
                        e=uuid.UUID(exam["id"])) == 2  # fmt: skip
    noop = x.blueprint(swapped["exam"], [row(1, 3, id=b), row(1, id=a)])
    assert noop["exam"]["version"] == version + 1 and noop["blueprint_change"]["kept"] == 2
    assert _foreign_rows(world) == 0


def test_bp07_reverse_fifty_rows(ws):
    x = ws["x"]
    exam = x.blueprint(x.exam(), [row(1) for _ in range(50)])["exam"]
    ids = [r["id"] for r in exam["blueprint"]["rows"]]
    result = x.blueprint(exam, [row(1, id=i) for i in reversed(ids)])
    assert [r["id"] for r in result["exam"]["blueprint"]["rows"]] == list(reversed(ids))
    assert [r["position"] for r in result["exam"]["blueprint"]["rows"]] == list(range(1, 51))
    assert result["exam"]["version"] == exam["version"] + 1
    r = ws["c"].put(x.e_path(exam["id"], "/blueprint"), json={"rows": [row(1) for _ in range(51)]}, headers=match(result["exam"]["version"]))
    assert r.status_code == 422 and r.json()["reason"] == "too_many_rows"


def test_bp08_bp09_mixed_replacement_and_removed_rows_unassign(ws):
    x, world = ws["x"], ws["world"]
    exam = x.blueprint(x.exam(), [row(1, type="mcq"), row(1), row(1, type="mcq"), row(1), row(1)])["exam"]
    r1, r2, r3, r4, r5 = (r["id"] for r in exam["blueprint"]["rows"])
    qa, qb, qc = x.question(mcq_body(stem="A?")), x.question(mcq_body(stem="B?")), x.question(tf_body())
    exam = x.add(x.add(x.add(exam, qa, row=r1), qb, row=r4), qc, row=r2)
    result = x.blueprint(exam, [row(1, id=r3, type="mcq"), row(1, type="short"), row(1, id=r1, type="mcq"),
                                row(1, id=r2, type="short"), row(2)])  # fmt: skip
    change = result["blueprint_change"]
    item_b = next(i["id"] for i in exam["items"] if i["question_id"] == qb["id"])
    assert (change["kept"], change["inserted"], change["removed"], change["unassigned_item_ids"]) == (3, 2, 2, [item_b])
    rows = result["exam"]["blueprint"]["rows"]
    assert [r["position"] for r in rows] == [1, 2, 3, 4, 5] and [rows[0]["id"], rows[2]["id"], rows[3]["id"]] == [r3, r1, r2]
    assigned = {i["question_id"]: i["blueprint_row_id"] for i in result["exam"]["items"]}
    assert assigned == {qa["id"]: r1, qb["id"]: None, qc["id"]: r2}
    mismatches = [i for i in result["exam"]["readiness"]["issues"] if i["code"] == "blueprint_criteria_mismatch"]
    item_c = next(i["id"] for i in exam["items"] if i["question_id"] == qc["id"])
    assert [(m["item_id"], m["blueprint_row_id"]) for m in mismatches] == [(item_c, r2)]  # only the true/false item
    assert world.scalar("SELECT count(*) FROM exam_blueprint_rows WHERE id = ANY(:ids)",
                        ids=[uuid.UUID(r4), uuid.UUID(r5)]) == 0  # fmt: skip
    assert _foreign_rows(world) == 0


def test_bp11_row_ids_of_other_exams_and_invalid_rows(ws):
    x, c, world = ws["x"], ws["c"], ws["world"]
    other = x.blueprint(x.exam("Other"), [row(1)])["exam"]
    foreign = other["blueprint"]["rows"][0]["id"]
    exam = x.blueprint(x.exam(), [row(1)])["exam"]
    before = exam["version"]
    for rows, reason in (([row(1, id=foreign)], "unknown_row"), ([row(1, id=str(uuid.uuid4()))], "unknown_row"),
                         ([row(1, id=exam["blueprint"]["rows"][0]["id"])] * 2, "duplicate_row"), ([row(0)], "count"),
                         ([row(201)], "count"), ([row(150), row(51)], "total_count"), ([row(1, 0)], "points"),
                         ([row(1, 101)], "points"), ([row(1, topic_id=str(ws["other_topic"]))], "topic"),
                         ([row(1, type="essay")], "criteria")):  # fmt: skip
        r = c.put(x.e_path(exam["id"], "/blueprint"), json={"rows": rows}, headers=match(before))
        assert r.status_code == 422 and r.json()["code"] == "invalid_blueprint" and r.json()["reason"] == reason, rows
    assert x.get(exam)["version"] == before and x.get(other)["blueprint"]["rows"][0]["id"] == foreign
    q = x.question()
    r = c.post(x.e_path(exam["id"], "/items"), headers=match(before),
               json={"question_id": q["id"], "points": 1, "position": None, "blueprint_row_id": foreign})  # fmt: skip
    assert r.status_code == 422 and r.json()["code"] == "invalid_blueprint_row" and r.json()["reason"] == "unknown_row"
    mismatch = x.blueprint(exam, [row(1, type="short")])["exam"]
    r = c.post(x.e_path(exam["id"], "/items"), headers=match(mismatch["version"]),
               json={"question_id": q["id"], "points": 1, "position": None,
                     "blueprint_row_id": mismatch["blueprint"]["rows"][0]["id"]})  # fmt: skip
    assert r.status_code == 422 and r.json()["reason"] == "criteria_mismatch"
    assert x.blueprint(mismatch, [])["exam"]["blueprint"]["rows"] == []
    assert _foreign_rows(world) == 0


def test_e17_fill_from_blueprint_is_deterministic(ws):
    x, c = ws["x"], ws["c"]
    approved = [x.question(mcq_body(stem=f"Fill {i}?"), approve=True) for i in range(3)]
    x.question(mcq_body(stem="Draft only?"))  # not approved: never selected
    x.question(short_body(), approve=True)
    exam = x.blueprint(x.exam(), [row(2, 3, type="mcq"), row(2, 10, type="short")])["exam"]
    r = c.post(x.e_path(exam["id"], "/fill-from-blueprint"), json={"row_ids": None}, headers=match(exam["version"]))
    assert r.status_code == 200
    filled = r.json()
    assert [(f["added"], f["missing"]) for f in filled["filled"]] == [(2, 0), (1, 1)]
    assert all("stopped_by" not in f for f in filled["filled"])
    items = filled["exam"]["items"]
    assert [i["question_id"] for i in items[:2]] == [approved[0]["id"], approved[1]["id"]]  # oldest first
    assert [i["points"] for i in items] == [3, 3, 10]
    again = c.post(x.e_path(exam["id"], "/fill-from-blueprint"), json={"row_ids": None}, headers=match(filled["exam"]["version"]))
    assert again.status_code == 200 and again.json()["exam"]["version"] == filled["exam"]["version"]  # nothing added
    bad = c.post(x.e_path(exam["id"], "/fill-from-blueprint"), json={"row_ids": [str(uuid.uuid4())]},
                 headers=match(filled["exam"]["version"]))  # fmt: skip
    assert bad.status_code == 422 and bad.json()["reason"] == "unknown_row"


@pytest.mark.parametrize("fits", [True, False])
def test_f07_blueprint_fill_budgets_the_approved_citation(ws, make_api, fits):
    world, course = ws["world"], ws["course"]
    api = make_api(settings_overrides={**ex.ENABLED, "exam_export_max_content_chars": 1000}).as_user(ws["professor"])
    x = ExamClient(api, course)
    short_title, long_title = "a.pdf", "a" * 200 + ".pdf"
    original, renamed = (short_title, long_title) if fits else (long_title, short_title)
    doc = world.document(course, title=original, status="indexed", page_count=1)
    ref = {"document_id": str(doc), "locator": {"page": 1}}
    x.question(mcq_body(stem="s" * 850, explanation=None, source_refs=[ref]), approve=True)
    world.execute("UPDATE documents SET title = :title WHERE id = :d", title=renamed, d=doc)
    draft = x.blueprint(x.exam(), [row(1)])["exam"]
    response = api.client.post(x.e_path(draft["id"], "/fill-from-blueprint"),
                               json={"row_ids": None}, headers=match(draft["version"]))
    assert response.status_code == 200, response.text
    result = response.json()
    outcome = result["filled"][0]
    if fits:
        assert (outcome["added"], outcome.get("stopped_by")) == (1, None)
        assert x.finalized(result["exam"])["content_chars"] == result["exam"]["content_chars"]
    else:
        assert (outcome["added"], outcome.get("stopped_by")) == (0, "export_budget")


def test_val08_export_budget_on_growth(ws, make_api):
    api = make_api(settings_overrides={**ex.ENABLED, "exam_export_max_content_chars": 1000}).as_user(ws["professor"])
    x, c = ExamClient(api, ws["course"]), api.client
    exam = x.exam("T", instructions=None)  # header: "CHEM 101" + "General Chemistry" + "T" = 26 code points
    big = x.question(mcq_body(stem="s" * 900, explanation=None, choices=[{"id": "a", "text": "x"}, {"id": "b", "text": "y"}],
                              answer_key={"choice_id": "a"}), approve=True)  # 902 code points
    filler = x.question(mcq_body(stem="t" * 70, explanation=None, choices=[{"id": "a", "text": "p"}, {"id": "b", "text": "q"}],
                                 answer_key={"choice_id": "a"}), approve=True)  # 72: total 1000 exactly
    over = x.question(mcq_body(stem="u", explanation=None, choices=[{"id": "a", "text": "v"}, {"id": "b", "text": "w"}],
                               answer_key={"choice_id": "a"}), approve=True)  # +3
    exam = x.add(x.add(exam, big), filler)
    assert exam["content_chars"] == 1000
    r = c.post(x.e_path(exam["id"], "/items"), headers=match(exam["version"]),
               json={"question_id": over["id"], "points": 1, "position": None, "blueprint_row_id": None})  # fmt: skip
    assert r.status_code == 409 and r.json() == {"detail": r.json()["detail"], "code": "export_budget_exceeded",
                                                 "content_chars": 1003, "export_budget": 1000}  # fmt: skip
    dup = c.post(x.e_path(exam["id"], "/duplicate"), json={"title": "T"}, headers=key())
    assert dup.status_code == 201  # same size: allowed
    edited = x.edit(filler, mcq_body(stem="t" * 80, explanation=None, choices=[{"id": "a", "text": "p"}, {"id": "b", "text": "q"}],
                                     answer_key={"choice_id": "a"}))  # bank edits are never blocked
    x.approve(edited)
    draft = x.get(exam)
    assert draft["content_chars"] == 1010
    assert {"code": "export_budget_exceeded", "content_chars": 1010, "export_budget": 1000, "overridable": False} in draft["readiness"]["issues"]
    r = x.finalize(draft)
    assert r.status_code == 409 and r.json()["code"] == "exam_not_ready"
    limited = make_api(settings_overrides={**ex.ENABLED, "exam_max_items": 1}).as_user(ws["professor"])
    lx = ExamClient(limited, ws["course"])
    one = lx.add(lx.exam("One"), big)
    r = limited.client.post(lx.e_path(one["id"], "/items"), headers=match(one["version"]),
                            json={"question_id": filler["id"], "points": 1, "position": None, "blueprint_row_id": None})  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "exam_item_limit"


def test_exam_limit(ws, make_api):
    api = make_api(settings_overrides={**ex.ENABLED, "exam_max_exams": 1}).as_user(ws["professor"])
    x = ExamClient(api, ws["course"])
    x.exam()
    r = api.client.post(x.e_path(), json={"title": "T", "instructions": None, "duration_minutes": None}, headers=key())
    assert r.status_code == 409 and r.json()["code"] == "exam_limit_reached"
