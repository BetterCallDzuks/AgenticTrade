"""Rank strategies on out-of-sample, cost-adjusted evidence.

A single backtest cannot rank strategies: the winner may just have the luckiest
parameters. This module runs every candidate through the same walk-forward
optimizer (which selects parameters on train slices and trades them on untouched
test slices) and the same cost-stress sweep, then ranks the results.

The ranking key is deliberately boring and documented: the chosen objective on
the out-of-sample aggregate, then the break-even multiplier (a wider cost margin
wins ties), then stability (``stable`` beats ``mixed`` beats ``unstable``), then
the strategy name for determinism. A report is a research artifact, not a
recommendation.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agentictrade.cost_stress import DEFAULT_MULTIPLIERS, stress_costs
from agentictrade.costs import COST_MODEL_VERSION
from agentictrade.ohlcv import BacktestConfig, Candle, make_strategy, normalize_candles
from agentictrade.optimizer import (
    OBJECTIVES,
    WINDOW_MODES,
    default_grid,
    objective_value,
    optimize_walk_forward,
)

_CONSISTENCY_RANK = {"stable": 0, "mixed": 1, "unstable": 2}

_AGGREGATE_KEYS = (
    "trades",
    "netPnlSol",
    "returnPct",
    "winRate",
    "profitFactor",
    "expectancySol",
    "maxDrawdownPct",
    "sharpePerTrade",
    "segments",
    "skippedSegments",
    "testCandles",
)


def default_strategies() -> List[Dict[str, Any]]:
    """Momentum and breakout with the same defaults the CLI uses."""
    return [
        {"name": "momentum", "params": {"fast": 9, "slow": 21}, "grid": default_grid("momentum")},
        {
            "name": "breakout",
            "params": {"lookback": 20, "threshold_pct": 0.5},
            "grid": default_grid("breakout"),
        },
    ]


def _compact(summary: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: summary[key] for key in _AGGREGATE_KEYS if key in summary}


def _score_key(entry: Mapping[str, Any]) -> tuple:
    break_even = entry.get("breakEvenMultiplier")
    break_even_key = float(break_even) if break_even is not None else float("inf")
    return (
        -float(entry.get("score") or 0.0),
        -break_even_key,
        _CONSISTENCY_RANK.get(str(entry.get("consistency") or "unstable"), 3),
        str(entry.get("strategy") or ""),
    )


def compare_strategies(
    candles: Sequence[Candle],
    strategies: Optional[Sequence[Mapping[str, Any]]] = None,
    config: Optional[BacktestConfig] = None,
    *,
    objective: str = "pnl",
    window_mode: str = "fixed",
    segments: int = 3,
    train_ratio: float = 0.6,
    min_trades: int = 3,
    train_candles: Optional[int] = None,
    test_candles: Optional[int] = None,
    multipliers: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    """Optimize, stress and rank every strategy on the same candles and costs.

    Each spec is ``{"name": ..., "params": {...}, "grid": {...}}``; ``params`` is
    the unoptimized baseline and ``grid`` the parameter search space. The same
    walk-forward windows, seed-free backtester and cost configuration are used
    for every candidate, so the comparison is apples-to-apples.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {', '.join(OBJECTIVES)}")
    mode = (window_mode or "fixed").strip().lower()
    if mode not in WINDOW_MODES:
        raise ValueError(f"window mode must be one of {', '.join(WINDOW_MODES)}")
    data = normalize_candles(candles)
    if not data:
        raise ValueError("no candles supplied")

    specs = list(strategies) if strategies is not None else default_strategies()
    if not specs:
        raise ValueError("no strategies supplied")
    cfg = config or BacktestConfig()
    sweep = list(multipliers) if multipliers is not None else list(DEFAULT_MULTIPLIERS)

    entries: List[Dict[str, Any]] = []
    for spec in specs:
        name = str(spec.get("name") or "").strip()
        if not name:
            raise ValueError("every strategy spec needs a name")
        params = dict(spec.get("params") or {})
        grid = dict(spec.get("grid") or {})
        if not grid:
            raise ValueError(f"strategy {name!r} has an empty grid")

        optimization = optimize_walk_forward(
            data,
            name,
            grid,
            cfg,
            segments=segments,
            train_ratio=train_ratio,
            objective=objective,
            min_trades=min_trades,
            baseline_params=params or None,
            window_mode=mode,
            train_candles=train_candles,
            test_candles=test_candles,
        )
        baseline_strategy = make_strategy(name, **params)
        stress = stress_costs(data, baseline_strategy, cfg, multipliers=sweep)

        aggregate = optimization["aggregate"]
        stability = optimization.get("stability") or {}
        entry: Dict[str, Any] = {
            "strategy": name,
            "params": params,
            "grid": {key: list(values) for key, values in grid.items()},
            "aggregate": _compact(aggregate),
            "baselineNetPnlSol": float(
                ((optimization.get("baseline") or {}).get("summary") or {}).get("netPnlSol") or 0.0
            ),
            "upliftSol": float(optimization.get("upliftSol") or 0.0),
            "stability": {
                "consistency": stability.get("consistency", "unstable"),
                "dominantSharePct": stability.get("dominantSharePct", 0.0),
                "profitableSharePct": stability.get("profitableSharePct", 0.0),
                "windows": stability.get("windows", 0),
            },
            "costStress": {
                "status": stress.get("status"),
                "breakEvenMultiplier": stress.get("breakEvenMultiplier"),
                "headroomPct": stress.get("headroomPct"),
                "grossEdgeSol": stress.get("grossEdgeSol"),
                "baselineNetPnlSol": float((stress.get("baseline") or {}).get("netPnlSol") or 0.0),
            },
        }
        entry["consistency"] = entry["stability"]["consistency"]
        entry["score"] = objective_value(aggregate, objective)
        entry["breakEvenMultiplier"] = entry["costStress"]["breakEvenMultiplier"]
        entries.append(entry)

    ranking = sorted(entries, key=_score_key)
    ranking_names = [entry["strategy"] for entry in ranking]

    report: Dict[str, Any] = {
        "objective": objective,
        "windowMode": mode,
        "segmentCount": segments,
        "trainRatio": train_ratio if mode == "fixed" else None,
        "minTrades": min_trades,
        "candles": len(data),
        "multipliers": sweep,
        "strategies": entries,
        "ranking": ranking_names,
        "winner": ranking_names[0] if ranking_names else None,
        "configHash": cfg.fingerprint(),
        "costModelVersion": COST_MODEL_VERSION,
    }
    if mode != "fixed":
        report["trainCandles"] = train_candles
        report["testCandles"] = test_candles
    return report


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "∞" if number > 0 else "-∞"
    return f"{number:,.{digits}f}"


