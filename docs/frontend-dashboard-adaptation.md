# Frontend and mobile adaptation for the M8 dashboard and analytics

For CE-2 (web) and the mobile client. Core-api is the source of truth; these are the shapes the
backend serves ([dashboard-api.md](dashboard-api.md), [analytics-privacy.md](analytics-privacy.md),
[study-events.md](study-events.md)). Frontend and mobile files are not changed by M8.

## Dashboard (`GET /courses/{id}/dashboard?tz=…`)

- Send `tz` with the device's IANA zone (`Intl.DateTimeFormat().resolvedOptions().timeZone`);
  days, streaks and `daily[]` use it. Invalid zones answer 422 `invalid_timezone`.
- `accuracy` (totals, topics, recent, daily) can be `null`: show "no data yet", never `0 %`.
- The old `TopicMastery` splits into `baseline` (deterministic `cumulative_accuracy/1`, label it
  "accuracy so far", never "AI mastery") and `prediction` (nullable model output). Show a model
  value only when `prediction` is present; show "estimate updating" when `prediction.stale`.
- `trend` and `nextReviewAt` are not on the dashboard as baseline data: use
  `prediction.next_review_at` / `review_schedule` only, which are empty without a valid model.
- `activity.study_time` values are nullable and must be labelled "estimated, reported by your
  device"; hide them when `available` is false. Do not treat them as verified time.
- `recommendations.items[]`: `reason` is an object (`code`, `text`, `evidence`), there is no
  `minutes`; label `basis: rule_based/1` as "suggested by rules" and `evidence_strength: limited`
  as "based on few answers". `practice_quiz` targets pre-fill the quiz form (`topic_ids`,
  `difficulty`); document targets carry `document_id`, `title`, and for external pages `url`,
  `licence`, `provider` (show the licence/attribution). Show `empty_reason` when there are none.
- Optional telemetry (only while study events are enabled): `recommendation_shown/opened/dismissed`
  with the item `id`, and `focus_interval` events; retry with the same `client_event_id` and payload.

## Analytics (`GET /courses/{id}/analytics`, owner professor)

- 503 `analytics_unavailable` is the default until the privacy review: show "not available yet".
- No filters or date pickers: weeks are fixed completed UTC weeks; label them "week of <date>,
  UTC, released <released_at>". `pending_release` weeks show "not yet published".
- Cells: `released` → `accuracy` (rounded to 5 %), `contributors` and `attempts` as **ranges**;
  `withheld` → "hidden for privacy" (show no reason and no count).
- `hardestItems` is removed: `item_analysis.available` is `false`; show its explanation instead.
- Show the `notice` text near the data (aggregates are not anonymous).
