"""Paper fill model and exit metadata.

The paper book does not have an order book, so fills are modelled with a flat
slippage plus a linear market-impact term based on trade size relative to pool
liquidity. This keeps results conservative and reproducible in tests.
"""

from typing import Dict, Optional, Tuple

EXIT_ICONS: Dict[str, str] = {
    "STOP_LOSS": "🛑",
    "FAST_INVALIDATION": "⚡",
    "TRAILING_STOP": "📉",
    "BREAKEVEN_STOP": "🛡️",
    "STEP_UP_STOP": "🪜",
    "TIME_STOP": "⏱️",
    "FLOW_EXIT": "💧",
    "WHALE_EXIT": "🐳",
    "TAKE_PROFIT_1": "🎯",
    "MANUAL_WEB_EXIT": "✋",
    "TELEGRAM_EXIT": "📱",
    "REPLAY_END": "⏹️",
}

LOSING_EXIT_REASONS = ("STOP_LOSS", "FAST_INVALIDATION", "FLOW_EXIT", "WHALE_EXIT", "TIME_STOP")
FAST_EXIT_REASONS = ("STOP_LOSS", "FAST_INVALIDATION", "FLOW_EXIT", "WHALE_EXIT")


def paper_fill(
    mid_price: float,
    side: str,
    size_sol: float,
    liquidity_usd: Optional[float],
    base_bps: float,
    impact_coeff: float,
    sol_price_usd: float = 185.0,
) -> Tuple[float, float]:
    """Return (fill price, slippage fraction) for a paper order."""
    liq_usd = float(liquidity_usd or 0.0)
    liquidity_sol = liq_usd / max(float(sol_price_usd or 185.0), 1e-9)
    impact = (max(0.0, float(size_sol)) / max(liquidity_sol, 1.0)) * max(0.0, float(impact_coeff))
    slip = max(0.0, float(base_bps)) / 10000.0 + impact
    slip = min(0.5, max(0.0, slip))
    if side.upper() == "BUY":
        return mid_price * (1.0 + slip), slip
    return mid_price * (1.0 - slip), slip


def exit_icon(reason: Optional[str]) -> str:
    return EXIT_ICONS.get(str(reason or "").strip().upper(), "📤")
