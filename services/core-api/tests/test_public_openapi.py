"""Public contracts are checked on the document clients actually fetch. No application dependencies are overridden."""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

COURSE = "/api/v1/courses/{course_id}"
BANK = COURSE + "/exam-questions"
QUESTION = BANK + "/{question_id}"
EXAMS = COURSE + "/exams"
EXAM = EXAMS + "/{exam_id}"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(scope="module")
def spec():
    with TestClient(create_app()) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def resolved(spec, schema):
    if "$ref" in schema:
        return spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
    return schema


def inputs(op):
    return {(p["in"], p["name"]): p for p in op.get("parameters", [])}


@pytest.mark.parametrize("path,method,header,required", [
    (BANK, "post", "Idempotency-Key", True), (QUESTION, "put", "If-Match", True),
    (QUESTION + "/approve", "post", "If-Match", False), (QUESTION + "/retire", "post", "If-Match", False),
    (EXAMS, "post", "Idempotency-Key", True), (EXAM, "patch", "If-Match", True),
    (EXAM + "/blueprint", "put", "If-Match", True), (EXAM + "/fill-from-blueprint", "post", "If-Match", True),
    (EXAM + "/items", "post", "If-Match", True), (EXAM + "/items/order", "put", "If-Match", True),
    (EXAM + "/items/{item_id}", "patch", "If-Match", True), (EXAM + "/items/{item_id}", "delete", "If-Match", False),
    (EXAM + "/archive", "post", "If-Match", False), (EXAM + "/finalize", "post", "If-Match", True),
    (EXAM + "/duplicate", "post", "Idempotency-Key", True),
])
def test_mutation_inputs_and_reconciliation(spec, path, method, header, required):
    op = spec["paths"][path][method]
    assert op["security"] == [{"HTTPBearer": []}]
    assert inputs(op)["header", header]["required"]
    assert op["requestBody"].get("required", False) == required
    assert "application/json" in op["requestBody"]["content"]
    assert "exam_response_unavailable" in json.dumps(op["responses"]["503"])
    assert "Retry-After" in op["responses"]["503"]["headers"]


