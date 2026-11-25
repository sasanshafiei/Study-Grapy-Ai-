# AI indexing integration (M4) — CE-side proposal "studygraph.index/1"

Owner: CE-1. Audience: AI-1 (implementer of the AI side), CE/AI reviewers.

> **PROPOSED.** The shared release candidate describes CE-1’s tested wire profile.
> Revision 0009 supplies CE’s persistence boundary (§8). Keep `INDEXING_ENABLED=false` pending
> joint approval, deployment grants and verification with a real AI-1 provider. [Provider handoff](ai-provider-handoff.md)

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
               verify stored digest + committed generation/chunks ─► job succeeded + document indexed
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
is a positive integer. The fixture provider reports `mock: true` on this endpoint.
CE refuses the probe before claiming a job or consuming an attempt. CE caches each probe result
(ready or not) for `AI_PROBE_CACHE_SECONDS` (30 s) per worker process, so an unavailable
service is asked at most once per period per process.

## 3a. Locator schemes (M6)

Backward-compatible extension, still `studygraph.index/1`. The capabilities object may
carry one optional key:

```json
{"protocol": "studygraph.index/1", "service": "ai-service", "mock": false,
 "completion": ["sync"], "max_request_bytes": 16777216, "locators": ["page", "section"]}
```

- Absent `locators` (every M4 service) means `["page"]`. If present it must be a list of
  distinct strings from `page`, `section` and must contain `page`; anything else makes
  the service not ready. Every other extra key is still refused.
- CE sends a **section-based** document only to a service that advertised `"section"`.
  Otherwise the external index job is deferred before it is claimed
  (`EXTERNAL_INDEX_DEFER_SECONDS`): no attempt is consumed, and since dispatch orders by
  `available_at`, deferred jobs never hold back page-based (PDF) jobs. CE re-checks the
  built request's scheme against the same capabilities after claiming and never sends a
  section document to a service that did not advertise it.
- Page-based documents (every class upload) are unchanged: same keys, same canonical
  bytes, same `content_digest` and `Idempotency-Key` as in M4.

A section-based request differs only in the document object and the section locators:

```json
{"protocol": "studygraph.index/1",
 "document": {"id": "…", "course_id": "…", "origin": "external", "kind": "article",
   "title": "Chemical equilibrium", "url": "https://en.wikipedia.org/wiki/Chemical_equilibrium",
   "licence": "CC BY-SA 4.0", "language": "en", "source_sha256": "<normalized content sha256>",
   "page_count": null, "locator_scheme": "section",
   "attribution": {"provider": "wikipedia", "page_id": 5821, "revision_id": 1234567,
     "revision_url": "https://en.wikipedia.org/w/index.php?oldid=1234567",
     "canonical_url": "https://en.wikipedia.org/wiki/Chemical_equilibrium",
     "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
     "attribution": "\"Chemical equilibrium\", Wikipedia (en), by Wikipedia contributors …"}},
 "sections": [
   {"ordinal": 0, "locator": {"anchor": null, "heading": null, "level": 0}, "text": "…"},
   {"ordinal": 1, "locator": {"anchor": "History", "heading": "History", "level": 2}, "text": "…"}],
 "content_digest": "…", "idempotency_key": "…"}
```

OpenStax documents (WP-6) have `"kind": "textbook"`, `"licence": "CC BY-NC-SA 4.0"` and an attribution with
`"provider": "openstax"`, `"page_id": 68664` (the digits of OpenStax module id `m68664`), `"revision_id": null`
and `"revision_key"`: the git blob SHA-1 of the module file. Wikipedia attributions are unchanged (no
`revision_key` key, so their bytes, digests and idempotency keys are as before).

Locators: level 0 is the lead (anchor and heading `null`), levels 2..6 carry the
provider's section anchor and heading. There are no page numbers. The response is the
§4 response, unchanged. **AI-side obligations before advertising `"section"`:** persist
chunk locators that M5 can display (`{"section": "<heading, ≤ 40 characters>"}` or an
agreed mapping), keep `attribution` with the document and surface it with any excerpt,
and filter retrieval by course id, origin, document status `indexed` and enabled source
(docs/external-harvesting.md §8). Each new harvested *version* is a new document id with
its own key; the AI side never receives a changed body for an existing document id.

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
| 200, valid, echoes match | verify PostgreSQL | publish only after matching source, generation and chunks; missing proof retries |
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
3. **Concurrent duplicates.** The write function serializes calls on the document row.
   A concurrent caller can wait for the commit and replay. A provider that bounds the
   wait can return `409 in_progress` (`retryable: true`).
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
what is stored *now* and checks the committed derived data under §8 before marking the document `indexed`. A different digest (content changed while indexing) fails the job
and the document (`index_content_changed`); nothing is re-sent automatically. Remote
deadlines are per attempt and start with the request; they never derive from the job's
`created_at` (jobs may wait a long time while indexing is disabled).

