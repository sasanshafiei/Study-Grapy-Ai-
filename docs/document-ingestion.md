# Document upload and ingestion (M2)

Owner: CE-1. Code under `services/core-api/`: `app/routers/documents.py`,
`app/services/documents.py`, `app/services/course_access.py`, `app/uploads.py`,
`app/storage.py`, `app/auth.py`; schema in migration `0002`.

## What M2 does, and what it does not do yet

An authorized professor can upload a PDF to their course. The API validates it,
stores the original file, computes its SHA-256, and records the document together
with a **queued `parse` job** in one database transaction. Authorized users can
list a course's documents.

**Processing (M3)** is described in [document-processing.md](document-processing.md):
after the commit the job is published to Redis, a Celery worker parses the PDF into
page-aware sections, and the document waits for indexing (M4) as `processing`. Nothing
is indexed or available to retrieval yet. Still not implemented: deleting, downloading
or renaming documents, formats other than PDF, OCR, malware scanning,
external/harvested documents. M3 also adds one upload rule: the upload store must be
the one the database is bound to, otherwise `503 storage_unavailable`.

## Endpoints

Base path `/api/v1`, JSON with snake_case keys, `Authorization: Bearer <token>`.

### `POST /api/v1/courses/{course_id}/documents`

`multipart/form-data` with exactly one file part named **`file`**. No other field
is required (up to four extra text fields are tolerated and ignored).

```bash
curl -X POST http://localhost:8000/api/v1/courses/$COURSE/documents \
  -H "Authorization: Bearer $TOKEN" -F "file=@Lecture 01.pdf;type=application/pdf"
```

`202 Accepted`:

```json
{
  "id": "6f1c2a9e-1d2b-4c57-9a43-0f8b3c1d2e4f",
  "title": "Lecture 01.pdf",
  "origin": "class",
  "kind": "notes",
  "status": "queued",
  "pages": null,
  "source_name": null,
  "url": null,
  "licence": null,
  "added_by": "Dr. Example",
  "added_at": "2026-11-19T12:00:00.123456Z",
  "error": null,
  "job_id": "0b7d4e2a-5c1f-4b8e-9d3a-2e6f7a8b9c0d"
}
```

This is the web client's `DocumentItem` plus one additive field, `job_id` (the
recorded parse job). Clients that do not know `job_id` ignore it.

### `GET /api/v1/courses/{course_id}/documents`

`200 OK` with a JSON array of the same objects (without `job_id`), newest first.
Only documents whose `course_id` is this course are returned; documents without a
course (for example future global external pages) never are.

### Errors

Body: `{"detail": "<message for the user>", "code": "<stable code>"}`. The web
client shows `detail` as-is. File paths, storage keys, SQL and exception text are
never included (they go to the server log).

| Situation | Status | `code` |
| --- | --- | --- |
| No or invalid token (and, until CE-3 lands, every request) | 401 + `WWW-Authenticate: Bearer` | `not_authenticated` |
| Account disabled | 403 | `account_disabled` |
| Course does not exist **or** caller has no relationship with it | 404 | `course_not_found` |
| Caller can see the course but may not upload (enrolled student) | 403 | `upload_not_allowed` |
| Body is not `multipart/form-data` | 415 | `unsupported_media_type` |
| Invalid `Content-Length` header (not 1*DIGIT) | 400 | `invalid_content_length` |
| Malformed multipart, a second file, too many fields | 400 | `invalid_multipart` |
| Multipart message ends before its closing boundary | 400 | `upload_incomplete` |
| Client disconnected mid-upload | 400 | `upload_interrupted` |
| No `file` part | 422 | `file_required` |
| File name empty after normalization | 422 | `invalid_filename` |
| Not `.pdf`, or declared type not a PDF type | 415 | `unsupported_file_type` |
| Empty file | 422 | `empty_file` |
| Content is not a PDF (signature) or is truncated | 422 | `invalid_pdf` |
| Larger than the limit (body or file) | 413 | `file_too_large` |
| Same content already uploaded to this course | 409 | `duplicate_document` (+ `document_id`) |
| File could not be stored | 500 | `storage_error` |
| Database failure, rolled back | 500 | `upload_failed` |
| Outcome could not be confirmed (see consistency) | 503 | `upload_unconfirmed` |
| Upload store not the one the database is bound to (M3; after the access checks, before the body is read) | 503 | `storage_unavailable` |
| Database unreachable while checking access or listing | 503 | `database_unavailable` |
| Any other unexpected server error | 500 | `server_error` |

