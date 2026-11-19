# AI indexing integration (M4) — CE-side proposal "studygraph.index/1"

Owner: CE-1. Audience: AI-1 (implementer of the AI side), CE/AI reviewers.

> **Status: proposal.** This is the interface CE-1 implemented and tests against
> isolated test doubles. It is **not** part of the shared contract:
> `contracts/ai-service.openapi.yaml` is unchanged and changes only with one CE and one
> AI approval. Nothing here has been confirmed by AI-1. Open decisions are listed at
> the end. `INDEXING_ENABLED` is `false` by default and must stay so until AI-1 ships a
> compatible, non-mock implementation.

## 1. Responsibilities and ownership

| Concern | Owner | Notes |
| --- | --- | --- |
| Database schema, migrations, grants (all tables, incl. chunk tables) | CE-1 | Alembic in `services/core-api/migrations`; AI-1 requests changes |
| Document lifecycle (`documents.status`, `ingestion_jobs`) | CE-1 | the AI side never writes these tables |
| PDF parsing, normalized `document_sections` | CE-1 | M3 |
| When to index, retries, backoff, leases, fencing | CE-1 | the M3 job state machine (`app/ingestion/jobs.py`) |
| Chunking, embedding, topic tagging | AI-1 | not implemented by CE-1 |
| Persisting chunks, embeddings and the index generation record | AI-1 | in the shared PostgreSQL, through a least-privilege role (§6) |
| Retrieval scoped by course | AI-1 | every query filters by `course_id` (§7) |

Synchronous HTTP is the initial protocol. Asynchrony on the CE side comes from the
Celery index worker: the user's upload never waits for indexing.

```
parse completes ─► queued index job (M3 hand-off, same transaction as the sections)
dispatch sweep (Beat, maintenance worker) ─► index queue         only while INDEXING_ENABLED
index worker:  indexing enabled? ─► GET capabilities (cached) ─► claim (attempt +1)
               ─► build canonical request from documents + document_sections
               ─► POST /ai/v1/index/documents  (Idempotency-Key, bearer token)
AI service:    validate ─► chunk + embed ─► ONE transaction: chunks + generation record
               ─► COMMIT ─► 200 {status: "indexed", echoes}
index worker:  validate echoes ─► one transaction, fenced by the attempt token:
               re-verify the stored content digest ─► job succeeded + document indexed
```

## 2. Transport and authentication

- Base URL: `AI_SERVICE_URL` (http or https, no credentials, query or fragment).
  Internal network only. Use https whenever traffic leaves a single Docker host.
- Every request carries `Authorization: Bearer <AI_SERVICE_TOKEN>`. The token is a
  random secret of at least 32 characters, provisioned from the secret store to both
  services (never in git, logs or URLs). The AI side compares it in constant time and
  should accept two tokens during rotation. Missing or wrong token: `401` with the
  error body (§5). CE treats 401/403 as "not ready" (no further claims in that worker
  process until the next probe) and never as a document problem.
- Every request carries `X-StudyGraph-Protocol: studygraph.index/1`.
- CE's client follows no redirects, uses no environment proxies, opens one connection
  per request and closes it (stdlib `http.client`, `app/indexing/client.py`).

## 3. `GET /ai/v1/index/capabilities` (readiness)

Authenticated. Must answer quickly (CE inactivity timeout: 3 s, see §9 for what that does and
does not bound) and must not do work. CE probes it inside the index task, when a job is
about to be claimed, never at worker start-up.

```json
{"protocol": "studygraph.index/1", "service": "ai-service", "mock": false,
 "completion": ["sync"], "max_request_bytes": 16777216}
```

CE requires exactly these keys; `mock` must be the boolean `false` (absent, `true` or
any other value is refused); `completion` must contain `"sync"`; `max_request_bytes`
is a positive integer. Anything else — including the repository's current mock, which
has no such endpoint and reports `"mock": true` on `/health` — makes the service *not
ready*: **no job is claimed and no attempt is consumed**. CE caches each probe result
(ready or not) for `AI_PROBE_CACHE_SECONDS` (30 s) per worker process, so an unavailable
service is asked at most once per period per process.

## 4. `POST /ai/v1/index/documents`

Headers: `Content-Type: application/json`, `Idempotency-Key: <64 hex>`, the bearer
token, the protocol header.

### Request body (canonical JSON)

