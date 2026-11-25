# What the web app needs from core-api

Owner of this document: **CE-2 (web)**. Owner of the implementation: **CE-1
(backend)**, with auth from **CE-3**.

The web app is finished and runs today against a mock. Every endpoint below is
already called by `apps/web/src/lib/api/live.ts`; the exact request and response
shapes are the TypeScript types in `apps/web/src/lib/api/types.ts`, which is the
precise version of this document.

Switch the web app to `NEXT_PUBLIC_API_MODE=live` and whatever is built starts
being used. Anything missing shows a readable error on that screen only, so we
can go endpoint by endpoint instead of waiting for a big bang.

## Ground rules

- Base path `/api/v1`. JSON in, JSON out, `snake_case` keys (the web client
  converts to camelCase on the way in).
- Auth: `Authorization: Bearer <access token>`.
- Errors: any non-2xx with `{"message": "..."}` or `{"detail": "..."}`. The
  message is shown to the user, so write it for them, not for a log.
  `401` signs the user out; `404` is reported as "not built yet or not found".
- The server decides what a role may do. The web app hides links by role, which
  is convenience, not a check.
- Anything slower than a second or two should return `202` with a job id and be
  polled, except `/chat`, which streams (below).

## Build in this order

The order matters: each step makes a screen work end to end.

| # | Endpoint | Method | Unlocks |
| --- | --- | --- | --- |
| 1 | `/auth/login` | POST | Sign in. Returns `{access_token, user}` |
| 2 | `/courses` | GET | The whole app (the sidebar needs one course) |
| 3 | `/courses/{id}/topics` | GET | Practice setup, exam blueprint |
| 4 | `/courses/{id}/documents` | GET | Materials list |
| 5 | `/courses/{id}/documents` | POST | Upload (multipart, field name `file`) |
| 6 | `/documents/{id}` | DELETE | Remove a file |
| 7 | `/courses/{id}/chat` | POST | **Ask** — streams, see below |
| 8 | `/courses/{id}/quizzes` | POST | Generate practice questions |
| 9 | `/courses/{id}/attempts` | POST | Submit an answer, returns the new mastery |
| 10 | `/courses/{id}/dashboard` | GET | Student Today screen |
| 11 | `/courses/{id}/analytics` | GET | Professor class screen |
| 12 | `/courses/{id}/exams` | GET, POST | Exam list, create from a blueprint |
| 13 | `/exams/{id}` | GET | Exam review |
| 14 | `/exams/{id}/items/{itemId}` | PATCH | Approve, reject, or edit one question |
| 15 | `/exams/{id}/items/{itemId}/regenerate` | POST | Draft a replacement question |
| 16 | `/exams/{id}/approve` | POST | Approve the exam |
| 17 | `/admin/overview` | GET | Admin overview |
| 18 | `/admin/users` | GET, POST | User list, add |
| 19 | `/admin/users/{id}` | PATCH | Change role, enable or disable |
| 20 | `/admin/sources` | GET, POST | Source list, allow a new domain |
| 21 | `/admin/sources/{id}` | PATCH | Turn a source on or off |
| 22 | `/admin/sources/{id}/run` | POST | Run a harvest now |
| 23 | `/admin/jobs` | GET | Job list |
| 24 | `/admin/jobs/{id}/retry` | POST | Retry a failed job |

## Three things that need agreeing early

### 1. `/courses/{id}/chat` streams server-sent events

Request: `{"question": "...", "scope": "class_only" | "class_plus_external" | "class_plus_web"}`

Response: `text/event-stream`

```
event: token
data: {"text": " next few words"}

event: done
data: { ...the full message, see below... }

event: error
data: {"message": "Something the user should read"}
```

The `done` payload is what the UI renders and is the single most important
shape in the project, because it is where the professors' "show where it came
from" requirement lands:

