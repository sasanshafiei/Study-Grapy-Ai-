"""AI-2 recommendation ranking for studygraph.recommendations/1 (model_ranked/1 on the core side).

The ranker only selects and orders the candidates core-api sent; every item carries a reason code
from the allowlist and the ids of the evidence (of the same topic) that justify it.

Per topic it estimates the probability of answering correctly from two signals:
- recent accuracy: the last <= 10 graded answers, smoothed toward a population prior
  (2 pseudo-answers at 0.65, the ASSISTments correct rate) so 2 answers cannot look certain;
  used from 2 answers on;
- the mastery prediction: P(learned) turned into P(correct) on a medium multiple-choice item
  with the served BKT parameters (app/mastery/params).
The estimate is the LOWER of the two. The prediction already contains the recent answers when
it is current, so adding them would count the same answers twice; taking the lower one also lets
recent errors win over a stale (older) prediction.

A topic is weak below 0.6 (the rule_based/1 threshold). Weak topics come first, weakest first
(ties: more recent answers, then topic id). Very weak topics (< 0.4) start with review and class
material; the others start with a practice quiz. Each weak topic gets up to 2 items (3 if it is
the only one), so up to 3 weak topics fit in 6 items. Free slots go to topics with no evidence
at all (`not_practised`), in the order core-api sent them, one item each. Topics that are not
weak are not recommended. `review_due` needs a review date that the proposed evidence does not
carry, so v0.1 never emits it. No randomness: the same request gives the same response.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..mastery import model as mastery_model
from .protocol import PROTOCOL

MODEL_NAME = "studygraph-rank"
MODEL_VERSION = "0.1.0"

WEAK_BELOW = 0.6
VERY_WEAK_BELOW = 0.4
PRIOR_ACCURACY = 0.65
PRIOR_STRENGTH = 2.0
MIN_WINDOW = 2
COLD_START_ITEMS = 3

ORDER_VERY_WEAK = ("review_topic", "class_material", "practice_quiz", "external_resource")
ORDER_WEAK = ("practice_quiz", "class_material", "review_topic", "external_resource")
ORDER_NOT_PRACTISED = ("practice_quiz", "class_material", "review_topic")


@dataclass(frozen=True)
class TopicEstimate:
    topic_id: str
    p_correct: float
    window: int
    reason_code: str
    evidence_ids: tuple[str, ...]


def recent_accuracy(window: int, correct: int) -> float:
    return (correct + PRIOR_STRENGTH * PRIOR_ACCURACY) / (window + PRIOR_STRENGTH)


def predicted_accuracy(params: mastery_model.BktParams, mastery: float) -> float:
    return mastery_model.predict_correct(params, mastery, "mcq", "medium")


def estimate_topics(request: dict, params: mastery_model.BktParams) -> tuple[dict[str, TopicEstimate], set[str]]:
    """Return the weak-topic estimates and the set of topics that have any evidence."""
    recent: dict[str, dict] = {}
    predicted: dict[str, dict] = {}
    for ev in request["evidence"]:
        (recent if ev["kind"] == "recent_window" else predicted)[ev["topic_id"]] = ev
    weak: dict[str, TopicEstimate] = {}
    for topic in sorted(set(recent) | set(predicted)):
        signals = []  # (p_correct, reason_code, evidence_id)
        window = 0
        if topic in recent:
            window = recent[topic]["window"]
            if window >= MIN_WINDOW:
                signals.append((recent_accuracy(window, recent[topic]["correct"]), "low_recent_accuracy",
                                recent[topic]["evidence_id"]))  # fmt: skip
        if topic in predicted:
            signals.append((predicted_accuracy(params, predicted[topic]["mastery"]), "low_prediction",
                            predicted[topic]["evidence_id"]))  # fmt: skip
        if not signals:
            continue
        p_correct, reason_code, _ = min(signals, key=lambda s: (s[0], s[1]))
        if p_correct < WEAK_BELOW:
            supporting = tuple(eid for p, _, eid in signals if p < WEAK_BELOW)
            weak[topic] = TopicEstimate(topic, p_correct, window, reason_code, supporting)
    return weak, set(recent) | set(predicted)


def _by_type(candidates: list[dict], order: tuple[str, ...]) -> list[dict]:
    rank = {kind: i for i, kind in enumerate(order)}
    eligible = [(rank[c["type"]], i, c) for i, c in enumerate(candidates) if c["type"] in rank]
    return [c for _, _, c in sorted(eligible, key=lambda e: (e[0], e[1]))]


def rank(request: dict, params: mastery_model.BktParams) -> list[dict]:
    """Ordered response items for a request that passed protocol.validate_request."""
    by_topic: dict[str, list[dict]] = {}
    for cand in request["candidates"]:
        by_topic.setdefault(cand["topic_id"], []).append(cand)
    weak, practised = estimate_topics(request, params)
    weak_topics = sorted((w for w in weak.values() if w.topic_id in by_topic),
                         key=lambda w: (w.p_correct, -w.window, w.topic_id))  # fmt: skip
    limit = request["max_items"] if practised else min(request["max_items"], COLD_START_ITEMS)
    per_topic = 3 if len(weak_topics) == 1 else 2

    items: list[dict] = []
    for est in weak_topics:
        order = ORDER_VERY_WEAK if est.p_correct < VERY_WEAK_BELOW else ORDER_WEAK
        for cand in _by_type(by_topic[est.topic_id], order)[:per_topic]:
            items.append({"candidate_id": cand["candidate_id"], "reason_code": est.reason_code,
                          "evidence_ids": list(est.evidence_ids)})  # fmt: skip
    for topic in by_topic:  # dict order = first appearance in the request
        if topic in practised:
            continue
        choice = _by_type(by_topic[topic], ORDER_NOT_PRACTISED)[:1]
        for cand in choice:
            items.append({"candidate_id": cand["candidate_id"], "reason_code": "not_practised", "evidence_ids": []})
    return items[:limit]


def build_response(request: dict, params: mastery_model.BktParams) -> dict:
    return {
        "protocol": PROTOCOL,
        "request_id": request["request_id"],
        "mock": False,
        "model": {"name": MODEL_NAME, "version": MODEL_VERSION},
        "items": rank(request, params),
    }
