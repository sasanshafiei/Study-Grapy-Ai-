# Practice quizzes and graded attempts — core-api quiz v1 (M7)

Backend-driven, API-first contract. CE-2's earlier frontend types are a reference, not a
constraint; the differences are listed in [frontend-quiz-adaptation.md](frontend-quiz-adaptation.md).
Everything is **off by default** (`QUIZ_ENABLED=false`). Generation depends on the AI
service adopting the proposed `studygraph.quiz/1` ([ai-quiz-integration.md](ai-quiz-integration.md));
grading is local and never calls the AI service.

Scope of M7: multiple-choice (`mcq`) and true/false (`true_false`) questions from the
course's own indexed class material (`class_only`). Deferred: short answers and semantic
grading, external/web scope, real AI mastery (M8, [ai-mastery-integration.md](ai-mastery-integration.md)),
recommendations, spaced repetition (`next_review_at`), exams (M9), professor preview.

## Conventions

- JSON, snake_case, UUIDs as canonical lower-case strings, timestamps ISO 8601 UTC (`Z`).
- Errors: `{"detail": "<for a person>", "code": "<stable>", ...extra}` (same as every
  core-api endpoint). Clients branch on `code` and HTTP status, never on `detail`.
- Every response of these endpoints, errors included, carries `Cache-Control: no-store`
  and `Pragma: no-cache`.
- Response bodies are validated against explicit schemas (`app/schemas/quiz.py`) with
  unknown fields forbidden: a field not listed below can never appear.

## Who may call

An authenticated (CE-3, fail-closed: 401 until real authentication exists), **active
student with a current `student` enrollment** in the course. Checked from the database on
every request, before anything else:

| Caller | Status / code |
|---|---|
| not authenticated | 401 `not_authenticated` |
| disabled account | 403 `account_disabled` |
| the professor who owns the course | 403 `practice_not_allowed` |
| other professors, admins, unenrolled students, unknown course | 404 `course_not_found` |

Then the switches: `QUIZ_ENABLED` off → 503 `quiz_unavailable` (all endpoints);
`QUIZ_ATTEMPTS_ENABLED` off → 503 `quiz_attempts_unavailable` (attempts only);
`QUIZ_GENERATION_ENABLED` off or no configured AI client → 503 `quiz_generation_unavailable`
(only for a *new* generation; replays and GET keep working).

## POST /api/v1/courses/{course_id}/quizzes

Header `Idempotency-Key: <uuid>` (required; exactly one; canonical lower-case UUID;
400 `idempotency_key_required` / `invalid_idempotency_key`). Use a **new key per quiz the
student asks for**, and the **same key to retry that request** after a timeout or a lost
response.

```json
{"topic_ids": ["<uuid>", "..."], "count": 5, "difficulty": "easy|medium|hard|mixed", "scope": "class_only"}
```

`topic_ids`: 1..5 distinct topics of this course; `count`: 1..10 (integer, not boolean);
`scope`: optional, only `class_only`. Exactly these fields (422 `invalid_fields`,
`invalid_topics`, `invalid_count`, `invalid_difficulty`, `scope_unavailable`). Body: JSON,
UTF-8, at most `QUIZ_MAX_BODY_BYTES` (413/415/408/400 as for chat).

**201 Created** (new) or **200 OK** with `Idempotent-Replayed: true` (replay); `Location`
points at the GET URL.

```json
{
  "quiz_id": "<uuid>", "course_id": "<uuid>", "status": "ready", "scope": "class_only",
  "difficulty": "medium", "count": 2, "created_at": "...Z", "completed_at": "...Z", "failure": null,
  "questions": [
    {"id": "<question uuid>", "position": 1, "topic_id": "<uuid>", "topic_name": "Stoichiometry",
     "type": "mcq", "difficulty": "medium", "stem": "...",
     "choices": [{"id": "a", "text": "..."}, {"id": "b", "text": "..."}],
     "hint": "...", "attempt": null},
    {"id": "...", "position": 2, "type": "true_false",
     "choices": [{"id": "true", "text": "True"}, {"id": "false", "text": "False"}], "...": "..."}
  ]
}
```

Unanswered questions never carry the correct answer, the answer key, the explanation,
grading metadata or citation evidence. MCQ choices are shuffled once, at generation, with
a CSPRNG and labelled `a`..`f` *after* shuffling; the order and the ids are persisted, so a
replay or GET shows exactly the same ids in the same order, and an id says nothing about
correctness. True/false ids are always `true` / `false`.

### Idempotency and the request fingerprint

