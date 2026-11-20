# Courses, topics and enrollment API (M5)

`GET /api/v1/courses` and `GET /api/v1/courses/{course_id}/topics` implement CE-2's
contract (`contracts/core-api.web.md`, `apps/web/src/lib/api/types.ts` on the
`ce2/web-app` branch). `POST /api/v1/courses` and `POST /api/v1/courses/join` are
**CE-side public API additions**: CE-2's files do not define them yet; this page is
their contract. Chat is in [ai-chat-integration.md](ai-chat-integration.md).

All endpoints need `Authorization: Bearer …` (the CE-3 dependency fails closed until
JWT exists). Errors are `{"detail": "<for the user>", "code": "<stable code>"}`.
Role and status are always read from the `users` row, never from the token.

## Shapes

`Course` (snake_case of the web type):

```json
{"id": "uuid", "code": "CHEM 101", "name": "General Chemistry",
 "professor_name": "Prof. Ada", "join_code": "K7Q2M9TB", "student_count": 42}
```

- `join_code` is the real code **only for the professor who owns the course**; every
  other caller gets `""` (the key is always present, as the web type expects).
- `code` is `""` for courses created before revision 0004 (**empty-code fallback**:
  no code was recorded; the web app may show the name alone).
- `student_count` counts enrollments with role `student` whose user is an active
  student (disabled accounts, other enrollment roles and non-student users are not
  counted).

`Topic`: `{"id": "uuid", "name": "Stoichiometry"}`.

## `GET /api/v1/courses`

- active professor: the courses they own (with join codes);
- active student: the courses they hold a `student` enrollment in;
- admin or any other role: `[]` (no implicit course access).
One statement for the whole list (no N+1), ordered by name then id, never truncated.
401 unauthenticated or unknown user, 403 disabled.

## `GET /api/v1/courses/{course_id}/topics`

Readers of the course (owner professor, enrolled student). Ordered by name. 404 for a
missing course and for a course the caller cannot read (ids cannot be probed).

## `POST /api/v1/courses` (professors)

Body exactly `{"name": "...", "code": "..."}` (JSON, ≤ 4 KiB, no compression). Both
are trimmed; `name` 1–150, `code` 1–32 characters, no control characters.
`201` with the `Course` (including its new join code). 403 `course_create_not_allowed`
for non-professors, 422 for invalid fields, 400/413/415 for invalid bodies. The caller
is authorized before the body is read.

Join codes: 8 characters from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (no look-alikes), drawn
with `secrets` (~8.5·10¹¹ codes). A collision with an existing code (exact, or the
same letters in another case) is retried up to 5 times, then 503
`join_code_unavailable`.

## `POST /api/v1/courses/join` (students)

Body exactly `{"join_code": "..."}`. Whitespace around it is ignored; lookups try the
code exactly as typed first (legacy codes may be lower case), then its upper-case form
without spaces or hyphens (`k7q2-m9tb` → `K7Q2M9TB`).

| Result | Status |
|---|---|
| enrolled now | `201` + `Course` (join code `""`) |
| already enrolled as a student | `200` + `Course` (idempotent) |
| already in the course with another enrollment role | `409 enrollment_conflict` (nothing changes) |
| no course with that code, or its owner is not an active professor | `404 join_code_not_found` |
| caller is not an active student | `403 join_not_allowed` / `account_disabled` |
| malformed code | `422 invalid_join_code` |
| more than `JOIN_RATE_LIMIT_ATTEMPTS` attempts per `JOIN_RATE_LIMIT_WINDOW_SECONDS` | `429 too_many_join_attempts` + `Retry-After` |

Concurrency: the caller, course and owner rows are locked `FOR SHARE` and the
enrollment is inserted with `ON CONFLICT (user_id, course_id) DO NOTHING`, so
simultaneous joins create exactly one row; the losers see the committed row and get
`200` only if it grants student access.

Rate limit: counted per user before anything else, successful attempts included.
**Single-process implementation** (memory bounded by `JOIN_RATE_LIMIT_MAX_USERS`):
with several API processes or replicas each enforces its own window, so a shared
(e.g. Redis) limiter is required before a multi-replica deployment.

## Not included

Leaving a course, removing students, rotating or revealing join codes to anyone but
the owner, course editing, admin course access.