## 7. Course isolation

- `course_id` is part of the digest and the key: identical text in two courses gives
  two independent requests, generations and chunk sets.
- Every chunk row stores the request's `course_id`.
- The retrieval role uses the following function for dense, sparse, hybrid, topic and
  citation reads. CE supplies the authorized course and origins for the request.

  ```sql
  SELECT * FROM public.retrieve_index_chunks(:course_id, :allowed_origins)
  ```

  The function checks the committed generation, course, origin, publication state and
  source visibility in one query (§8). After a lost response the chunks stay hidden
  until CE recovers the job and verifies the committed data.
- CE validates that the confirmation echoes the request's `course_id`; a confirmation
  for another course is refused.

## 8. Durable PostgreSQL handoff (revision 0009)

CE-1 supplies the migration, the restricted SQL operations and the publication check. AI-1 supplies the actual chunks, embeddings, tags and model provenance. This allocation is **proposed pending joint approval**. The execution plan describes asynchronous jobs, shared external chunks linked by topic, and broader direct AI table grants. This first-release boundary uses a synchronous committed receipt inside the existing CE job, course-local derived rows, and separate write and retrieval operations. The F02 HTTP body and headers stay at `studygraph.index/1`.

### AI-1 write and replay

After validating the F02 request, compute the derived rows outside the database transaction. In a short PostgreSQL transaction at READ COMMITTED, issue the following parameterized call:

```sql
SELECT chunk_count, replayed
FROM public.commit_index_generation(
    :document_id, :course_id, :idempotency_key, :content_digest, :section_count,
    :embedding_model, :embedding_version, CAST(:chunks AS jsonb)
);
```

- `chunks` is a JSON array. Each object carries integer `section_ordinal`, nonblank `text`, object `locator`, and `embedding` as an array of 384 finite numbers with nonzero norm. Optional `topic_ids` is an array of distinct UUIDs belonging to this course. PostgreSQL assigns chunk IDs and derives document, course and origin from the locked document. Tagging may be omitted.
- Every source section needs at least one chunk. Each chunk's text is a contiguous excerpt of that section. Overlap and multiple chunks per section are allowed. Split at section boundaries. For physical pages copy the section locator exactly. For section-based versions use `{"section": label}`. Start with the heading, or `Introduction` for a lead. Replace each POSIX control character with a space, trim ASCII spaces at both ends and take the first 40 characters. The source ordinal retains the exact location when headings coincide. These are proposed **database inputs** for AI-1; the F02 receipt carries neither ordinals nor model identifiers.
- Model name and version use 1–100 ASCII characters and the alphabet in the migration. CE verifies presence and vector storage; model choice, consistent query-vector space and embedding quality need AI-1 evaluation. `committed_at` is a database-clock timestamp assigned within the write transaction. Visibility to a different transaction establishes commit.
- The first writer locks the document, checks the course and derived key, requires a pending document with live CE index work, and locks its source sections. One statement writes the generation, all chunks and topic links. Database constraints and the evidence check run before the statement succeeds. **Commit the transaction before sending HTTP 200.** A failure during any insert rolls back the entire call.
- For a cheap replay lookup pass SQL NULL for chunks, model and version, with the same five identity arguments. No row means there is no generation. End that short transaction before computing embeddings. A matching row returns original counts with `replayed=true`; reconstruct the F02 receipt from the validated request and the returned count. Simultaneous first writers serialize on the document. A caller that lost the commit acknowledgement reconnects and uses this same lookup. A committed generation with another key yields `generation_conflict`; a mismatched course yields `course_mismatch`. Neither path replaces data.
- Map identity conflicts to the documented permanent 409, malformed derived inputs to a bounded permanent error, and a temporary lock, serialization failure or unavailable live claim to a retryable error. Return the F02 error envelope with safe messages. Keep raw driver diagnostics, vectors, passages and credentials out of HTTP and logs.

### CE publication and retrieval

The provider transaction and CE publication are separate commits. Between them a generation exists and remains invisible to the retriever. CE locks the relevant course settings, source, external resource, current job and document in its established order. It verifies current routing, pending status, active provenance, the attempt token and the canonical digest of the locked source. It calls `verified_index_generation` using the rebuilt identity and the HTTP chunk count. The query counts **all** chunks of the document, including unexpected rows. It checks agreement with the generation's course, key, digest, both counts, source ordinals, exact locators, text excerpts, origin and nonzero vectors. Composite keys constrain new topic associations. The restricted writer cannot add, update or delete derived rows after its first commit.

