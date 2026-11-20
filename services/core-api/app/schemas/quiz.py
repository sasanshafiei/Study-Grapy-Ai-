"""Public response schemas of the quiz API (core-api quiz v1, docs/quiz-api.md).

Every response is validated through these models before it is sent, with
extra="forbid": a field that is not listed here (an answer key, grading
metadata, evidence) makes the request fail with a 500 instead of leaking.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Choice(_Strict):
    id: str
    text: str


class CorrectAnswer(_Strict):
    choice_id: str
    text: str


class Citation(_Strict):
    chunk_id: str
    origin: Literal["class"]
    title: str
    locator: str
    url: str | None
    snippet: str


class Baseline(_Strict):
    kind: Literal["baseline"]
    method: Literal["cumulative_accuracy/1"]
    topic_id: str
    topic_name: str
    before: float | None
    after: float
    attempts: int
    correct: int
    trend: Literal["first", "up", "down", "steady"]
    next_review_at: None


class AttemptFeedback(_Strict):
    attempt_id: str
    quiz_id: str
    question_id: str
    answer: str
    correct: bool
    correct_answer: CorrectAnswer
    explanation: str
    citations: list[Citation]
    citations_omitted: int
    hint_count: int
    response_ms: int | None
    submitted_at: str
    mastery: Baseline


class AttemptResponse(AttemptFeedback):
    replayed: bool


class QuizQuestion(_Strict):
    id: str
    position: int
    topic_id: str
    topic_name: str
    type: Literal["mcq", "true_false"]
    difficulty: Literal["easy", "medium", "hard"]
    stem: str
    choices: list[Choice]
    hint: str
    attempt: AttemptFeedback | None


class QuizFailure(_Strict):
    code: str
    detail: str
    retry: Literal["new_idempotency_key"]


class QuizResponse(_Strict):
    quiz_id: str
    course_id: str
    status: Literal["generating", "ready", "failed"]
    scope: Literal["class_only"]
    difficulty: Literal["easy", "medium", "hard", "mixed"]
    count: int
    created_at: str
    completed_at: str | None
    failure: QuizFailure | None
    questions: list[QuizQuestion]
