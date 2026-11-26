# Web client ↔ core-api contract reconciliation (WP-10)

Compares every call in `apps/web/src/lib/api/live.ts` (shapes in `apps/web/src/lib/api/types.ts`, intent in
`contracts/core-api.web.md`) with the implemented API (`contracts/core-api.v1.openapi.json`, authoritative
for existing behaviour). The web draft is a set of requirements, not a reason to weaken checks or break
working endpoints. Audited at main `09b63e4` + WP-10.

Legend: **OK** compatible · **CE-1** backend fix · **CE-2** frontend adaptation · **Blocked** another team.

## Matrix

| # | Web method → request | Backend | Verdict |
| --- | --- | --- | --- |
| 1 | `login` POST `/auth/login` | missing; `get_current_user` fails closed (401) everywhere | **Blocked CE-3** (JWT, login) |
| 2 | `listCourses` GET `/courses` | `CourseItem[]` = web `Course` | **OK** |
| 3 | `listTopics` GET `/courses/{id}/topics` | `TopicItem[]` `{id, name}` | **OK** |
| 4 | `listDocuments` GET `/courses/{id}/documents` | `DocumentItem[]` (built from the web type) | **OK** |
| 5 | `uploadDocument` POST multipart `file` | 202 + `DocumentItem` + `job_id`; 409 duplicate carries `document_id`; PDF only (415) | **OK** (extra field ignored) |
| 6 | `deleteDocument` DELETE `/documents/{id}` | was missing | **CE-1 fixed in WP-10**: 204, see [document-withdrawal.md](document-withdrawal.md) |
| 7 | `ask` POST `/courses/{id}/chat` `{question, scope}`, SSE `token`/`done`/`error` | same body (strict) and framing | **OK** for `class_only`, `class_plus_external`; **CE-2** for the rest (below); answers need the AI-1 chat service (**Blocked AI-1** for real use) |
| 8 | `generateQuiz` POST `/courses/{id}/quizzes` → `Question[]` | needs `Idempotency-Key`; returns the `QuizResponse` envelope | **CE-2** ([frontend-quiz-adaptation.md](frontend-quiz-adaptation.md)); generation **Blocked AI-1** |
| 9 | `submitAttempt` POST `/courses/{id}/attempts` | needs `quiz_id`; `citations[]`, baseline `mastery`, no `mastery_before` | **CE-2** (same doc); model mastery **Blocked AI-2** |
| 10 | `getStudentDashboard` GET `/courses/{id}/dashboard` | `DashboardResponse` (`?tz=`) | **CE-2** ([frontend-dashboard-adaptation.md](frontend-dashboard-adaptation.md)); predictions **Blocked AI-2** |
| 11 | `getClassAnalytics` GET `/courses/{id}/analytics` | `AnalyticsResponse` (privacy policy, weekly releases) | **CE-2** (same doc) |
| 12–17 | `listExams`, `getExam`, `createExam`, `updateExamItem`, `approveExam` on `/exams/{id}/…` | course-scoped `/courses/{c}/exams/{e}/…`, `Idempotency-Key`, `If-Match`, cursor envelope, finalize instead of approve | **CE-2** ([frontend-exam-adaptation.md](frontend-exam-adaptation.md)) |
| 15 | `regenerateExamItem` POST `/exams/{id}/items/{item}/regenerate` | no AI drafting of exam items | **Blocked AI-1** (no endpoint until an AI exam-drafting contract exists) |
| 18 | `getSystemOverview` GET `/admin/overview` | `AdminOverview` (feature switches, source/document/job counts; `documents.withdrawn` added in WP-10) | **CE-2**; service health and token usage are not collected (no data, not planned in CE-1) |
| 19 | `listUsers`, `createUser`, `updateUser` on `/admin/users` | missing | **Blocked CE-3** (accounts, invitations, role changes) |
| 20 | `listSources` GET `/admin/sources` | `{items, next_cursor}` of `AdminSource` | **CE-2** |
| 21 | `createSource` POST `/admin/sources` (any domain) | missing by design: sources are provider connectors (Wikipedia, OpenStax) with licence, budget and parser | **Not implemented** (product decision; arbitrary domains would bypass the harvesting safeguards) |
| 22 | `updateSource` PATCH `/admin/sources/{id}` `{enabled}` | PUT `/admin/sources/{id}/enabled` `{enabled}` → `AdminSourceChange` | **CE-2** |
| 23 | `runSource` POST `/admin/sources/{id}/run` | POST `/admin/harvest-runs` `{course_id, source_id}` → 201 `AdminHarvestRunCreated` (harvests are per course) | **CE-2** |
| 24 | `listJobs` GET `/admin/jobs` → `Job[]` | `{items, next_cursor}` of `AdminJob` | **CE-2** |
| 25 | `retryJob` POST `/admin/jobs/{id}/retry` → `Job` | same path, 201 `AdminJobRetry` | **CE-2** (response shape) |

