"""AI-2 week-1 mastery baseline on ASSISTments: next-answer prediction on held-out students.

Models (all trained on 80 % of users, scored online on the other 20 %):
- majority          constant training correct rate (AUC 0.5 reference)
- logreg_agnostic   logistic regression on what the protocol carries: prior successes and
                    failures on the topic (log1p), difficulty one-hot and an mcq flag. This is the
                    execution plan's "logistic regression baseline" and is deployable as is.
- logreg_skill      the same plus one-hot skill intercepts (PFA-like). NOT deployable (course
                    topics are new UUIDs); reported only as an upper reference.
- bkt_difficulty    the BKT served by ai-service, fitted here (topic-agnostic, difficulty emissions,
                    guess and slip bounded at 0.3)
- bkt_unbounded_reference  the same fit with guess/slip up to 0.45, to show what the bounds cost

Usage:
  python run_baseline.py --csv D:/data/assistments/skill_builder_data_corrected_collapsed.csv \
      --out results/assist09.json --write-params ../../services/ai-service/app/mastery/params/bkt_assist09.json
  python run_baseline.py --synthetic --out results/synthetic.json   # no dataset needed
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, log_loss, roc_auc_score

import bkt_fit
import data


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {
        "auc": round(float(roc_auc_score(y, p)), 4) if len(np.unique(y)) > 1 else None,
        "f1": round(float(f1_score(y, p >= 0.5)), 4),
        "accuracy": round(float(accuracy_score(y, p >= 0.5)), 4),
        "rmse": round(float(np.sqrt(np.mean((y - p) ** 2))), 4),
        "log_loss": round(float(log_loss(y, p, labels=[0, 1])), 4),
    }


def agnostic_features(df: pd.DataFrame) -> np.ndarray:
    diff = [(df["difficulty"] == d).to_numpy(float) for d in ("easy", "hard")]
    return np.column_stack([np.log1p(df["prior_correct"]), np.log1p(df["prior_wrong"]), *diff,
                            (df["qtype"] == "mcq").to_numpy(float)])


def skill_features(df: pd.DataFrame, topics: list[str]) -> sparse.csr_matrix:
    index = {t: i for i, t in enumerate(topics)}
    cols = df["topic"].map(index).fillna(-1).astype(int).to_numpy()
    rows = np.arange(len(df))
    keep = cols >= 0
    onehot = sparse.csr_matrix((np.ones(keep.sum()), (rows[keep], cols[keep])), shape=(len(df), len(topics)))
    return sparse.hstack([sparse.csr_matrix(agnostic_features(df)), onehot]).tocsr()


def prepare(args) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    if args.synthetic:
        df = data.synthetic(seed=args.seed)
        source = f"synthetic BKT data, seed {args.seed}, truth {data.SYNTHETIC_TRUTH}"
    else:
        df = data.load_assistments(args.csv)
        source = f"ASSISTments skill builder: {Path(args.csv).name}"
    train, test = data.split_users(df, seed=args.seed)
    if args.synthetic:  # recovery check: use the generating difficulty, not the tercile relabel
        train["difficulty"], test["difficulty"] = train["true_difficulty"], test["true_difficulty"]
    else:
        train, test = data.add_difficulty(train, test)
    train, test = data.add_sequence(train), data.add_sequence(test)
    key = ["user", "topic", "seq"]
    return train.sort_values(key).reset_index(drop=True), test.sort_values(key).reset_index(drop=True), source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--csv")
    source_group.add_argument("--synthetic", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", required=True)
    parser.add_argument("--write-params", help="write the fitted BKT as a service parameter file")
    parser.add_argument("--version", help="model version for --write-params (CE pins it)")
    args = parser.parse_args()
    args.version = args.version or (f"0.0.0-synthetic.{date.today():%Y%m%d}" if args.synthetic
                                    else f"1.0.0-assist09.{date.today():%Y%m%d}")

    started = time.time()
    train, test, source = prepare(args)
    y_train, y_test = train["correct"].to_numpy(), test["correct"].to_numpy()
    results: dict = {"source": source, "seed": args.seed,
                     "rows": {"train": int(len(train)), "test": int(len(test))},
                     "users": {"train": int(train["user"].nunique()), "test": int(test["user"].nunique())},
                     "topics": int(pd.concat([train["topic"], test["topic"]]).nunique()), "models": {}}

    results["models"]["majority"] = metrics(y_test, np.full(len(y_test), y_train.mean()))

    logreg = LogisticRegression(max_iter=1000).fit(agnostic_features(train), y_train)
    results["models"]["logreg_agnostic"] = metrics(y_test, logreg.predict_proba(agnostic_features(test))[:, 1])

    topics = sorted(train["topic"].unique())
    skill = LogisticRegression(max_iter=2000, C=1.0).fit(skill_features(train, topics), y_train)
    results["models"]["logreg_skill_reference"] = metrics(
        y_test, skill.predict_proba(skill_features(test, topics))[:, 1])

    train_arrays, test_arrays = data.to_arrays(train), data.to_arrays(test)
    wide, wide_nll = bkt_fit.fit(train_arrays, bounds=bkt_fit.WIDE_BOUNDS)
    results["models"]["bkt_unbounded_reference"] = {
        **metrics(y_test, bkt_fit.observed_predictions(wide, test_arrays)), "train_nll": round(wide_nll, 4),
        "theta": [round(float(v), 4) for v in wide]}
    theta, train_nll = bkt_fit.fit(train_arrays)
    test_pred = bkt_fit.observed_predictions(theta, test_arrays)
    results["models"]["bkt_difficulty"] = {**metrics(y_test, test_pred), "train_nll": round(train_nll, 4)}
    params = bkt_fit.to_service_params(theta, "studygraph-bkt", args.version, source)
    results["bkt_params"] = params
    results["seconds"] = round(time.time() - started, 1)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if args.write_params:
        Path(args.write_params).write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if k != "bkt_params"}, indent=2))
    print("BKT:", json.dumps(params, indent=2))


if __name__ == "__main__":
    main()
