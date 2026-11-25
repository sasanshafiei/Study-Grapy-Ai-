# Admin observability API (WP-4, read-only)

Three `GET` endpoints for the admin console's health views. They read existing tables and change
nothing. Shapes are in [`contracts/core-api.v1.openapi.json`](../contracts/core-api.v1.openapi.json)
(`Admin*` schemas). Source enable/disable, "run now" and job retry are a later work package; until
then use the operator CLIs (`app.harvest.admin`, `app.ingestion.admin`).

| Endpoint | Query parameters | Returns |
| --- | --- | --- |
| `GET /api/v1/admin/overview` | none | feature switches, source totals, document counts by status, parse/index job and harvest run counts by status |
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

## Limitations

- Usage and cost (`llm_usage`) and service health beyond the feature switches are not available:
  the schema has no usage table and the API makes no calls to other services.
- Mastery delivery batches are not listed (they are learner-linked).
- `attempts.limit` is `max_attempts` for parse/index jobs and `max_claims` (slices) for harvest runs.
- The field names follow this API's conventions, not yet `apps/web`'s draft admin types; CE-2 maps
  them (or the two are reconciled in a later work package).
