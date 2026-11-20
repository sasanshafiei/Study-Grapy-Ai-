"""Index request payloads: built from the database, validated, canonical, content-addressed.

Wire format: the CE-side *proposal* "studygraph.index/1"
(docs/ai-indexing-integration.md). It is not part of the shared OpenAPI
contract until CE and AI approve it.

Determinism. The body is canonical JSON: keys sorted, no insignificant
whitespace, UTF-8 without ASCII escaping, sections in ordinal order. The same
database content always gives the same bytes.

Identity.
- content_digest = SHA-256 of the canonical JSON of {"protocol", "document",
  "sections"}: everything the AI service indexes, including the document and
  course ids, so identical text in two courses (or two documents) never shares
  a digest.
- idempotency_key = SHA-256 of "studygraph.index/1", document id, course id
  and content_digest, NUL-separated. It is a pure function of the content and
  its identity: every attempt of a job, and a later retry-index of unchanged
  content, sends the same key. It never depends on job ids, attempt numbers or
  timestamps.

Section locators (M6). A harvested external document (one with an
external_document_versions row) is section-based: its sections carry
{"anchor", "heading", "level"} locators, and its document object gains
"locator_scheme": "section" and an "attribution" object (provider, page and
revision identity, revision URL, licence URL, attribution text) taken from
its immutable provenance row. Every other document keeps exactly the M4
page-based body: the same keys, bytes, content digest and idempotency key as
before (no new key is added to it). A service receives a section-based
document only after it advertised "section" in its capabilities' optional
"locators" list (app/indexing/client.py, app/indexing/pipeline.py).

Nothing here talks to the network; load_content only reads.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

PROTOCOL = "studygraph.index/1"
ORIGINS = ("class", "external", "web_cache")
MAX_PAGE = 2**31 - 1
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


class InvalidIndexContent(ValueError):
    """The stored document cannot be expressed as a valid index request (permanent)."""


LOCATOR_SCHEMES = ("page", "section")
MAX_ANCHOR = 255
_ATTRIBUTION_KEYS = ("page_id", "revision_id", "revision_url", "canonical_url", "licence_url", "attribution", "provider")


@dataclass(frozen=True)
class IndexRequest:
    document_id: uuid.UUID
    course_id: uuid.UUID
    content_digest: str
    idempotency_key: str
    section_count: int
    body: bytes  # canonical JSON
    locator_scheme: str = "page"

    def __repr__(self) -> str:  # never dumps document text into logs
        return (
            f"IndexRequest(document_id={self.document_id}, course_id={self.course_id}, "
            f"sections={self.section_count}, bytes={len(self.body)}, key={self.idempotency_key[:12]}...)"
        )


def canonical_json(value: object) -> bytes:
    """Canonical encoding; raises InvalidIndexContent for values that cannot be encoded exactly."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise InvalidIndexContent(f"not encodable: {type(exc).__name__}") from None


