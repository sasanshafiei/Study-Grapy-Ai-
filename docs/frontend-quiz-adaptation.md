# Quiz and attempts: adaptation notes for web and mobile (CE-2)

The backend contract ([quiz-api.md](quiz-api.md), core-api quiz v1) is authoritative. The
earlier CE-2 types were used as a reference; these are the differences the clients need to
handle. Nothing in M7 changes `apps/web` or `apps/mobile`.

1. **Choices are objects, answers are choice ids.** `choices: [{id, text}]`; submit
   `answer: "<id>"`, never the text (texts can repeat or be normalized differently). True/false
   ids are `"true"` / `"false"`. Render choices in the order received; do not reshuffle.
2. **Envelope, not an array.** POST/GET quiz return `{quiz_id, status, scope, difficulty,
   count, created_at, completed_at, failure, questions: [...]}`. Each question has
   `position`, `topic_id`, `topic_name`, `type`, `difficulty`, `stem`, `choices`, `hint`,
   and `attempt` (null until answered).
3. **Idempotency-Key header** on POST quiz: generate a UUID (lower case) when the student
   asks for a quiz; keep it for retries of that request; a new one for the next quiz.
4. **Attempt request** adds `quiz_id`: `{quiz_id, question_id, answer, hint_count, response_ms}`;
   `hint_count` is 0 or 1 (one hint per question in M7); `response_ms` may be null.
5. **Statuses to handle:** quiz 201 (new) / 200 (replay) / 409 `quiz_generation_in_progress`
   (wait `Retry-After`, retry the same key or GET) / 409 `idempotency_key_reused` (a client
   bug: new key) / 429 `quiz_generation_busy` / 429 `quiz_quota_exceeded` / 502 `quiz_rejected`,
   503 `quiz_ai_unavailable` / `quiz_generation_failed` / `quiz_generation_unavailable`,
   504 `quiz_timeout` (with `quiz_id` and `retry: "new_idempotency_key"` when a session
   failed). Attempts 201 / 200 (`replayed: true`) / 409 `already_answered` / 422
   `invalid_answer` / 404 `question_not_found` / 503 `quiz_attempts_unavailable`.
   Professors get 403 `practice_not_allowed` on all practice endpoints.
6. **Lost responses:** if a POST quiz times out, retry with the same key or GET
   `/quizzes/{quiz_id}` when the id is known. A double-tapped answer returns the same result.
7. **Feedback:** `correct`, `correct_answer {choice_id, text}`, `explanation`,
   `citations[]` (may be empty: a source that became unavailable is hidden; show
   `citations_omitted` as "1 source is no longer available" if desired) — replaces the single,
   non-null `citation`.
8. **Mastery** is one object: `{kind: "baseline", method, topic_id, topic_name, before,
   after, attempts, correct, trend, next_review_at}`. `before` is null on the first attempt
   (`trend: "first"`); `next_review_at` is always null. Label it as practice accuracy, not
   as an AI mastery prediction (show `kind`).
9. **No short answers** in M7 (`type` is `mcq` or `true_false` only).
10. **Caching:** responses are `no-store`; do not persist quiz bodies in shared caches.
    Mobile may keep the current quiz in memory/secure storage for the session only.
11. **Errors:** branch on `code`, show `detail`.
