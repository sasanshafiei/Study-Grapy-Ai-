import copy
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.recommendations import protocol  # noqa: E402

TOPIC = str(uuid.UUID(int=1))
DOC = str(uuid.UUID(int=2))
NAME, VERSION = "studygraph-rank", "0.1.0"


def valid_request():
    return {
        "protocol": protocol.PROTOCOL, "request_id": "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b", "learner_ref": "a" * 64,
        "course_ref": "b" * 64, "max_items": 6,
        "evidence": [{"evidence_id": "e1", "kind": "recent_window", "topic_id": TOPIC, "window": 5, "correct": 2},
                     {"evidence_id": "e2", "kind": "prediction", "topic_id": TOPIC, "mastery": 0.41,
                      "model_version": "1.0.0-assist09", "through_seq": 12}],
        "candidates": [{"candidate_id": "c1", "type": "review_topic", "topic_id": TOPIC},
                       {"candidate_id": "c2", "type": "class_material", "topic_id": TOPIC, "document_id": DOC}],
    }  # fmt: skip


def valid_response(req):
    return {"protocol": protocol.PROTOCOL, "request_id": req["request_id"], "mock": False,
            "model": {"name": NAME, "version": VERSION},
            "items": [{"candidate_id": "c2", "reason_code": "low_recent_accuracy", "evidence_ids": ["e1"]}]}  # fmt: skip


def test_valid_request_and_response_pass():
    req = protocol.validate_request(valid_request())
    protocol.check_response(req, valid_response(req), NAME, VERSION)


BAD_REQUESTS = {
    "extra key": lambda r: r.update(hint="x"),
    "wrong protocol": lambda r: r.update(protocol="studygraph.recommendations/2"),
    "non-canonical request id": lambda r: r.update(request_id=r["request_id"].upper()),
    "short learner ref": lambda r: r.update(learner_ref="a" * 63),
    "max_items 0": lambda r: r.update(max_items=0),
    "max_items 7": lambda r: r.update(max_items=7),
    "max_items bool": lambda r: r.update(max_items=True),
    "unknown evidence kind": lambda r: r["evidence"][0].update(kind="study_event"),
    "unhashable evidence kind": lambda r: r["evidence"][0].update(kind=["recent_window"]),
    "correct above window": lambda r: r["evidence"][0].update(correct=6),
    "window above 10": lambda r: r["evidence"][0].update(window=11, correct=0),
    "mastery above 1": lambda r: r["evidence"][1].update(mastery=1.01),
    "answer text in evidence": lambda r: r["evidence"][0].update(answer="B"),
    "repeated evidence id": lambda r: r["evidence"][1].update(evidence_id="e1"),
    "two windows for one topic": lambda r: r["evidence"].append(dict(r["evidence"][0], evidence_id="e9")),
    "repeated candidate id": lambda r: r["candidates"][1].update(candidate_id="c1"),
    "document on review_topic": lambda r: r["candidates"][0].update(document_id=DOC),
    "missing document": lambda r: r["candidates"][1].pop("document_id"),
    "url in candidate": lambda r: r["candidates"][1].update(url="https://example.org"),
    "unknown candidate type": lambda r: r["candidates"][0].update(type="video"),
}


@pytest.mark.parametrize("name", BAD_REQUESTS)
def test_invalid_requests_are_refused(name):
    req = valid_request()
    BAD_REQUESTS[name](req)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(req)


BAD_RESPONSES = {
    "mock true": lambda r: r.update(mock=True),
    "other request id": lambda r: r.update(request_id=str(uuid.UUID(int=4))),
    "unpinned model": lambda r: r["model"].update(version="0.2.0"),
    "extra field": lambda r: r.update(explanation="because"),
    "title in item": lambda r: r["items"][0].update(title="Kinetics"),
    "unknown candidate": lambda r: r["items"][0].update(candidate_id="c9"),
    "repeated candidate": lambda r: r["items"].append(dict(r["items"][0])),
    "reason not allowed": lambda r: r["items"][0].update(reason_code="you_are_bad"),
    "unknown evidence": lambda r: r["items"][0].update(evidence_ids=["e9"]),
    "repeated evidence": lambda r: r["items"][0].update(evidence_ids=["e1", "e1"]),
    "too many items": lambda r: r["items"].extend(
        {"candidate_id": "c1", "reason_code": "not_practised", "evidence_ids": []} for _ in range(6)),
}


@pytest.mark.parametrize("name", BAD_RESPONSES)
def test_invalid_responses_are_rejected(name):
    req = protocol.validate_request(valid_request())
    res = copy.deepcopy(valid_response(req))
    BAD_RESPONSES[name](res)
    with pytest.raises(protocol.ProtocolError):
        protocol.check_response(req, res, NAME, VERSION)


def test_evidence_of_another_topic_is_rejected():
    req = valid_request()
    other = str(uuid.UUID(int=5))
    req["evidence"].append({"evidence_id": "e3", "kind": "recent_window", "topic_id": other, "window": 3, "correct": 0})
    req = protocol.validate_request(req)
    res = valid_response(req)
    res["items"][0]["evidence_ids"] = ["e3"]
    with pytest.raises(protocol.ProtocolError):
        protocol.check_response(req, res, NAME, VERSION)
