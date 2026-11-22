# AI chat integration (M5) — CE-side proposal "studygraph.chat/1"

**PROPOSED.** The shared release candidate describes the CE chat adapter.
`CHAT_ENABLED=false` by default. The packaged fixture provider identifies itself as a mock.
CE integration tests use an isolated raw-socket provider. Real retrieval and generation
require AI-1 implementation and joint approval. [Provider handoff](ai-provider-handoff.md)

Related: CE-2's web contract (`contracts/core-api.web.md` on the `ce2/web-app` branch,
§1 "`/courses/{id}/chat` streams server-sent events"), and the M4 proposal
[ai-indexing-integration.md](ai-indexing-integration.md), whose §7 (course isolation)
and §8 (least-privilege roles, `index_generations`, chunk constraints) are
**prerequisites for activating chat** (§10).

## 1. Who does what

| Concern | Owner |
|---|---|
| Public endpoint `POST /api/v1/courses/{id}/chat`, authentication, authorization, limits, SSE | CE-1 (core-api) |
| Capability check, request, strict response validation, citation and topic verification | CE-1 |
| Retrieval, generation, citation selection, topic tagging | AI-1 (ai-service) — not implemented |
| Retrieval permissions in PostgreSQL (roles, views) | CE-1 migration on AI-1's request (M4 §8) — not yet applied |

The AI service **proposes** an answer; core-api **decides** what a user sees.

## 2. Transport and authentication

- HTTP/1.1 from core-api to `AI_SERVICE_URL` (http or https; no credentials, query or
  fragment in the URL). Every request carries `Authorization: Bearer <AI_SERVICE_TOKEN>`
  (≥ 32 characters), `X-StudyGraph-Protocol: studygraph.chat/1`,
  `Accept: application/json`, `Accept-Encoding: identity`.
- The same token as indexing (M4). The token is never logged, echoed or placed in URLs.
- **Responses** must be `application/json` with exactly one numeric `Content-Length`
  that the body fills exactly. Refused: any `Transfer-Encoding` (chunked included),
  close-delimited bodies, any `Content-Encoding` other than identity, 3xx (redirects
  are never followed), bodies above the limit, JSON with a repeated object key,
  `NaN`/`Infinity`, invalid UTF-8.
- **HTTP framing is HTTPX/h11's, not a custom parser.** For `Content-Length`, h11
  follows RFC 9112 §6.3: a repeated header, or a comma-separated list, whose values are
  **all identical** (`Content-Length: 8` twice, `8, 8`) is normalized to the single
  length and accepted; **conflicting** values (`8` and `9`, `8, 9`), signs and
  non-digits are rejected as a protocol error (`transport_RemoteProtocolError`). The
  client's own check then insists on exactly one resulting value that the body fills
  exactly. Ambiguous framing that h11 does not reject (no length, close-delimited
  bodies, `Transfer-Encoding`), short or long bodies, and unsupported compression stay
  refused. Tests: `test_identical_duplicate_content_lengths_are_normalized_by_httpx`,
  `test_conflicting_or_malformed_content_lengths_are_rejected`.
- core-api uses `httpx.AsyncClient` with `trust_env=False` (no proxy or `.netrc` from
  the environment), `follow_redirects=False`, one connection per call, inactivity
  timeouts (`CHAT_CONNECT_TIMEOUT_SECONDS`, `CHAT_READ_TIMEOUT_SECONDS`) and an
  absolute deadline per call (`CHAT_PROBE_DEADLINE_SECONDS`,
  `CHAT_ANSWER_DEADLINE_SECONDS`), plus an overall deadline for the whole request.
- **No automatic retry.** An answer request is sent once; on any failure the user sees
  an error and may ask again. The AI side must therefore not depend on retries.

## 3. `GET /ai/v1/chat/capabilities`

```json
{"protocol": "studygraph.chat/1", "service": "ai-service", "mock": false,
 "scopes": ["class_only"], "max_question_chars": 4000}
```

Exactly these keys. core-api treats the service as **not ready** unless `mock` is the
boolean `false`, `protocol` and `service` match, `scopes` is a list of distinct known
scopes, and `max_question_chars` is a positive integer. A mock or incompatible service
never produces a successful answer. Results (ready or not) are cached per (URL, token
fingerprint) for `CHAT_CAPABILITIES_CACHE_SECONDS` (≤ 300 s), at most 4 entries.

