# Asynchronous document processing (M3)

Owner: CE-1. Code under `services/core-api/`: `app/ingestion/`, `app/worker.py`,
`app/celery_app.py`, `app/storage.py`, `app/storage_identity.py`; schema in migration
`0003`. Upload and listing (M2) are described in `docs/document-ingestion.md`.

M3 turns a stored PDF and its queued `parse` job into ordered, page-aware, normalized
**document sections**, then hands the document to indexing (M4) with a queued `index`
job. It does not chunk, embed, tag topics or call the AI service: a parsed document is
**not** ready for retrieval, and is never shown as `ready`.

```
upload ─► document + parse job (one transaction) ─► COMMIT confirmed  ┐ the table is the
dispatch sweep (Beat, every 10 s) ─► publish new, due or lost jobs     ┘ durable outbox
       (optional, off by default: the API also publishes right after the response)
parse worker ─► storage identity check ─► claim (token + lease) ─► open file (no symlinks)
       ─► verify size + SHA-256 ─► extract in a limited subprocess ─► normalize ─► sections
       ─► one transaction: sections + page_count + job succeeded + queued index job
lease recovery (Beat, every 60 s) ─► requeue or fail attempts that died
reconciliation (Beat, hourly) ─► remove provably abandoned temp files, report the rest
```

## Processes

| Service (Compose) | Command | Queue | Needs |
| --- | --- | --- | --- |
| `worker` | `celery -A app.worker worker -Q ingestion.parse -c 2` | `ingestion.parse` | DB, Redis, uploads volume |
| `worker-maintenance` | `celery -A app.worker worker -Q ingestion.maintenance -c 2` | `ingestion.maintenance` | DB, Redis, uploads volume |
| `beat` | `celery -A app.worker beat -s /tmp/celerybeat-schedule` | publishes to `ingestion.maintenance` | Redis only |
| `worker-index` (M4) | `celery -A app.worker worker -Q ingestion.index -c 2` | `ingestion.index` | DB, Redis, AI service; **no** uploads volume |

All three use the core-api image and code; there is no separate worker image or
dependency set (`services/worker/` only holds a pointer README). Maintenance tasks run
in their own worker so that long parses can never starve dispatch, recovery or
reconciliation (the integration test `test_maintenance_keeps_running_while_every_parse_slot_is_busy`
checks this with the only parse slot blocked). Beat must run as **one** instance; the
tasks it triggers are safe to run concurrently anyway.

## Celery configuration (`app/celery_app.py`)

| Setting | Value | Why |
| --- | --- | --- |
| Broker | `REDIS_URL` | the existing Redis |
| Result backend | none, `task_ignore_result` | PostgreSQL is the source of truth; nothing is written back to Redis |
| Serialization | JSON only | a broker message can never carry pickled code |
| `task_acks_late` | on | a message taken by a worker that dies is delivered again; duplicates are harmless |
| `task_reject_on_worker_lost` | off | a pool process killed by a document is not handed it again in a loop; the lease decides |
| `worker_prefetch_multiplier` | 1 | a busy process holds no queue of messages |
| Concurrency | 2 parse, 2 maintenance | memory budget of the extractor (below) |
| Parse task time limits | soft 150 s, hard 180 s | above the extractor timeout (120 s), below the lease |
| Maintenance time limits | soft 100 s, hard 120 s | |
| Redis `visibility_timeout` | 3600 s | far above any task; must exceed twice the hard limit (enforced) |
| Broker connection (workers) | retry on start-up and on loss, forever | workers wait for Redis |
| Broker connection (publishers) | bounded, see Dispatch | requests and sweeps never wait indefinitely |
| `worker_cancel_long_running_tasks_on_connection_loss` | off | a Redis blip does not kill a running parse; fencing absorbs duplicates |
| Remote control, events, gossip, mingle, heartbeat | off | nothing but tasks is read from the broker |
| `worker_max_tasks_per_child` / `worker_max_memory_per_child` | 100 / ~400 MB | recycles pool processes that grow |

Prefork and the database: the worker's main process never keeps a connection (its
start-up check uses a throwaway `NullPool` engine and disposes it); every pool process
builds its own `NullPool` engine lazily, keyed by its pid, and `worker_process_init`
drops anything inherited. No connection or pool crosses a fork.

