"""Simulated learners with one known weak topic: does the weak topic reach the top 3? (plan, Table 6)

Each learner has a course of 8 topics: 6 practised (one weak, five stronger) and 2 not practised.
Every practised topic has a true probability of a correct answer; answers are drawn from it and
interleaved in time. From those answers we build exactly what core-api would have:
- the evidence of the proposed studygraph.recommendations/1 (last <= 10 answers per topic and,
  in the "with predictions" condition, the served BKT mastery after all answers);
- the inputs of core-api's own rule_based/1 (services/core-api/app/dashboard/recommendations.py,
  loaded unchanged with a stand-in for its database-backed materials module).
Both systems see the same materials (class 80%, external 50% of topics) and quizzes are available.

Metrics per scenario: weak topic among the topics of the first 3 items (hit@3, the plan's target),
first item on the weak topic (hit@1), no recommendation at all (empty), and the share of the
first 3 items that point to a strong topic (true P(correct) >= 0.75, a needless recommendation).
"Visible" is the share of learners whose weak topic shows in its own last <= 10 answers (at least
3, under 60% correct); hit@3 is also reported for them, since no rule on answers can find the rest.
95% Wilson intervals. The ranker's constants were set before this simulation (0.6 from
rule_based/1, the 0.65 prior from ASSISTments) and were not tuned on it.

    python simulate.py --learners 2000 --seed 2026 --out results/simulated.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
import types
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "ai-service"))
from app.mastery import model as mastery_model  # noqa: E402
from app.recommendations import protocol, ranker  # noqa: E402

CE_RECOMMENDATIONS = ROOT / "services" / "core-api" / "app" / "dashboard" / "recommendations.py"
TOPICS, PRACTISED = 8, 6
STRONG_AT = 0.75
SCENARIOS = {
    # attempts per practised topic, true P(correct) of the weak topic and of the others
    "sparse (2-5 answers per topic)": {"attempts": (2, 5), "weak": (0.20, 0.45), "other": (0.60, 0.95)},
    "typical (4-12 answers)": {"attempts": (4, 12), "weak": (0.20, 0.45), "other": (0.60, 0.95)},
    "rich (10-30 answers)": {"attempts": (10, 30), "weak": (0.20, 0.45), "other": (0.60, 0.95)},
    "close (weak 0.30-0.50, others 0.55-0.80)": {"attempts": (4, 12), "weak": (0.30, 0.50), "other": (0.55, 0.80)},
}


def load_rule_based():
    """Import core-api's rule_based/1 unchanged; only its Material type is provided here."""
    package = types.ModuleType("ce_dashboard")
    package.__path__ = []
    materials = types.ModuleType("ce_dashboard.materials")

    @dataclass(frozen=True)
    class Material:
        document_id: uuid.UUID
        title: str
        origin: str
        chunks: int
        url: str | None = None
        licence: str | None = None
        provider: str | None = None

    materials.Material = Material
    sys.modules.update({"ce_dashboard": package, "ce_dashboard.materials": materials})
    spec = importlib.util.spec_from_file_location("ce_dashboard.recommendations", CE_RECOMMENDATIONS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, Material


@dataclass
class Learner:
    topics: list[uuid.UUID]
    weak: uuid.UUID
    truth: dict[uuid.UUID, float]
    answers: dict[uuid.UUID, list[tuple[bool, str, datetime]]]  # correct, difficulty, time
    class_chunks: dict[uuid.UUID, int]
    external: set[uuid.UUID]


def make_learner(rng: random.Random, scenario: dict) -> Learner:
    topics = [uuid.UUID(int=rng.getrandbits(128), version=4) for _ in range(TOPICS)]
    practised = topics[:PRACTISED]
    weak = practised[0]
    truth = {t: rng.uniform(*scenario["other"]) for t in practised}
    truth[weak] = rng.uniform(*scenario["weak"])
    queue = [t for t in practised for _ in range(rng.randint(*scenario["attempts"]))]
    rng.shuffle(queue)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    answers: dict[uuid.UUID, list] = {t: [] for t in practised}
    for i, topic in enumerate(queue):
        answers[topic].append((rng.random() < truth[topic], rng.choice(("easy", "medium", "hard")),
                               start + timedelta(minutes=3 * i)))  # fmt: skip
    rng.shuffle(topics)  # nothing in the order reveals the weak topic
    class_chunks = {t: rng.randint(1, 20) for t in topics if rng.random() < 0.8}
    external = {t for t in topics if rng.random() < 0.5}
    return Learner(topics, weak, truth, answers, class_chunks, external)


def ranker_request(learner: Learner, params, with_predictions: bool) -> dict:
    evidence, candidates = [], []
    for topic, answers in learner.answers.items():
        last = answers[-protocol.RECENT_WINDOW:]
        evidence.append({"evidence_id": f"r{len(evidence)}", "kind": "recent_window", "topic_id": str(topic),
                         "window": len(last), "correct": sum(a[0] for a in last)})  # fmt: skip
        if with_predictions:
            p = params.p_init
            for correct, difficulty, _ in answers:
                p = mastery_model.update(params, p, "mcq", difficulty, correct)
            evidence.append({"evidence_id": f"p{len(evidence)}", "kind": "prediction", "topic_id": str(topic),
                             "mastery": round(p, 6), "model_version": params.version,
                             "through_seq": len(answers)})  # fmt: skip
    for topic in sorted(learner.topics, key=lambda t: (-learner.class_chunks.get(t, 0), str(t))):
        kinds = ["review_topic"] + (["class_material"] if topic in learner.class_chunks else [])
        kinds += ["practice_quiz"] + (["external_resource"] if topic in learner.external else [])
        for kind in kinds:
            cand = {"candidate_id": f"c{len(candidates)}", "type": kind, "topic_id": str(topic)}
            if kind in protocol.DOCUMENT_TYPES:
                cand["document_id"] = str(uuid.uuid5(topic, kind))
            candidates.append(cand)
    return protocol.validate_request({
        "protocol": protocol.PROTOCOL, "request_id": str(uuid.UUID(int=len(candidates), version=4)),
        "learner_ref": "0" * 64, "course_ref": "1" * 64, "max_items": 6, "evidence": evidence,
        "candidates": candidates,
    })  # fmt: skip


def ranker_topics(learner: Learner, params, with_predictions: bool) -> list[uuid.UUID]:
    req = ranker_request(learner, params, with_predictions)
    res = ranker.build_response(req, params)
    protocol.check_response(req, res, ranker.MODEL_NAME, ranker.MODEL_VERSION)  # every response is contract-valid
    topic = {c["candidate_id"]: uuid.UUID(c["topic_id"]) for c in req["candidates"]}
    return [topic[item["candidate_id"]] for item in res["items"]]


def rule_based_topics(learner: Learner, rules, Material) -> list[uuid.UUID]:
    evidence = []
    for topic in learner.topics:
        answers = learner.answers.get(topic, [])
        last = answers[-rules.WINDOW:]
        evidence.append(rules.TopicEvidence(
            topic_id=topic, name=f"Topic {str(topic)[:8]}", attempts=len(answers), recent_n=len(last),
            recent_correct=sum(a[0] for a in last), recent_attempt_ids=tuple(uuid.uuid4() for _ in last),
            last_attempt_at=last[-1][2] if last else None,
        ))  # fmt: skip
    class_docs = {t: Material(uuid.uuid5(t, "class"), "Class notes", "class", n) for t, n in learner.class_chunks.items()}
    external = {t: Material(uuid.uuid5(t, "ext"), "Page", "external", 1, "https://example.org/p", "CC BY 4.0", "OpenStax")
                for t in learner.external}  # fmt: skip
    out = rules.build(evidence, class_docs, external, quiz_available=True)
    return [uuid.UUID(item["topic_id"]) for item in out["items"]]


def wilson(hits: int, n: int) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    z, p = 1.96, hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(centre - half, 4), round(centre + half, 4)]