```json
{
  "id": "msg_01",
  "role": "assistant",
  "text": "A sentence from the lecture. [1] A sentence from OpenStax. [2]",
  "citations": [
    {
      "marker": 1,
      "chunk_id": "c_81f",
      "origin": "class",
      "title": "Lecture 04 - Stoichiometry.pdf",
      "locator": "Slide 12",
      "url": null,
      "snippet": "The limiting reactant limits the amount of product...",
      "provenance": null
    },
    {
      "marker": 2,
      "chunk_id": "c_a02",
      "origin": "external",
      "title": "OpenStax Chemistry 2e, 4.4 Reaction Yields",
      "locator": "Section 4.4",
      "url": "https://openstax.org/books/chemistry-2e/pages/4-4",
      "snippet": "Work out how much product each reactant could make...",
      "provenance": null
    }
  ],
  "grounded": true,
  "used_web": false,
  "scope": "class_plus_external",
  "topic_ids": ["t_stoich"],
  "created_at": "2026-11-17T12:00:00Z"
}
```

Points to hold to:

- **`[n]` markers in `text` must match `marker` values.** The web app splits the
  text on them and highlights each run in its source's colour. A sentence with
  no marker is shown unhighlighted, which reads as "not attributed".
- **`origin` is one of `class`, `external`, `web`** and decides the colour.
- **`locator` is a human-readable string** — "Slide 12", "Section 4.4", "Page 6".
  The web app prints it as-is, so format it on the server.
- **`provenance`** (WP-5, additive) is `null` for class material and for external
  material that was not harvested. For harvested external material it is the stored
  record of exactly the cited revision. Show `attribution` with links to `revision_url`
  and `licence_url` (the licence requires it). Never build attribution from other fields
  or from the answer text. `revision_id` is an integer. Treat every URL as untrusted text
  and render it only as an `http(s)` link (core-api already refuses any other scheme).

  ```json
  "provenance": {
    "source_url": "https://en.wikipedia.org/wiki/Chemical_equilibrium",
    "revision_id": 1234567,
    "revision_url": "https://en.wikipedia.org/w/index.php?oldid=1234567",
    "attribution": "\"Chemical equilibrium\", Wikipedia (en), by Wikipedia contributors (...), revision 1234567, ...",
    "licence": "CC BY-SA 4.0",
    "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/"
  }
  ```
- **`grounded: false`** means the sources do not support an answer. The web app
  then shows `text` as a warning instead of as an answer, so send a sentence
  explaining that, not a guess.

### 2. `POST /courses/{id}/attempts` returns the new mastery

The practice screen shows the mastery change immediately after an answer, so
the response has to include both numbers:

```json
{
  "correct": false,
  "correct_answer": "It shifts toward reactants and K decreases",
  "explanation": "Heat is released in the forward direction...",
  "citation": { ...one citation, same shape as above, class material only, without provenance... },
  "mastery_before": 0.38,
  "mastery": {
    "topic_id": "t_equil",
    "topic_name": "Equilibrium",
    "mastery": 0.33,
    "attempts": 14,
    "trend": "down",
    "next_review_at": "2026-11-18T00:00:00Z"
  }
}
```

### 3. Recommendations carry their reason

`/courses/{id}/dashboard` returns `recommendations[]`, and each one is shown to
the student with a plain-language reason. An unexplained recommendation is not
useful, and explainability is one of our own success criteria:

```json
{
  "id": "rec_1",
  "type": "external_resource",
  "topic_id": "t_equil",
  "topic_name": "Equilibrium",
  "title": "OpenStax Chemistry 2e, 13.3 Shifting Equilibria",
  "reason": "3 of your last 5 equilibrium answers were wrong",
  "origin": "external",
  "source_title": "OpenStax Chemistry 2e, 13.3 Shifting Equilibria",
  "url": "https://openstax.org/...",
  "minutes": 15
}
```

`type` is one of `review_topic`, `practice_quiz`, `class_material`,
`external_resource`, and decides where the button goes.

## Two rules the UI assumes and the server must enforce

1. **A draft exam question never reaches a student.** Questions stored with
   `purpose = exam` must be excluded from `/quizzes`, from `/chat` retrieval,
   and from anything a student can call. Worth a test in CI.
2. **The professor screens show class aggregates only.** `/analytics` must never
   return per-student rows, and a topic with fewer than `minimum_group_size`
   students (we show 5) should come back hidden rather than averaged.