def render_report(report: Mapping[str, Any]) -> str:
    """Markdown rendering of a :func:`compare_strategies` report."""
    mode = report.get("windowMode", "fixed")
    if mode == "fixed":
        window_text = (
            f"{report.get('segmentCount', 0)} window(s), "
            f"train ratio {_fmt(float(report.get('trainRatio') or 0.0) * 100, 0)}%"
        )
    else:
        window_text = (
            f"{mode}, {report.get('trainCandles') or 'auto'} train / "
            f"{report.get('testCandles') or 'auto'} test candles"
        )

    lines = [
        "# Strategy comparison",
        "",
        f"- Objective: `{report.get('objective', 'pnl')}`",
        f"- Windows: {window_text}",
        f"- Candles: {report.get('candles', 0)}",
        f"- Winner: **{report.get('winner') or '—'}**",
        "",
        "| Rank | Strategy | Score | Net PnL | Return % | Trades | Profit factor | Max DD % |"
        " Stability | Break-even | Uplift |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    ranking = list(report.get("ranking") or [])
    by_name = {entry["strategy"]: entry for entry in report.get("strategies") or []}
    for position, name in enumerate(ranking, start=1):
        entry = by_name.get(name) or {}
        aggregate = entry.get("aggregate") or {}
        stability = entry.get("stability") or {}
        cost = entry.get("costStress") or {}
        break_even = cost.get("breakEvenMultiplier")
        break_even_text = f"{_fmt(break_even, 2)}x" if break_even is not None else "none"
        lines.append(
            f"| {position} | {name} | {_fmt(entry.get('score'), 6)} | "
            f"{_fmt(aggregate.get('netPnlSol'), 6)} | {_fmt(aggregate.get('returnPct'), 2)} | "
            f"{aggregate.get('trades', 0)} | {_fmt(aggregate.get('profitFactor'), 3)} | "
            f"{_fmt(aggregate.get('maxDrawdownPct'), 2)} | {stability.get('consistency', '—')} | "
            f"{break_even_text} | {_fmt(entry.get('upliftSol'), 6)} |"
        )

    lines += [
        "",
        "Score is the chosen objective on the out-of-sample aggregate. Ties break on the "
        "break-even multiplier, then stability, then the strategy name.",
    ]
    return "\n".join(lines)
