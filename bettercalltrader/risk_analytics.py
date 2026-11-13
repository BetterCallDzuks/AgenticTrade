"""Risk analytics helpers used by the v10 portfolio risk and backtest layers.

The functions are pure and dependency-free so they can be unit tested without
a database or network access.
"""

import math
from typing import Iterable, List, Sequence


def _clean(values: Iterable[float]) -> List[float]:
    out: List[float] = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def max_drawdown_pct(pnls: Sequence[float], initial_equity: float = 0.0) -> float:
    """Maximum peak-to-trough drawdown of a realized-PnL equity curve, in percent."""
    equity = float(initial_equity)
    peak = equity
    worst = 0.0
    for pnl in _clean(pnls):
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst * 100.0


def historical_var(pnls: Sequence[float], confidence: float = 0.95) -> float:
    """Historical Value-at-Risk as a positive SOL loss at the given confidence."""
    values = sorted(_clean(pnls))
    if not values:
        return 0.0
    confidence = min(max(float(confidence), 0.5), 0.999)
    index = max(0, min(len(values) - 1, int((1.0 - confidence) * len(values))))
    return max(0.0, -values[index])


def expected_shortfall(pnls: Sequence[float], confidence: float = 0.95) -> float:
    """Average loss in the worst tail beyond the historical VaR threshold."""
    values = sorted(_clean(pnls))
    if not values:
        return 0.0
    confidence = min(max(float(confidence), 0.5), 0.999)
    cutoff = max(1, int((1.0 - confidence) * len(values)))
    tail = values[:cutoff]
    return max(0.0, -(sum(tail) / len(tail)))


def profit_factor(pnls: Sequence[float]) -> float:
    values = _clean(pnls)
    gross_win = sum(v for v in values if v > 0)
    gross_loss = -sum(v for v in values if v < 0)
    if gross_loss <= 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def expectancy(pnls: Sequence[float]) -> float:
    values = _clean(pnls)
    return (sum(values) / len(values)) if values else 0.0


def win_rate(pnls: Sequence[float]) -> float:
    values = _clean(pnls)
    if not values:
        return 0.0
    return sum(1 for v in values if v > 0) / len(values)


def herfindahl_index(weights: Sequence[float]) -> float:
    """HHI of portfolio weights. 0 = perfectly spread, 1 = single position."""
    values = [abs(float(w)) for w in weights if w is not None]
    total = sum(values)
    if total <= 0:
        return 0.0
    return sum((v / total) ** 2 for v in values)


def average_win_loss(pnls: Sequence[float]) -> dict:
    values = _clean(pnls)
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    return {
        "avgWinSol": (sum(wins) / len(wins)) if wins else 0.0,
        "avgLossSol": (sum(losses) / len(losses)) if losses else 0.0,
        "wins": len(wins),
        "losses": len(losses),
    }


def summarize(pnls: Sequence[float], initial_equity: float = 0.0) -> dict:
    """One-call summary used by both the API and the backtest report."""
    values = _clean(pnls)
    net = sum(values)
    win_loss = average_win_loss(values)
    pf = profit_factor(values)
    return {
        "trades": len(values),
        "netPnlSol": net,
        "winRate": win_rate(values),
        "profitFactor": pf if math.isfinite(pf) else None,
        "expectancySol": expectancy(values),
        "maxDrawdownPct": max_drawdown_pct(values, initial_equity),
        "var95Sol": historical_var(values, 0.95),
        "expectedShortfall95Sol": expected_shortfall(values, 0.95),
        **win_loss,
    }
