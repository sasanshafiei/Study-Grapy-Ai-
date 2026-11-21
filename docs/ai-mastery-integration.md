# AI-2 mastery integration: studygraph.mastery/1 (M8)

**Status: core-api side implemented; delivery OFF by default (`MASTERY_DELIVERY_ENABLED=false`).**
The protocol is a **proposal** until AI-2 and CE approve it; the shared OpenAPI contract
(`contracts/ai-service.openapi.yaml`) and the AI service are not changed by M8. The current
AI-service mock for `/mastery/update` is refused (no literal `mock: false`, made-up fields).
No real AI request, provider traffic or model activation happens in M8.

Ownership: core-api owns the attempts (immutable events), the delivery state and the stored
predictions. The AI service computes predictions; it **never writes** `mastery_scores`,
analytics or any other core table (team agreement needed: the capstone plan and CONTRIBUTING
describe AI writing those tables directly; M8 rejects that).

## Events: the transactional outbox

- Every committed graded M7 attempt is one event; **the event id is the attempt id**.
- `attempts_outbox_enqueue` (AFTER INSERT of a graded attempt) inserts the `mastery_outbox` row and,
  if needed, the learner's `mastery_streams` row **in the attempt's own transaction**: attempt,
  practice statistics and event commit or roll back together (tested). M7 code, response schemas,
  grading snapshots and idempotent replays are unchanged; a replay inserts nothing, so it creates
  no event (tested through the M7 API). Events are written whatever `MASTERY_DELIVERY_ENABLED`
  says; nothing is lost while delivery is off.
- **Per-topic order** is `attempts.stats_attempts` (`topic_seq`): the M7 statistics upsert locks
  the (student, topic) row until commit, so the numbers are gapless and in commit order. Revision
  0007 enforces it: UNIQUE `(user_id, topic_id, stats_attempts)` for graded attempts and
  `attempts_mastery_sequence_guard`, which refuses a graded attempt whose counters are not exactly
  its statistics row's or that does not continue the previous graded attempt of the topic. The
  outbox row's composite key names the attempt's own `(id, user_id, course_id, topic_id,
  stats_attempts)`.
- **Cross-topic order is approximate.** Events of one request are listed by `submitted_at`
  (transaction start), with each topic's events in `topic_seq` order in the positions that topic
  occupies. Order-sensitive cross-topic models (e.g. DKT over a global sequence) are **not
  supported** without a revised contract (a course-level lock in M7 would change its semantics).
- Delivered, pending and failed outbox rows are all **kept** in M8. Automatic deletion of
  delivered rows waits for approved reconciliation and retention policies.
- Revision 0007 backfilled one event per graded attempt that existed before it, under a table
  lock that M7 transactions wait for ([database-migrations.md](database-migrations.md)).

## Delivery: streams and durable batches

One stream per (student, course), at most **one active batch**:

1. **Maintenance tick** (`mastery.maintain`, maintenance queue, every 30 s, database only):
   recovers expired leases (always), resumes paused streams whose access is back (always), and —
   only while delivery is enabled, the pins are complete and the model is not blocked —
   reserves due streams (`FOR NO KEY UPDATE SKIP LOCKED`, `last_dispatched_at`) and publishes
   `{"user_id": "<uuid>", "course_id": "<uuid>"}` to `mastery.deliver`. Redis carries identifiers
   only; a lost message is published again after `MASTERY_REDISPATCH_AFTER_SECONDS`; a failed
   publication releases its own reservations.
2. **Dedicated mastery worker** (`worker-mastery`, queue `mastery.deliver`, concurrency 2): slow
   model requests never block ingestion, indexing or harvest maintenance. No host port, no upload
   volume. Start-up checks: database at revision 0007, `AI_SERVICE_URL`, and while enabled
   `AI_SERVICE_TOKEN` and the complete pins. No AI request at start-up.
