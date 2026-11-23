import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.mastery import model, schedule  # noqa: E402

PARAMS = model.load_params()


def test_correct_answers_raise_and_wrong_answers_lower_mastery():
    p = PARAMS.p_init
    up = model.update(PARAMS, p, "mcq", "medium", True)
    down = model.update(PARAMS, p, "mcq", "medium", False)
    assert down < p < up


def test_missing_an_easy_item_is_stronger_evidence_than_missing_a_hard_one():
    p = PARAMS.p_init
    assert model.update(PARAMS, p, "mcq", "easy", False) < model.update(PARAMS, p, "mcq", "hard", False)


def test_served_parameters_are_the_assistments_fit():
    assert (PARAMS.name, PARAMS.version) == ("studygraph-bkt", "1.0.0-assist09")
    assert all(e.guess <= 0.3 and e.slip <= 0.3 for e in PARAMS.emissions.values())


def test_true_false_guess_floor_weakens_a_correct_answer():
    p = PARAMS.p_init
    assert PARAMS.emission("true_false", "hard").guess == 0.5
    assert model.update(PARAMS, p, "true_false", "medium", True) < model.update(PARAMS, p, "mcq", "medium", True)


def test_mastery_converges_and_stays_a_probability():
    p = PARAMS.p_init
    for _ in range(30):
        p = model.update(PARAMS, p, "mcq", "medium", True)
        assert 0.0 <= p <= 1.0
    assert p > 0.99
    for _ in range(30):
        p = model.update(PARAMS, p, "true_false", "easy", False)
        assert 0.0 <= p <= 1.0
    assert 0.0 < model.predict_correct(PARAMS, p, "mcq", "easy") < 1.0


@pytest.mark.parametrize("patch", [
    {"p_init": 1.0}, {"p_transit": 0}, {"p_init": True},
    {"emissions": {"easy": {"guess": 0.6, "slip": 0.1}, "medium": {"guess": 0.2, "slip": 0.1},
                   "hard": {"guess": 0.1, "slip": 0.1}}},
    {"model": {"name": "bad name", "version": "1"}},
])
def test_invalid_parameter_files_are_refused(patch):
    raw = {
        "model": {"name": "m", "version": "1"}, "p_init": 0.3, "p_transit": 0.1,
        "emissions": {d: {"guess": 0.2, "slip": 0.1} for d in model.DIFFICULTIES},
        "guess_floor": {"mcq": 0.25, "true_false": 0.5},
    }
    model.parse_params(raw)
    with pytest.raises((ValueError, KeyError)):
        model.parse_params({**raw, **patch})


def test_review_ladder_and_clamping():
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert schedule.interval_days(0.9, 0) == 1
    assert schedule.interval_days(0.3, 5) == 1
    assert schedule.interval_days(0.9, 3) == 7
    assert schedule.interval_days(0.99, 50) == 60
    assert schedule.next_review(now - timedelta(days=400), 0.9, 3, now) == now  # overdue: due now
    assert schedule.next_review(now, 0.9, 3, now) == now + timedelta(days=7)
    assert schedule.format_timestamp(now) == "2026-11-23T00:00:00.000Z"