```json
{
  "protocol": "studygraph.index/1",
  "document": {
    "id": "<uuid>", "course_id": "<uuid>", "origin": "class",
    "kind": "notes", "title": "Lecture 4.pdf", "url": null, "licence": null,
    "language": "en", "source_sha256": "<64 hex or null>", "page_count": 12
  },
  "sections": [
    {"ordinal": 0, "locator": {"page": 1}, "text": "..."},
    {"ordinal": 1, "locator": {"page": 3}, "text": "..."}
  ],
  "content_digest": "<64 hex>",
  "idempotency_key": "<64 hex>"
}
```

- Canonical form: keys sorted, separators `,` and `:` without spaces, UTF-8 without
  ASCII escaping, no NaN/Infinity. The same stored content always yields the same bytes.
- `document.course_id` is always present: documents without a course are never sent.
- `sections`: at least one; `ordinal` contiguous from 0; `locator` exactly
  `{"page": n}`, 1-based physical page, strictly increasing, ≤ `page_count`; `text`
  non-blank, normalized by CE (see `docs/document-processing.md`). Chunks must carry
  the locator of the section(s) they come from and reference `(document_id, page)`,
  never section ids (they change when a document is reparsed).
- `content_digest = SHA-256(canonical({"protocol", "document", "sections"}))`.
- `idempotency_key = SHA-256("studygraph.index/1" NUL document.id NUL document.course_id NUL content_digest)`.
  Both are pure functions of content and identity (no job id, attempt or time), so all
  attempts of a job, and a later `retry-index` of unchanged content, send the same key.
  The AI side should recompute both and refuse a mismatch (`400 inconsistent_request`).
- Size: CE sends at most `min(INDEX_MAX_PAYLOAD_BYTES (16 MiB), max_request_bytes)`;
  larger documents fail permanently on the CE side without a request.

### Success: `200 OK`, only after the commit (§6)

```json
{"protocol": "studygraph.index/1", "status": "indexed",
 "document_id": "<uuid>", "course_id": "<uuid>",
 "content_digest": "<64 hex>", "idempotency_key": "<64 hex>",
 "section_count": 2, "chunk_count": 7, "replayed": false}
```

CE validates strictly: complete, unambiguous HTTP framing (exactly one numeric
`Content-Length` that the body fills, for every body-bearing response, error bodies
included. Any `Transfer-Encoding` (chunked too), repeated or non-numeric lengths and
close-delimited bodies are refused, so a truncated body is never mistaken for a
confirmation; the AI service must therefore answer with `Content-Length`, not chunked), `application/json`, no repeated key in any
JSON object, exactly these keys, exact types (a boolean
is not an integer), every echoed value equal to the request, `chunk_count ≥ 1`. Any
deviation is *incompatible*: the attempt is retried later and the worker stops
claiming until the next probe; the document is never marked indexed. `replayed: true`
when the key had already been committed (§6).

`202 Accepted` is **never** success. With the sync protocol CE treats it as a transient
failure and replays the same key later (an idempotent service then answers `200`). An
asynchronous completion protocol is an open decision (§10); CE keeps it behind an
adapter boundary (`CompletionAdapter` in `app/indexing/pipeline.py`).

## 5. Errors and CE's reaction

Error body for every non-2xx answer:
`{"error": {"code": "<snake_case>", "message": "<text>", "retryable": <bool>}}`.

| Answer | CE classification | Effect |
| --- | --- | --- |
| 200, valid, echoes match | confirmed | complete (digest re-verified), document `indexed` |
| 200 invalid / other 2xx / 3xx / 4xx without the error body / 404 / 405 | incompatible | attempt retried later; worker stops claiming until the next probe |
| 202 | not confirmed | attempt retried later with the same key |
| 400, 409, 422 with error body, `retryable: false` | rejected (permanent) | job and document fail (`index_rejected`); operator may `retry-index` |
| 413 with error body, `retryable: false` | too large (permanent) | job and document fail (`index_payload_too_large`) |
| 401, 403 | auth refused | attempt retried later; worker stops claiming until the next probe |
| 408, 409, 425, 429, 5xx, or error body with `retryable: true` | transient | retried with backoff, same key |
| connect failure (nothing sent) | unavailable | attempt retried; worker stops claiming until the next probe |
| timeout / connection lost after sending | outcome unknown | retried with the same key (lost response) |
| response over 64 KiB | transient | retried |