A malformed course id in the path gets FastAPI's standard 422.

## Public values (provisional mappings, extended in M3)

| `documents.status` | Condition | Public `status` |
| --- | --- | --- |
| `pending` | the document has a `running` job | `processing` |
| `pending` | a parse job succeeded: parsed, waiting for indexing (M3) | `processing` |
| `pending` | otherwise | `queued` |
| `indexed` | | `ready` |
| `failed` | | `failed`, `error` = `documents.error_message` |
| `stale` | | `failed`, `error` = the stored message or "The original source is no longer available." |

`stale` (a harvested page that disappeared upstream) is deliberately **not**
`ready`. Whether the web app should get its own `stale` value is a CE-2 decision.

- `origin`: `class` → `class`, `external` → `external`, `web_cache` → `web`.
- `kind`: uploads are stored as `notes` (a default; the type of a document is not
  inferred from its file). Legacy and external rows have no kind (NULL); the API
  shows `notes` for class documents and `article` otherwise, without writing it.
- `added_by`: the uploader's `display_name`; `null` when there is no uploader.
- `added_at`: `documents.created_at` in UTC (see timestamps).
- `pages`: `documents.page_count` (filled by parsing, M3). `source_name`: always
  `null` so far.

## Authentication and authorization

`app/auth.py` defines the seam CE-3 implements:

```python
@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: uuid.UUID
    role: str    # student | professor | admin
    status: str  # active | disabled

def get_current_user(credentials = Depends(HTTPBearer(auto_error=False))) -> CurrentUser: ...
```

Until CE-3 replaces its body it **fails closed**: every protected request gets
401. There is no development token, hard-coded user or bypass header. Tests
inject principals with `dependency_overrides` on their own application instance.

Course-level rules (`app/services/course_access.py`), decided from the `users`,
`courses` and `enrollments` rows:

| Caller | Upload | List |
| --- | --- | --- |
| Active professor who owns the course | 202 | 200 |
| Another professor | 404 | 404 |
| Active student with a `student` enrollment | 403 | 200 |
| Student without an enrollment | 404 | 404 |
| Admin (with or without an enrollment) | 404 | 404 |
| Any other role, or an enrollment with another role | 404 | 404 |
| Disabled account (principal or `users` row) | 403 | 403 |
| Not authenticated | 401 | 401 |
| Unknown course | 404 | 404 |

A course that does not exist and one the caller has no relationship with give the
same 404, so course ids cannot be probed. Admins get no implicit access; a
read-only admin exception needs team approval.

The upload is authorized **before its body is read** (unauthorized requests never
reach parsing or storage), and authorized again inside the write transaction with
`FOR SHARE` locks on the user and course rows, so access lost while the file was
uploading (account disabled, ownership changed) is refused.

## Upload limits and validation

- **PDF only.** Extension `.pdf` (any case); declared part type `application/pdf`,
  `application/x-pdf`, `application/octet-stream` or none. The content decides:
  it must start with `%PDF-1.x`/`%PDF-2.x` and contain `%%EOF` in its last 1024
  bytes. A damaged or encrypted PDF can pass these checks; M3's parser marks it
  failed.
- **Size.** `MAX_UPLOAD_BYTES`, default 26 214 400 (25 MiB, the limit the web
  client already states). The request body may be at most that plus 64 KiB of
  multipart framing:
  - `Content-Length` must be ASCII digits (leading zeros allowed), otherwise 400.
    A value above the limit is refused with 413 before any byte is read; it is
    compared by its number of significant digits first, so even a value with
    thousands of digits is never converted to an integer;
  - bodies without a length (chunked) or with a wrong one are counted as they
    arrive and stopped at the limit with 413;
  - the file itself is counted again while it is copied.
