# Document withdrawal (WP-10)

`DELETE /api/v1/documents/{document_id}` lets the professor who owns a course withdraw a class upload.
The web client already calls it (`deleteDocument` in `apps/web/src/lib/api/live.ts`). Revision 0013.

## Contract

| Case | Status | `code` |
| --- | --- | --- |
| Withdrawn now, or already withdrawn (repeat, double click, retry after a lost response) | **204**, empty body | – |
| No token / unknown principal | 401 (`WWW-Authenticate: Bearer`) | `not_authenticated` |
| Disabled account | 403 | `account_disabled` |
| The caller can read the course but does not own it (an enrolled student) | 403 | `withdraw_not_allowed` |
| No such document, a document without a course, or a course the caller cannot read (other professors, a professor holding a student enrollment, unenrolled users, admins) | 404 | `document_not_found` |
| Owner, but the document is external material (harvested, OpenStax/Wikipedia, web cache) | 409 | `document_not_withdrawable` |
| Malformed id (not a UUID) | 422 | – (FastAPI's validation body, `detail` is a list) |
| Database unreachable | 503 | `database_unavailable` (repeating the request is safe) |

Errors use the usual body `{"detail": "...", "code": "..."}`. Permission is checked before anything about
the document is revealed: a student gets 403 even for external material, and a caller with no access to
the course cannot tell a missing document from a hidden one.

## Behaviour

One transaction, in the ingestion state machine's lock order (`app/ingestion/jobs.py`):

1. Re-check access on the locked user and course rows (`FOR SHARE`), as an upload does.
2. Lock the document's job rows (parse before index), then the document (`FOR UPDATE`).
3. Already `withdrawn`: nothing to do (204). Not a class upload: 409.
4. Cancel every `queued`/`running` job of the document: `failed`, `error_message =
   "document_withdrawn: …"` (admin job lists show the code `document_withdrawn`).
5. `documents.status = 'withdrawn'`, `withdrawn_at`, `withdrawn_by` (the professor).

After the commit:

- **Listing.** `GET /courses/{id}/documents` no longer returns it, for anyone.
- **Retrieval, chat, quizzes.** `retrieve_index_chunks` serves only `indexed` documents; chat citation
  verification and quiz provenance reject a document that is not `indexed`; stored quiz citations to it are
  hidden and counted in `citations_omitted`; dashboard material targets and `document_viewed` study events
  require `indexed`. No AI-side change is needed.
- **Workers cannot resurrect it.** A running attempt is fenced (its writes require `status = 'running'` and
  its token, so parse/index completion and failure return `stale` and write nothing); parse and index
  completion refuse a non-`pending` document; `commit_index_generation` refuses a non-`pending` document and
  a job that is not running; parse/index retries (worker, maintenance and `POST /admin/jobs/{id}/retry`)
  accept only `failed` documents (409 `retry_refused`). The trigger `documents_withdrawal_final` refuses any
  later `UPDATE` of a withdrawn row, whatever the code path or role.
- **Races.** A parse completion in progress makes the withdrawal wait for it; the index job it hands off is
  then cancelled. A provider generation committed just before the withdrawal stays unpublished. A quiz being
  persisted holds the cited documents `FOR SHARE`, so it either commits first (its citation is hidden later)
  or fails verification.
- **Deduplication.** Withdrawn rows leave the course-scoped unique index, so the same PDF can be uploaded
  again (a new document and job). Duplicates of a live document are still refused (409).
- **Harvesting.** External material is never touched, and the course's `dirty_generation` is not raised:
  harvest targets come from the course's topics, not from individual uploads.
- **Admin overview.** `documents.withdrawn` counts withdrawn rows (additive field).

## Retention and limitations (deferred)

Withdrawal hides; it does not erase. Kept on purpose, for a later controlled purge:

- the stored PDF (`storage_key`), its sections, chunks, embeddings, topic links and index generation;
- its jobs (history) and the `withdrawn_at` / `withdrawn_by` record.

Nothing is deleted before or after the commit, so a failed request can never lose a file a committed row
still points to. A purge (files and derived index data) needs its own work package: the WP-8 runtime role
cannot delete `chunks` or `index_generations` by design, so it would be a reviewed owner-run routine, and a
data-removal request (personal data uploaded by mistake) must be handled by an operator until then.

Other limitations: withdrawal is final (no undo; upload the file again instead), there is no professor UI
for withdrawn history, and only the course owner can withdraw (no admin override, consistent with the
course access policy in [courses-api.md](courses-api.md)).

## Operations

- Migration: [database-migrations.md](database-migrations.md#revision-0013-rollout). No grant changes; run
  `scripts/db_privileges.sql` after the upgrade as after every upgrade.
- Log line on success: `document_withdrawn document_id=… course_id=… by=… cancelled_jobs=…`.
- Tests: `services/core-api/tests/test_wp10_withdrawal.py` (disposable infrastructure only).
