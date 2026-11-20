# External harvesting (M6, Lane A) — CE-1

Status: implemented behind flags, **not activated**. `HARVEST_ENABLED=false`,
`INDEXING_ENABLED=false` and `CHAT_ENABLED=false` by default; no provider source exists
until an operator creates one, and a created source starts disabled. No provider
traffic and no real AI service were used to build or test this milestone: every test
runs offline against fixtures and a disposable PostgreSQL/Redis.

Wikipedia (MediaWiki Action API) is the only implemented provider and the only one
eligible for future activation. OpenStax and OpenAlex are **unavailable** (the schema
CHECK on `sources.provider` admits only `wikipedia`). There is no live web search, no
generic scraper, no paid API and no user-configurable domain.

## 1. Overview

```
Beat ──► maintenance worker: harvest.maintain (every HARVEST_MAINTENANCE_INTERVAL_SECONDS)
            recover expired leases · bounded history cleanup ·
            schedule due targets (HARVEST_ENABLED only) · dispatch runs (HARVEST_ENABLED only)
                     │  {"run_id": "<uuid>"}  (Redis = hint only)
                     ▼
         harvest worker (-Q harvest.run, dedicated, concurrency 1): harvest.run_slice
            gates → claim (lease, token) → refresh existing resources → search approved
            queries → quality gates → revision-pinned content → persist under the lease
                     │  queued index job (ingestion_jobs, kind index) per new version
                     ▼
         index worker (M4, -Q ingestion.index) — only while INDEXING_ENABLED and only if
            the AI service advertised "section" locators; else the job waits, unclaimed
```

PostgreSQL is the authoritative store for every state; Redis messages only say "this
run may be claimable". No database transaction is ever open during a provider request.

## 2. Schema (revision 0005)

| Table / column | Purpose |
| --- | --- |
| `sources.provider`, `.language`, `.licence_url` | provider identity of an operator-created source (NULL for every legacy row; one source per provider+language) |
| `harvest_course_settings` | explicit course opt-in (`enabled` default false), active-document quota, `dirty_generation` |
| `harvest_targets` | course × source selected for harvesting: `harvested_generation`, refresh interval, `next_refresh_at`, `not_before` |
| `harvest_queries` | approved public query text per course topic (`approved` / `revoked`) |
| `harvest_provider_budgets` | per-source request window, minimum spacing, `blocked_until` |
| `harvest_runs` | durable harvest jobs: claims, failures, token, lease, dispatch reservation, persisted progress |
| `external_resources` | durable identity of a provider page within a course: revision tracking, dedup state, missing evidence; `current_document_id` is a composite key `(current_document_id, id) → documents (id, external_resource_id)`: the current version must exist and belong to the same resource |
| `external_document_versions` | provenance of each harvested document version (immutable except stale marking) |
| `documents.external_resource_id` | links a version to its resource through the composite key `(external_resource_id, course_id, source_id) → external_resources (id, course_id, source_id)`: a harvested document can only reference a resource of its own course and source; partial unique indexes: ≤ 1 `indexed` and ≤ 1 `pending` version per resource |
| `document_sections.locator` | CHECK widened to also accept `{"anchor", "heading", "level"}` section locators |

