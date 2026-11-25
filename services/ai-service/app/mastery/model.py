"""Bayesian Knowledge Tracing with difficulty-conditioned emissions (topic-agnostic parameters).

Why this model fits studygraph.mastery/1:

- Events carry a topic UUID, a per-topic sequence number, question type, difficulty and
  correctness only. There is no question id (so no item-level IRT) and cross-topic order is
  approximate (so no DKT over a global sequence). BKT runs one independent chain per topic.
- Course topics are UUIDs, not ASSISTments skills, so per-skill parameters cannot transfer.
  The parameters are shared by every topic and conditioned on what each event does carry:
  difficulty sets guess and slip; the question type raises guess to at least its chance rate.
- `hint_used` and `response_ms` are untrusted client telemetry and are ignored on purpose.

The served mastery value is P(L), the posterior probability that the topic is learned after
the last applied event. Parameters come from a JSON file (fitted on ASSISTments by
eval/mastery/run_baseline.py); the file's model name and version are what CE pins.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

DIFFICULTIES = ("easy", "medium", "hard")
QTYPES = ("mcq", "true_false")
MODEL_FIELD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$")
DEFAULT_PARAMS = Path(__file__).resolve().parent / "params" / "bkt_assist09.json"


@dataclass(frozen=True)
class Emission:
    guess: float
    slip: float


@dataclass(frozen=True)
class BktParams:
    name: str
    version: str
    p_init: float
    p_transit: float
    emissions: dict[str, Emission]  # by difficulty
    guess_floor: dict[str, float]  # by question type (chance rate)

    def emission(self, qtype: str, difficulty: str) -> Emission:
        base = self.emissions[difficulty]
        guess = max(base.guess, self.guess_floor[qtype])
        # Keep the model identifiable (guess + slip < 1) even after the floor is applied.
        return Emission(guess, min(base.slip, 0.99 - guess))


def _probability(value: object, label: str, upper: float = 1.0) -> float:
    if type(value) not in (int, float) or not 0.0 < float(value) < upper:
        raise ValueError(f"{label} must be a number in (0, {upper})")
    return float(value)


def parse_params(raw: dict) -> BktParams:
    model = raw.get("model", {})
    for key in ("name", "version"):
        if not isinstance(model.get(key), str) or not MODEL_FIELD.fullmatch(model[key]):
            raise ValueError(f"model.{key} must match {MODEL_FIELD.pattern}")
    emissions = {}
    for difficulty in DIFFICULTIES:
        entry = raw["emissions"][difficulty]
        guess = _probability(entry["guess"], f"{difficulty}.guess", 0.5)
        slip = _probability(entry["slip"], f"{difficulty}.slip", 0.5)
        emissions[difficulty] = Emission(guess, slip)
    floors = {q: _probability(raw["guess_floor"][q], f"guess_floor.{q}", 0.75) for q in QTYPES}
    return BktParams(
        name=model["name"], version=model["version"],
        p_init=_probability(raw["p_init"], "p_init"), p_transit=_probability(raw["p_transit"], "p_transit"),
        emissions=emissions, guess_floor=floors,
    )


def load_params(path: str | Path | None = None) -> BktParams:
    return parse_params(json.loads(Path(path or DEFAULT_PARAMS).read_text(encoding="utf-8")))


def predict_correct(params: BktParams, p_learned: float, qtype: str, difficulty: str) -> float:
    """P(next answer correct) before observing it."""
    e = params.emission(qtype, difficulty)
    return p_learned * (1.0 - e.slip) + (1.0 - p_learned) * e.guess


def update(params: BktParams, p_learned: float, qtype: str, difficulty: str, correct: bool) -> float:
    """Condition P(L) on one observed answer, then apply the learning transition."""
    e = params.emission(qtype, difficulty)
    if correct:
        hit, miss = p_learned * (1.0 - e.slip), (1.0 - p_learned) * e.guess
    else:
        hit, miss = p_learned * e.slip, (1.0 - p_learned) * (1.0 - e.guess)
    posterior = hit / (hit + miss)
    learned = posterior + (1.0 - posterior) * params.p_transit
    return min(1.0, max(0.0, learned))