Database target of the workers (`app/ingestion/runtime.py`): taken only from
`DATABASE_URL` in the process environment and validated with the migration safety
primitives (`app/migration_safety.py`): PostgreSQL URL shape and explicit port; no
loopback or Docker host-alias address unless `MIGRATIONS_ALLOW_LOOPBACK_PORT` names that
port; refused while `PGHOSTADDR`, `PGSERVICE` or `PGOPTIONS` is set. The operational
engine is built from that validated URL, and **every connection it opens is verified
before anything runs on it**: database, role and schema are the ones the URL names and,
when `DATABASE_REQUIRE_COMMENT` is set, the database carries exactly that COMMENT (set
but empty is an error, never "no check"). A failed check raises before the first
statement, so no claim or job write can reach another database.

Start-up checks (run in `worker_init`; failure exits with status 78 before anything is
consumed): `DATABASE_URL`, `REDIS_URL` and `UPLOAD_DIR` set explicitly (no built-in
defaults, no `.env`); the database target valid, reachable and verified as above; the
schema at the revision the code needs (`0003`). Workers never run migrations. Beat only
requires `REDIS_URL`.

Configuration: every value above that is not a code constant is an environment variable
of `app/config.py` (for example `PARSE_TIMEOUT_SECONDS`, `PARSE_MEMORY_LIMIT_MB`,
`PARSE_MAX_PAGES`, `DISPATCH_INTERVAL_SECONDS`, `REDISPATCH_AFTER_SECONDS`); see
`.env.example`. The settings refuse inconsistent limits (extractor timeout < soft <
hard limit; visibility timeout > 2 × hard limit; distinct queue names). `DATABASE_URL`
and `REDIS_URL` are secret fields: `repr()`/`str()` of the settings and validation
errors never show them.

## Schema (migration 0003)

`ingestion_jobs` gains:

| Column | Type | Meaning |
| --- | --- | --- |
| `attempt_token` | `uuid` NULL | identity of the attempt holding (or that finished) the job; new for every claim |
| `lease_expires_at` | `timestamptz` NULL | a running attempt not finished by then is presumed dead |
| `available_at` | `timestamptz` NOT NULL DEFAULT `now()` | neither dispatched nor claimed before (retry backoff) |
| `last_dispatched_at` | `timestamptz` NULL | last publication reservation; NULL = never published |

plus `ingestion_jobs_lease_check`: a running job has a token and a lease after its start.

`document_sections` (new):

| Column | Type | Rule |
| --- | --- | --- |
| `id` | `uuid` PK | `gen_random_uuid()` |
| `document_id` | `uuid` NOT NULL | FK `documents(id)` **ON DELETE CASCADE** (sections are derived data) |
| `ordinal` | `integer` NOT NULL | `>= 0`, `UNIQUE (document_id, ordinal)` |
| `text` | `text` NOT NULL | not blank |
| `locator` | `jsonb` NOT NULL | exactly `{"page": n}`, n an integer 1 .. 2³¹−1 |
| `created_at` | `timestamptz` NOT NULL | `now()` (all sections of one parse share it) |

Conventions: one section per page that has text; **ordinal is 0-based and contiguous**
in page order; **`page` is the 1-based physical page number** (not a printed page
label). Pages without text produce no section and do not shift the numbers of the pages
after them. The unique constraint's index serves ordered reads by document (checked
with `EXPLAIN` in the tests). The CHECKs are written to evaluate to TRUE or FALSE, never
NULL. A later format (slides, headings) extends the locator CHECK in its own migration.

`storage_identity` (new): a single row (`id = 1`) with the `storage_id` of the upload
store this database's files live in (see Storage identity).

`documents` is unchanged: no new status. Details, upgrade preflight and downgrade guard
in `docs/database-migrations.md`.

## Job lifecycle

```
queued ──claim──► running ──complete──► succeeded   (+ sections, page_count, queued index job)
  ▲                  ├── permanent, or transient on the last attempt ──► failed (document failed if pending)
  └─ transient ──────┘   (available_at = now() + backoff, token and lease cleared)
running, lease expired ──recovery──► queued (attempts left) │ failed (exhausted)
```

- **Claim** (one short transaction, committed before parsing starts): a conditional
  `UPDATE` that requires `kind = 'parse'`, `status = 'queued'`, `available_at <= now()`
  and attempts left; it increments `attempt_count`, sets `started_at`, a new
  `attempt_token` and `lease_expires_at = now() + lease`.
