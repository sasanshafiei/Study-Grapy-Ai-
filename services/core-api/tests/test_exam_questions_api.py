"""Question bank API (E1-E7): validation, true/false normalization, approval revisions, replays, pagination.

Ids in test names refer to the M9 test plan (VAL-*, IDEM-*, CUR-*, SRC-*, CONC-01).
"""

from __future__ import annotations

import copy
import json
import uuid

import pytest

from tests import exam_support as ex
from tests.exam_support import ExamClient, key, match, mcq_body, short_body, tf_body


@pytest.fixture
def bank(world, make_api):
    professor = world.user("professor", name="Prof. Ada")
    course = world.course(professor, code="CHEM 101")
    topic = world.topic(course, "Stoichiometry")
    other_course = world.course(professor, code="PHYS 1")
    other_topic = world.topic(other_course, "Kinematics")
    document = world.document(course, title="Lecture_04_Stoichiometry.pdf", status="indexed", page_count=12)
    api = make_api(settings_overrides=ex.ENABLED).as_user(professor)
    return {"world": world, "api": api, "x": ExamClient(api, course), "course": course, "topic": topic,
            "other_course": other_course, "other_topic": other_topic, "document": document, "professor": professor}  # fmt: skip


def _invalid(response, reason=None, code="invalid_question"):
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == code
    if reason is not None:
        assert body["reason"] == reason, body
    assert response.headers["cache-control"] == "no-store"


def test_create_read_list_and_no_store(bank):
    x = bank["x"]
    created = bank["api"].client.post(x.q_path(), json=mcq_body(topic_id=str(bank["topic"])), headers=key())
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store" and created.headers["pragma"] == "no-cache"
    q = created.json()
    assert created.headers["location"].endswith(f"/exam-questions/{q['id']}")
    assert (q["status"], q["version"], q["approved_revision"], q["topic"]["name"]) == ("draft", 1, None, "Stoichiometry")
    assert q["answer_key"] == {"choice_id": "c"} and q["used_in"] == {"draft_exams": 0, "finalized_exams": 0}
    read = bank["api"].client.get(x.q_path(q["id"]))
    assert read.status_code == 200 and read.headers["etag"] == '"1"' and read.json() == q
    listed = bank["api"].client.get(x.q_path()).json()
    assert [i["id"] for i in listed["items"]] == [q["id"]] and listed["next_cursor"] is None
    assert set(listed["items"][0]) == {"id", "status", "version", "type", "difficulty", "bloom_level", "topic",
                                       "stem_preview", "approved_revision", "updated_at"}  # no answer key, no explanation


def test_val07_true_false_normalization(bank):
    x, c = bank["x"], bank["api"].client
    canonical = [{"id": "true", "text": "True"}, {"id": "false", "text": "False"}]
    a = x.question(tf_body())
    b = x.question(tf_body(choices=canonical))
    assert a["choices"] == b["choices"] == canonical and a["content_sha256"] == b["content_sha256"]
    same = c.put(x.q_path(a["id"]), json=tf_body(choices=canonical), headers=match(1))  # differs only in that respect
    assert same.status_code == 200 and same.json()["question"]["version"] == 1 and same.json()["approval_revoked"] is False
    for choices in (list(reversed(canonical)), [{"id": "true", "text": "Yes"}, {"id": "false", "text": "No"}],
                    canonical[:1], [], [{"id": "t", "text": "True"}, {"id": "f", "text": "False"}]):  # fmt: skip
        _invalid(c.post(x.q_path(), json=tf_body(choices=choices), headers=key()), "true_false_choices")
    _invalid(c.post(x.q_path(), json=mcq_body(choices=None), headers=key()), "type_shape")
    _invalid(c.post(x.q_path(), json=short_body(choices=canonical), headers=key()), "type_shape")
    approved = x.approve(a)
    revisions = c.get(x.q_path(a["id"], "/revisions")).json()["items"]
    assert approved["choices"] == revisions[0]["choices"] == canonical


