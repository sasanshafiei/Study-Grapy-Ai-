"""Course document endpoints (public API, /api/v1).

Thin handlers: authorization, body parsing and the service call. The upload
handler deliberately takes the raw Request instead of an UploadFile
parameter: FastAPI would otherwise parse (and spool to disk) the whole
multipart body before any dependency runs, i.e. before the caller is
authenticated. Here the caller and their course access are checked first,
and only then is the body read, under a size limit.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from ..auth import CurrentUser, get_current_user
from ..ingestion import dispatch
from ..schemas.documents import DocumentItem, DocumentUploadResponse, DuplicateDocumentBody, ErrorBody
from ..schemas.openapi import BEARER, validation_error
from ..services import documents
from ..uploads import FILE_FIELD, MAX_FORM_FIELDS, read_upload_form

router = APIRouter(prefix="/api/v1", tags=["documents"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    500: {"model": ErrorBody},
    401: {"model": ErrorBody, "description": "Not signed in", "headers": BEARER},
    403: {"model": ErrorBody, "description": "Account disabled, or not allowed to do this in the course"},
    404: {"model": ErrorBody, "description": "Course not found (or not visible to the caller)"},
}

_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [FILE_FIELD],
                    "maxProperties": 1 + MAX_FORM_FIELDS,
                    "additionalProperties": {"type": "string", "description": "Additional text fields are ignored."},
                    "properties": {
                        FILE_FIELD: {
                            "type": "string",
                            "format": "binary",
                            "description": "One PDF file (25 MiB by default, see MAX_UPLOAD_BYTES).",
                        }
                    },
                }
            }
        },
    }
}


@router.get(
    "/courses/{course_id}/documents",
    response_model=list[DocumentItem],
    responses={**_ERRORS, 503: {"model": ErrorBody, "description": "database_unavailable"}},
    summary="List a course's materials",
)
def list_course_documents(
    course_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)
) -> list[DocumentItem]:
    return documents.list_documents(request.app.state.session_factory, user, course_id)


@router.post(
    "/courses/{course_id}/documents",
    status_code=202,
    response_model=DocumentUploadResponse,
    responses={
        **_ERRORS,
        400: {"model": ErrorBody, "description": "Malformed or interrupted multipart body"},
        409: {"model": DuplicateDocumentBody, "description": "The same file is already in this course"},
        413: {"model": ErrorBody, "description": "File larger than the limit"},
        415: {"model": ErrorBody, "description": "Not a PDF, or not multipart/form-data"},
        422: validation_error(),
        503: {"model": ErrorBody, "description": "Outcome could not be confirmed, or uploads are unavailable"},
    },
    openapi_extra=_UPLOAD_BODY,
    summary="Upload a PDF to a course",
    description=(
        "Stores the file, records the document and a queued parse job, and returns the document "
        "with status `queued`. The job is then processed asynchronously: the document shows as "
        "`processing` while it is parsed and until it is indexed, then `ready`, or `failed` with an `error`."
    ),
)
async def upload_course_document(
    course_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> DocumentUploadResponse:
    state = request.app.state
    # 401/403/404 (then 503 if the upload store is unavailable) are decided
    # here, before a single byte of the body is read.
    await run_in_threadpool(documents.authorize_upload, state.session_factory, user, course_id, state.storage)
    max_bytes = state.settings.max_upload_bytes
    async with read_upload_form(request, max_file_bytes=max_bytes) as upload:
        incoming = documents.IncomingFile(upload.filename, upload.content_type, upload.file)
        response = await run_in_threadpool(
            documents.ingest_upload,
            state.session_factory,
            state.storage,
            user,
            course_id,
            incoming,
            max_bytes=max_bytes,
        )
    # Reached only when the commit is confirmed. Runs after the response is
    # sent; bounded, and the dispatch sweep covers any failure.
    if state.publisher is not None:
        background_tasks.add_task(
            dispatch.publish_new_job, state.session_factory, state.publisher, response.job_id, state.settings
        )
    return response


@router.delete(
    "/documents/{document_id}",
    status_code=204,
    response_class=Response,
    responses={
        500: {"model": ErrorBody},
        401: {"model": ErrorBody, "description": "Not signed in", "headers": BEARER},
        403: {"model": ErrorBody, "description": "Account disabled, or not the professor who owns the course"},
        404: {"model": ErrorBody, "description": "Document not found (or its course is not visible to the caller)"},
        409: {
            "model": ErrorBody,
            "description": "document_not_withdrawable: harvested or other external material (it follows its source)",
        },
        422: validation_error(),
        503: {"model": ErrorBody, "description": "database_unavailable; repeating the request is safe"},
    },
    summary="Withdraw a class upload from its course",
    description=(
        "Only the professor who owns the course. The document disappears from the course's materials, "
        "is never used again for retrieval, chat or quiz citations, and its queued or running processing "
        "jobs are cancelled. Withdrawal is final and idempotent: repeating the request answers 204 again. "
        "The same file can be uploaded again afterwards (as a new document). The stored file and derived "
        "index data are retained for a later purge; nothing is returned."
    ),
)
def withdraw_document(
    document_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user)
) -> Response:
    documents.withdraw_document(request.app.state.session_factory, user, document_id)
    return Response(status_code=204)
