"""Proposed studygraph.quiz/1 message shapes and their strict validation (no I/O here).

PROPOSAL (docs/ai-quiz-integration.md). The shared OpenAPI contract is not
changed by M7; the AI team has to adopt this protocol before activation.

The AI service proposes questions; core-api decides. A generation response is
accepted only as a whole: exact keys and types (a bool is not an int), echoed
identities, exactly the requested number of questions, negotiated question
types, consistent difficulty, bounded text, unique stems and choices, answer
indexes in range and 1..MAX_CITATIONS citations per question. One violation
rejects the entire response; nothing is repaired, dropped or partially kept.
Provenance (chunk, document, course, locator and quote) is verified against
PostgreSQL afterwards (app/quiz/provenance.py), inside the persistence
transaction.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass

from ..chat.protocol import has_forbidden_controls
from ..chat.verify import VerificationFailed, public_locator
from .scopes import SCOPE_ORIGINS, SCOPES, parse_quiz_scopes  # noqa: F401  (re-exported)

PROTOCOL = "studygraph.quiz/1"
SERVICE = "ai-service"
CAPABILITIES_PATH = "/ai/v1/quiz/capabilities"
GENERATE_PATH = "/ai/v1/quiz/generate"

SUPPORTED_TYPES = ("mcq", "true_false")  # short answers are deferred
KNOWN_TYPES = frozenset({"mcq", "true_false", "short"})
DIFFICULTIES = ("easy", "medium", "hard")
REQUEST_DIFFICULTIES = (*DIFFICULTIES, "mixed")

MAX_STEM_CHARS = 1000
MAX_HINT_CHARS = 300
MAX_EXPLANATION_CHARS = 2000
MIN_CHOICES = 2
MAX_CHOICES = 6
MAX_CHOICE_CHARS = 300
MIN_CITATIONS = 1
MAX_CITATIONS = 4
MAX_QUOTE_CHARS = 500
MAX_MODEL_FIELD_CHARS = 100
# A correct answer this long or longer must not appear verbatim (normalized) in the stem or hint.
LEAK_MIN_CHARS = 4

CAPABILITY_KEYS = frozenset({"protocol", "service", "mock", "scopes", "question_types", "max_questions", "max_topics"})
RESPONSE_KEYS = frozenset({"protocol", "request_id", "course_id", "scope", "mock", "grounded", "model", "questions"})
MODEL_KEYS = frozenset({"name", "version"})
QUESTION_KEYS = frozenset(
    {"topic_id", "type", "difficulty", "stem", "choices", "answer", "hint", "explanation", "citations"}
)
CITATION_KEYS = frozenset({"chunk_id", "document_id", "origin", "locator", "quote"})

_WHITESPACE = re.compile(r"\s+")


class ProtocolViolation(Exception):
    """The AI response does not follow the protocol; `reason` is for the log only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class QuizCapabilities:
    ready: bool
    reason: str
    scopes: frozenset[str] = frozenset()
    question_types: frozenset[str] = frozenset()
    max_questions: int = 0
    max_topics: int = 0


@dataclass(frozen=True)
class ProposedCitation:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    origin: str
    locator: dict
    quote: str


@dataclass(frozen=True)
class ProposedQuestion:
    topic_id: uuid.UUID
    qtype: str
    difficulty: str
    stem: str
    choices: tuple[str, ...]  # mcq: the AI's order; true_false: ()
    answer_index: int | None  # mcq
    answer_bool: bool | None  # true_false
    hint: str
    explanation: str
    citations: tuple[ProposedCitation, ...]


@dataclass(frozen=True)
class ProposedQuiz:
    model_name: str
    model_version: str
    questions: tuple[ProposedQuestion, ...]