Cross-cutting, all **OK**: base path `/api/v1`, snake_case, bearer auth, error bodies (`detail` + `code`; the
client already reads `detail`), 401/403 handling, CORS (WP-7: `DELETE`, `If-Match`, `Idempotency-Key`
allowed). The client's 404 text ("may not be built") now usually means "not found or not yours".

## CE-1 changes in WP-10

1. `DELETE /api/v1/documents/{document_id}` (row 6). Matches `deleteDocument` as written: no body, 204.
2. `AdminOverview.documents.withdrawn` (additive; without it a withdrawn row would make the overview fail).

Nothing else was changed in the backend: every other difference is either a deliberate, documented backend
contract (idempotency, optimistic concurrency, privacy envelopes, course-scoped paths) or owned by another
team. Compatibility aliases (e.g. `PATCH /admin/sources/{id}`, course-less `/exams/{id}`) were rejected: the
response shapes would still differ, and they would duplicate audited or concurrency-guarded mutations.

## CE-2 adaptation requirements (exact)

- **`deleteDocument`**: no change needed. Optionally treat 409 `document_not_withdrawable` as "external
  material cannot be removed" and hide the remove action for `origin !== "class"` and for non-owners.
- **`ask`**: offer `class_plus_web` nowhere (422; live-web retrieval does not exist). Add `provenance` and
  `topic_ids` to `ChatMessage`/`Citation` and render `provenance.attribution` with links to `revision_url`
  and `licence_url` for harvested external citations (`core-api.web.md` §1). Handle 429 with `Retry-After`
  and the `error` event's `code` (the `message` is user-facing).
- **Quiz, attempts, dashboard, analytics, exams**: follow the three adaptation documents linked in the matrix;
  they list request bodies, headers, envelopes, and status codes field by field.
- **Admin overview**: render `features`, `sources {total, enabled}`, `documents {pending, indexed, failed,
  stale, withdrawn}`, `jobs.{parse,index,harvest}` counts; drop `services`/`usageByDay` or show "not
  available".
- **Admin sources**: list from `items` (paginate with `cursor=next_cursor`); map `provider`/`kind`, `enabled`,
  `licence`, `licence_url`, `documents` counts, `last_run {status, created_at, finished_at, error_code}`.
  Toggle with `PUT /admin/sources/{id}/enabled` and body `{"enabled": true|false}`. "Run now" needs a course:
  `POST /admin/harvest-runs` with `{"course_id", "source_id"}`. Hide "add source".
- **Admin jobs**: list from `items`; `kind ∈ {parse, index, harvest}`, `status ∈ {queued, running,
  succeeded, failed, cancelled}`, `attempts`, `subject`, `error` (a stable code such as `document_withdrawn`,
  never detail). After `retryJob`, reload the list (the response is `{job_id, retried_job_id, document_id,
  kind, status, created_at, audit_event_id}`); 409 `retry_refused` explains why a job cannot be retried.
- Mutations: see [admin-api.md](admin-api.md) for 409/422 codes and audit behaviour.

## Blocked by other teams

- **CE-3**: `POST /auth/login` and token issuing, the real `get_current_user`; `/admin/users` management.
  Until then live mode answers 401 everywhere (by design: no development bypass).
- **AI-1**: chat answers, quiz generation, indexing provider (documents stay `processing` without it), exam
  item regeneration; live-web scope.
- **AI-2**: mastery predictions (`trend`, `next_review_at`, model mastery on dashboard and attempts).
