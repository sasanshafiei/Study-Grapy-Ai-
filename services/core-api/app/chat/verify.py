"""Source verification of a proposed answer against PostgreSQL, and the public message.

Runs in ONE short transaction (REPEATABLE READ, READ ONLY, statement and lock timeouts),
so the authorization re-check, every citation and every topic are judged on
the same snapshot. It is called after the AI call has finished; no database
session is held across upstream HTTP or client streaming.

A citation is accepted only if ALL hold, else the whole answer is rejected:
- the chunk exists and belongs to the cited document;
- chunk origin == document origin, and its public form (class, external,
  web) == the cited origin, which the requested scope permits;
- the document is `indexed` (pending, failed and stale documents never
  surface, M4 §7);
- class material: chunk course == document course == the requested course;
- external material: the scope permits it, the document comes from a source
  that exists and is enabled, and chunk course == document course == the
  requested course (course-scoped, exactly like class material; globally
  shared external material, both course ids NULL, is refused until M6);
- web material: never (live web search is not available yet);
- the quote, whitespace-normalized, occurs verbatim in the chunk text and is
  not trivially short;
- the public metadata derived from the records is safe: a title without
  control characters, an http(s) URL without credentials (or none), and a
  locator of a known shape;
- harvested external material (documents.external_resource_id set) has the
  provenance record of exactly the cited document version
  (external_document_versions.document_id = the cited document, of the same
  resource), with safe URLs, a revision id, an attribution and a licence.
Topics must be topics of the requested course.

Public fields come from the database records, never from the AI response:
title (documents.title), url (documents.url, external only), locator
(chunks.locator: {"page": n} -> "Page n", the physical PDF page, since slide
numbering is not known; {"slide": n} -> "Slide n" only when the indexer
recorded real slide numbers; {"section": s} or {"heading": s} -> "Section s"),
snippet (the matching passage of chunks.text), provenance (WP-5: the cited
version's external_document_versions row - source_url, revision_id,
revision_url, attribution, licence, licence_url - or null for class material
and for external material that was not harvested; never filled in or guessed).

Verification establishes provenance only: each quoted passage exists in a
permitted, indexed source of this course. It does NOT establish that the
passage supports (entails) the sentence it is attached to.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..services.course_access import require_read
from .db import apply_short_timeouts
from .protocol import ProposedAnswer, has_forbidden_controls
from .scopes import SCOPE_ORIGINS

INSUFFICIENT_EVIDENCE = (
    "I could not find enough support in this course's materials to answer that. "
    "Try rephrasing the question, or ask your professor."
)
SNIPPET_MAX_CHARS = 300
MIN_QUOTE_CHARS = 20
MAX_URL_CHARS = 2048
MAX_SECTION_CHARS = 40
MAX_ATTRIBUTION_CHARS = 2000
MAX_SAFE_INTEGER = 2**53 - 1  # revision ids must be exact in JavaScript clients

_LICENCE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 .+-]{0,98}[A-Za-z0-9.+-])?")
_VERSION_COLUMNS = ("resource_id", "revision_id", "canonical_url", "revision_url", "attribution", "licence", "licence_url")

_PUBLIC_ORIGIN = {"class": "class", "external": "external", "web_cache": "web"}
_WHITESPACE = re.compile(r"\s+")


class VerificationFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedAnswer:
    grounded: bool
    text: str
    citations: tuple[dict, ...]
    topic_ids: tuple[uuid.UUID, ...]


def normalize(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def public_locator(locator: object) -> str:
    if not isinstance(locator, dict) or len(locator) != 1:
        raise VerificationFailed("locator_shape")
    (key, value), = locator.items()
    if key in ("page", "slide"):
        if type(value) is not int or not 1 <= value <= 2_147_483_647:
            raise VerificationFailed("locator_number")
        return f"{'Page' if key == 'page' else 'Slide'} {value}"
    if key in ("section", "heading"):
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_SECTION_CHARS or has_forbidden_controls(value) or "\n" in value or "\t" in value:
            raise VerificationFailed("locator_section")
        return f"Section {value.strip()}"
    raise VerificationFailed("locator_shape")


def public_url(url: object) -> str | None:
    if url is None:
        return None
    if not isinstance(url, str) or len(url) > MAX_URL_CHARS or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise VerificationFailed("url_unsafe")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise VerificationFailed("url_unsafe") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise VerificationFailed("url_unsafe")
    return url


def public_provenance(resource_id: uuid.UUID | None, version: Mapping[str, object] | None) -> dict | None:
    """Version-specific attribution of a cited external document, copied from its own version record.

    `resource_id` is documents.external_resource_id of the cited document and
    `version` its external_document_versions row (None when there is none).
    Missing or malformed metadata of a harvested document rejects the answer.
    """
    if version is None:
        if resource_id is None:
            return None  # not harvested: no version record, so there is nothing to show (and nothing is invented)
        raise VerificationFailed("provenance_missing")
    if resource_id is None or version["resource_id"] != resource_id:
        raise VerificationFailed("provenance_mismatch")
    revision_id = version["revision_id"]
    if type(revision_id) is not int or not 1 <= revision_id <= MAX_SAFE_INTEGER:
        raise VerificationFailed("provenance_revision")
    attribution = version["attribution"]
    if (not isinstance(attribution, str) or not attribution.strip() or len(attribution) > MAX_ATTRIBUTION_CHARS
            or has_forbidden_controls(attribution) or "\n" in attribution or "\t" in attribution):  # fmt: skip
        raise VerificationFailed("provenance_attribution")
    licence = version["licence"]
    if not isinstance(licence, str) or not _LICENCE.fullmatch(licence):
        raise VerificationFailed("provenance_licence")
    urls = [public_url(version[key]) for key in ("canonical_url", "revision_url", "licence_url")]
    if any(url is None for url in urls):
        raise VerificationFailed("url_unsafe")
    source_url, revision_url, licence_url = urls
    return {"source_url": source_url, "revision_id": revision_id, "revision_url": revision_url,
            "attribution": attribution.strip(), "licence": licence, "licence_url": licence_url}  # fmt: skip


def public_title(title: object) -> str:
    if not isinstance(title, str) or not title.strip() or len(title) > 255 or has_forbidden_controls(title) or "\n" in title:
        raise VerificationFailed("title_unsafe")
    return title.strip()


def snippet_for(chunk_text: str, quote: str) -> str:
    """The passage of the chunk that the quote matched (normalized), from the database text."""
    haystack, needle = normalize(chunk_text), normalize(quote)
    if len(needle) < min(MIN_QUOTE_CHARS, len(haystack)):
        raise VerificationFailed("quote_too_short")
    start = haystack.find(needle)
    if start < 0:
        raise VerificationFailed("quote_not_in_chunk")
    passage = haystack[start : start + len(needle)]
    return passage if len(passage) <= SNIPPET_MAX_CHARS else passage[: SNIPPET_MAX_CHARS - 1].rstrip() + "…"


def _begin_snapshot(session: Session) -> None:
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    session.execute(text("SET TRANSACTION READ ONLY"))
    apply_short_timeouts(session)  # statement and lock timeouts (app/chat/db.py)


def verify_answer(
    session: Session,
    user: CurrentUser,
    course_id: uuid.UUID,
    scope: str,
    proposed: ProposedAnswer,
) -> VerifiedAnswer:
    """Re-check access and verify every citation and topic in one consistent snapshot.

    Raises ApiError (access lost) or VerificationFailed (answer rejected).
    """
    _begin_snapshot(session)
    require_read(session, user, course_id)
    if not proposed.grounded:
        return VerifiedAnswer(False, INSUFFICIENT_EVIDENCE, (), ())

    allowed = SCOPE_ORIGINS[scope]
    chunk_ids = list({c.chunk_id for c in proposed.citations})
    rows = session.execute(
        text(
            "SELECT c.id, c.document_id, c.course_id AS chunk_course, c.origin AS chunk_origin, c.text, c.locator, "
            "d.course_id AS doc_course, d.origin AS doc_origin, d.status, d.title, d.url, d.source_id, "
            "d.external_resource_id, s.enabled AS source_enabled, v.document_id AS v_document_id, "
            + ", ".join(f"v.{column} AS v_{column}" for column in _VERSION_COLUMNS)
            + " FROM chunks c JOIN documents d ON d.id = c.document_id "
            "LEFT JOIN sources s ON s.id = d.source_id "
            "LEFT JOIN external_document_versions v ON v.document_id = d.id WHERE c.id = ANY(:ids)"
        ),
        {"ids": chunk_ids},
    ).all()
    by_id = {r.id: r for r in rows}

    citations = []
    for cited in proposed.citations:
        row = by_id.get(cited.chunk_id)
        if row is None:
            raise VerificationFailed("unknown_chunk")
        if row.document_id != cited.document_id:
            raise VerificationFailed("document_mismatch")
        if row.chunk_origin != row.doc_origin or row.doc_origin not in _PUBLIC_ORIGIN:
            raise VerificationFailed("origin_mismatch")
        origin = _PUBLIC_ORIGIN[row.doc_origin]
        if origin != cited.origin or origin not in allowed:
            raise VerificationFailed("origin_not_permitted")
        if row.status != "indexed":
            raise VerificationFailed("document_not_indexed")
        if origin == "class":
            if row.chunk_course != course_id or row.doc_course != course_id:
                raise VerificationFailed("course_mismatch")
            url = provenance = None
        elif origin == "external":
            if row.source_id is None or row.source_enabled is not True:
                raise VerificationFailed("source_not_enabled")
            if row.chunk_course != course_id or row.doc_course != course_id:
                raise VerificationFailed("course_mismatch")  # NULL (shared) is refused until M6
            url = public_url(row.url)
            version = None if row.v_document_id is None else {c: getattr(row, f"v_{c}") for c in _VERSION_COLUMNS}
            provenance = public_provenance(row.external_resource_id, version)
        else:
            raise VerificationFailed("web_not_available")
        citations.append(
            {
                "marker": cited.marker,
                "chunk_id": str(row.id),
                "origin": origin,
                "title": public_title(row.title),
                "locator": public_locator(row.locator),
                "url": url,
                "snippet": snippet_for(row.text, cited.quote),
                "provenance": provenance,
            }
        )

    if proposed.topic_ids:
        found = set(
            session.execute(
                text("SELECT id FROM topics WHERE course_id = :c AND id = ANY(:ids)"),
                {"c": course_id, "ids": list(proposed.topic_ids)},
            ).scalars()
        )
        if found != set(proposed.topic_ids):
            raise VerificationFailed("topic_not_in_course")
    return VerifiedAnswer(True, proposed.text, tuple(citations), proposed.topic_ids)