def normalize_text(value: str) -> str:
    """For comparisons only (uniqueness, leak heuristic): NFKC, casefold, collapsed whitespace."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


def canonical_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def validate_capabilities(status: int, body: object) -> QuizCapabilities:
    if status != 200:
        return QuizCapabilities(False, f"capabilities_http_{status}")
    if not isinstance(body, dict) or set(body) != CAPABILITY_KEYS:
        return QuizCapabilities(False, "capabilities_invalid")
    if body["mock"] is not False:  # true, null or not a boolean: a mock never generates
        return QuizCapabilities(False, "mock_service")
    if body["protocol"] != PROTOCOL:
        return QuizCapabilities(False, "protocol_mismatch")
    if body["service"] != SERVICE:
        return QuizCapabilities(False, "capabilities_invalid")
    scopes, types = body["scopes"], body["question_types"]
    for values, known in ((scopes, frozenset(("class_only", "class_plus_external", "class_plus_web"))), (types, KNOWN_TYPES)):
        if (
            not isinstance(values, list)
            or not all(isinstance(v, str) and v in known for v in values)
            or len(set(values)) != len(values)
        ):
            return QuizCapabilities(False, "capabilities_invalid")
    max_questions, max_topics = body["max_questions"], body["max_topics"]
    if type(max_questions) is not int or type(max_topics) is not int or max_questions < 1 or max_topics < 1:
        return QuizCapabilities(False, "capabilities_invalid")
    return QuizCapabilities(True, "ready", frozenset(scopes), frozenset(types), max_questions, max_topics)


def negotiate_types(capabilities: QuizCapabilities) -> tuple[str, ...]:
    """The question types both sides support, in a fixed order (empty: nothing to ask for)."""
    return tuple(t for t in SUPPORTED_TYPES if t in capabilities.question_types)


def build_request(
    request_id: uuid.UUID,
    course_id: uuid.UUID,
    scope: str,
    topics: list[tuple[uuid.UUID, str]],
    count: int,
    difficulty: str,
    question_types: tuple[str, ...],
) -> dict:
    return {
        "protocol": PROTOCOL,
        "request_id": str(request_id),
        "course_id": str(course_id),
        "scope": scope,
        "allowed_origins": list(SCOPE_ORIGINS[scope]),
        "topics": [{"topic_id": str(topic_id), "name": name} for topic_id, name in topics],
        "count": count,
        "difficulty": difficulty,
        "question_types": list(question_types),
        "limits": {
            "max_stem_chars": MAX_STEM_CHARS,
            "max_hint_chars": MAX_HINT_CHARS,
            "max_explanation_chars": MAX_EXPLANATION_CHARS,
            "min_choices": MIN_CHOICES,
            "max_choices": MAX_CHOICES,
            "max_choice_chars": MAX_CHOICE_CHARS,
            "min_citations": MIN_CITATIONS,
            "max_citations": MAX_CITATIONS,
            "max_quote_chars": MAX_QUOTE_CHARS,
        },
    }


def _text(value: object, max_chars: int, reason: str, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars or has_forbidden_controls(value):
        raise ProtocolViolation(reason)
    if single_line and ("\n" in value or "\t" in value):
        raise ProtocolViolation(reason)
    return value.strip()


def _citation(value: object, allowed_origins: tuple[str, ...]) -> ProposedCitation:
    if not isinstance(value, dict) or set(value) != CITATION_KEYS:
        raise ProtocolViolation("citation_keys")
    chunk_id, document_id = canonical_uuid(value["chunk_id"]), canonical_uuid(value["document_id"])
    if chunk_id is None or document_id is None:
        raise ProtocolViolation("citation_identity")
    origin = value["origin"]
    if not isinstance(origin, str) or origin not in allowed_origins:
        raise ProtocolViolation("citation_origin")
    locator = value["locator"]
    try:
        public_locator(locator)  # exact known shape: {"page": n}, {"slide": n}, {"section"|"heading": s}
    except VerificationFailed:
        raise ProtocolViolation("citation_locator") from None
    quote = _text(value["quote"], MAX_QUOTE_CHARS, "citation_quote")
    return ProposedCitation(chunk_id, document_id, origin, locator, quote)


def _question(value: object, request: dict, topic_ids: frozenset[uuid.UUID]) -> ProposedQuestion:
    if not isinstance(value, dict) or set(value) != QUESTION_KEYS:
        raise ProtocolViolation("question_keys")
    topic_id = canonical_uuid(value["topic_id"])
    if topic_id is None or topic_id not in topic_ids:
        raise ProtocolViolation("question_topic")
    qtype = value["type"]
    if not isinstance(qtype, str) or qtype not in request["question_types"]:
        raise ProtocolViolation("question_type")
    difficulty = value["difficulty"]
    if not isinstance(difficulty, str) or difficulty not in DIFFICULTIES:
        raise ProtocolViolation("question_difficulty")
    if request["difficulty"] != "mixed" and difficulty != request["difficulty"]:
        raise ProtocolViolation("question_difficulty_mismatch")
    stem = _text(value["stem"], MAX_STEM_CHARS, "question_stem")
    hint = _text(value["hint"], MAX_HINT_CHARS, "question_hint")
    explanation = _text(value["explanation"], MAX_EXPLANATION_CHARS, "question_explanation")
    choices_raw, answer = value["choices"], value["answer"]
    if qtype == "mcq":
        if not isinstance(choices_raw, list) or not MIN_CHOICES <= len(choices_raw) <= MAX_CHOICES:
            raise ProtocolViolation("mcq_choices")
        choices = tuple(_text(c, MAX_CHOICE_CHARS, "mcq_choice", single_line=True) for c in choices_raw)
        if len({normalize_text(c) for c in choices}) != len(choices):
            raise ProtocolViolation("mcq_duplicate_choice")
        if type(answer) is not int or not 0 <= answer < len(choices):
            raise ProtocolViolation("mcq_answer")
        correct = normalize_text(choices[answer])
        if len(correct) >= LEAK_MIN_CHARS and (correct in normalize_text(stem) or correct in normalize_text(hint)):
            raise ProtocolViolation("answer_leak")
        answer_index, answer_bool = answer, None
    else:  # true_false
        if choices_raw is not None:
            raise ProtocolViolation("true_false_choices")
        if type(answer) is not bool:
            raise ProtocolViolation("true_false_answer")
        choices, answer_index, answer_bool = (), None, answer
    citations_raw = value["citations"]
    if not isinstance(citations_raw, list) or not MIN_CITATIONS <= len(citations_raw) <= MAX_CITATIONS:
        raise ProtocolViolation("citation_count")
    allowed = tuple(request["allowed_origins"])
    citations = tuple(_citation(c, allowed) for c in citations_raw)
    if len({(c.chunk_id, normalize_text(c.quote)) for c in citations}) != len(citations):
        raise ProtocolViolation("citation_duplicate")
    return ProposedQuestion(
        topic_id, qtype, difficulty, stem, choices, answer_index, answer_bool, hint, explanation, citations
    )


def validate_response(body: object, request: dict) -> ProposedQuiz:
    if not isinstance(body, dict) or set(body) != RESPONSE_KEYS:
        raise ProtocolViolation("response_keys")
    if body["mock"] is not False:
        raise ProtocolViolation("mock_response")
    for key in ("protocol", "request_id", "course_id", "scope"):
        if not isinstance(body[key], str) or body[key] != request[key]:
            raise ProtocolViolation(f"echo_{key}")
    if body["grounded"] is not True:
        raise ProtocolViolation("not_grounded")
    model = body["model"]
    if not isinstance(model, dict) or set(model) != MODEL_KEYS:
        raise ProtocolViolation("model")
    name = _text(model["name"], MAX_MODEL_FIELD_CHARS, "model", single_line=True)
    version = _text(model["version"], MAX_MODEL_FIELD_CHARS, "model", single_line=True)
    questions_raw = body["questions"]
    if not isinstance(questions_raw, list) or len(questions_raw) != request["count"]:
        raise ProtocolViolation("question_count")
    topic_ids = frozenset(uuid.UUID(t["topic_id"]) for t in request["topics"])
    questions = tuple(_question(q, request, topic_ids) for q in questions_raw)
    if len({normalize_text(q.stem) for q in questions}) != len(questions):
        raise ProtocolViolation("duplicate_stem")
    return ProposedQuiz(name, version, questions)