Missing or inconsistent proof returns `index_proof_missing` through the existing bounded retry/backoff path. A stale token or expired lease cannot publish. The successful job update checks the database clock and retains its row lock through commit. The external version switch, document status and job success commit together. A crash before that commit leaves work for CE lease recovery; a later provider replay supplies the original receipt and CE verifies the database again.

AI-1 reads through `public.retrieve_index_chunks(course_uuid, allowed_origins_text_array)` using its separate retrieval identity. It returns published, generation-bound chunks, their vectors, model identity and course-local topic IDs. Supply the course and origin list authorized by CE for this request. The database enforces the supplied scope. End-user authorization still belongs to CE. The function excludes disabled sources and stale external versions. The prior indexed version remains visible while a replacement is pending. Course harvest opt-out stops new publication and keeps previously published material citable under the existing M6 policy.

### Privilege and deployment handoff

| Identity | Privileges for this boundary |
| --- | --- |
| Migration owner | Trusted schema and routine owner. Runs Alembic and grants. Keep owner membership and schema creation away from service logins. |
| CE runtime | Existing CE lifecycle, source reads and row-lock permissions, plus SELECT on generations and EXECUTE on the invoker-rights verifier. CE-1 owns publication. |
| `studygraph_ai_indexer` | EXECUTE on the insert-once function. It can replay its known request identity. |
| `studygraph_ai_retriever` | EXECUTE on the course- and origin-scoped retrieval function. |

CE-3 provisions distinct non-owner login identities and the three fresh unprivileged groups named in the grant script. Use separate credentials/pools, private transport, timeouts and secret rotation. Service identities must lack superuser, role creation, database creation, replication, bypass-RLS, schema CREATE, and inherited table or owner grants. Audit effective PUBLIC grants and role memberships on the target, including older PostgreSQL deployments. The two definer-rights entry points use qualified objects and a fixed search path. The migration revokes PUBLIC execution within the DDL transaction.

The F03 grants are part of the complete F05 grant script, applied as the owner in one transaction after every migration: `psql -X --single-transaction -v ON_ERROR_STOP=1 -f services/core-api/scripts/db_privileges.sql`, after the cluster groups from `db_roles.sql`. Roles, rollout and rollback: [database-security.md](database-security.md).

### Existing data and rollout

Keep indexing disabled. Inventory indexed document IDs/status, existing chunks, null/mismatched courses, missing vectors, topic associations and active jobs on the target. Take a restorable backup and rehearse the upgrade with representative data. Drain older workers before the maintenance window. The migration needs exclusive locks while it extends the derived tables and withdraws old publication.

Revision 0009 retains legacy chunks, topic links, source and job history. Legacy chunks have a null generation key. Previously indexed documents move to `failed` for review. Their original bytes remain, and no generation is synthesized. A document with legacy chunks cannot acquire a new generation under the same ID. For those documents, preserve archived evidence and use a reviewed replacement/import or retention procedure. Keep the quarantine when a safe replacement is unavailable. The normal retry-index rules still refuse a document with a historical successful index job. A failed document with stored sections, no legacy chunks and eligible job history can follow the normal retry process. No reindex runs during upgrade.

Downgrade refuses while any generation exists. A schema-only downgrade retains the quarantine and the legacy data. It does not republish documents. Restore the reviewed backup if the original publication state is required. CE-3 validates grants with real deployment identities; AI-1 demonstrates a real commit, restart/replay, isolated retrieval and model compatibility in staging before any activation decision.

### Hybrid search

The migrations and development catalog have a pgvector column of dimension 384. Neither defines a generated `tsvector` column or a GIN text-search index. PostgreSQL can compute `to_tsvector` over the permitted chunk text at query time. A materialized or indexed sparse search path will need additional DDL once AI-1 and CE agree on language configuration, ranking and measured workload. Revision 0009 adds a document/generation B-tree for integrity and verification; ranking and search indexes are outside F03.

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
- The public API reports `ready` after CE verifies the receipt, current source digest
  and committed chunks. Parsed documents awaiting publication show `processing`.
  Index failures show `failed` with a public message.

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

1. Joint approval of the release candidate in `contracts/ai-service.openapi.yaml`.
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
misbehaving peers. Worker integration tests use the restricted SQL writer on disposable PostgreSQL with deterministic test vectors. HTTP-only protocol tests also use an in-memory double.
The tests show that CE-1's worker, client, state machine and recovery behave as
specified; **no real AI indexing has been performed or verified.** The repository's
mock `services/ai-service` is refused by the readiness gate and must never mark a
document indexed.
