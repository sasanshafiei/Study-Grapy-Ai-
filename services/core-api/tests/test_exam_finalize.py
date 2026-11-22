"""Preview, finalize, archive and duplicate (E18-E21) on real PostgreSQL: preview fingerprint (FP-*), strict
blueprint (BP-01 ... BP-05), finalize intent and replays, duplicate report (DUP-01), source freezing (SRC-01)."""

from __future__ import annotations

import uuid

import pytest

from app.exams import snapshot
from tests import exam_support as ex
from tests.exam_support import ExamClient, key, match, mcq_body, row, short_body, tf_body


@pytest.fixture
def fx(world, make_api):
    professor = world.user("professor", name="Prof. Ada")
    course = world.course(professor, name="General Chemistry", code="CHEM 101")
    topic = world.topic(course, "Stoichiometry")
    document = world.document(course, title="Lecture_04.pdf", status="indexed", page_count=20)
    api = make_api(settings_overrides=ex.ENABLED).as_user(professor)
    return {"world": world, "api": api, "x": ExamClient(api, course), "c": api.client, "course": course,
            "topic": topic, "document": document, "professor": professor}  # fmt: skip


def _ready_exam(x, n: int = 2):
    exam = x.exam()
    for i in range(n):
        exam = x.add(exam, x.question(mcq_body(stem=f"Ready {i}?"), approve=True))
    return exam


def _audits(world, exam_id, action) -> int:
    return world.scalar("SELECT count(*) FROM exam_audit_events WHERE entity_id = :e AND action = :a",
                        e=uuid.UUID(exam_id), a=action)  # fmt: skip


