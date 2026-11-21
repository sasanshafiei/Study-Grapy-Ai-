# Professor analytics and privacy (M8)

`GET /api/v1/courses/{course_id}/analytics` — no parameters.

**Status: implemented, OFF by default (`ANALYTICS_ENABLED=false`) pending a privacy review.**
While off, the endpoint answers 503 `analytics_unavailable` after authorization and maintenance
generates no release. Implementation: `app/dashboard/analytics.py`, schema `AnalyticsResponse`.

**This is suppression, rounding and banding. It is NOT anonymisation, NOT k-anonymity in the
formal sense and NOT differential privacy.** A minimum group size of five does not make
individual results impossible to infer; the residual risks below remain.

## Access

| Caller | Result |
|---|---|
| active professor who owns the course | 200 (503 while the flag is off) |
| enrolled student | 403 `analytics_not_allowed` |
| other professors, admins, unenrolled users, unknown course | 404 `course_not_found` |

Any query parameter is refused (422 `invalid_query`): no date range, filter, topic subset or
student subset exists by design. Access is re-checked in a fresh transaction before delivery;
`Cache-Control: no-store`.

## Policy `analytics_release/1`

- **Unit**: fixed, non-overlapping, completed UTC ISO weeks `[Monday 00:00 UTC, next Monday)`.
  The current week is never released. A week is publishable once it ended at least
  `ANALYTICS_PUBLICATION_DELAY_SECONDS` ago (default 6 h, minimum 1 h). The endpoint lists the
  latest `ANALYTICS_WEEKS` (12) publishable weeks that ended after the course was created.
- **Population**: historical participation — every student with a committed graded M7 practice
  attempt (`submitted_at`) in the week, still enrolled or not. Current enrollment is not used, so
  a student leaving does not change a released week (tested). Legacy and exam attempts are
  excluded. Study events (client telemetry) are never used.
- **Releases are immutable.** The maintenance tick (`analytics.maintain`, maintenance queue,
  every 15 min, database work only, no upstream call) generates missing releases of publishable
  weeks, at most `ANALYTICS_RELEASE_BATCH_SIZE` per tick, each in its own `REPEATABLE READ`
  transaction, `INSERT … ON CONFLICT (course_id, week_start) DO NOTHING` into
  `analytics_weekly_releases`. A trigger refuses every UPDATE; a CHECK refuses a week that is not
  a Monday, a release created before its week ended and a minimum group size below 5. The stored
  payload is already filtered; its SHA-256 is stored alongside. History is never silently
  overwritten. The endpoint only reads releases; a missing one is shown as `pending_release` and
  is never computed live.
- **Late data**: an attempt that commits after its week was released (possible only for a
  transaction that started before the week ended and committed after the publication delay) is
  never added to that release and never counted in another week (weeks are by `submitted_at`).
  Deleting a release deliberately would let the next tick regenerate it from current data.
- **One breakdown**: an overall cell per week and one topic breakdown (topics of the course at
  generation time, ≤ 200). No difficulty breakdown, no topic × difficulty, no cumulative
  course-to-date total (which could be differenced across releases), no per-week exact counts.

### Cells

A cell is `released` only if **all** hold, otherwise `withheld`:

1. at least `ANALYTICS_MIN_GROUP_SIZE` (default 5, never below 5) **distinct** contributing students
   (5 attempts by one student are one contributor);
2. dominance: no student contributes > 50 % of the cell's attempts, no two together > 75 %;
3. no homogeneity: at least two students' per-student accuracies differ from the most common value;
4. not extreme: the student-weighted accuracy lies in [0.10, 0.90].

Then **complementary protection**: when the overall cell is released and exactly one topic with
contributors is withheld, the released topic with the fewest contributors is withheld too; when
the overall cell is withheld, every topic cell is.

A released cell carries `accuracy` (mean of per-student accuracies — student-weighted, so a heavy
user does not dominate — rounded to 0.05), `contributors` as a band (`5-9`, `10-19`, `20-49`,
`50-99`, `100+`) and `attempts` as a band (`5-24`, `25-99`, `100-499`, `500+`). No `correct`
count, no exact count, no student id, per-student row, score, answer, question text or individual
mastery exists anywhere in the response schema. A withheld cell is only `{"status": "withheld"}`:
no count and no specific reason (a reason such as "extreme" would itself disclose the outcome);
"no data" and "fewer than five" are indistinguishable on purpose.

### Item analysis: unavailable

`item_analysis = {"available": false, "reason": "no_shared_items"}`. Every M7 question is
generated for one session of one student (`UNIQUE quiz_items.question_id`): no question has
independent answers from several students, so per-item difficulty or point-biserial values would
be fabricated. Grouping questions by stem similarity or `content_hash` is refused (unreliable, and
it would weaken assignment integrity). Prerequisite: a shared, approved question bank with a
stable item identity answered independently by enough students (M9 or later), plus these rules.

## Residual inference risks (documented, not solved)

- **Side knowledge**: a professor who knows who practised in a week (e.g. only five students
  attended a session) learns about that group; with exactly five contributors and a known four,
  the fifth student's contribution is constrained by the band and the rounded mean.
- **Small classes**: most cells of a small course are withheld; what is released is still about
  very few people.
- **Repeated weeks**: weekly releases are disjoint, but a student who practises alone in a topic
  for many weeks is withheld every week, which itself signals low participation.
- **Rounding and bands** leak coarse information by design; they are not noise.
- **Topic lists**: a topic created after a release is absent from it; topic names are read live.
- **Collusion**: students sharing their own results with the professor reduce everyone else's
  protection.

Deferred: differential-privacy noise, wider banding for 5 ≤ n < 10, per-course opt-in by
students. A privacy review must accept these limits before `ANALYTICS_ENABLED` is turned on.

## Activation prerequisites

1. Privacy review approves this policy (threshold, rules, residual risks) and the notice text.
2. `ANALYTICS_ENABLED=true` on core-api **and** worker-maintenance (which generates releases).
3. Revision 0007 applied; the maintenance worker and the single Beat running.
4. CE-2 renders cells per [frontend-dashboard-adaptation.md](frontend-dashboard-adaptation.md).
