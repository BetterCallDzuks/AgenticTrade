"""Named paper-trading risk profiles.

Profiles only fill values that are not explicitly set in the environment, so an
existing .env keeps full control. `balanced` mirrors the historical defaults.
"""

from typing import Dict

PROFILES: Dict[str, Dict[str, float]] = {
    "conservative": {
        "TRADE_SIZE_SOL": 0.25,
        "MAX_OPEN_POSITIONS": 2,
        "STOP_LOSS_PCT": -18.0,
        "TP1_PCT": 60.0,
        "TRAILING_STOP_PCT": 15.0,
        "TRAILING_STOP_PCT_AFTER_TP1": 12.0,
        "MAX_DAILY_STOP_LOSSES": 1,
        "MAX_DAILY_DRAWDOWN_PCT": 3.0,
        "MAX_DAILY_TRADES": 4,
        "FAST_INVALIDATION_PCT": -8.0,
    },
    "balanced": {
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
    },
    "aggressive": {
        "TRADE_SIZE_SOL": 0.75,
        "MAX_OPEN_POSITIONS": 3,
        "STOP_LOSS_PCT": -35.0,
        "TP1_PCT": 120.0,
        "TRAILING_STOP_PCT": 25.0,
        "TRAILING_STOP_PCT_AFTER_TP1": 18.0,
        "MAX_DAILY_STOP_LOSSES": 3,
        "MAX_DAILY_DRAWDOWN_PCT": 8.0,
        "MAX_DAILY_TRADES": 12,
        "FAST_INVALIDATION_PCT": -18.0,
    },
}

# Maps profile keys to PaperTradingManager attribute names.
ATTRIBUTE_MAP = {
    "TRADE_SIZE_SOL": "trade_size_sol",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "STOP_LOSS_PCT": "stop_loss_pct",
    "TP1_PCT": "tp1_pct",
    "TRAILING_STOP_PCT": "trailing_stop_pct",
    "TRAILING_STOP_PCT_AFTER_TP1": "trailing_stop_pct_after_tp1",
    "MAX_DAILY_STOP_LOSSES": "max_daily_stop_losses",
    "MAX_DAILY_DRAWDOWN_PCT": "max_daily_drawdown_pct",
    "MAX_DAILY_TRADES": "max_daily_trades",
    "FAST_INVALIDATION_PCT": "fast_invalidation_pct",
}


def normalize_profile(name: str = "balanced") -> str:
    value = (name or "").strip().lower()
    return value if value in PROFILES else "balanced"


def profile_values(name: str = "balanced") -> Dict[str, float]:
    return dict(PROFILES[normalize_profile(name)])


def resolve_profile(env: dict, name: str = "balanced") -> Dict[str, object]:
    """Return attribute overrides for env keys that are absent or empty."""
    overrides: Dict[str, object] = {}
    for key, value in profile_values(name).items():
        if not str(env.get(key) or "").strip():
            overrides[ATTRIBUTE_MAP[key]] = value
    return overrides