@pytest.mark.parametrize("change,reason", [
    ({"stem": "x" * 4001}, "length"), ({"stem": "   "}, "length"), ({"stem": "bad\x00"}, "control_characters"),
    ({"stem": "bad\ud800"}, "control_characters"), ({"explanation": "e" * 4001}, "length"),
    ({"choices": [{"id": "a", "text": "A"}]}, "choice_count"),
    ({"choices": [{"id": x, "text": x} for x in "abcdefg"]}, "choice_count"),
    ({"choices": [{"id": "b", "text": "A"}, {"id": "a", "text": "B"}]}, "choice_ids"),
    ({"choices": [{"id": "a", "text": "Same"}, {"id": "b", "text": " same "}]}, "duplicate_choice"),
    ({"choices": [{"id": "a", "text": "ﬁ"}, {"id": "b", "text": "fi"}]}, "duplicate_choice"),
    ({"choices": [{"id": "a", "text": "two\nlines"}, {"id": "b", "text": "B"}]}, "control_characters"),
    ({"choices": [{"id": "a", "text": "x" * 501}, {"id": "b", "text": "B"}]}, "length"),
    ({"answer_key": {"choice_id": "e"}}, "answer_key"), ({"answer_key": {"choice_id": "a", "x": 1}}, "answer_key"),
    ({"type": "essay"}, "type_shape"), ({"difficulty": "extreme"}, "type_shape"), ({"bloom_level": "memorize"}, "type_shape"),
    ({"topic_id": "not-a-uuid"}, "topic"), ({"topic_id": str(uuid.uuid4()).upper()}, "topic"),
    ({"source_refs": [{"document_id": str(uuid.uuid4()), "locator": {"page": True}}]}, "locator"),
    ({"source_refs": [{"document_id": str(uuid.uuid4()), "locator": {"page": 1, "x": 2}}]}, "locator"),
    ({"source_refs": [{"document_id": str(uuid.uuid4()), "locator": {"page": 1}}] * 5}, "source_ref"),
])  # fmt: skip
def test_val02_val03_bounds_and_shapes(bank, change, reason):
    raw = json.dumps(mcq_body(**change))  # ASCII-escaped, so a lone surrogate arrives as \ud800
    _invalid(bank["api"].client.post(bank["x"].q_path(), content=raw, headers={**key(), "Content-Type": "application/json"}),
             reason)


def test_val02_edges_accepted(bank):
    x = bank["x"]
    six = [{"id": c, "text": f"Option {c}"} for c in "abcdef"]
    q = x.question(mcq_body(stem="s" * 4000, choices=six, answer_key={"choice_id": "f"}, explanation="e" * 4000))
    assert len(q["stem"]) == 4000 and len(q["choices"]) == 6
    q2 = x.question(mcq_body(stem="  Trimmed\tstem with tab  ", choices=[{"id": "a", "text": " A "}, {"id": "b", "text": "B"}],
                             answer_key={"choice_id": "a"}))  # fmt: skip
    assert q2["stem"] == "Trimmed\tstem with tab" and q2["choices"][0]["text"] == "A"
    s = x.question(short_body(answer_key={"model_answer": "m" * 4000, "rubric": None}))
    assert s["answer_key"] == {"model_answer": "m" * 4000, "rubric": None} and s["choices"] is None


