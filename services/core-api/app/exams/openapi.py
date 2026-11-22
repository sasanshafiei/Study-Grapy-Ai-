"""Wire contracts for exam routes. The handlers enforce these inputs after owner authorization."""

from fastapi import Response

from . import validation as v
from .http import DEFAULT_LIMIT, MAX_LIMIT
from ..schemas.documents import ErrorBody
from ..schemas.exams import MCQ_ID, MCQ_KEY, SHORT_KEY, TF_KEY
from ..schemas.openapi import (
    BEARER, INTEGER, LOCATION, REPLAYED, RETRY_AFTER, STRING, UUID, array, enum, exact, header, json_body, nullable, query, text_field,
)

VERSION = {**STRING, "pattern": '^"(0|[1-9][0-9]{0,8})"$'}
FINGERPRINT = {**STRING, "pattern": "^pf1:[0-9a-f]{64}$"}
ETAG = {"ETag": header("Quoted version of the returned question or exam.", VERSION, required=True)}
KEY_REPLAY = {"Idempotent-Replayed": header("The original key and intent already succeeded.", enum("true"), required=True)}
IDEMPOTENCY = {"name": "Idempotency-Key", "in": "header", "required": True, "schema": UUID,
               "description": "One new lower-case UUID per creation. Reuse it with the same intent to recover a replay."}
IF_MATCH = {"name": "If-Match", "in": "header", "required": True, "schema": VERSION,
            "description": 'One quoted version, e.g. "7". Item and blueprint operations use the parent exam version.'}