def test_exam_payload_shapes_and_replays(spec):
    def body(path, method):
        return resolved(spec, spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"])

    create = body(EXAMS, "post")
    assert set(create["required"]) == {"title", "instructions", "duration_minutes"}
    patch = body(EXAM, "patch")
    assert not patch.get("required") and patch["minProperties"] == 1
    question = body(BANK, "post")
    assert question == body(QUESTION, "put")
    shapes = [resolved(spec, v) for v in question["oneOf"]]
    assert {v["properties"]["type"]["const"] for v in shapes} == {"mcq", "short", "true_false"}
    for v in shapes:
        assert set(v["required"]) == set(v["properties"])
        ref = resolved(spec, v["properties"]["source_refs"]["items"])
        assert "page" in resolved(spec, ref["properties"]["locator"])["required"]
    row = resolved(spec, body(EXAM + "/blueprint", "put")["properties"]["rows"]["items"])
    assert set(row["required"]) == {"id", "type", "count", "topic_id", "difficulty", "bloom_level", "points_each"}
    final = body(EXAM + "/finalize", "post")
    assert set(final["required"]) == {"preview_fingerprint", "blueprint_deviation_reason"}
    assert "pf1:" in final["properties"]["preview_fingerprint"]["pattern"]
    assert "replayed_revision" in json.dumps(spec["components"]["schemas"]["ExamQuestionReplay"])
    assert "ExamQuestionReplay" in json.dumps(spec["paths"][QUESTION + "/approve"]["post"]["responses"]["200"])
    dup = spec["paths"][EXAM + "/duplicate"]["post"]["responses"]
    assert dup["200"]["content"] == dup["201"]["content"]
    assert "Idempotent-Replayed" in dup["200"]["headers"] and "Location" in dup["201"]["headers"]
    assert "ETag" in spec["paths"][EXAM]["get"]["responses"]["200"]["headers"]


@pytest.mark.parametrize("suffix", ["export", "preview-file"])
def test_file_contract(spec, suffix):
    op = spec["paths"][EXAM + "/" + suffix]["get"]
    params = inputs(op)
    for name, values in (("variant", {"student", "answer_key"}), ("format", {"pdf", "docx"})):
        assert params["query", name]["required"] and set(params["query", name]["schema"]["enum"]) == values
    success = op["responses"]["200"]
    assert set(success["content"]) == {"application/pdf", DOCX}
    assert all(m["schema"]["format"] == "binary" for m in success["content"].values())
    assert {"Content-Disposition", "X-Exam-Renderer-Version", "X-Exam-Content-SHA256"} <= success["headers"].keys()
    assert "Retry-After" in op["responses"]["429"]["headers"]
    assert "exam_response_unavailable" not in json.dumps(op["responses"]["503"])


def test_lists_preview_and_non_exam_contracts(spec):
    paths = spec["paths"]
    assert {p["name"] for p in paths[BANK]["get"]["parameters"] if p["in"] == "query"} == {
        "type", "status", "bloom_level", "difficulty", "topic_id", "limit", "cursor", "q"}
    preview = inputs(paths[EXAM + "/preview"]["get"])["query", "variant"]
    assert not preview["required"] and preview["schema"]["default"] == "student"
    shape = resolved(spec, paths[EXAM + "/preview"]["get"]["responses"]["200"]["content"]["application/json"]["schema"])
    assert {branch["properties"]["variant"]["const"]: branch["properties"]["answer_key"]["type"]
            for branch in shape["oneOf"]} == {"student": "null", "answer_key": "array"}
    assert inputs(paths[COURSE + "/dashboard"]["get"])["query", "tz"]["schema"]["default"] == "UTC"
    events = paths[COURSE + "/study-events"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    item = resolved(spec, resolved(spec, events)["properties"]["events"]["items"])
    assert {"document_viewed", "focus_interval", "recommendation_opened"} <= {
        t for s in item["oneOf"] for t in (resolved(spec, s)["properties"]["type"].get("enum")
            or [resolved(spec, s)["properties"]["type"].get("const")])}
    assert set(paths[COURSE + "/chat"]["post"]["responses"]["200"]["content"]) == {"text/event-stream"}


def test_fingerprint_conflict_validation_and_recovery_fields(spec):
    responses = spec["paths"][EXAM + "/finalize"]["post"]["responses"]

    def variants(status):
        schema = responses[status]["content"]["application/json"]["schema"]
        return {code: shape for shape in schema.get("oneOf", [schema])
                for code in shape["properties"]["code"]["enum"]}

    for code in ("version_mismatch", "exam_preview_stale"):
        assert "current_version" in variants("412")[code]["required"]
    blocked = variants("409")["exam_not_ready"]
    assert "issues" in blocked["required"]
    assert resolved(spec, blocked["properties"]["issues"]["items"]) == spec["components"]["schemas"]["Issue"]
    assert {"precondition_required", "preview_fingerprint_required"} == variants("428").keys()
    ambiguous = variants("503")["exam_response_unavailable"]
    assert set(ambiguous["properties"]) == {"code", "detail"}
    assert "No Retry-After" in responses["503"]["description"]
    assert not responses["503"]["headers"]["Retry-After"]["required"]
    assert "429" not in responses and "504" not in responses


def test_blueprint_items_paging_and_file_versions(spec):
    def body(path, method):
        return spec["paths"][EXAM + path][method]["requestBody"]["content"]["application/json"]["schema"]

    assert set(body("/items", "post")["required"]) == {"question_id", "points", "position", "blueprint_row_id"}
    assert body("/items/{item_id}", "patch")["minProperties"] == 1
    assert body("/items/order", "put")["properties"]["item_ids"]["uniqueItems"]
    assert body("/fill-from-blueprint", "post")["required"] == ["row_ids"]
    assert not body("/archive", "post")["additionalProperties"]
    assert body("/duplicate", "post")["required"] == ["title"]
    for path in (EXAMS, BANK, QUESTION + "/revisions", QUESTION + "/history", EXAM + "/history"):
        params = inputs(spec["paths"][path]["get"])
        assert params["query", "limit"]["schema"]["default"] == 50
        assert not params["query", "cursor"]["required"]
    for path in (QUESTION, EXAM):
        params = inputs(spec["paths"][path]["get"])
        assert all(param["schema"]["format"] == "uuid" for (kind, _), param in params.items() if kind == "path")
    draft = spec["paths"][EXAM + "/preview-file"]["get"]["responses"]["200"]["headers"]
    official = spec["paths"][EXAM + "/export"]["get"]["responses"]["200"]["headers"]
    assert "X-Exam-Preview-Fingerprint" in draft and "X-Exam-Preview-Fingerprint" not in official
    assert official["X-Exam-Content-SHA256"]["required"] and not draft["X-Exam-Content-SHA256"]["required"]


def test_non_exam_wire_shapes_and_security(spec):
    for path, methods in spec["paths"].items():
        for op in methods.values():
            if path.startswith("/api/"):
                assert op["security"] == [{"HTTPBearer": []}]
            else:
                assert not op.get("security")
    quiz = spec["paths"][COURSE + "/quizzes"]["post"]["responses"]
    assert {"Location", "Idempotent-Replayed"} <= quiz["200"]["headers"].keys()
    assert "Location" in quiz["201"]["headers"]
    assert "Retry-After" in quiz["409"]["headers"] and "Retry-After" in quiz["429"]["headers"]
    upload = spec["paths"][COURSE + "/documents"]["post"]
    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert "HTTPValidationError" in json.dumps(upload["responses"]["422"])
    assert set(spec["paths"]["/health/db"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["required"]) == {
        "pgvector_installed", "status"}
