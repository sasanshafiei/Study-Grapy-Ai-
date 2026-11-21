"""Strict validation of exam request bodies, and the canonical content hash of a question.

Everything here is pure (no database): the parts that need the database (topic of this course, class
documents and page counts) are checked by the transactions in bank.py and builder.py.

Text rules (contract §1.1, §1.9): strings are trimmed; no control characters except newline and tab in
multi-line fields, none at all in single-line fields (titles, choice texts); no unpaired surrogates;
lengths are Unicode code points. Booleans are never accepted as integers.

True/false (H1): `choices` is null or exactly the canonical array; both are stored, hashed, returned and
printed as the canonical array (M7's TRUE_FALSE_CHOICES), so the two request forms are the same content.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field

from ..chat.protocol import has_forbidden_controls
from ..errors import ApiError
from ..quiz.protocol import canonical_uuid
from .http import sha256_hex

QUESTION_TYPES = ("mcq", "true_false", "short")
DIFFICULTIES = ("easy", "medium", "hard")
BLOOM_LEVELS = ("remember", "understand", "apply", "analyze", "evaluate", "create")
CHOICE_LABELS = "abcdef"
TRUE_FALSE_CHOICES = ({"id": "true", "text": "True"}, {"id": "false", "text": "False"})
QUESTION_KEYS = frozenset({"type", "difficulty", "bloom_level", "topic_id", "stem", "choices", "answer_key",
                           "explanation", "source_refs"})  # fmt: skip
MAX_STEM = MAX_EXPLANATION = MAX_MODEL_ANSWER = MAX_RUBRIC = 4000
MAX_CHOICE = 500
MAX_SOURCE_REFS = 4
MAX_TITLE = 200
MAX_INSTRUCTIONS = 4000
MAX_DURATION = 600
MAX_BLUEPRINT_ROWS = 50
MAX_BLUEPRINT_TOTAL = 200
MAX_POINTS = 100
CONTENT_HASH_VERSION = 1
_WHITESPACE = re.compile(r"\s+")


def invalid_question(field_name: str, reason: str, message: str | None = None) -> ApiError:
    return ApiError(422, "invalid_question", message or f"The question's {field_name} is not valid ({reason}).",
                    extra={"field": field_name, "reason": reason})  # fmt: skip


def invalid_fields(keys) -> ApiError:
    return ApiError(422, "invalid_fields", f"The request must contain exactly these fields: {', '.join(sorted(keys))}.")


def require_exact_keys(body: dict, keys: frozenset[str]) -> None:
    if set(body) != keys:
        raise invalid_fields(keys)


def normalized_for_comparison(value: str) -> str:
    """NFKC + casefold + collapsed whitespace (uniqueness of choices, duplicate stems)."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


def clean_text(value: object, *, max_chars: int, min_chars: int = 1, single_line: bool = False, nullable: bool = False):
    """The trimmed string, None (nullable and null/empty), or a reason string ('length' | 'control_characters' |
    'type_shape') wrapped in a ValueError."""
    if value is None:
        if nullable:
            return None
        raise ValueError("type_shape")
    if not isinstance(value, str):
        raise ValueError("type_shape")
    if has_forbidden_controls(value) or (single_line and ("\n" in value or "\t" in value or "\r" in value)):
        raise ValueError("control_characters")
    if "\r" in value:
        raise ValueError("control_characters")
    trimmed = value.strip()
    if not trimmed and nullable:
        return None
    if not min_chars <= len(trimmed) <= max_chars:
        raise ValueError("length")
    return trimmed


def _text(field_name: str, value, **kwargs):
    try:
        return clean_text(value, **kwargs)
    except ValueError as exc:
        raise invalid_question(field_name, str(exc)) from None


