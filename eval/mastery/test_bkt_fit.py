"""The offline fitter recovers known parameters and computes exactly what ai-service serves."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

import bkt_fit
import data

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "ai-service"))
from app.mastery import model  # noqa: E402


def synthetic_arrays(seed):
    df = data.synthetic(n_users=300, seed=seed)
    df["difficulty"] = df["true_difficulty"]
    return data.to_arrays(data.add_sequence(df))


def test_fit_recovers_generating_parameters():
    theta, _ = bkt_fit.fit(synthetic_arrays(seed=11))
    truth = data.SYNTHETIC_TRUTH
    expected = [truth["p_init"], truth["p_transit"], *truth["guess"].values(), *truth["slip"].values()]
    assert np.allclose(theta, expected, atol=0.05)


def test_fitter_and_service_use_the_same_update():
    theta = np.array([0.3, 0.12, 0.3, 0.2, 0.12, 0.06, 0.1, 0.16])
    params = model.parse_params({**bkt_fit.to_service_params(theta, "m", "1", "test"),
                                 "guess_floor": {"mcq": 0.01, "true_false": 0.01}})  # no floor: pure fit
    answers = [(1, "medium"), (0, "hard"), (1, "easy"), (1, "hard"), (0, "medium")]
    frame = pd.DataFrame({"user": "u", "topic": "t", "seq": range(1, 6), "correct": [a for a, _ in answers],
                          "difficulty": [d for _, d in answers]})
    offline = bkt_fit.observed_predictions(theta, data.to_arrays(frame))
    p, served = params.p_init, []
    for correct, difficulty in answers:
        served.append(model.predict_correct(params, p, "mcq", difficulty))
        p = model.update(params, p, "mcq", difficulty, bool(correct))
    assert np.allclose(offline, served, atol=1e-9)
