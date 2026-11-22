"""Response schemas of the professor exam API (core-api exams v1, docs/exam-api.md).

Every response is validated through these models before it is sent, with extra="forbid": a field that is
not listed here can never appear (a violation is a 500, never a leak). Readiness issues and warnings, and
preview questions, carry only the keys that apply to them (None-valued optional keys are dropped), exactly
as in the contract examples.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, WithJsonSchema, model_serializer

from ..exams.validation import CHOICE_LABELS, MAX_MODEL_ANSWER, MAX_RUBRIC
from .openapi import enum, exact, nullable, text_field

MCQ_ID = enum(*CHOICE_LABELS)
MCQ_KEY = exact(choice_id=MCQ_ID)
TF_KEY = exact(choice_id=enum("true", "false"))
SHORT_KEY = exact(model_answer=text_field(MAX_MODEL_ANSWER), rubric=nullable(text_field(MAX_RUBRIC, 0)))
ANSWER_KEY = {"oneOf": [MCQ_KEY, TF_KEY, SHORT_KEY]}

AnswerKey = Annotated[dict[str, Any], WithJsonSchema(ANSWER_KEY)]

Status = Literal["draft", "approved", "retired"]
QType = Literal["mcq", "true_false", "short"]
Difficulty = Literal["easy", "medium", "hard"]
Bloom = Literal["remember", "understand", "apply", "analyze", "evaluate", "create"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Sparse(_Strict):
    """Optional keys that are None are left out of the JSON."""

    @model_serializer(mode="wrap")
    def _drop_none(self, handler):
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None or k in self._keep_null()}

    @classmethod
    def _keep_null(cls) -> frozenset[str]:
        return frozenset()


class Topic(_Strict):
    id: str
    name: str


class Choice(_Strict):
    id: str
    text: str


class Locator(_Strict):
    page: int


class SourceRef(_Strict):
    document_id: str
    title: str | None
    locator: Locator
    locator_label: str
    available: bool


class UsedIn(_Strict):
    draft_exams: int
    finalized_exams: int


class ExamQuestion(_Strict):
    id: str
    course_id: str
    status: Status
    version: int
    type: QType
    difficulty: Difficulty
    bloom_level: Bloom | None
    topic: Topic | None
    stem: str
    choices: list[Choice] | None
    answer_key: AnswerKey
    explanation: str | None
    source_refs: list[SourceRef]
    approved_revision: int | None
    content_sha256: str
    used_in: UsedIn
    created_at: str
    updated_at: str


class ExamQuestionReplay(ExamQuestion):
    replayed_revision: int


class ReplaceResponse(_Strict):
    question: ExamQuestion
    approval_revoked: bool


class ExamQuestionSummary(_Strict):
    id: str
    status: Status
    version: int
    type: QType
    difficulty: Difficulty
    bloom_level: Bloom | None
    topic: Topic | None
    stem_preview: str
    approved_revision: int | None
    updated_at: str


class QuestionList(_Strict):
    items: list[ExamQuestionSummary]
    next_cursor: str | None


class RevisionSource(_Strict):
    document_id: str
    title: str
    locator_label: str


class QuestionRevision(_Strict):
    revision: int
    approved_at: str
    approved_by_name: str
    content_sha256: str
    type: QType
    difficulty: Difficulty
    bloom_level: Bloom | None
    topic: Topic | None
    stem: str
    choices: list[Choice] | None
    answer_key: AnswerKey
    explanation: str | None
    source_refs: list[RevisionSource]


class RevisionList(_Strict):
    items: list[QuestionRevision]
    next_cursor: str | None


class AuditEvent(_Strict):
    id: str
    action: str
    entity_kind: str
    entity_id: str
    entity_version: int | None
    actor_name: str
    created_at: str
    details: dict[str, Any]


class AuditList(_Strict):
    items: list[AuditEvent]
    next_cursor: str | None


# --- exams ------------------------------------------------------------------------------------------------------


class CourseSnapshot(_Strict):
    code: str
    name: str


class BlueprintRow(_Strict):
    id: str
    position: int
    topic: Topic | None
    type: QType | None
    difficulty: Difficulty | None
    bloom_level: Bloom | None
    count: int
    points_each: int
    assigned: int


class Blueprint(_Strict):
    rows: list[BlueprintRow]
    total_count: int
    total_points: int


class ItemQuestion(_Strict):
    type: QType
    difficulty: Difficulty
    bloom_level: Bloom | None
    topic: Topic | None
    stem: str
    choices: list[Choice] | None
    answer_key: AnswerKey
    explanation: str | None
    source_refs: list[SourceRef]


class ExamItem(_Strict):
    id: str
    position: int
    points: int
    blueprint_row_id: str | None
    question_id: str
    question_status: Status
    question_version: int
    pinned_revision: int | None
    question: ItemQuestion


IssueCode = Literal[
    "no_items", "question_not_approved", "question_retired", "too_many_items", "export_budget_exceeded",
    "blueprint_row_underfilled", "blueprint_row_overfilled", "item_not_in_blueprint", "blueprint_criteria_mismatch",
]  # fmt: skip
WarningCode = Literal[
    "points_differ_from_blueprint", "duplicate_stem", "unsupported_characters", "missing_duration",
    "item_changed_since_source",
]  # fmt: skip


class Issue(_Sparse):
    code: IssueCode
    overridable: bool
    item_id: str | None = None
    question_id: str | None = None
    blueprint_row_id: str | None = None
    assigned: int | None = None
    count: int | None = None
    content_chars: int | None = None
    export_budget: int | None = None


class Warning_(_Sparse):
    code: WarningCode
    item_id: str | None = None
    item_ids: list[str] | None = None
    points: int | None = None
    points_each: int | None = None


class ReadinessBlueprint(_Strict):
    declared: bool
    fulfilled: bool


class Readiness(_Strict):
    ready: bool
    issues: list[Issue]
    warnings: list[Warning_]
    blueprint: ReadinessBlueprint
    content_chars: int
    export_budget: int
    preview_fingerprint: str


class Exam(_Strict):
    id: str
    course_id: str
    status: Literal["draft", "finalized", "archived"]
    version: int
    title: str
    instructions: str | None
    duration_minutes: int | None
    derived_from_exam_id: str | None
    created_at: str
    updated_at: str
    finalized_at: str | None
    archived_at: str | None
    course_snapshot: CourseSnapshot | None
    content_sha256: str | None
    finalized_version: int | None
    blueprint_status: Literal["none", "fulfilled", "deviation_accepted"] | None
    blueprint_deviation_reason: str | None
    finalize_preview_fingerprint: str | None
    content_chars: int
    export_budget: int
    item_count: int
    total_points: int
    blueprint: Blueprint
    items: list[ExamItem]
    readiness: Readiness | None


class ExamSummary(_Strict):
    id: str
    status: Literal["draft", "finalized", "archived"]
    version: int
    title: str
    item_count: int
    total_points: int
    finalized_at: str | None
    archived_at: str | None
    derived_from_exam_id: str | None
    updated_at: str


class ExamList(_Strict):
    items: list[ExamSummary]
    next_cursor: str | None


class BlueprintChange(_Strict):
    kept: int
    inserted: int
    removed: int
    unassigned_item_ids: list[str]


class BlueprintResponse(_Strict):
    exam: Exam
    blueprint_change: BlueprintChange


class Filled(_Sparse):
    blueprint_row_id: str
    added: int
    missing: int
    stopped_by: Literal["export_budget", "item_limit"] | None = None


class FillResponse(_Strict):
    exam: Exam
    filled: list[Filled]


class DuplicateItem(_Strict):
    item_id: str
    question_id: str
    source_revision: int | None
    current_status: Status
    current_approved_revision: int | None
    content: Literal["unchanged", "changed", "not_approved", "retired"]


class DuplicateResponse(_Strict):
    exam: Exam
    duplicate_report: list[DuplicateItem]


# --- preview --------------------------------------------------------------------------------------------------


class PreviewHeader(_Strict):
    course_code: str
    course_name: str
    title: str
    instructions: str | None
    duration_minutes: int | None
    question_count: int
    total_points: int


class PreviewOption(_Strict):
    label: str | None
    text: str


class PreviewQuestion(_Sparse):
    number: int
    points: int
    type: QType
    stem: str
    options: list[PreviewOption] | None
    answer_lines: int | None = None

    @classmethod
    def _keep_null(cls) -> frozenset[str]:
        return frozenset({"options"})


class PreviewAnswer(_Strict):
    number: int
    points: int
    answer: str | None
    answer_text: str | None
    model_answer: str | None
    rubric: str | None
    explanation: str | None
    sources: list[str]


class PreviewReadiness(_Strict):
    ready: bool
    issues: list[Issue]
    warnings: list[Warning_]


class Preview(_Strict):
    # Both variants share this runtime model. The print path chooses exactly one answer-key shape.
    model_config = ConfigDict(json_schema_extra={"oneOf": [
        {"properties": {"variant": {"const": "student"}, "answer_key": {"type": "null"}}},
        {"properties": {"variant": {"const": "answer_key"}, "answer_key": {"type": "array", "items": {}}}},
    ]})

    exam_id: str
    status: Literal["draft", "finalized", "archived"]
    version: int
    variant: Literal["student", "answer_key"]
    header: PreviewHeader
    questions: list[PreviewQuestion]
    answer_key: list[PreviewAnswer] | None
    readiness: PreviewReadiness | None
    preview_fingerprint: str | None