- **Complete message.** The multipart body must reach its closing boundary
  (`--boundary--`). A body cut off after the file part (a next part announced but
  not sent, a half-sent field, a closing boundary without its hyphens) is refused
  with 400 `upload_incomplete`, even though the file part itself was complete.
  python-multipart reports the closing boundary through its `end` callback, which
  a two-line subclass of Starlette's parser records; no custom parser is involved.
  Starlette spools the file part to a temporary file (in memory up to 1 MiB, then
  on disk); every spooled file is closed on every path, including errors and
  client disconnects.
- **What this does not cover:** the limit protects this process's memory, temporary
  disk and storage. The connection, slow clients and request rates must be limited
  at the reverse proxy in production (CE-3), e.g. nginx `client_max_body_size 26m`
  plus timeouts and rate limits.
- **File name.** Only the last path component is kept; Unicode is NFC-normalized;
  control, format (including bidirectional overrides), surrogate, private-use and
  unassigned characters are removed; whitespace is collapsed; names longer than 255
  characters are shortened, keeping the extension. The result is stored as `title`
  and `original_filename` and is never used in a path.

## File storage

| Setting | Default | Notes |
| --- | --- | --- |
| `UPLOAD_DIR` | `/data/uploads` | docker-compose mounts the named volume `uploads` here (core-api and the M3 workers) |
| `MAX_UPLOAD_BYTES` | `26214400` | see above |

Layout: `<UPLOAD_DIR>/<course_id>/<document_id>.pdf`; files being received live
in `<UPLOAD_DIR>/.incoming/`. The storage key stored in `documents.storage_key` is
`<course_id>/<document_id>.pdf`; it is generated by the server, validated against
a strict two-UUID pattern before use, and never returned by the API.

- Directories are created `0750` and must be real directories (a symlink is refused).
- Temporary files are created with `O_EXCL` and moved into place with `os.link`,
  which fails instead of overwriting; files are `0640`.
- Storage keys are checked with a full-string match (a trailing newline or space
  is refused).
- A temporary file that cannot be removed is logged (`upload_reconcile ...`, with
  its path, in the server log) and left for reconciliation.
- The directory is not served over HTTP.
- Nothing is created until the first upload, so building the app or running the
  tests touches no real directory.

## Consistency between the file and the database

The file and the database rows cannot share a transaction. The sequence
(`app/services/documents.py`):

1. Validate name and type, stream the body into `.incoming` (size, SHA-256, PDF
   checks).
2. One transaction: record the connection's backend id, re-check authorization
   with `FOR SHARE`, look for a duplicate, insert the document (`pending`) and its
   `parse` job (`queued`).
3. Place the file, syncing everything a committed row will rely on: `fsync` the
   file's data; make sure the course directory exists and `fsync` the storage root
   (every time, because a concurrent upload may have just created the directory
   without its entry being durable yet); `os.link` to the final name; `fsync` the
   course directory. Any failure here, before or after the link, rolls the
   transaction back and removes only what this upload created.
4. `COMMIT`. The response was built before step 3, so nothing after a successful
   commit touches the database.

The upload owns its temporary name from the start and its final name from the
moment `os.link` succeeds until the commit is confirmed; cleanup only ever touches
those two names (never another upload's file: keys contain a fresh UUID and
placement never overwrites).

| What happened | Database | File | Response |
| --- | --- | --- | --- |
| Validation fails | nothing | temporary file removed | 4xx |
| Insert fails (incl. duplicate) | rolled back | temporary file removed | 409 / 500 |
| Placement fails, even after the link was made | rolled back | final and temporary file removed | 500 |
| PostgreSQL rejects the COMMIT (e.g. a deferred constraint) | rolled back | removed | 500 |
| COMMIT raised and a fresh connection finds the document | committed | **kept** | 202 |
| COMMIT raised, the original backend is gone or idle, no document | rolled back | removed | 500 |
| COMMIT raised and the original backend still has a transaction open (or the check fails) | unknown | **kept**, `upload_reconcile` logged at ERROR with document id and storage key | 503 |