- **Complete** (one transaction): lock the job row `WHERE status = 'running' AND
  attempt_token = :token`, lock the document row and re-check that it is still `pending`,
  check that no index job is active, replace the document's sections, set `page_count`,
  mark the job `succeeded`, insert the queued `index` job. The document stays `pending`.
- **Fail / requeue** (one transaction): the same token-conditioned lock first.
- No transaction or row lock is held while a PDF is parsed.

### Attempt fencing and concurrency assumptions

- PostgreSQL READ COMMITTED: when two sessions race for a row, the second waits for the
  first's row lock and re-evaluates its `WHERE` against the new version. Exactly one
  claim of a queued job succeeds (tested with eight concurrent sessions).
- Every write on behalf of an attempt is conditional on its token. Recovery and requeue
  clear the token; a new claim replaces it. A worker frozen past its lease (stopped
  container, suspended VM) that resumes finds no row and writes nothing: no sections, no
  job state, no document state (tested in process and with real workers).
- Fencing is by token, not by time: an attempt whose lease has expired but which has not
  been recovered yet may still complete, because no newer attempt exists.
- Lock order is the same everywhere: parse job row(s), then the document row, then
  (completion only) the active index job. Recovery and dispatch take job rows with `SKIP
  LOCKED`. The tests run competing operations (completion against recovery, concurrent
  recoveries, concurrent retries, concurrent claims); this is evidence, not a proof that
  no deadlock can ever occur.
- All times come from the database clock.

### Retry policy

| Class | Examples | Effect |
| --- | --- | --- |
| Not claimed | duplicate message, job done, backoff not over, **upload store not established** | nothing written, no attempt consumed |
| Permanent (the document) | encrypted, unreadable, no text, over a limit, timeout/memory limit, file missing from a verified store, not a regular file, size or SHA-256 mismatch | job and document fail at once |
| Transient (the environment) | database or storage I/O error, store stops verifying after the claim, extractor crash, soft time limit, unexpected error | requeued with backoff while attempts remain, then failed as exhausted |

- Attempts: `max_attempts` (default 3) counted by `attempt_count`, incremented only at
  claim. Celery's own retries are not used, so there is no second counter.
- Backoff: `RETRY_BACKOFF_BASE_SECONDS × 2^(attempt−1)`, ±20 % jitter, capped at
  `RETRY_BACKOFF_MAX_SECONDS` (defaults 30 s and 600 s), stored in `available_at`; the
  dispatch sweep publishes the job when it is due. No ETA/countdown messages are used.
- `ingestion_jobs.error_message` holds `code: detail` for operators (never public).
  `documents.error_message` holds one of these user messages:

| Code | Message |
| --- | --- |
| `encrypted_pdf` | This PDF is password-protected or encrypted. Upload a copy without protection. |
| `invalid_pdf` | This PDF could not be read. It may be damaged; export it again and upload the new file. |
| `no_text` | No selectable text was found in this PDF. Scanned documents are not supported yet. |
| `too_many_pages` / `too_much_text` | This PDF has more pages / contains more text than the current processing limit allows. |
| `processing_limit` | This PDF is too complex to process within the current limits. |
| `file_unavailable` | The stored file for this document is unavailable, so it could not be processed. |
| `retries_exhausted` | Processing failed repeatedly because of a temporary server problem. |
| other | This document could not be processed because of a server problem. |

  Failures never overwrite a document that is no longer `pending` (indexed, stale).
- Re-uploading the same file after a failure is still a 409 (M2 dedup counts failed
  documents); the user messages only suggest re-uploading a *different* file. An operator
  retries with `retry-parse` (below).

### Lease recovery and crash windows

Lease = parse hard time limit + `JOB_LEASE_MARGIN_SECONDS` (180 + 120 = 300 s). A live
worker kills its own attempt at the hard limit, so a lease only expires for an attempt
that is dead or frozen. Recovery (every 60 s) requeues such jobs (or fails them when no
attempt is left) and also fails queued jobs with no attempts left (only possible after
manual edits). It takes rows with `SKIP LOCKED` and conditions every update on the token
it read, so concurrent schedulers are safe.

