# studygraph.mastery/1 — proposed AI-2 mastery integration (M8, not implemented)

**Status: PROPOSAL; nothing here is implemented in M7.** M7 shows a deterministic baseline
(`cumulative_accuracy/1`, `topic_practice_stats`) and never writes `mastery_scores`, which
stays reserved for the real AI-2 model. The current AI service mock for `/mastery/update`
returns fixed values, non-UUID topic ids, a made-up `next_review_at` and no `mock` field; a
core-api client following this proposal would refuse it.

## Events, not snapshots

Every committed M7 attempt is an immutable event (append-only, database-enforced). AI-2
consumes events and returns model outputs; core-api remains the source of truth for
attempts and for what is shown.

### GET /ai/v1/mastery/capabilities

```json
{"protocol": "studygraph.mastery/1", "service": "ai-service", "mock": false,
 "model": {"name": "...", "version": "..."}, "max_events": 100}
```

`mock` must be literally `false`; otherwise nothing is sent.

### POST /ai/v1/mastery/update

```json
{
  "protocol": "studygraph.mastery/1", "request_id": "<uuid>", "course_id": "<uuid>", "user_id": "<uuid>",
  "events": [
    {"event_id": "<attempt_id>", "topic_id": "<uuid>", "question_id": "<uuid>", "qtype": "mcq",
     "difficulty": "medium", "correct": true, "hint_count": 1, "response_ms": 5400,
     "telemetry_trusted": false, "submitted_at": "...Z"}
  ]
}
```

At most 100 events, one student and course per request. `hint_count` / `response_ms` are
client telemetry and explicitly untrusted.

Response:

```json
{"protocol": "studygraph.mastery/1", "request_id": "<echo>", "mock": false,
 "model": {"name": "...", "version": "..."},
 "applied_event_ids": ["..."], "duplicate_event_ids": ["..."],
 "scores": [{"topic_id": "<uuid>", "mastery": 0.62, "attempts_seen": 14, "next_review_at": null}]}
```

AI-2 must deduplicate by `event_id` (an attempt id): re-sending an event must not change
the result. `applied ∪ duplicate` must equal the request's event ids; `mastery ∈ [0, 1]`;
topics must be topics of the course; strict validation as for quizzes, all or nothing.

## Reliable delivery (M8 design)

1. **Transactional outbox.** M8 adds `mastery_outbox (attempt_id PK → attempts, user_id,
   course_id, created_at, delivered_at, attempts, last_error_code)`. The attempt transaction
   inserts the outbox row together with the attempt (same commit). M8's migration
   **backfills** the outbox from every existing M7 attempt (`quiz_item_id IS NOT NULL`), so
   attempts committed before M8 are not lost.
2. **Dispatch** on the existing maintenance worker: claim undelivered rows with
   `FOR UPDATE SKIP LOCKED` and a lease, group by (user, course), send ≤ 100 events, mark
   delivered on a valid response. At-least-once delivery; AI-2's `event_id` deduplication
   makes the effect exactly-once.
3. **No sequence cursor.** Commit order differs from sequence/timestamp order, so a
   "last seen id" cursor can skip events committed late; the outbox's per-row state avoids that.
4. **Results** go to `mastery_scores` with the model name/version and the method, plus a
   `[0, 1]` CHECK (M8 migration). The baseline stays in `topic_practice_stats`.
5. **Switch** `MASTERY_SOURCE=baseline|model` decides what the attempt response shows;
   until a model score exists for a topic, the baseline is shown with `kind: "baseline"`.
   `next_review_at` stays null until a model provides it.

## Decisions required

- AI-2 and CE approval of the protocol and its addition to the shared OpenAPI.
- Whether the model sees legacy (pre-M7) attempts (recommended: no; they have no
  assignment or grading snapshot).
- Retention of delivered outbox rows.
