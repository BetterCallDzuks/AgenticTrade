"""Dependency-free logistic-regression advisory model.

The model learns from *closed paper trades* joined with the most recent BUY
journal entry for the same mint. It is advisory only: it never changes sizing or
blocks/opens trades. A model is only trained when there are enough samples.
"""

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from agentictrade.costs import COST_MODEL_VERSION

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

# The model contract. A model trained under an older cost model, feature set or
# label definition is not comparable with today's trades, so it is refused
# instead of quietly producing a probability from stale economics.
MODEL_SCHEMA_VERSION = 2
FEATURE_SET_VERSION = "1"
LABEL_SOURCE = "trade_history.net_pnl_sol"


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


def data_fingerprint(rows: List[dict]) -> str:
    """Short hash of the training rows, so two runs can be compared."""
    payload = sorted(
        (str(row.get("mint")), str(row.get("closed_at")), round(float(row.get("label") or 0.0), 6))
        for row in rows
    )
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def staleness_reasons(model: Optional[dict]) -> List[str]:
    """Return why a model cannot be used, or an empty list when it is current."""
    if not model:
        return ["no model file"]
    if not model.get("ok"):
        return [str(model.get("reason") or "model training failed")]
    reasons = []
    if model.get("schema") != MODEL_SCHEMA_VERSION:
        reasons.append(f"schema {model.get('schema')} != {MODEL_SCHEMA_VERSION}")
    if model.get("featureSetVersion") != FEATURE_SET_VERSION:
        reasons.append(f"feature set {model.get('featureSetVersion')!r} != {FEATURE_SET_VERSION!r}")
    if list(model.get("featureNames") or []) != FEATURE_NAMES:
        reasons.append("feature names changed")
    if model.get("costModelVersion") != COST_MODEL_VERSION:
        reasons.append(
            f"cost model v{model.get('costModelVersion')} != v{COST_MODEL_VERSION}; labels are not comparable"
        )
    if model.get("labelSource") != LABEL_SOURCE:
        reasons.append(f"label source {model.get('labelSource')!r} != {LABEL_SOURCE!r}")
    return reasons


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
        "schema": MODEL_SCHEMA_VERSION,
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
        # Labels are net of the shared CostModel, so a model trained today is
        # not comparable with one trained under different economics.
        "featureSetVersion": FEATURE_SET_VERSION,
        "labelSource": LABEL_SOURCE,
        "costModelVersion": COST_MODEL_VERSION,
        "dataFingerprint": data_fingerprint(rows),
        "minSamples": MIN_SAMPLES,
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


def load_model_any(path: str) -> Optional[dict]:
    """Read the file without judging it (diagnostics only)."""
    try:
        model = json.loads(Path(path).read_text(encoding="utf-8"))
        return model if isinstance(model, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def load_model(path: str) -> Optional[dict]:
    """Load a model only if it still matches the current contract.

    Fail closed: a stale model returns ``None`` so the caller cannot score a
    trade with economics the labels were not trained on.
    """
    model = load_model_any(path)
    if staleness_reasons(model):
        return None
    return model


def model_status(path: str) -> dict:
    """Report whether the model on disk is usable, and why not."""
    raw = load_model_any(path)
    if raw is None:
        return {"state": "missing", "reasons": ["no model file"], "path": str(path)}
    reasons = staleness_reasons(raw)
    return {
        "state": "stale" if reasons else "ok",
        "reasons": reasons,
        "path": str(path),
        "summary": model_summary(raw) if not reasons else None,
    }


def train_if_stale(db_path: str, path: str, **kwargs) -> dict:
    """Retrain only when the model is missing or no longer matches the contract."""
    status = model_status(path)
    if status["state"] == "ok":
        return {"trained": True, "retrained": False, "status": status}
    model = train_model(db_path, **kwargs)
    if not model.get("ok"):
        return {
            "trained": False,
            "retrained": False,
            "reason": model.get("reason"),
            "previous": status,
            "status": model_status(path),
        }
    save_model(model, path)
    return {"trained": True, "retrained": True, "status": model_status(path)}


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
        "schemaVersion": model.get("schema"),
        "featureSetVersion": model.get("featureSetVersion"),
        "labelSource": model.get("labelSource"),
        "costModelVersion": model.get("costModelVersion"),
        "dataFingerprint": model.get("dataFingerprint"),
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
        print(json.dumps(model_status(args.model), indent=2))
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