| The worker dies … | Result |
| --- | --- |
| before claiming | the message comes back (warm shutdown: at once; SIGKILL: after the visibility timeout) or the sweep republishes it; no attempt used |
| after claiming | the job stays `running` until its lease expires; recovery requeues it (attempt counted) or fails it when exhausted |
| after extraction, before persisting | same; the next attempt extracts again |
| during the final COMMIT | the commit is atomic: either the full result and `succeeded`, or nothing; if the outcome is unclear the worker checks once on a fresh connection, logs, and writes nothing more |
| after its result committed, before the ack | the message is delivered again; the claim finds the job `succeeded` and does nothing |

## Dispatch (`app/ingestion/dispatch.py`)

- **New uploads are published by the sweep** (default). The API process does not talk to
  the broker: a broker's socket timeouts bound each read or write's *inactivity*, not
  the total time of an operation, so a slowly answering broker could otherwise hold an
  API worker thread. The cost is latency: a new job waits for the next sweep, at most
  `DISPATCH_INTERVAL_SECONDS` (10 s) plus the sweep's own run time.
- **Optional immediate publication** (`DISPATCH_ON_UPLOAD=true`): only once the commit is
  confirmed (the M2 rules decide; an unconfirmed upload, 503, is never published), the
  router schedules `publish_new_job` as a background task that runs after the response
  is sent. It reserves the job, then publishes; it never raises. Enable it only with a
  broker you trust to answer promptly: it has no wall-clock bound (see Broker I/O).
- **Reservation**: `UPDATE … SET last_dispatched_at = now()` only if the job is queued,
  due (`available_at <= now()`), has attempts left, and was never published or was
  published more than `REDISPATCH_AFTER_SECONDS` (300 s) ago. Upload path and sweep use
  the same compare-and-set, so concurrent publishers publish a job at most once per
  interval (tested with concurrent upload publications and two sweeps).
- **Failed publication**: the reservation is released only if it is still this
  publisher's (`last_dispatched_at` = the reserved value), and `available_at` moves
  `PUBLISH_RETRY_DELAY_SECONDS` (30 s) ahead. An uncertain publication is treated the
  same way; a message that did arrive meets a job that is not claimable yet and is
  dropped, and the job is published again later.
- **Sweep** (every 10 s, batch of 100, `FOR UPDATE SKIP LOCKED`): publishes jobs never
  published (created before M3, publish failed, API crashed after commit, requeued by
  retry or recovery) as soon as they are due, and republishes jobs whose message was
  presumably lost. It first asks the broker for the parse queue's depth: unreachable
  broker → it reserves nothing (one connection attempt per sweep); depth ≥
  `DISPATCH_MAX_QUEUE_DEPTH` (100) → it publishes nothing. That check is **advisory**:
  uploads publish without it and concurrent sweeps can read the same depth.
- **Broker I/O, precisely**: every publication uses a fresh, pool-free broker
  connection, closed on every path; at most `BROKER_PUBLISH_ATTEMPTS` (2) attempts, and
  no new attempt starts once the attempt budget is spent. Within an attempt, the connect
  timeout (2 s) bounds connecting and the socket timeout (5 s) bounds each read or write
  *between bytes*: a refused port or a silent broker ends quickly (both tested: one
  connection per attempt, all closed), but a broker that keeps answering slowly can
  stretch an attempt. **There is no wall-clock guarantee in the publisher itself.** The
  absolute bound for the sweep is the maintenance task's Celery hard time limit (120 s:
  the pool process is killed and replaced); that is why the API does not publish by
  default.
- `index` jobs are never published or claimed in M3. Since M4 they are published by
  `sweep_index` on the same reservation rules, only while `INDEXING_ENABLED` is true
  (see `docs/ai-indexing-integration.md`).

## PDF extraction (`app/ingestion/extraction.py`, `extract_child.py`)

Library: **pypdf 7.1.1** (BSD-3-Clause, pure Python, no runtime dependency on Python
3.11). Compared: pdfminer.six (slower, heavier, extra dependencies), pypdfium2 (native
C++ parser of untrusted input), PyMuPDF (AGPL). pypdf had many resource-exhaustion
advisories in 2026, all fixed in the pinned release; that history is why it runs in a
limited subprocess.

- The worker opens the file through the storage abstraction (`open_stored`: only
  server-generated keys, no symlinked directory or file, regular files only), checks its
  size and SHA-256 against the document row, and passes the open descriptor to the child,
  which therefore reads exactly the bytes that were verified.
