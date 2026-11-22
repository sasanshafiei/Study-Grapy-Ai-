# Professor exam API (core-api exams v1, M9-A1)

The manual professor exam builder: question bank, approval with immutable revisions, exam drafts with a
blueprint, items and ordering, preview with a review fingerprint, finalization, archival, duplication,
PDF/DOCX export ([exam-export.md](exam-export.md)), course-scoped authorization, audit and idempotency.
It implements the approved Phase A V3 contract (`M9_API_CONTRACT_DRAFT.md`); the few points where the
implementation had to decide something the contract left open are listed in
[Clarifications](#clarifications-relative-to-the-v3-contract).

**Not in M9-A1:** AI-generated exam candidates, regeneration and quality flags (M9-A2). They need AI-1's
approval of the proposed `studygraph.exam/1` protocol first; `EXAM_GENERATION_ENABLED=true` is refused by
the settings validator and no generation code path exists. M9-A1 alone is a manual builder and does not
complete the capstone's AI-generated exam workflow.

All paths are under `/api/v1/courses/{course_id}`. Every switch is off by default
(`EXAMS_ENABLED`, `EXAM_EXPORT_ENABLED`, `EXAM_GENERATION_ENABLED`).

## 1. Conventions

- JSON (UTF-8), snake_case, canonical lower-case UUIDs, timestamps `2026-11-21T10:15:00Z`.
  A non-canonical id in a path names nothing and is answered like an unknown one (404).
- Bodies are read strictly (`app/json_body.py`): `application/json` (charset utf-8 only), no
  compression, valid Content-Length, at most `EXAM_MAX_BODY_BYTES` (64 KiB), within 10 s, no duplicate
  keys, no NaN/Infinity, top level an object, **exact key sets** (nullable keys are sent as `null`;
  unknown or missing keys: 422 `invalid_fields`). A boolean is never an integer.
- The body is read only after authentication, authorization and the switches.
- Responses are validated through `app/schemas/exams.py` (`extra="forbid"`).
- Text: trimmed; no control characters except newline and tab in multi-line fields (stem,
  explanation, model answer, rubric, instructions, deviation reason); none at all in single-line
  fields (exam title, choice texts); no unpaired surrogates; lengths in code points.
- Every response, errors and the 401 of the authentication dependency included, carries
  `Cache-Control: no-store` and `Pragma: no-cache` (files: `no-store, private`).
- Errors: `{"detail": "<for a person>", "code": "<stable>", ...extra}`. Branch on `code`.

### Order of checks

1. Authentication (CE-3's dependency; fails closed: 401 `not_authenticated` until JWT exists).
2. Authorization from the `users`, `courses`, `enrollments` rows:

   | Caller | Result |
   |---|---|
   | unauthenticated / unknown user | 401 `not_authenticated` (+ `WWW-Authenticate: Bearer`) |
   | disabled account | 403 `account_disabled` |
   | active professor who currently owns the course | allowed |
   | active student enrolled as `student` | 403 `exams_not_allowed` (before any exam/question id is looked up) |
   | other professors, admins, unenrolled students, unknown course | 404 `course_not_found` (identical body) |

   Ownership is always the current `courses.owner_id` (`created_by` is historical). Admins have no
   implicit access.
3. `EXAMS_ENABLED` → 503 `exams_unavailable`; for files also `EXAM_EXPORT_ENABLED` → 503
   `exam_export_unavailable`.
4. Preconditions: `Idempotency-Key` (creates) or `If-Match` (mutations).
5. Query string / body.
6. Lookup (always filtered by course) and the work, in one short transaction with statement and lock
   timeouts; writes re-check authorization with `FOR SHARE` locks on user and course.
7. The response body is built in a fresh REPEATABLE READ READ ONLY transaction that re-checks
   authorization first (a revocation in between: 403/404, nothing sent). Files: the audit event
   transaction re-checks it, with `FOR SHARE` locks, before the first byte is sent.

### Idempotent creates (`Idempotency-Key`)

Required on E2, E9, E21: exactly one lower-case UUID (400 `idempotency_key_required` /
`invalid_idempotency_key`). New key → 201 + `Location`; same key and same fingerprint (operation,
course, canonical body) → 200 + `Idempotent-Replayed: true` with the resource's **current**
representation; same key, different fingerprint → 409 `idempotency_key_reused`. Keys are per user. Two
concurrent requests with one key create one resource; the other replays.

### Optimistic concurrency (`If-Match`)

Every mutable resource has an integer `version` (also `ETag: "<version>"` on single-resource
responses). Mutations need `If-Match: "<version>"`: exactly one strong tag of digits; missing → 428
`precondition_required`; weak tag, list, `*`, unquoted or leading zeros → 400 `invalid_precondition`;
stale → 412 `version_mismatch` with `current_version`. Item and blueprint endpoints use the **exam**
version. A request that changes nothing (identical body) answers 200 without a version bump.

**Transitions are idempotent** (E5 approve, E6 retire, E19 finalize, E20 archive), evaluated under the
row lock: version = If-Match → perform (already in the target state: 200 replay, no bump); else the
audit event of this transition with `entity_version = If-Match + 1` exists → 200 replay with
`Idempotent-Replayed: true` and the current representation (approve replays add `replayed_revision`);
else 412. **Finalize** replays only the identical intent: `finalized_version = If-Match + 1` and the
same `preview_fingerprint` and `blueprint_deviation_reason` as stored; otherwise 409
`finalize_intent_mismatch`. Read the state from the body, never from the replay header.

### Lists and cursors

`limit` 1–100 (default 50), opaque `cursor`; `{"items": [...], "next_cursor": "..." | null}`; no total.
Order `updated_at DESC, id DESC` (bank, exams), `created_at DESC, id DESC` (history), `revision DESC`
(revisions). Unknown, repeated or malformed parameters → 422 `invalid_query`. A cursor is base64url (no
padding, ≤ 512 characters) of `{"v": 1, "k": <sort key>, "id": <last id>, "b": <binding>}`; the binding
is the first 32 hex digits of SHA-256 over (endpoint, caller, course, exam/question, normalized filters,
sort, version). Any mismatch, malformed document, extra key or wrong type → 422 `invalid_cursor`. A cursor
is a position inside an authorization-scoped query, not a capability. Rows edited while paging can move.

### Bounds

| Bound | Value |
|---|---|
| stem / explanation | 1–4000 / ≤ 4000 or null |
| MCQ choices | 2–6, ids `a, b, c…` in order, text 1–500 single line, unique after NFKC + casefold + whitespace |
| short answer | model answer 1–4000, rubric ≤ 4000 or null (empty → null) |
| source references | ≤ 4, `{"document_id", "locator": {"page": n}}`, a class document of this course, `n ≤ page_count` when known |
| exam title / instructions / duration | 1–200 single line / ≤ 4000 or null / 1–600 minutes or null |
| items per exam | ≤ `EXAM_MAX_ITEMS` (200); points 1–100 |
| blueprint | ≤ 50 rows, count 1–200 per row, ≤ 200 in total, points_each 1–100 |
| bank / exams per course | ≤ 5000 (409 `question_limit_reached`) / ≤ 500 (409 `exam_limit_reached`) |
| export content budget | ≤ 300,000 code points over everything the answer key prints (§4) |

## 2. Question bank

A bank question is a `questions` row with `purpose = 'exam'` and the M9 marker `version`. Legacy and
practice questions are invisible here (404 `question_not_found`). States: `draft → approved` (E5),
`approved → draft` by any content change (E4, approval revoked, revisions kept), `draft | approved →
retired` (E6, final). Bank questions are never deleted.

**ExamQuestion** (detail; owner only): `id, course_id, status, version, type (mcq|true_false|short),
difficulty, bloom_level|null, topic {id,name}|null, stem, choices, answer_key, explanation|null,
source_refs [{document_id, title|null, locator, locator_label, available}], approved_revision|null,
content_sha256, used_in {draft_exams, finalized_exams}, created_at, updated_at`. `title` and
`available` are read live (null / false once the document is gone). True/false `choices` is always
`[{"id":"true","text":"True"},{"id":"false","text":"False"}]`; `answer_key` is `{"choice_id": ...}` for
MCQ/true-false and `{"model_answer", "rubric"}` for short answers.

**ExamQuestionSummary** (lists; no answer key, no explanation): `id, status, version, type, difficulty,
bloom_level, topic, stem_preview (first 200 code points), approved_revision, updated_at`.

| Id | Endpoint | Notes |
|---|---|---|
| E1 | `GET /exam-questions` | `status` (draft\|approved\|retired; default draft + approved), `type`, `difficulty`, `topic_id`, `bloom_level`, `q` (1–100 printable, case-insensitive substring; `%`, `_`, `\` are literal), `limit`, `cursor` |
| E2 | `POST /exam-questions` | Idempotency-Key. Body exactly `type, difficulty, bloom_level, topic_id, stem, choices, answer_key, explanation, source_refs`. 201 ExamQuestion (draft, version 1) |
| E3 | `GET /exam-questions/{id}` | 200 ExamQuestion + ETag |
| E4 | `PUT /exam-questions/{id}` | If-Match. Same body as E2, full replacement. 200 `{"question", "approval_revoked"}`; identical content → no change; retired → 409 `question_retired` |
| E5 | `POST /exam-questions/{id}/approve` | If-Match, no body (or `{}`). Rechecks source pages and freezes the current question and source titles in revision n+1. See approval rules below. |
| E6 | `POST /exam-questions/{id}/retire` | If-Match, no body |
| E7 | `GET /exam-questions/{id}/revisions` | `{"items": [QuestionRevision], "next_cursor"}`, newest first |
| — | `GET /exam-questions/{id}/history` | audit events of the question |

**True/false normalization:** `choices` is `null` or exactly the canonical array (anything else: 422
`invalid_question`, reason `true_false_choices`); both forms are the same content (same
`content_hash`; a PUT that differs only in that respect changes nothing). `mcq` with `choices: null` and
`short` with choices are 422 (`type_shape`).

`invalid_question` carries `field` and `reason` ∈ `length, control_characters, choice_count,
choice_ids, duplicate_choice, answer_key, type_shape, true_false_choices, topic, source_ref, locator,
source_ref_unavailable`.

**Source pages at approval.** A draft may cite a PDF before parsing finishes. Approval rechecks each
referenced class document in the same course under a row lock. A page above its known count returns
422 `invalid_question`, field `source_refs`, reason `locator`. Page numbers start at 1 and the last
physical page is valid, including a blank page. A missing document or a PDF without a page count returns
the same 422 with reason `source_ref_unavailable`. Once parsing records the count, the professor can
retry with the same question version if the draft has not changed. Indexing is independent of approval.

The upload service records PDFs as `application/pdf`. Other class sources and legacy rows without a
recorded MIME type enforce the upper bound when a count exists.
Exam source references accept only page locators. These checks apply when creating a new approved
revision. Previously approved revisions and finalized exams retain their frozen citations.

**QuestionRevision:** `revision, approved_at, approved_by_name, content_sha256, type, difficulty,
bloom_level, topic, stem, choices, answer_key, explanation, source_refs [{document_id, title,
locator_label}]` (titles frozen at approval, never re-resolved).

## 3. Exams

States: `draft` (editable) → `finalized` (immutable, exportable) → `archived`; `draft → archived`; no
delete, no re-opening. A correction is a duplicate (E21) that is finalized again.

**Exam:** `id, course_id, status, version, title, instructions, duration_minutes, derived_from_exam_id,
created_at, updated_at, finalized_at, archived_at, course_snapshot {code,name}|null, content_sha256,
finalized_version, blueprint_status (none|fulfilled|deviation_accepted)|null,
blueprint_deviation_reason, finalize_preview_fingerprint, content_chars, export_budget, item_count,
total_points, blueprint {rows [{id, position, topic, type, difficulty, bloom_level, count, points_each,
assigned}], total_count, total_points}, items [{id, position, points, blueprint_row_id, question_id,
question_status, question_version, pinned_revision, question {type, difficulty, bloom_level, topic, stem,
choices, answer_key, explanation, source_refs}}], readiness`.

Drafts show the **live** bank content (`pinned_revision: null`) and `readiness`; finalized (and
archived-finalized) exams show the **pinned revisions**, the frozen course header and `readiness: null`.

**Readiness** (drafts): `{ready, issues, warnings, blueprint {declared, fulfilled}, content_chars,
export_budget, preview_fingerprint}`. Issues block finalization; each carries `overridable`:

- never overridable: `no_items`, `question_not_approved`, `question_retired`, `too_many_items`,
  `export_budget_exceeded` (`content_chars`, `export_budget`);
- overridable **only** by an explicit `blueprint_deviation_reason` at E19: `blueprint_row_underfilled`
  / `blueprint_row_overfilled` (`blueprint_row_id`, `assigned`, `count`), `item_not_in_blueprint`
  (`item_id`), `blueprint_criteria_mismatch` (`item_id`, `blueprint_row_id`).

Warnings never block: `points_differ_from_blueprint`, `duplicate_stem` (`item_ids`),
`unsupported_characters` (`item_ids`; characters the vendored font cannot print), `missing_duration`,
`item_changed_since_source` (duplicates).

| Id | Endpoint | Notes |
|---|---|---|
| E8 | `GET /exams` | `status` (draft\|finalized\|archived; default draft + finalized), `limit`, `cursor`. ExamSummary: `id, status, version, title, item_count, total_points, finalized_at, archived_at, derived_from_exam_id, updated_at` |
| E9 | `POST /exams` | Idempotency-Key; `{"title", "instructions", "duration_minutes"}`; 201 Exam; 422 `invalid_exam` (`field`) |
| E10 | `GET /exams/{id}` | 200 Exam + ETag, one snapshot; `readiness.preview_fingerprint` equals E18's for that snapshot |
| E11 | `PATCH /exams/{id}` | If-Match; non-empty subset of the three fields; draft only (409 `exam_not_draft`) |
| E12 | `PUT /exams/{id}/blueprint` | If-Match; `{"rows": [{"id"\|null, "topic_id", "type", "difficulty", "bloom_level", "count", "points_each"}]}` → `{"exam", "blueprint_change": {kept, inserted, removed, unassigned_item_ids}}` |
| E13 | `POST /exams/{id}/items` | If-Match; `{"question_id", "points", "position"\|null, "blueprint_row_id"\|null}` → 201 Exam + `X-Exam-Item-Id` |
| E14 | `PATCH /exams/{id}/items/{item_id}` | If-Match; subset of `{"points", "blueprint_row_id"}` |
| E15 | `DELETE /exams/{id}/items/{item_id}` | If-Match; later positions shift down; 200 Exam |
| E16 | `PUT /exams/{id}/items/order` | If-Match; `{"item_ids": [...]}`, an exact permutation (422 `invalid_order`, reason `missing\|duplicate\|unknown`) |
| E17 | `POST /exams/{id}/fill-from-blueprint` | If-Match; `{"row_ids": null \| [...]}` → `{"exam", "filled": [{blueprint_row_id, added, missing, stopped_by?}]}`; approved, unretired, matching questions not yet in the exam, oldest first (`created_at, id`), at `points_each` |
| E18 | `GET /exams/{id}/preview` | `variant=student\|answer_key` (default student) |
| E19 | `POST /exams/{id}/finalize` | If-Match + `{"preview_fingerprint", "blueprint_deviation_reason"}` |
| E20 | `POST /exams/{id}/archive` | If-Match, no body |
| E21 | `POST /exams/{id}/duplicate` | Idempotency-Key; `{"title": str \| null}` → 201 `{"exam", "duplicate_report"}` |
| E22 | `GET /exams/{id}/export` | [exam-export.md](exam-export.md) |
| E23 | `GET /exams/{id}/preview-file` | [exam-export.md](exam-export.md) |
| E24 | `GET /exams/{id}/history` | audit events of the exam, its items, blueprint and files |

E13 errors: 404 `question_not_found`, 409 `question_retired`, `question_already_in_exam` (`item_id`),
`exam_item_limit`, `exam_not_draft`, `export_budget_exceeded` (`content_chars`, `export_budget`), 422
`invalid_points`, `invalid_position`, `invalid_blueprint_row` (`reason`: `unknown_row` |
`criteria_mismatch`). E12 errors: 422 `invalid_blueprint` (`row` index or null, `reason`: `count,
total_count, points, topic, criteria, unknown_row, duplicate_row, too_many_rows`; an id of another exam,
even of the same professor, is `unknown_row`).

### Blueprint replacement (E12)

One transaction (architecture §4.5): authorize (FOR SHARE) → exam FOR UPDATE, draft, If-Match → current
rows FOR UPDATE (ascending id) → validate the whole request → unassign the items of removed rows → delete
those rows → update retained rows (ids kept, also when only their position changes) and insert new ones at
their positions (the position key is `DEFERRABLE INITIALLY DEFERRED`, so swaps and reversals are legal) →
`SET CONSTRAINTS exam_blueprint_rows_exam_id_position_key IMMEDIATE` and revalidate positions 1..n, the
total count and that every item's row belongs to this exam (a failure is a defect: rollback, 500) → one
version bump → audit `blueprint_replaced` → commit. Retained rows keep their assignments even when their
criteria changed (non-matching items then show `blueprint_criteria_mismatch`). Item insert, removal and
reorder use the same pattern on `exam_items_exam_id_position_key`. Every composition change locks the exam
first, so concurrent changes are serialized and the loser gets 412.

### Preview and the preview fingerprint (E18)

```json
{"exam_id": "…", "status": "draft", "version": 7, "variant": "student",
 "header": {"course_code", "course_name", "title", "instructions", "duration_minutes", "question_count", "total_points"},
 "questions": [{"number": 1, "points": 2, "type": "mcq", "stem": "…", "options": [{"label": "A", "text": "…"}]},
               {"number": 2, "points": 1, "type": "true_false", "stem": "…", "options": [{"label": null, "text": "True"}, {"label": null, "text": "False"}]},
               {"number": 3, "points": 5, "type": "short", "stem": "…", "options": null, "answer_lines": 6}],
 "answer_key": null,
 "readiness": {"ready": false, "issues": [], "warnings": []},
 "preview_fingerprint": "pf1:<64 hex>"}
```

`variant=answer_key` adds `answer_key: [{number, points, answer, answer_text, model_answer, rubric,
explanation, sources ["<frozen title>, p. 6"]}]` (MCQ: `answer` = label, `answer_text` = choice text;
true/false: `answer` = "True"/"False"; short: `model_answer`, `rubric`). Drafts use the live course header;
finalized exams the frozen one. Draft previews (E18 and E23) print approved questions from their current
approved revisions, including the titles recorded at approval. The draft editor still reports the current
title and availability when a source is renamed or removed. Unapproved questions preview their working copies
and block finalization. The JSON preview works with `EXAM_EXPORT_ENABLED=false`.

**`preview_fingerprint`** = `"pf1:"` + lower-case SHA-256 of the canonical JSON (UTF-8, sorted keys, no
insignificant whitespace) of

```
{"v": 2,
 "exam": {"id", "version", "status", "title", "instructions", "duration_minutes"},
 "course": {"id", "code", "name"},                       (live values that finalization freezes)
 "items": [{"id", "position", "points", "blueprint_row_id", "question_id", "question_status",
            "approved_revision_no", "approved_content_sha256"}]   (by position)
 "blueprint": [{"id", "position", "topic_id", "qtype", "difficulty", "bloom_level", "question_count",
                "points_each"}]}                         (by position)
```

It is built from a query that loads no stem, choice, answer key, model answer, rubric or explanation, is
the same for both variants, always 68 characters, `null` for finalized exams, and is also returned in E10
`readiness` and the `X-Exam-Preview-Fingerprint` header of E23. It changes when anything finalization
would freeze or decide on changes (exam metadata, course code/name, items, points, order, assignments,
blueprint rows, a linked question's status or approved revision) and not for edits of questions outside
the exam. Input version 2 binds the review to the approved-revision printing rule. An outstanding draft
fingerprint issued under version 1 returns `412 exam_preview_stale` at finalization and needs a fresh
preview. The wire format stays `pf1:<64 hex>`. An identical retry of a previously committed finalization
still uses its stored fingerprint.

### Finalize (E19)

Body (both keys required): `{"preview_fingerprint": "pf1:…", "blueprint_deviation_reason": null | "…"}`.

| Situation | Result |
|---|---|
| no body or wrong key set | 422 `invalid_fields` |
| `preview_fingerprint` null | 428 `preview_fingerprint_required` |
| not `^pf1:[0-9a-f]{64}$` (prefix, case, length, type) | 400 `invalid_preview_fingerprint` |
| reason not 10–500 characters or with control characters | 422 `invalid_blueprint_override` |
| If-Match stale, not a replay of this intent | 412 `version_mismatch` (`current_version`) |
| `finalized_version = If-Match + 1` but other fingerprint or reason | 409 `finalize_intent_mismatch` |
| same version, recomputed fingerprint differs | **412 `exam_preview_stale`** (`current_version`; the current digest is not returned: preview again and review) |
| any non-overridable issue (a reason does not help) | 409 `exam_not_ready` (`issues`) |
| only blueprint issues, reason null | 409 `exam_not_ready` (issues with `overridable: true`) |
| reason given although the blueprint is fulfilled or absent | 422 `blueprint_override_not_needed` |
| exam finalized/archived, same version | 409 `exam_not_draft` |
| identical intent repeated (lost response) | 200 + `Idempotent-Replayed: true`, current state (also after archival) |
| ok | 200 Exam: `blueprint_status` `none` (no rows), `fulfilled`, or `deviation_accepted` (reason stored) |

Transaction: re-authorize (FOR SHARE) → exam FOR UPDATE → blueprint rows FOR SHARE and items FOR UPDATE
(ascending id) → questions FOR SHARE (ascending id) → recompute the fingerprint from the locked rows
(412 before any write) → readiness, strict blueprint decision and export budget on that snapshot, using
the approved revisions that will be pinned → pin every item to its question's current approved revision
(exactly the revisions the fingerprint named) → freeze course code/name, `item_count`, `total_points`,
`content_sha256`, `content_chars`, `finalized_version`, `blueprint_status` and reason,
`finalize_preview_fingerprint` → status finalized, version + 1 (the database re-verifies items, pins and
blueprint) → audit `exam_finalized` (fingerprint, content hash and size, totals, deviations) → commit.

**Strict blueprint:** an exam with blueprint rows is finalized only when every item is assigned, every row
has exactly `count` items and every assigned item's pinned revision matches the row's criteria; or with an
explicit, stored and audited deviation reason that applies to exactly the reviewed snapshot (bound by
If-Match and the fingerprint). Points may differ from `points_each` (warning).

### Archive (E20) and duplicate (E21)

Archive bumps `version` and sets `archived_at`; every finalization field, the pinned items, export bytes,
file names (`v<finalized_version>`) and file headers stay unchanged. An archived draft is never exportable.

Duplicate copies metadata (title `"<title> (copy)"` truncated to 200 when null), blueprint rows (new ids),
items (same bank questions, points, order, row assignments) and sets `derived_from_exam_id`. The new draft
shows the **current editable bank content**, not the source's pinned revisions. `duplicate_report` per
item: `{item_id, question_id, source_revision, current_status, current_approved_revision, content:
unchanged|changed|not_approved|retired}` (draft source: `source_revision` null, `unchanged`); every
non-`unchanged` item has warning `item_changed_since_source`. Above the budget → 409
`export_budget_exceeded`.

### History (E24) and audit

`AuditEvent`: `id, action, entity_kind, entity_id, entity_version, actor_name, created_at, details`.
Actions: `question_created, question_updated, question_approved, question_approval_revoked,
question_retired, exam_created, exam_updated, blueprint_replaced, item_added, item_updated, item_removed,
items_reordered, exam_filled_from_blueprint, exam_finalized, exam_archived, exam_duplicated,
export_downloaded, preview_file_downloaded`. `details` hold ids, hashes and counts only (≤ 4 KiB; long
id lists are truncated with a count), never stems, answers or explanations. Events are append-only.

## 4. Export content budget

`content_chars` counts every text the answer-key variant prints: exam title, instructions, course code and
name, and per item stem, choice texts (true/false: "True" and "False"), model answer, rubric, explanation,
source titles and locator labels. Approved items count their frozen titles even in drafts. It must stay ≤
`EXAM_EXPORT_MAX_CONTENT_CHARS` (300,000): adding items, filling and duplicating refuse growth beyond it
(409 `export_budget_exceeded`); bank edits are not blocked, readiness then shows the blocking issue and
finalization refuses. The finalized value is stored (CHECK 1–300,000). Calibration: [exam-export.md](exam-export.md).

## 5. Error codes

| Status | Codes |
|---|---|
| 400 | `invalid_body`, `idempotency_key_required`, `invalid_idempotency_key`, `invalid_precondition`, `invalid_preview_fingerprint` |
| 401 | `not_authenticated` |
| 403 | `account_disabled`, `exams_not_allowed` |
| 404 | `course_not_found`, `exam_not_found`, `question_not_found`, `exam_item_not_found` |
| 408 / 413 / 415 | `request_timeout` / `body_too_large` / `unsupported_media_type`, `unsupported_encoding` |
| 409 | `idempotency_key_reused`, `question_retired`, `question_already_in_exam`, `exam_item_limit`, `exam_not_draft`, `exam_not_ready`, `exam_not_finalized`, `question_limit_reached`, `exam_limit_reached`, `export_budget_exceeded`, `finalize_intent_mismatch` |
| 412 | `version_mismatch`, `exam_preview_stale` |
| 422 | `invalid_fields`, `invalid_query`, `invalid_cursor`, `invalid_question`, `invalid_exam`, `invalid_blueprint`, `invalid_blueprint_row`, `invalid_points`, `invalid_position`, `invalid_order`, `invalid_blueprint_override`, `blueprint_override_not_needed`, `invalid_export_option` |
| 428 | `precondition_required`, `preview_fingerprint_required` |
| 429 | `export_busy`, `too_many_exports` (+ `Retry-After`) |
| 500 | `server_error`, `export_failed` |
| 503 | `exams_unavailable`, `exam_export_unavailable`, `exam_busy` (with `Retry-After: 2`), `exam_response_unavailable` |
| 504 | `export_timeout` |

Unexpected errors are logged as exception class + SQLSTATE only, never with statement parameters.

### Recovering from an unavailable response

Lock and statement timeouts, deadlocks and serialization failures have two different recovery paths.
`exam_busy` on an ordinary read or before a write commits means this attempt made no new change.
Wait for `Retry-After` and retain the original request preconditions. An earlier attempt with the same
key or intent may already have succeeded.

For bank mutations (E2, E4–E6), exam creation and editing (E9, E11–E17), and finalization, archive
and duplication (E19–E21), the API reads the response in a fresh, authorized transaction. A busy error
during that read returns `503 exam_response_unavailable`. A change may already be committed. Replays
and no-ops can also receive this code. The response contains only `code` and generic `detail`,
with no resource identifiers or `Retry-After` header. Failed authorization still returns the usual
401, 403 or 404.

These recovery steps also apply to a generic server error or a lost connection when the outcome is unknown.

- **Create or duplicate.** Repeat the same operation in the same course, as the same user, with the
  original `Idempotency-Key` and equivalent body. A committed creation replays its current representation.
- **Approve, retire, finalize or archive.** Refetch the resource or replay the original transition with
  its original `If-Match`. Finalization also requires the exact reviewed fingerprint and deviation reason.
- **Other edits, including item and blueprint changes.** Fetch the question or exam and compare its
  version and content. Consult its history if later edits have intervened. A retry using a consumed
  version returns 412. Use a newer version only for a newly reviewed edit.

File endpoints (E22–E23) authorize before rendering, then recheck access and commit the download audit
event before sending bytes. A lost download response can leave an audit event even if the client received no file.

## 6. Leakage isolation

Exam questions are `purpose = 'exam'` rows: `quiz_items` (CHECK `purpose = 'practice'` + composite key)
can never reference one, `attempts_exam_guard` refuses any attempt on one, and no code path sends exam
content to indexing, chat, practice generation, mastery delivery, the dashboard or analytics.
`tests/test_exam_leakage.py` writes canaries into every exam field, calls every student-facing endpoint
as an enrolled student, inspects the payloads the fake AI services received, the student-side tables,
the upload directory and the logs.

## 7. Activation prerequisites

1. Migration 0008 applied ([database-migrations.md](database-migrations.md)); workers need revision 0008.
2. CE-3 authentication live (until then every call is 401).
3. CE-2 builder and CE-3 mobile approval screens adapted ([frontend-exam-adaptation.md](frontend-exam-adaptation.md)).
4. Advisors confirm that a *finalized* exam is the capstone's "approved exam" and the export layout.
5. `EXAMS_ENABLED=true`, then `EXAM_EXPORT_ENABLED=true` after a manual review of real exports.
6. Export limits are per API process; a shared (Redis) limiter is needed before several processes run.
7. CE-2 and CE-3 agree on the exact web origins for each deployment. Set `CORS_ALLOWED_ORIGINS`
   on core-api and verify cross-origin downloads through the deployed proxy (D-25).
   [Browser origins and readable headers](frontend-exam-adaptation.md#browser-origins-and-readable-headers)
8. M9-A2 (AI candidates) only after AI-1 approves `studygraph.exam/1`, the OpenAPI change is signed by one
   CE and one AI member, migration 0009 is reviewed, and its own test cycle passes.

## Clarifications relative to the V3 contract

Decisions the implementation had to make where the contract was silent; none weakens an approved rule.

| Topic | Implementation |
|---|---|
| Absent optional keys | Readiness issues/warnings carry only their own keys; preview questions carry `answer_lines` only for short answers; `filled[].stopped_by` appears only when filling stopped (`export_budget`, or `item_limit` when the 200-item cap stopped it) |
| E12 criteria values | an unknown `type`/`difficulty`/`bloom_level` is 422 `invalid_blueprint` with reason `criteria` |
| E7 cursor | the cursor's sort key is the revision number |
| Question history | `GET /exam-questions/{id}/history` (optional in the contract) is implemented |
| Unavailable source | live `source_refs[].title` is `null` when the document is gone (`available: false`) |
| True/false preview options | `label: null` (no letters, as printed) |
| No-op mutations | identical E4, E11, E12, E14, E16 bodies answer 200 without a version bump |
| Metadata edits | E11 does not check the export budget (like bank edits; finalization is authoritative) |
| Archived draft | E23 is 409 `exam_not_finalized` (only drafts and finalized exams have files) |
| Busy | A lock or statement timeout, deadlock or serialization failure returns the phase-specific 503 described in §5 |
| Audit timestamps | `clock_timestamp()`, so several events of one transaction keep their order |
