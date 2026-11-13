"""Single source of truth for strategy defaults and the effective config hash.

The balanced profile is the historical default. Before this module existed the
same numbers were repeated in ``strategies.PROFILES``, ``PaperTradingManager``
and ``ohlcv.BacktestConfig``, so a change in one place silently measured a
different strategy in another. Import ``STRATEGY_DEFAULTS`` instead of
hardcoding the numbers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

DEFAULT_PROFILE = "balanced"

STRATEGY_DEFAULTS: dict[str, float] = {
    "TRADE_SIZE_SOL": 0.5,
    "MAX_OPEN_POSITIONS": 2,
    "STOP_LOSS_PCT": -25.0,
    "TP1_PCT": 80.0,
    "TRAILING_STOP_PCT": 20.0,
    "TRAILING_STOP_PCT_AFTER_TP1": 15.0,
    "MAX_DAILY_STOP_LOSSES": 2,
    "MAX_DAILY_DRAWDOWN_PCT": 5.0,
    "MAX_DAILY_TRADES": 8,
    "FAST_INVALIDATION_PCT": -12.0,
}

# Transaction-cost defaults shared by the paper book and the OHLCV backtester.
# ``base_bps`` applies to buys, ``exit_base_bps`` to sells; the paper book has
# always crossed a wider spread on the way out.
COST_DEFAULTS: dict[str, float] = {
    "base_bps": 50.0,
    "exit_base_bps": 80.0,
    "amm_fee_bps": 25.0,
    "impact_coeff": 1.0,
    "impact_exponent": 1.0,
    "priority_fee_sol": 0.0015,
    "failed_tx_prob": 0.02,
    "sol_price_usd": 185.0,
}


# Exit-policy defaults. ``agentictrade.exits.ExitPolicy`` is the shared
# implementation; these values are the ones the balanced profile has always used.
EXIT_DEFAULTS: dict[str, float] = {
    "tp1_fraction": 0.5,
    "trailing_arm_gain_pct": 30.0,
    "breakeven_buffer_pct": 1.0,
    "step1_trigger_pct": 30.0,
    "step1_floor_pct": -5.0,
    "step2_trigger_pct": 50.0,
    "step2_floor_pct": 15.0,
    "fast_invalidation_pct": -12.0,
    "fast_invalidation_window_sec": 90.0,
    "max_hold_no_pump_sec": 900.0,
    "time_stop_min_profit_pct": 20.0,
    "flow_exit_min_sells": 3.0,
    "flow_exit_sell_ratio": 1.3,
}

# LLM provider defaults. ``auto`` uses Gemini when a key is present, otherwise a
# local OpenAI-compatible server when LLM_BASE_URL is set, otherwise heuristics.
LLM_DEFAULTS: dict = {
    "provider": "auto",
    "base_url": "",
    "timeout_sec": 60.0,
    "temperature": 0.2,
    "max_tokens": 1024,
}


def config_hash(values: Mapping[str, Any], extra: Optional[Mapping[str, Any]] = None) -> str:
    """Return a short, stable hash of an effective configuration.

    Floats are normalised so ``80`` and ``80.0`` hash identically. Nested
    mappings are sorted by key, so the hash is reproducible across processes.
    """
    payload: dict[str, Any] = {str(key): _normalise(value) for key, value in values.items()}
    if extra:
        for key, value in extra.items():
            payload[str(key)] = _normalise(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def _normalise(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {str(key): _normalise(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value
