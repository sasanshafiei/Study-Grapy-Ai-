"""Public response schemas of the read-only admin endpoints (WP-4; docs/admin-api.md).

Every response is validated through these models with extra="forbid" before it is sent: a
field not listed here (an operational message, document text, a credential) makes the
request fail with the generic 500 instead of leaking.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HarvestStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminFeatures(_Strict):
    dispatch_on_upload: bool
    indexing_enabled: bool
    chat_enabled: bool
    harvest_enabled: bool
    quiz_enabled: bool
    dashboard_enabled: bool
    analytics_enabled: bool
    study_events_enabled: bool
    mastery_delivery_enabled: bool
    exams_enabled: bool
    exam_export_enabled: bool


class AdminSourceTotals(_Strict):
    total: int = Field(ge=0)
    enabled: int = Field(ge=0)


class AdminDocumentCounts(_Strict):
    pending: int = Field(ge=0)
    indexed: int = Field(ge=0)
    failed: int = Field(ge=0)
    stale: int = Field(ge=0)


class AdminIngestionCounts(_Strict):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)


class AdminHarvestCounts(AdminIngestionCounts):
    cancelled: int = Field(ge=0)


class AdminJobCounts(_Strict):
    parse: AdminIngestionCounts
    index: AdminIngestionCounts
    harvest: AdminHarvestCounts


class AdminOverview(_Strict):
    as_of: str
    features: AdminFeatures
    sources: AdminSourceTotals
    documents: AdminDocumentCounts
    jobs: AdminJobCounts


class AdminLastRun(_Strict):
    id: str
    status: HarvestStatus
    created_at: str
    finished_at: str | None
    error_code: str | None


class AdminSource(_Strict):
    id: str
    kind: str | None
    provider: str | None
    language: str | None
    domain: str | None
    licence: str | None
    licence_url: str | None
    enabled: bool
    created_at: str | None
    courses: int = Field(ge=0, description="Courses with a harvest target for this source.")
    documents: AdminDocumentCounts
    last_run: AdminLastRun | None


class AdminSourceList(_Strict):
    items: list[AdminSource]
    next_cursor: str | None


class AdminAttempts(_Strict):
    used: int = Field(ge=0)
    limit: int = Field(ge=0, description="max_attempts for parse/index jobs; max_claims (slices) for harvest runs.")


class AdminSubject(_Strict):
    course_id: str | None
    document_id: str | None
    source_id: str | None
    trigger: Literal["dirty", "refresh", "manual"] | None


class AdminJobError(_Strict):
    code: str
    message: str | None = Field(description="Reviewed public text for document jobs; null for harvest runs.")


class AdminJob(_Strict):
    id: str
    kind: Literal["parse", "index", "harvest"]
    status: HarvestStatus
    created_at: str
    started_at: str | None
    finished_at: str | None
    attempts: AdminAttempts
    subject: AdminSubject
    error: AdminJobError | None


class AdminJobList(_Strict):
    items: list[AdminJob]
    next_cursor: str | None