Retries: at most `max_attempts` (3) per job, counted at claim and never decremented;
backoff `30 s × 2^(n−1)` ±20 %, capped at 600 s; then the job fails as exhausted and the
document shows "Preparing this document for search failed repeatedly…". An attempt
already claimed when the service turns out to be incompatible counts; the readiness
gate makes that at most one attempt per worker process per probe period.

Codes CE expects AI-1 to use (proposal): `invalid_request`, `inconsistent_request`,
`unsupported_language`, `payload_too_large`, `generation_conflict`, `course_mismatch`,
`in_progress` (retryable), `overloaded` (retryable), `unauthorized`.

## 6. Idempotency, durability and what the AI side must guarantee

Local fencing (attempt tokens, leases, `SKIP LOCKED`) guarantees that **CE** records at
most one outcome per attempt and never lets a stale attempt write. It does **not**
make the remote call idempotent: CE may send the same request more than once
(duplicate broker deliveries are absorbed locally, but lost responses, timeouts, worker
crashes after sending and lease recovery all cause replays). AI-1 must guarantee:

1. **Durable completion.** Return `200` only after chunks, embeddings and the
   generation record are committed in one transaction. Never `200` before the commit.
2. **Replay.** A request whose `idempotency_key` is already committed returns the same
   `200` body with `replayed: true` and writes nothing.
3. **Concurrent duplicates.** Two requests with the same key at the same time must not
   both write: serialize on the key (`pg_advisory_xact_lock(hashtext(key))` or a
   unique constraint on the generation record). The loser either waits and replays or
   answers `409 in_progress` (`retryable: true`).
4. **Lost responses.** A request whose response was lost is replayed by CE with the
   same key and body; rule 2 applies.
5. **Stale generations.** A document has at most one committed generation. A request
   for a document that already has a committed generation with a *different* key is
   refused with `409 generation_conflict` (`retryable: false`), so a delayed or
   duplicated old request can never overwrite newer data, and CE never replaces a
   generation implicitly. (Re-indexing changed content needs an explicit replace or
   purge operation: open decision.)
6. **Course isolation.** A `document.id` committed under one course is never accepted
   under another (`409 course_mismatch`, `retryable: false`).
7. **No partial state.** A failed request leaves no chunks behind.

CE's side: completion is one transaction fenced by the attempt token; it locks the
document, reads its sections `FOR SHARE`, recomputes the canonical content digest from
what is stored *now* and marks the document `indexed` only if it equals the digest the
AI service confirmed. A different digest (content changed while indexing) fails the job
and the document (`index_content_changed`); nothing is re-sent automatically. Remote
deadlines are per attempt and start with the request; they never derive from the job's
`created_at` (jobs may wait a long time while indexing is disabled).

## 7. Course isolation

- `course_id` is part of the digest and the key: identical text in two courses gives
  two independent requests, generations and chunk sets.
- Every chunk row stores the request's `course_id`.
- **Retrieval MUST filter by BOTH course identity AND document status**, in the same
  query, for every read of `chunks` (dense, sparse, hybrid, topic or citation lookups):

  ```sql
  FROM chunks c JOIN documents d ON d.id = c.document_id
  WHERE c.course_id = :course_id AND d.course_id = :course_id AND d.status = 'indexed'
  ```

  The course filter keeps one course's material out of another's answers. The status
  filter is what makes a generation *visible*: the AI side commits chunks first, and CE
  marks the document `indexed` only after validating the confirmation and re-verifying
  the content digest. Until then (and forever after a lost completion, `index_content_changed`,
  a failed or `stale` document) the chunks exist but must never be retrieved. Neither
  filter substitutes for the other, and a caller-supplied course id is only trusted after
  the enrolment check in core-api. §8 makes the database enforce what it can.
- CE validates that the confirmation echoes the request's `course_id`; a confirmation
  for another course is refused.

## 8. Persistence in the shared PostgreSQL (proposal)

CE-1 keeps schema ownership; AI-1 writes only its own derived data through a dedicated
role. Proposed for the **next CE migration after AI-1 agrees** (M4 adds no migration):