PAGE = [query("limit", {**INTEGER, "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT}, "Page size."),
        query("cursor", {**STRING, "pattern": "^[A-Za-z0-9_-]{1,512}$"}, "Opaque next_cursor for the same list and filters.")]
QTYPE = enum(*v.QUESTION_TYPES)
DIFFICULTY = enum(*v.DIFFICULTIES)
BLOOM = enum(*v.BLOOM_LEVELS)
QUESTIONS = [query("status", enum("draft", "approved", "retired"), "Omit for draft and approved questions."),
             query("type", QTYPE, "Question type."), query("difficulty", DIFFICULTY, "Question difficulty."),
             query("bloom_level", BLOOM, "Bloom level."), query("topic_id", UUID, "Topic of this course."),
             query("q", {**STRING, "minLength": 1, "maxLength": 100}, "Printable stem search text."), *PAGE]
EXAMS = [query("status", enum("draft", "finalized", "archived"), "Omit for draft and finalized exams."), *PAGE]
VARIANT = enum("student", "answer_key")
PREVIEW = [query("variant", {**VARIANT, "default": "student"}, "Student omits answers (answer_key is null).")]
FILE_QUERY = [query("variant", VARIANT, "Student paper or professor answer key.", required=True),
              query("format", enum("pdf", "docx"), "Determines the download media type.", required=True)]


def bounded(low, high):
    return {**INTEGER, "minimum": low, "maximum": high}


def patch(schema):
    return {k: value for k, value in schema.items() if k != "required"} | {"minProperties": 1}


SOURCES = array(exact(document_id=UUID, locator=exact(page=bounded(1, 2_147_483_647))),
                maxItems=v.MAX_SOURCE_REFS, uniqueItems=True, description="Class PDF pages, numbered from 1. Approval verifies each page.")
MCQ_CHOICES = array(exact(id=MCQ_ID, text=text_field(v.MAX_CHOICE)), minItems=2, maxItems=6,
                    description="Use a, b, c ... in order with distinct single-line texts. The key names a supplied choice.")
TF_CHOICES = nullable({"type": "array", "const": list(v.TRUE_FALSE_CHOICES),
                       "items": exact(id=enum("true", "false"), text=enum("True", "False"))})


def question(kind, choices, key):
    return exact(type={**STRING, "const": kind}, difficulty=DIFFICULTY, bloom_level=nullable(BLOOM),
                 topic_id=nullable(UUID), stem=text_field(v.MAX_STEM), choices=choices, answer_key=key,
                 explanation=nullable(text_field(v.MAX_EXPLANATION, 0)), source_refs=SOURCES)


QUESTION = {"oneOf": [question("mcq", MCQ_CHOICES, MCQ_KEY), question("true_false", TF_CHOICES, TF_KEY),
                      question("short", {"type": "null"}, SHORT_KEY)]}
TITLE = {**text_field(v.MAX_TITLE), "description": "Single-line title, trimmed."}
EXAM = exact(title=TITLE, instructions=nullable(text_field(v.MAX_INSTRUCTIONS, 0)),
             duration_minutes=nullable(bounded(1, v.MAX_DURATION)))
POINTS = bounded(1, v.MAX_POINTS)
ROW = exact(id=nullable(UUID), topic_id=nullable(UUID), type=nullable(QTYPE), difficulty=nullable(DIFFICULTY),
            bloom_level=nullable(BLOOM), count=bounded(1, v.MAX_BLUEPRINT_TOTAL), points_each=POINTS)
BLUEPRINT = exact(rows=array(ROW, maxItems=v.MAX_BLUEPRINT_ROWS,
                             description="Null id creates a row. Total count across all rows is at most 200."))
FILL = exact(row_ids=nullable(array(UUID, maxItems=v.MAX_BLUEPRINT_ROWS, description="Null selects all rows.")))
ITEM = exact(question_id=UUID, points=POINTS, position=nullable({**INTEGER, "minimum": 1}), blueprint_row_id=nullable(UUID))
ITEM_PATCH = patch(exact(points=POINTS, blueprint_row_id=nullable(UUID)))
ORDER = exact(item_ids=array(UUID, maxItems=1000, uniqueItems=True, description="Every current item exactly once."))
EMPTY = exact()
DUPLICATE = exact(title=nullable(TITLE))
FINALIZE = exact(
    preview_fingerprint={**FINGERPRINT, "description": "Fingerprint of the draft the professor actually reviewed."},
    blueprint_deviation_reason={**nullable(text_field(500, 10)), "description":
        "Send null unless accepting overridable blueprint issues. Otherwise give a trimmed 10–500 character reason."},
)

# Some machine codes have mandatory recovery fields beyond the common code and detail.
ERROR_FIELDS = {
    "version_mismatch": {"current_version": INTEGER}, "exam_preview_stale": {"current_version": INTEGER},
    "exam_not_ready": {"issues": array({"$ref": "#/components/schemas/Issue"})},
    "question_already_in_exam": {"item_id": UUID},
    "export_budget_exceeded": {"content_chars": INTEGER, "export_budget": INTEGER},
    **{code: {"limit": INTEGER} for code in ("exam_item_limit", "question_limit_reached", "exam_limit_reached")},
    "invalid_question": {"field": STRING, "reason": STRING}, "invalid_exam": {"field": STRING},
    "invalid_blueprint": {"row": nullable(INTEGER), "reason": STRING}, "invalid_blueprint_row": {"reason": STRING},
    "invalid_order": {"reason": STRING},
}


def error_codes(codes, description=None, headers=None):
    names = codes.split()
    plain = [code for code in names if code not in ERROR_FIELDS]
    variants = [exact(code=enum(*plain), detail=STRING)] if plain else []
    variants.extend(exact(code=enum(code), detail=STRING, **ERROR_FIELDS[code]) for code in names if code in ERROR_FIELDS)
    schema = variants[0] if len(variants) == 1 else {"oneOf": variants}
    return {"description": description or ", ".join(names), "headers": headers or {},
            "content": {"application/json": {"schema": schema}}}


BASE = {
    401: {"model": ErrorBody, "description": "not_authenticated", "headers": BEARER},
    403: {"model": ErrorBody, "description": "account_disabled or exams_not_allowed. Requires the course's owner professor."},
    404: {"model": ErrorBody, "description": "Course hidden from the caller or requested resource unavailable in this course."},
    500: {"model": ErrorBody, "description": "server_error"},
}
BUSY = "exam_busy before commit or on read: wait for Retry-After. exams_unavailable: feature disabled."
UNKNOWN = (" exam_response_unavailable: the change may have committed. No Retry-After. Reload before editing; "
           "retry creation with the original key and body, or a transition with its original intent.")


def read(model, params=(), *, etag=False):
    invalid = "invalid_query invalid_cursor" if any(p["name"] == "cursor" for p in params) else "invalid_query"
    return {"openapi_extra": {"parameters": list(params)}, "responses": {
        **BASE, 200: {"model": model, "headers": ETAG if etag else {}},
        422: error_codes(invalid), 503: error_codes("exam_busy exams_unavailable", BUSY, RETRY_AFTER),
    }}


def mutation(model, body, *, keyed=False, replay=False, item=False, conflicts="", invalid=""):
    final = body is FINALIZE
    responses = {
        **BASE, 400: error_codes("invalid_body " + ("idempotency_key_required invalid_idempotency_key" if keyed
                                else "invalid_precondition") + (" invalid_preview_fingerprint" if final else "")),
        408: error_codes("request_timeout"), 413: error_codes("body_too_large"),
        415: error_codes("unsupported_media_type unsupported_encoding"), 422: error_codes("invalid_fields " + invalid),
        503: error_codes("exam_busy exams_unavailable exam_response_unavailable", BUSY + UNKNOWN, RETRY_AFTER),
    }
    if conflicts:
        responses[409] = error_codes(conflicts)
    if keyed:
        responses[201] = {"model": model, "description": "Created", "headers": {**ETAG, **LOCATION}}
        responses[200] = {"model": model, "description": "Replay, current representation", "headers": {**ETAG, **KEY_REPLAY}}
    else:
        responses[412] = error_codes("version_mismatch" + (" exam_preview_stale" if final else ""))
        responses[428] = error_codes("precondition_required" + (" preview_fingerprint_required" if final else ""))
        headers = {**ETAG, **(REPLAYED if replay else {})}
        if item:
            headers["X-Exam-Item-Id"] = header("New exam item id.", UUID, required=True)
        responses[201 if item else 200] = {"model": model, "headers": headers}
    return {"responses": responses, "openapi_extra": {
        **json_body(body, required=body is not EMPTY), "parameters": [IDEMPOTENCY if keyed else IF_MATCH],
    }}


def download(*, draft=False):
    from .export import FILE_HEADERS, MEDIA_TYPES

    headers = {name: header("File response policy.", enum(value), required=True) for name, value in FILE_HEADERS.items()}
    headers.update({
        "Content-Disposition": header("Attachment filename.", required=True), "Content-Length": header("Byte length.", INTEGER, required=True),
        "X-Exam-Renderer-Version": header("Renderer version.", required=True),
        "X-Exam-Content-SHA256": header("Frozen exam content digest on finalized files.", {**STRING, "pattern": "^[a-f0-9]{64}$"}, required=not draft),
        "X-Exam-Finalized-Version": header("Version frozen at finalization, on finalized files.", INTEGER, required=not draft),
    })
    if draft:
        headers.update({"X-Exam-Version": header("Draft version on watermarked files.", INTEGER),
                        "X-Exam-Preview-Fingerprint": header("Reviewed fingerprint on watermarked files.", FINGERPRINT)})
    binary = {"schema": {"type": "string", "format": "binary"}}
    return {"response_class": Response, "openapi_extra": {"parameters": FILE_QUERY}, "responses": {
        **BASE, 200: {"description": "Attachment in the requested format.", "headers": headers,
                      "content": {mime: binary for mime in MEDIA_TYPES.values()}},
        409: error_codes("exam_not_finalized" + (" export_budget_exceeded" if draft else "")),
        422: error_codes("invalid_export_option invalid_query"), 429: error_codes("export_busy too_many_exports", headers=RETRY_AFTER),
        500: error_codes("server_error export_failed"), 503: error_codes("exam_busy exams_unavailable exam_export_unavailable",
            BUSY + " exam_export_unavailable: file feature disabled.", RETRY_AFTER),
        504: error_codes("export_timeout"),
    }}
