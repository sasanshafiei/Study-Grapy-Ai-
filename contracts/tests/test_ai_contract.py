"""Conformance of the shared document, fixture service, and CE's unchanged consumer profiles."""

import asyncio
import importlib.util
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from openapi_spec_validator import validate

from app.chat import protocol as chat
from app.chat.client import AiChatClient
from app.chat.verify import VerificationFailed, snippet_for
from app.config import Settings
from app.indexing import client as indexing, payload as index_payload
from app.mastery import protocol as mastery
from app.mastery.client import AiMasteryClient
from app.mastery.pins import Pins
from app.quiz import protocol as quiz
from app.quiz.client import AiQuizClient

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts"
SPEC = yaml.safe_load((CONTRACT / "ai-service.openapi.yaml").read_text())
POSTS = {"index": "documents", "chat": "answer", "quiz": "generate", "mastery": "update"}
TOKEN = "fixture-only-test-credential-of-32-characters"


def path(profile, action):
    return f"/ai/v1/{profile}/{action}"


def fixture(profile):
    return json.loads((CONTRACT / "fixtures" / f"{profile}.json").read_text())


def check(schema, instance):
    document = {"components": SPEC["components"], **schema}
    Draft202012Validator(document, format_checker=FormatChecker()).validate(instance)


def operation(profile, get=False):
    action = "capabilities" if get else POSTS[profile]
    return SPEC["paths"][path(profile, action)]["get" if get else "post"]


def response_schema(op, status):
    response = op["responses"][str(status)]
    if "$ref" in response:
        response = SPEC["components"]["responses"][response["$ref"].rsplit("/", 1)[1]]
    return response["content"]["application/json"]["schema"]


def headers(profile):
    return {"Authorization": "Bearer " + TOKEN, "X-StudyGraph-Protocol": f"studygraph.{profile}/1"}


def test_published_openapi_is_valid():
    validate(SPEC)


def test_exact_supported_inventory():
    expected = {path(p, action) for p, post in POSTS.items() for action in ("capabilities", post)}
    assert set(SPEC["paths"]) == expected


@pytest.mark.parametrize("profile", POSTS)
def test_operations_describe_auth_headers_and_wire_types(profile):
    for get in (True, False):
        op = operation(profile, get)
        assert op.get("security", SPEC.get("security")) == [{"ServiceBearer": []}]
        params = {p["name"]: p for p in op["parameters"]}
        assert params["X-StudyGraph-Protocol"]["required"]
        assert params["X-StudyGraph-Protocol"]["schema"]["const"] == f"studygraph.{profile}/1"
        assert ("Idempotency-Key" in params) == (profile == "index" and not get)
        assert ("requestBody" in op) == (not get)
        if not get:
            assert op["requestBody"]["required"] and op["requestBody"]["content"]["application/json"]["schema"]
        for status in (200, 400, 401, 422, 503):
            assert response_schema(op, status)


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setenv("AI_SERVICE_TOKEN", TOKEN)
    module = importlib.util.spec_from_file_location("fixture_service", ROOT / "services/ai-service/app/main.py")
    service = importlib.util.module_from_spec(module)
    module.loader.exec_module(service)
    with TestClient(service.app) as client:
        yield client


@pytest.mark.parametrize("profile", POSTS)
def test_shipped_capabilities_identify_mock(profile, mock):
    response = mock.get(path(profile, "capabilities"), headers=headers(profile))
    assert response.status_code == 200
    assert response.json()["mock"] is True


def index_request(sample):
    body = sample["request"]
    doc = body["document"]
    return index_payload.build_request(
        {**doc, "content_hash": doc["source_sha256"], "_provenance": doc.get("attribution")}, body["sections"]
    )


def pins():
    cap = fixture("mastery")["capabilities"]
    return Pins(cap["model"]["name"], cap["model"]["version"], UUID(cap["state_epoch"]),
                "fixture-v1", b"public-conformance-example-key-0001")


def events(req):
    return [mastery.BatchEvent(UUID(e["event_id"]), UUID(e["topic_id"]), e["topic_seq"], e["qtype"],
                               e["difficulty"], e["correct"], e["hint_used"], e["response_ms"],
                               datetime.fromisoformat(e["submitted_at"])) for e in req["events"]]


def chat_limits(req):
    return chat.Limits(req["max_answer_chars"], req["max_citations"], 16)


def index_http(body, status=200):
    return indexing.HttpResponse(status, "application/json", json.dumps(body).encode())


def accept_response(profile, sample, body):
    """Use CE's real consumer. Returns False for a refused result, regardless of profile API."""
    req = sample["request"]
    if profile == "index":
        return indexing.validate_indexed(index_http(body), index_request(sample)).outcome == indexing.Outcome.INDEXED
    try:
        if profile == "chat":
            chat.validate_answer(body, req, chat_limits(req))
        elif profile == "quiz":
            quiz.validate_response(body, req)
        else:
            mastery.validate_update(body, req, events(req), pins())
    except (chat.ProtocolViolation, quiz.ProtocolViolation, mastery.ProtocolViolation, mastery.StateMismatch):
        return False
    return True