- The child: `python -I extract_child.py`, empty environment (no database or broker
  settings; nothing secret in its arguments), working directory `/`, only the PDF
  descriptor inherited, its own process group. Before importing pypdf it sets
  `RLIMIT_AS` (`PARSE_MEMORY_LIMIT_MB`, 512), `RLIMIT_CPU` (timeout + 5 s), no core
  files, `RLIMIT_FSIZE` 0 (it cannot create files) and 64 descriptors.
- Lifecycle: `PR_SET_PDEATHSIG = SIGKILL` is set between fork and exec, and the child
  then checks its parent is still the process that started it (closing the race where
  the parent dies first). If the pool process dies (hard time limit, SIGKILL, OOM), the
  kernel kills the extractor (tested by killing a real worker's pool process). The
  return value of `prctl` is checked: if the signal cannot be set, or `prctl` is not
  available (not Linux), no extractor runs and the attempt fails as transient
  (`extractor_unprotected`). The signal follows the parent *thread*; tasks run on the
  pool process's main thread. `RLIMIT_CPU` independently ends a runaway child.
- Output is read incrementally with a deadline (`PARSE_TIMEOUT_SECONDS`, 120) and a byte
  cap on stdout (6 bytes per allowed character, the worst case of JSON escaping, plus
  per-page overhead) and stderr (64 KiB). Exceeding either kills the child. On every
  exit path (success, failure, timeout, oversized output, exception, soft time limit)
  the process group is killed if still running and the child is reaped.
- The output must match a strict schema (exact keys, types, increasing pages within the
  page count, total text within the limit), or it is rejected as transient.
- Limits: `PARSE_MAX_PAGES` 1000, `PARSE_MAX_TEXT_CHARS` 5 000 000, pypdf's own
  decompression limits (75 MB per stream; a 200 MiB deflate bomb in a 200 KiB file is
  stopped by them), and the memory limit (a 60 MiB stream under a 160 MiB limit is
  stopped by it). Reaching any of them is a permanent `processing_limit`,
  `too_many_pages` or `too_much_text`.
- **Encryption policy**: every encrypted PDF is rejected (`encrypted_pdf`), including
  owner-password-only files that open without a password; no decryption is attempted.
- **No OCR.** A PDF whose pages yield no text (scans) fails with `no_text`.
- pypdf reads; it does not run JavaScript, follow launch actions or fetch remote
  content.

**This is resource isolation, not a security sandbox.** The child runs as the same user
with the same filesystem view and network access, without seccomp or namespaces; a
parser bug that leads to code execution would not be contained. `RLIMIT_AS` and an empty
environment bound memory and remove credentials from the child's environment; they do
not make arbitrary malicious PDFs safe.

## Normalization (`app/ingestion/normalize.py`)

Per page: Unicode NFC (never NFKC, which would turn H₂O into H2O and x² into x2); Latin
ligatures ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ spelled out; CR LF / CR / VT / FF / NEL / U+2028 / U+2029 → LF;
NUL and other control characters (except TAB and LF), lone surrogates, U+FEFF, U+200B
and soft hyphens removed (ZWJ and ZWNJ kept); horizontal whitespace, including
no-break spaces, collapsed to one space; line ends trimmed; three or more line breaks
collapsed to two; the page trimmed. Not done: case folding, symbol or punctuation
removal, joining lines or hyphenated words, header/footer removal, correction by any
model, chunking.

Known limitations of the extracted text: multi-column pages may come out in content
stream order rather than reading order; tables are flattened to lines; formulas drawn as
graphics or images are lost, and formulas set with special fonts may extract with odd
characters; running headers and footers repeat on every page; scanned pages yield
nothing.

## Document status and the M4 hand-off

| `documents.status` | Condition | Public `status` |
| --- | --- | --- |
| `pending` | a job of the document is running | `processing` |
| `pending` | a parse job succeeded (sections stored, indexing pending) | `processing` |
| `pending` | otherwise (parse job queued or waiting to retry) | `queued` |
| `indexed` | | `ready` (set by M4 only, after a confirmed, digest-verified indexing) |
| `failed` | | `failed` with `error` |
| `stale` | | `failed` (unchanged from M2) |

The API is backward compatible: same endpoints, fields and values; `pages` is filled
after parsing. A successful parse creates, in the same transaction, a queued
`ingestion_jobs` row of kind `index`: the durable hand-off M4 consumes. M3 never claims,
publishes or recovers it, and creates none if one is already active (that is treated as
a conflict and fails the parse job with a generic message, not as a successful hand-off).

## Storage identity (`app/storage_identity.py`)

The upload store carries a marker at its root, `.studygraph-storage`
(`{"kind": "studygraph-upload-store", "version": 1, "storage_id": "<uuid>"}`), and the
database records the same id in `storage_identity`. Both must exist and match:

- before the API accepts an upload (else `503 storage_unavailable`, after the access
  checks and before the body is read, and again right before the file is written);
- before a worker claims a job (else nothing is claimed and no attempt is consumed, so a
  missing, empty, wrong or unreadable mount — a typo in a volume name creates a new
  empty volume — fails closed for every job instead of failing every document);
- before reconciliation does anything.

A missing file is a *confirmed* missing file only when the store verifies; if the store
stops verifying between the claim and opening the file, the attempt is transient.

**One root.** `LocalFileStorage` pins its root at first use: the fully resolved path
and the directory's identity (`st_dev`, `st_ino`). Every later access — the identity
check, the ownership lock, staging, placement, reads, reconciliation — first checks that
the configured path still resolves to that path and that the directory there is still
that directory. A retargeted symlink or a replaced mount makes every operation fail
(`503 storage_unavailable` for uploads, no claim for workers) instead of verifying the
marker in one store and writing into another (regression-tested with exactly that
sequence). Supported setting: a single host, the store a trusted local directory or
volume that is not swapped while the service runs; the instant between a check and the
system call after it is not protected against an attacker with write access to the
volume.

The marker is read through a descriptor of the root without following symlinks; it
must be a small regular file and parse exactly. The binding is created only by the
explicit `init-storage` command, never automatically, and always in this order: the
database record is committed first, the marker written second, both under a
PostgreSQL advisory lock that serializes concurrent runs. Consequences:

- a marker without a record was not written by this database's initialization: the
  store was initialized for another database (or deployment). It is **never adopted
  silently**, even when the store is empty; `--adopt-existing` records its id;
- a record without a marker is an interrupted initialization (or a wrong, empty mount):
  completed automatically only when neither the store nor the database holds uploads,
  otherwise `--adopt-existing` is required;
- a different marker, or a directory without a marker once the database is bound and
  holds uploads, is refused;
- whenever stored documents exist, every one's file must be present in the directory,
  unless `--allow-missing` is added.

## Reconciliation (`app/ingestion/reconcile.py`)

Nothing happens unless the store's identity is established (so a database outage or a
wrong mount never leads to cleanup).

