"""Deterministic, explainable, rule-based recommendations (rule_based/1; no AI, nothing persisted).

Evidence is only the student's own committed graded attempts (M7, legacy and exam
attempts excluded) and the course's visible materials (app/dashboard/materials.py).

Rules:
- weak topic: among the topic's last <= 10 graded attempts (by topic sequence), n >= 3
  and correct / n < 0.6. Evidence strength: "limited" for n < 5, else "moderate".
- for each weak topic (at most 3, weakest first), up to 3 recommendations in this order:
  review_topic (always), class_material (the topic's indexed class document with the most
  linked chunks), practice_quiz (only while QUIZ_ENABLED and QUIZ_GENERATION_ENABLED, i.e.
  when the student can actually start one), external_resource (a visible harvested page).
- remaining slots (6 in total): topics the student has not practised that have class
  material, most material first: practice_quiz if available, else class_material.
- no graded attempt at all (cold start): the same "not practised" rule, at most 3.
Ties are broken by recent accuracy, n (more evidence first), last attempt (newer first),
topic name, topic id. The id of a recommendation is the first 16 hex characters of the
SHA-256 of its type, topic, target and evidence: the same evidence gives the same id.
Texts are templates; they never claim mastery or certainty.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from .materials import Material

BASIS = "rule_based/1"
WINDOW = 10
MIN_EVIDENCE = 3
WEAK_BELOW = 0.6
MAX_WEAK_TOPICS = 3
PER_TOPIC = 3
MAX_ITEMS = 6
COLD_START_ITEMS = 3


@dataclass(frozen=True)
class TopicEvidence:
    topic_id: uuid.UUID
    name: str
    attempts: int
    recent_n: int
    recent_correct: int
    recent_attempt_ids: tuple[uuid.UUID, ...]
    last_attempt_at: datetime | None


def _rec_id(kind: str, topic: uuid.UUID, target: dict, evidence: dict) -> str:
    canonical = json.dumps([kind, str(topic), target, evidence], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _item(kind: str, topic: TopicEvidence, target: dict, reason: dict, strength: str) -> dict:
    return {
        "id": _rec_id(kind, topic.topic_id, target, reason["evidence"]),
        "type": kind,
        "topic_id": str(topic.topic_id),
        "topic_name": topic.name,
        "basis": BASIS,
        "evidence_strength": strength,
        "reason": reason,
        "target": target,
    }


def _document_target(material: Material) -> dict:
    target = {"kind": "document", "document_id": str(material.document_id), "title": material.title,
              "origin": material.origin, "url": None, "licence": None, "provider": None}  # fmt: skip
    if material.origin == "external":
        target.update(url=material.url, licence=material.licence, provider=material.provider)
    return target


def _weak_reason(topic: TopicEvidence) -> dict:
    wrong = topic.recent_n - topic.recent_correct
    return {
        "code": "recent_errors",
        "text": f"{wrong} of your last {topic.recent_n} answers in {topic.name} were incorrect.",
        "evidence": {"window": topic.recent_n, "correct": topic.recent_correct,
                     "attempt_ids": [str(a) for a in topic.recent_attempt_ids]},  # fmt: skip
    }


def _unpractised_reason(topic: TopicEvidence) -> dict:
    return {
        "code": "not_practised",
        "text": f"You have not practised {topic.name} yet, and this course has material on it.",
        "evidence": {"window": 0, "correct": 0, "attempt_ids": []},
    }


def build(
    topics: list[TopicEvidence],
    class_docs: dict[uuid.UUID, Material],
    external_docs: dict[uuid.UUID, Material],
    quiz_available: bool,
) -> dict:
    weak = [
        t for t in topics
        if t.recent_n >= MIN_EVIDENCE and t.recent_correct / t.recent_n < WEAK_BELOW
    ]  # fmt: skip
    weak.sort(
        key=lambda t: (
            t.recent_correct / t.recent_n, -t.recent_n,
            -(t.last_attempt_at.timestamp() if t.last_attempt_at else 0.0), t.name, str(t.topic_id),
        )
    )  # fmt: skip
    items: list[dict] = []
    for topic in weak[:MAX_WEAK_TOPICS]:
        reason = _weak_reason(topic)
        strength = "limited" if topic.recent_n < 5 else "moderate"
        accuracy = topic.recent_correct / topic.recent_n
        candidates = [_item("review_topic", topic, {"kind": "topic", "topic_id": str(topic.topic_id)}, reason, strength)]
        if topic.topic_id in class_docs:
            candidates.append(_item("class_material", topic, _document_target(class_docs[topic.topic_id]), reason, strength))
        if quiz_available:
            target = {"kind": "quiz", "topic_ids": [str(topic.topic_id)], "difficulty": "easy" if accuracy < 0.4 else "medium"}
            candidates.append(_item("practice_quiz", topic, target, reason, strength))
        if topic.topic_id in external_docs:
            candidates.append(
                _item("external_resource", topic, _document_target(external_docs[topic.topic_id]), reason, strength)
            )
        items.extend(candidates[:PER_TOPIC])
    practised_any = any(t.attempts > 0 for t in topics)
    limit = MAX_ITEMS if practised_any else COLD_START_ITEMS
    unpractised = [t for t in topics if t.attempts == 0 and t.topic_id in class_docs]
    unpractised.sort(key=lambda t: (-class_docs[t.topic_id].chunks, t.name, str(t.topic_id)))
    for topic in unpractised:
        if len(items) >= limit:
            break
        reason = _unpractised_reason(topic)
        if quiz_available:
            target = {"kind": "quiz", "topic_ids": [str(topic.topic_id)], "difficulty": "easy"}
            items.append(_item("practice_quiz", topic, target, reason, "none"))
        else:
            items.append(_item("class_material", topic, _document_target(class_docs[topic.topic_id]), reason, "none"))
    items = items[:MAX_ITEMS]
    empty_reason = None
    if not items:
        empty_reason = "no_material" if not practised_any else "no_rule_matched"
    return {"basis": BASIS, "items": items, "empty_reason": empty_reason}