def test_preview_variants_and_fingerprint_properties_fp10(fx):
    x, c = fx["x"], fx["c"]
    ref = {"document_id": str(fx["document"]), "locator": {"page": 6}}
    exam = x.exam()
    exam = x.add(exam, x.question(mcq_body(source_refs=[ref]), approve=True), points=2)
    exam = x.add(exam, x.question(tf_body(), approve=True), points=1)
    exam = x.add(exam, x.question(short_body(), approve=True), points=5)
    student, answers = x.preview(exam), x.preview(exam, "answer_key")
    assert student["preview_fingerprint"] == answers["preview_fingerprint"] == x.get(exam)["readiness"]["preview_fingerprint"]
    assert student["answer_key"] is None and student["header"] == {
        "course_code": "CHEM 101", "course_name": "General Chemistry", "title": "Midterm 1",
        "instructions": "Answer all questions.", "duration_minutes": 90, "question_count": 3, "total_points": 8}
    mcq, tf, short = student["questions"]
    assert [o["label"] for o in mcq["options"]] == ["A", "B", "C", "D"] and "answer_lines" not in mcq
    assert tf["options"] == [{"label": None, "text": "True"}, {"label": None, "text": "False"}]
    assert short["options"] is None and short["answer_lines"] == 6
    assert answers["answer_key"][0] == {"number": 1, "points": 2, "answer": "C", "answer_text": "4.0 mol", "model_answer": None,
                                        "rubric": None, "explanation": "CH₄ + 2 O₂ → CO₂ + 2 H₂O.",
                                        "sources": ["Lecture_04.pdf, p. 6"]}  # fmt: skip
    assert answers["answer_key"][1]["answer"] == "True" and answers["answer_key"][2]["model_answer"].startswith("H₂ is limiting")
    r = c.get(x.e_path(exam["id"], "/preview"), params={"variant": "teacher"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_query"
    finalized = x.finalized(exam)
    assert finalized["readiness"] is None and x.preview(finalized)["preview_fingerprint"] is None


def test_fp05_finalize_pins_the_previewed_revisions_and_freezes_everything(fx):
    x, world = fx["x"], fx["world"]
    exam = _ready_exam(x)
    preview = x.preview(exam)
    fingerprint = preview["preview_fingerprint"]
    final = x.finalized(exam, fingerprint=fingerprint)
    assert final["status"] == "finalized" and final["version"] == exam["version"] + 1
    assert final["finalized_version"] == final["version"] and final["blueprint_status"] == "none"
    assert final["finalize_preview_fingerprint"] == fingerprint and final["course_snapshot"] == {"code": "CHEM 101", "name": "General Chemistry"}
    assert all(i["pinned_revision"] == 1 for i in final["items"]) and len(final["content_sha256"]) == 64
    details = world.rows("SELECT details FROM exam_audit_events WHERE entity_id = :e AND action = 'exam_finalized'",
                         e=uuid.UUID(exam["id"]))[0]["details"]  # fmt: skip
    assert details["preview_fingerprint"] == fingerprint and details["content_sha256"] == final["content_sha256"]
    world.execute("UPDATE courses SET name = 'Renamed', code = 'NEW' WHERE id = :c", c=fx["course"])
    assert x.get(final)["course_snapshot"] == {"code": "CHEM 101", "name": "General Chemistry"}
    assert x.preview(final)["header"]["course_code"] == "CHEM 101"


def test_fp01_re_approved_question_after_preview_is_stale(fx):
    x, world = fx["x"], fx["world"]
    q = x.question(mcq_body(), approve=True)
    exam = x.add(x.exam(), q)
    old = x.preview(exam)["preview_fingerprint"]
    x.approve(x.edit(q, mcq_body(stem="Changed after the preview?")))  # revision 2, exam version unchanged
    r = x.finalize(exam, fingerprint=old)
    assert r.status_code == 412 and r.json() == {"detail": r.json()["detail"], "code": "exam_preview_stale",
                                                 "current_version": exam["version"]}  # fmt: skip
    after = x.get(exam)
    assert after["status"] == "draft" and after["version"] == exam["version"] and after["items"][0]["pinned_revision"] is None
    assert _audits(world, exam["id"], "exam_finalized") == 0
    assert after["readiness"]["preview_fingerprint"] != old


def test_fp02_changes_outside_the_exam_do_not_affect_the_fingerprint(fx):
    x = fx["x"]
    exam = _ready_exam(x)
    old = x.preview(exam)["preview_fingerprint"]
    outsider = x.question(mcq_body(stem="Not in the exam?"), approve=True)
    x.approve(x.edit(outsider, mcq_body(stem="Still not in the exam?")))
    assert x.preview(exam)["preview_fingerprint"] == old
    assert x.finalize(exam, fingerprint=old).status_code == 200


@pytest.mark.parametrize("change", ["title", "instructions", "duration", "blueprint", "add", "points", "order", "remove"])
def test_fp03_any_exam_change_after_the_preview(fx, change):
    x, c = fx["x"], fx["c"]
    exam = _ready_exam(x, 3)
    old = x.preview(exam)["preview_fingerprint"]
    items = exam["items"]
    v = match(exam["version"])
    if change in ("title", "instructions", "duration"):
        field = {"title": {"title": "New"}, "instructions": {"instructions": "New."}, "duration": {"duration_minutes": 60}}[change]
        r = c.patch(x.e_path(exam["id"]), json=field, headers=v)
    elif change == "blueprint":
        r = c.put(x.e_path(exam["id"], "/blueprint"), json={"rows": [row(3)]}, headers=v)
    elif change == "add":
        r = c.post(x.e_path(exam["id"], "/items"), headers=v, json={"question_id": x.question(tf_body(), approve=True)["id"],
                                                                     "points": 1, "position": None, "blueprint_row_id": None})  # fmt: skip
    elif change == "points":
        r = c.patch(x.e_path(exam["id"], f"/items/{items[0]['id']}"), json={"points": 9}, headers=v)
    elif change == "order":
        r = c.put(x.e_path(exam["id"], "/items/order"), json={"item_ids": [i["id"] for i in reversed(items)]}, headers=v)
    else:
        r = c.delete(x.e_path(exam["id"], f"/items/{items[0]['id']}"), headers=v)
    assert r.status_code in (200, 201), r.text
    new_version = (r.json()["exam"] if "exam" in r.json() else r.json())["version"]
    assert x.finalize(exam, fingerprint=old, version=new_version).json()["code"] == "exam_preview_stale"
    stale = x.finalize(exam, fingerprint=old, version=exam["version"])
    assert stale.status_code == 412 and stale.json()["code"] == "version_mismatch" and stale.json()["current_version"] == new_version


def test_fp04_course_header_change_makes_the_preview_stale(fx):
    x, world = fx["x"], fx["world"]
    exam = _ready_exam(x)
    old = x.preview(exam)["preview_fingerprint"]
    world.execute("UPDATE courses SET code = 'CHEM 102' WHERE id = :c", c=fx["course"])
    assert x.finalize(exam, fingerprint=old).json()["code"] == "exam_preview_stale"
    world.execute("UPDATE courses SET code = 'CHEM 101', name = 'Chemistry I' WHERE id = :c", c=fx["course"])
    assert x.finalize(exam, fingerprint=old).json()["code"] == "exam_preview_stale"


def test_fp07_fp08_replay_and_intent_mismatch(fx):
    x, c, world = fx["x"], fx["c"], fx["world"]
    exam = _ready_exam(x)
    fingerprint = x.preview(exam)["preview_fingerprint"]
    first = x.finalize(exam, fingerprint=fingerprint)
    assert first.status_code == 200 and "idempotent-replayed" not in first.headers
    lost = x.finalize(exam, fingerprint=fingerprint)  # identical retry after a lost response
    assert lost.status_code == 200 and lost.headers["idempotent-replayed"] == "true" and lost.json() == first.json()
    archived = c.post(x.e_path(exam["id"], "/archive"), headers=match(first.json()["version"]))
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    after_archive = x.finalize(exam, fingerprint=fingerprint)
    assert after_archive.status_code == 200 and after_archive.json()["status"] == "archived"
    assert after_archive.headers["idempotent-replayed"] == "true"
    other = "pf1:" + "0" * 64
    for fp, reason in ((other, None), (fingerprint, "A deviation that was never sent.")):
        r = x.finalize(exam, fingerprint=fp, reason=reason)
        assert r.status_code == 409 and r.json()["code"] == "finalize_intent_mismatch"
    assert _audits(world, exam["id"], "exam_finalized") == 1
    same_version = x.finalize(exam, fingerprint=fingerprint, version=archived.json()["version"])
    assert same_version.status_code == 409 and same_version.json()["code"] == "exam_not_draft"


def test_fp09_malformed_or_missing_fingerprint(fx):
    x, c = fx["x"], fx["c"]
    exam = _ready_exam(x)
    path, v = x.e_path(exam["id"], "/finalize"), match(exam["version"])
    r = c.post(path, headers=v, json={"preview_fingerprint": None, "blueprint_deviation_reason": None})
    assert r.status_code == 428 and r.json()["code"] == "preview_fingerprint_required"
    for bad in ("pf2:" + "0" * 64, "pf1:" + "A" * 64, "pf1:" + "0" * 63, "pf1:" + "0" * 65, 7, ["pf1:"]):
        r = c.post(path, headers=v, json={"preview_fingerprint": bad, "blueprint_deviation_reason": None})
        assert r.status_code == 400 and r.json()["code"] == "invalid_preview_fingerprint", bad
    for body in ({"preview_fingerprint": "pf1:" + "0" * 64}, {}):
        r = c.post(path, headers=v, json=body)
        assert r.status_code == 422 and r.json()["code"] == "invalid_fields"
    r = c.post(path, headers=v)
    assert r.status_code == 422 and r.json()["code"] == "invalid_fields"
    r = c.post(path, json={"preview_fingerprint": "pf1:" + "0" * 64, "blueprint_deviation_reason": None})
    assert r.status_code == 428 and r.json()["code"] == "precondition_required"
    assert x.get(exam)["status"] == "draft"


def _demo_bank(x):
    mcq = [x.question(mcq_body(stem=f"Stoichiometry {i}?", topic_id=None), approve=True) for i in range(10)]
    tf = [x.question(tf_body(stem=f"Gas law {i} holds.", difficulty="easy"), approve=True) for i in range(6)]
    short = [x.question(short_body(stem=f"Explain limiting reactant case {i}."), approve=True) for i in range(4)]
    return mcq, tf, short


def test_bp01_demo_blueprint_filled_exactly_is_fulfilled(fx):
    x, c = fx["x"], fx["c"]
    _demo_bank(x)
    exam = x.blueprint(x.exam(), [row(10, 2, type="mcq"), row(6, 1, type="true_false"), row(4, 10, type="short")])["exam"]
    filled = c.post(x.e_path(exam["id"], "/fill-from-blueprint"), json={"row_ids": None}, headers=match(exam["version"])).json()
    exam = filled["exam"]
    assert exam["item_count"] == 20 and exam["total_points"] == 66 and exam["readiness"]["ready"] is True
    assert exam["readiness"]["blueprint"] == {"declared": True, "fulfilled": True}
    final = x.finalized(exam)
    assert final["blueprint_status"] == "fulfilled" and final["blueprint_deviation_reason"] is None


def test_bp02_bp03_bp04_strict_blueprint_and_explicit_deviation(fx):
    x, c, world = fx["x"], fx["c"], fx["world"]
    q1, q2, q3 = (x.question(mcq_body(stem=f"Strict {i}?"), approve=True) for i in range(3))
    exam = x.blueprint(x.exam(), [row(2, type="mcq"), row(1, type="short")])["exam"]
    r1, r2 = (r["id"] for r in exam["blueprint"]["rows"])
    exam = x.add(x.add(exam, q1, row=r1), q2)  # row 1 underfilled, row 2 empty, q2 unassigned
    r = x.finalize(exam)
    assert r.status_code == 409 and r.json()["code"] == "exam_not_ready"
    codes = {(i["code"], i["overridable"]) for i in r.json()["issues"]}
    assert codes == {("blueprint_row_underfilled", True), ("item_not_in_blueprint", True)}
    bad = x.finalize(exam, reason="short")
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_blueprint_override"
    bad = x.finalize(exam, reason="x\x00" * 10)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_blueprint_override"
    fingerprint = x.preview(exam)["preview_fingerprint"]
    reason = "Two short-answer slots intentionally left out: lab cancelled."
    final = x.finalized(exam, fingerprint=fingerprint, reason=reason)
    assert final["blueprint_status"] == "deviation_accepted" and final["blueprint_deviation_reason"] == reason
    details = world.rows("SELECT details FROM exam_audit_events WHERE entity_id = :e AND action = 'exam_finalized'",
                         e=uuid.UUID(exam["id"]))[0]["details"]  # fmt: skip
    assert sorted(map(tuple, details["deviations"]["rows"])) == sorted([(r1, 2, 1), (r2, 1, 0)])
    assert details["deviations"]["unassigned_items"] == 1
    # reason with a fulfilled or absent blueprint
    plain = x.add(x.exam("Plain"), q3)
    r = x.finalize(plain, reason=reason)
    assert r.status_code == 422 and r.json()["code"] == "blueprint_override_not_needed"
    # BP-04: a reason never overrides a non-blueprint issue
    draft_q = x.question(mcq_body(stem="Unapproved?"))
    blocked = x.blueprint(x.exam("Blocked"), [row(2)])["exam"]
    blocked = x.add(blocked, draft_q, row=blocked["blueprint"]["rows"][0]["id"])
    r = x.finalize(blocked, reason=reason)
    assert r.status_code == 409 and {i["code"] for i in r.json()["issues"] if not i["overridable"]} == {"question_not_approved"}
    assert c.get(x.e_path(blocked["id"])).json()["status"] == "draft"


def test_bp02_criteria_mismatch_after_edit_and_reapproval(fx):
    x = fx["x"]
    q = x.question(mcq_body(difficulty="medium"), approve=True)
    exam = x.blueprint(x.exam(), [row(1, difficulty="medium")])["exam"]
    exam = x.add(exam, q, row=exam["blueprint"]["rows"][0]["id"])
    x.approve(x.edit(q, mcq_body(difficulty="hard")))
    r = x.finalize(x.get(exam))
    assert r.status_code == 409 and [i["code"] for i in r.json()["issues"]] == ["blueprint_criteria_mismatch"]


def test_bp05_fp11_deviation_is_bound_to_the_reviewed_snapshot(fx):
    x, c = fx["x"], fx["c"]
    q1, q2 = (x.question(mcq_body(stem=f"Bound {i}?"), approve=True) for i in range(2))
    exam = x.blueprint(x.exam(), [row(3)])["exam"]
    exam = x.add(exam, q1, row=exam["blueprint"]["rows"][0]["id"])
    reviewed = x.preview(exam)["preview_fingerprint"]
    added = c.post(x.e_path(exam["id"], "/items"), headers=match(exam["version"]),
                   json={"question_id": q2["id"], "points": 1, "position": None, "blueprint_row_id": None}).json()  # fmt: skip
    reason = "Only one question this week on purpose."
    assert x.finalize(exam, fingerprint=reviewed, reason=reason).json()["code"] == "version_mismatch"
    assert x.finalize(exam, fingerprint=reviewed, reason=reason, version=added["version"]).json()["code"] == "exam_preview_stale"
    assert x.get(exam)["status"] == "draft"
    assert x.finalized(added, reason=reason)["blueprint_status"] == "deviation_accepted"


def test_finalize_blocking_issues(fx):
    x = fx["x"]
    empty = x.exam()
    r = x.finalize(empty)
    assert r.status_code == 409 and [i["code"] for i in r.json()["issues"]] == ["no_items"]
    retired = x.question(mcq_body(), approve=True)
    exam = x.add(x.exam("R"), retired)
    fx["c"].post(x.q_path(retired["id"], "/retire"), headers=match(retired["version"]))
    r = x.finalize(x.get(exam))
    assert r.status_code == 409 and r.json()["issues"][0]["code"] == "question_retired"


def test_archive_semantics_and_replays(fx):
    x, c = fx["x"], fx["c"]
    draft = x.exam("Draft")
    archived = c.post(x.e_path(draft["id"], "/archive"), headers=match(1))
    assert archived.status_code == 200 and archived.json()["finalized_version"] is None and archived.json()["version"] == 2
    again = c.post(x.e_path(draft["id"], "/archive"), headers=match(1))
    assert again.status_code == 200 and again.headers["idempotent-replayed"] == "true"
    same = c.post(x.e_path(draft["id"], "/archive"), headers=match(2))
    assert same.status_code == 200 and same.json()["version"] == 2 and same.headers["idempotent-replayed"] == "true"
    assert c.post(x.e_path(draft["id"], "/archive"), headers=match(7)).json()["code"] == "version_mismatch"
    r = c.patch(x.e_path(draft["id"]), json={"title": "x"}, headers=match(2))
    assert r.status_code == 409 and r.json()["code"] == "exam_not_draft"
    final = x.finalized(_ready_exam(x))
    frozen = {k: final[k] for k in ("finalized_version", "content_sha256", "content_chars", "blueprint_status", "finalized_at",
                                    "course_snapshot", "items", "finalize_preview_fingerprint")}  # fmt: skip
    archived = c.post(x.e_path(final["id"], "/archive"), headers=match(final["version"])).json()
    assert archived["status"] == "archived" and archived["version"] == final["version"] + 1
    assert {k: archived[k] for k in frozen} == frozen
    r = c.post(x.e_path(final["id"], "/items"), headers=match(archived["version"]),
               json={"question_id": x.question()["id"], "points": 1, "position": None, "blueprint_row_id": None})  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "exam_not_draft"


def test_dup01_duplicate_reports_current_bank_content(fx):
    x, c = fx["x"], fx["c"]
    same, changed, unapproved, retired = (x.question(mcq_body(stem=f"Dup {i}?"), approve=True) for i in range(4))
    exam = x.blueprint(x.exam(), [row(4)])["exam"]
    for q in (same, changed, unapproved, retired):
        exam = x.add(exam, q, row=exam["blueprint"]["rows"][0]["id"])
    final = x.finalized(exam)
    x.approve(x.edit(changed, mcq_body(stem="Dup changed?")))
    x.edit(unapproved, mcq_body(stem="Dup unapproved?"))
    c.post(x.q_path(retired["id"], "/retire"), headers=match(retired["version"]))
    headers = key()
    r = c.post(x.e_path(final["id"], "/duplicate"), json={"title": None}, headers=headers)
    assert r.status_code == 201
    copy = r.json()
    new = copy["exam"]
    assert new["title"] == "Midterm 1 (copy)" and new["derived_from_exam_id"] == final["id"] and new["status"] == "draft"
    assert new["blueprint"]["rows"][0]["id"] != exam["blueprint"]["rows"][0]["id"]
    assert all(i["blueprint_row_id"] == new["blueprint"]["rows"][0]["id"] for i in new["items"])
    report = {i["question_id"]: (i["content"], i["source_revision"], i["current_status"]) for i in copy["duplicate_report"]}
    assert report == {same["id"]: ("unchanged", 1, "approved"), changed["id"]: ("changed", 1, "approved"),
                      unapproved["id"]: ("not_approved", 1, "draft"), retired["id"]: ("retired", 1, "retired")}  # fmt: skip
    warned = {w["item_id"] for w in new["readiness"]["warnings"] if w["code"] == "item_changed_since_source"}
    assert len(warned) == 3
    assert next(i for i in new["items"] if i["question_id"] == changed["id"])["question"]["stem"] == "Dup changed?"
    replay = c.post(x.e_path(final["id"], "/duplicate"), json={"title": None}, headers=headers)
    assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true" and replay.json()["exam"]["id"] == new["id"]
    conflict = c.post(x.e_path(final["id"], "/duplicate"), json={"title": "Other"}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_key_reused"
    long = c.post(x.e_path(final["id"], "/duplicate"), json={"title": None}, headers=key()).json()
    assert long["exam"]["id"] != new["id"]
    draft_copy = c.post(x.e_path(new["id"], "/duplicate"), json={"title": "Second"}, headers=key()).json()
    assert {i["content"] for i in draft_copy["duplicate_report"]} == {"unchanged"}
    assert all(i["source_revision"] is None for i in draft_copy["duplicate_report"])


def test_src01_finalized_exam_keeps_frozen_titles(fx):
    x, world = fx["x"], fx["world"]
    ref = {"document_id": str(fx["document"]), "locator": {"page": 2}}
    exam = x.add(x.exam(), x.question(mcq_body(source_refs=[ref]), approve=True))
    final = x.finalized(exam)
    world.execute("UPDATE documents SET title = 'Renamed.pdf' WHERE id = :d", d=fx["document"])
    assert x.preview(final, "answer_key")["answer_key"][0]["sources"] == ["Lecture_04.pdf, p. 2"]
    assert x.get(final)["items"][0]["question"]["source_refs"][0]["title"] == "Lecture_04.pdf"
    assert x.get(final)["content_sha256"] == final["content_sha256"]


def test_f07_reviewed_citation_matches_finalization_while_editor_tracks_the_source(fx):
    x, world, doc = fx["x"], fx["world"], fx["document"]
    ref = {"document_id": str(doc), "locator": {"page": 2}}
    q = x.question(mcq_body(source_refs=[ref]), approve=True)
    revision = fx["c"].get(x.q_path(q["id"], "/revisions")).json()["items"][0]
    frozen = revision["source_refs"][0]
    assert (frozen["title"], frozen["locator_label"]) == ("Lecture_04.pdf", "p. 2")

    world.execute("UPDATE documents SET title = 'Renamed.pdf' WHERE id = :d", d=doc)
    draft = x.add(x.exam(), q)
    editor_ref = draft["items"][0]["question"]["source_refs"][0]
    assert (editor_ref["title"], editor_ref["available"]) == ("Renamed.pdf", True)
    reviewed = x.preview(draft, "answer_key")

    world.execute("DELETE FROM documents WHERE id = :d", d=doc)
    editor_ref = x.get(draft)["items"][0]["question"]["source_refs"][0]
    assert (editor_ref["title"], editor_ref["available"]) == (None, False)
    after_removal = x.preview(draft, "answer_key")
    final = x.finalized(draft, fingerprint=reviewed["preview_fingerprint"])
    official = x.preview(final, "answer_key")
    assert official["answer_key"][0]["sources"] == [f"{frozen['title']}, {frozen['locator_label']}"]
    assert reviewed["answer_key"] == official["answer_key"]
    assert after_removal == reviewed
    assert reviewed["questions"] == official["questions"]
    assert draft["content_chars"] == final["content_chars"]
    final_ref = final["items"][0]["question"]["source_refs"][0]
    assert (final_ref["title"], final_ref["available"]) == (frozen["title"], False)


def test_f07_edit_revokes_approval_and_reapproval_selects_the_new_revision(fx):
    x, world, doc = fx["x"], fx["world"], fx["document"]
    body = mcq_body(source_refs=[{"document_id": str(doc), "locator": {"page": 2}}])
    q = x.question(body, approve=True)
    old_final = x.finalized(x.add(x.exam(), q))
    draft = x.add(x.exam("Second exam"), q)
    old_review = x.preview(draft, "answer_key")
    world.execute("UPDATE documents SET title = 'Second.pdf' WHERE id = :d", d=doc)
    edited = x.edit(q, {**body, "stem": "Updated working copy?"})
    review = x.preview(draft, "answer_key")
    assert review["questions"][0]["stem"] == "Updated working copy?"
    assert review["answer_key"][0]["sources"] == ["Second.pdf, p. 2"]
    assert x.preview(draft)["questions"] == review["questions"]
    assert x.finalize(draft, fingerprint=review["preview_fingerprint"]).json()["code"] == "exam_not_ready"
    x.approve(edited)
    world.execute("UPDATE documents SET title = 'Third.pdf' WHERE id = :d", d=doc)
    assert x.finalize(draft, fingerprint=old_review["preview_fingerprint"]).json()["code"] == "exam_preview_stale"
    reviewed = x.preview(draft, "answer_key")
    final = x.finalized(draft, fingerprint=reviewed["preview_fingerprint"])
    assert final["items"][0]["pinned_revision"] == 2
    assert reviewed["answer_key"] == x.preview(final, "answer_key")["answer_key"] == review["answer_key"]
    assert x.preview(old_final, "answer_key")["answer_key"] == old_review["answer_key"]


def test_f07_pre_fix_draft_requires_review_but_committed_finalize_can_replay(fx, monkeypatch):
    x = fx["x"]
    ref = {"document_id": str(fx["document"]), "locator": {"page": 2}}
    q = x.question(mcq_body(source_refs=[ref]), approve=True)
    fx["world"].execute("UPDATE documents SET title = 'Renamed.pdf' WHERE id = :d", d=fx["document"])
    draft = x.add(x.exam(), q)
    completed = x.add(x.exam("Already finalized"), q)

    # Mint both intents with the old canonical input, exactly as an older server did.
    current_object = snapshot.fingerprint_object
    with monkeypatch.context() as old_server:
        old_server.setattr(snapshot, "fingerprint_object", lambda *args: {**current_object(*args), "v": 1})
        old_review = x.preview(draft, "answer_key")["preview_fingerprint"]
        completed_review = x.preview(completed, "answer_key")["preview_fingerprint"]
        x.finalized(completed, fingerprint=completed_review)

    stale = x.finalize(draft, fingerprint=old_review)
    assert stale.status_code == 412 and stale.json()["code"] == "exam_preview_stale"
    assert x.get(draft)["status"] == "draft"
    fresh = x.preview(draft, "answer_key")
    assert x.finalized(draft, fingerprint=fresh["preview_fingerprint"])["status"] == "finalized"
    replay = x.finalize(completed, fingerprint=completed_review)
    assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
