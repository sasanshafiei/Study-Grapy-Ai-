"""Pure functions of the exam builder (no database): fingerprint, hashing, validation, cursors, preconditions."""

from __future__ import annotations

import inspect
import json
import re
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.errors import ApiError
from app.exams import http, snapshot
from app.exams.validation import copy_title, parse_question, parse_reason
from tests.exam_support import mcq_body, short_body, tf_body

CANARY = "ANSWERONLY-CANARY"


def _request(headers: dict[str, str] | list, query: str = "") -> Request:
    items = headers.items() if isinstance(headers, dict) else headers
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": query.encode(),
                    "headers": [(k.lower().encode(), v.encode()) for k, v in items]})  # fmt: skip


def _exam(**overrides):
    values = {"id": uuid.uuid4(), "version": 7, "status": "draft", "title": "Midterm", "instructions": None,
              "duration_minutes": 90, **overrides}  # fmt: skip
    return SimpleNamespace(**values)


def test_fp10_fingerprint_object_is_canonical_versioned_and_content_free():
    exam = _exam()
    course = SimpleNamespace(id=uuid.uuid4(), code="CHEM 101", name="General Chemistry")
    item = SimpleNamespace(id=uuid.uuid4(), position=1, points=2, blueprint_row_id=None, question_id=uuid.uuid4(),
                           question_status="approved", approved_revision_no=1, approved_content_sha256="a" * 64)  # fmt: skip
    obj = snapshot.fingerprint_object(exam, course, [], [item])
    assert set(obj) == {"v", "exam", "course", "items", "blueprint"} and obj["v"] == 1
    assert set(obj["items"][0]) == {"id", "position", "points", "blueprint_row_id", "question_id", "question_status",
                                    "approved_revision_no", "approved_content_sha256"}  # fmt: skip
    digest = snapshot.digest(obj)
    assert re.fullmatch(r"pf1:[0-9a-f]{64}", digest) and len(digest) == 68
    assert snapshot.digest(json.loads(json.dumps(obj))) == digest  # stable across serialization
    assert snapshot.digest(snapshot.fingerprint_object(_exam(id=exam.id, version=8), course, [], [item])) != digest
    source = inspect.getsource(snapshot.fingerprint_input)
    for column in ("text", "answer_key", "explanation", "choices", "stem", "citations", "source_refs"):
        assert not re.search(rf"\b(q|r)\.{column}\b", source), column
    # the state-based builder (finalize, under locks) produces the same object without question content
    question = SimpleNamespace(id=item.question_id, status="approved", approved_revision_no=1, text=CANARY,
                               answer_key={"model_answer": CANARY}, explanation=CANARY)  # fmt: skip
    state = snapshot.ExamState(exam, course, [], [snapshot.ItemState(item, question, SimpleNamespace(content_sha256="a" * 64), None)])
    from_state = snapshot.state_fingerprint_object(state)
    assert from_state == obj and CANARY not in json.dumps(from_state)


def test_canonical_hash_is_independent_of_true_false_request_form():
    a = parse_question(tf_body())
    b = parse_question(tf_body(choices=[{"id": "true", "text": "True"}, {"id": "false", "text": "False"}]))
    assert a == b and a.content_hash() == b.content_hash()
    assert parse_question(mcq_body()).content_hash() != parse_question(mcq_body(stem="Other?")).content_hash()
    assert parse_question(short_body(answer_key={"model_answer": " M ", "rubric": "   "})).answer_key == {"model_answer": "M", "rubric": None}
    assert http.canonical_json({"b": 1, "a": "ü"}) == '{"a":"ü","b":1}'.encode()


@pytest.mark.parametrize("raw,expected", [('"7"', 7), (' "0" ', 0), ('"123456789"', 123456789)])
def test_if_match_accepts_one_strong_tag(raw, expected):
    assert http.if_match(_request({"If-Match": raw})) == expected


@pytest.mark.parametrize("headers,code", [
    ({}, "precondition_required"), ({"If-Match": 'W/"7"'}, "invalid_precondition"), ({"If-Match": "*"}, "invalid_precondition"),
    ({"If-Match": '"7", "8"'}, "invalid_precondition"), ({"If-Match": '"07"'}, "invalid_precondition"),
    ({"If-Match": '"1234567890"'}, "invalid_precondition"), ([("If-Match", '"1"'), ("If-Match", '"1"')], "invalid_precondition"),
])  # fmt: skip
def test_if_match_refuses_everything_else(headers, code):
    with pytest.raises(ApiError) as raised:
        http.if_match(_request(headers))
    assert raised.value.code == code


def test_cursor_round_trip_and_binding():
    user, course = uuid.uuid4(), uuid.uuid4()
    from datetime import datetime, timezone

    when = datetime(2026, 11, 21, 10, 0, 0, 123456, tzinfo=timezone.utc)
    last = uuid.uuid4()
    cursor = http.encode_cursor("e", user, course, None, {"status": ""}, when, last)
    assert len(cursor) <= 512 and "=" not in cursor
    assert http.decode_cursor(cursor, "e", user, course, None, {"status": ""}) == (when, last)
    for args in (("f", user, course, None, {"status": ""}), ("e", uuid.uuid4(), course, None, {"status": ""}),
                 ("e", user, uuid.uuid4(), None, {"status": ""}), ("e", user, course, uuid.uuid4(), {"status": ""}),
                 ("e", user, course, None, {"status": "draft"})):  # fmt: skip
        with pytest.raises(ApiError) as raised:
            http.decode_cursor(cursor, *args)
        assert raised.value.code == "invalid_cursor"
    import base64

    for document in ({"v": 2, "k": "x", "id": str(last), "b": "x"}, {"v": 1, "k": "x", "id": str(last)}, [1],
                     {"v": True, "k": "2026-11-21T10:00:00.000000Z", "id": str(last), "b": "x"}):  # fmt: skip
        forged = base64.urlsafe_b64encode(json.dumps(document).encode()).decode().rstrip("=")
        with pytest.raises(ApiError):
            http.decode_cursor(forged, "e", user, course, None, {"status": ""})


def test_query_params_refuse_unknown_and_repeated():
    assert http.query_params(_request({}, "limit=5"), frozenset({"limit"})) == {"limit": "5"}
    for query in ("limit=5&limit=6", "other=1"):
        with pytest.raises(ApiError) as raised:
            http.query_params(_request({}, query), frozenset({"limit"}))
        assert raised.value.code == "invalid_query"


def test_titles_and_reasons():
    assert copy_title("Midterm") == "Midterm (copy)"
    assert copy_title("t" * 200) == "t" * 193 + " (copy)" and len(copy_title("t" * 200)) == 200
    assert parse_reason(None) is None and parse_reason("  ten chars!  ") == "ten chars!"
    for bad in ("short", "x" * 501, 5, "bad\x00reason here"):
        with pytest.raises(ApiError) as raised:
            parse_reason(bad)
        assert raised.value.code == "invalid_blueprint_override"
