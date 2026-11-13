"""Walk-forward parameter optimization for the OHLCV backtester.

The plain ``walk_forward`` helper evaluates one fixed parameter set on each
out-of-sample slice. This module adds the missing half: for every slice it
grid-searches the parameters on the in-sample (train) part, then trades only
the winning set on the untouched test part. The aggregate therefore reports
what a parameter-selection process would actually have earned, not what the
best parameters look like in hindsight.

Each slice starts from the same bankroll, matching ``walk_forward``. A
parameter set is only eligible when the train run produced at least
``min_trades`` trades; a slice with no eligible candidate is skipped rather
than filled with an untested winner. All costs, exits and sizing come from the
same :class:`agentictrade.ohlcv.BacktestConfig` as a normal backtest.
"""

from __future__ import annotations

import itertools
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agentictrade.costs import COST_MODEL_VERSION
from agentictrade.ohlcv import (
    BacktestConfig,
    Candle,
    make_strategy,
    normalize_candles,
    run_backtest,
)
from agentictrade.risk_analytics import summarize

OBJECTIVES = ("pnl", "return_pct", "profit_factor", "expectancy", "sharpe")

_DEFAULT_GRIDS: Dict[str, Dict[str, List[Any]]] = {
    "momentum": {"fast": [5, 9, 13], "slow": [13, 21, 34]},
    "breakout": {"lookback": [10, 20, 30, 55], "threshold_pct": [0.0, 0.5, 1.0]},
}

_SUMMARY_KEYS = (
    "trades",
    "netPnlSol",
    "returnPct",
    "winRate",
    "profitFactor",
    "expectancySol",
    "maxDrawdownPct",
    "sharpePerTrade",
    "exitReasons",
)


def default_grid(strategy_name: str) -> Dict[str, List[Any]]:
    """A small, sane grid for the built-in strategies."""
    normalized = (strategy_name or "momentum").strip().lower()
    key = "breakout" if normalized in ("breakout", "range") else "momentum"
    return {name: list(values) for name, values in _DEFAULT_GRIDS[key].items()}


def parse_grid(text: Optional[str], cast=int) -> List[Any]:
    """Parse ``"5,9,13"`` into ``[5, 9, 13]``. An empty value yields ``[]``."""
    if not text:
        return []
    out: List[Any] = []
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(cast(chunk))
        except (TypeError, ValueError):
            raise ValueError(f"invalid grid value {chunk!r}") from None
    return out


