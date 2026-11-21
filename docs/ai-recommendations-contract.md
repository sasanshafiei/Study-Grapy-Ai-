# studygraph.recommendations/1 — proposed AI recommendation ranking (NOT implemented)

**Status: PROPOSAL.** M8 ships only the deterministic `rule_based/1` recommendations
([dashboard-api.md](dashboard-api.md)). `AI_RECOMMENDATIONS_ENABLED` exists so that the switch is
documented, and Settings refuses `true`: there is no integration to turn on. Nothing here changes
the shared OpenAPI contract; AI-2 and CE must approve it first.

## Principle: the AI ranks candidates, core-api decides

Core-api builds the candidate list from data it already trusts; the AI may only **select and
order** those candidates and attach a reason code. It can never introduce a target, a URL, free
text or evidence that core-api did not send.

## Request (core-api → AI)

```json
{"protocol": "studygraph.recommendations/1", "request_id": "<uuid>", "learner_ref": "<64 hex>",
 "course_ref": "<64 hex>", "max_items": 6,
 "evidence": [{"evidence_id": "e1", "kind": "recent_window", "topic_id": "<uuid>", "window": 5, "correct": 2},
              {"evidence_id": "e2", "kind": "prediction", "topic_id": "<uuid>", "mastery": 0.41,
               "model_version": "1.4.0", "through_seq": 12}],
 "candidates": [{"candidate_id": "c1", "type": "review_topic", "topic_id": "<uuid>"},
                {"candidate_id": "c2", "type": "class_material", "topic_id": "<uuid>", "document_id": "<uuid>"},
                {"candidate_id": "c3", "type": "external_resource", "topic_id": "<uuid>", "document_id": "<uuid>"},
                {"candidate_id": "c4", "type": "practice_quiz", "topic_id": "<uuid>"}]}
```

- Candidates are exactly what `rule_based/1` may recommend now: topics of the course, documents
  visible under the M5/M6 rules, `practice_quiz` only while quizzes can be started.
- Evidence is the caller's own committed attempts and valid predictions only (pseudonymous, no
  answers, no question text). Study events are not evidence.

## Response (AI → core-api)

```json
{"protocol": "studygraph.recommendations/1", "request_id": "<echo>", "mock": false,
 "model": {"name": "…", "version": "…"},
 "items": [{"candidate_id": "c2", "reason_code": "low_recent_accuracy", "evidence_ids": ["e1"]}]}
```

## Validation (all or nothing, then fall back to rule_based/1)

- exact keys, echoed request id, `mock` literally `false`, a pinned model identity;
- every `candidate_id` was sent, no repeats, at most `max_items`;
- `reason_code` from a fixed allowlist (`low_recent_accuracy`, `low_prediction`, `not_practised`,
  `review_due`); every `evidence_ids` entry was sent and refers to the candidate's topic;
- no URL, title, text or id that core-api did not send; any other field rejects the response.

Core-api renders the text from templates and the persisted records (title, URL, licence), labels
items `basis: "model_ranked/1"`, keeps `evidence_strength` from its own evidence, re-checks
visibility in the response snapshot, and serves `rule_based/1` whenever the AI is unavailable,
refused or slow. Interaction events (shown/opened/dismissed) stay untrusted telemetry and are not
training labels without a separate approval.