Migration safety: see [database-migrations.md](database-migrations.md#revision-0005-external-harvesting-m6).

## 3. Durable runs

`harvest_runs` follows the M3 job rules (conditional UPDATEs, `gen_random_uuid()`
attempt tokens, leases from the Celery hard limit plus margin, `SKIP LOCKED` sweeps,
database time only), with two counters that are never decremented:

- `claim_count` / `max_claims` — every claim (slice) uses one; a slice ends by
  completing, yielding or failing. Runs are therefore finite even if they only yield.
- `failure_count` / `max_failures` — only actual failed attempts: an error during the
  slice or an expired lease. Yields never count.

A **yield** (slice request/time limit reached, budget wait above
`HARVEST_BUDGET_MAX_WAIT_SECONDS`, provider throttle, a gate closed mid-slice) keeps the
persisted progress and re-queues the run. Progress (`harvest_runs.progress`: phase,
refresh cursor, the finished queries and the CURRENT query by its stable row id, that query's candidate list and position, tallies)
is written under the attempt token together with every persisted document, so nothing
is redone or lost. Work is done in *units* (one search; one info request plus at least
one content request) that never straddle a slice boundary, so every slice progresses.

**Approval and activation are re-read before every request.** Immediately before each public request that
belongs to a query (the search, and the info and content requests of its hits) the executor re-reads, from
the database, the course opt-in, the source activation, the target, lease ownership and the exact approved
query (the same `harvest_queries` row still `approved`, its topic still deriving exactly the approved text).
After any budget wait the same checks run again immediately before network I/O. A query that stopped being
usable (revoked, topic renamed) is dropped together with its candidate list and the run continues with the
next usable query; a closed gate or lost lease ends the slice without a further request. Search progress is
keyed by the query's row id (`progress.query_id`, `progress.done_queries`), never by list position, so an
approval change between slices cannot attach one query's hits to another query or text.

*Unavoidable races.* A request that has already been sent (or is about to be: the interval between the last
check and the socket write) when an operator revokes, renames, opts out or disables is not recalled; its
response is only used if the persistence step, which re-checks the gates under locks, still allows it. No
database transaction (or lock) is held across network I/O to narrow this window: that would block opt-out,
revocation and the maintenance sweeps for as long as the provider takes to answer.

**Scheduling.** One active (`queued`/`running`) run per target (partial unique index;
insert with `ON CONFLICT DO NOTHING`). A target is due when the course's
`dirty_generation` exceeds the target's `harvested_generation` or `next_refresh_at` has
passed, and `not_before` (failure backoff) allows it. A run records the generation it was
created for; completion raises `harvested_generation` only to that value. A class
indexing confirmed during a running harvest bumps `dirty_generation` past it, so the
target stays dirty and another run follows: no notification is lost. **Only class
indexing bumps it**: external indexing never triggers harvesting.

**Lock order** (every multi-row transaction):
`harvest_course_settings → sources → harvest_targets → harvest_runs → external_resources →
ingestion_jobs → documents (new version, then old)`. Index completion (M4 code) now takes
the course row (and for external documents the source and the resource) before the job
and document rows; harvest persistence, scheduling, opt-out and source disable follow the
same order. Concurrency tests exercise switch + persistence + class completion together.

**Recovery and cleanup** (always, also while harvesting is disabled): expired leases
count as failed attempts (re-queued with backoff, or failed when exhausted); queued runs
without claims or failures left fail; finished runs older than
`HARVEST_HISTORY_RETENTION_DAYS` are deleted in bounded batches (provenance keeps a NULL
run reference).

## 4. Topics, queries and privacy

The baseline is deterministic: a query is the **existing course topic name**,
NFC-normalized, control characters removed, whitespace collapsed (`derive_query`).

- An operator approves **the exact text** for a course topic (`approve-query`). The
  harvester only sends approved texts, and only while the topic still derives exactly
  that text: a renamed topic stops being searched until its new text is approved.
  Re-approval revokes the old row. At most `HARVEST_MAX_QUERIES_PER_COURSE` approvals.
- What a provider request can carry: one approved query text (search), page ids and
  revision ids obtained from the provider itself (info/content), and fixed protocol
  parameters. Never document passages, chunks, student questions or answers, course,
  document, user or topic identifiers, names, e-mail addresses or join codes.
- Screens (e-mail, URL, long digit runs, UUIDs, course code / join code / id, display
  names of course members, length and word limits) are **defence in depth, not the
  guarantee**. Pattern matching cannot recognize all personal information; the guarantee
  is that inputs are curated topic labels plus an explicit human approval of each text.

### Proposed `studygraph.topics/1` (not implemented; AI-2 owns the endpoint)

`POST /ai/v1/topics/extract` (path already listed in `contracts/ai-service.openapi.yaml`;
the shared contract is **not** changed here). Proposal:

```json
{"protocol": "studygraph.topics/1", "course_id": "<uuid>",
 "documents": [{"document_id": "<uuid>", "content_digest": "<sha256 of the indexed content>"}],
 "max_topics": 20}
```
Response `200`:
```json
{"protocol": "studygraph.topics/1", "mock": false, "course_id": "<uuid>",
 "topics": [{"name": "Chemical equilibrium", "query_candidates": ["Chemical equilibrium"],
             "evidence": [{"document_id": "<uuid>", "section_ordinal": 3}]}]}
```
Rules: `mock` must be `false` (mock output is refused, never stored); the AI side reads
indexed course material itself (CE sends identifiers and digests, no text); candidates are
only *proposals* shown to an operator, never approved automatically, and go through the
same screens and exact-text approval as topic names. Embedding-based relevance
(similarity between a candidate article and course material) is deferred: today's
relevance check is a **lexical** gate (a query word of ≥ 3 letters in the title or lead)
and is not described as anything else.

## 5. Wikipedia connector and HTTP security

Requests (GET `https://<language>.wikipedia.org/w/api.php`, `format=json`,
`formatversion=2`, `maxlag=HARVEST_MAXLAG_SECONDS`), parameters built in code only:

| Purpose | Parameters |
| --- | --- |
| search | `action=query&list=search&srsearch=<approved text>&srnamespace=0&srlimit=N&srprop=&srinfo=` |
| info (≤ 50 ids) | `action=query&prop=info\|pageprops\|revisions&pageids=…&rvprop=ids\|timestamp&ppprop=disambiguation` |
| content | `action=parse&oldid=<revision id from info>&prop=text\|sections\|revid&disableeditsection=1&disabletoc=1&disablelimitreport=1` |

Transport (`app/harvest/transport.py`; HTTP/1.1 framing is parsed by `h11`, already part of the pinned
runtime set as a dependency of httpx and now pinned directly): the host comes from a language allowlist, path
fixed, port 443; one DNS resolution, every address must be public unicast (private,
loopback, link-local, CGNAT, reserved, multicast, documentation, NAT64/6to4/Teredo and
IPv4-mapped private addresses refused; a mixed answer is refused entirely); the TCP
connection goes to a verified IP (no second resolution); TLS with certificate and
hostname verification, SNI and `Host` = the provider host, TLS ≥ 1.2; no redirects
(any 3xx is an error), no proxies (the socket is opened directly; no proxy environment is read), no cookies,
no credentials; `Accept-Encoding: identity`, any other `Content-Encoding` refused;
declared and received body sizes bounded (`HARVEST_MAX_RESPONSE_BYTES`), truncation, a chunked body
without its final chunk, an invalid chunk delimiter or size line, and ambiguous framing refused (the standard
library's `http.client` accepted the first two as complete JSON, which is why it is not used for responses); connect/read inactivity timeouts plus a deadline between body
reads; strict JSON (no duplicate keys, NaN/Infinity, non-object). DNS resolution itself
is bounded only by the harvest task's Celery limits (`HARVEST_TASK_SOFT/HARD_TIME_LIMIT`).

Provider policy: a configured, validated `HARVEST_USER_AGENT` naming the client and a real
contact (Wikimedia User-Agent policy) is required while harvesting is on; requests are
serial per worker, spaced (`min_interval_ms`) and budgeted per source window (atomic
reservation by database time, `harvest_provider_budgets`); `maxlag` is always sent; HTTP
429/503 and the API errors `maxlag`/`ratelimited` move `blocked_until` forward to the
provider's FULL `Retry-After` (or a default) and yield the run. The block is stored in PostgreSQL in full and never
shortened; `HARVEST_RETRY_AFTER_MAX_SECONDS` only bounds how soon the *run* is looked at again (it then
yields again while the budget still refuses). A delay that cannot be represented safely (not finite, or longer
than one year) suspends the source (`enabled = false`) for operator intervention instead; HTTP 403 and other API errors are permanent for the
run and also block the source for `HARVEST_THROTTLE_DEFAULT_SECONDS`.

**What application controls do not provide.** These controls constrain what this code
connects to; they are not network isolation. Before activation, infrastructure must
restrict egress of the harvest worker only (TCP 443 to Wikimedia's published ranges,
DNS to the platform resolver; nothing else), keep the API, parse and index workers
without internet egress, and monitor egress denials.

## 6. Revision consistency, provenance and licensing

The revision id recorded is never obtained separately from the content: the content
request names the revision (`oldid`) taken from the info response, whose `lastrevid` and
`revisions[0].revid` must agree; the parse response must echo the same `revid` and
`pageid`, else the candidate is refused (fail closed). Namespace 0, content model
`wikitext`, page language = source language, not a redirect, not a disambiguation page
are checked on the same info response. MediaWiki renders an old revision with the
templates current at fetch time; the stored normalized text is what that fetch produced.

**Licence verification.** Before the first content of a run is accepted, one budgeted request
(`action=query&meta=siteinfo&siprop=rightsinfo`, counted in the run, slice and provider budgets) must show the
provider's declared site licence as Creative Commons Attribution-ShareAlike 4.0 (URL and text). Another
licence, a missing or malformed answer, or ambiguous text fails the run permanently (`provider_licence_mismatch`
/ `provider_licence_unverified`) and blocks the source briefly; nothing new is accepted and already harvested
documents are untouched. This verifies the site's *declaration* only.

Stored per version: canonical URL (`/wiki/<title>`), revision URL (`index.php?oldid=`),
page and revision ids, revision timestamp, licence `CC BY-SA 4.0` and its URL, an
attribution text (title, "Wikipedia contributors", history URL, revision, licence),
fetch time, source, language and the normalized content SHA-256.

Excluded (never stored or indexed): tables, infoboxes, figures/images/media/galleries
and captions, maps, math markup, block quotations, references and citation markers,
navigation boxes, hatnotes, maintenance boxes, edit links, hidden elements and the
"References / External links / See also / Further reading / Notes …" sections. The
site-wide CC BY-SA licence is not treated as proof that every element is reusable: what
may carry another licence or third-party origin is dropped. Filtering by HTML structure does **not** prove
that all remaining prose is CC BY-SA or free of third-party content (for example text copied from another
source into an article body, or quotations in running prose); the limitation stays documented (§11) and the
attribution and activation prerequisites below still apply.

Section locators are genuine: a heading starts a section only if its anchor is in the
section list of the same parse response; the lead is `{"anchor": null, "heading": null,
"level": 0}`; headings are `{"anchor", "heading", "level": 2..6}`. No page numbers.

**Attribution obligations (before anything is shown).** Any surface that shows harvested
text or citations must display title, a link to the source (canonical or revision URL),
"Wikipedia contributors" with the history link, and the licence name with its URL, and
must indicate that excerpts may be modified (normalized). Derived works that reproduce
text are ShareAlike. These are activation prerequisites (§10), not implemented UI.

## 7. Deduplication, refresh and versions

All harvested documents are course-scoped (`documents.course_id`, provenance per course).
Accepting a revision (one transaction, under the course gate lock and the run lease):
document (external, fileless, `pending`), sections, provenance, queued index job,
resource update and run progress.

- Same normalized text as the resource's current version (whatever the revision id):
  nothing is created — no document, no index job; only `last_revision_id` moves.
- Same text as another accepted resource of the course: recorded as `duplicate`
  (resource identity and revision kept, no document); promoted to its own document when its text
  diverges. Deduplication is **version-aware** (decided under the course gate lock): content counts as
  held by another resource while ANY of its versions is pending, indexed or failed, not only its newest
  one - so an indexed X stays held while a newer version Y is pending, and a second resource with X is a
  duplicate. A duplicate is **reconsidered at its next refresh check even if its own revision is unchanged**
  when no accepted resource holds its content any more (the referenced resource changed content and the
  old version went stale, or became missing): it is fetched again and becomes its own document, or a
  duplicate of another holder.
- Course quota (`max_active_documents`): no new accepted resource beyond it.
- Changed text: a new version, unless one is still `pending` (then nothing now; the
  revision is not recorded, so the next refresh looks again). The previous `indexed`
  version stays visible; index completion of the replacement switches old → `stale`
  (reason `superseded`) **then** new → `indexed`, in one transaction. A replacement whose
  resource moved on (newer current version, page missing) never becomes indexed: it is
  marked stale. Indexed payload fields are never modified (digests stay valid).
- Periodic refresh inspects every due resource of the target by page id (batched info),
  including duplicates and missing ones, independent of search results.
- Missing pages (API `missing`, now a redirect, or moved out of namespace 0): first
  observation recorded; stale only after a second observation at least
  `HARVEST_MISSING_CONFIRM_SECONDS` later, with both observations stored as evidence
  on the resource and the stale version. Disappearing from search results, provider
  errors and ambiguous answers never stale anything. Nothing is deleted.

## 8. Integration with M4 indexing and M5 courses/chat

- **Payload.** Page-based documents keep the exact M4 body (keys, bytes, digest,
  idempotency key). Harvested versions are section-based: `document.locator_scheme =
  "section"`, `document.attribution = {provider, page_id, revision_id, revision_url,
  canonical_url, licence_url, attribution}` and section locators. See
  [ai-indexing-integration.md §3a](ai-indexing-integration.md#3a-locator-schemes-m6).
- **Capability negotiation.** Optional capabilities key `locators` (absent = `["page"]`,
  every M4 service stays compatible; unknown keys still refused). A section-based job for
  a service that did not advertise `"section"` — or whose source is disabled or course
  opted out — is **deferred without being claimed** (`EXTERNAL_INDEX_DEFER_SECONDS`, no
  attempt consumed, dispatch reservation released); dispatch takes jobs by
  `available_at`, so deferred external jobs never hold back PDF jobs.
- **Completion.** External completion re-checks under lock that the source is enabled
  and the course opted in (else job and document fail, retryable) and that the version is
  still current.
- **retry-index** accepts a failed fileless external version (no parse job is invented)
  if it is the resource's current version.
- **Listing** (`GET /courses/{id}/documents`, unchanged shape, no new filter): harvested
  versions appear with `origin: external`, `kind: article`, `source_name: "Wikipedia"`,
  URL and licence, status from their own index job lifecycle (`queued` → `processing` →
  `ready` / `failed`). Hidden: stale harvested versions, versions of disabled sources,
  and a not-yet-indexed replacement while an indexed version exists. Class documents
  and legacy external rows behave exactly as before.
- **Citations** (M5, unchanged): external citations need an indexed document, an
  enabled source and matching chunk/document/requested course.
- **Not yet in place (AI side, required before real external retrieval):** chunk
  persistence for section locators (chunk `locator` ≤ 40-character `section` strings for
  M5 display, or an agreed mapping), retrieval filters (course id, `origin`, document
  `status = indexed`, source enabled), carrying attribution into answers, and the AI
  persistence prerequisites of ai-indexing-integration.md §8.

## 9. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARVEST_ENABLED` | `false` | master switch (scheduling, dispatch, claims, requests) |
| `HARVEST_USER_AGENT` | — | required when enabled; validated (client/version + real contact) |
| `HARVEST_QUEUE` | `harvest.run` | dedicated queue; must differ from the other queues |
| `HARVEST_TASK_SOFT/HARD_TIME_LIMIT_SECONDS` | 240 / 270 | Celery limits; lease = hard + `JOB_LEASE_MARGIN_SECONDS` |
| `HARVEST_SLICE_MAX_REQUESTS` / `HARVEST_RUN_MAX_REQUESTS` | 20 / 300 | per slice / per run |
| `HARVEST_RUN_MAX_CLAIMS` / `HARVEST_RUN_MAX_FAILURES` | 40 / 3 | finite runs |
| `HARVEST_MAX_QUERIES_PER_COURSE` / `HARVEST_CANDIDATES_PER_QUERY` | 20 / 5 | query and candidate bounds |
| `HARVEST_MAX_CANDIDATES_PER_RUN` / `HARVEST_MAX_ACCEPTED_PER_RUN` | 60 / 20 | per run |
| `HARVEST_DEFAULT_MAX_ACTIVE_DOCUMENTS` | 20 | course quota written at opt-in |
| `HARVEST_MIN/MAX_DOCUMENT_CHARS`, `HARVEST_MAX_SECTIONS` | 1500 / 300000, 200 | quality gates |
| `HARVEST_MAX_RESPONSE_BYTES` | 4 MiB | per provider response |
| `HARVEST_CONNECT/READ_TIMEOUT_SECONDS`, `HARVEST_REQUEST_DEADLINE_SECONDS` | 5 / 15, 30 | per request |
| `HARVEST_MAXLAG_SECONDS`, `HARVEST_THROTTLE_DEFAULT_SECONDS`, `HARVEST_RETRY_AFTER_MAX_SECONDS` | 5, 300, 86400 | provider courtesy |
| `HARVEST_BUDGET_WINDOW_SECONDS` / `_MAX_REQUESTS` / `_MIN_INTERVAL_MS` | 60 / 30 / 1000 | defaults written by `init-source` |
| `HARVEST_MISSING_CONFIRM_SECONDS` | 86400 | spacing of the two missing observations |
| `HARVEST_HISTORY_RETENTION_DAYS` | 30 | run history kept |
| `EXTERNAL_INDEX_DEFER_SECONDS` | 900 | deferral of external index jobs that cannot be sent |

## 10. Operator commands and activation

All commands: `python -m app.harvest.admin <command> --database NAME …` inside a
core-api or worker container (same target verification as `app.ingestion.admin`).
Worker: `celery -A app.worker worker -Q harvest.run --concurrency 1` (refused if mixed
with other queues); `python -m app.ingestion.admin preflight --role harvest`.

**Compose.** `docker-compose.yml` defines `worker-harvest`: queue `harvest.run` only, concurrency 1, no upload
volume, no published ports, `HARVEST_ENABLED: "false"`. Setting the flag on that service alone does nothing
useful: **both** workers need `HARVEST_ENABLED=true` (and the same database):

- `worker-maintenance` runs `harvest.maintain`, which schedules due targets and dispatches runs only while
  the flag is on (recovery and cleanup always run);
- `worker-harvest` claims runs and talks to the provider only while the flag is on, and also needs
  `HARVEST_USER_AGENT` (validated; refused when missing or a placeholder).

The compose network gives `worker-harvest` ordinary outbound access. **The infrastructure egress restriction
(§5) is a separate prerequisite that compose does not provide** and must be in place (this service only: TCP
443 to the provider, DNS to the platform resolver) before the flag is turned on anywhere that matters. The
other workers and the API must stay without internet egress.

```
init-source     --provider wikipedia --language en      # creates the source DISABLED + budget
enable-source   --source SOURCE_ID                       # explicit activation
course-opt-in   --course COURSE_ID --source SOURCE_ID [--max-documents N]
propose-queries --course COURSE_ID                       # read only: derived text + screen result
approve-query   --course COURSE_ID --topic TOPIC_ID --text "EXACT TEXT"
trigger         --course COURSE_ID --source SOURCE_ID    # optional manual run
status          --course COURSE_ID
revoke-query    --query QUERY_ID
course-opt-out  --course COURSE_ID                       # cancels runs; existing documents kept
disable-source  --source SOURCE_ID                       # cancels runs; hides its documents
```

**Activation prerequisites (none is met by this milestone; all are required):**
1. Infrastructure egress restriction for the harvest worker (§5) and monitoring.
2. `HARVEST_ENABLED=true` on both `worker-maintenance` and `worker-harvest`, and a real `HARVEST_USER_AGENT` with a monitored contact; review of Wikimedia's current
   API etiquette and robot policy; conservative budgets.
3. Attribution and licence display wherever harvested text or citations appear (§6),
   reviewed against CC BY-SA 4.0; ShareAlike implications for derived output reviewed.
4. AI side: section-locator indexing, retrieval filters and attribution (§8), and the
   M4 persistence prerequisites; `"section"` advertised only once these exist.
5. Approval process for queries (who approves, record keeping) and course opt-in by
   the course owner.
6. A staging run with `HARVEST_ENABLED=true` against a non-production database.

## 11. Known limitations

- Relevance is lexical only; no embedding similarity until the AI service offers it.
- Topic extraction is not automated (`studygraph.topics/1` is a proposal).
- Course opt-out stops harvesting and indexing of new versions; already indexed versions
  stay visible and citable (source disable hides them). Per-course withdrawal of indexed
  material is a later milestone, as is any deletion (M6 deletes nothing).
- Templates are rendered at fetch time; a template change alone can produce a new
  version on refresh (only when the normalized text changes).
- English titles are used to exclude reference-type sections; for other languages the
  class-based exclusions apply.
- DNS resolution is bounded only by Celery task limits.
- A duplicate is reconsidered at its own next refresh check, not the moment the resource it duplicates changes;
  until then its content is covered only if another version still holds it.
- An unrepresentable `Retry-After` suspends the whole source until an operator re-enables it.
- In-flight requests cannot be recalled (§3, "Unavoidable races").
- Licence verification covers the site-wide declaration, not individual passages (§6).
- One provider; OpenStax and OpenAlex need their own connectors, licence review and a
  migration widening `sources_provider_check`.
