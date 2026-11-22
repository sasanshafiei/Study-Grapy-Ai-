"""Authenticated wire fixture for CE and provider development. Every success identifies the mock."""

import hashlib
import hmac
import json
import os
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator, FormatChecker

CONTRACT = (Path(__file__).resolve().parent / "../../../contracts").resolve()
SPEC = yaml.safe_load((CONTRACT / "ai-service.openapi.yaml").read_text())
ACTIONS = {"index": "documents", "chat": "answer", "quiz": "generate", "mastery": "update"}
FIXTURES = {name: json.loads((CONTRACT / "fixtures" / f"{name}.json").read_text()) for name in ACTIONS}
BYTE_LIMIT = FIXTURES["index"]["capabilities"]["max_request_bytes"]

app = FastAPI(title=SPEC["info"]["title"], version=SPEC["info"]["version"])
app.openapi = lambda: SPEC
# Only the two published index examples and the single mastery event can enter this state.
served_documents = set()
mastery_applied = False


class WireError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message


@app.exception_handler(WireError)
async def wire_error(request, exc):
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
    return JSONResponse({"error": {"code": exc.code, "message": exc.message, "retryable": exc.status == 503}},
                        status_code=exc.status, headers=headers)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "service": "ai-service", "mock": True}


def authorize(request, protocol):
    secret = os.environ.get("AI_SERVICE_TOKEN", "")
    if len(secret) < 32:
        raise WireError(503, "unconfigured", "Fixture credential is unavailable.")
    values = request.headers.getlist("Authorization")
    if len(values) != 1 or not hmac.compare_digest(values[0].encode(), ("Bearer " + secret).encode()):
        raise WireError(401, "unauthorized", "Service credential required.")
    if request.headers.getlist("X-StudyGraph-Protocol") != [protocol]:
        raise WireError(400, "protocol_mismatch", "Expected the endpoint's protocol version.")


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate member")
    return result


def invalid_constant(value):
    raise ValueError("invalid JSON number")


async def read_payload(request):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise WireError(415, "unsupported_media_type", "Expected application/json.")
    body = bytearray()
    async for block in request.stream():
        if len(body) + len(block) > BYTE_LIMIT:
            raise WireError(413, "payload_too_large", "Request exceeds the fixture byte limit.")
        body.extend(block)
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise WireError(400, "invalid_request", "Expected a UTF-8 JSON object.") from None


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def unavailable():
    return WireError(422, "fixture_unavailable", "Only the published example inputs are available.")


def index_result(request, payload, fixture):
    document = payload["document"]
    content = {k: payload[k] for k in ("protocol", "document", "sections")}
    digest = hashlib.sha256(canonical(content)).hexdigest()
    material = "\0".join((payload["protocol"], document["id"], document["course_id"], digest))
    key = hashlib.sha256(material.encode()).hexdigest()
    if (digest != payload["content_digest"] or key != payload["idempotency_key"]
            or request.headers.getlist("Idempotency-Key") != [key]):
        raise WireError(400, "inconsistent_request", "Digest and both copies of the key must match the content.")
    for sample in (fixture, fixture["section_example"]):
        receipt = sample["response"]
        if receipt["document_id"] != document["id"]:
            continue
        replayed = document["id"] in served_documents
        if key != receipt["idempotency_key"]:
            if replayed:
                code = "course_mismatch" if document["course_id"] != receipt["course_id"] else "generation_conflict"
                raise WireError(409, code, "A different fixture generation already exists.")
            break
        served_documents.add(document["id"])
        return {**receipt, "mock": True, "replayed": replayed}
    raise unavailable()


def fixture_result(profile, request, payload):
    global mastery_applied
    fixture = FIXTURES[profile]
    if profile == "index":
        return index_result(request, payload, fixture)
    # Permit a fresh correlation ID, but never return canned evidence or scores for another input.
    expected = {**fixture["request"], "request_id": payload["request_id"]}
    if canonical(payload) != canonical(expected):
        raise unavailable()
    result = {**fixture["response"], "request_id": payload["request_id"], "mock": True}
    if profile == "mastery":
        if mastery_applied:
            result["duplicate_event_ids"] = result["applied_event_ids"]
            result["applied_event_ids"] = []
        mastery_applied = True
    return result


def validator(schema):
    return Draft202012Validator({"components": SPEC["components"], **schema}, format_checker=FormatChecker())


def register(profile, action, method):
    path = f"/ai/v1/{profile}/{action}"
    op = SPEC["paths"][path][method]
    protocol = FIXTURES[profile]["capabilities"]["protocol"]
    output = validator(op["responses"]["200"]["content"]["application/json"]["schema"])
    input_schema = validator(op["requestBody"]["content"]["application/json"]["schema"]) if method == "post" else None

    async def handle(request: Request):
        authorize(request, protocol)
        if input_schema is None:
            result = {**FIXTURES[profile]["capabilities"], "mock": True}
        else:
            payload = await read_payload(request)
            if not input_schema.is_valid(payload):
                raise WireError(422, "invalid_request", "Request does not match the protocol schema.")
            try:
                result = fixture_result(profile, request, payload)
            except (ValueError, RecursionError):
                raise WireError(422, "invalid_request", "Request contains an unencodable value.") from None
        output.validate(result)
        return JSONResponse(result)

    app.add_api_route(path, handle, methods=[method.upper()], name=op["operationId"])


for name, action in ACTIONS.items():
    register(name, "capabilities", "get")
    register(name, action, "post")