## 4. `POST /ai/v1/chat/answer`

Request (`Content-Type: application/json`, with `Content-Length`):

```json
{"protocol": "studygraph.chat/1",
 "request_id": "5b0c…", "course_id": "8d2e…",
 "scope": "class_only", "allowed_origins": ["class"],
 "question": "What limits the yield?",
 "max_citations": 16, "max_answer_chars": 8000}
```

Success: `200`, exactly these keys:

```json
{"protocol": "studygraph.chat/1", "request_id": "5b0c…", "course_id": "8d2e…",
 "scope": "class_only", "mock": false, "grounded": true,
 "answer": "The limiting reactant caps the product [1]. Hence lower yields [1].",
 "citations": [{"marker": 1, "chunk_id": "c81f…", "document_id": "a3b2…",
                "origin": "class", "quote": "The limiting reactant limits the amount of product"}],
 "topic_ids": ["7e1d…"], "used_web": false}
```

Rules (any violation discards the whole answer; nothing is repaired or partly shown):

- `protocol`, `request_id`, `course_id`, `scope` echo the request; `mock` is `false`;
  `used_web` is `false` (no web search exists); `grounded` is a boolean.
- **Citations** (grounded answers): 1..`max_citations` objects with exactly
  `marker` (integer 1..max, unique), `chunk_id`, `document_id` (canonical lower-case
  UUIDs), `origin` (`class`, `external`, `web`, and permitted by the scope), `quote`
  (1..500 characters, copied verbatim from the chunk text). No titles, URLs, locators
  or snippets: core-api derives all public metadata from its own records (§6).
- **Markers**: the set of `[n]` markers in `answer` must equal the set of citation
  markers — every citation used, no marker without a citation. A marker may appear
  several times. The only valid spelling is `[n]`: ASCII digits, no sign, no leading
  zero, no spaces, no list or decimal, `n` between 1 and `max_citations`. Every
  bracket that holds only digits and number punctuation is treated as a marker, so
  `[12345]`, `[00001]`, `[0]`, `[+1]`, `[ 1 ]`, `[1,2]`, `[1.0]`, `[１]` (full-width) and
  an out-of-range `[17]` each reject the whole answer, even next to a valid `[1]`.
  Brackets with other content (`[H+]`, `[A]`, `x[i]`) are ordinary text.
- `answer` ≤ `max_answer_chars`, no control characters other than newline and tab and
  no unpaired UTF-16 surrogate (U+D800–U+DFFF). The same holds for citation quotes and
  for the user's question. A valid JSON surrogate pair (`\ud83d\ude00`) is one character
  and is fine; a lone half is not valid text and is rejected.
- `topic_ids`: distinct canonical UUIDs of topics **of this course**, at most
  `CHAT_MAX_TOPICS`.
- **`grounded: false`** means the permitted sources do not support an answer:
  `citations` must be `[]`. core-api ignores `answer` and shows its own message
  ("I could not find enough support in this course's materials …") with no citations.
  Do not guess.

Errors: any non-200 (the M4 error body `{"error": {"code", "message", "retryable"}}`
is welcome but not required). core-api shows a generic message; the AI message is
never shown to users.

## 5. Course, scope and retrieval semantics

| Scope | Permitted origins | Status |
|---|---|---|
| `class_only` | `class` | the only scope enabled by default |
| `class_plus_external` | `class`, `external` | can be enabled (`CHAT_ENABLED_SCOPES`) once external sources exist |
| `class_plus_web` | + `web` | **unavailable** until live web search has its own milestone; cannot be enabled |

- core-api never changes the requested scope. A scope that is not enabled, or not
  offered by the service's capabilities, is refused (422 before the stream, or an
  `error` event), never narrowed or widened.
- **Retrieval isolation** (AI side, M4 §7): every retrieval query filters by the
  request's `course_id` for class material **and** by `documents.status = 'indexed'`,
  in the same query, through the read-only retriever role / `retrievable_chunks` view
  proposed in M4 §8. External material: only `origin = 'external'` documents from an
  enabled source whose chunk **and** document `course_id` both equal the requested
  `course_id`, exactly like class material. Material with a NULL course (globally shared
  external material) is **not** retrievable or citable in M5; it is deferred to M6, which
  must define its own isolation rules. Never another course's material.
- **Draft-exam exclusion**: questions (`questions` table) are never retrieval
  material. In particular questions with `purpose = 'exam'` must never be read,
  embedded, retrieved, quoted or summarized by chat. Chat citations can only point at
  `chunks`, so core-api would reject any other reference.