1. **Temporary files** (`.incoming/staged-*.part`) are removed only when provably
   abandoned. Protocol (`app/storage.py`): an upload takes `.incoming.lock` shared,
   creates its file, locks it exclusively with `flock(2)` on a second descriptor and
   releases `.incoming.lock`; it keeps the file lock (which covers the final name too:
   same inode) until it has finished, removes its temporary name, and releases the lock
   last. The reconciler takes `.incoming.lock` exclusively — so no file can be between
   creation and locking while it looks — and removes a file only if it can take the
   file's lock itself (flock locks die with their process), the file is older than
   `RECONCILE_TEMP_GRACE_SECONDS` (1 h), and the name still refers to the inode it
   locked; it opens entries relative to the directory without following symlinks and
   never waits for a file lock. Removing a temporary name never removes a stored file.
   Uploads wait at most 10 s for the reconciler, then fail with `storage_error`.
2. **M2 temporary files** (`upload-*.part`, created without the ownership protocol),
   symlinks and anything unexpected in `.incoming`: **reported only**.
3. **Stored files without a document row** (a crash between placing a file and
   committing): **reported only**, never moved or deleted, after a grace period
   (`RECONCILE_ORPHAN_GRACE_SECONDS`, 24 h), skipping files whose upload still holds the
   lock, and asking the database again after that check. Automatic removal is not done:
   an upload whose COMMIT outcome was unknown may still have committed.
4. **Documents whose file is missing** (or not a regular file): **reported only**. A
   queued document fails through its parse job; parsed or indexed documents are left as
   they are.

Bounded and fair: each of the three scans examines at most `RECONCILE_BATCH_SIZE`
(1000) entries per run, starting at a random point and wrapping around, so a store
larger than the batch is covered over successive runs instead of always the same first
entries. Coverage is probabilistic (a window of the batch size per run), not a
guaranteed full sweep; a persisted cursor would make it deterministic if stores grow
far beyond the batch size.

