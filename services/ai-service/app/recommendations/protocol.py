"""studygraph.recommendations/1: strict request checks and the response checks core-api applies.

Follows the PROPOSAL in docs/ai-recommendations-contract.md (not yet in the shared OpenAPI):
core-api sends the candidates and the evidence, the AI may only select and order candidates and
attach a reason code and evidence ids. Nothing else is accepted in either direction.

`check_response` mirrors the proposal's "Validation" list so that core-api can reuse it verbatim:
any failure rejects the whole response (core-api then serves rule_based/1).

Points the proposal leaves open, decided here and listed for CE-1 + AI-2 sign-off:
- `max_items` is 1..6 (rule_based/1 and the dashboard show at most 6);
- at most one evidence of each kind per topic (otherwise which one counts is ambiguous);
- `document_id` is required for class_material / external_resource and forbidden otherwise;
- size limits: 500 evidence entries, 300 candidates.
"""

from __future__ import annotations

import re
import uuid

PROTOCOL = "studygraph.recommendations/1"
REASON_CODES = frozenset({"low_recent_accuracy", "low_prediction", "not_practised", "review_due"})
CANDIDATE_TYPES = frozenset({"review_topic", "class_material", "external_resource", "practice_quiz"})
DOCUMENT_TYPES = frozenset({"class_material", "external_resource"})
EVIDENCE_KINDS = frozenset({"recent_window", "prediction"})
MAX_ITEMS = 6
MAX_EVIDENCE = 500
MAX_CANDIDATES = 300
RECENT_WINDOW = 10  # rule_based/1 looks at the last <= 10 graded attempts of a topic

REQUEST_KEYS = {"protocol", "request_id", "learner_ref", "course_ref", "max_items", "evidence", "candidates"}
RESPONSE_KEYS = {"protocol", "request_id", "mock", "model", "items"}
ITEM_KEYS = {"candidate_id", "reason_code", "evidence_ids"}
EVIDENCE_KEYS = {
    "recent_window": {"evidence_id", "kind", "topic_id", "window", "correct"},
    "prediction": {"evidence_id", "kind", "topic_id", "mastery", "model_version", "through_seq"},
}

_REF = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$")


class ProtocolError(ValueError):
    """A request or response that does not follow studygraph.recommendations/1."""


def _fail(message: str) -> None:
    raise ProtocolError(message)


def _keys(obj: object, expected: set[str], label: str) -> dict:
    if not isinstance(obj, dict):
        _fail(f"{label} must be an object")
    if set(obj) != expected:
        missing, extra = sorted(expected - set(obj)), sorted(set(obj) - expected)
        _fail(f"{label} keys: missing {missing}, unexpected {extra}")
    return obj


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be a UUID string")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        canonical = None
    if canonical != value:
        _fail(f"{label} must be a canonical lowercase UUID")
    return value