3. **Per task** (`app/mastery/runner.py`): capability probe (cached per process, outside any
   transaction); **claim** in one short transaction (stream `FOR NO KEY UPDATE SKIP LOCKED`,
   database wall-clock `clock_timestamp()` read *after* the lock): if the stream has no active batch,
   the batch is created first — per topic a contiguous prefix of pending events starting right
   after the last delivered `topic_seq`, ≤ min(`MASTERY_MAX_BATCH_EVENTS`, advertised
   `max_events`) — and its **identity (`batch_id`, also the request id), epoch and exact membership
   (`mastery_outbox.batch_id`, immutable by trigger) are committed before anything is sent**. The
   claim stores a fresh, renewable `claim_token` and a lease (`mastery_lease_seconds` = task hard
   limit + margin). A gap in a topic's sequence **blocks** the stream; an earlier undelivered event
   is never skipped for a later one (head-of-line blocking is intended).
4. Access is re-checked in a fresh transaction **right before sending** (active student, current
   enrollment); if revoked, the stream is `paused`, nothing is sent and the batch is kept.
5. One POST, no automatic retry, no transaction open. Then strict validation and **one
   persistence transaction**: lock order `mastery_model_state` (FOR SHARE) → stream → outbox rows
   → scores; the claim token, batch and **lease (`clock_timestamp()` after the locks)** must still
   be current, otherwise the completion is stale and changes nothing. Predictions are upserted only
   if they continue the prior accepted state (first `topic_seq` of the batch = stored `through_seq`
   + 1 in the same epoch) and never overwrite a higher `through_seq`; events become `delivered`
   with their `applied`/`duplicate` outcome; the stream returns to `ready` without a batch.

**Expired claims change nothing.** Every exit of a claim — completion, failure, block, pause,
model suspension — first locks the stream row on its state, claim token and batch, then checks
the lease with `clock_timestamp()` in a separate statement after that lock. A claim whose lease
expired (even if recovery has not run yet) cannot pause or release the stream, count a failure,
block it, mark the model incompatible or overwrite a newer claim; a late response (including a
late state mismatch) is ignored and lease recovery counts the failure once.

**Retries never change a batch.** A timeout, lost response, transport or HTTP error, protocol
violation, commit uncertainty or dead worker leaves the batch in place; the next claim resends the
**same batch id and events** (byte-identical; features come from immutable attempt and question
rows). Events committed meanwhile wait for the next batch (tested for each case).

**Lock order** (also in `app/models.py`): users → courses → enrollments → mastery_model_state →
mastery_streams → mastery_outbox → mastery_scores. A graded attempt only inserts new outbox rows
and inserts its stream row `ON CONFLICT DO NOTHING`; delivery locks streams `FOR NO KEY UPDATE`,
which does not conflict with the outbox key's `KEY SHARE` check, so an attempt never waits for a
delivery (tested: an attempt commits while a request is in flight).

**Counters and bounds.** `dispatch_count`, `claim_count`, `batch_claims` (≤
`MASTERY_MAX_BATCH_CLAIMS`, then `claim_limit_exhausted`) and `failure_count` (actual delivery
failures of the current batch, ≤ `MASTERY_MAX_FAILURES`) are separate. Backoff:
`base · 2^(n−1)` capped at `MASTERY_BACKOFF_MAX_SECONDS`, ±20 %. An expired lease counts as a
failure. Exhaustion → `blocked` (terminal), the batch's events `failed` (kept). An unavailable AI
service is detected by the probe before any claim and consumes nothing. Stored error codes are
sanitized to `^[a-z][a-z0-9_]{0,39}$`.

**Operator seams for M10** (`app/mastery/admin.py`, no endpoint in M8, admin actor required,
logged): `retry_stream` (blocked/paused → ready with the SAME batch, counters of the batch reset,
`operator_retry_count`, `last_operator_retry_at`, `last_operator_actor_id` recorded) and
`clear_model_block` (`incompatible` → `unknown`).

**At-least-once** is the guarantee. Exactly-once *effect* requires the AI service to deduplicate by
`(state_epoch, learner_ref, event_id)` — a **required external contract** that core-api cannot
enforce. A lost response is completed by the retry, which the service must answer with the events
as `duplicate_event_ids` and its current scores.