- core-api never sends question bank content, user data other than the question text,
  or anything from another course.

## 6. Verification on the CE side

After a strictly valid response, core-api opens **one** short read-only `REPEATABLE
READ` transaction (statement timeout 5 s) and, on that snapshot, re-checks the
caller's course access and verifies:

- each chunk exists and belongs to the cited document; chunk origin = document origin =
  cited origin, permitted by the scope;
- the document is `indexed`;
- class: chunk course = document course = the requested course;
- external: the source exists and is enabled; chunk course = document course = the
  requested course (a NULL course on either is refused; shared external material is
  deferred to M6); web: always rejected;
- the quote (whitespace-normalized) occurs in the chunk text and is not trivially short;
- metadata is safe: title without control characters; external URL http(s) without
  credentials; locator of a known shape;
- every topic id is a topic of the course.

Public citation fields come from the records: `title` = `documents.title`; `url` =
`documents.url` (external only, class material has none); `locator` from
`chunks.locator` — `{"page": n}` → **"Page n"** (the physical PDF page; slide
numbers are not known), `{"slide": n}` → "Slide n" only if the indexer records real
slide numbering, `{"section": s}` / `{"heading": s}` → "Section s"; `snippet` = the
matching passage of `chunks.text`.

**Limits of verification.** This establishes provenance: each quoted passage exists in
a permitted, indexed source of this course. It does **not** prove that the passage
supports (entails) the sentence it is attached to, nor that the answer is correct.

## 7. The public endpoint (CE-2 contract)

`POST /api/v1/courses/{course_id}/chat`, body exactly `{"question": "...", "scope": "..."}`.

Order: authenticate → authorize course read (owner professor or enrolled student, from
the database) → `CHAT_ENABLED` and a configured client (else 503) → read the body
(bytes actually received ≤ `CHAT_MAX_BODY_BYTES`, within `CHAT_BODY_TIMEOUT_SECONDS`;
`Content-Length` must match; no `Content-Encoding`) → strict JSON and field validation
→ take a global and a per-user slot (`CHAT_MAX_CONCURRENT`,
`CHAT_MAX_CONCURRENT_PER_USER`; else 429 immediately) → open the stream. Inside the
stream: re-authorize → capabilities → re-authorize → one answer request → validate →
re-authorize and verify (§6) → emit. No database session is held across upstream HTTP
or while sending to the client.

Response: `text/event-stream`, LF-framed, compatible with CE-2's parser:

```
: studygraph validated answer stream

event: token
data: {"text":"The limiting reactant caps the "}

event: done
data: {"id":"msg_…","role":"assistant","text":"…","citations":[…],"grounded":true,"used_web":false,"scope":"class_only","topic_ids":["…"],"created_at":"2026-11-20T12:00:00Z"}
```

or a single `event: error` / `data: {"message": "<for the user>", "code": "…"}`.

This is **validated SSE delivery, not model-token streaming**: the answer is complete
and verified before the first `token` event; the token events are the verified text
cut into pieces. Errors before the stream opens are JSON (`{"detail", "code"}`) with
an HTTP status (400, 401, 403, 404, 408, 413, 415, 422, 429, 503).

Resources: the concurrency slot is released on every exit (completion, error, client
disconnect — which cancels the upstream call — server cancellation, a stalled send, and
a response that is never started); every HTTP client is closed by `async with`. The
limiter is **in-process**: a shared limiter is required before running several API
processes.

**Bounded lifetime of the slot.** From the moment the stream is created the slot is
held for at most: the opening (`http.response.start` + the comment line) accepted by
ASGI `send` within `CHAT_DELIVERY_DEADLINE_SECONDS` (15 s) **+** the pipeline deadline
(`CHAT_PROBE_DEADLINE_SECONDS` + `CHAT_ANSWER_DEADLINE_SECONDS` + 10 s for the awaited
steps, plus up to the database time limits below, since a running database call
defers the deadline) **+** the verified frames and the final empty body accepted
within another `CHAT_DELIVERY_DEADLINE_SECONDS`. A connected client that stops reading
and never disconnects therefore cannot keep the slot: when a `send` stalls past its
deadline the delivery is abandoned, the slot is released and the server closes the
connection (the user sees a cut-off stream and may ask again). Regression tests:
`test_a_stalled_asgi_send_does_not_hold_the_lease` (opening, first frame, final body)
and `test_a_stalled_send_without_disconnect_releases_the_slot`.

