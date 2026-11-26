# Admin API (WP-4 observability, WP-9 audited mutations)

Three `GET` endpoints for the admin console's health views, which read existing tables and change
nothing, and three audited mutations (see [Mutations](#mutations-wp-9)). Shapes are in
[`contracts/core-api.v1.openapi.json`](../contracts/core-api.v1.openapi.json) (`Admin*` schemas).

| Endpoint | Query parameters | Returns |
| --- | --- | --- |
| `GET /api/v1/admin/overview` | none | feature switches, source totals, document counts by status (incl. `withdrawn`, WP-10), parse/index job and harvest run counts by status |
| `GET /api/v1/admin/sources` | `enabled=true\|false`, `limit`, `cursor` | `{items, next_cursor}`: each source's provider, language, domain, licence, enabled, number of courses with a harvest target, document counts by status, and its latest harvest run |
| `GET /api/v1/admin/jobs` | `kind=parse\|index\|harvest`, `status=queued\|running\|succeeded\|failed\|cancelled`, `limit`, `cursor` | `{items, next_cursor}`: document parse/index jobs and harvest runs with status, timestamps, attempts, subject ids and a safe error summary |

## Access

- Authentication is CE-3's `get_current_user` (it still refuses every request with 401 until CE-3
  ships it). There is no bypass.
- The caller's `users` row is read again for every request and inside the data snapshot: only an
  existing, `active` account with role `admin` passes. A vanished account gets 401
  `not_authenticated`, a disabled one 403 `account_disabled`, any other role 403 `admin_required`.
  A role claimed by the token alone is not trusted.
- Authorization comes before parameter validation, so non-admins learn nothing about the API.

## Paging, filters and errors

- Lists are newest first: `created_at` descending, `id` descending as the tie-breaker. `limit` is
  1–100 (default 50). `next_cursor` is `null` on the last page.
- A cursor is opaque and bound to the endpoint, the admin who received it and the filters; reuse with
  other filters or by another admin gives 422 `invalid_cursor`.
- Unknown, repeated or malformed parameters give 422 `invalid_query`. An empty result is
  `{"items": [], "next_cursor": null}`.
- Every response, errors included, carries `Cache-Control: no-store`. Unexpected failures are the
  generic 500; details go to the log as exception class and SQLSTATE only.

## What is never returned

Document text, titles, class-material URLs, learner data, credentials or the stored operational
message of a job. A parse/index job's `error` is `{code, message}`: the stable failure code and the
reviewed public text for that code (unknown codes get the generic text, an unparseable legacy value
the code `unknown`). A harvest run's `error` carries its stored `error_code` and `message: null`.

## Mutations (WP-9)

| Endpoint | Body | Success |
| --- | --- | --- |
| `PUT /api/v1/admin/sources/{source_id}/enabled` | `{"enabled": true\|false}` | 200 `{source_id, enabled, changed, cancelled_runs, audit_event_id}` |
| `POST /api/v1/admin/harvest-runs` | `{"course_id": "<uuid>", "source_id": "<uuid>"}` | 201 `{run_id, course_id, source_id, status: "queued", trigger: "manual", created_at, audit_event_id}` |
| `POST /api/v1/admin/jobs/{job_id}/retry` | none (or `{}`) | 201 `{job_id, retried_job_id, document_id, kind, status: "queued", created_at, audit_event_id}` |

Each runs exactly the transition of the operator CLI it mirrors (`app.harvest.admin enable-source` /
`disable-source` / `trigger`, `app.ingestion.admin retry-parse` / `retry-index`), through the same functions:

- **Sources.** Only provider-managed sources (`provider` set) can be switched; nothing is created. Enabling
  only sets the flag: course opt-in, approved queries, `HARVEST_ENABLED` and the provider budget still gate
  every run, and no run is scheduled by it. Disabling cancels the source's queued and running runs
  (`error_code = source_disabled`); a running attempt is fenced out by its token. A request for the state the
  source already has, with no run to cancel, returns `changed: false` and writes nothing.
- **Harvest trigger.** Refused with 409 while `HARVEST_ENABLED` is false (`harvest_disabled`), the course is
  not opted in (`course_not_opted_in`), the course is not opted in for that source (`target_missing`) or
  the source is disabled (`source_disabled`); 404 for an unknown course or source. One active run per course
  × source (the partial unique index): a repeat gets 409 `harvest_run_active` with the active `run_id`. The
  run is only queued, with `HARVEST_RUN_MAX_CLAIMS` / `MAX_FAILURES`; the harvest worker makes every
  provider request later, under the budget and quota rules.
- **Job retry.** `job_id` is a failed `parse` or `index` job from `GET /admin/jobs`; its document and kind come
  from the job, so no other resource can be named. A harvest run id is 404 `job_not_found`. A job that is not
  `failed` is 409 `job_not_failed`; an index retry while `INDEXING_ENABLED` is false is 409
  `indexing_disabled`. Everything else is the CLI's rules (document `failed`, no active job, no succeeded job of
  that kind, stored file / stored sections, current external version), refused as 409 `retry_refused`. A new
  job row is inserted; the failed job keeps its status, attempts and stored error.

Order of checks: authentication, the admin row (read-only), an empty query string, the path id (a
non-canonical UUID is 404), the strict JSON body (`application/json`, at most 1 KiB, exactly the listed
fields: 400/408/413/415/422 as in the other JSON endpoints), then one transaction that locks the caller's
`users` row `FOR SHARE`, re-checks it is an active admin, applies the transition, inserts the audit event and
commits. A lock or statement timeout is 503 `admin_busy` with `Retry-After: 2`; nothing was committed. Every
response carries `Cache-Control: no-store`.

### Audit events

`admin_audit_events` (revision 0012) gets one row per committed mutation, in the same transaction, so a
change and its audit row commit together or not at all:

| Column | Content |
| --- | --- |
| `actor_id` | the authenticated admin (`users.id`), taken from the server-side principal, never from the request |
| `action`, `entity_kind`, `entity_id` | `source_enabled` / `source_disabled` → source; `harvest_triggered` → course; `parse_retried` / `index_retried` → document |
| `operation_id` | the run or job the action created (null for sources) |
| `details` | ids, before/after states, counts and the failed job's stable error code; at most 2 KiB |
| `created_at` | database clock |

A trigger refuses an inserted row whose actor is not an existing, active admin, and refuses UPDATE, DELETE
and TRUNCATE; the actor foreign key keeps the account while history names it. There is no API to read,
edit or delete audit events. Details never contain titles, text, URLs, stored operational messages,
tokens or exception text; the server log gets one `admin_mutation` line with ids only.

## Limitations

- Usage and cost (`llm_usage`) and service health beyond the feature switches are not available:
  the schema has no usage table and the API makes no calls to other services.
- Mastery delivery batches are not listed (they are learner-linked).
- `attempts.limit` is `max_attempts` for parse/index jobs and `max_claims` (slices) for harvest runs.
- The field names follow this API's conventions, not yet `apps/web`'s draft admin types; CE-2 maps
  them (or the two are reconciled in a later work package).
