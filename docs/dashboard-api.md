# Student dashboard API (M8)

`GET /api/v1/courses/{course_id}/dashboard?tz=<IANA zone>`

The caller's own learning data for one course, computed by core-api from committed records.
Deterministic, read-only and independent of any AI service: it works with the AI service down,
with mastery delivery off, and with every AI flag off. Implementation:
`app/dashboard/student.py`, `app/dashboard/recommendations.py`, `app/routers/dashboard.py`,
response schema `app/schemas/dashboard.py` (`DashboardResponse`, `extra="forbid"`).

## Access and flags

| Caller | Result |
|---|---|
| active student with a current `student` enrollment | 200 |
| the professor who owns the course | 403 `dashboard_not_allowed` |
| disabled account | 403 `account_disabled` |
| anyone else (other professors, admins, unenrolled or removed students), unknown course | 404 `course_not_found` |

Order: authentication (CE-3 dependency; no bypass exists, tests use dependency overrides),
authorization from the `users`/`courses`/`enrollments` rows, `DASHBOARD_ENABLED` (default
`true`; 503 `dashboard_unavailable` after authorization, so the flag never reveals whether a
course exists), the query string (only `tz`; any other parameter is 422 `invalid_query`), then
the snapshot. There is no user id in the path: only the caller's rows are ever read. After the
snapshot, access is checked **again in a fresh transaction** right before the body is sent; a
revocation in between turns the response into 404 and nothing is delivered.

Every response, errors included, carries `Cache-Control: no-store`. An unexpected error is the
generic 500 `server_error`; the log line holds the exception class and SQLSTATE only.

## Source of truth and snapshot

- Only **committed graded M7 practice attempts** (`attempts.quiz_item_id IS NOT NULL`). Legacy
  attempts (no assignment, no grading snapshot) are excluded. Exam attempts cannot exist in this
  set: an assignment's purpose is CHECKed to `practice`.
- All statements run in **one** `REPEATABLE READ READ ONLY` transaction with statement and lock
  timeouts. `as_of` is that transaction's database time. A concurrent commit is either entirely
  visible or not at all (tested by committing an attempt between two statements of the snapshot).
- A fixed number of bounded statements: topics ≤ 200 (`topics_truncated`), recent window ≤ 10 per
  topic, recent activity ≤ 10, 30 history days, 367 streak days, recommendations ≤ 6.

## Null semantics

No measurement is `null`, never `0`. `accuracy = correct / attempts`, `null` when `attempts = 0`;
counters may be 0. An unpractised topic has `accuracy`, `recent`, `baseline`, `prediction` and
`last_attempt_at` all `null`. An empty day has `attempts: 0, correct: 0, accuracy: null`.

## Time

- `tz`: an IANA zone name known to Python's tz database (`zoneinfo`, from the image's system tz
  data), default `UTC`, at most 64 characters. `localtime`, `Factory`, `posixrules` and the
  `posix/` and `right/` trees are refused (422 `invalid_timezone`).
- Day boundaries are computed **in Python** and passed to PostgreSQL as explicit UTC instants;
  PostgreSQL's tz database is never used, so differing tz database versions cannot shift a bucket.
- A local day `d` is `[start(d), start(d+1))`, `start(d)` = local midnight resolved with
  `fold=0`: the first instant whose local date is `d`. A DST day lasts 23 or 25 hours; in a
  spring-forward gap at midnight (e.g. `America/Havana`) the day starts at 01:00; in zones that
  fall back across midnight the repeated hour belongs to the day that started first.