A session is identified by (student, `Idempotency-Key`). Its fingerprint is the SHA-256 of
the canonical JSON `{v: 1, course_id, topic_ids (sorted), count, difficulty, scope}`, so
the topic order does not matter but everything else does.

| Existing session for (student, key) | Answer |
|---|---|
| none | generate (below) |
| different fingerprint (other course, topics, count, difficulty or scope) | 409 `idempotency_key_reused` |
| `ready` | 200 replay of the persisted quiz (no AI call, no quota, works with generation disabled or the AI service down) |
| `generating`, lease valid | 409 `quiz_generation_in_progress`, `Retry-After: 5`, extra `{quiz_id, status: "generating"}` |
| `generating`, lease expired | recovered to `failed` (`lease_expired`), then as `failed` |
| `failed` | the same stable error as the first response (below) |

Other students' keys are independent.

### Generation lifecycle and states

```
                    (reserve: quota, lease)          (fenced persist)
  [no session] ───────────────────────────▶ generating ───────────────▶ ready   (terminal)
                                                │
                    upstream error, rejection,  │  lease expired + recovery
                    deadline, cancel, access    ▼  ("recovered")
                    lost, server error        failed  (terminal)
```

- **generating** — a request holds the session: a random attempt token and a lease in
  database time (`QUIZ_PIPELINE_DEADLINE_SECONDS + QUIZ_LEASE_MARGIN_SECONDS`). At most one
  per student (partial unique index).
- **ready** — questions and assignments were committed together with the state change, by
  the holder of the token while its lease was valid (fencing: a delayed generator that lost
  its lease can never finalize; the database trigger also requires exactly `count`
  assignments).
- **failed** — terminal, with an internal `failure_code`. Publicly:

  | failure_code | HTTP | code |
  |---|---|---|
  | `ai_unavailable` (upstream HTTP error, transport error, redirect, framing) | 503 | `quiz_ai_unavailable` |
  | `upstream_timeout`, `deadline` | 504 | `quiz_timeout` |
  | `rejected_protocol`, `rejected_provenance` | 502 | `quiz_rejected` |
  | `access_lost`, `cancelled`, `lease_expired`, `server_error` | 503 | `quiz_generation_failed` |

  Error extra: `{quiz_id, status: "failed", retry: "new_idempotency_key"}`; GET shows
  `failure: {code, detail, retry}` with the same public code.
- **recovered** — not a separate state: a `generating` session whose lease expired (the
  process crashed or was killed) is turned into `failed` / `lease_expired` by the
  maintenance recovery task (`recover_stale_parse_jobs`, every `RECOVERY_INTERVAL_SECONDS`),
  by the student's next POST, and by GET of that quiz.

**Client retry behaviour.**
- Network error, timeout, or 5xx without a body you can read → retry the *same* request
  with the *same* key. You get the quiz (200), `quiz_generation_in_progress` (wait
  `Retry-After`, retry the same key) or the stable failure.
- 409 `quiz_generation_in_progress` → wait and retry with the same key, or GET the quiz.
- A stable failure (502/503/504 with `quiz_id` and `retry: "new_idempotency_key"`) → the
  attempt is over; to try again, send a **new key** (this consumes quota again).
- 503 `quiz_ai_unavailable` / 504 `quiz_timeout` **without** `quiz_id` → nothing was
  created (refused before the reservation: the AI service is not usable, or the deadline
  passed before the reservation); the same key may be retried.
- 429 `quiz_generation_busy` (another generation of this student, or the server's
  concurrency limit) → retry after `Retry-After`; nothing was created.
- 429 `quiz_quota_exceeded` → `Retry-After` is the number of seconds until the next UTC day.

### Request order and bounds

1. lookup (short transaction) → replay without the AI service;
2. generation switch and client, in-process slot (`QUIZ_MAX_CONCURRENT`, one per student);
3. capability probe (cached ≤ `QUIZ_CAPABILITIES_CACHE_SECONDS`): `mock:false`, protocol,
   scope, types, `max_questions`, `max_topics` — refusal creates nothing;
4. **reserve** (short transaction, FOR SHARE on user/course/enrollment): idempotency again,
   topics of the course (422 `invalid_topics`, nothing consumed), recover this student's
   expired session, reserve one quota unit (atomic upsert), insert `generating`;
5. one upstream request (no retry) and strict validation of the whole response;
6. **persist** (short transaction): re-authorize with locks, lock the session FOR UPDATE and
   check token, then the lease against the database wall clock (`clock_timestamp()`, after
   the lock was acquired), lock and verify every citation, insert, finalize with an UPDATE
   that repeats status + token + lease, build the response, check the deadline, commit.

