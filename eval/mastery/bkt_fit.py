"""Maximum-likelihood fit of topic-agnostic BKT with difficulty-conditioned guess and slip.

Parameter vector: [p_init, p_transit, guess_easy, guess_medium, guess_hard, slip_easy, slip_medium,
slip_hard]. The update equations are the same as services/ai-service/app/mastery/model.py, so
the fitted file can be served as is (a test in this folder checks they agree).
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize

from data import DIFFICULTIES

# Served fit: guess and slip at most 0.3 (Corbett & Anderson-style bounds against BKT degeneracy,
# where a "learned" student guesses or slips half the time). On ASSISTments 2009 this costs about
# 0.001 AUC against the unbounded fit, whose guess and slip run into their ceilings.
BOUNDS = [(0.01, 0.99), (0.005, 0.6)] + [(0.01, 0.3)] * 3 + [(0.01, 0.3)] * 3
WIDE_BOUNDS = [(0.01, 0.99), (0.005, 0.6)] + [(0.01, 0.45)] * 3 + [(0.01, 0.45)] * 3
START = np.array([0.4, 0.1, 0.25, 0.2, 0.15, 0.1, 0.1, 0.1])
EPS = 1e-9


def unpack(theta: np.ndarray) -> tuple[float, float, np.ndarray, np.ndarray]:
    return theta[0], theta[1], np.asarray(theta[2:5]), np.asarray(theta[5:8])


def forward(theta: np.ndarray, arrays: dict) -> np.ndarray:
    """P(correct) for every observed step, predicted before that step is seen (online)."""
    p_init, transit, guess, slip = unpack(theta)
    correct, diff, mask = arrays["correct"], arrays["difficulty"], arrays["mask"]
    n, length = correct.shape
    learned = np.full(n, p_init)
    out = np.zeros((n, length))
    for t in range(length):
        live = mask[:, t]
        if not live.any():
            break
        g, s = guess[diff[:, t]], slip[diff[:, t]]
        p_correct = learned * (1 - s) + (1 - learned) * g
        out[:, t] = p_correct
        c = correct[:, t] == 1
        hit = np.where(c, learned * (1 - s), learned * s)
        miss = np.where(c, (1 - learned) * g, (1 - learned) * (1 - g))
        posterior = hit / (hit + miss + EPS)
        updated = posterior + (1 - posterior) * transit
        learned = np.where(live, updated, learned)
    return out


def neg_log_likelihood(theta: np.ndarray, arrays: dict) -> float:
    p = np.clip(forward(theta, arrays), EPS, 1 - EPS)
    c, m = arrays["correct"], arrays["mask"]
    ll = np.where(c == 1, np.log(p), np.log(1 - p))
    return float(-(ll * m).sum() / m.sum())


def fit(arrays: dict, bounds: list = BOUNDS, start: np.ndarray = START) -> tuple[np.ndarray, float]:
    start = np.clip(start, [lo for lo, _ in bounds], [hi - 1e-3 for _, hi in bounds])
    result = minimize(neg_log_likelihood, start, args=(arrays,), method="L-BFGS-B", bounds=bounds,
                      options={"maxiter": 200})
    return result.x, float(result.fun)


def to_service_params(theta: np.ndarray, name: str, version: str, source: str) -> dict:
    p_init, transit, guess, slip = unpack(theta)
    return {
        "model": {"name": name, "version": version},
        "source": source,
        "p_init": round(float(p_init), 6),
        "p_transit": round(float(transit), 6),
        "emissions": {d: {"guess": round(float(guess[i]), 6), "slip": round(float(slip[i]), 6)}
                      for i, d in enumerate(DIFFICULTIES)},
        "guess_floor": {"mcq": 0.25, "true_false": 0.5},
    }


def observed_predictions(theta: np.ndarray, arrays: dict) -> np.ndarray:
    """Predictions aligned with the row order used to build `arrays` (chain, pos)."""
    return forward(theta, arrays)[arrays["chain"], arrays["pos"]]