A COMMIT that raises is never assumed to have rolled back: the outcome is checked
on a fresh connection, first that the original backend (pid and start time) has
ended its transaction, then whether the document row exists. A file is only
deleted when the rollback is certain.

**Durability assumptions.** The fsync sequence assumes POSIX semantics on a local
Linux filesystem (the Docker volume) and a storage stack that honours `fsync`; on
Docker Desktop that includes the virtual machine's disk-cache settings. The
storage root is normally the volume's mount point, which Docker creates; if the
application has to create it, the parent of every level it creates is synced too.
`.incoming` is not synced (a temporary name that reappears after a crash is
garbage, never referenced). The tests check which calls are made, in which order,
and that every failure prevents the commit; they cannot prove that data survives
a real power loss.

**Remaining windows (process crash, power loss).** A crash while receiving leaves a
`.part` file in `.incoming`; a crash between placement and COMMIT leaves a stored
file without a document row. Neither leaves a document without its file. M3's
reconciliation (see [document-processing.md](document-processing.md)) removes
temporary files only when they are provably abandoned (an ownership lock protocol, not
age alone), and only reports stored files without a row and documents whose file is
missing; a parse job whose file is missing from a verified store fails.

## Deduplication

Unique index `documents_course_id_content_hash_idx` on `(course_id, content_hash)`
`WHERE origin = 'class' AND storage_key IS NOT NULL`:

- the same file twice in one course → 409 with the existing `document_id`, whatever
  the file names (also under concurrency: the database decides, only that index's
  violation becomes 409);
- the same file in two courses → two documents, two files; nothing about the other
  course is revealed;
- legacy rows (no stored file) are outside the index, so their hash format does not
  matter and is not rewritten;
- a `failed` document still counts: re-processing is an operator retry
  (`python -m app.ingestion.admin retry-parse`, M3), not a re-upload.

## Ingestion jobs

`ingestion_jobs`: `id`, `document_id` (FK, `ON DELETE CASCADE`), `kind`
(`parse` | `index`), `status` (`queued` | `running` | `succeeded` | `failed`,
default `queued`), `attempt_count` (default 0), `max_attempts` (default 3),
`error_message` (operational, never public), `created_at`, `updated_at`,
`started_at`, `finished_at` (all `timestamptz`).

Invariants enforced by CHECK constraints (each written to be TRUE or FALSE, never
NULL): known kind and status; `1 <= max_attempts`, `0 <= attempt_count <=
max_attempts`; `queued` has no `finished_at`; `running` has `started_at`, no
`finished_at` and at least one attempt; `succeeded` has both times in order and at
least one attempt; `failed` has `finished_at` (after `started_at` if it started).
A partial unique index allows at most one `queued`/`running` job per document and
kind; finished jobs are kept as history.

Transitions (applied by M3 with conditional `UPDATE`s, fenced by an attempt token):
`queued → running` (claim, `attempt_count + 1`, `started_at`, token, lease),
`running → succeeded`, `running → queued` (retryable failure while attempts remain,
with backoff), `running → failed`. A failed job stays failed: an operator retry
creates a **new** job, so no attempt identity is ever reused. Revision 0003 adds the
scheduling and fencing columns; the full state machine, dispatch and lease recovery
are in [document-processing.md](document-processing.md).

## Dependencies

