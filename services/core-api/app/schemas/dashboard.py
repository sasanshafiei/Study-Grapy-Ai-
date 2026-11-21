"""Public response schemas of the dashboard, analytics and study-event endpoints (M8).

Every response is validated through these models before it is sent, with extra="forbid":
a field not listed here (a student id in analytics, an answer, a key) makes the request
fail with a generic 500 instead of leaking. Analytics deliberately has no field that could
carry a student id, an exact count or a per-student value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- student dashboard ---------------------------------------------------------------------------


class Window(_Strict):
    tz: str
    days: int
    start: str
    end: str


class Totals(_Strict):
    attempts: int
    correct: int
    accuracy: float | None


class Recent(_Strict):
    window: int
    correct: int
    accuracy: float | None


class Baseline(_Strict):
    kind: Literal["baseline"]
    method: Literal["cumulative_accuracy/1"]
    value: float | None


class ModelIdentity(_Strict):
    name: str
    version: str


class Prediction(_Strict):
    kind: Literal["model"]
    mastery: float = Field(ge=0, le=1)
    model: ModelIdentity
    through_seq: int
    pending_events: int
    stale: bool
    received_at: str
    next_review_at: str | None


class TopicView(_Strict):
    topic_id: str
    name: str
    practiced: bool
    attempts: int
    correct: int
    accuracy: float | None
    last_attempt_at: str | None
    recent: Recent | None
    baseline: Baseline | None
    prediction: Prediction | None
    has_class_material: bool


class Daily(_Strict):
    date: str
    attempts: int
    correct: int
    accuracy: float | None


class Activity(_Strict):
    attempt_id: str
    quiz_id: str
    topic_id: str
    topic_name: str
    correct: bool
    submitted_at: str


class Streak(_Strict):
    days: int
    through: str | None
    capped: bool


class StudyMinutes(_Strict):
    date: str
    minutes: int | None


class StudyTime(_Strict):
    measurement: Literal["client_reported_estimate"]
    available: bool
    minutes_7d: int | None
    minutes_30d: int | None
    by_day: list[StudyMinutes] | None


class ActivitySummary(_Strict):
    active_days_7: int
    active_days_30: int
    study_time: StudyTime


class Review(_Strict):
    topic_id: str
    name: str
    next_review_at: str


class Evidence(_Strict):
    window: int
    correct: int
    attempt_ids: list[str]


class Reason(_Strict):
    code: Literal["recent_errors", "not_practised"]
    text: str
    evidence: Evidence


class Target(_Strict):
    kind: Literal["topic", "quiz", "document"]
    topic_id: str | None = None
    topic_ids: list[str] | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    document_id: str | None = None
    title: str | None = None
    origin: Literal["class", "external"] | None = None
    url: str | None = None
    licence: str | None = None
    provider: str | None = None


class Recommendation(_Strict):
    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    type: Literal["review_topic", "practice_quiz", "class_material", "external_resource"]
    topic_id: str
    topic_name: str
    basis: Literal["rule_based/1"]
    evidence_strength: Literal["none", "limited", "moderate"]
    reason: Reason
    target: Target


class Recommendations(_Strict):
    basis: Literal["rule_based/1"]
    items: list[Recommendation] = Field(max_length=6)
    empty_reason: Literal["no_material", "no_rule_matched"] | None


class DashboardResponse(_Strict):
    as_of: str
    course_id: str
    window: Window
    totals: Totals
    topics: list[TopicView] = Field(max_length=200)
    topics_truncated: bool
    daily: list[Daily] = Field(min_length=30, max_length=30)
    recent_activity: list[Activity] = Field(max_length=10)
    streak: Streak
    activity: ActivitySummary
    review_schedule: list[Review] = Field(max_length=200)
    recommendations: Recommendations


# --- professor analytics ---------------------------------------------------------------------------


class Cell(_Strict):
    status: Literal["released", "withheld"]
    accuracy: float | None = Field(default=None, ge=0, le=1)
    contributors: Literal["5-9", "10-19", "20-49", "50-99", "100+"] | None = None
    attempts: Literal["5-24", "25-99", "100-499", "500+"] | None = None


class TopicCell(_Strict):
    topic_id: str
    name: str | None
    cell: Cell


class Week(_Strict):
    week_start: str
    week_end: str
    status: Literal["released", "pending_release"]
    released_at: str | None
    min_group_size: int | None
    overall: Cell | None
    topics: list[TopicCell] | None
    topics_truncated: bool | None


class Policy(_Strict):
    version: Literal["analytics_release/1"]
    min_group_size: int = Field(ge=5)
    week: str
    publication_delay_hours: float
    accuracy: str
    population: str
    contributor_bands: list[str]
    attempt_bands: list[str]


class ItemAnalysis(_Strict):
    available: Literal[False]
    reason: Literal["no_shared_items"]
    prerequisite: str


class AnalyticsResponse(_Strict):
    as_of: str
    course_id: str
    policy: Policy
    weeks: list[Week] = Field(max_length=26)
    item_analysis: ItemAnalysis
    notice: str


# --- study events -------------------------------------------------------------------------------------


class EventResult(_Strict):
    client_event_id: str
    status: Literal["accepted", "duplicate"]


class StudyEventsResponse(_Strict):
    accepted: int
    duplicates: int
    results: list[EventResult]
    measurement: Literal["client_reported"]
