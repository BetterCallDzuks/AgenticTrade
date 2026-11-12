"""Environment validation shared by the bot and the `doctor` CLI.

The rules here are intentionally *advisory but explicit*: every check returns an
`Issue` with a level instead of raising, so the bot can keep running with a
legacy `.env` while `python -m agentictrade.doctor` explains what to fix.

Levels:
- ``fail``: the value is unsafe or unusable (doctor exits non-zero)
- ``warn``: the value is legal but suspicious or risky
- ``info``: a note about optional features
"""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from agentictrade.strategies import PROFILES, normalize_profile


@dataclass(frozen=True)
class Issue:
    key: str
    level: str
    message: str

    def as_dict(self) -> Dict[str, str]:
        return {"key": self.key, "level": self.level, "message": self.message}


_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off", "")


def _raw(env: Mapping[str, str], key: str) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_bool(value: Optional[str]) -> Optional[bool]:
    """Parse a boolean env value, returning None when it is not recognisable."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def _as_float(env: Mapping[str, str], key: str) -> Optional[float]:
    text = _raw(env, key)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(env: Mapping[str, str], key: str) -> Optional[int]:
    text = _raw(env, key)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


class _Collector:
    def __init__(self) -> None:
        self.issues: List[Issue] = []

    def fail(self, key: str, message: str) -> None:
        self.issues.append(Issue(key, "fail", message))

    def warn(self, key: str, message: str) -> None:
        self.issues.append(Issue(key, "warn", message))

    def info(self, key: str, message: str) -> None:
        self.issues.append(Issue(key, "info", message))

    def numeric(
        self,
        env: Mapping[str, str],
        key: str,
        *,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
        integer: bool = False,
        warn_min: Optional[float] = None,
        warn_max: Optional[float] = None,
        allow_zero: bool = False,
    ) -> Optional[float]:
        """Validate one numeric key and return its parsed value (or None)."""
        text = _raw(env, key)
        if text is None:
            return None
        value = _as_int(env, key) if integer else _as_float(env, key)
        if value is None:
            kind = "integer" if integer else "number"
            self.fail(key, f"expected a {kind}, got {text!r}")
            return None
        if not allow_zero and value == 0:
            self.fail(key, "must not be zero")
            return value
        if minimum is not None and value < minimum:
            self.fail(key, f"must be >= {minimum:g}, got {value:g}")
        if maximum is not None and value > maximum:
            self.fail(key, f"must be <= {maximum:g}, got {value:g}")
        if warn_min is not None and value < warn_min:
            self.warn(key, f"{value:g} is unusually low (expected >= {warn_min:g})")
        if warn_max is not None and value > warn_max:
            self.warn(key, f"{value:g} is unusually high (expected <= {warn_max:g})")
        return value


def validate_env(env: Mapping[str, str]) -> List[Issue]:
    """Return every configuration issue found in ``env``."""
    c = _Collector()

    # ---- execution mode / live trading safety -------------------------
    live_text = _raw(env, "LIVE_TRADING_ENABLED")
    live = parse_bool(live_text)
    if live_text is not None and live is None:
        c.fail("LIVE_TRADING_ENABLED", f"expected true/false, got {live_text!r}")
    confirmed_text = _raw(env, "LIVE_TRADING_CONFIRMED")
    confirmed = parse_bool(confirmed_text)
    if confirmed_text is not None and confirmed is None:
        c.fail("LIVE_TRADING_CONFIRMED", f"expected true/false, got {confirmed_text!r}")

    if live:
        if not confirmed:
            c.fail(
                "LIVE_TRADING_CONFIRMED",
                "live trading is enabled but not confirmed; set LIVE_TRADING_CONFIRMED=true "
                "only after reading RELEASE_CHECKLIST.md",
            )
        if not _raw(env, "SOLANA_PRIVATE_KEY"):
            c.fail("SOLANA_PRIVATE_KEY", "required when live trading is enabled")
        c.warn(
            "LIVE_TRADING_ENABLED",
            "live trading is ON; paper trading is the safe default",
        )
    elif live is False:
        c.info("LIVE_TRADING_ENABLED", "paper trading mode (no on-chain orders)")

    max_live = c.numeric(
        env, "MAX_LIVE_TRADE_SOL", minimum=0.0, maximum=5.0, warn_max=0.25
    )
    if live and (max_live is None or max_live <= 0):
        c.fail("MAX_LIVE_TRADE_SOL", "must be set above zero when live trading is enabled")

    # ---- portfolio sizing --------------------------------------------
    initial = c.numeric(env, "INITIAL_PAPER_SOL", minimum=0.01, maximum=1_000_000.0)
    trade = c.numeric(env, "TRADE_SIZE_SOL", minimum=0.0001, maximum=1_000.0)
    if initial is not None and trade is not None and trade > initial:
        c.fail(
            "TRADE_SIZE_SOL",
            f"trade size {trade:g} exceeds initial bankroll {initial:g}",
        )

    max_pos = c.numeric(env, "MAX_POSITION_SOL", minimum=0.0001, maximum=1_000.0)
    min_pos = c.numeric(
        env, "MIN_POSITION_SOL", minimum=0.0, maximum=1_000.0, allow_zero=True
    )
    if max_pos is not None and min_pos is not None and min_pos > max_pos:
        c.fail("MIN_POSITION_SOL", f"min {min_pos:g} is above max {max_pos:g}")

    c.numeric(env, "MAX_OPEN_POSITIONS", minimum=1, maximum=50, integer=True, warn_max=10)
    c.numeric(env, "MIN_SECONDS_BETWEEN_ENTRIES", minimum=0, maximum=86_400, integer=True)
    c.numeric(env, "POSITION_MONITOR_SEC", minimum=1, maximum=3_600, warn_min=2)
    c.numeric(env, "MAX_HOLD_NO_PUMP_SEC", minimum=1, maximum=604_800, integer=True)

    # ---- exit rules ---------------------------------------------------
    c.numeric(env, "STOP_LOSS_PCT", minimum=-100.0, maximum=-0.1)
    c.numeric(env, "TP1_PCT", minimum=1.0, maximum=10_000.0, warn_min=10.0)
    c.numeric(env, "TRAILING_STOP_PCT", minimum=0.0, maximum=99.0, allow_zero=True)
    c.numeric(
        env, "TRAILING_STOP_PCT_AFTER_TP1", minimum=0.0, maximum=99.0, allow_zero=True
    )
    c.numeric(env, "FAST_INVALIDATION_PCT", minimum=-100.0, maximum=-0.1)
    c.numeric(env, "FAST_INVALIDATION_WINDOW_SEC", minimum=0, maximum=86_400, integer=True)
    c.numeric(env, "STEP1_TRIGGER_PCT", minimum=0.0, maximum=10_000.0, allow_zero=True)
    c.numeric(env, "STEP1_FLOOR_PCT", minimum=-100.0, maximum=10_000.0, allow_zero=True)
    c.numeric(env, "STEP2_TRIGGER_PCT", minimum=0.0, maximum=10_000.0, allow_zero=True)
    c.numeric(env, "STEP2_FLOOR_PCT", minimum=-100.0, maximum=10_000.0, allow_zero=True)
    c.numeric(env, "BREAKEVEN_BUFFER_PCT", minimum=0.0, maximum=100.0, allow_zero=True)

    # ---- entry gates --------------------------------------------------
    c.numeric(env, "ENTRY_CONFIRM_SEC", minimum=0, maximum=3_600, integer=True)
    c.numeric(env, "ENTRY_CONFIRM_MAX_DROP_PCT", minimum=0.0, maximum=100.0, allow_zero=True)
    c.numeric(env, "MIN_LIQUIDITY_USD", minimum=0.0, maximum=1e12, allow_zero=True)
    c.numeric(env, "MIN_VOLUME_H1_USD", minimum=0.0, maximum=1e12, allow_zero=True)
    c.numeric(env, "MAX_TOP10_HOLDER_PCT", minimum=1.0, maximum=100.0)
    c.numeric(env, "MAX_CHASE_H1_PCT", minimum=0.0, maximum=10_000.0, allow_zero=True)
    c.numeric(env, "REJECT_M5_DUMP_PCT", minimum=-100.0, maximum=0.0, allow_zero=True)
    c.numeric(env, "STALE_PRICE_SEC", minimum=1, maximum=86_400)
    c.numeric(env, "CLUSTER_COOLDOWN_SEC", minimum=0, maximum=604_800, integer=True)
    c.numeric(env, "CLUSTER_OPEN_DD_PCT", minimum=0.0, maximum=100.0, allow_zero=True)
    c.numeric(env, "CLUSTER_UNPROVEN_SEC", minimum=0, maximum=604_800, integer=True)
    c.numeric(env, "STOP_LOSS_COOLDOWN_SEC", minimum=0, maximum=604_800, integer=True)

    # ---- daily limits -------------------------------------------------
    c.numeric(env, "MAX_DAILY_TRADES", minimum=1, maximum=10_000, integer=True)
    c.numeric(env, "MAX_DAILY_STOP_LOSSES", minimum=1, maximum=10_000, integer=True)
    c.numeric(env, "MAX_DAILY_DRAWDOWN_PCT", minimum=0.1, maximum=100.0)

    # ---- cost model ---------------------------------------------------
    c.numeric(env, "PAPER_SLIPPAGE_BPS", minimum=0.0, maximum=5_000.0, allow_zero=True)
    c.numeric(env, "PAPER_EXIT_SLIPPAGE_BPS", minimum=0.0, maximum=5_000.0, allow_zero=True)
    c.numeric(env, "PAPER_IMPACT_COEFF", minimum=0.0, maximum=100.0, allow_zero=True)
    c.numeric(env, "PAPER_SOL_PRICE_USD", minimum=1.0, maximum=1_000_000.0)
    c.numeric(env, "SLIPPAGE_BPS", minimum=0.0, maximum=5_000.0, allow_zero=True)
    c.numeric(env, "PRIORITY_FEE_SOL", minimum=0.0, maximum=10.0, allow_zero=True)

    # ---- sizing multipliers -------------------------------------------
    for key in (
        "SIZE_AFTER_LOSS_MULT",
        "SIZE_AI_ONLY_MULT",
        "SIZE_HEURISTIC_MULT",
        "SIZE_DRAWDOWN_MULT",
    ):
        c.numeric(env, key, minimum=0.0, maximum=10.0, allow_zero=True)
    c.numeric(env, "SIZE_DRAWDOWN_TRIGGER_PCT", minimum=0.0, maximum=100.0, allow_zero=True)

    # ---- flow exit / wallet quality -----------------------------------
    c.numeric(env, "FLOW_EXIT_MIN_SELLS", minimum=1, maximum=10_000, integer=True)
    c.numeric(env, "FLOW_EXIT_SELL_RATIO", minimum=0.1, maximum=100.0)
    c.numeric(env, "WALLET_QUALITY_MIN_SAMPLES", minimum=1, maximum=10_000, integer=True)
    c.numeric(env, "WALLET_QUALITY_MIN_WIN_RATE", minimum=0.0, maximum=1.0)
    c.numeric(env, "WALLET_QUALITY_WINDOW", minimum=1, maximum=1_000, integer=True)

    # ---- API / observability ------------------------------------------
    c.numeric(env, "API_RATE_LIMIT_PER_MIN", minimum=1, maximum=100_000, integer=True, warn_min=30)
    c.numeric(env, "PRICE_CACHE_TTL_SEC", minimum=0.0, maximum=3_600.0, allow_zero=True)
    c.numeric(env, "ALERT_COOLDOWN_SEC", minimum=0.0, maximum=86_400.0, allow_zero=True)

    token = _raw(env, "DASHBOARD_TOKEN")
    if not token:
        c.warn(
            "DASHBOARD_TOKEN",
            "empty token; the API is unauthenticated (set COMMAND_CENTER_HOST=127.0.0.1 "
            "or configure a token)",
        )
    elif len(token) < 12:
        c.warn("DASHBOARD_TOKEN", "shorter than 12 characters; use a long random token")

    if parse_bool(_raw(env, "CORS_ALLOW_ALL")):
        c.warn("CORS_ALLOW_ALL", "every browser origin is allowed; prefer ALLOWED_ORIGINS")

    if parse_bool(_raw(env, "JSON_LOGS")):
        c.info("JSON_LOGS", "structured JSON logging enabled")

    profile = _raw(env, "STRATEGY_PROFILE")
    if profile and normalize_profile(profile) != profile.strip().lower():
        c.warn(
            "STRATEGY_PROFILE",
            f"unknown profile {profile!r}; falling back to 'balanced' "
            f"(known: {', '.join(sorted(PROFILES))})",
        )

    timezone_name = _raw(env, "BOT_TIMEZONE")
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(timezone_name)
        except Exception:
            c.warn(
                "BOT_TIMEZONE",
                f"cannot load timezone {timezone_name!r}; a fixed CET/CEST fallback is used",
            )

    if not _raw(env, "GEMINI_API_KEY"):
        c.info("GEMINI_API_KEY", "missing; the research agent uses the heuristic mode")

    return c.issues


def summarize(issues: List[Issue]) -> Tuple[int, int, int]:
    """Return (failures, warnings, infos)."""
    fails = sum(1 for issue in issues if issue.level == "fail")
    warns = sum(1 for issue in issues if issue.level == "warn")
    infos = sum(1 for issue in issues if issue.level == "info")
    return fails, warns, infos
