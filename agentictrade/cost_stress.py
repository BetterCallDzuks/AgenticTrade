"""Cost stress testing and break-even analysis for the OHLCV backtester.

A backtest is only interesting if its edge survives the cost assumptions. This
module replays the same candles and strategy with the modelled costs scaled by
a multiplier and answers the question a single backtest cannot: *at what cost
level does this strategy stop making money?*

``multiplier=0`` removes every modelled cost (AMM fee, spread/impact, priority
fee, failed-transaction risk), so its net PnL is the gross edge. ``1.0`` is the
cost level the backtester normally trades at. Above ``1.0`` is a stress test for
a busier pool, a thinner book or a worse slot.

The break-even multiplier is estimated by linear interpolation between the
adjacent sweep points where net PnL changes sign. It is a *model* answer, not a
promise: costs are not perfectly linear in the multiplier.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agentictrade.costs import COST_MODEL_VERSION
from agentictrade.ohlcv import (
    BacktestConfig,
    Candle,
    Strategy,
    normalize_candles,
    run_backtest,
)

DEFAULT_MULTIPLIERS: tuple = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)

_POINT_KEYS = (
    "trades",
    "netPnlSol",
    "returnPct",
    "winRate",
    "profitFactor",
    "expectancySol",
    "maxDrawdownPct",
    "feesPaidSol",
    "costsPaidSol",
    "exitReasons",
)


def parse_multipliers(text: Optional[str]) -> List[float]:
    """Parse ``"0,0.5,1,2"`` into a sorted, de-duplicated list. Empty uses defaults."""
    if not text:
        return list(DEFAULT_MULTIPLIERS)
    return _normalize_multipliers(str(text).split(","))


def _normalize_multipliers(values: Sequence[Any]) -> List[float]:
    out: List[float] = []
    for chunk in values:
        if isinstance(chunk, str) and not chunk.strip():
            continue
        try:
            value = float(chunk)
        except (TypeError, ValueError):
            raise ValueError(f"invalid cost multiplier {chunk!r}") from None
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"cost multiplier must be finite and non-negative, got {value!r}")
        out.append(value)
    if not out:
        raise ValueError("no cost multipliers supplied")
    return sorted(set(out))


def scaled_config(config: BacktestConfig, multiplier: float) -> BacktestConfig:
    """Return a copy of ``config`` with every modelled cost scaled.

    ``failed_tx_prob`` is a probability, so it is clamped to ``[0, 1]`` instead
    of being allowed past 1.0. Everything else scales linearly.
    """
    if not math.isfinite(multiplier) or multiplier < 0.0:
        raise ValueError("multiplier must be finite and non-negative")
    return replace(
        config,
        fee_bps=config.fee_bps * multiplier,
        slippage_bps=config.slippage_bps * multiplier,
        exit_slippage_bps=config.exit_slippage_bps * multiplier,
        impact_coeff=config.impact_coeff * multiplier,
        priority_fee_sol=config.priority_fee_sol * multiplier,
        failed_tx_prob=min(max(config.failed_tx_prob * multiplier, 0.0), 1.0),
    )


def _interpolate_break_even(points: Sequence[Mapping[str, Any]]) -> Optional[float]:
    """Multiplier where net PnL first crosses zero, or ``None`` if it never does.

    A sweep whose very first point is already non-profitable has no cost
    headroom; that is reported as ``unprofitable-at-zero-cost`` rather than a
    break-even at 0x.
    """
    for index, point in enumerate(points):
        pnl = float(point["netPnlSol"])
        if pnl > 0.0:
            continue
        if index == 0:
            return None
        left = points[index - 1]
        left_pnl = float(left["netPnlSol"])
        span = left_pnl - pnl
        if span <= 0.0:
            return float(point["multiplier"])
        weight = left_pnl / span
        return float(left["multiplier"]) + weight * (
            float(point["multiplier"]) - float(left["multiplier"])
        )
    return None


def _point(cfg: BacktestConfig, multiplier: float, summary: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "multiplier": multiplier,
        "feeBps": cfg.fee_bps,
        "slippageBps": cfg.slippage_bps,
        "exitSlippageBps": cfg.exit_slippage_bps,
        "impactCoeff": cfg.impact_coeff,
        "priorityFeeSol": cfg.priority_fee_sol,
        "failedTxProb": cfg.failed_tx_prob,
        "netPnlSol": float(summary.get("netPnlSol") or 0.0),
        "feesPaidSol": float(summary.get("feesPaidSol") or 0.0),
        "costsPaidSol": float(summary.get("costsPaidSol") or 0.0),
        **{key: summary[key] for key in _POINT_KEYS if key in summary},
    }


def stress_costs(
    candles: Sequence[Candle],
    strategy: Strategy,
    config: Optional[BacktestConfig] = None,
    *,
    multipliers: Optional[Sequence[float]] = None,
    baseline_multiplier: float = 1.0,
) -> Dict[str, Any]:
    """Replay one strategy across a multiplier sweep and locate break-even.

    The whole candle series is used for every point, so the only variable is the
    cost level. ``baseline_multiplier`` selects which point is reported as the
    reference (normally 1.0) and must be present in the sweep.
    """
    cfg = config or BacktestConfig()
    data = normalize_candles(candles)
    if not data:
        raise ValueError("no candles supplied")

    values = list(DEFAULT_MULTIPLIERS) if multipliers is None else _normalize_multipliers(multipliers)
    if baseline_multiplier not in values:
        raise ValueError(
            f"baseline multiplier {baseline_multiplier} is not in the sweep {values}"
        )

    points: List[Dict[str, Any]] = []
    for multiplier in values:
        scaled = scaled_config(cfg, multiplier)
        result = run_backtest(data, strategy, scaled)
        points.append(_point(scaled, multiplier, result.summary))

    break_even = _interpolate_break_even(points)
    # The gross edge is only meaningful when the sweep actually includes zero.
    has_zero = bool(points) and float(points[0]["multiplier"]) == 0.0
    gross_edge = float(points[0]["netPnlSol"]) if has_zero else None
    profitable = [point for point in points if float(point["netPnlSol"]) > 0.0]

    if break_even is not None:
        headroom_pct = (break_even / baseline_multiplier - 1.0) * 100.0 if baseline_multiplier else 0.0
        status = "break-even"
    elif not profitable:
        headroom_pct = None
        status = "unprofitable-at-zero-cost" if has_zero else "unprofitable-throughout"
    else:
        headroom_pct = None
        status = "profitable-at-max-multiplier"

    baseline = next(point for point in points if point["multiplier"] == baseline_multiplier)
    report: Dict[str, Any] = {
        "strategy": strategy.name,
        "params": {key: value for key, value in vars(strategy).items() if not key.startswith("_")},
        "candles": len(data),
        "baselineMultiplier": baseline_multiplier,
        "multipliers": values,
        "points": points,
        "grossEdgeSol": gross_edge,
        "status": status,
        "breakEvenMultiplier": break_even,
        "headroomPct": headroom_pct,
        "baseline": baseline,
        "configHash": cfg.fingerprint(strategy),
        "costModelVersion": COST_MODEL_VERSION,
    }
    if status == "unprofitable-at-zero-cost":
        report["reason"] = "the strategy loses money even with every modelled cost removed"
    elif status == "unprofitable-throughout":
        report["reason"] = "the strategy loses money at every tested cost level"
    elif status == "profitable-at-max-multiplier":
        report["reason"] = (
            f"the strategy is still profitable at the highest multiplier "
            f"({max(values):g}x); extend the sweep to find the break-even"
        )
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
    """Markdown rendering of a :func:`stress_costs` report."""
    params = report.get("params") or {}
    params_text = ", ".join(f"{key}={value}" for key, value in sorted(params.items())) or "—"
    break_even = report.get("breakEvenMultiplier")
    headroom = report.get("headroomPct")
    if break_even is None:
        verdict = f"no break-even in the sweep ({report.get('status')})"
    else:
        verdict = f"break-even at **{_fmt(break_even, 2)}x**"
        if headroom is not None:
            verdict += f" ({_fmt(headroom, 1)}% cost headroom)"

    lines = [
        f"# Cost stress — {report.get('strategy', 'strategy')}",
        "",
        f"- Params: {params_text}",
        f"- Candles: {report.get('candles', 0)}",
        f"- Gross edge (costs removed): {_fmt(report.get('grossEdgeSol'), 6)} SOL",
        f"- Verdict: {verdict}",
        "",
        "| Multiplier | Fee bps | Entry bps | Exit bps | Priority SOL | Trades | Net PnL | Return % |"
        " Fees | Costs |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for point in report.get("points") or []:
        flag = " **(baseline)**" if point["multiplier"] == report.get("baselineMultiplier") else ""
        lines.append(
            f"| {_fmt(point['multiplier'], 2)}x{flag} | {_fmt(point['feeBps'], 1)} | "
            f"{_fmt(point['slippageBps'], 1)} | {_fmt(point['exitSlippageBps'], 1)} | "
            f"{_fmt(point['priorityFeeSol'], 6)} | {point.get('trades', 0)} | "
            f"{_fmt(point.get('netPnlSol'), 6)} | {_fmt(point.get('returnPct'), 2)} | "
            f"{_fmt(point.get('feesPaidSol'), 6)} | {_fmt(point.get('costsPaidSol'), 6)} |"
        )
    reason = report.get("reason")
    if reason:
        lines += ["", f"_{reason}._"]
    lines += [
        "",
        "Break-even is interpolated between the adjacent sweep points where net PnL "
        "changes sign; costs are not perfectly linear in the multiplier.",
    ]
    return "\n".join(lines)
