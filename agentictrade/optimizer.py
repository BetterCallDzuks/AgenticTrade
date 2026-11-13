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
import statistics
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

WINDOW_MODES = ("fixed", "anchored", "rolling")

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


def objective_value(summary: Mapping[str, Any], objective: str) -> float:
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


def _auto_train_candles(total: int) -> int:
    return max(60, total // 3)


def _auto_test_candles(total: int) -> int:
    return max(20, total // 8)


def window_ranges(
    candles: int,
    mode: str = "fixed",
    *,
    segments: int = 3,
    train_ratio: float = 0.6,
    train_candles: Optional[int] = None,
    test_candles: Optional[int] = None,
) -> List[Dict[str, int]]:
    """Split ``candles`` into train/test ranges for the requested window mode.

    ``fixed`` keeps the original behaviour: ``segments`` equal blocks, each cut
    at ``train_ratio``. ``anchored`` uses an expanding train window that always
    starts at candle 0 and a test window that steps forward. ``rolling`` keeps
    the train window a constant size and slides both windows forward.

    Anchored and rolling modes need a train and a test size; ``None`` derives
    them from the series length (``max(60, n // 3)`` and ``max(20, n // 8)``).
    Ranges with fewer than 10 test candles are still returned so the caller can
    report them as skipped; the caller decides what to do with them.
    """
    mode = (mode or "fixed").strip().lower()
    if mode not in WINDOW_MODES:
        raise ValueError(f"window mode must be one of {', '.join(WINDOW_MODES)}")

    if mode == "fixed":
        segments = max(1, int(segments))
        train_ratio = min(max(float(train_ratio), 0.1), 0.9)
        block_size = max(1, candles // segments)
        ranges: List[Dict[str, int]] = []
        for segment in range(segments):
            start = segment * block_size
            end = candles if segment == segments - 1 else (segment + 1) * block_size
            split = start + max(1, int((end - start) * train_ratio))
            ranges.append(
                {
                    "index": segment + 1,
                    "trainStart": start,
                    "trainEnd": split,
                    "testStart": split,
                    "testEnd": end,
                }
            )
        return ranges

    train_size = int(train_candles) if train_candles else _auto_train_candles(candles)
    test_size = int(test_candles) if test_candles else _auto_test_candles(candles)
    if train_size < 2:
        raise ValueError("train_candles must be at least 2")
    if test_size < 10:
        raise ValueError("test_candles must be at least 10")
    if train_size >= candles:
        raise ValueError(
            f"train_candles ({train_size}) must be smaller than the series ({candles})"
        )

    ranges = []
    index = 1
    test_start = train_size
    while test_start < candles:
        test_end = min(test_start + test_size, candles)
        train_start = 0 if mode == "anchored" else test_start - train_size
        ranges.append(
            {
                "index": index,
                "trainStart": train_start,
                "trainEnd": test_start,
                "testStart": test_start,
                "testEnd": test_end,
            }
        )
        index += 1
        test_start = test_end
        if test_end >= candles:
            break
    if not ranges:
        raise ValueError("no walk-forward windows fit in this series; lower train_candles")
    return ranges


_CONSISTENCY_STABLE = 0.6
_CONSISTENCY_UNSTABLE = 0.4


def _stability(windows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """How consistently the optimizer picked and profited across windows.

    ``consistency`` is a coarse label, not a score: ``stable`` needs the
    dominant parameter set to win at least 60% of the traded windows *and* at
    least 60% of them to be profitable; ``unstable`` is either share below 40%;
    everything else is ``mixed``.
    """
    traded = [window for window in windows if not window.get("skipped")]
    if not traded:
        return {
            "windows": 0,
            "paramSets": [],
            "dominantSharePct": 0.0,
            "profitableWindows": 0,
            "profitableSharePct": 0.0,
            "bestWindowPnlSol": None,
            "worstWindowPnlSol": None,
            "medianWindowPnlSol": None,
            "pnlStdevSol": 0.0,
            "consistency": "unstable",
        }

    counts: Dict[tuple, int] = {}
    params_by_key: Dict[tuple, Dict[str, Any]] = {}
    for window in traded:
        params = window.get("bestParams") or {}
        key = tuple(sorted(params.items()))
        counts[key] = counts.get(key, 0) + 1
        params_by_key[key] = dict(params)

    param_sets: List[Dict[str, Any]] = [
        {
            "params": params_by_key[key],
            "count": count,
            "sharePct": count / len(traded) * 100.0,
        }
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    ]
    dominant_share = param_sets[0]["sharePct"] / 100.0

    pnls = [float(window["testPnlSol"]) for window in traded]
    profitable = [pnl for pnl in pnls if pnl > 0.0]
    profitable_share = len(profitable) / len(pnls)

    if dominant_share >= _CONSISTENCY_STABLE and profitable_share >= _CONSISTENCY_STABLE:
        consistency = "stable"
    elif dominant_share < _CONSISTENCY_UNSTABLE or profitable_share < _CONSISTENCY_UNSTABLE:
        consistency = "unstable"
    else:
        consistency = "mixed"

    return {
        "windows": len(traded),
        "paramSets": param_sets,
        "dominantSharePct": dominant_share * 100.0,
        "profitableWindows": len(profitable),
        "profitableSharePct": profitable_share * 100.0,
        "bestWindowPnlSol": max(pnls),
        "worstWindowPnlSol": min(pnls),
        "medianWindowPnlSol": statistics.median(pnls),
        "pnlStdevSol": statistics.pstdev(pnls) if len(pnls) >= 2 else 0.0,
        "consistency": consistency,
    }


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
    window_mode: str = "fixed",
    train_candles: Optional[int] = None,
    test_candles: Optional[int] = None,
) -> Dict[str, Any]:
    """Grid-search each train slice and trade the winner on the test slice.

    ``window_mode`` selects how the slices are built: ``fixed`` (default)
    cuts ``segments`` equal blocks; ``anchored`` uses an expanding train window
    from the first candle; ``rolling`` slides a constant-size train window.

    Returns a JSON-friendly report with per-window choices, a ``stability``
    block (parameter persistence and the share of profitable windows),
    aggregate out-of-sample performance and, when ``baseline_params`` is given,
    the same windows traded with the unoptimized parameters.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {', '.join(OBJECTIVES)}")
    mode = (window_mode or "fixed").strip().lower()
    if mode not in WINDOW_MODES:
        raise ValueError(f"window mode must be one of {', '.join(WINDOW_MODES)}")
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
    windows = window_ranges(
        len(data),
        mode,
        segments=segments,
        train_ratio=train_ratio,
        train_candles=train_candles,
        test_candles=test_candles,
    )
    if mode != "fixed" and not any(
        window["testEnd"] - window["testStart"] >= 10 for window in windows
    ):
        raise ValueError(
            "no walk-forward window has at least 10 test candles; "
            "lower train_candles or test_candles"
        )

    baseline_runner = None
    if baseline_params is not None:
        baseline_runner = make_strategy(strategy_name, **dict(baseline_params))

    results: List[Dict[str, Any]] = []
    baseline_segments: List[Dict[str, Any]] = []
    oos_pnls: List[float] = []
    baseline_pnls: List[float] = []
    baseline_test_candles = 0

    for window in windows:
        segment = window["index"]
        train = data[window["trainStart"] : window["trainEnd"]]
        test = data[window["testStart"] : window["testEnd"]]
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
            score = objective_value(summary, objective)
            if best is None or score > best[0]:
                best = (score, params, summary)

        if best is None:
            results.append(
                {
                    "segment": segment + 1,
                    "trainCandles": len(train),
                    "testCandles": len(test),
                    "trainStartTs": train[0].ts if train else None,
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
                "trainStartTs": train[0].ts if train else None,
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
        "windowMode": mode,
        "minTrades": min_trades,
        "trainRatio": train_ratio if mode == "fixed" else None,
        "segmentCount": segments if mode == "fixed" else len(windows),
        "windowCount": len(windows),
        "grid": {name: list(values) for name, values in grid.items()},
        "combinations": len(combinations),
        "segments": results,
        "stability": _stability(results),
        "aggregate": aggregate,
    }
    if mode != "fixed":
        first = windows[0]
        report["trainCandles"] = first["trainEnd"] - first["trainStart"]
        report["testCandles"] = first["testEnd"] - first["testStart"]

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
        f"- Window mode: `{report.get('windowMode', 'fixed')}` "
        f"({report.get('windowCount', report.get('segmentCount', 0))} window(s))",
        f"- Grid: {grid_text or '—'} ({report.get('combinations', 0)} combinations)",
    ]
    if report.get("windowMode", "fixed") == "fixed":
        lines.append(
            f"- Segments: {report.get('segmentCount', 0)} "
            f"(train ratio {_fmt(float(report.get('trainRatio') or 0.0) * 100, 0)}%)"
        )
    else:
        lines.append(
            f"- Window sizes: {report.get('trainCandles', '—')} train / "
            f"{report.get('testCandles', '—')} test candles"
        )
    lines += [
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

    stability = report.get("stability") or {}
    if stability.get("windows"):
        lines += [
            "",
            "## Stability",
            "",
            f"- Consistency: **{stability.get('consistency', 'unknown')}**",
            f"- Dominant parameter set share: {_fmt(stability.get('dominantSharePct'), 1)}%",
            f"- Profitable windows: {stability.get('profitableWindows', 0)}"
            f"/{stability.get('windows', 0)} ({_fmt(stability.get('profitableSharePct'), 1)}%)",
            f"- Window PnL: best {_fmt(stability.get('bestWindowPnlSol'), 6)}, "
            f"median {_fmt(stability.get('medianWindowPnlSol'), 6)}, "
            f"worst {_fmt(stability.get('worstWindowPnlSol'), 6)} SOL "
            f"(stdev {_fmt(stability.get('pnlStdevSol'), 6)})",
            "",
            "| Parameter set | Windows | Share |",
            "|---|---|---|",
        ]
        for entry in stability.get("paramSets") or []:
            lines.append(
                f"| {_params_text(entry.get('params'))} | {entry.get('count', 0)} | "
                f"{_fmt(entry.get('sharePct'), 1)}% |"
            )

    if "upliftSol" in report:
        lines.append(
            f"Uplift over baseline: **{_fmt(report['upliftSol'], 6)} SOL** "
            f"(baseline {_params_text((report.get('baseline') or {}).get('params'))})."
        )
    return "\n".join(lines)