Guarantees and limits: the protocol relies on `flock` between processes of one host
sharing a local filesystem (containers on one Docker host qualify; network filesystems
do not). Reports are log lines (`reconcile_*`); acting on them is an operator decision.

## Broker security

`docker-compose.yml` does **not** publish Redis on the host: every service reaches it
over the Compose network as `redis:6379`. (Before M3 it was published on
`0.0.0.0:6379` without a password; the running development container keeps that
mapping until it is recreated during adoption.) If a developer needs Redis from the
host, publish it on loopback only, in an untracked override file:

```yaml
services:
  redis:
    ports: ["127.0.0.1:6379:6379"]
```

In production use an authenticated Redis (`requirepass` or ACL users, the credentials
in `REDIS_URL` from a secret, `rediss://` if it crosses a network) on a private network
reachable only by core-api, the workers and Beat.
Defence in depth in the code: JSON-only messages; a parse message must be exactly
`{"job_id": "<canonical uuid>"}` and maintenance messages carry nothing (anything else
is logged and dropped); the job id is only ever used to look up a row, never as a path
or command; remote control, events, gossip and mingle are disabled.

## Running it

### Safe adoption in the development environment (operator, once, after review)

Not done in M3 development (the development database stays at 0002). With the Compose
files you normally use (`-f docker-compose.yml -f compose.local.yml`):

1. Recreate Redis without its host port first (`docker compose … up -d redis`: the
   Compose file no longer publishes it; check that nothing on the host relies on
   `localhost:6379`; coordinate with CE-3). Redis keeps no job state that matters:
   the job table is the outbox.
2. Back up the database (`pg_dump -Fc` from the `db` container).
3. `docker compose … build core-api worker worker-maintenance beat`
4. `docker compose … run --rm core-api alembic upgrade head --sql` — read the SQL.
5. `docker compose … run --rm core-api alembic upgrade head`, then `alembic current`
   (expect `0003 (head)`).
6. `docker compose … run --rm core-api python -m app.ingestion.admin init-storage --database studygraph`
   (add `--adopt-existing` if the volume or database already holds uploads).
7. `docker compose … up -d core-api worker worker-maintenance beat`

Workers refuse to start (exit 78) until step 5 is done; uploads answer 503 until step 6.

### Operator commands

```bash
docker compose exec worker python -m app.ingestion.admin preflight
docker compose exec worker python -m app.ingestion.admin init-storage --database studygraph
docker compose exec worker python -m app.ingestion.admin retry-parse --database studygraph <document-id>
```

`retry-parse` only accepts a `failed` document with a stored file and no succeeded or
active parse job; it creates a **new** job (the failed one stays as history) and sets
the document back to `pending`; the next sweep publishes it. The mutating commands take
their target only from `DATABASE_URL` (validated like migrations), require `--database`
to repeat the database name, and print no credentials.

## Tests

```bash
bash services/core-api/scripts/test_migrations.sh -v
```

The runner starts a disposable PostgreSQL **and** a disposable Redis (password, no
persistence, no published port) on an internal Docker network, with identity markers
written outside the test process; see `docs/database-migrations.md`. The M3 tests run
real Celery worker and Beat processes (`celery -A tests.worker_app`, the production app
plus a test-only fake-extractor hook) against them, each test on its own uniquely named
queues; nothing is ever flushed. Without the disposable servers, database and broker
tests are skipped and the rest (normalization, extraction subprocess, storage protocol,
configuration) still run. CI should call the runner (it needs Docker); installing both
services' requirements into one environment does not work (different FastAPI versions).

## Known limitations

- Text quality limits listed under Normalization; no OCR; encrypted PDFs rejected.
- Resource isolation is not a sandbox (see PDF extraction).
- Reconciliation reports orphans and missing files; it never repairs them.
- `flock`-based ownership and the pinned storage root assume one host, a local
  filesystem and a store nobody swaps out while the service runs.
- A frozen worker that resumes after its lease wastes its work (fenced off), and the
  attempt it used counts towards `max_attempts`.
- A new upload waits for the next dispatch sweep (up to `DISPATCH_INTERVAL_SECONDS`,
  10 s) before a worker sees it; a job whose message is lost waits up to
  `REDISPATCH_AFTER_SECONDS` plus one sweep.
- Broker operations are bounded by socket inactivity timeouts and attempt counts, not
  by a wall clock; the sweep's absolute bound is the Celery hard time limit.