@dataclass(frozen=True)
class QuestionContent:
    qtype: str
    difficulty: str
    bloom_level: str | None
    topic_id: uuid.UUID | None
    stem: str
    choices: tuple[dict, ...] | None
    answer_key: dict
    explanation: str | None
    source_refs: tuple[tuple[uuid.UUID, int], ...] = field(default_factory=tuple)  # (document_id, page)

    def choices_json(self) -> list | None:
        return None if self.choices is None else [dict(c) for c in self.choices]

    def citations_json(self) -> list | None:
        if not self.source_refs:
            return None
        return [{"document_id": str(d), "locator": {"page": page}} for d, page in self.source_refs]

    def canonical(self) -> dict:
        """The hashed form: every content field, true/false choices already canonical."""
        return {
            "v": CONTENT_HASH_VERSION, "type": self.qtype, "difficulty": self.difficulty, "bloom_level": self.bloom_level,
            "topic_id": None if self.topic_id is None else str(self.topic_id), "stem": self.stem,
            "choices": self.choices_json(), "answer_key": self.answer_key, "explanation": self.explanation,
            "source_refs": [{"document_id": str(d), "locator": {"page": page}} for d, page in self.source_refs],
        }  # fmt: skip

    def content_hash(self) -> str:
        return sha256_hex(self.canonical())


def content_from_row(row) -> QuestionContent:
    """The stored working copy (or a revision) as QuestionContent, for hashing and comparisons."""
    refs = row.citations if hasattr(row, "citations") else row.source_refs
    return QuestionContent(
        qtype=row.qtype, difficulty=row.difficulty, bloom_level=row.bloom_level, topic_id=row.topic_id,
        stem=row.text if hasattr(row, "text") else row.stem,
        choices=None if row.choices is None else tuple(dict(c) for c in row.choices),
        answer_key=dict(row.answer_key), explanation=row.explanation,
        source_refs=tuple((uuid.UUID(r["document_id"]), r["locator"]["page"]) for r in (refs or [])),
    )  # fmt: skip


