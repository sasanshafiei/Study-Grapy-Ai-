# Study activity events (M8)

`POST /api/v1/courses/{course_id}/study-events`

**Status: implemented, OFF by default (`STUDY_EVENTS_ENABLED=false`).** While off, the endpoint
answers 503 `study_events_unavailable` after authorization and the dashboard reports no study
time. Implementation: `app/dashboard/study_events.py`, table `study_events` (revision 0007).

These events are **untrusted client telemetry**. They never affect grading, mastery, streaks,
recommendation ranking or professor analytics, and study time derived from them is always
labelled `client_reported_estimate` — never verified study time. A recommendation event is not
evidence that a recommendation was issued.

## Access and limits

Active student with a current `student` enrollment (the owning professor 403
`study_events_not_allowed`, others 404). The authorization is re-checked inside the write
transaction with `FOR SHARE` locks on the user, course and enrollment rows (the M7 rule). Body
≤ `STUDY_EVENTS_MAX_BODY_BYTES` (16 KiB, bytes actually received), received within 10 s;
`{"events": [...]}` with 1..`STUDY_EVENTS_MAX_BATCH` (25) events. No query parameters.

## Event schema (allowlist, exact keys)

| type | fields |
|---|---|
| `document_viewed` | `client_event_id`, `type`, `occurred_at`, `document_id` |
| `recommendation_shown` / `_opened` / `_dismissed` | `client_event_id`, `type`, `occurred_at`, `recommendation_id` (16 hex), `recommendation_type` (`review_topic` \| `practice_quiz` \| `class_material` \| `external_resource`), `topic_id` (or null), `target_document_id` (or null) |
| `focus_interval` | `client_event_id`, `type`, `started_at`, `ended_at`, `surface` (`document` \| `quiz` \| `dashboard` \| `chat`), `topic_id` (or null), `document_id` (or null) |

No free text, URL, user agent, browser history or other browser data is accepted or stored. Ids
are lower-case canonical UUIDs. A topic must belong to the course; a document must be visible to a
student of the course (the class/external rules of [dashboard-api.md](dashboard-api.md)).

**Validation is atomic**: one invalid event rejects the whole batch with 422
(`invalid_study_event`, with the event's `index`) and nothing is stored.

## Time

Timestamps are RFC 3339 with an explicit offset (`Z` or `±hh:mm`); naive times are refused. Each
timestamp given — `occurred_at`, and for a focus interval `started_at` **and** `ended_at`
independently — must lie within `[now − 24 h, now + STUDY_EVENTS_FUTURE_SKEW_SECONDS (120 s)]`,
`now` being database time (`clock_timestamp()`). A focus interval needs `ended_at > started_at`
and lasts at most `STUDY_EVENTS_MAX_INTERVAL_SECONDS` (30 min). Its stored `occurred_at` is
`ended_at`. A database CHECK repeats looser bounds (25 h, 10 min, 1 h).

## Idempotency

`(student, client_event_id)` is unique. The fingerprint is the SHA-256 of the canonical event
(course, type, fields; timestamps normalized to UTC microseconds, so the same instant written with
another offset is the same event). The same id with the same fingerprint is reported as
`duplicate`: nothing is written and **no quota is used**. The same id with another fingerprint
(other payload or other course) is 409 `event_id_conflict` for the whole batch. Two events of one
batch with the same id are 422 `duplicate_event_ids`.

Response: `{"accepted": n, "duplicates": m, "results": [{"client_event_id", "status"}],
"measurement": "client_reported"}`.

## Quota (authoritative, database-backed)

`STUDY_EVENTS_DAILY_QUOTA` (2000) new events per student and UTC day (`received_at`), across every
API process and replica: the write transaction takes `pg_advisory_xact_lock` on the student, counts
that day's rows through the index `study_events (user_id, received_at)`, and refuses the whole
batch with 429 `study_event_quota_exceeded` if it would exceed the quota (tested with three
concurrent API instances). No separate quota table is needed for this design. A process-local
rate limit could be added as extra protection; it would not be authoritative.

## Study-time estimate

For the dashboard window (30 local days), the focus intervals are clipped to the window and to
`as_of` (no future time is counted), merged with `range_agg` (overlaps — e.g. two devices — are
counted once), intersected with each local day's `[start, end)` from [dashboard-api.md](dashboard-api.md)
(so an interval crossing midnight or a DST change is split correctly), and summed. Per day `null`
when nothing was reported that day; totals `null` when nothing was reported in the window.

## Retention

Maintenance (`analytics.maintain`, every 15 min) deletes rows received more than
`STUDY_EVENTS_RETENTION_DAYS` (180, minimum 2) ago, `STUDY_EVENTS_CLEANUP_BATCH` per tick, whatever
the flag says. Idempotency implication: an event can only be accepted within 24 h of its
timestamps, and its row lives far longer, so every acceptable replay still finds its original;
after deletion the id could in principle be reused by a client, but such an event would be
refused as too old anyway. Deleting a document deletes its study events (`ON DELETE CASCADE`).

## Activation prerequisites

1. Privacy approval of the collected fields, retention and the "estimate" labelling.
2. `STUDY_EVENTS_ENABLED=true` on core-api (the dashboard shows study time only while it is on).
3. CE-2/mobile send canonical UUIDs per event and retry with the same id and payload.