**Deadlines.** Steps 3–5 run under the absolute `QUIZ_PIPELINE_DEADLINE_SECONDS` (AnyIO).
Step 6 runs in a worker thread that AnyIO cannot interrupt, so it carries the same deadline as
a monotonic instant: every lock wait and statement is bounded by the time left
(`SET LOCAL lock_timeout/statement_timeout`, never more than 2 s / 5 s), and the deadline is
checked after each database step and immediately before COMMIT. Expiry before COMMIT rolls
everything back and fails the session `deadline` (504). **After COMMIT has been issued no
deadline applies and nothing can undo it:** the quiz is `ready`, the response is delivered or
replayed as ready, and a later cancellation or timeout never marks it failed (the failure
update is fenced on the attempt token, which finalization clears). Not bounded by any deadline:
COMMIT itself (stalled network, synchronous replica), waiting for a pool connection, and the
delivery of the response to the client. A COMMIT whose outcome is unknown is resolved from the
database: the fenced failure update is a no-op if the commit happened, and the persisted quiz is
returned. No transaction is open during a network call.

**Reservation identity.** The attempt token is chosen before step 4. A reservation that commits
but whose result never reaches the request (deadline or cancellation while the worker thread
returns, or a lost connection) is resolved by `(student, Idempotency-Key, token)`: failed
`deadline` / `cancelled` / `server_error`, quota consumed once, never overwriting a committed
outcome. The outcome reported is always the one the database holds: a completion that raced a
lease recovery is reported as `ready`.

### Quota

`QUIZ_DAILY_GENERATION_QUOTA` generations per student per UTC day, counted in
`quiz_generation_quotas` and reserved in step 4 in the same transaction as the session.
**Consumes a unit:** every reservation, whatever happens next (ready, rejected, timed out,
cancelled, abandoned, access lost). **Does not:** anything refused before step 4
(validation, authorization, switches, busy, AI unusable, idempotency conflict), any replay,
and a reservation rolled back in step 4 itself. Rows older than
`QUIZ_QUOTA_RETENTION_DAYS` are removed by the recovery task.

## GET /api/v1/courses/{course_id}/quizzes/{quiz_id}

The caller's own quiz in this course (404 `quiz_not_found` otherwise — another student's
quiz, another course's path). Same body as above, in any state: `generating` (no
questions yet), `ready`, or `failed` (with `failure`). Answered questions carry `attempt`
(the feedback object below, without `replayed`). Needs only `QUIZ_ENABLED`; never contacts
the AI service. This is how a client recovers a quiz whose POST response was lost.

## POST /api/v1/courses/{course_id}/attempts

```json
{"quiz_id": "<uuid>", "question_id": "<uuid>", "answer": "<choice id>", "hint_count": 0, "response_ms": 5400}
```

Exactly these fields. `answer`: a choice id of that question (1..16 printable ASCII);
`hint_count`: 0 or 1 (M7 shows one hint per question); `response_ms`: 0..3 600 000 or
null. `hint_count` and `response_ms` are stored as **untrusted telemetry**: they never
influence grading or the baseline.

| Situation | Answer |
|---|---|
| first valid answer | 201, graded, `replayed: false` |
| same answer again (retry) | 200, the stored result, `replayed: true`; counters unchanged; the first submission's telemetry is kept |
| different answer after an answer | 409 `already_answered` (+ `attempt_id`), nothing changes |
| not one of the question's choice ids | 422 `invalid_answer`, nothing stored |
| unsupported question type | 422 `unsupported_question_type` |
| another student's question, an unassigned or exam question, another quiz or course | 404 `question_not_found` (all the same) |

```json
{
  "attempt_id": "<uuid>", "quiz_id": "<uuid>", "question_id": "<uuid>", "answer": "b", "correct": false,
  "correct_answer": {"choice_id": "c", "text": "..."}, "explanation": "...",
  "citations": [{"chunk_id": "<uuid>", "origin": "class", "title": "Lecture 3.pdf", "locator": "Page 4",
                 "url": null, "snippet": "..."}],
  "citations_omitted": 0, "hint_count": 1, "response_ms": 5400, "submitted_at": "...Z",
  "mastery": {"kind": "baseline", "method": "cumulative_accuracy/1", "topic_id": "<uuid>",
              "topic_name": "Stoichiometry", "before": 1.0, "after": 0.5, "attempts": 2, "correct": 1,
              "trend": "down", "next_review_at": null},
  "replayed": false
}
```

Grading is deterministic: the submitted choice id against the persisted answer key
(`choice_id/1`); the database re-checks it on insert. One transaction holds: re-authorization
(FOR SHARE), the assignment row (FOR UPDATE — serializes simultaneous submissions; UNIQUE
`quiz_item_id` is the backstop), the statistics upsert, the append-only attempt with its
feedback snapshot. They commit together or not at all.

