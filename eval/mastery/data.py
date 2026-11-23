"""ASSISTments loading and the features the StudyGraph mastery protocol can actually carry.

The service only ever sees (topic, per-topic sequence, qtype, difficulty, correct). To make the
offline evaluation honest, the ASSISTments rows are reduced to exactly that:

- topic      = ASSISTments skill (one BKT chain per user x skill, like one per learner x topic)
- difficulty = tercile of the problem's correct rate on TRAINING users only (easy / medium / hard);
               problems with fewer than MIN_PROBLEM_OBS training answers are "medium"
- qtype      = "mcq" for answer_type choose_1, otherwise "other" (no chance-rate floor is applied
               when fitting; the service applies its floors for mcq and true_false at serving time)
- hints and response times are dropped (untrusted telemetry in the protocol)

Expected input: the 2009-2010 "skill builder" CSV (skill_builder_data*.csv) with columns
order_id, user_id, problem_id, skill_id, correct, and optionally original and answer_type.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DIFFICULTIES = ("easy", "medium", "hard")
MIN_PROBLEM_OBS = 5
MAX_SEQ_LEN = 200


def load_assistments(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="latin-1", low_memory=False)
    needed = {"order_id", "user_id", "problem_id", "skill_id", "correct"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing columns: {sorted(missing)}")
    if "original" in df.columns:
        df = df[df["original"] == 1]  # drop scaffolding sub-questions
    df = df.dropna(subset=["skill_id"])
    df = df[df["correct"].isin([0, 1])]
    df = df.drop_duplicates(subset=["order_id", "skill_id"])
    qtype = np.where(df.get("answer_type", pd.Series("", index=df.index)) == "choose_1", "mcq", "other")
    return pd.DataFrame({
        "order": df["order_id"].astype(np.int64), "user": df["user_id"].astype(str),
        "topic": df["skill_id"].astype(str), "problem": df["problem_id"].astype(str),
        "correct": df["correct"].astype(np.int8), "qtype": qtype,
    })


def split_users(df: pd.DataFrame, test_share: float = 0.2, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    users = np.array(sorted(df["user"].unique()))
    rng = np.random.default_rng(seed)
    test = set(rng.choice(users, size=max(1, int(len(users) * test_share)), replace=False))
    mask = df["user"].isin(test)
    return df[~mask].copy(), df[mask].copy()


def add_difficulty(train: pd.DataFrame, *frames: pd.DataFrame) -> list[pd.DataFrame]:
    stats = train.groupby("problem")["correct"].agg(["mean", "size"])
    stats = stats[stats["size"] >= MIN_PROBLEM_OBS]
    low, high = np.quantile(stats["mean"], [1 / 3, 2 / 3]) if len(stats) else (0.5, 0.5)
    label = pd.Series("medium", index=stats.index)
    label[stats["mean"] >= high] = "easy"
    label[stats["mean"] < low] = "hard"
    out = []
    for frame in (train, *frames):
        frame = frame.copy()
        frame["difficulty"] = frame["problem"].map(label).fillna("medium")
        out.append(frame)
    return out


def add_sequence(df: pd.DataFrame) -> pd.DataFrame:
    """Order each (user, topic) chain by order_id, number it from 1 and add prior success/failure counts."""
    df = df.sort_values(["user", "topic", "order"]).copy()
    group = df.groupby(["user", "topic"], sort=False)
    df["seq"] = group.cumcount() + 1
    df = df[df["seq"] <= MAX_SEQ_LEN].copy()
    group = df.groupby(["user", "topic"], sort=False)
    df["prior_correct"] = group["correct"].cumsum() - df["correct"]
    df["prior_wrong"] = (df["seq"] - 1) - df["prior_correct"]
    return df


def to_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """Pad chains into (n_chains, max_len) arrays for the vectorised BKT forward pass."""
    df = df.sort_values(["user", "topic", "seq"])
    chain = df.groupby(["user", "topic"], sort=False).ngroup().to_numpy()
    pos = df["seq"].to_numpy() - 1
    n, length = chain.max() + 1, pos.max() + 1
    correct = np.zeros((n, length), dtype=np.int8)
    difficulty = np.zeros((n, length), dtype=np.int8)
    mask = np.zeros((n, length), dtype=bool)
    correct[chain, pos] = df["correct"].to_numpy()
    difficulty[chain, pos] = df["difficulty"].map({d: i for i, d in enumerate(DIFFICULTIES)}).to_numpy()
    mask[chain, pos] = True
    return {"correct": correct, "difficulty": difficulty, "mask": mask, "chain": chain, "pos": pos}


def synthetic(n_users: int = 400, n_topics: int = 12, seed: int = 3, truth: dict | None = None) -> pd.DataFrame:
    """Data drawn from a known BKT model, for smoke tests and parameter-recovery checks."""
    truth = truth or SYNTHETIC_TRUTH
    rng = np.random.default_rng(seed)
    rows, order = [], 0
    for u in range(n_users):
        for t in range(n_topics):
            learned = rng.random() < truth["p_init"]
            for _ in range(rng.integers(3, 25)):
                problem = int(rng.integers(0, 300))
                diff = DIFFICULTIES[problem % 3]
                g, s = truth["guess"][diff], truth["slip"][diff]
                correct = int(rng.random() < ((1 - s) if learned else g))
                rows.append((order, f"u{u}", f"t{t}", f"p{problem}", correct, "mcq", diff))
                order += 1
                learned = learned or rng.random() < truth["p_transit"]
    df = pd.DataFrame(rows, columns=["order", "user", "topic", "problem", "correct", "qtype", "true_difficulty"])
    return df


SYNTHETIC_TRUTH = {
    "p_init": 0.35, "p_transit": 0.15,
    "guess": {"easy": 0.28, "medium": 0.2, "hard": 0.12},
    "slip": {"easy": 0.05, "medium": 0.1, "hard": 0.2},
}