def test_val01_strict_json_and_exact_keys(bank):
    c, path = bank["api"].client, bank["x"].q_path()
    body = mcq_body()
    missing = dict(body)
    del missing["explanation"]
    _invalid(c.post(path, json=missing, headers=key()), code="invalid_fields")
    _invalid(c.post(path, json={**body, "extra": 1}, headers=key()), code="invalid_fields")
    raw = '{"type": "mcq", "type": "mcq"}'
    r = c.post(path, content=raw, headers={**key(), "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_body"
    r = c.post(path, content='{"a": NaN}', headers={**key(), "Content-Type": "application/json"})
    assert r.status_code == 400
    r = c.post(path, content=b"{}", headers={**key(), "Content-Type": "text/plain"})
    assert r.status_code == 415
    r = c.post(path, content=b"{}", headers={**key(), "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 415 and r.json()["code"] == "unsupported_encoding"
    r = c.post(path, content=b'{"x": "' + b"y" * 70000 + b'"}', headers={**key(), "Content-Type": "application/json"})
    assert r.status_code == 413
    _invalid(c.post(path, json=mcq_body(difficulty=True), headers=key()), "type_shape")
    for headers, code in (({}, "idempotency_key_required"), ({"Idempotency-Key": "nope"}, "invalid_idempotency_key"),
                          ({"Idempotency-Key": str(uuid.uuid4()).upper()}, "invalid_idempotency_key")):  # fmt: skip
        r = c.post(path, json=body, headers=headers)
        assert r.status_code == 400 and r.json()["code"] == code


def test_val04_source_references(bank):
    world, x, c = bank["world"], bank["x"], bank["api"].client
    ok = {"document_id": str(bank["document"]), "locator": {"page": 12}}
    q = x.question(mcq_body(source_refs=[ok]))
    assert q["source_refs"] == [{**ok, "title": "Lecture_04_Stoichiometry.pdf", "locator_label": "p. 12", "available": True}]
    foreign = world.document(bank["other_course"], title="Other.pdf")
    external = world.document(bank["course"], origin="external", title="Wiki")
    for ref, reason in (({**ok, "locator": {"page": 13}}, "locator"), ({**ok, "document_id": str(foreign)}, "source_ref"),
                        ({**ok, "document_id": str(external)}, "source_ref"),
                        ({**ok, "document_id": str(uuid.uuid4())}, "source_ref")):  # fmt: skip
        _invalid(c.post(x.q_path(), json=mcq_body(source_refs=[ref]), headers=key()), reason)
    _invalid(c.post(x.q_path(), json=mcq_body(topic_id=str(bank["other_topic"])), headers=key()), "topic")


def test_idem01_replay_conflict_and_per_user_scope(bank, make_api):
    world, x, c = bank["world"], bank["x"], bank["api"].client
    headers = key()
    first = c.post(x.q_path(), json=mcq_body(), headers=headers)
    assert first.status_code == 201
    x.approve(first.json())
    replay = c.post(x.q_path(), json=mcq_body(), headers=headers)
    assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"] and replay.json()["status"] == "approved"  # current representation
    other = c.post(x.q_path(), json=mcq_body(stem="Different?"), headers=headers)
    assert other.status_code == 409 and other.json()["code"] == "idempotency_key_reused"
    professor2 = world.user("professor")
    course2 = world.course(professor2)
    api2 = make_api(settings_overrides=ex.ENABLED).as_user(professor2)
    independent = api2.client.post(f"/api/v1/courses/{course2}/exam-questions", json=mcq_body(), headers=headers)
    assert independent.status_code == 201 and independent.json()["id"] != first.json()["id"]


def test_replace_approve_revoke_and_revisions(bank):
    x, c = bank["x"], bank["api"].client
    q = x.question()
    stale = c.put(x.q_path(q["id"]), json=mcq_body(stem="v2?"), headers=match(5))
    assert stale.status_code == 412 and stale.json() == {"detail": stale.json()["detail"], "code": "version_mismatch",
                                                         "current_version": 1}  # fmt: skip
    for header, code in (({}, "precondition_required"), ({"If-Match": 'W/"1"'}, "invalid_precondition"),
                         ({"If-Match": "*"}, "invalid_precondition"), ({"If-Match": '"1", "2"'}, "invalid_precondition"),
                         ({"If-Match": "1"}, "invalid_precondition")):  # fmt: skip
        r = c.put(x.q_path(q["id"]), json=mcq_body(), headers=header)
        assert r.status_code in (400, 428) and r.json()["code"] == code
    approved = x.approve(q)
    assert (approved["status"], approved["version"], approved["approved_revision"]) == ("approved", 2, 1)
    edited = c.put(x.q_path(q["id"]), json=mcq_body(stem="Edited?"), headers=match(2))
    assert edited.status_code == 200 and edited.json()["approval_revoked"] is True
    e = edited.json()["question"]
    assert (e["status"], e["version"], e["approved_revision"]) == ("draft", 3, 1)
    again = x.approve(e)
    assert again["approved_revision"] == 2
    revisions = c.get(x.q_path(q["id"], "/revisions")).json()
    assert [r["revision"] for r in revisions["items"]] == [2, 1]
    assert revisions["items"][1]["stem"] == q["stem"] and revisions["items"][0]["stem"] == "Edited?"
    assert revisions["items"][0]["approved_by_name"] == "Prof. Ada"
    history = c.get(x.q_path(q["id"], "/history")).json()["items"]
    assert [h["action"] for h in history] == ["question_approved", "question_approval_revoked", "question_updated",
                                              "question_approved", "question_created"]  # fmt: skip
    assert all(set(h["details"]) <= {"revision_no", "content_sha256", "previous_content_sha256"} for h in history)


def test_idem03_transition_replays(bank):
    x, c = bank["x"], bank["api"].client
    q = x.question()
    first = c.post(x.q_path(q["id"], "/approve"), headers=match(1))
    assert first.status_code == 200 and "idempotent-replayed" not in first.headers
    lost = c.post(x.q_path(q["id"], "/approve"), headers=match(1))  # lost response, retried
    assert lost.status_code == 200 and lost.headers["idempotent-replayed"] == "true" and lost.json()["replayed_revision"] == 1
    same = c.post(x.q_path(q["id"], "/approve"), headers=match(2))  # already approved at that version: no bump
    assert same.status_code == 200 and same.json()["version"] == 2 and same.json()["replayed_revision"] == 1
    x.edit(same.json(), mcq_body(stem="Later edit?"))  # an unrelated later change
    replay = c.post(x.q_path(q["id"], "/approve"), headers=match(1))
    assert replay.status_code == 200 and replay.json()["status"] == "draft" and replay.json()["replayed_revision"] == 1
    stale = c.post(x.q_path(q["id"], "/approve"), headers=match(2))  # 2 -> 3 was an edit, not an approval
    assert stale.status_code == 412 and stale.json()["current_version"] == 3
    retired = c.post(x.q_path(q["id"], "/retire"), headers=match(3))
    assert retired.status_code == 200 and retired.json()["status"] == "retired"
    again = c.post(x.q_path(q["id"], "/retire"), headers=match(3))
    assert again.status_code == 200 and again.headers["idempotent-replayed"] == "true" and again.json()["version"] == 4
    for path, body in ((x.q_path(q["id"], "/approve"), None), (x.q_path(q["id"]), mcq_body())):
        r = c.post(path, headers=match(4)) if body is None else c.put(path, json=body, headers=match(4))
        assert r.status_code == 409 and r.json()["code"] == "question_retired"
    r = c.post(x.q_path(q["id"], "/retire"), headers=match(4), json={"x": 1})
    assert r.status_code == 422 and r.json()["code"] == "invalid_fields"


def test_src01_approval_freezes_source_titles(bank):
    world, x, c = bank["world"], bank["x"], bank["api"].client
    ref = {"document_id": str(bank["document"]), "locator": {"page": 3}}
    q = x.question(mcq_body(source_refs=[ref]), approve=True)
    world.execute("UPDATE documents SET title = 'Renamed.pdf' WHERE id = :d", d=bank["document"])
    revision = c.get(x.q_path(q["id"], "/revisions")).json()["items"][0]
    assert revision["source_refs"] == [{"document_id": ref["document_id"], "title": "Lecture_04_Stoichiometry.pdf",
                                        "locator_label": "p. 3"}]  # fmt: skip
    assert c.get(x.q_path(q["id"])).json()["source_refs"][0]["title"] == "Renamed.pdf"
    gone = x.question(mcq_body(stem="Another?", source_refs=[ref]))
    world.execute("UPDATE documents SET course_id = :o WHERE id = :d", o=bank["other_course"], d=bank["document"])
    view = c.get(x.q_path(gone["id"])).json()
    assert view["source_refs"][0]["available"] is False and view["source_refs"][0]["title"] is None
    refused = c.post(x.q_path(gone["id"], "/approve"), headers=match(1))
    _invalid(refused, "source_ref_unavailable")
    assert c.get(x.q_path(gone["id"])).json()["version"] == 1
    world.execute("UPDATE documents SET course_id = :c WHERE id = :d", c=bank["course"], d=bank["document"])


def test_cur01_val06_pagination_filters_and_cursor_binding(bank, make_api):
    world, x, c = bank["world"], bank["x"], bank["api"].client
    made = [x.question(mcq_body(stem=f"Question {i} 100%_\\?")) for i in range(5)]
    x.question(tf_body(), approve=True)
    page1 = c.get(x.q_path(), params={"limit": 2, "type": "mcq"}).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = c.get(x.q_path(), params={"limit": 2, "type": "mcq", "cursor": page1["next_cursor"]}).json()
    page3 = c.get(x.q_path(), params={"limit": 2, "type": "mcq", "cursor": page2["next_cursor"]}).json()
    seen = [i["id"] for i in page1["items"] + page2["items"] + page3["items"]]
    assert sorted(seen) == sorted(q["id"] for q in made) and page3["next_cursor"] is None
    for params in ({"limit": 2, "type": "true_false", "cursor": page1["next_cursor"]}, {"limit": 2, "cursor": page1["next_cursor"]},
                   {"cursor": "!!"}, {"cursor": "e30"}, {"cursor": "x" * 600}):  # fmt: skip
        r = c.get(x.q_path(), params=params)
        assert r.status_code == 422 and r.json()["code"] in ("invalid_cursor", "invalid_query"), params
    other = world.user("professor")
    api2 = make_api(settings_overrides=ex.ENABLED).as_user(other)
    course2 = world.course(other)
    r = api2.client.get(f"/api/v1/courses/{course2}/exam-questions", params={"type": "mcq", "limit": 2, "cursor": page1["next_cursor"]})
    assert r.status_code == 422 and r.json()["code"] == "invalid_cursor"
    for query in ("limit=0", "limit=101", "limit=x", "type=essay", "unknown=1", "type=mcq&type=mcq", "q=", "topic_id=nope"):
        r = c.get(x.q_path() + "?" + query)
        assert r.status_code == 422 and r.json()["code"] == "invalid_query", query
    found = c.get(x.q_path(), params={"q": "100%_\\"}).json()["items"]
    assert len(found) == 5
    assert c.get(x.q_path(), params={"q": "1%0"}).json()["items"] == []  # % is literal
    assert [i["type"] for i in c.get(x.q_path(), params={"status": "approved"}).json()["items"]] == ["true_false"]


def test_conc01_two_writers_same_version(bank):
    x, c = bank["x"], bank["api"].client
    q = x.question()
    a = c.put(x.q_path(q["id"]), json=mcq_body(stem="A?"), headers=match(1))
    b = c.put(x.q_path(q["id"]), json=mcq_body(stem="B?"), headers=match(1))
    assert (a.status_code, b.status_code) == (200, 412) and b.json()["current_version"] == 2


def test_limits_and_not_found(bank, make_api):
    world = bank["world"]
    api = make_api(settings_overrides={**ex.ENABLED, "exam_max_bank_questions": 2}).as_user(bank["professor"])
    x = ExamClient(api, bank["course"])
    x.question()
    x.question(tf_body())
    r = api.client.post(x.q_path(), json=short_body(), headers=key())
    assert r.status_code == 409 and r.json()["code"] == "question_limit_reached"
    legacy = world._one("INSERT INTO questions (course_id, purpose, qtype, difficulty, text, status) "
                        "VALUES (:c, 'exam', 'mcq', 'easy', 'Legacy', 'approved') RETURNING id", c=bank["course"])  # fmt: skip
    for qid in (str(legacy), str(uuid.uuid4()), "nope", str(uuid.uuid4()).upper()):
        r = api.client.get(x.q_path(qid))
        assert r.status_code == 404 and r.json()["code"] == "question_not_found"


def test_body_is_deep_copied_not_trusted(bank):
    body = mcq_body()
    snapshot = copy.deepcopy(body)
    bank["x"].question(body)
    assert body == snapshot
