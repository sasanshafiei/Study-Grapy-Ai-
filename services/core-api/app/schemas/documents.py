"""Public document shapes and the mapping from internal values.

The shape is the web client's DocumentItem (apps/web/src/lib/api/types.ts on
the CE-2 branch), in snake_case. Internal storage details (storage key, file
path, hash, size, MIME type, job errors) are never part of it.

Mappings (provisional, see docs/document-ingestion.md and docs/document-processing.md):

  documents.status   condition                              public status
  pending            a job of the document runs             processing
  pending            parsed (succeeded parse job), waiting  processing
                     for indexing (M4)
  pending            otherwise                              queued
  indexed                                                   ready
  failed                                                    failed
  stale                                                     failed, with an explanation

  Parsing alone never makes a document ready: only indexing (M4) sets indexed.

  documents.origin: class -> class, external -> external, web_cache -> web
  documents.kind:   stored value; when NULL (legacy and external rows) a
                    display default: class -> notes, otherwise article
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

PublicOrigin = Literal["class", "external", "web"]
PublicKind = Literal["slides", "notes", "syllabus", "textbook", "article"]
PublicStatus = Literal["queued", "processing", "ready", "failed"]

STALE_MESSAGE = "The original source is no longer available."

_ORIGINS: dict[str, PublicOrigin] = {"class": "class", "external": "external", "web_cache": "web"}
_STATUSES: dict[str, PublicStatus] = {"indexed": "ready", "failed": "failed", "stale": "failed"}


class DocumentItem(BaseModel):
    id: uuid.UUID
    title: str
    origin: PublicOrigin
    kind: PublicKind
    status: PublicStatus
    pages: int | None
    source_name: str | None
    url: str | None
    licence: str | None
    added_by: str | None
    added_at: datetime = Field(description="UTC, ISO 8601")
    error: str | None


class DocumentUploadResponse(DocumentItem):
    job_id: uuid.UUID = Field(description="The parse job recorded for this upload (queued for processing).")


class ErrorBody(BaseModel):
    detail: str
    code: str


class DuplicateDocumentBody(ErrorBody):
    document_id: uuid.UUID | None = None


def public_origin(origin: str) -> PublicOrigin:
    return _ORIGINS[origin]  # values are constrained by documents_origin_check


def public_status(status: str, *, job_running: bool, parsed: bool = False) -> PublicStatus:
    if status == "pending":
        return "processing" if job_running or parsed else "queued"
    return _STATUSES[status]  # values are constrained by documents_status_check


def public_kind(kind: str | None, origin: str) -> PublicKind:
    if kind is not None:
        return kind  # type: ignore[return-value]  # constrained by documents_kind_check
    return "notes" if origin == "class" else "article"


def public_error(status: str, error_message: str | None) -> str | None:
    if status == "stale" and not error_message:
        return STALE_MESSAGE
    return error_message


def as_utc(value: datetime) -> datetime:
    """documents.created_at is a naive timestamp holding UTC (baseline schema)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