FastAPI 0.141.1, Starlette 1.3.1 (pinned directly, because the upload code uses
Starlette's multipart parser), python-multipart 0.0.34. Starlette 1.3.1 fixes every
Starlette advisory published so far, including CVE-2025-54121 (event-loop blocking
during multipart file rollover, fixed in 0.47.2); python-multipart 0.0.34 includes
all of its own published fixes. Starlette 1.3.1 was the current release when
FastAPI 0.141.1 was published. Newer FastAPI lines (0.142 and later) add an
`opentelemetry-api` dependency, and 0.164+ require Starlette 1.6+; moving to them
is a separate, deliberate upgrade.

## Timestamps

New columns are `timestamptz`. `documents.created_at` is the baseline's naive
`timestamp`; it is read as UTC. That holds for the docker-compose database, whose
server time zone is UTC (the image default), and for every upload, which writes
`timezone('UTC', now())` explicitly. Legacy values are not converted; if a database
ever ran with another server time zone, its legacy values would be in that zone.

## Tests

```bash
bash services/core-api/scripts/test_migrations.sh -v
```

Runs everything on a throwaway PostgreSQL (see `docs/database-migrations.md`). The
API tests build their own application per test (`tests/api_support.py`): a session
factory whose every connection re-verifies the disposable database, a temporary
upload directory, and a principal injected on that instance only. Each test deletes
the rows it created. Without the disposable database the database tests are
skipped; the file-name, PDF, multipart, storage, mapping and offline-SQL tests
still run.

## Integration notes

### CE-2 (web)

- Endpoints, request and response shapes are as in `apps/web/src/lib/api/live.ts`
  and `types.ts`; `job_id` is extra and can be ignored.
- Only PDF is accepted for now. The file picker offers PDF, PPTX, DOCX, TXT and MD;
  the others get 415 `unsupported_file_type` with a readable message. Consider
  `accept=".pdf"` until more formats land.
- Since M3, documents move from `queued` to `processing` (parsing, then waiting for
  indexing) and stay `processing` until M4 indexes them, so the materials screen keeps
  polling every 1.5 s while one is listed; a back-off would help. See the CE-2 notes
  in [document-processing.md](document-processing.md). `stale` shows as `failed`
  with an explanation.
- `kind` can be `null` in the database for legacy and external rows; the API sends a
  display default. Making `kind` nullable in `types.ts` would be more honest.
- Still missing on the server: `GET /courses` and `/auth/login` (needed before the
  live web app can reach this screen), `DELETE /documents/{id}` (the UI's remove
  button gets 404).
- `contracts/core-api.web.md` (CE-2's file) could note the 409 duplicate response
  and the additive `job_id`.

### CE-3 (auth, DevOps)

- Implement `app.auth.get_current_user` per the contract in that module: verify the
  JWT, load the `users` row every request, 401 for missing/invalid/unknown, reject
  disabled accounts (403 is what CE-1 assumes; tell us if you prefer 401), return
  `CurrentUser(id, role, status)`. No other module parses tokens.
- Course-level checks (ownership, enrollment) live in `app/services/course_access.py`;
  please confirm this split matches "one current_user dependency".
- Compose: the core-api service mounts the named volume `uploads` at `/data/uploads`
  and sets `UPLOAD_DIR`; the M3 workers mount the same volume (CE-3 notes in
  [document-processing.md](document-processing.md)).
- Production proxy: body limit around 26 MB, timeouts and rate limiting.
- CI: run `bash services/core-api/scripts/test_migrations.sh -v`; plain `pytest`
  skips every database test.

### AI-1 (indexing) — proposal, not a contract change

- CE-1 parses; AI-1 chunks and embeds. Since M3, CE-1 stores normalized
  `document_sections` (`document_id`, `ordinal`, `text`, `locator` JSONB such as
  `{"page": 6}`) and queues an `index` job; calling the index endpoint is M4
  (see [document-processing.md](document-processing.md)).
- Proposed `POST /ai/v1/index/documents` payload: `document_id`, `course_id`,
  `origin`, `title`, `url`, `licence`, `language`, and `sections: [{ordinal, text,
  locator}]`. Chunks keep `document_id`/`course_id`/`origin` and carry the
  section's locator. Changing `contracts/ai-service.openapi.yaml` needs one CE and
  one AI approval; M2 does not change it.
