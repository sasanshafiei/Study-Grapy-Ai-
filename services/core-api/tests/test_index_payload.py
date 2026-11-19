"""Index request payloads: canonical, deterministic, validated, content-addressed, course-isolated.

No database or network: build_request is a pure function of the stored rows.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from app.indexing.payload import PROTOCOL, InvalidIndexContent, build_request, canonical_json, idempotency_key

DOC = uuid.UUID("11111111-1111-4111-8111-111111111111")
COURSE_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
COURSE_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def metadata(**overrides):
    values = {
        "id": DOC, "course_id": COURSE_A, "origin": "class", "kind": "notes", "title": "Lecture 4.pdf",
        "url": None, "licence": None, "language": "en", "content_hash": "a" * 64, "page_count": 5,
    }  # fmt: skip
    values.update(overrides)
    return values


def sections(*pages_and_texts):
    pages_and_texts = pages_and_texts or ((1, "H₂O → H⁺ + OH⁻"), (3, "Le Chatelier « ok »"))
    return [{"ordinal": i, "text": t, "locator": {"page": p}} for i, (p, t) in enumerate(pages_and_texts)]


def test_same_content_gives_identical_bytes_digest_and_key():
    first = build_request(metadata(), sections())
    second = build_request(dict(reversed(list(metadata().items()))), [dict(reversed(list(s.items()))) for s in sections()])
    assert first.body == second.body
    assert (first.content_digest, first.idempotency_key) == (second.content_digest, second.idempotency_key)


def test_body_is_canonical_json_with_identity_fields():
    request = build_request(metadata(), sections())
    body = json.loads(request.body)
    assert request.body == json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert "H₂O" in request.body.decode("utf-8")  # UTF-8, not \\u escapes
    assert set(body) == {"protocol", "document", "sections", "content_digest", "idempotency_key"}
    assert body["protocol"] == PROTOCOL
    assert body["document"]["course_id"] == str(COURSE_A)
    assert body["sections"] == [
        {"ordinal": 0, "locator": {"page": 1}, "text": "H₂O → H⁺ + OH⁻"},
        {"ordinal": 1, "locator": {"page": 3}, "text": "Le Chatelier « ok »"},
    ]


def test_digest_covers_exactly_protocol_document_and_sections():
    request = build_request(metadata(), sections())
    body = json.loads(request.body)
    content = {k: body[k] for k in ("protocol", "document", "sections")}
    assert request.content_digest == hashlib.sha256(canonical_json(content)).hexdigest()
    assert request.idempotency_key == idempotency_key(str(DOC), str(COURSE_A), request.content_digest)


def test_course_isolation_same_text_in_another_course_has_another_digest_and_key():
    a = build_request(metadata(course_id=COURSE_A), sections())
    b = build_request(metadata(course_id=COURSE_B), sections())
    assert a.content_digest != b.content_digest
    assert a.idempotency_key != b.idempotency_key
    assert json.loads(b.body)["document"]["course_id"] == str(COURSE_B)


def test_another_document_with_the_same_text_has_another_key():
    other = build_request(metadata(id=uuid.uuid4()), sections())
    assert other.idempotency_key != build_request(metadata(), sections()).idempotency_key


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Lecture 4 (v2).pdf"},
        {"licence": "CC-BY-4.0"},
        {"language": "tr"},
        {"content_hash": "b" * 64},
        {"page_count": 6},
        {"kind": "slides"},
        {"url": "https://example.test/x"},
    ],
)
def test_any_metadata_change_changes_the_digest(change):
    assert build_request(metadata(**change), sections()).content_digest != build_request(metadata(), sections()).content_digest


def test_any_text_or_locator_change_changes_the_digest():
    base = build_request(metadata(), sections()).content_digest
    assert build_request(metadata(), sections((1, "H₂O → H⁺ + OH⁻ "), (3, "Le Chatelier « ok »"))).content_digest != base
    assert build_request(metadata(), sections((2, "H₂O → H⁺ + OH⁻"), (3, "Le Chatelier « ok »"))).content_digest != base


def test_key_is_independent_of_jobs_attempts_and_time():
    # Nothing but content and identity goes in: no job id, attempt or timestamp parameter exists.
    assert build_request(metadata(), sections()).idempotency_key == build_request(metadata(), sections()).idempotency_key


@pytest.mark.parametrize(
    "meta, rows, message",
    [
        (metadata(course_id=None), sections(), "no course"),
        (metadata(origin="other"), sections(), "origin"),
        (metadata(title="  "), sections(), "title"),
        (metadata(language=""), sections(), "language"),
        (metadata(content_hash="ABC"), sections(), "content_hash"),
        (metadata(page_count=0), sections(), "page_count"),
        (metadata(page_count=True), sections(), "page_count"),
        (metadata(), [], "no sections"),
        (metadata(), [{"ordinal": 1, "text": "x", "locator": {"page": 1}}], "contiguous"),
        (metadata(), [{"ordinal": 0, "text": " \n", "locator": {"page": 1}}], "no text"),
        (metadata(), [{"ordinal": 0, "text": "x", "locator": {"page": 1, "line": 2}}], "locator"),
        (metadata(), [{"ordinal": 0, "text": "x", "locator": {"page": "1"}}], "page"),
        (metadata(), [{"ordinal": 0, "text": "x", "locator": {"page": True}}], "page"),
        (metadata(), sections((2, "a"), (2, "b")), "increase"),
        (metadata(page_count=2), sections((1, "a"), (3, "b")), "beyond"),
        (metadata(), [{"ordinal": 0, "text": "bad \ud800 surrogate", "locator": {"page": 1}}], "encodable"),
    ],
)
def test_invalid_content_is_refused(meta, rows, message):
    with pytest.raises(InvalidIndexContent, match=message):
        build_request(meta, rows)


def test_repr_never_contains_document_text():
    request = build_request(metadata(), sections())
    assert "H₂O" not in repr(request) and "Le Chatelier" not in repr(request)