- Role `studygraph_ai_indexer`: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS`, `CONNECTION LIMIT` small, `statement_timeout` and
  `idle_in_transaction_session_timeout` set on the role, its own password from the
  secret store. No ownership of any object; no DDL.
- Grants of the indexer: `SELECT, INSERT, DELETE` on `chunks` and `chunk_topics`;
  `SELECT, INSERT` on a new `index_generations`; `SELECT (id, course_id)` on `documents`
  (to check that the document exists and belongs to the request's course). **No** access to
  `users`, `enrollments`, `ingestion_jobs`, `document_sections` or uploads, no `UPDATE` on
  `documents` and **no way to set `documents.status`**: only CE marks a document `indexed`.
- Retrieval needs the same two filters as §7, so it gets its own read-only role
  `studygraph_ai_retriever` (same role attributes; it may be the same service with a second
  connection pool), never the indexer's write role: `SELECT` on `chunks` and `chunk_topics`
  plus `SELECT (id, course_id, status)` on `documents`, nothing else. Preferred: a CE-owned
  view `retrievable_chunks` (`security_barrier`) that joins `chunks` to `documents` with
  `c.course_id = d.course_id AND d.status = 'indexed'` already applied, with the retriever
  granted `SELECT` on the view only and nothing on the base tables. Callers then add
  `WHERE course_id = :course_id`; with the view, forgetting the status filter is impossible.
  Without the view, the §7 query is a mandatory contract and is covered by an AI-side test.
- `index_generations` (CE migration): `document_id` (`UNIQUE`: one committed generation per
  document), `course_id`, `idempotency_key` (`UNIQUE`, 64 hex), `content_digest`,
  `section_count`, `chunk_count`, `committed_at`; **composite foreign key
  `(document_id, course_id) REFERENCES documents (id, course_id) ON DELETE CASCADE`**
  (needs `UNIQUE (id, course_id)` on `documents`, redundant with the primary key but required
  as a foreign-key target), so a generation can never name a course other than its
  document's. The AI side inserts it in the same transaction as the chunks.
- `chunks` (same migration): `course_id` becomes `NOT NULL` for indexed material, with the
  same composite foreign key `(document_id, course_id) REFERENCES documents (id, course_id)
  ON DELETE CASCADE` and a reference to the generation (`document_id, idempotency_key`
  → `index_generations`). A chunk therefore cannot carry a course different from its
  document's, and a retrieval filtered by `c.course_id` cannot leak a document that
  belongs to another course. (Today a document with chunks could not be deleted: the cascade
  fixes that too.)
- CE side, proposed hardening of `complete_index`: in its existing transaction, also require
  that an `index_generations` row exists for `(document_id, confirmed key, confirmed digest)`
  before setting `documents.status = 'indexed'`. Leaving `indexed` (reparse, `stale`,
  failure) hides the chunks at once through the status filter; deleting them is the explicit
  purge operation (open decision 6), never implicit.
- Embedding dimension stays 384 (`app/models.py`) unless AI-1 asks otherwise.

## 9. CE implementation summary (M4)

- `app/indexing/payload.py`: canonical request, digest and key, strict validation.
- `app/indexing/client.py`: bounded stdlib HTTP client, readiness probe and cache,
  strict response validation, outcome classification.
- `app/indexing/pipeline.py`: one index attempt; the 202 adapter boundary.
- `app/ingestion/jobs.py`: `claim_index`, `complete_index`, `retry_index`; the M3
  claim, fencing, failure, backoff and recovery code shared by both kinds.
- `app/ingestion/dispatch.py`: `sweep_index` (only while enabled) on the same
  reservation rules; `app/worker.py`: the index task; index dispatch and recovery run
  inside the existing maintenance tasks (no new Beat entries).
- Worker roles (`app/ingestion/runtime.py`): a worker consuming exactly the index
  queue runs the index preflight (database verified as in M3, `AI_SERVICE_URL`
  validated, `AI_SERVICE_TOKEN` required while enabled; **no network call to the AI
  service at start-up**, because a start-up probe would run outside every Celery time
  limit) and needs **no upload volume**; mixing the index queue with other queues is refused (exit 78).
- Compose: `worker-index` (`-Q ingestion.index`, no `uploads` volume,
  `INDEXING_ENABLED: "false"`). Operator: `python -m app.ingestion.admin retry-index
  --database NAME DOCUMENT_ID` (failed document with a succeeded parse and stored
  sections; no extraction; same key for unchanged content), and
  `preflight --role index`.
- Public API unchanged: `indexed` → `ready` only after a confirmed, digest-verified
  completion; parsed-but-not-indexed stays `processing`; index failures show `failed`
  with a public message.

### Time bounds, precisely

| Bound | Default | What it limits — and what it does not |
| --- | --- | --- |
| `AI_CONNECT_TIMEOUT_SECONDS` | 3 s | TCP connect + TLS handshake (not DNS resolution) |
| `AI_READ_TIMEOUT_SECONDS` | 60 s | each socket operation (sending, status line, **each header line**, each body read): inactivity, restarted by every byte; not a total |
| `AI_REQUEST_DEADLINE_SECONDS` | 150 s | checked only **between body reads, after the headers arrived**. It does not cover connecting, sending, `getresponse()` or the header phase: a peer that trickles header bytes can outlast it (a reviewer measured a 1.45 s call against a 0.2 s probe timeout) |
| `AI_PROBE_TIMEOUT_SECONDS` | 3 s | the readiness probe's connect and inactivity timeouts (and, as above, the same deadline check between body reads); **not a hard deadline** |
| index task soft / hard limit | 240 s / 270 s | Celery: soft raises in the task (recorded as transient); **hard kills the pool process — the only absolute wall-clock bound** |
| index lease | 270 + 120 s | a running attempt not finished by then is recovered |

Every network call to the AI service, the readiness probe included, runs inside an index
task and is therefore under those Celery limits; none runs at worker start-up or in the
dispatcher. The settings refuse `probe + deadline + read timeout ≥ soft limit`, `soft ≥
hard`, and a visibility timeout not above twice the hard limit: that is a sanity relation
for well-behaved peers, **not** a guarantee that a slow peer is cut off by then.

## 9a. Enabling real indexing later (not authorized now)

`INDEXING_ENABLED` is read independently by each process, and **two** worker deployments
must agree; `false` stays the value everywhere until AI-1 and the open decisions of §10
are settled and someone with authority approves activation.

| Deployment | Needs when enabled | If it alone is enabled |
| --- | --- | --- |
| dispatch: `worker-maintenance` (runs `dispatch_parse_jobs`, which calls `sweep_index`) | `INDEXING_ENABLED=true`, `INDEX_QUEUE` equal to the index worker's. No AI token, URL or upload access is needed: it never contacts the AI service | jobs are published to a queue nobody consumes, or consumed by workers that are disabled: they stay queued and are published again after `REDISPATCH_AFTER_SECONDS` (300 s) |
| index: `worker-index` (`-Q ingestion.index` only) | `INDEXING_ENABLED=true`, `AI_SERVICE_URL`, `AI_SERVICE_TOKEN` (≥ 32 chars, from the secret store), the same `INDEX_QUEUE`, the same task time limits and `JOB_LEASE_MARGIN_SECONDS` as the dispatcher's lease maths; no `uploads` volume | nothing is ever published to it: queued index jobs never start |

If only the dispatcher is enabled, a message for a still-disabled index worker is
acknowledged without a claim (`index_not_claimed reason=indexing_disabled`): no attempt is
consumed. Activate in this order: provision the token and the §8 database objects, start
`worker-index` enabled, check the log for `ai_not_ready` (a mock or incompatible service is
refused), then enable the dispatcher. Disable in the reverse order. Lease recovery of
running index attempts is not gated by the flag. The `ai-service` mock in compose is
refused by the readiness gate and must not be pointed at.

## 10. Open decisions (need AI-1 / CE agreement)

1. Approval of this wire format and its addition to `contracts/ai-service.openapi.yaml`.
2. Authentication: static bearer token (proposed) vs mTLS or signed short-lived tokens;
   rotation procedure.
3. Persistence: the role, grants and `index_generations` table of §8 (CE migration),
   and who runs embeddings at which throughput.
4. Payload limits: the AI side's `max_request_bytes`; whether very large documents need
   paging (several requests per generation).
5. Asynchronous completion: whether `202` + polling or a callback is needed for large
   documents, and its protocol. Until then sync only; `202` is never success.
6. Re-indexing / generation replacement (content changes, reparsing, re-uploads): an
   explicit replace or purge operation; not done implicitly by either side.
7. Error code catalogue and which codes are user-visible.

## 11. Test doubles vs real indexing

Every indexing test in this repository runs against **test doubles**:
`services/core-api/tests/fake_ai_service.py` (a standard-library HTTP server in the
test process implementing this proposal: idempotent replay, one committed generation per
document with `generation_conflict` and `course_mismatch` refusals (also under concurrent
requests with different keys), lost responses, blocking, scripted errors) and raw sockets for
misbehaving peers. The doubles store nothing in PostgreSQL and compute no embeddings.
The tests show that CE-1's worker, client, state machine and recovery behave as
specified; **no real AI indexing has been performed or verified.** The repository's
mock `services/ai-service` is refused by the readiness gate and must never mark a
document indexed.
