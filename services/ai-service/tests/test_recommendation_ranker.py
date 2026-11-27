import copy
import random
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.mastery import model as mastery_model  # noqa: E402
from app.recommendations import protocol, ranker  # noqa: E402

PARAMS = mastery_model.load_params()
T = [str(uuid.UUID(int=i + 1)) for i in range(6)]
DOC = str(uuid.UUID(int=999))


def candidates(topics, types=("review_topic", "class_material", "practice_quiz", "external_resource")):
    out = []
    for topic in topics:
        for kind in types:
            cand = {"candidate_id": f"c{len(out) + 1}", "type": kind, "topic_id": topic}
            if kind in protocol.DOCUMENT_TYPES:
                cand["document_id"] = DOC
            out.append(cand)
    return out


def recent(eid, topic, window, correct):
    return {"evidence_id": eid, "kind": "recent_window", "topic_id": topic, "window": window, "correct": correct}


def prediction(eid, topic, mastery):
    return {"evidence_id": eid, "kind": "prediction", "topic_id": topic, "mastery": mastery,
            "model_version": "1.0.0-assist09", "through_seq": 7}  # fmt: skip


def request(evidence, cands, max_items=6):
    return protocol.validate_request({
        "protocol": protocol.PROTOCOL, "request_id": str(uuid.uuid4()), "learner_ref": "a" * 64,
        "course_ref": "b" * 64, "max_items": max_items, "evidence": evidence, "candidates": cands,
    })  # fmt: skip


def respond(req):
    res = ranker.build_response(req, PARAMS)
    protocol.check_response(req, res, ranker.MODEL_NAME, ranker.MODEL_VERSION)
    return res


def topics_of(req, res):
    topic = {c["candidate_id"]: c["topic_id"] for c in req["candidates"]}
    return [topic[i["candidate_id"]] for i in res["items"]]


def types_of(req, res):
    kind = {c["candidate_id"]: c["type"] for c in req["candidates"]}
    return [kind[i["candidate_id"]] for i in res["items"]]


def test_weakest_topic_comes_first_with_its_recent_evidence():
    req = request([recent("e1", T[0], 10, 9), recent("e2", T[1], 5, 1), recent("e3", T[2], 6, 3)], candidates(T[:3]))
    res = respond(req)
    assert topics_of(req, res)[:2] == [T[1], T[1]]
    assert res["items"][0] == {"candidate_id": res["items"][0]["candidate_id"], "reason_code": "low_recent_accuracy",
                               "evidence_ids": ["e2"]}  # fmt: skip
    assert T[0] not in topics_of(req, res)  # 9 of 10 correct: not recommended


def test_very_weak_starts_with_review_and_moderately_weak_with_practice():
    very = request([recent("e1", T[0], 10, 1)], candidates([T[0]]))
    assert types_of(very, respond(very)) == ["review_topic", "class_material", "practice_quiz"]
    moderate = request([recent("e1", T[0], 10, 5)], candidates([T[0]]))
    assert types_of(moderate, respond(moderate)) == ["practice_quiz", "class_material", "review_topic"]


def test_a_low_prediction_alone_makes_a_topic_weak():
    req = request([prediction("p1", T[0], 0.2), recent("e2", T[1], 1, 1)], candidates(T[:2]))
    res = respond(req)
    assert topics_of(req, res)[0] == T[0]
    assert res["items"][0]["reason_code"] == "low_prediction" and res["items"][0]["evidence_ids"] == ["p1"]


def test_recent_errors_beat_a_stale_high_prediction():
    req = request([prediction("p1", T[0], 0.95), recent("e1", T[0], 5, 0)], candidates([T[0]]))
    res = respond(req)
    assert res["items"][0]["reason_code"] == "low_recent_accuracy"
    assert res["items"][0]["evidence_ids"] == ["e1"]  # the high prediction does not support the claim


def test_both_signals_are_cited_when_both_are_low():
    req = request([prediction("p1", T[0], 0.1), recent("e1", T[0], 4, 1)], candidates([T[0]]))
    assert set(respond(req)["items"][0]["evidence_ids"]) == {"e1", "p1"}


def test_one_answer_is_not_enough_evidence():
    req = request([recent("e1", T[0], 1, 0)], candidates([T[0]]))
    assert respond(req)["items"] == []  # practised, so not "not_practised" either


def test_unpractised_topics_fill_free_slots_in_request_order():
    req = request([recent("e1", T[0], 10, 2), recent("e2", T[1], 10, 10)], candidates([T[0], T[3], T[2], T[1]]))
    res = respond(req)
    assert topics_of(req, res) == [T[0]] * 3 + [T[3], T[2]]
    assert all(i["reason_code"] == "not_practised" and i["evidence_ids"] == [] for i in res["items"][3:])


def test_cold_start_is_limited_to_three_items():
    req = request([], candidates(T))
    res = respond(req)
    assert len(res["items"]) == 3 and set(types_of(req, res)) == {"practice_quiz"}


def test_two_items_per_topic_when_several_topics_are_weak():
    req = request([recent(f"e{i}", T[i], 10, i) for i in range(4)], candidates(T[:4]))
    assert topics_of(req, respond(req)) == [T[0], T[0], T[1], T[1], T[2], T[2]]


def test_max_items_is_respected():
    req = request([recent("e1", T[0], 10, 0)], candidates(T), max_items=2)
    assert len(respond(req)["items"]) == 2


def test_same_request_same_response():
    req = request([recent("e1", T[0], 10, 3), prediction("p2", T[1], 0.3)], candidates(T))
    assert ranker.build_response(req, PARAMS) == ranker.build_response(copy.deepcopy(req), PARAMS)


@pytest.mark.parametrize("seed", range(300))
def test_random_requests_always_pass_core_validation(seed):
    rng = random.Random(seed)
    topics = T[: rng.randint(1, 6)]
    cands = [c for c in candidates(topics) if rng.random() < 0.7]
    rng.shuffle(cands)
    evidence = []
    for i, topic in enumerate(topics):
        if rng.random() < 0.7:
            window = rng.randint(1, 10)
            evidence.append(recent(f"r{i}", topic, window, rng.randint(0, window)))
        if rng.random() < 0.5:
            evidence.append(prediction(f"p{i}", topic, rng.random()))
    req = request(evidence, cands, max_items=rng.randint(1, 6))
    respond(req)  # check_response raises on any contract violation