def _int(value: object, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(f"{label} must be an integer in [{low}, {high}]")
    return value


def _local_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _LOCAL_ID.match(value):
        _fail(f"{label} must match {_LOCAL_ID.pattern}")
    return value


def validate_request(request: object) -> dict:
    """Return the request unchanged if it is valid, else raise ProtocolError (the route maps it to 422)."""
    req = _keys(request, REQUEST_KEYS, "request")
    if req["protocol"] != PROTOCOL:
        _fail(f"protocol must be {PROTOCOL}")
    _uuid(req["request_id"], "request_id")
    for ref in ("learner_ref", "course_ref"):
        if not isinstance(req[ref], str) or not _REF.match(req[ref]):
            _fail(f"{ref} must be 64 lowercase hex characters")
    _int(req["max_items"], "max_items", 1, MAX_ITEMS)

    evidence = req["evidence"]
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        _fail(f"evidence must be a list of at most {MAX_EVIDENCE}")
    seen_ids: set[str] = set()
    seen_kinds: set[tuple[str, str]] = set()
    for i, ev in enumerate(evidence):
        label = f"evidence[{i}]"
        if not isinstance(ev, dict) or not isinstance(ev.get("kind"), str) or ev["kind"] not in EVIDENCE_KINDS:
            _fail(f"{label}.kind must be one of {sorted(EVIDENCE_KINDS)}")
        _keys(ev, EVIDENCE_KEYS[ev["kind"]], label)
        eid = _local_id(ev["evidence_id"], f"{label}.evidence_id")
        if eid in seen_ids:
            _fail(f"{label}.evidence_id {eid!r} is repeated")
        seen_ids.add(eid)
        topic = _uuid(ev["topic_id"], f"{label}.topic_id")
        if (topic, ev["kind"]) in seen_kinds:
            _fail(f"{label}: more than one {ev['kind']} evidence for topic {topic}")
        seen_kinds.add((topic, ev["kind"]))
        if ev["kind"] == "recent_window":
            window = _int(ev["window"], f"{label}.window", 1, RECENT_WINDOW)
            _int(ev["correct"], f"{label}.correct", 0, window)
        else:
            mastery = ev["mastery"]
            if isinstance(mastery, bool) or not isinstance(mastery, (int, float)) or not 0.0 <= mastery <= 1.0:
                _fail(f"{label}.mastery must be a number in [0, 1]")
            if not isinstance(ev["model_version"], str) or not _VERSION.match(ev["model_version"]):
                _fail(f"{label}.model_version is invalid")
            _int(ev["through_seq"], f"{label}.through_seq", 1, 2**31 - 1)

    candidates = req["candidates"]
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        _fail(f"candidates must be a list of at most {MAX_CANDIDATES}")
    seen_candidates: set[str] = set()
    for i, cand in enumerate(candidates):
        label = f"candidates[{i}]"
        if not isinstance(cand, dict) or not isinstance(cand.get("type"), str) or cand["type"] not in CANDIDATE_TYPES:
            _fail(f"{label}.type must be one of {sorted(CANDIDATE_TYPES)}")
        expected = {"candidate_id", "type", "topic_id"} | ({"document_id"} if cand["type"] in DOCUMENT_TYPES else set())
        _keys(cand, expected, label)
        cid = _local_id(cand["candidate_id"], f"{label}.candidate_id")
        if cid in seen_candidates:
            _fail(f"{label}.candidate_id {cid!r} is repeated")
        seen_candidates.add(cid)
        _uuid(cand["topic_id"], f"{label}.topic_id")
        if "document_id" in cand:
            _uuid(cand["document_id"], f"{label}.document_id")
    return req


def check_response(request: dict, response: object, model_name: str, model_version: str) -> dict:
    """The proposal's all-or-nothing response validation, as core-api would apply it."""
    res = _keys(response, RESPONSE_KEYS, "response")
    if res["protocol"] != PROTOCOL:
        _fail("protocol mismatch")
    if res["request_id"] != request["request_id"]:
        _fail("request_id is not echoed")
    if res["mock"] is not False:
        _fail("mock must be literally false")
    model = _keys(res["model"], {"name", "version"}, "model")
    if (model["name"], model["version"]) != (model_name, model_version):
        _fail("model identity is not the pinned one")
    items = res["items"]
    if not isinstance(items, list) or len(items) > request["max_items"]:
        _fail("items must be a list of at most max_items")
    candidate_topic = {c["candidate_id"]: c["topic_id"] for c in request["candidates"]}
    evidence_topic = {e["evidence_id"]: e["topic_id"] for e in request["evidence"]}
    seen: set[str] = set()
    for i, item in enumerate(items):
        _keys(item, ITEM_KEYS, f"items[{i}]")
        cid = item["candidate_id"]
        if not isinstance(cid, str) or cid not in candidate_topic:
            _fail(f"items[{i}].candidate_id was not sent")
        if cid in seen:
            _fail(f"items[{i}].candidate_id is repeated")
        seen.add(cid)
        if not isinstance(item["reason_code"], str) or item["reason_code"] not in REASON_CODES:
            _fail(f"items[{i}].reason_code is not allowed")
        ids = item["evidence_ids"]
        if not isinstance(ids, list) or not all(isinstance(e, str) for e in ids) or len(set(ids)) != len(ids):
            _fail(f"items[{i}].evidence_ids must be a list without repeats")
        for eid in ids:
            if not isinstance(eid, str) or evidence_topic.get(eid) != candidate_topic[cid]:
                _fail(f"items[{i}].evidence_ids: {eid!r} was not sent for this candidate's topic")
    return res
