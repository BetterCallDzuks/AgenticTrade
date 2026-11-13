"""Paper fill model and exit metadata.

The paper book does not have an order book, so fills are modelled with a flat
slippage plus a linear market-impact term based on trade size relative to pool
liquidity. This keeps results conservative and reproducible in tests.
"""

from typing import Dict, Optional, Tuple

from agentictrade.costs import CostModel

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
    """Return ``(fill price, cost fraction)`` for a paper order.

    This is the legacy two-value interface. It delegates to the shared
    :class:`CostModel` with AMM and flat fees disabled; new code should build a
    ``CostModel`` directly so every cost component is explicit.
    """
    model = CostModel(
        base_bps=base_bps,
        amm_fee_bps=0.0,
        impact_coeff=impact_coeff,
        priority_fee_sol=0.0,
        failed_tx_prob=0.0,
        sol_price_usd=sol_price_usd,
    )
    if side.upper() == "BUY":
        fill = model.entry(mid_price, size_sol, liquidity_usd)
        return fill.price, fill.slippage_fraction
    fill = model.exit_value(size_sol, mid_price, liquidity_usd)
    return fill.price, fill.slippage_fraction


def exit_icon(reason: Optional[str]) -> str:
    return EXIT_ICONS.get(str(reason or "").strip().upper(), "📤")