def validate_request(profile, req):
    check(operation(profile)["requestBody"]["content"]["application/json"]["schema"], req)


@pytest.mark.parametrize("profile", POSTS)
def test_shared_vectors_match_builders_and_real_provider_validators(profile):
    f = fixture(profile)
    req, cap = f["request"], f["capabilities"]
    validate_request(profile, req)
    check(response_schema(operation(profile, get=True), 200), cap)
    check(response_schema(operation(profile), 200), f["response"])
    assert accept_response(profile, f, f["response"])
    if profile == "index":
        assert indexing.validate_capabilities(index_http(cap)).ready
        for sample in (f, f["section_example"]):
            body = sample["request"]
            built = index_request(sample)
            assert built.body == json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            validate_request(profile, body)
            check(response_schema(operation(profile), 200), sample["response"])
            for replay in (False, True):
                result = indexing.classify(index_http({**sample["response"], "replayed": replay}), built)
                assert result.outcome == indexing.Outcome.INDEXED
                assert result.replayed is replay and result.chunk_count == sample["response"]["chunk_count"]
            assert indexing.classify(index_http({}, 202), built).outcome == indexing.Outcome.ACCEPTED
    elif profile == "chat":
        assert chat.validate_capabilities(200, cap).ready
        assert req == chat.build_request(UUID(req["request_id"]), UUID(req["course_id"]), req["scope"],
                                         req["question"], chat_limits(req))
        external = chat.build_request(UUID(req["request_id"]), UUID(req["course_id"]), "class_plus_external",
                                      req["question"], chat_limits(req))
        validate_request(profile, external)
        with pytest.raises(ValidationError):
            validate_request(profile, {**external, "allowed_origins": ["class"]})
    elif profile == "quiz":
        negotiated = quiz.validate_capabilities(200, cap)
        assert negotiated.ready
        topics = [(UUID(t["topic_id"]), t["name"]) for t in req["topics"]]
        assert req == quiz.build_request(UUID(req["request_id"]), UUID(req["course_id"]), req["scope"], topics,
                                         req["count"], req["difficulty"], quiz.negotiate_types(negotiated))
    else:
        assert mastery.validate_capabilities(200, cap, pins()).status == "compatible"
        assert req == mastery.build_request(UUID(req["request_id"]), UUID(int=6), UUID(int=1), events(req), pins())


@pytest.mark.parametrize("profile", POSTS)
def test_shared_negative_vectors(profile):
    sample = fixture(profile)
    schema = response_schema(operation(profile), 200)
    for case in sample["invalid_responses"]:
        body = deepcopy(sample["response"])
        parent = body
        for key in case["path"][:-1]:
            parent = parent[key]
        parent[case["path"][-1]] = case["value"]
        if case["schema_valid"]:  # Correlation, evidence or accounting across fields.
            check(schema, body)
        else:
            with pytest.raises(ValidationError):
                check(schema, body)
        assert not accept_response(profile, sample, body), case["path"]


def post(mock, profile, payload, **extra_headers):
    hdr = headers(profile)
    if profile == "index":
        hdr["Idempotency-Key"] = payload["idempotency_key"]
    return mock.post(path(profile, POSTS[profile]), headers={**hdr, **extra_headers}, json=payload)


@pytest.mark.parametrize("profile", POSTS)
def test_mock_success_and_production_payload_refusal(profile, mock):
    sample = fixture(profile)
    response = post(mock, profile, sample["request"])
    assert response.status_code == 200
    assert response.json() == {**sample["response"], "mock": True}
    check(response_schema(operation(profile), 200), response.json())
    assert not accept_response(profile, sample, response.json())
    if profile == "mastery":
        dup = post(mock, profile, {**sample["request"], "request_id": str(UUID(int=34))}).json()
        assert dup["duplicate_event_ids"] == sample["response"]["applied_event_ids"] and dup["applied_event_ids"] == []
        assert dup["scores"] == response.json()["scores"]
    if profile == "index":
        for f in (sample, sample["section_example"]):
            first = post(mock, profile, f["request"])
            assert first.json()["replayed"] is (f is sample)
            assert post(mock, profile, f["request"]).json() == {**f["response"], "mock": True, "replayed": True}


@pytest.mark.parametrize("profile", POSTS)
def test_actual_production_client_refuses_shipped_probe(profile, mock):
    settings = Settings(_env_file=None, ai_service_token=TOKEN)
    if profile == "index":
        def transport(url, method, route, body, headers, bounds):
            reply = mock.request(method, route, headers=headers, content=body)
            return indexing.HttpResponse(reply.status_code, reply.headers["content-type"], reply.content)
        result = indexing.AiIndexClient(settings, transport=transport).readiness()
    else:
        transport = httpx.ASGITransport(app=mock.app)
        if profile == "mastery":
            result = AiMasteryClient(settings, transport=transport).probe_sync(pins())
            assert result.status == "incompatible"
        else:
            client = (AiChatClient if profile == "chat" else AiQuizClient)(settings, transport=transport)
            result = asyncio.run(client.capabilities())
    assert result.reason == "mock_service"


