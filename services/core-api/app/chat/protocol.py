"""studygraph.chat/1 message shapes and their strict validation (no I/O here).

The AI service proposes; core-api decides. Everything an AI response carries
is checked for exact keys, exact types (a bool is not an int), echoed
identities and internal consistency before anything is looked up in the
database (app/chat/verify.py) or shown to a user. A response that fails any
check is discarded as a whole; nothing is repaired or partially shown.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from .scopes import SCOPE_ORIGINS, SCOPES

PROTOCOL = "studygraph.chat/1"
SERVICE = "ai-service"
CAPABILITIES_PATH = "/ai/v1/chat/capabilities"
ANSWER_PATH = "/ai/v1/chat/answer"

CAPABILITY_KEYS = frozenset({"protocol", "service", "mock", "scopes", "max_question_chars"})
ANSWER_KEYS = frozenset(
    {"protocol", "request_id", "course_id", "scope", "mock", "grounded", "answer", "citations", "topic_ids",
     "used_web"}
)  # fmt: skip
CITATION_KEYS = frozenset({"marker", "chunk_id", "document_id", "origin", "quote"})
PUBLIC_ORIGINS = frozenset({"class", "external", "web"})
MAX_QUOTE_CHARS = 500
# Citation markers. CANONICAL_MARKER is the only valid spelling: ASCII digits, no sign, no leading zero.
# NUMERIC_BRACKET finds everything that merely looks like a numeric marker ([01], [12345], [ 1 ], [+1],
# [1,2], [1.0], full-width digits ...); anything it finds that is not canonical is a violation. Brackets that
# hold other text ("[H+]", "[A]", "[x]") are ordinary text and are not markers.
CANONICAL_MARKER = re.compile(r"[1-9][0-9]{0,3}")
NUMERIC_BRACKET = re.compile(r"\[([\s\d+\-\u2212.,;]*\d[\s\d+\-\u2212.,;]*)\]")
_ALLOWED_CONTROLS = frozenset("\n\t")


class ProtocolViolation(Exception):
    """The AI response does not follow the protocol; `reason` is for the log only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Capabilities:
    ready: bool
    reason: str
    scopes: frozenset[str] = frozenset()
    max_question_chars: int = 0


@dataclass(frozen=True)
class ProposedCitation:
    marker: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    origin: str
    quote: str


@dataclass(frozen=True)
class ProposedAnswer:
    grounded: bool
    text: str
    citations: tuple[ProposedCitation, ...]
    topic_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class Limits:
    max_answer_chars: int
    max_citations: int
    max_topics: int


def has_unpaired_surrogate(value: str) -> bool:
    """A lone UTF-16 surrogate (U+D800..U+DFFF). A valid pair in JSON ("\\ud83d\\ude00") is already one character."""
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def has_forbidden_controls(value: str) -> bool:
    """Control characters other than newline/tab, C1 controls, and unpaired surrogates (not valid text)."""
    return has_unpaired_surrogate(value) or any(
        (ord(ch) < 32 and ch not in _ALLOWED_CONTROLS) or 0x7F <= ord(ch) <= 0x9F for ch in value
    )


def _canonical_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def validate_capabilities(status: int, body: object) -> Capabilities:
    if status != 200:
        return Capabilities(False, f"capabilities_http_{status}")
    if not isinstance(body, dict) or set(body) != CAPABILITY_KEYS:
        return Capabilities(False, "capabilities_invalid")
    if body["mock"] is not False:  # true, missing or not a boolean: a mock never answers
        return Capabilities(False, "mock_service")
    if body["protocol"] != PROTOCOL:
        return Capabilities(False, "protocol_mismatch")
    if body["service"] != SERVICE:
        return Capabilities(False, "capabilities_invalid")
    scopes = body["scopes"]
    if (
        not isinstance(scopes, list)
        or not all(isinstance(s, str) and s in SCOPES for s in scopes)
        or len(set(scopes)) != len(scopes)
    ):
        return Capabilities(False, "capabilities_invalid")
    limit = body["max_question_chars"]
    if type(limit) is not int or limit < 1:
        return Capabilities(False, "capabilities_invalid")
    return Capabilities(True, "ready", frozenset(scopes), limit)


