"""Dependency-free logistic-regression advisory model.

The model learns from *closed paper trades* joined with the most recent BUY
journal entry for the same mint. It is advisory only: it never changes sizing or
blocks/opens trades. A model is only trained when there are enough samples.
"""

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

FEATURE_NAMES = [
    "risk",
    "hype",
    "risk_x_hype",
    "log_invested",
    "is_high_conviction",
    "is_manual",
    "is_pumpfun",
    "bias",
]

MIN_SAMPLES = 12


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _feature_vector(risk: float, hype: float, invested: float, conviction: str, source: str) -> List[float]:
    conviction = (conviction or "").lower()
    source = (source or "").upper()
    risk_n = max(0.0, min(1.0, float(risk) / 100.0))
    hype_n = max(0.0, min(1.0, float(hype) / 100.0))
    return [
        risk_n,
        hype_n,
        risk_n * hype_n,
        math.log1p(max(0.0, float(invested))),
        1.0 if conviction in ("high", "ai") else 0.0,
        1.0 if conviction == "manual" else 0.0,
        1.0 if "PUMP" in source else 0.0,
        1.0,
    ]


def build_training_rows(db_path: str) -> List[dict]:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"trade_history", "decision_journal"}.issubset(tables):
            return []
        rows = conn.execute(
            """
            SELECT t.mint, t.closed_at, t.net_pnl_sol, t.invested_sol, t.conviction, t.source,
                   (SELECT j.risk_score FROM decision_journal j
                     WHERE j.mint = t.mint AND j.risk_score IS NOT NULL
                     ORDER BY j.id DESC LIMIT 1) AS risk_score,
                   (SELECT j.hype_score FROM decision_journal j
                     WHERE j.mint = t.mint AND j.hype_score IS NOT NULL
                     ORDER BY j.id DESC LIMIT 1) AS hype_score,
                   (SELECT j.suggested_size_sol FROM decision_journal j
                     WHERE j.mint = t.mint AND j.risk_score IS NOT NULL
                     ORDER BY j.id DESC LIMIT 1) AS suggested_size_sol
            FROM trade_history t
            WHERE t.net_pnl_sol IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        if row["risk_score"] is None or row["hype_score"] is None:
            continue
        invested = row["suggested_size_sol"] or row["invested_sol"] or 0.0
        out.append(
            {
                "mint": row["mint"],
                "closed_at": row["closed_at"],
                "features": _feature_vector(
                    row["risk_score"], row["hype_score"], invested, row["conviction"], row["source"]
                ),
                "label": 1.0 if float(row["net_pnl_sol"] or 0.0) > 0 else 0.0,
            }
        )
    return out


def _standardize(rows: List[dict]) -> Tuple[List[List[float]], List[float], List[float]]:
    matrix = [list(r["features"]) for r in rows]
    width = len(matrix[0])
    means = [sum(row[i] for row in matrix) / len(matrix) for i in range(width)]
    stds = []
    for i in range(width):
        variance = sum((row[i] - means[i]) ** 2 for row in matrix) / len(matrix)
        stds.append(math.sqrt(variance) or 1.0)
    normalized = [[(row[i] - means[i]) / stds[i] for i in range(width)] for row in matrix]
    return normalized, means, stds


def train_model(db_path: str, iterations: int = 600, learning_rate: float = 0.15, l2: float = 0.01) -> dict:
    rows = build_training_rows(db_path)
    if len(rows) < MIN_SAMPLES:
        return {
            "ok": False,
            "reason": f"not enough labelled trades ({len(rows)}/{MIN_SAMPLES})",
            "samples": len(rows),
        }

    matrix, means, stds = _standardize(rows)
    labels = [r["label"] for r in rows]
    width = len(FEATURE_NAMES)
    weights = [0.0] * width
    bias = 0.0

    for _ in range(max(1, iterations)):
        grad_w = [0.0] * width
        grad_b = 0.0
        for row, label in zip(matrix, labels):
            z = bias + sum(w * x for w, x in zip(weights, row))
            error = label - _sigmoid(z)
            for i in range(width):
                grad_w[i] += error * row[i]
            grad_b += error
        scale = 1.0 / len(matrix)
        for i in range(width):
            weights[i] += learning_rate * (grad_w[i] * scale - l2 * weights[i])
        bias += learning_rate * grad_b * scale

    correct = 0
    logloss = 0.0
    positives = 0
    for row, label in zip(matrix, labels):
        p = _sigmoid(bias + sum(w * x for w, x in zip(weights, row)))
        positives += int(label)
        if (p >= 0.5) == (label >= 0.5):
            correct += 1
        p = min(max(p, 1e-9), 1 - 1e-9)
        logloss -= label * math.log(p) + (1 - label) * math.log(1 - p)

    return {
        "ok": True,
        "schema": 1,
        "trainedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "samples": len(rows),
        "positives": positives,
        "accuracy": correct / len(rows),
        "logLoss": logloss / len(rows),
        "iterations": iterations,
        "learningRate": learning_rate,
        "l2": l2,
        "featureNames": FEATURE_NAMES,
        "mean": means,
        "std": stds,
        "weights": weights,
        "bias": bias,
    }


def predict_probability(model: dict, features: List[float]) -> Optional[float]:
    if not model or not model.get("ok"):
        return None
    try:
        normalized = [
            (float(value) - float(model["mean"][i])) / float(model["std"][i])
            for i, value in enumerate(features)
        ]
        z = float(model["bias"]) + sum(w * x for w, x in zip(model["weights"], normalized))
        return _sigmoid(z)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def features_from_journal(db_path: str, mint: str) -> Optional[List[float]]:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT risk_score, hype_score, suggested_size_sol, extra_json
            FROM decision_journal
            WHERE mint = ? AND risk_score IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (mint,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    conviction = "ai"
    source = "UNKNOWN"
    try:
        extra = json.loads(row["extra_json"] or "{}")
        conviction = extra.get("conviction", conviction)
        source = extra.get("source", source)
    except (json.JSONDecodeError, TypeError):
        pass
    return _feature_vector(
        row["risk_score"], row["hype_score"], row["suggested_size_sol"] or 0.0, conviction, source
    )


def predict_mint(db_path: str, mint: str, model: dict) -> Optional[float]:
    features = features_from_journal(db_path, mint)
    if features is None:
        return None
    return predict_probability(model, features)


def save_model(model: dict, path: str) -> None:
    Path(path).write_text(json.dumps(model, indent=2), encoding="utf-8")


def load_model(path: str) -> Optional[dict]:
    try:
        model = json.loads(Path(path).read_text(encoding="utf-8"))
        return model if model.get("ok") else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def model_summary(model: Optional[dict]) -> dict:
    if not model or not model.get("ok"):
        return {"trained": False, "reason": (model or {}).get("reason", "no model file")}
    return {
        "trained": True,
        "trainedAt": model.get("trainedAt"),
        "samples": model.get("samples"),
        "positives": model.get("positives"),
        "accuracy": model.get("accuracy"),
        "logLoss": model.get("logLoss"),
        "topWeights": sorted(
            (
                {"feature": name, "weight": round(weight, 5)}
                for name, weight in zip(model.get("featureNames", []), model.get("weights", []))
            ),
            key=lambda item: abs(item["weight"]),
            reverse=True,
        )[:5],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="AgenticTrade v10 ML advisory model")
    parser.add_argument("command", choices=["train", "status"], help="train a model or show model status")
    parser.add_argument("--db", default="paper_portfolio.db")
    parser.add_argument("--model", default="ml_model.json")
    args = parser.parse_args()

    if args.command == "status":
        print(json.dumps(model_summary(load_model(args.model)), indent=2))
        return 0

    model = train_model(args.db)
    if not model.get("ok"):
        print(f"training skipped: {model.get('reason')}")
        return 1
    save_model(model, args.model)
    print(json.dumps(model_summary(model), indent=2))
    print(f"model written to {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