@pytest.mark.parametrize("profile", POSTS)
@pytest.mark.parametrize("get", (False, True))
def test_credentials_and_versions(profile, get, mock):
    route = path(profile, "capabilities" if get else POSTS[profile])
    method = "GET" if get else "POST"
    for header, value, expected in (("Authorization", "Bearer incorrect", 401),
                                    ("X-StudyGraph-Protocol", "studygraph.other/2", 400)):
        resp = mock.request(method, route, headers={**headers(profile), header: value})
        assert resp.status_code == expected
        check(response_schema(operation(profile, get), expected), resp.json())


@pytest.mark.parametrize("profile", POSTS)
def test_required_members_enforced_by_service(profile, mock):
    req = fixture(profile)["request"].copy()
    req.pop("protocol")
    response = post(mock, profile, req)
    assert response.status_code == 422
    check(response_schema(operation(profile), 422), response.json())
    req["protocol"] = "studygraph.other/2"
    assert post(mock, profile, req).status_code == 422


def test_index_rejects_tampering_and_conflicts(mock):
    f = fixture("index")
    req = deepcopy(f["request"])
    assert post(mock, "index", req).status_code == 200
    assert post(mock, "index", req, **{"Idempotency-Key": "0" * 64}).status_code == 400
    req["document"]["title"] += " revised"
    assert post(mock, "index", req).status_code == 400
    for field, value, code in (("title", "Other", "generation_conflict"),
                               ("course_id", str(UUID(int=90)), "course_mismatch")):
        changed = deepcopy(f)
        changed["request"]["document"][field] = value
        built = json.loads(index_request(changed).body)
        resp = post(mock, "index", built)
        assert resp.status_code == 409 and resp.json()["error"]["code"] == code
        check(response_schema(operation("index"), 409), resp.json())


def test_only_contract_operations_registered(mock):
    assert mock.get("/openapi.json").json() == SPEC
    registered = {(r.path, method) for r in mock.app.routes if r.path.startswith("/ai/")
                  for method in r.methods}
    assert registered == {(p, m.upper()) for p, ops in SPEC["paths"].items() for m in ops}
    for route in ("/ai/v1/topics/extract", "/ai/v1/exam/generate", "/ai/v1/recommendations",
                  "/ai/v1/analytics/refresh", "/internal/web/search"):
        assert mock.post(route, headers=headers("chat")).status_code == 404
    assert mock.get("/ai/v1/jobs/" + str(UUID(int=1))).status_code == 404
    assert mock.delete("/ai/v1/index/documents/" + str(UUID(int=1))).status_code == 404


def test_example_evidence_matches_indexed_source_text():
    source = fixture("index")["request"]
    for citation in (fixture("chat")["response"]["citations"][0],
                     fixture("quiz")["response"]["questions"][0]["citations"][0]):
        assert citation["document_id"] == source["document"]["id"]
        text = source["sections"][0]["text"]
        assert snippet_for(text, citation["quote"])
        with pytest.raises(VerificationFailed):
            snippet_for(text, "An invented quotation that is absent from this source.")


def test_fixture_service_fails_closed_without_secret(mock, monkeypatch):
    monkeypatch.delenv("AI_SERVICE_TOKEN")
    response = mock.get(path("chat", "capabilities"), headers=headers("chat"))
    assert response.status_code == 503
    check(response_schema(operation("chat", True), 503), response.json())


@pytest.mark.parametrize("profile", POSTS)
def test_schema_requires_every_field_emitted_by_builder(profile):
    samples = [fixture(profile)["request"]]
    if profile == "index":
        samples.append(fixture(profile)["section_example"]["request"])

    def members(value, path=()):
        if isinstance(value, dict):
            for key, item in value.items():
                yield (*path, key)
                yield from members(item, (*path, key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                yield from members(item, (*path, i))

    for sample in samples:
        for path in members(sample):
            incomplete = deepcopy(sample)
            parent = incomplete
            for part in path[:-1]:
                parent = parent[part]
            del parent[path[-1]]
            with pytest.raises(ValidationError):
                validate_request(profile, incomplete)
        with pytest.raises(ValidationError):
            validate_request(profile, {**sample, "unexpected": True})


@pytest.mark.parametrize("wire,content_type,status", [
    (b'{"protocol": "studygraph.chat/1", "protocol": "studygraph.chat/1"}', "application/json", 400),
    (b'{"question": NaN}', "application/json", 400),
    (b'invalid', "text/plain", 415),
])
def test_fixture_service_protocol_errors(mock, wire, content_type, status):
    reply = mock.post(path("chat", "answer"), headers={**headers("chat"), "Content-Type": content_type}, content=wire)
    assert reply.status_code == status
    check(response_schema(operation("chat"), status), reply.json())


def test_fixture_data_is_scoped(mock):
    for profile in ("chat", "quiz", "mastery"):
        req = fixture(profile)["request"]
        if profile == "mastery":
            req["learner_ref"] = "c" * 64
        else:
            req["course_id"] = str(UUID(int=90))
        resp = post(mock, profile, req)
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "fixture_unavailable"
