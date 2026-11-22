# studygraph.quiz/1 — proposed AI quiz generation protocol (M7)

**PROPOSED.** The shared release candidate describes the synchronous CE quiz adapter.
Activation requires joint approval and a conformant AI-1 provider. The packaged fixture
provider always reports `mock: true`. [Provider handoff](ai-provider-handoff.md)

## Endpoints

Both require `Authorization: Bearer <AI_SERVICE_TOKEN>` (≥ 32 characters) and receive
`X-StudyGraph-Protocol: studygraph.quiz/1`, `Accept: application/json`,
`Accept-Encoding: identity`.

### GET /ai/v1/quiz/capabilities

```json
{"protocol": "studygraph.quiz/1", "service": "ai-service", "mock": false,
 "scopes": ["class_only"], "question_types": ["mcq", "true_false"], "max_questions": 10, "max_topics": 5}
```

Exactly these keys. core-api refuses to generate (503, nothing created, no quota) when:
`mock` is not literally `false` (true, null or missing), the protocol or service differs,
a list has unknown or repeated values, a limit is not a positive integer (a boolean is not
an integer), `class_only` is not offered, no type in common with `mcq`/`true_false`, or the
request exceeds `max_questions` / `max_topics`. Results (including failures) are cached for
`QUIZ_CAPABILITIES_CACHE_SECONDS` (≤ 300 s).

### POST /ai/v1/quiz/generate

Request (core-api → AI):

```json
{
  "protocol": "studygraph.quiz/1",
  "request_id": "<quiz_id>",
  "course_id": "<uuid>",
  "scope": "class_only",
  "allowed_origins": ["class"],
  "topics": [{"topic_id": "<uuid>", "name": "Stoichiometry"}],
  "count": 5,
  "difficulty": "easy|medium|hard|mixed",
  "question_types": ["mcq", "true_false"],
  "limits": {"max_stem_chars": 1000, "max_hint_chars": 300, "max_explanation_chars": 2000,
             "min_choices": 2, "max_choices": 6, "max_choice_chars": 300,
             "min_citations": 1, "max_citations": 4, "max_quote_chars": 500}
}
```

`request_id` is the quiz session id; it is stable for the session and lets the AI side
deduplicate. core-api sends the request **once** (no automatic retry).

Response (200, `application/json`):

```json
{
  "protocol": "studygraph.quiz/1", "request_id": "<echo>", "course_id": "<echo>", "scope": "class_only",
  "mock": false, "grounded": true, "model": {"name": "...", "version": "..."},
  "questions": [
    {"topic_id": "<one of the requested topics>", "type": "mcq", "difficulty": "medium",
     "stem": "...", "choices": ["...", "...", "...", "..."], "answer": 2,
     "hint": "...", "explanation": "...",
     "citations": [{"chunk_id": "<uuid>", "document_id": "<uuid>", "origin": "class",
                    "locator": {"page": 4}, "quote": "<verbatim passage of that chunk>"}]},
    {"topic_id": "...", "type": "true_false", "difficulty": "medium", "stem": "...",
     "choices": null, "answer": true, "hint": "...", "explanation": "...", "citations": ["..."]}
  ]
}
```

## Validation in core-api (all or nothing)

Any violation rejects the **entire** response (502 `quiz_rejected` to the student; the
session fails with `rejected_protocol` or `rejected_provenance`); nothing is repaired,
dropped or partially kept, and nothing is retried.

- Exact keys at every level; strict JSON (no repeated keys, no NaN/Infinity); a boolean is
  not an integer.
- `protocol`, `request_id`, `course_id`, `scope` echo the request exactly; `mock` is
  `false`; `grounded` is `true`; `model.name` / `model.version` 1..100 characters, one line.
- Exactly `count` questions. Each: `topic_id` one of the requested topics; `type` one of the
  negotiated types; `difficulty` equal to the requested one, or any of easy/medium/hard for
  `mixed`; `stem` 1..1000, `hint` 1..300, `explanation` 1..2000 characters; no control
  characters except newline/tab, no unpaired surrogates.
- `mcq`: 2..6 single-line choices of 1..300 characters, unique after NFKC + casefold +
  whitespace normalization; `answer` an integer index in range. `true_false`: `choices`
  null, `answer` a boolean.
- Stems unique within the quiz (normalized). Heuristic leak check: a correct MCQ choice of
  ≥ 4 characters must not appear (normalized) in its stem or hint. **This does not detect
  semantic leaks** (paraphrases); see "Activation" in [quiz-api.md](quiz-api.md).
- 1..4 citations per question, no duplicate (chunk, quote); `origin` must be `class`
  (`external` and `web` are refused in M7); `locator` of a known shape (`{"page": n}`,
  `{"slide": n}`, `{"section"|"heading": s}`).

### Provenance (PostgreSQL, inside the persistence transaction)

After locking the cited documents and chunks (FOR SHARE, ordered by id) in the same
transaction that inserts the questions, each citation must satisfy: the chunk exists and
belongs to the cited document; chunk and document origin are `class`; the document is
`indexed`; chunk course = document course = requested course; the cited locator equals the
chunk's stored locator exactly; the whitespace-normalized quote is ≥ 20 characters and
occurs verbatim in the chunk text; the document title is safe to show. The checks reuse the
M5 grounding helpers (`app/chat/verify.py`) without changing chat behaviour.

**Provenance is not entailment**: it proves that the quoted passage exists in the course's
material, not that it supports the question, the keyed answer or the explanation.

## Transport (same rules as M5 chat)

A new HTTP client per call, closed on every exit; `trust_env=False` (no environment
proxies, `.netrc` or certificate overrides); redirects refused; `Content-Encoding` other
than identity refused; `Transfer-Encoding` refused; exactly one numeric `Content-Length`
that the raw body must match, at most `QUIZ_MAX_RESPONSE_BYTES` (probe: 16 KiB); a running
byte count; absolute deadlines (`QUIZ_PROBE_DEADLINE_SECONDS`,
`QUIZ_GENERATE_DEADLINE_SECONDS`, both inside `QUIZ_PIPELINE_DEADLINE_SECONDS`); no
retries. Upstream failures are logged as safe codes only (`http_500`, `redirect_refused`,
`invalid_framing`, `deadline`, …); the token, request bodies and generated text are never logged.

## What core-api does with an accepted response

Per question: MCQ choices are shuffled with a CSPRNG and labelled `a`..`f` after shuffling
(so the AI's tendency to put the answer at a fixed index does not leak); the answer key
becomes `{"choice_id": ...}`; the question is stored with `status = 'generated'`, its
evidence (chunk/document ids, origin, locator, normalized quote), `generation`
(`protocol`, `service`, `request_id`, `scope`, `model`) and a content hash, owned by the
session and student, and assigned at its position. Students receive only stem, choices,
hint, topic and type until they answer.

## Mock and test doubles

core-api never accepts mock output and never falls back to fixed questions. The tests use a
raw-socket fake (`tests/fake_quiz_service.py`) that generates nothing; it is not evidence
that generation works.

## Open points for the AI team

1. Jointly approve the synchronous quiz profile published in the shared release candidate.
2. Retrieval per topic (`chunk_topics` or the AI side's own topic assignment).
3. Latency budget for 10 questions (target from the brief: under 30 s; not measured).
4. Quality evaluation of stems/hints/distractors before activation.