def _valid_params(strategy_name: str, params: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a parameter dict, or return ``None`` when it is not usable."""
    normalized = (strategy_name or "momentum").strip().lower()
    try:
        if normalized in ("momentum", "ema", "ema_cross"):
            fast = int(params.get("fast", 9))
            slow = int(params.get("slow", 21))
            if fast < 2 or slow <= fast:
                return None
            out: Dict[str, Any] = {"fast": fast, "slow": slow}
            if "min_gap_pct" in params:
                gap = float(params["min_gap_pct"])
                if not math.isfinite(gap) or gap < 0.0 or gap > 100.0:
                    return None
                out["min_gap_pct"] = gap
            return out
        if normalized in ("breakout", "range"):
            lookback = int(params.get("lookback", 20))
            threshold = float(params.get("threshold_pct", 0.5))
            if lookback < 2 or threshold < 0.0 or threshold > 100.0:
                return None
            return {"lookback": lookback, "threshold_pct": threshold}
        # Unknown strategies are accepted as-is; make_strategy() will reject
        # them when the run starts.
        return dict(params)
    except (TypeError, ValueError):
        return None


def grid_combinations(strategy_name: str, grid: Mapping[str, Sequence[Any]]) -> List[Dict[str, Any]]:
    """Every valid, de-duplicated parameter combination in deterministic order."""
    keys = sorted(name for name, values in grid.items() if values)
    if not keys:
        return []
    combinations: List[Dict[str, Any]] = []
    seen = set()
    for values in itertools.product(*(list(grid[name]) for name in keys)):
        params = _valid_params(strategy_name, dict(zip(keys, values)))
        if params is None:
            continue
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        combinations.append(params)
    return combinations


def _objective_value(summary: Mapping[str, Any], objective: str) -> float:
    if objective == "pnl":
        return float(summary.get("netPnlSol") or 0.0)
    if objective == "return_pct":
        return float(summary.get("returnPct") or 0.0)
    if objective == "expectancy":
        return float(summary.get("expectancySol") or 0.0)
    if objective == "sharpe":
        return float(summary.get("sharpePerTrade") or 0.0)
    if objective == "profit_factor":
        profit_factor = summary.get("profitFactor")
        if profit_factor is None:
            return float("inf") if float(summary.get("netPnlSol") or 0.0) > 0 else 0.0
        return float(profit_factor)
    raise ValueError(f"unknown objective: {objective}")


def _summary_subset(summary: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: summary[key] for key in _SUMMARY_KEYS if key in summary}


def _finite(value: float) -> Optional[float]:
    return value if math.isfinite(value) else None


def optimize_walk_forward(
    candles: Sequence[Candle],
    strategy_name: str,
    grid: Mapping[str, Sequence[Any]],
    config: Optional[BacktestConfig] = None,
    *,
    segments: int = 3,
    train_ratio: float = 0.6,
    objective: str = "pnl",
    min_trades: int = 3,
    baseline_params: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Grid-search each train slice and trade the winner on the test slice.

    Returns a JSON-friendly report with per-segment choices, aggregate
    out-of-sample performance and (when ``baseline_params`` is given) the same
    windows traded with the unoptimized parameters.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {', '.join(OBJECTIVES)}")
    data = normalize_candles(candles)
    if not data:
        raise ValueError("no candles supplied")

    combinations = grid_combinations(strategy_name, grid)
    if not combinations:
        raise ValueError("grid produced no valid parameter combinations")
    # Fail fast on an unknown strategy instead of reporting every segment as
    # skipped.
    make_strategy(strategy_name, **combinations[0])

    cfg = config or BacktestConfig()
    segments = max(1, int(segments))
    train_ratio = min(max(float(train_ratio), 0.1), 0.9)
    min_trades = max(0, int(min_trades))
    block_size = max(1, len(data) // segments)

    baseline_runner = None
    if baseline_params is not None:
        baseline_runner = make_strategy(strategy_name, **dict(baseline_params))

    results: List[Dict[str, Any]] = []
    baseline_segments: List[Dict[str, Any]] = []
    oos_pnls: List[float] = []
    baseline_pnls: List[float] = []
    baseline_test_candles = 0

    for segment in range(segments):
        start = segment * block_size
        end = len(data) if segment == segments - 1 else (segment + 1) * block_size
        block = data[start:end]
        split = max(1, int(len(block) * train_ratio))
        train = block[:split]
        test = block[split:]
        if len(test) < 10:
            continue

        baseline_summary = None
        if baseline_runner is not None:
            baseline_result = run_backtest(test, baseline_runner, cfg)
            baseline_summary = baseline_result.summary
            baseline_pnls.extend(float(trade["pnlSol"]) for trade in baseline_result.trades)
            baseline_test_candles += len(test)
            baseline_segments.append(
                {
                    "segment": segment + 1,
                    "testCandles": len(test),
                    "test": _summary_subset(baseline_summary),
                }
            )

        best: Optional[tuple] = None
        for params in combinations:
            try:
                result = run_backtest(train, make_strategy(strategy_name, **params), cfg)
            except (ValueError, IndexError):
                continue
            summary = result.summary
            if int(summary.get("trades") or 0) < min_trades:
                continue
            score = _objective_value(summary, objective)
            if best is None or score > best[0]:
                best = (score, params, summary)

        if best is None:
            results.append(
                {
                    "segment": segment + 1,
                    "trainCandles": len(train),
                    "testCandles": len(test),
                    "startTs": test[0].ts,
                    "endTs": test[-1].ts,
                    "skipped": True,
                    "reason": f"no candidate reached {min_trades} train trades",
                    "baseline": _summary_subset(baseline_summary) if baseline_summary else None,
                }
            )
            continue

        score, best_params, train_summary = best
        test_result = run_backtest(test, make_strategy(strategy_name, **best_params), cfg)
        oos_pnls.extend(float(trade["pnlSol"]) for trade in test_result.trades)
        results.append(
            {
                "segment": segment + 1,
                "trainCandles": len(train),
                "testCandles": len(test),
                "startTs": test[0].ts,
                "endTs": test[-1].ts,
                "bestParams": best_params,
                "score": _finite(score),
                "train": _summary_subset(train_summary),
                "test": _summary_subset(test_result.summary),
                "testPnlSol": float(test_result.summary.get("netPnlSol") or 0.0),
                "baseline": _summary_subset(baseline_summary) if baseline_summary else None,
            }
        )

    aggregate = summarize(oos_pnls, cfg.initial_sol)
    traded_segments = sum(1 for item in results if not item.get("skipped"))
    aggregate.update(
        {
            "segments": traded_segments,
            "skippedSegments": len(results) - traded_segments,
            "testCandles": sum(int(item["testCandles"]) for item in results),
            "initialSol": cfg.initial_sol,
            "finalEquitySol": cfg.initial_sol + float(aggregate["netPnlSol"]),
            "returnPct": (
                float(aggregate["netPnlSol"]) / cfg.initial_sol * 100.0
                if cfg.initial_sol
                else 0.0
            ),
            "configHash": cfg.fingerprint(),
            "costModelVersion": COST_MODEL_VERSION,
        }
    )

    report: Dict[str, Any] = {
        "strategy": strategy_name,
        "objective": objective,
        "minTrades": min_trades,
        "trainRatio": train_ratio,
        "segmentCount": segments,
        "grid": {name: list(values) for name, values in grid.items()},
        "combinations": len(combinations),
        "segments": results,
        "aggregate": aggregate,
    }

    if baseline_runner is not None and baseline_segments:
        baseline_summary = summarize(baseline_pnls, cfg.initial_sol)
        baseline_summary.update(
            {
                "segments": len(baseline_segments),
                "testCandles": baseline_test_candles,
                "initialSol": cfg.initial_sol,
                "finalEquitySol": cfg.initial_sol + float(baseline_summary["netPnlSol"]),
                "returnPct": (
                    float(baseline_summary["netPnlSol"]) / cfg.initial_sol * 100.0
                    if cfg.initial_sol
                    else 0.0
                ),
                "configHash": cfg.fingerprint(baseline_runner),
                "costModelVersion": COST_MODEL_VERSION,
            }
        )
        report["baseline"] = {
            "params": dict(baseline_params or {}),
            "summary": baseline_summary,
            "segments": baseline_segments,
        }
        report["upliftSol"] = float(aggregate["netPnlSol"]) - float(baseline_summary["netPnlSol"])

    return report


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "∞" if number > 0 else "-∞"
    return f"{number:,.{digits}f}"


def _params_text(params: Optional[Mapping[str, Any]]) -> str:
    if not params:
        return "—"
    return ", ".join(f"{key}={value}" for key, value in sorted(params.items()))


def render_report(report: Mapping[str, Any]) -> str:
    """Markdown rendering of an :func:`optimize_walk_forward` report."""
    strategy = report.get("strategy", "momentum")
    objective = report.get("objective", "pnl")
    grid = report.get("grid") or {}
    grid_text = "; ".join(
        f"{name}=[{', '.join(str(value) for value in values)}]" for name, values in grid.items()
    )
    lines = [
        f"# Walk-forward optimization — {strategy}",
        "",
        f"- Objective: `{objective}`",
        f"- Grid: {grid_text or '—'} ({report.get('combinations', 0)} combinations)",
        f"- Segments: {report.get('segmentCount', 0)} "
        f"(train ratio {_fmt(report.get('trainRatio', 0.0) * 100, 0)}%)",
        f"- Minimum train trades per candidate: {report.get('minTrades', 0)}",
        "",
        "| Segment | Train | Test | Best params | Train net | Test net | Test trades | Test return |"
        " Baseline net |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in report.get("segments") or []:
        baseline = item.get("baseline") or {}
        if item.get("skipped"):
            lines.append(
                f"| {item.get('segment')} | {item.get('trainCandles')} | {item.get('testCandles')} | "
                f"skipped: {item.get('reason', '')} | — | — | — | — | {_fmt(baseline.get('netPnlSol'))} |"
            )
            continue
        test = item.get("test") or {}
        train = item.get("train") or {}
        lines.append(
            f"| {item.get('segment')} | {item.get('trainCandles')} | {item.get('testCandles')} | "
            f"{_params_text(item.get('bestParams'))} | {_fmt(train.get('netPnlSol'), 6)} | "
            f"{_fmt(test.get('netPnlSol'), 6)} | {test.get('trades', 0)} | "
            f"{_fmt(test.get('returnPct'), 2)}% | {_fmt(baseline.get('netPnlSol'), 6)} |"
        )

    aggregate = report.get("aggregate") or {}
    lines += [
        "",
        "## Out-of-sample aggregate",
        "",
        "| Metric | Optimized | Baseline |",
        "|---|---|---|",
    ]
    baseline = (report.get("baseline") or {}).get("summary") or {}
    rows = (
        ("Trades", "trades", 0),
        ("Net PnL (SOL)", "netPnlSol", 6),
        ("Return (%)", "returnPct", 2),
        ("Win rate (%)", "winRate", 2),
        ("Profit factor", "profitFactor", 3),
        ("Expectancy (SOL)", "expectancySol", 6),
        ("Max drawdown (%)", "maxDrawdownPct", 2),
    )
    for label, key, digits in rows:
        mine = aggregate.get(key)
        if key == "winRate":
            mine = float(mine or 0.0) * 100.0
            other = float(baseline.get(key) or 0.0) * 100.0 if baseline else None
        else:
            other = baseline.get(key) if baseline else None
        lines.append(f"| {label} | {_fmt(mine, digits)} | {_fmt(other, digits) if baseline else '—'} |")

    lines.append("")
    skipped = aggregate.get("skippedSegments", 0)
    if skipped:
        lines.append(f"Skipped {skipped} segment(s) with no eligible parameter set.")
    if "upliftSol" in report:
        lines.append(
            f"Uplift over baseline: **{_fmt(report['upliftSol'], 6)} SOL** "
            f"(baseline {_params_text((report.get('baseline') or {}).get('params'))})."
        )
    return "\n".join(lines)