def observably_weak(learner: Learner) -> bool:
    """The weak topic's last <= 10 answers show it: at least 3 answers, under 60% correct."""
    last = learner.answers[learner.weak][-protocol.RECENT_WINDOW:]
    return len(last) >= 3 and sum(a[0] for a in last) / len(last) < 0.6


def score(results: list[tuple[Learner, list[uuid.UUID]]]) -> dict:
    n = len(results)
    hit3 = sum(lr.weak in topics[:3] for lr, topics in results)
    hit1 = sum(bool(topics) and topics[0] == lr.weak for lr, topics in results)
    empty = sum(not topics for _, topics in results)
    top3 = [(lr, t) for lr, topics in results for t in topics[:3]]
    strong = sum(lr.truth.get(t, 0.0) >= STRONG_AT for lr, t in top3)
    visible = [(lr, topics) for lr, topics in results if observably_weak(lr)]
    hit3_visible = sum(lr.weak in topics[:3] for lr, topics in visible)
    return {"learners": n, "hit_at_3": round(hit3 / n, 4), "hit_at_3_ci95": wilson(hit3, n),
            "hit_at_1": round(hit1 / n, 4), "empty": round(empty / n, 4),
            "strong_topic_share_of_top3": round(strong / len(top3), 4) if top3 else 0.0,
            "observably_weak_share": round(len(visible) / n, 4),
            "hit_at_3_when_observably_weak": round(hit3_visible / len(visible), 4) if visible else None}  # fmt: skip


def run(learners: int, seed: int) -> dict:
    params = mastery_model.load_params()
    rules, Material = load_rule_based()
    report = {"seed": seed, "learners_per_scenario": learners, "target": "weak topic in the top 3",
              "ranker": f"{ranker.MODEL_NAME} {ranker.MODEL_VERSION}", "mastery_params": params.version,
              "scenarios": {}}  # fmt: skip
    for s, (name, scenario) in enumerate(SCENARIOS.items()):
        rng = random.Random(seed * 1000 + s)
        population = [make_learner(rng, scenario) for _ in range(learners)]
        report["scenarios"][name] = {
            "rule_based/1 (core-api today)": score([(lr, rule_based_topics(lr, rules, Material)) for lr in population]),
            "ranker, recent answers only": score([(lr, ranker_topics(lr, params, False)) for lr in population]),
            "ranker, recent answers + mastery": score([(lr, ranker_topics(lr, params, True)) for lr in population]),
        }
    return report


def print_table(report: dict) -> None:
    print(f"{'scenario':44} {'system':34} {'hit@3':>7} {'95% CI':>15} {'hit@1':>7} {'empty':>7} {'strong':>7}"
          f" {'visible':>8} {'hit@3|vis':>9}")
    for name, systems in report["scenarios"].items():
        for system, m in systems.items():
            ci = f"{m['hit_at_3_ci95'][0]:.3f}-{m['hit_at_3_ci95'][1]:.3f}"
            print(f"{name:44} {system:34} {m['hit_at_3']:7.3f} {ci:>15} {m['hit_at_1']:7.3f} "
                  f"{m['empty']:7.3f} {m['strong_topic_share_of_top3']:7.3f} {m['observably_weak_share']:8.3f}"
                  f" {m['hit_at_3_when_observably_weak']:9.3f}")  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--learners", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = run(args.learners, args.seed)
    print_table(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