### Baseline (`mastery`)

`kind: "baseline"`, `method: "cumulative_accuracy/1"` = correct / attempts over the
student's committed, graded M7 attempts on that topic (`topic_practice_stats`; legacy
attempts are not counted). It is **a deterministic baseline, not an AI mastery
prediction**, and is never written to `mastery_scores`. `before` is the value before this
attempt (`null` when there was none), `after` the value including it, both derived from
the serialized counters returned by the upsert. `trend`: `first` (no earlier attempt), else
`up` / `down` / `steady` comparing `after` with `before`. `next_review_at` is always `null`.
The values are stored with the attempt, so a replay shows the original numbers.

### Citations and feedback visibility

The question's citations were verified at generation (provenance only: the quoted passage
exists in an indexed class document of this course — **not** that it entails the answer).
The attempt stores the original evidence. Whenever feedback is delivered (first answer,
identical retry, GET), each citation is re-checked against the current records (document
still `indexed`, same course, same locator, quote still in the chunk). A citation that is no
longer visible is **omitted** and counted in `citations_omitted`; its title or passage is
never shown from an old snapshot. The recorded answer, correctness and baseline never change.

## Revocation and its limits

Access is re-checked from the database before the upstream request, inside the persistence
transaction (with FOR SHARE locks on user, course and enrollment, so a concurrent
deactivation or unenrollment waits for it), and in the same transaction that reads data for
any response. What cannot be prevented: a response already committed and being written to
the network when access is revoked is still delivered (the window is the time to send one
response). To narrow that window, every sensitive body (a fresh quiz, a replay, `GET`, and graded
feedback including its replay) is preceded by one more authorization in its own short read-only
transaction, opened after the data transaction ended, so no transaction spans the network
write. If access was lost in between the answer is 404/403 and no quiz content is sent; a quiz
or attempt that was already committed stays stored (an attempt is recorded, its feedback
withheld). Removing a student from a course keeps their quiz and attempt history (there is
deliberately no foreign key from quiz sessions to enrollments); they just cannot read or
answer it any more.

## Data model (revision 0006)

`quiz_sessions` (request, fingerprint, state, token/lease) · `quiz_items` (assignment of a
generated question to its own session and student at a position) · `questions` (generated
rows: `status = 'generated'`, complete metadata, immutable) · `attempts` (M7 rows:
append-only grading snapshot) · `topic_practice_stats` (baseline counters) ·
`quiz_generation_quotas`. Composite keys guarantee session/user/course, assignment/question/
purpose/course and attempt/assignment/user/question/topic/course consistency; an exam
question can never be assigned. Details and operator notes: [database-migrations.md](database-migrations.md).

## Logging

Only ids, internal codes and counts are logged (`quiz_generated`, `quiz_failed code=…
reason=…`, `quiz_attempt_graded`). Never answers, answer keys, stems, explanations or course
passages; the AI token never appears. An unexpected exception is logged as its class name and
PostgreSQL SQLSTATE only (`error_class=DataError sqlstate=22001`; `app/quiz/diagnostics.py`):
never `str(exc)`, never a traceback, because SQLAlchemy errors render the statement and its bound
parameters. The three quiz endpoints answer such errors themselves with the generic 500
`server_error` (raised `from None`), so the server's own "Exception in ASGI application"
traceback is not produced for them either. Celery's own logging of the maintenance task is
outside this and carries no quiz payload (it updates sessions by id only).

## Activation

Before `QUIZ_ENABLED=true` anywhere real:
1. The AI team implements `studygraph.quiz/1` (or an agreed revision) with `mock: false`,
   and the shared OpenAPI contract is updated in its own reviewed change.
2. **Question quality evaluation**: generated stems and hints can leak the answer
   semantically (paraphrase); the substring check in core-api is only a heuristic. AI-2
   metrics plus human sampling of real generated quizzes per course are required.
3. Measured p95 generation latency within `QUIZ_GENERATE_DEADLINE_SECONDS`; otherwise move
   to asynchronous generation (202 + GET polling) as a separately announced contract version.
4. CE-3 authentication in place (until then every request is 401).
5. A shared (Redis) concurrency limiter before running several API processes
   (`QUIZ_MAX_CONCURRENT` is per process; the per-student limit and the quota are database-wide).
6. Migrate to 0006 before starting the new workers.

`HARVEST_ENABLED`, `CHAT_ENABLED` and `INDEXING_ENABLED` stay false; nothing in M7 uses
a mock, makes live AI requests or bypasses authentication.