**Database time limits.** The pre-stream authorization, both re-authorizations and
the verification snapshot each run in their own short read-only transaction in a
worker thread, with `SET LOCAL statement_timeout = 5000` and
`SET LOCAL lock_timeout = 2000` (app/chat/db.py). Threads run in AnyIO's
non-abandoning mode: a deadline or disconnect that arrives while a database call is
running is **deferred until that call returns** (the task keeps waiting for the worker
thread), so the pipeline deadline is **not** an unconditional wall-clock bound during
database operations. The call itself is bounded by PostgreSQL's timeouts, about 5 s per
statement (plus up to 2 s waiting for a lock). After every threadpool call the pipeline
takes an explicit cancellation checkpoint inside the deadline scope, so an expired
deadline or cancelled request cannot start another upstream request or emit a
successful answer (tests: `test_an_expired_deadline_during_verification_never_emits_an_answer`,
`test_an_expired_deadline_during_authorization_sends_no_upstream_request`). Remaining limits that this code does **not** bound, to be covered by
infrastructure settings before activation: waiting for a free connection in
SQLAlchemy's pool (`pool_timeout`, default 30 s; the engine is the shared one in
`app/db.py`), opening a new TCP connection to PostgreSQL (no `connect_timeout` is set),
and a network path that goes silent without closing (the server-side timeouts cannot
fire if the packets never arrive; TCP keepalives or a connection-level timeout are the
remedy), and the size of the worker thread pool, which is shared with all other
requests.

## 8. Limits (settings)

`CHAT_MAX_BODY_BYTES` 16 KiB, `CHAT_BODY_TIMEOUT_SECONDS` 10, `CHAT_MAX_QUESTION_CHARS`
2000 (also capped by the service's `max_question_chars`), `CHAT_MAX_CONCURRENT` 8,
`CHAT_MAX_CONCURRENT_PER_USER` 1, `CHAT_CONNECT_TIMEOUT_SECONDS` 3,
`CHAT_READ_TIMEOUT_SECONDS` 30, `CHAT_PROBE_DEADLINE_SECONDS` 5,
`CHAT_ANSWER_DEADLINE_SECONDS` 60, `CHAT_DELIVERY_DEADLINE_SECONDS` 15,
`CHAT_MAX_RESPONSE_BYTES` 256 KiB (probe 16 KiB),
`CHAT_CAPABILITIES_CACHE_SECONDS` 30, `CHAT_MAX_ANSWER_CHARS` 8000,
`CHAT_MAX_CITATIONS` 16, `CHAT_MAX_TOPICS` 16.

## 9. Error handling summary

| Situation | User sees |
|---|---|
| service down, not ready, mock, protocol mismatch, non-200, bad framing | `error` "not available right now" |
| deadline or inactivity timeout | `error` "took too long" |
| scope not offered by the service | `error` "these sources are not available" |
| invalid response, invented/mismatched citation, failed verification | `error` "could not be checked … not shown" |
| access lost during the request | `error` "no longer have access" |
| client stops reading (a `send` stalls past the delivery deadline) | stream cut off; slot released |
| `grounded: false` | `done` with the insufficient-evidence message, no citations |

## 10. Activation prerequisites (not authorized now)

1. AI-1 implements §3–§5 and agrees to this contract (or a revision of it).
2. M4 §8 persistence prerequisites are applied by a CE migration: least-privilege
   retriever role, `retrievable_chunks` view (course + `indexed` filters), chunk course
   constraints, `index_generations`.
3. Real indexing (M4) is active, so `indexed` documents and their chunks exist.
4. Retrieval isolation and draft-exam exclusion are tested on the AI side against the
   shared schema.
5. A shared (multi-process) limiter if more than one API process runs.
6. Authentication (CE-3, JWT) replaces the fail-closed placeholder.
7. Someone with authority sets `CHAT_ENABLED=true` (and, separately, any scope beyond
   `class_only`).

## 11. Open decisions

- Real slide numbering in chunk locators (today: physical pages, "Page n").
- Minimum quote length and whether quotes may span chunk boundaries (today: no).
- Whether ungrounded answers may carry topic ids for recommendations (today: dropped).
- Persisting chat history (`ChatMessage.id` is generated per answer; nothing is stored).