def _optional_str(value, name: str, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidIndexContent(f"{name} must be a string")
    if max_len is not None and len(value) > max_len:
        raise InvalidIndexContent(f"{name} is longer than {max_len}")
    return value


def _document(metadata: Mapping) -> dict:
    try:
        document_id = uuid.UUID(str(metadata["id"]))
    except (KeyError, ValueError):
        raise InvalidIndexContent("document id missing or not a UUID") from None
    course = metadata.get("course_id")
    if course is None:
        # Course isolation: nothing is indexed without the course it belongs to.
        raise InvalidIndexContent("the document has no course")
    try:
        course_id = uuid.UUID(str(course))
    except ValueError:
        raise InvalidIndexContent("course id is not a UUID") from None
    origin = metadata.get("origin")
    if origin not in ORIGINS:
        raise InvalidIndexContent("origin is not one of class, external, web_cache")
    title = _optional_str(metadata.get("title"), "title", 255)
    if not title or not title.strip():
        raise InvalidIndexContent("title is empty")
    language = _optional_str(metadata.get("language"), "language", 10)
    if not language:
        raise InvalidIndexContent("language is empty")
    source_sha256 = _optional_str(metadata.get("content_hash"), "content_hash")
    if source_sha256 is not None and not _HEX64.match(source_sha256):
        raise InvalidIndexContent("content_hash is not a lowercase SHA-256")
    page_count = metadata.get("page_count")
    if page_count is not None and (type(page_count) is not int or not 1 <= page_count <= MAX_PAGE):
        raise InvalidIndexContent("page_count is not a positive integer")
    return {
        "id": str(document_id),
        "course_id": str(course_id),
        "origin": origin,
        "kind": _optional_str(metadata.get("kind"), "kind", 20),
        "title": title,
        "url": _optional_str(metadata.get("url"), "url", 4096),
        "licence": _optional_str(metadata.get("licence"), "licence", 100),
        "language": language,
        "source_sha256": source_sha256,
        "page_count": page_count,
    }


def _sections(rows: Sequence[Mapping], page_count: int | None) -> list[dict]:
    if not rows:
        raise InvalidIndexContent("the document has no sections")
    sections = []
    previous_page = 0
    for expected, row in enumerate(rows):
        ordinal, body, locator = row.get("ordinal"), row.get("text"), row.get("locator")
        if type(ordinal) is not int or ordinal != expected:
            raise InvalidIndexContent(f"section ordinals are not contiguous from 0 (at {expected})")
        if not isinstance(body, str) or not body.strip():
            raise InvalidIndexContent(f"section {expected} has no text")
        if not isinstance(locator, Mapping) or set(locator) != {"page"}:
            raise InvalidIndexContent(f"section {expected} locator is not exactly {{page}}")
        page = locator["page"]
        if type(page) is not int or not 1 <= page <= MAX_PAGE:
            raise InvalidIndexContent(f"section {expected} page is not a positive integer")
        if page <= previous_page:
            raise InvalidIndexContent(f"section {expected} page does not increase")
        if page_count is not None and page > page_count:
            raise InvalidIndexContent(f"section {expected} page is beyond the page count")
        previous_page = page
        sections.append({"ordinal": ordinal, "locator": {"page": page}, "text": body})
    return sections


def _section_sections(rows: Sequence[Mapping]) -> list[dict]:
    if not rows:
        raise InvalidIndexContent("the document has no sections")
    sections = []
    for expected, row in enumerate(rows):
        ordinal, body, locator = row.get("ordinal"), row.get("text"), row.get("locator")
        if type(ordinal) is not int or ordinal != expected:
            raise InvalidIndexContent(f"section ordinals are not contiguous from 0 (at {expected})")
        if not isinstance(body, str) or not body.strip():
            raise InvalidIndexContent(f"section {expected} has no text")
        if not isinstance(locator, Mapping) or set(locator) != {"anchor", "heading", "level"}:
            raise InvalidIndexContent(f"section {expected} locator is not exactly {{anchor, heading, level}}")
        anchor, heading, level = locator["anchor"], locator["heading"], locator["level"]
        if type(level) is not int:
            raise InvalidIndexContent(f"section {expected} level is not an integer")
        if level == 0:
            if expected != 0 or anchor is not None or heading is not None:
                raise InvalidIndexContent(f"section {expected}: only the first section may be the lead")
        elif 2 <= level <= 6:
            for name, value in (("anchor", anchor), ("heading", heading)):
                if not isinstance(value, str) or not value.strip() or len(value) > MAX_ANCHOR:
                    raise InvalidIndexContent(f"section {expected} {name} is not a non-empty string")
        else:
            raise InvalidIndexContent(f"section {expected} level is not 0 or 2..6")
        sections.append({"ordinal": ordinal, "locator": {"anchor": anchor, "heading": heading, "level": level}, "text": body})
    return sections


def _attribution(provenance: Mapping) -> dict:
    if set(_ATTRIBUTION_KEYS) - set(provenance):
        raise InvalidIndexContent("external provenance is incomplete")
    for key in ("page_id", "revision_id"):
        if type(provenance[key]) is not int or provenance[key] < 1:
            raise InvalidIndexContent(f"{key} is not a positive integer")
    result = {"page_id": provenance["page_id"], "revision_id": provenance["revision_id"]}
    for key in ("revision_url", "canonical_url", "licence_url", "attribution", "provider"):
        result[key] = _optional_str(provenance[key], key, 4096)
        if not result[key]:
            raise InvalidIndexContent(f"{key} is empty")
    return result


def build_request(metadata: Mapping, section_rows: Sequence[Mapping]) -> IndexRequest:
    """Validate the content and build its canonical request. Pure and deterministic.

    metadata["_provenance"] (set by load_content for harvested documents)
    selects the section scheme; without it the M4 page scheme applies, byte
    for byte as before.
    """
    document = _document(metadata)
    provenance = metadata.get("_provenance")
    if provenance is None:
        scheme = "page"
        sections = _sections(section_rows, document["page_count"])
    else:
        if document["origin"] != "external" or document["page_count"] is not None:
            raise InvalidIndexContent("only fileless external documents are section-based")
        scheme = "section"
        document["locator_scheme"] = "section"
        document["attribution"] = _attribution(provenance)
        sections = _section_sections(section_rows)
    content = {"protocol": PROTOCOL, "document": document, "sections": sections}
    content_digest = hashlib.sha256(canonical_json(content)).hexdigest()
    key = idempotency_key(document["id"], document["course_id"], content_digest)
    body = canonical_json({**content, "content_digest": content_digest, "idempotency_key": key})
    return IndexRequest(
        document_id=uuid.UUID(document["id"]),
        course_id=uuid.UUID(document["course_id"]),
        content_digest=content_digest,
        idempotency_key=key,
        section_count=len(sections),
        body=body,
        locator_scheme=scheme,
    )


def idempotency_key(document_id: str, course_id: str, content_digest: str) -> str:
    material = "\0".join((PROTOCOL, document_id, course_id, content_digest)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


_METADATA = text(
    "SELECT id, course_id, origin, kind, title, url, licence, language, content_hash, page_count "
    "FROM documents WHERE id = :id"
)
_SECTIONS = "SELECT ordinal, text, locator FROM document_sections WHERE document_id = :id ORDER BY ordinal"
# Immutable provenance of a harvested version (M6); its stale columns are not read.
_PROVENANCE = text(
    "SELECT v.page_id, v.revision_id, v.revision_url, v.canonical_url, v.licence_url, v.attribution, s.provider "
    "FROM external_document_versions v JOIN documents d ON d.id = v.document_id "
    "JOIN sources s ON s.id = d.source_id WHERE v.document_id = :id"
)


def load_content(session: Session, document_id: uuid.UUID, *, lock_sections: bool = False):
    """(metadata, sections) of a document as stored now; None if the document does not exist.

    With lock_sections the section rows are read FOR SHARE, so they cannot
    change until the caller's transaction ends (used at completion, where the
    caller already holds the document row lock).
    """
    metadata = session.execute(_METADATA, {"id": document_id}).mappings().one_or_none()
    if metadata is None:
        return None
    query = _SECTIONS + (" FOR SHARE" if lock_sections else "")
    sections = [dict(row) for row in session.execute(text(query), {"id": document_id}).mappings()]
    metadata = dict(metadata)
    if metadata["origin"] == "external":
        provenance = session.execute(_PROVENANCE, {"id": document_id}).mappings().one_or_none()
        if provenance is not None:
            metadata["_provenance"] = dict(provenance)
    return metadata, sections


def request_for(session: Session, document_id: uuid.UUID, *, lock_sections: bool = False) -> IndexRequest:
    loaded = load_content(session, document_id, lock_sections=lock_sections)
    if loaded is None:
        raise InvalidIndexContent("no such document")
    return build_request(*loaded)
