"""M7 units without infrastructure: protocol validation, negotiation, settings, shuffling, request parsing."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections import Counter

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import ApiError
from app.quiz import generation
from app.quiz.client import AiQuizClient, QuizUpstreamError
from app.quiz.protocol import (
    ProposedQuestion,
    ProtocolViolation,
    build_request,
    negotiate_types,
    validate_capabilities,
    validate_response,
)
from app.quiz.views import trend
from app.routers.quizzes import parse_attempt_request, parse_quiz_request
from tests.fake_quiz_service import Source, default_quiz_capabilities, quiz_for

COURSE = uuid.uuid4()
TOPICS = [(uuid.uuid4(), "Stoichiometry"), (uuid.uuid4(), "Kinetics")]
SOURCE = Source(uuid.uuid4(), uuid.uuid4(), "The limiting reagent is consumed first in the reaction.")


def _request(count=4, difficulty="medium", types=("mcq", "true_false")) -> dict:
    return build_request(uuid.uuid4(), COURSE, "class_only", TOPICS, count, difficulty, types)


def _valid(request=None) -> tuple[dict, dict]:
    request = request or _request()
    return request, quiz_for(request, [SOURCE])


def test_a_valid_response_is_accepted_in_full():
    request, body = _valid()
    quiz = validate_response(body, request)
    assert len(quiz.questions) == 4 and quiz.model_name == "test-generator"
    assert {q.qtype for q in quiz.questions} == {"mcq", "true_false"}


def _mutate(body: dict, path: tuple, value) -> dict:
    body = copy.deepcopy(body)
    target = body
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return body


@pytest.mark.parametrize(
    "path, value, reason",
    [
        (("mock",), True, "mock_response"),
        (("mock",), KeyError, "response_keys"),
        (("extra",), 1, "response_keys"),
        (("request_id",), str(uuid.uuid4()), "echo_request_id"),
        (("course_id",), str(uuid.uuid4()), "echo_course_id"),
        (("protocol",), "studygraph.quiz/2", "echo_protocol"),
        (("scope",), "class_plus_external", "echo_scope"),
        (("grounded",), False, "not_grounded"),
        (("model", "name"), "", "model"),
        (("questions",), [], "question_count"),
        (("questions", 0, "topic_id"), str(uuid.uuid4()), "question_topic"),
        (("questions", 0, "type"), "short", "question_type"),
        (("questions", 0, "difficulty"), "hard", "question_difficulty_mismatch"),
        (("questions", 0, "stem"), "x" * 1001, "question_stem"),
        (("questions", 0, "stem"), "bad\x00stem", "question_stem"),
        (("questions", 0, "hint"), " ", "question_hint"),
        (("questions", 0, "explanation"), None, "question_explanation"),
        (("questions", 0, "choices"), ["only one"], "mcq_choices"),
        (("questions", 0, "choices"), ["Same", "same ", "Other"], "mcq_duplicate_choice"),
        (("questions", 0, "answer"), 4, "mcq_answer"),
        (("questions", 0, "answer"), True, "mcq_answer"),
        (("questions", 0, "hint"), "It is: correct claim 1", "answer_leak"),
        (("questions", 1, "choices"), ["True", "False"], "true_false_choices"),
        (("questions", 1, "answer"), 1, "true_false_answer"),
        (("questions", 0, "citations"), [], "citation_count"),
        (("questions", 0, "citations", 0, "origin"), "external", "citation_origin"),
        (("questions", 0, "citations", 0, "origin"), "web", "citation_origin"),
        (("questions", 0, "citations", 0, "chunk_id"), "NOT-A-UUID", "citation_identity"),
        (("questions", 0, "citations", 0, "locator"), {"page": "1"}, "citation_locator"),
        (("questions", 0, "citations", 0, "extra"), 1, "citation_keys"),
        (("questions", 0, "extra"), 1, "question_keys"),
    ],
)
def test_any_violation_rejects_the_whole_response(path, value, reason):
    request, body = _valid()
    with pytest.raises(ProtocolViolation) as caught:
        validate_response(_mutate(body, path, value), request)
    assert caught.value.reason == reason


def test_duplicate_stems_and_citations_are_rejected():
    request, body = _valid()
    body["questions"][1]["stem"] = body["questions"][0]["stem"].upper()
    with pytest.raises(ProtocolViolation, match="duplicate_stem"):
        validate_response(body, request)
    request, body = _valid()
    body["questions"][0]["citations"].append(dict(body["questions"][0]["citations"][0]))
    with pytest.raises(ProtocolViolation, match="citation_duplicate"):
        validate_response(body, request)


def test_mixed_difficulty_accepts_each_level_and_types_must_be_negotiated():
    request = _request(difficulty="mixed", types=("mcq",))
    body = quiz_for(request, [SOURCE], types=("mcq",))
    assert {q.difficulty for q in validate_response(body, request).questions} == {"easy", "medium", "hard"}
    with pytest.raises(ProtocolViolation, match="question_type"):
        validate_response(quiz_for(request, [SOURCE], types=("true_false",)), request)


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"mock": True}, "mock_service"),
        ({"mock": None}, "mock_service"),
        ({"protocol": "studygraph.chat/1"}, "protocol_mismatch"),
        ({"service": "other"}, "capabilities_invalid"),
        ({"question_types": ["mcq", "mcq"]}, "capabilities_invalid"),
        ({"max_questions": True}, "capabilities_invalid"),
        ({"scopes": ["everything"]}, "capabilities_invalid"),
    ],
)
def test_capabilities_refuse_mocks_and_malformed_answers(change, reason):
    assert validate_capabilities(200, {**default_quiz_capabilities(), **change}).reason == reason
    missing = default_quiz_capabilities()
    del missing["mock"]
    assert validate_capabilities(200, missing).reason == "capabilities_invalid"
    assert validate_capabilities(503, None).reason == "capabilities_http_503"


def test_negotiation_keeps_only_supported_types():
    caps = validate_capabilities(200, {**default_quiz_capabilities(), "question_types": ["short", "true_false"]})
    assert negotiate_types(caps) == ("true_false",)
    caps = validate_capabilities(200, {**default_quiz_capabilities(), "question_types": ["short"]})
    assert negotiate_types(caps) == ()


def test_settings_default_off_and_validated():
    s = Settings()
    assert (s.quiz_enabled, s.harvest_enabled, s.chat_enabled, s.indexing_enabled) == (False, False, False, False)
    assert s.quiz_lease_seconds > s.quiz_pipeline_deadline_seconds
    for bad in ({"quiz_enabled_scopes": "class_plus_external"}, {"quiz_enabled_scopes": "class_plus_web"},
                {"quiz_probe_deadline_seconds": 30, "quiz_generate_deadline_seconds": 40,
                 "quiz_pipeline_deadline_seconds": 60}):  # fmt: skip
        with pytest.raises(ValidationError):
            Settings(**bad)


def _question(qtype="mcq", answer_index=2) -> ProposedQuestion:
    choices = ("w", "x", "y", "z") if qtype == "mcq" else ()
    return ProposedQuestion(uuid.uuid4(), qtype, "easy", "Stem", choices, answer_index if qtype == "mcq" else None,
                            True if qtype == "true_false" else None, "Hint", "Expl", ())  # fmt: skip


def test_choices_are_shuffled_with_a_csprng_and_labels_do_not_encode_correctness():
    labels = Counter()
    orders = set()
    for _ in range(800):
        choices, key = generation._shuffled(_question())
        assert [c["id"] for c in choices] == ["a", "b", "c", "d"]
        assert next(c["text"] for c in choices if c["id"] == key) == "y"
        labels[key] += 1
        orders.add(tuple(c["text"] for c in choices))
    assert set(labels) == {"a", "b", "c", "d"} and min(labels.values()) > 120, labels
    assert len(orders) == 24
    choices, key = generation._shuffled(_question("true_false"))
    assert [c["id"] for c in choices] == ["true", "false"] and key == "true"


def test_fingerprint_covers_course_topics_count_difficulty_scope():
    key = uuid.uuid4()
    base = generation.QuizRequest(COURSE, key, tuple(sorted(t for t, _ in TOPICS)), 3, "easy", "class_only")
    same = generation.QuizRequest(COURSE, uuid.uuid4(), base.topic_ids, 3, "easy", "class_only")
    assert base.fingerprint == same.fingerprint
    for other in (
        generation.QuizRequest(uuid.uuid4(), key, base.topic_ids, 3, "easy", "class_only"),
        generation.QuizRequest(COURSE, key, base.topic_ids[:1], 3, "easy", "class_only"),
        generation.QuizRequest(COURSE, key, base.topic_ids, 4, "easy", "class_only"),
        generation.QuizRequest(COURSE, key, base.topic_ids, 3, "mixed", "class_only"),
    ):
        assert other.fingerprint != base.fingerprint


def test_quiz_request_parsing_normalizes_topic_order_and_is_strict():
    s = Settings()
    t1, t2 = sorted(str(t) for t, _ in TOPICS)
    key = uuid.uuid4()
    req = parse_quiz_request({"topic_ids": [t2, t1], "count": 2, "difficulty": "easy"}, COURSE, key, s)
    assert [str(t) for t in req.topic_ids] == [t1, t2] and req.scope == "class_only"
    for body in (
        {"topic_ids": [t1], "count": 2},
        {"topic_ids": [t1], "count": 2, "difficulty": "easy", "extra": 1},
        {"topic_ids": [t1, t1], "count": 2, "difficulty": "easy"},
        {"topic_ids": [t1.upper()], "count": 2, "difficulty": "easy"},
        {"topic_ids": [t1], "count": True, "difficulty": "easy"},
        {"topic_ids": [t1], "count": 11, "difficulty": "easy"},
        {"topic_ids": [t1], "count": 2, "difficulty": "brutal"},
        {"topic_ids": [t1], "count": 2, "difficulty": "easy", "scope": "class_plus_web"},
        {"topic_ids": [str(uuid.uuid4()) for _ in range(6)], "count": 2, "difficulty": "easy"},
    ):
        with pytest.raises(ApiError) as caught:
            parse_quiz_request(body, COURSE, key, s)
        assert caught.value.status_code == 422


def test_attempt_parsing_bounds_telemetry_and_answers():
    ok = {"quiz_id": str(uuid.uuid4()), "question_id": str(uuid.uuid4()), "answer": "b", "hint_count": 1,
          "response_ms": None}  # fmt: skip
    assert parse_attempt_request(ok).hint_count == 1
    for change in ({"hint_count": 2}, {"hint_count": True}, {"hint_count": -1}, {"response_ms": -5},
                   {"response_ms": 3_600_001}, {"answer": ""}, {"answer": "x" * 17}, {"answer": 1},
                   {"answer": "a\n"}, {"quiz_id": "nope"}, {"correct": True}):  # fmt: skip
        with pytest.raises(ApiError) as caught:
            parse_attempt_request({**ok, **change})
        assert caught.value.status_code == 422


def test_trend_is_explicit_for_a_first_attempt():
    assert trend(None, 1.0) == "first"
    assert (trend(0.5, 1.0), trend(1.0, 0.5), trend(0.5, 0.5)) == ("up", "down", "steady")


def test_quiz_client_refuses_redirects_and_sends_protocol_header_without_env_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://elsewhere/"}, content=b"")

    settings = Settings(ai_service_url="http://ai-service:8000", ai_service_token="t" * 40)
    client = AiQuizClient(settings, transport=httpx.MockTransport(handler))
    caps = asyncio.run(client.capabilities())
    assert not caps.ready and caps.reason == "probe_redirect_refused"
    with pytest.raises(QuizUpstreamError) as caught:
        asyncio.run(client.generate({"x": 1}))
    assert caught.value.code == "redirect_refused"
    assert [r.url.path for r in seen] == ["/ai/v1/quiz/capabilities", "/ai/v1/quiz/generate"]
    assert seen[0].headers["x-studygraph-protocol"] == "studygraph.quiz/1"
    assert seen[0].headers["authorization"] == "Bearer " + "t" * 40
    assert "t" * 40 not in repr(client)