- "Today" is the local date of `as_of`. `submitted_at` (the attempt transaction's start time) is
  used for day buckets only, never for ordering.

## Response

```json
{
  "as_of": "2026-11-21T09:00:00.123456Z",
  "course_id": "…",
  "window": {"tz": "Europe/Istanbul", "days": 30, "start": "2026-10-23", "end": "2026-11-21"},
  "totals": {"attempts": 42, "correct": 30, "accuracy": 0.7143},
  "topics": [{
    "topic_id": "…", "name": "Kinetics", "practiced": true, "attempts": 14, "correct": 9, "accuracy": 0.6429,
    "last_attempt_at": "…Z", "recent": {"window": 10, "correct": 5, "accuracy": 0.5},
    "baseline": {"kind": "baseline", "method": "cumulative_accuracy/1", "value": 0.6429},
    "prediction": null, "has_class_material": true
  }],
  "topics_truncated": false,
  "daily": [{"date": "2026-11-21", "attempts": 0, "correct": 0, "accuracy": null}],
  "recent_activity": [{"attempt_id": "…", "quiz_id": "…", "topic_id": "…", "topic_name": "Kinetics",
                       "correct": true, "submitted_at": "…Z"}],
  "streak": {"days": 4, "through": "2026-11-20", "capped": false},
  "activity": {"active_days_7": 3, "active_days_30": 11,
               "study_time": {"measurement": "client_reported_estimate", "available": false,
                              "minutes_7d": null, "minutes_30d": null, "by_day": null}},
  "review_schedule": [],
  "recommendations": {"basis": "rule_based/1", "items": [], "empty_reason": "no_rule_matched"}
}
```

| Field | Definition |
|---|---|
| `totals` | All-time graded attempts of the caller in the course (unweighted). Equals Σ `topic_practice_stats.attempts` of the caller in the course (tested). |
| `topics[]` | Every topic of the course, by name then id. `recent`: the topic's last ≤ 10 graded attempts by topic sequence (`stats_attempts`, never by time). `baseline`: the M7 deterministic `cumulative_accuracy/1`, never called mastery. `has_class_material`: an indexed class chunk of this course is linked to the topic. |
| `daily` | Exactly 30 local days ending today, empty days included. |
| `recent_activity` | Last 10 graded attempts: ids, topic, correctness, time. No question text, answer or key. |
| `streak` | Consecutive local days with ≥ 1 graded attempt, ending today, or yesterday when today has none yet (grace). Look-back 366 days before today; `capped` when the streak reaches it. Study events never count. |
| `activity.active_days_*` | Local days with ≥ 1 graded attempt among the last 7 / 30 days. |
| `activity.study_time` | Client-reported estimate only ([study-events.md](study-events.md)); `null` values when `STUDY_EVENTS_ENABLED` is off or nothing was reported. Never verified study time. |
| `prediction` | AI-2 model output, see below; `null` unless valid. |
| `review_schedule` | Topics whose valid prediction has a `next_review_at`, by time then topic id. Never derived from the baseline. |
| `recommendations` | Rule-based, see below. |

### Predictions (`prediction`)

```json
{"kind": "model", "mastery": 0.62, "model": {"name": "…", "version": "…"}, "through_seq": 14,
 "pending_events": 0, "stale": false, "received_at": "…Z", "next_review_at": "…Z"}
```

Shown only when **all** hold: the `mastery_scores` row has complete M8 provenance; its protocol,
model name, model version, state epoch and pseudonym key id equal the pins configured in this
deployment; `mastery_model_state` says `compatible` for exactly those pins (a blocked or
unconfirmed model hides every prediction); it was received within
`MASTERY_PREDICTION_MAX_AGE_DAYS` (30); and `through_seq` does not exceed the committed attempts.
`pending_events` = committed graded attempts of the topic − `through_seq`; `stale` when > 0.
`next_review_at` (and the review schedule) only for a fresh prediction with `pending_events = 0`.
Legacy `mastery_scores` rows (no provenance) are never shown. M7 attempt responses and their
idempotent replays are unchanged and keep showing the baseline only.

**Delivery off does not hide predictions.** `MASTERY_DELIVERY_ENABLED=false` pauses event
delivery; it does not withdraw the model. Predictions already validated and stored stay visible
under every guard above and turn `stale` (with `pending_events`, and without `next_review_at`) as
new attempts are committed; after `MASTERY_PREDICTION_MAX_AGE_DAYS` they disappear. To hide them,
the model state must be `incompatible` (an identity change, a state loss, or an operator decision)
or the pins must change.

### Recommendations (`rule_based/1`)

Computed at read time from the same snapshot; nothing is stored. Evidence is only the caller's own
committed graded attempts and the course's visible materials. Rules (`app/dashboard/recommendations.py`):

- **weak topic**: among the last ≤ 10 graded attempts of the topic, n ≥ 3 and correct/n < 0.6.
  `evidence_strength`: `limited` for n < 5, `moderate` otherwise.
- per weak topic (≤ 3, weakest first), ≤ 3 items in this order: `review_topic` (always),
  `class_material` (the indexed class document with most chunks linked to the topic),
  `practice_quiz` (only while `QUIZ_ENABLED` **and** `QUIZ_GENERATION_ENABLED`, i.e. when the
  student can actually start one; the target pre-fills the quiz form), `external_resource`.
- remaining slots (≤ 6 total): unpractised topics with class material (most material first):
  `practice_quiz` if available, else `class_material`, `evidence_strength: none`.
- cold start (no graded attempt): the same, ≤ 3. `empty_reason`: `no_material` (nothing to
  point to) or `no_rule_matched`.
- ties: recent accuracy, n (more evidence first), last attempt (newer first), topic name, id.
- `id`: first 16 hex characters of SHA-256 over type, topic, target and evidence: stable for the
  same evidence. `reason.text` is a template ("2 of your last 3 answers in Kinetics were
  incorrect."), never claims mastery; `reason.evidence.attempt_ids` are the caller's own attempts.

Visibility (`app/dashboard/materials.py`) reuses the M5/M6 rules: class documents are `indexed`,
`origin = 'class'` and of this course; external documents are `indexed`, `origin = 'external'`,
of this course, from an existing **enabled** source, part of a harvested resource of the same
course, with a non-stale version record. The older indexed version stays visible while its
replacement is pending (`current_document_id` is not required to point to it). Title, URL and
licence come from persisted records (`documents.title`, `external_document_versions.canonical_url`
and `licence`); the URL must be a safe `https` URL without credentials. No URL is generated.

AI-generated recommendations are deferred (`AI_RECOMMENDATIONS_ENABLED` cannot be enabled); the
proposed contract is [ai-recommendations-contract.md](ai-recommendations-contract.md).

## Backend-driven decisions (for CE-2)

The API shape follows backend correctness, not the current web types: `accuracy` and `minutes`
are nullable, baseline and model are separate fields, `trend` and point-biserial item statistics
do not exist. Adaptation notes: [frontend-dashboard-adaptation.md](frontend-dashboard-adaptation.md).