def build_request(
    request_id: uuid.UUID, course_id: uuid.UUID, scope: str, question: str, limits: Limits
) -> dict:
    return {
        "protocol": PROTOCOL,
        "request_id": str(request_id),
        "course_id": str(course_id),
        "scope": scope,
        "allowed_origins": sorted(SCOPE_ORIGINS[scope]),
        "question": question,
        "max_citations": limits.max_citations,
        "max_answer_chars": limits.max_answer_chars,
    }


def _citation(value: object, allowed_origins: frozenset[str], max_marker: int) -> ProposedCitation:
    if not isinstance(value, dict) or set(value) != CITATION_KEYS:
        raise ProtocolViolation("citation_keys")
    marker = value["marker"]
    if type(marker) is not int or not 1 <= marker <= max_marker:
        raise ProtocolViolation("citation_marker")
    chunk_id = _canonical_uuid(value["chunk_id"])
    document_id = _canonical_uuid(value["document_id"])
    if chunk_id is None or document_id is None:
        raise ProtocolViolation("citation_identity")
    origin = value["origin"]
    if not isinstance(origin, str) or origin not in PUBLIC_ORIGINS:
        raise ProtocolViolation("citation_origin")
    if origin not in allowed_origins:
        raise ProtocolViolation("citation_origin_not_permitted")
    quote = value["quote"]
    if (
        not isinstance(quote, str)
        or not quote.strip()
        or len(quote) > MAX_QUOTE_CHARS
        or has_forbidden_controls(quote)
    ):
        raise ProtocolViolation("citation_quote")
    return ProposedCitation(marker, chunk_id, document_id, origin, quote)


def text_markers(text: str, max_marker: int) -> set[int]:
    """The citation markers [n] used in the text, strictly.

    Every numeric-looking bracket must be a canonical marker (ASCII digits, no sign, no leading zero,
    no spaces or lists) within 1..max_marker; anything else rejects the answer.
    """
    markers = set()
    for match in NUMERIC_BRACKET.finditer(text):
        digits = match.group(1)
        if not CANONICAL_MARKER.fullmatch(digits):
            raise ProtocolViolation("marker_format")
        value = int(digits)
        if not 1 <= value <= max_marker:
            raise ProtocolViolation("marker_range")
        markers.add(value)
    return markers


def validate_answer(body: object, request: dict, limits: Limits) -> ProposedAnswer:
    if not isinstance(body, dict) or set(body) != ANSWER_KEYS:
        raise ProtocolViolation("answer_keys")
    if body["mock"] is not False:
        raise ProtocolViolation("mock_answer")
    for key in ("protocol", "request_id", "course_id", "scope"):
        if body[key] != request[key] or not isinstance(body[key], str):
            raise ProtocolViolation(f"echo_{key}")
    if body["used_web"] is not False:  # live web search is not available in any scope yet
        raise ProtocolViolation("used_web")
    grounded = body["grounded"]
    if type(grounded) is not bool:
        raise ProtocolViolation("grounded")
    text = body["answer"]
    if not isinstance(text, str) or len(text) > limits.max_answer_chars or has_forbidden_controls(text):
        raise ProtocolViolation("answer_text")
    citations_raw, topics_raw = body["citations"], body["topic_ids"]
    if not isinstance(citations_raw, list) or not isinstance(topics_raw, list):
        raise ProtocolViolation("lists")
    if len(topics_raw) > limits.max_topics:
        raise ProtocolViolation("too_many_topics")
    topic_ids = tuple(_canonical_uuid(t) for t in topics_raw)
    if any(t is None for t in topic_ids) or len(set(topic_ids)) != len(topic_ids):
        raise ProtocolViolation("topic_ids")

    if not grounded:
        if citations_raw:
            raise ProtocolViolation("ungrounded_with_citations")
        return ProposedAnswer(False, "", (), ())

    if not text.strip():
        raise ProtocolViolation("empty_answer")
    if not 1 <= len(citations_raw) <= limits.max_citations:
        raise ProtocolViolation("citation_count")
    allowed = SCOPE_ORIGINS[request["scope"]]
    citations = tuple(_citation(c, allowed, limits.max_citations) for c in citations_raw)
    markers = [c.marker for c in citations]
    if len(set(markers)) != len(markers):
        raise ProtocolViolation("duplicate_marker")
    # Every marker in the text has a citation and every citation is used (repeats are fine).
    if text_markers(text, limits.max_citations) != set(markers):
        raise ProtocolViolation("marker_mismatch")
    return ProposedAnswer(True, text, tuple(sorted(citations, key=lambda c: c.marker)), topic_ids)