## Protocol

Both endpoints use the M5/M7 transport: bearer `AI_SERVICE_TOKEN`, `X-StudyGraph-Protocol`,
`trust_env=False` (no environment proxies), no redirects, identity encoding, exactly one
Content-Length, byte limits (`MASTERY_MAX_REQUEST_BYTES`, `MASTERY_MAX_RESPONSE_BYTES`), absolute
deadlines, strict JSON (no repeated keys, no NaN), no automatic retry, logs with fixed codes only
(never bodies, ids, pseudonyms or the key). Task failures are logged as class + SQLSTATE only.

### `GET /ai/v1/mastery/capabilities`

```json
{"protocol": "studygraph.mastery/1", "service": "ai-service", "mock": false,
 "model": {"name": "<pinned>", "version": "<pinned>"}, "state_epoch": "<pinned uuid>",
 "max_events": 100, "ordering": "per_topic"}
```

`compatible` only with exactly these keys, `mock` literally `false` (missing, null, `"false"` or 0
are refused), and the pinned protocol, model name, version and state epoch. A mismatch marks
`mastery_model_state` `incompatible` (sticky; only `clear_model_block` leaves it), which stops
dispatch and claims and hides every prediction. Probe outcomes are fenced by database time: an
older probe never overwrites a newer one, and a cached result can only ever block, never unblock.

### `POST /ai/v1/mastery/update`

Request (one student, one course, one batch):

```json
{"protocol": "studygraph.mastery/1", "request_id": "<batch uuid>", "state_epoch": "<pinned uuid>",
 "pseudonym_namespace": "<deployment>", "course_ref": "<64 hex>", "learner_ref": "<64 hex>",
 "events": [{"event_id": "<attempt uuid>", "topic_id": "<uuid>", "topic_seq": 3, "qtype": "mcq",
             "difficulty": "easy", "correct": true, "hint_used": 0, "response_ms": 5400,
             "telemetry_trusted": false, "submitted_at": "…Z"}]}
```

`learner_ref = HMAC-SHA256(MASTERY_PSEUDONYM_KEY, "studygraph.mastery/1|learner|<namespace>|<user>|<course>")`,
`course_ref` likewise. Never sent: user or course ids, names, e-mail, answers, answer keys,
question ids or text, passages. `hint_used`/`response_ms` are client telemetry, marked untrusted.

Response (all or nothing):

```json
{"protocol": "studygraph.mastery/1", "request_id": "<echo>", "state_epoch": "<echo>",
 "course_ref": "<echo>", "learner_ref": "<echo>", "mock": false,
 "model": {"name": "<pinned>", "version": "<pinned>"},
 "applied_event_ids": ["…"], "duplicate_event_ids": ["…"],
 "scores": [{"topic_id": "<uuid>", "mastery": 0.62, "attempts_seen": 14, "through_seq": 14,
             "next_review_at": null}]}
```

Rules: exact keys; echoed identities; pinned model; applied and duplicate ids canonical, without
repeats, disjoint, together exactly the batch; exactly one score per batch topic; `mastery` a
finite number in [0, 1] (bools refused); `through_seq` = the batch's last `topic_seq` of the topic;
`attempts_seen` = `through_seq` (lower → **state mismatch**, higher → violation); `next_review_at`
null or RFC 3339 with offset within [now − 1 day, now + 365 days] of database time. An identity
violation — the response names another protocol, model, version or state epoch than the probe
confirmed, or claims to be a mock — suspends the model globally (`mastery_model_state` becomes
`incompatible` with reason `response_<violation>`, sticky until `clear_model_block`), hides every
prediction and releases the stream to `ready` **with its batch and without counting a failure**,
so the same batch is resent after reconciliation. Only a **well-formed** value that explicitly
differs counts: a malformed identity field (`model` null, `{}`, a missing or non-string name or
version, an invalid model field, a malformed protocol or state epoch, a non-boolean `mock` such as
null or `"true"`) establishes no identity change and is rejected as `*_malformed`, an ordinary
failure of the batch. Any other violation (echoed request id or
references, accounting, scores, sequences, review times) is a failure of this batch only (same
batch retried, backoff, `blocked` after `MASTERY_MAX_FAILURES`), never a global block. A state mismatch stores nothing, blocks the stream and
marks the model `incompatible` (`ai_state_mismatch`): every stream stops, predictions are hidden,
all events are retained.

