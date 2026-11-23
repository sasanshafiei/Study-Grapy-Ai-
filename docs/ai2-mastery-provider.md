# AI-2 mastery provider: studygraph.mastery/1 (real implementation)

**Status: implemented behind `MASTERY_PROVIDER=real` (default `fixture`).** Needs CE-1 + AI-2
sign-off of the items at the end before CE turns on `MASTERY_DELIVERY_ENABLED`.
Code: `services/ai-service/app/mastery/`. Offline fit and baseline: `eval/mastery/`.
Wire contract: [ai-mastery-integration.md](ai-mastery-integration.md) (unchanged).

## Model

Bayesian Knowledge Tracing, one chain per (learner, topic), with **topic-agnostic** parameters:
P(init), P(transit), and guess/slip per difficulty (easy, medium, hard). The question type
raises guess to its chance rate (mcq 0.25, true_false 0.5). `mastery` = P(learned) after the
last applied event. `hint_used` and `response_ms` are ignored (untrusted telemetry).

Why BKT and not item IRT or DKT: events carry no question id, cross-topic order is approximate
(DKT unsupported by the contract), and course topics are new UUIDs, so per-skill ASSISTments
parameters cannot transfer. Only what the wire carries is used, offline and online.

Parameters live in a JSON file whose `model.name`/`model.version` are what CE pins
(`MASTERY_MODEL_NAME`/`_VERSION`). The served default is `params/bkt_assist09.json`
(`studygraph-bkt` / `1.0.0-assist09`), fitted by `eval/mastery/run_baseline.py --write-params`;
`params/bkt_prior.json` keeps the literature priors (`0.1.0-prior`) for comparison. After a
parameter upgrade each topic is re-folded from its stored events, so one chain never mixes two
parameter sets.

## Offline evaluation (ASSISTments 2009-2010 skill builder, corrected)

Next-answer prediction on held-out students: 3,331 training and 832 test users, 206,531 / 52,778
answers, 145 skills; difficulty = tercile of the problem's correct rate on training users only.
Full output: `eval/mastery/results/assist09.json`.

| Model | AUC | F1 | Accuracy | RMSE | Log loss |
| --- | --- | --- | --- | --- | --- |
| Majority (constant) | 0.500 | 0.785 | 0.646 | 0.479 | 0.651 |
| Logistic regression, topic-agnostic (week-1 baseline) | 0.718 | 0.780 | 0.694 | 0.446 | 0.584 |
| Logistic regression + skill intercepts (not deployable, reference) | 0.722 | 0.782 | 0.696 | 0.445 | 0.580 |
| BKT, unbounded (reference) | 0.711 | 0.789 | 0.697 | 0.448 | 0.588 |
| **BKT, guess/slip ≤ 0.3 (served)** | **0.710** | **0.790** | 0.690 | 0.448 | 0.589 |

The unbounded fit is degenerate (easy-item guess at its 0.45 ceiling, hard-item slip 0.44);
bounding costs 0.001 AUC. Served parameters: P(init) 0.75, P(transit) 0.09, guess/slip easy
0.30/0.05, medium 0.26/0.19, hard 0.09/0.30. P(init) is ASSISTments' population prior and is
a transfer assumption for chemistry; revisit it once StudyGraph has its own attempts. BKT is
served instead of the slightly stronger logistic model because it yields an interpretable
P(learned) per topic, which the dashboard and recommendations need; the gap (0.008 AUC) is small.

## Review scheduling

Fixed expanding intervals (plan cut item 4): 1 day if the last answer was wrong or mastery
< 0.6, else 1, 2, 4, 7, 14, 30, 60 days by correct streak, anchored at the last event's
`submitted_at`, clamped to [now, now + 364 days] (overdue = due now).

## State and guarantees

SQLite at `MASTERY_STATE_PATH` on the `ai_mastery_state` volume; the AI side writes no core
table. Exactly once per `(state_epoch, learner_ref, event_id)`; a retried batch returns its
events as `duplicate_event_ids` with the current scores. A batch commits all or nothing.

| Situation | Reply |
| --- | --- |
| Next sequence of the topic | applied |
| Same id at an already applied sequence | duplicate |
| Another id at an applied sequence / id reused elsewhere | 409 `sequence_conflict` / `event_conflict` |
| Gap after the stored sequence (state lost) | 200 with true `attempts_seen` < `through_seq`: CE's StateMismatch; nothing stored |
| Other epoch | 409 `epoch_mismatch` |
| Non-consecutive sequences inside one topic, > `max_events` | 422 |

## Configuration

`MASTERY_PROVIDER=real`, `MASTERY_STATE_EPOCH` (UUID, the value CE pins), `MASTERY_STATE_PATH`,
optional `MASTERY_PARAMS_PATH`, `MASTERY_MAX_EVENTS` (default 200). Index, chat and quiz stay on
the fixture service.

## Tests

```sh
cd services/ai-service && python -m pytest -q tests      # drives CE's own build_request / validate_update
cd eval/mastery && python -m pytest -q test_bkt_fit.py    # parameter recovery; fitter == served model
python run_baseline.py --csv <skill_builder CSV> --out results/assist09.json
```

## Sign-off needed

1. AI never writes core tables; the provider keeps its own SQLite state (contradicts CONTRIBUTING).
2. On lost state the non-duplicate ids are listed under `applied_event_ids` although nothing was
   applied, because the protocol has no third bucket. CE discards the response on StateMismatch.
3. Model identity `studygraph-bkt` / version from the parameter file; epoch value and its owner.
4. Recovery after state loss stays CE's M10 procedure (new epoch, replay from `topic_seq = 1`).
