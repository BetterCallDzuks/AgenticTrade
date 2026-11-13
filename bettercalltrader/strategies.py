"""Named paper-trading risk profiles.

Profiles only fill values that are not explicitly set in the environment, so an
existing .env keeps full control. `balanced` mirrors the historical defaults,
which now live in :mod:`bettercalltrader.defaults`.

The exception is an *override profile* (currently only ``high_risk``): a risk
mode is an explicit opt-in, so its values win over the environment. Otherwise a
legacy .env that pins the balanced sizing/exits would silently turn
``STRATEGY_PROFILE=high_risk`` into a no-op, which is the worst possible
outcome for a mode a user asked for.
"""

from typing import Dict

from bettercalltrader.defaults import STRATEGY_DEFAULTS

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
    "balanced": dict(STRATEGY_DEFAULTS),
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
    # High risk / high reward: 3x the balanced size, twice the concurrent
    # exposure, a wider stop that survives memecoin wicks, a much higher TP1
    # on a smaller fraction (the rest rides the trailing stop), and looser
    # daily limits. Entry filters (liquidity, holders, mint/freeze) are
    # unchanged on purpose: the mode scales the bet, it does not buy junk.
    "high_risk": {
        "TRADE_SIZE_SOL": 1.5,
        "MAX_POSITION_SOL": 1.5,
        "MAX_OPEN_POSITIONS": 4,
        "STOP_LOSS_PCT": -45.0,
        "TP1_PCT": 250.0,
        "TP1_FRACTION": 0.35,
        "TRAILING_STOP_PCT": 35.0,
        "TRAILING_STOP_PCT_AFTER_TP1": 25.0,
        "TRAILING_ARM_GAIN_PCT": 60.0,
        "MAX_DAILY_STOP_LOSSES": 5,
        "MAX_DAILY_DRAWDOWN_PCT": 20.0,
        "MAX_DAILY_TRADES": 25,
        "FAST_INVALIDATION_PCT": -25.0,
        "MIN_SECONDS_BETWEEN_ENTRIES": 20,
        "STOP_LOSS_COOLDOWN_SEC": 60,
    },
}

# Accepted spellings for the profile names.
PROFILE_ALIASES = {
    "high-risk": "high_risk",
    "highrisk": "high_risk",
    "high risk": "high_risk",
    "degen": "high_risk",
    "risky": "high_risk",
}

# Profiles whose values always win over the environment (see module docstring).
OVERRIDE_PROFILES = frozenset({"high_risk"})

# Maps profile keys to PaperTradingManager attribute names.
ATTRIBUTE_MAP = {
    "TRADE_SIZE_SOL": "trade_size_sol",
    "MAX_POSITION_SOL": "max_position_sol",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "STOP_LOSS_PCT": "stop_loss_pct",
    "TP1_PCT": "tp1_pct",
    "TP1_FRACTION": "tp1_fraction",
    "TRAILING_STOP_PCT": "trailing_stop_pct",
    "TRAILING_STOP_PCT_AFTER_TP1": "trailing_stop_pct_after_tp1",
    "TRAILING_ARM_GAIN_PCT": "trailing_arm_gain_pct",
    "MAX_DAILY_STOP_LOSSES": "max_daily_stop_losses",
    "MAX_DAILY_DRAWDOWN_PCT": "max_daily_drawdown_pct",
    "MAX_DAILY_TRADES": "max_daily_trades",
    "FAST_INVALIDATION_PCT": "fast_invalidation_pct",
    "MIN_SECONDS_BETWEEN_ENTRIES": "min_seconds_between_entries",
    "STOP_LOSS_COOLDOWN_SEC": "stop_loss_cooldown_sec",
}


def normalize_profile(name: str = "balanced") -> str:
    value = (name or "").strip().lower()
    value = PROFILE_ALIASES.get(value, value)
    return value if value in PROFILES else "balanced"


def profile_values(name: str = "balanced") -> Dict[str, float]:
    return dict(PROFILES[normalize_profile(name)])


def is_override_profile(name: str = "balanced") -> bool:
    """True for profiles that intentionally overwrite the environment."""
    return normalize_profile(name) in OVERRIDE_PROFILES


def is_known_profile(name: str = "") -> bool:
    """True when ``name`` is a profile or a documented alias."""
    value = (name or "").strip().lower()
    return value in PROFILES or value in PROFILE_ALIASES


def resolve_profile(env: dict, name: str = "balanced", force: bool = False) -> Dict[str, object]:
    """Return attribute overrides for env keys that are absent or empty.

    ``force`` applies every profile value regardless of the environment; it is
    used for :data:`OVERRIDE_PROFILES` (``high_risk``).
    """
    overrides: Dict[str, object] = {}
    for key, value in profile_values(name).items():
        attr = ATTRIBUTE_MAP.get(key)
        if attr is None:
            continue
        if force or not str(env.get(key) or "").strip():
            overrides[attr] = value
    return overrides