def _enum(field_name: str, value, values, *, nullable: bool = False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in values:
        raise invalid_question(field_name, "type_shape")
    return value


def parse_question(body: dict) -> QuestionContent:
    require_exact_keys(body, QUESTION_KEYS)
    qtype = _enum("type", body["type"], QUESTION_TYPES)
    difficulty = _enum("difficulty", body["difficulty"], DIFFICULTIES)
    bloom = _enum("bloom_level", body["bloom_level"], BLOOM_LEVELS, nullable=True)
    topic_id = None
    if body["topic_id"] is not None:
        topic_id = canonical_uuid(body["topic_id"])
        if topic_id is None:
            raise invalid_question("topic_id", "topic")
    stem = _text("stem", body["stem"], max_chars=MAX_STEM)
    explanation = _text("explanation", body["explanation"], max_chars=MAX_EXPLANATION, nullable=True)
    choices, answer_key = _parse_shape(qtype, body["choices"], body["answer_key"])
    refs = _parse_source_refs(body["source_refs"])
    return QuestionContent(qtype, difficulty, bloom, topic_id, stem, choices, answer_key, explanation, refs)


def _parse_shape(qtype: str, raw_choices, raw_key) -> tuple[tuple[dict, ...] | None, dict]:
    if qtype == "true_false":
        if raw_choices is not None and raw_choices != [dict(c) for c in TRUE_FALSE_CHOICES]:
            raise invalid_question("choices", "true_false_choices",
                                   'For true/false send choices as null or exactly [{"id":"true","text":"True"},{"id":"false","text":"False"}].')  # fmt: skip
        if not isinstance(raw_key, dict) or set(raw_key) != {"choice_id"} or raw_key["choice_id"] not in ("true", "false"):
            raise invalid_question("answer_key", "answer_key")
        return tuple(dict(c) for c in TRUE_FALSE_CHOICES), {"choice_id": raw_key["choice_id"]}
    if qtype == "short":
        if raw_choices is not None:
            raise invalid_question("choices", "type_shape", "A short-answer question has no choices (send null).")
        if not isinstance(raw_key, dict) or set(raw_key) != {"model_answer", "rubric"}:
            raise invalid_question("answer_key", "answer_key")
        model = _text("answer_key.model_answer", raw_key["model_answer"], max_chars=MAX_MODEL_ANSWER)
        rubric = _text("answer_key.rubric", raw_key["rubric"], max_chars=MAX_RUBRIC, nullable=True)
        return None, {"model_answer": model, "rubric": rubric}
    # mcq
    if not isinstance(raw_choices, list):
        raise invalid_question("choices", "type_shape", "A multiple-choice question needs a list of choices.")
    if not 2 <= len(raw_choices) <= len(CHOICE_LABELS):
        raise invalid_question("choices", "choice_count", "A multiple-choice question has 2 to 6 choices.")
    choices = []
    seen: set[str] = set()
    for index, choice in enumerate(raw_choices):
        if not isinstance(choice, dict) or set(choice) != {"id", "text"}:
            raise invalid_question("choices", "type_shape")
        if choice["id"] != CHOICE_LABELS[index]:
            raise invalid_question("choices", "choice_ids", "Choice ids must be a, b, c ... in order.")
        text_value = _text("choices", choice["text"], max_chars=MAX_CHOICE, single_line=True)
        normalized = normalized_for_comparison(text_value)
        if normalized in seen:
            raise invalid_question("choices", "duplicate_choice", "Two choices have the same text.")
        seen.add(normalized)
        choices.append({"id": choice["id"], "text": text_value})
    ids = [c["id"] for c in choices]
    if not isinstance(raw_key, dict) or set(raw_key) != {"choice_id"} or raw_key["choice_id"] not in ids:
        raise invalid_question("answer_key", "answer_key", "The answer key must name one of the choices.")
    return tuple(choices), {"choice_id": raw_key["choice_id"]}


def _parse_source_refs(raw) -> tuple[tuple[uuid.UUID, int], ...]:
    if not isinstance(raw, list) or len(raw) > MAX_SOURCE_REFS:
        raise invalid_question("source_refs", "source_ref", f"Give at most {MAX_SOURCE_REFS} source references.")
    refs = []
    for ref in raw:
        if not isinstance(ref, dict) or set(ref) != {"document_id", "locator"}:
            raise invalid_question("source_refs", "source_ref")
        document_id = canonical_uuid(ref["document_id"])
        if document_id is None:
            raise invalid_question("source_refs", "source_ref")
        locator = ref["locator"]
        if not isinstance(locator, dict) or set(locator) != {"page"} or type(locator["page"]) is not int \
                or not 1 <= locator["page"] <= 2_147_483_647:  # fmt: skip
            raise invalid_question("source_refs", "locator", 'A locator is exactly {"page": <page number>}.')
        if (document_id, locator["page"]) in refs:
            raise invalid_question("source_refs", "source_ref", "A source reference is listed twice.")
        refs.append((document_id, locator["page"]))
    return tuple(refs)


def locator_label(page: int) -> str:
    return f"p. {page}"


# --- exams ----------------------------------------------------------------------------------------------------


def invalid_exam(field_name: str, message: str) -> ApiError:
    return ApiError(422, "invalid_exam", message, extra={"field": field_name})


def parse_exam_fields(body: dict, *, partial: bool) -> dict:
    keys = frozenset({"title", "instructions", "duration_minutes"})
    if partial:
        if not body or not set(body) <= keys:
            raise ApiError(422, "invalid_fields", "Send a non-empty subset of: duration_minutes, instructions, title.")
    else:
        require_exact_keys(body, keys)
    result = {}
    if "title" in body:
        try:
            result["title"] = clean_text(body["title"], max_chars=MAX_TITLE, single_line=True)
        except ValueError:
            raise invalid_exam("title", f"The title is 1 to {MAX_TITLE} characters on one line.") from None
    if "instructions" in body:
        try:
            result["instructions"] = clean_text(body["instructions"], max_chars=MAX_INSTRUCTIONS, nullable=True)
        except ValueError:
            raise invalid_exam("instructions", f"Instructions are at most {MAX_INSTRUCTIONS} characters.") from None
    if "duration_minutes" in body:
        value = body["duration_minutes"]
        if value is not None and (type(value) is not int or not 1 <= value <= MAX_DURATION):
            raise invalid_exam("duration_minutes", f"The duration is 1 to {MAX_DURATION} minutes, or null.")
        result["duration_minutes"] = value
    return result


def parse_duplicate_title(body: dict) -> str | None:
    require_exact_keys(body, frozenset({"title"}))
    if body["title"] is None:
        return None
    try:
        return clean_text(body["title"], max_chars=MAX_TITLE, single_line=True)
    except ValueError:
        raise invalid_exam("title", f"The title is 1 to {MAX_TITLE} characters on one line.") from None


def copy_title(title: str) -> str:
    suffix = " (copy)"
    return (title[: MAX_TITLE - len(suffix)].rstrip() + suffix) if len(title) + len(suffix) > MAX_TITLE else title + suffix


@dataclass(frozen=True)
class BlueprintRowRequest:
    id: uuid.UUID | None
    topic_id: uuid.UUID | None
    qtype: str | None
    difficulty: str | None
    bloom_level: str | None
    count: int
    points_each: int


def invalid_blueprint(row: int | None, reason: str, message: str) -> ApiError:
    return ApiError(422, "invalid_blueprint", message, extra={"row": row, "reason": reason})


BLUEPRINT_ROW_KEYS = frozenset({"id", "topic_id", "type", "difficulty", "bloom_level", "count", "points_each"})


def parse_blueprint(body: dict) -> list[BlueprintRowRequest]:
    require_exact_keys(body, frozenset({"rows"}))
    rows = body["rows"]
    if not isinstance(rows, list):
        raise invalid_fields({"rows"})
    if len(rows) > MAX_BLUEPRINT_ROWS:
        raise invalid_blueprint(None, "too_many_rows", f"A blueprint has at most {MAX_BLUEPRINT_ROWS} rows.")
    parsed: list[BlueprintRowRequest] = []
    seen: set[uuid.UUID] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != BLUEPRINT_ROW_KEYS:
            raise invalid_fields(BLUEPRINT_ROW_KEYS)
        row_id = None
        if row["id"] is not None:
            row_id = canonical_uuid(row["id"])
            if row_id is None:
                raise invalid_blueprint(index, "unknown_row", "This blueprint row does not exist in this exam.")
            if row_id in seen:
                raise invalid_blueprint(index, "duplicate_row", "A blueprint row is listed twice.")
            seen.add(row_id)
        topic_id = None
        if row["topic_id"] is not None:
            topic_id = canonical_uuid(row["topic_id"])
            if topic_id is None:
                raise invalid_blueprint(index, "topic", "The topic is not a topic of this course.")
        criteria = []
        for name, values in (("type", QUESTION_TYPES), ("difficulty", DIFFICULTIES), ("bloom_level", BLOOM_LEVELS)):
            value = row[name]
            if value is not None and (not isinstance(value, str) or value not in values):
                raise invalid_blueprint(index, "criteria", f"{name} must be one of: {', '.join(values)}, or null.")
            criteria.append(value)
        count, points = row["count"], row["points_each"]
        if type(count) is not int or not 1 <= count <= MAX_BLUEPRINT_TOTAL:
            raise invalid_blueprint(index, "count", f"count is 1 to {MAX_BLUEPRINT_TOTAL}.")
        if type(points) is not int or not 1 <= points <= MAX_POINTS:
            raise invalid_blueprint(index, "points", f"points_each is 1 to {MAX_POINTS}.")
        parsed.append(BlueprintRowRequest(row_id, topic_id, *criteria, count, points))
    if sum(r.count for r in parsed) > MAX_BLUEPRINT_TOTAL:
        raise invalid_blueprint(None, "total_count", f"A blueprint asks for at most {MAX_BLUEPRINT_TOTAL} questions in total.")
    return parsed


def parse_points(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_POINTS:
        raise ApiError(422, "invalid_points", f"Points are a whole number from 1 to {MAX_POINTS}.")
    return value


def parse_reason(value) -> str | None:
    if value is None:
        return None
    try:
        return clean_text(value, max_chars=500, min_chars=10)
    except ValueError:
        raise ApiError(422, "invalid_blueprint_override", "The deviation reason is 10 to 500 characters.") from None


def matches_row(row, *, topic_id, qtype, difficulty, bloom_level) -> bool:
    """Blueprint criteria (NULL = any) against one question's content."""
    return ((row.topic_id is None or row.topic_id == topic_id) and (row.qtype is None or row.qtype == qtype)
            and (row.difficulty is None or row.difficulty == difficulty)
            and (row.bloom_level is None or row.bloom_level == bloom_level))  # fmt: skip