## Pins, epochs and state loss

Pinned in the environment (all required while delivery is enabled; malformed values refuse start):
`MASTERY_MODEL_NAME`, `MASTERY_MODEL_VERSION`, `MASTERY_STATE_EPOCH` (UUID),
`MASTERY_PSEUDONYM_NAMESPACE` (per deployment; pseudonyms of two deployments differ even with one
key), `MASTERY_PSEUDONYM_KEY` (≥ 32 characters, secret, never logged; its keyed id is stored with
each prediction, a rotation hides older predictions). Nothing new is accepted automatically: a
different model, version or epoch, a mock, or lost remote state blocks delivery safely, hides
incompatible predictions and keeps every event. Predictions carry full provenance (protocol,
model, version, epoch, key id, `through_seq`, batch, `received_at`); legacy rows without it are
preserved and never shown as model output. A changed pin (e.g. a reviewed version upgrade) hides
predictions made under the old pin until new ones arrive; a batch built for another epoch is blocked
(`epoch_changed`); a delayed response of an old epoch is rejected (echo and pin checks).

**Reconciliation and epoch transitions (M10 requirement, not implemented):**

1. Stop dispatch (`MASTERY_DELIVERY_ENABLED=false` on worker-maintenance) and wait until no stream is
   `delivering` (or let the leases expire and recovery run), so no in-flight work survives.
2. Agree the new epoch with AI-2; the AI service starts an empty state for it.
3. Replay from the immutable attempts with an audited, bounded job that rebuilds outbox membership
   for the new epoch in per-topic order from `topic_seq = 1`. The M8 outbox guard forbids rewriting
   delivered rows, so this needs its own reviewed procedure (likely an epoch column on the outbox).
4. Update `MASTERY_STATE_EPOCH`, call `clear_model_block`, re-enable dispatch. A probe started
   before the switch cannot overwrite the new state (database-time fence); responses carry the
   epoch and are checked against the pin.

No automatic reset or full-corpus replay exists in M8.

## Operations

- Flags: `MASTERY_DELIVERY_ENABLED` on **worker-maintenance** (dispatch) and **worker-mastery**
  (claims and requests). Outbox rows are written by the database regardless.
- Observe: `mastery_streams` (`state`, `last_error_code`, `failure_count`, `batch_claims`),
  `mastery_outbox` (`status`, `outcome`), `mastery_model_state` (`status`, `reason`). Logs carry
  `batch_id` and fixed codes only.
- A blocked stream: inspect `last_error_code`; fix the cause; `retry_stream` with an admin actor.
- A blocked model: inspect `reason`; reconcile; `clear_model_block`.
- `BROKER_VISIBILITY_TIMEOUT_SECONDS` must exceed twice `MASTERY_TASK_TIME_LIMIT_SECONDS` (validated);
  exactly one Beat schedules both M8 ticks.

## Activation prerequisites

1. AI-2 implements this protocol (non-mock, deduplication by epoch/learner/event, duplicates
   answered with current scores, per-topic ordering) and CE + AI approve it for the shared OpenAPI.
2. Team agreement that AI never writes core tables; model identity, version and epoch agreed and pinned.
3. Pseudonym namespace and key provisioned per deployment (secret store), `AI_SERVICE_TOKEN` provisioned.
4. Revision 0007 applied; worker-mastery deployed; `MASTERY_DELIVERY_ENABLED=true` on
   worker-maintenance and worker-mastery.
5. Retention and reconciliation policies approved before any delivered-row cleanup is added.