- Reconciliation scans a random window of `RECONCILE_BATCH_SIZE` entries per run:
  coverage of a much larger store is probabilistic, not a guaranteed sweep.
- Language detection, page labels, header/footer removal, PPTX/DOCX: not in M3.

## Integration notes

### CE-2 (web)

- Statuses: `queued` → `processing` (parsing, **and** parsed while waiting for indexing)
  → `ready` once M4 indexes it, or `failed` with a user-readable `error` (table above).
  Until M4 exists, successfully parsed documents stay `processing`, so the materials
  screen keeps polling; please add a back-off.
- A new upload stays `queued` for up to about 10 s before a worker picks it up (the
  dispatch sweep publishes it), then `processing`.
- `pages` is filled once a document is parsed.
- New error on upload: `503 storage_unavailable` ("Uploads are temporarily
  unavailable. Try again later.") when the server's upload store is not available.
- No response shape changed; no new endpoint. A separate "parsed" indicator would be an
  API change for the team to agree.

### CE-3 (DevOps)

- New services `worker`, `worker-maintenance`, `beat` (same image as core-api, no
  ports). Workers need the `uploads` volume, `DATABASE_URL`, `REDIS_URL`, `UPLOAD_DIR`;
  Beat only `REDIS_URL`. One Beat instance only. `stop_grace_period` ≥ 210 s for the
  parse worker.
- Order: migrate (`run --rm core-api alembic upgrade head`), `init-storage` once, then
  start the workers. Workers exit with status 78 on a wrong schema revision or missing
  settings; a restart policy (`unless-stopped`) is reasonable for them.
- Resources: each parse slot may use up to `PARSE_MEMORY_LIMIT_MB` (512 MiB) in its
  extractor plus the worker process; about 1.5 GiB for the parse worker with
  concurrency 2. Workers need no Internet access in M3.
- Redis (minimal Compose change): the `ports: - "6379:6379"` mapping of the `redis`
  service is removed, nothing else. Services keep using `redis://redis:6379/0` over the
  Compose network. Anyone who needs host access publishes `127.0.0.1:6379:6379` in an
  untracked override. Production: authenticated Redis on a private network (Broker
  security).
- Workers' database: from `DATABASE_URL` only, validated like migrations; a loopback
  database address needs `MIGRATIONS_ALLOW_LOOPBACK_PORT`, and `PGHOSTADDR`,
  `PGSERVICE`, `PGOPTIONS` must not be set in worker containers.
- The core-api image upgrades pip and setuptools (pinned) before installing the
  requirements; rebuild it.
- CI: `bash services/core-api/scripts/test_migrations.sh -v` (Docker; Linux runners are
  fine). Keep core-api and ai-service dependencies in separate environments.
- Hardening for later: non-root containers (the volume is root-owned today).

### AI-1 (indexing) — proposal, not a contract change

- Read sections in order:
  `SELECT ordinal, text, locator FROM document_sections WHERE document_id = :id ORDER BY ordinal`.
  Source metadata is on `documents`: `course_id`, `origin`, `title`, `url`, `licence`,
  `language` (no detection yet: `en`).
- Locators are `{"page": n}`, 1-based physical pages. Section ids may change when a
  document is reparsed: reference `(document_id, page)` in chunks, not section ids.
- M4 hand-off: a queued `ingestion_jobs` row with `kind = 'index'` per parsed document.
  Proposed M4 flow: a CE-1 worker claims it (same fencing), builds the payload from the
  plan (document id, origin, course, title, URL, licence, language, sections with
  locators) and calls `POST /ai/v1/index/documents`; only then does the document become
  `indexed`. Changing `contracts/ai-service.openapi.yaml` needs one CE and one AI
  approval; M3 does not change it.


## M6 addendum: harvest worker role

A worker consuming exactly `HARVEST_QUEUE` (`harvest.run`) has the role `harvest`
(`app/ingestion/runtime.py`): it needs `DATABASE_URL` and `REDIS_URL`, the same
database identity and revision checks (now `0005`), and while `HARVEST_ENABLED` a valid
`HARVEST_USER_AGENT`; it needs no upload store and no AI service and makes no provider
request at start-up. Mixing the harvest or index queue with any other queue is refused.
The maintenance worker additionally runs `harvest.maintain` (Beat entry
`harvest-maintenance`): lease recovery and history cleanup always, scheduling and
dispatch only while `HARVEST_ENABLED`. See [external-harvesting.md](external-harvesting.md).
