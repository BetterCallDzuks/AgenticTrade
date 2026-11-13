"""Event-driven OHLCV backtester.

This is deliberately separate from `agentictrade.backtest`, which replays the
trades the bot actually recorded. Here you supply historical candles (CSV,
JSON, or GeckoTerminal) and a strategy, and the engine simulates entries and
exits with configurable fees, slippage and signal latency.

The engine is long-only, uses one position at a time and fills signals at the
open of a later candle, so it cannot peek at the close that generated the
signal. Exit checks use candle high/low and the shared
:class:`agentictrade.exits.ExitPolicy`, the same policy the paper book trades:
take profit is a *partial* exit (``tp1_fraction``, 50% by default), the
remainder runs on the effective stop ladder.

A gap through a stop fills at ``min(open, stop)`` and a gap through take profit
at ``max(open, tp)``, so a wide candle is never flattered. Trailing stops use
the peak *before* the current candle, and the entry candle is excluded from
exit checks (its high still counts towards the peak). If a stop and take profit
are both inside the same candle, the stop wins and the trade is flagged
``ambiguousBar``.

Position size is capped by ``max_participation_pct`` of the candle volume when
that is enabled, so a thin candle produces a smaller position instead of a
fantasy fill. The exit also pays for the depth the entry consumed.

Fees and slippage come from :class:`agentictrade.costs.CostModel`, the same
model the paper book and the ML labels use, so a round trip costs the same in
research and in the live paper ledger.

Examples:
    python -m agentictrade.ohlcv --csv candles.csv --strategy momentum
    python -m agentictrade.ohlcv --json candles.json --walk-forward 3
    python -m agentictrade.ohlcv --csv candles.csv --optimize --objective sharpe
    python -m agentictrade.ohlcv --pool <pool-address> --timeframe hour
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from agentictrade.costs import COST_MODEL_VERSION, CostModel, depth_used_fraction
from agentictrade.defaults import COST_DEFAULTS, EXIT_DEFAULTS, STRATEGY_DEFAULTS, config_hash
from agentictrade.exits import END_OF_DATA, SIGNAL, TIME_STOP, ExitPolicy
from agentictrade.risk_analytics import summarize

_TF_TO_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}
_TS_KEYS = ("ts", "time", "timestamp", "date", "datetime", "open_time", "start")
_OPEN_KEYS = ("open", "o")
_HIGH_KEYS = ("high", "h")
_LOW_KEYS = ("low", "l")
_CLOSE_KEYS = ("close", "c")
_VOLUME_KEYS = ("volume", "vol", "basevolume", "base_volume", "v")


@dataclass(frozen=True)
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.ts, self.open, self.high, self.low, self.close)):
            raise ValueError("candle contains a non-finite value")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("candle prices must be positive")
        if self.high < self.low:
            raise ValueError("candle high is below low")


def _pick(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def _to_float(value: Any, label: str, index: int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"row {index}: invalid {label} value {value!r}") from None


def _parse_ts(value: Any, index: int) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e11 else number
    text = str(value).strip()
    if not text:
        raise ValueError(f"row {index}: missing timestamp")
    try:
        number = float(text)
        return number / 1000.0 if number > 1e11 else number
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"row {index}: invalid timestamp {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def candle_from_mapping(row: Mapping[str, Any], index: int = 0) -> Candle:
    ts = _parse_ts(_pick(row, _TS_KEYS), index)
    return Candle(
        ts=ts,
        open=_to_float(_pick(row, _OPEN_KEYS), "open", index),
        high=_to_float(_pick(row, _HIGH_KEYS), "high", index),
        low=_to_float(_pick(row, _LOW_KEYS), "low", index),
        close=_to_float(_pick(row, _CLOSE_KEYS), "close", index),
        volume=_to_float(_pick(row, _VOLUME_KEYS) or 0.0, "volume", index),
    )


def normalize_candles(candles: Sequence[Candle]) -> List[Candle]:
    """Sort by timestamp and keep the last candle for duplicate timestamps."""
    ordered = sorted(candles, key=lambda candle: candle.ts)
    deduped: List[Candle] = []
    for candle in ordered:
        if deduped and candle.ts == deduped[-1].ts:
            deduped[-1] = candle
        else:
            deduped.append(candle)
    return deduped


def load_candles_csv(path: str, delimiter: str = ",") -> List[Candle]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        candles = [candle_from_mapping(row, index) for index, row in enumerate(reader, start=1)]
    return normalize_candles(candles)


def load_candles_json(path: str) -> List[Candle]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = payload.get("candles") or payload.get("data") or []
    if not isinstance(payload, list):
        raise ValueError("JSON must contain a list of candles")
    candles = []
    for index, row in enumerate(payload, start=1):
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            candles.append(
                Candle(
                    ts=_parse_ts(row[0], index),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]) if len(row) > 5 else 0.0,
                )
            )
        else:
            candles.append(candle_from_mapping(row, index))
    return normalize_candles(candles)


async def fetch_geckoterminal_candles(
    pool: str,
    network: str = "solana",
    timeframe: str = "minute",
    aggregate: int = 1,
    limit: int = 1000,
    session: Any = None,
) -> List[Candle]:
    """Fetch USD candles from GeckoTerminal (optional network helper)."""
    if timeframe not in _TF_TO_SECONDS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/{timeframe}"
    params = {"aggregate": max(1, int(aggregate)), "limit": max(1, min(int(limit), 1000))}

    import aiohttp

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GeckoTerminal returned HTTP {resp.status}")
            payload = await resp.json()
    finally:
        if owns_session:
            await session.close()

    rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    candles = [
        Candle(
            ts=float(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )
        for row in rows
        if isinstance(row, (list, tuple)) and len(row) >= 5
    ]
    return normalize_candles(candles)


class Strategy:
    name = "base"

    def generate(self, history: Sequence[Candle]) -> Optional[str]:
        raise NotImplementedError


def _ema(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    multiplier = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = price * multiplier + value * (1.0 - multiplier)
    return value


@dataclass
class EmaCrossStrategy(Strategy):
    fast: int = 9
    slow: int = 21
    min_gap_pct: float = 0.0
    name: str = field(default="momentum", init=False)

    def generate(self, history: Sequence[Candle]) -> Optional[str]:
        if len(history) < self.slow + 2:
            return None
        closes = [candle.close for candle in history]
        fast_now = _ema(closes, self.fast)
        slow_now = _ema(closes, self.slow)
        fast_prev = _ema(closes[:-1], self.fast)
        slow_prev = _ema(closes[:-1], self.slow)
        if fast_now is None or slow_now is None or fast_prev is None or slow_prev is None:
            return None
        threshold = max(0.0, float(self.min_gap_pct)) / 100.0
        buy = fast_prev <= slow_prev * (1.0 + threshold) and fast_now > slow_now * (1.0 + threshold)
        sell = fast_prev >= slow_prev * (1.0 - threshold) and fast_now < slow_now * (1.0 - threshold)
        if buy:
            return "BUY"
        if sell:
            return "SELL"
        return None


@dataclass
class BreakoutStrategy(Strategy):
    lookback: int = 20
    threshold_pct: float = 0.5
    name: str = field(default="breakout", init=False)

    def generate(self, history: Sequence[Candle]) -> Optional[str]:
        if len(history) < self.lookback + 1:
            return None
        window = history[-self.lookback - 1 : -1]
        prior_high = max(candle.high for candle in window)
        prior_low = min(candle.low for candle in window)
        close = history[-1].close
        threshold = max(0.0, float(self.threshold_pct)) / 100.0
        if close > prior_high * (1.0 + threshold):
            return "BUY"
        if close < prior_low * (1.0 - threshold):
            return "SELL"
        return None


def make_strategy(name: str, **params: Any) -> Strategy:
    normalized = (name or "momentum").strip().lower()
    if normalized in ("momentum", "ema", "ema_cross"):
        return EmaCrossStrategy(**params)
    if normalized in ("breakout", "range"):
        return BreakoutStrategy(**params)
    raise ValueError(f"unknown strategy: {name}")


def _strategy_fingerprint(strategy: Strategy) -> Dict[str, Any]:
    """Return the strategy name and its declared parameters for the config hash."""
    params = {key: value for key, value in vars(strategy).items() if not key.startswith("_")}
    return {"name": strategy.name, **params}


@dataclass
class BacktestConfig:
    initial_sol: float = 10.0
    trade_size_sol: float = STRATEGY_DEFAULTS["TRADE_SIZE_SOL"]
    # ``fee_bps`` is the AMM/DEX fee charged on the traded notional, kept under
    # its historical name so existing callers and the CLI stay compatible.
    fee_bps: float = COST_DEFAULTS["amm_fee_bps"]
    slippage_bps: float = COST_DEFAULTS["base_bps"]
    exit_slippage_bps: float = COST_DEFAULTS["exit_base_bps"]
    impact_coeff: float = COST_DEFAULTS["impact_coeff"]
    impact_exponent: float = COST_DEFAULTS["impact_exponent"]
    liquidity_usd: float = 0.0
    priority_fee_sol: float = COST_DEFAULTS["priority_fee_sol"]
    failed_tx_prob: float = COST_DEFAULTS["failed_tx_prob"]
    sol_price_usd: float = COST_DEFAULTS["sol_price_usd"]
    latency_candles: int = 1
    # Flat overrides for the three knobs the CLI exposes. Everything else lives
    # in ``policy``; ``exit_policy()`` merges the two.
    stop_loss_pct: float = STRATEGY_DEFAULTS["STOP_LOSS_PCT"]
    take_profit_pct: float = STRATEGY_DEFAULTS["TP1_PCT"]
    trailing_stop_pct: float = STRATEGY_DEFAULTS["TRAILING_STOP_PCT"]
    tp1_fraction: float = EXIT_DEFAULTS["tp1_fraction"]
    final_take_profit_pct: float = 0.0
    policy: ExitPolicy = field(default_factory=ExitPolicy)
    # 0 disables the cap. Otherwise a fill cannot exceed this share of the
    # candle's volume (a thin candle fills a smaller position).
    max_participation_pct: float = 0.0
    # The shared policy's second-based time stop is tuned for minute data, so
    # backtests opt in explicitly; ``max_hold_candles`` is the timeframe-neutral
    # alternative.
    time_stop_enabled: bool = False
    max_hold_candles: int = 0

    def __post_init__(self) -> None:
        if self.initial_sol <= 0:
            raise ValueError("initial_sol must be positive")
        if self.trade_size_sol <= 0:
            raise ValueError("trade_size_sol must be positive")
        if self.trade_size_sol > self.initial_sol:
            raise ValueError("trade_size_sol cannot exceed initial_sol")
        if self.fee_bps < 0 or self.slippage_bps < 0 or self.exit_slippage_bps < 0:
            raise ValueError("fee_bps, slippage_bps and exit_slippage_bps must be non-negative")
        if self.impact_coeff < 0 or self.liquidity_usd < 0:
            raise ValueError("impact_coeff and liquidity_usd must be non-negative")
        if self.impact_exponent <= 0:
            raise ValueError("impact_exponent must be positive")
        if self.priority_fee_sol < 0:
            raise ValueError("priority_fee_sol must be non-negative")
        if not 0.0 <= self.failed_tx_prob <= 1.0:
            raise ValueError("failed_tx_prob must be between 0 and 1")
        if self.sol_price_usd <= 0:
            raise ValueError("sol_price_usd must be positive")
        if not 0.0 < self.tp1_fraction <= 1.0:
            raise ValueError("tp1_fraction must be in (0, 1]")
        if self.final_take_profit_pct < 0:
            raise ValueError("final_take_profit_pct must be non-negative")
        if not 0.0 <= self.max_participation_pct <= 1.0:
            raise ValueError("max_participation_pct must be in [0, 1]")
        self.latency_candles = max(1, int(self.latency_candles))
        # Eagerly build both models so an invalid cost combination fails here.
        self.entry_model()
        self.exit_model()
        self.exit_policy()

    def exit_policy(self) -> ExitPolicy:
        """The shared policy with the flat CLI overrides applied."""
        return replace(
            self.policy,
            stop_loss_pct=self.stop_loss_pct,
            tp1_pct=self.take_profit_pct,
            tp1_fraction=self.tp1_fraction,
            final_take_profit_pct=self.final_take_profit_pct,
            trailing_stop_pct=self.trailing_stop_pct,
            time_stop_enabled=self.time_stop_enabled,
        )

    def entry_model(self) -> CostModel:
        return CostModel(
            base_bps=self.slippage_bps,
            amm_fee_bps=self.fee_bps,
            impact_coeff=self.impact_coeff,
            impact_exponent=self.impact_exponent,
            priority_fee_sol=self.priority_fee_sol,
            failed_tx_prob=self.failed_tx_prob,
            sol_price_usd=self.sol_price_usd,
        )

    def exit_model(self) -> CostModel:
        return CostModel(
            base_bps=self.exit_slippage_bps,
            amm_fee_bps=self.fee_bps,
            impact_coeff=self.impact_coeff,
            impact_exponent=self.impact_exponent,
            priority_fee_sol=self.priority_fee_sol,
            failed_tx_prob=self.failed_tx_prob,
            sol_price_usd=self.sol_price_usd,
        )

    def liquidity(self) -> Optional[float]:
        return float(self.liquidity_usd) if self.liquidity_usd > 0 else None

    def fingerprint(self, strategy: Optional[Strategy] = None) -> str:
        """Stable id of this strategy, its exits and its cost assumptions."""
        values = {key: value for key, value in vars(self).items() if key != "policy"}
        payload: Dict[str, Any] = {
            "costModelVersion": COST_MODEL_VERSION,
            "entryCost": self.entry_model().as_dict(),
            "exitCost": self.exit_model().as_dict(),
            "exitPolicy": self.exit_policy().as_dict(),
        }
        if strategy is not None:
            payload["strategy"] = _strategy_fingerprint(strategy)
        return config_hash(values, payload)


@dataclass
class Position:
    entry_index: int
    entry_ts: float
    entry_price: float
    qty: float
    invested_sol: float
    entry_fee_sol: float
    entry_cost_sol: float
    peak_price: float
    initial_qty: float = 0.0
    realized_sol: float = 0.0
    exit_fee_sol: float = 0.0
    exit_cost_sol: float = 0.0
    tp1_hit: bool = False
    tp1_price: float = 0.0
    tp1_proceeds_sol: float = 0.0
    depth_used: float = 0.0
    legs: int = 0

    def __post_init__(self) -> None:
        if self.initial_qty <= 0:
            self.initial_qty = self.qty


@dataclass
class BacktestResult:
    strategy: str
    candles: int
    trades: List[Dict[str, Any]]
    equity_curve: List[Dict[str, Any]]
    summary: Dict[str, Any]
    config: BacktestConfig

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "candles": self.candles,
            "config": self.config.__dict__,
            "configHash": self.summary.get("configHash"),
            "costModelVersion": COST_MODEL_VERSION,
            "summary": self.summary,
            "equityCurve": self.equity_curve,
            "trades": self.trades,
        }


def _trade_return_pcts(trades: Sequence[Mapping[str, Any]]) -> List[float]:
    returns = []
    for trade in trades:
        invested = float(trade.get("investedSol") or 0.0)
        if invested > 0:
            returns.append(float(trade.get("pnlSol") or 0.0) / invested)
    return returns


def _max_drawdown_from_curve(equity_values: Sequence[float]) -> float:
    if not equity_values:
        return 0.0
    peak = equity_values[0]
    worst = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return worst


def _fill_capacity(cfg: BacktestConfig, candle: Candle) -> Optional[float]:
    """Largest SOL notional this candle can absorb, or ``None`` when uncapped.

    ``Candle.volume`` is quote (USD) notional, matching the GeckoTerminal
    ``volume_usd`` field and the sample CSV. A zero volume means the feed does
    not report volume, so the cap is skipped rather than blocking every trade.
    """
    if cfg.max_participation_pct <= 0:
        return None
    volume = float(candle.volume or 0.0)
    if volume <= 0:
        return None
    return (volume * cfg.max_participation_pct) / max(cfg.sol_price_usd, 1e-9)


def run_backtest(
    candles: Sequence[Candle],
    strategy: Strategy,
    config: Optional[BacktestConfig] = None,
    *,
    close_at_end: bool = True,
) -> BacktestResult:
    cfg = config or BacktestConfig()
    data = normalize_candles(candles)
    if not data:
        raise ValueError("no candles supplied")

    entry_model = cfg.entry_model()
    exit_model = cfg.exit_model()
    policy = cfg.exit_policy()
    liquidity = cfg.liquidity()
    # Fingerprint before the strategy runs, so mutable test state such as a
    # "already signalled" flag cannot change the id.
    config_id = cfg.fingerprint(strategy)
    cash = float(cfg.initial_sol)
    position: Optional[Position] = None
    pending: Optional[Dict[str, Any]] = None
    trades: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    exposure_candles = 0
    requested_entries = 0
    partial_entries = 0

    def close_leg(
        index: int,
        timestamp: float,
        raw_price: float,
        reason: str,
        fraction: float = 1.0,
        ambiguous: bool = False,
    ) -> float:
        """Sell ``fraction`` of the remaining position and return the proceeds.

        Partial legs (TP1) accumulate into the position; the trade row is only
        written once the position is flat, so a round trip stays one row with
        its TP1 details attached.
        """
        nonlocal cash, position
        assert position is not None
        sell_qty = position.qty if fraction >= 1.0 else position.qty * fraction
        mid_value = sell_qty * raw_price
        exit_fill = exit_model.exit_value(mid_value, raw_price, liquidity, position.depth_used)
        cash += exit_fill.net_sol
        position.qty = max(0.0, position.qty - sell_qty)
        position.realized_sol += exit_fill.net_sol
        position.exit_fee_sol += exit_fill.explicit_fee_sol
        position.exit_cost_sol += exit_fill.total_cost_sol
        position.legs += 1

        if fraction < 1.0:
            return exit_fill.net_sol

        invested = position.invested_sol
        returned = position.realized_sol
        pnl = returned - invested
        trades.append(
            {
                "entryIndex": position.entry_index,
                "exitIndex": index,
                "entryTs": position.entry_ts,
                "exitTs": timestamp,
                "entryPrice": position.entry_price,
                "exitPrice": exit_fill.price,
                "exitMidPrice": raw_price,
                "qty": position.initial_qty,
                "investedSol": invested,
                "returnedSol": returned,
                "pnlSol": pnl,
                "returnPct": (pnl / invested * 100.0) if invested else 0.0,
                "feeSol": position.entry_fee_sol + position.exit_fee_sol,
                "costSol": position.entry_cost_sol + position.exit_cost_sol,
                "holdCandles": index - position.entry_index,
                "exitReason": reason,
                "ambiguousBar": bool(ambiguous),
                "tp1Hit": position.tp1_hit,
                "tp1Price": position.tp1_price,
                "tp1ProceedsSol": position.tp1_proceeds_sol,
                "legs": position.legs,
                "depthUsedFraction": position.depth_used,
                "requestedSol": cfg.trade_size_sol,
                "filledFraction": (invested / cfg.trade_size_sol) if cfg.trade_size_sol else 1.0,
            }
        )
        position = None
        return exit_fill.net_sol

    for index, candle in enumerate(data):
        if pending is not None and pending["index"] <= index:
            action = pending["action"]
            pending = None
            if action == "BUY" and position is None and cash > 1e-12:
                requested = float(cfg.trade_size_sol)
                budget = min(requested, cash)
                capacity = _fill_capacity(cfg, candle)
                if capacity is not None:
                    budget = min(budget, capacity)
                requested_entries += 1
                if budget < requested * (1.0 - 1e-9):
                    partial_entries += 1
                if budget > 1e-9:
                    entry_fill = entry_model.entry(candle.open, budget, liquidity, 0.0)
                    if entry_fill.qty > 0:
                        cash -= budget
                        position = Position(
                            entry_index=index,
                            entry_ts=candle.ts,
                            entry_price=entry_fill.price,
                            qty=entry_fill.qty,
                            invested_sol=budget,
                            entry_fee_sol=entry_fill.explicit_fee_sol,
                            entry_cost_sol=entry_fill.total_cost_sol,
                            peak_price=entry_fill.price,
                            depth_used=depth_used_fraction(budget, liquidity, cfg.sol_price_usd),
                        )
            elif action == "SELL" and position is not None:
                close_leg(index, candle.ts, candle.open, SIGNAL)

        if position is not None:
            exposure_candles += 1
            # The entry candle only feeds the peak; its own high/low never exits
            # the position that was just opened at its open.
            is_entry_bar = position.entry_index == index
            if not is_entry_bar:
                bar = policy.intrabar(
                    entry_price=position.entry_price,
                    peak_price=position.peak_price,
                    tp1_hit=position.tp1_hit,
                    open_price=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    age_sec=candle.ts - position.entry_ts,
                )
                if bar is not None:
                    if bar.fraction < 1.0:
                        proceeds = close_leg(
                            index, candle.ts, bar.price, bar.reason, bar.fraction, bar.ambiguous
                        )
                        position.tp1_hit = True
                        position.tp1_price = bar.price
                        position.tp1_proceeds_sol += proceeds
                    else:
                        close_leg(index, candle.ts, bar.price, bar.reason, 1.0, bar.ambiguous)
                elif cfg.max_hold_candles > 0 and index - position.entry_index >= cfg.max_hold_candles:
                    close_leg(index, candle.ts, candle.close, TIME_STOP)
            if position is not None:
                position.peak_price = max(position.peak_price, candle.high)

        if pending is None:
            signal = strategy.generate(data[: index + 1])
            if position is None and signal == "BUY" and cash >= cfg.trade_size_sol:
                pending = {"index": index + cfg.latency_candles, "action": "BUY"}
            elif position is not None and signal == "SELL":
                pending = {"index": index + cfg.latency_candles, "action": "SELL"}

        marked_equity = cash + (position.qty * candle.close if position is not None else 0.0)
        equity_curve.append(
            {
                "index": index,
                "ts": candle.ts,
                "equitySol": marked_equity,
                "close": candle.close,
                "inPosition": position is not None,
            }
        )

    if position is not None and close_at_end:
        last_index = len(data) - 1
        close_leg(last_index, data[last_index].ts, data[last_index].close, END_OF_DATA)
        if equity_curve:
            equity_curve[-1]["equitySol"] = cash
            equity_curve[-1]["inPosition"] = False

    pnls = [float(trade["pnlSol"]) for trade in trades]
    base = summarize(pnls, cfg.initial_sol)
    equity_values = [float(point["equitySol"]) for point in equity_curve]
    final_equity = equity_values[-1] if equity_values else cfg.initial_sol
    returns = _trade_return_pcts(trades)
    volatility = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    sharpe = (statistics.mean(returns) / volatility) if volatility > 0 else 0.0
    fees = sum(float(trade["feeSol"]) for trade in trades)
    costs = sum(float(trade["costSol"]) for trade in trades)
    ambiguous_bars = sum(1 for trade in trades if trade.get("ambiguousBar"))
    tp1_hits = sum(1 for trade in trades if trade.get("tp1Hit"))
    holds = [int(trade["holdCandles"]) for trade in trades]
    exit_reasons = _count_by(trades, "exitReason")
    summary = {
        **base,
        "initialSol": cfg.initial_sol,
        "finalEquitySol": final_equity,
        "returnPct": ((final_equity / cfg.initial_sol) - 1.0) * 100.0,
        "maxDrawdownPct": _max_drawdown_from_curve(equity_values),
        "exposurePct": (exposure_candles / len(data) * 100.0) if data else 0.0,
        "feesPaidSol": fees,
        "costsPaidSol": costs,
        "ambiguousBars": ambiguous_bars,
        "tp1Exits": tp1_hits,
        "partialEntries": partial_entries,
        "requestedEntries": requested_entries,
        "configHash": config_id,
        "costModelVersion": COST_MODEL_VERSION,
        "sharpePerTrade": sharpe,
        "avgHoldCandles": (sum(holds) / len(holds)) if holds else 0.0,
        "bestTradeSol": max(pnls) if pnls else 0.0,
        "worstTradeSol": min(pnls) if pnls else 0.0,
        "exitReasons": exit_reasons,
    }
    return BacktestResult(
        strategy=getattr(strategy, "name", "unknown"),
        candles=len(data),
        trades=trades,
        equity_curve=equity_curve,
        summary=summary,
        config=cfg,
    )


def _count_by(rows: Sequence[Mapping[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        label = str(row.get(key) or "unknown")
        counts[label] = counts.get(label, 0) + 1
    return counts


def walk_forward(
    candles: Sequence[Candle],
    strategy_factory: Callable[[], Strategy],
    config: Optional[BacktestConfig] = None,
    segments: int = 3,
    train_ratio: float = 0.6,
) -> Dict[str, Any]:
    """Evaluate sequential out-of-sample slices.

    Each segment is split into a train part (not traded) and a test part (traded)
    so the test trades never see candles from their own future. Every test
    segment starts with the same bankroll, which keeps each segment comparable.
    """
    data = normalize_candles(candles)
    segments = max(1, int(segments))
    train_ratio = min(max(float(train_ratio), 0.1), 0.9)
    block_size = max(1, len(data) // segments)
    cfg = config or BacktestConfig()
    config_id = cfg.fingerprint(strategy_factory())
    results = []
    all_pnls: List[float] = []
    for segment in range(segments):
        start = segment * block_size
        end = len(data) if segment == segments - 1 else (segment + 1) * block_size
        block = data[start:end]
        split = max(1, int(len(block) * train_ratio))
        train = block[:split]
        test = block[split:]
        if len(test) < 10:
            continue
        result = run_backtest(test, strategy_factory(), cfg)
        segment_pnls = [float(trade["pnlSol"]) for trade in result.trades]
        all_pnls.extend(segment_pnls)
        results.append(
            {
                "segment": segment + 1,
                "trainCandles": len(train),
                "testCandles": len(test),
                "startTs": test[0].ts,
                "endTs": test[-1].ts,
                "summary": result.summary,
            }
        )

    base = summarize(all_pnls, cfg.initial_sol)
    return {
        "segments": results,
        "aggregate": {
            **base,
            "segments": len(results),
            "testCandles": sum(item["testCandles"] for item in results),
            "configHash": config_id,
            "costModelVersion": COST_MODEL_VERSION,
        },
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isinf(number):
            return "inf"
        return f"{number:.{digits}f}".rstrip("0").rstrip(".") if "." in f"{number:.{digits}f}" else f"{number:.{digits}f}"
    return str(value)


def render_markdown(result: BacktestResult, trades_to_show: int = 15) -> str:
    summary = result.summary
    lines = [
        "# AgenticTrade OHLCV backtest",
        "",
        f"- Strategy: `{result.strategy}`",
        f"- Candles: `{result.candles}`",
        f"- Cost model: `v{COST_MODEL_VERSION}` | AMM fee: `{result.config.fee_bps:g} bps` | "
        f"Entry slippage: `{result.config.slippage_bps:g} bps` | Exit slippage: `{result.config.exit_slippage_bps:g} bps`",
        f"- Priority fee: `{result.config.priority_fee_sol:g} SOL` | Failed-tx prob: `{result.config.failed_tx_prob:g}` | "
        f"Liquidity: `${result.config.liquidity_usd:,.0f}` | Latency: `{result.config.latency_candles}` candle(s)",
        f"- TP1: `{result.config.take_profit_pct:g}%` sells `{result.config.tp1_fraction * 100:g}%` | "
        f"Final target: `{result.config.final_take_profit_pct:g}%` | Participation cap: "
        f"`{result.config.max_participation_pct * 100:g}%`",
        f"- Config hash: `{summary.get('configHash', '-')}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Trades | {summary['trades']} |",
        f"| Win rate | {_fmt(summary['winRate'] * 100, 2)}% |",
        f"| Net PnL | {_fmt(summary['netPnlSol'], 6)} SOL |",
        f"| Return | {_fmt(summary['returnPct'], 2)}% |",
        f"| Final equity | {_fmt(summary['finalEquitySol'], 6)} SOL |",
        f"| Max drawdown | {_fmt(summary['maxDrawdownPct'], 2)}% |",
        f"| Profit factor | {_fmt(summary['profitFactor'], 3)} |",
        f"| Expectancy | {_fmt(summary['expectancySol'], 6)} SOL |",
        f"| Fees paid | {_fmt(summary['feesPaidSol'], 6)} SOL |",
        f"| Total costs | {_fmt(summary.get('costsPaidSol', 0.0), 6)} SOL |",
        f"| Ambiguous bars | {summary.get('ambiguousBars', 0)} |",
        f"| TP1 partial exits | {summary.get('tp1Exits', 0)} |",
        f"| Depth-limited entries | {summary.get('partialEntries', 0)} / {summary.get('requestedEntries', 0)} |",
        f"| Exposure | {_fmt(summary['exposurePct'], 2)}% |",
        f"| Sharpe (per trade) | {_fmt(summary['sharpePerTrade'], 3)} |",
        f"| Avg hold | {_fmt(summary['avgHoldCandles'], 1)} candles |",
        "",
    ]
    if summary.get("exitReasons"):
        lines.append("## Exit reasons")
        lines.append("")
        for reason, count in sorted(summary["exitReasons"].items()):
            lines.append(f"- `{reason}`: {count}")
        lines.append("")

    if result.trades:
        lines.extend(
            [
                "## Trades (most recent first)",
                "",
                "| # | Entry | Exit | PnL SOL | Return | Reason | Hold |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for trade in result.trades[-max(1, trades_to_show) :][::-1]:
            lines.append(
                "| {idx} | {entry} | {exit} | {pnl} | {ret}% | {reason} | {hold} |".format(
                    idx=trade["exitIndex"],
                    entry=trade["entryTs"],
                    exit=trade["exitTs"],
                    pnl=_fmt(trade["pnlSol"], 6),
                    ret=_fmt(trade["returnPct"], 2),
                    reason=trade["exitReason"],
                    hold=trade["holdCandles"],
                )
            )
        lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentictrade.ohlcv",
        description="Event-driven OHLCV backtest for AgenticTrade strategies.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--csv", help="CSV file with ts,open,high,low,close,volume columns")
    source.add_argument("--json", help="JSON file with candle objects or arrays")
    source.add_argument("--pool", help="GeckoTerminal pool address (network fetch)")
    parser.add_argument("--network", default="solana", help="GeckoTerminal network (default: solana)")
    parser.add_argument("--timeframe", default="minute", choices=sorted(_TF_TO_SECONDS))
    parser.add_argument("--aggregate", type=int, default=1)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--strategy", default="momentum", choices=("momentum", "breakout"))
    parser.add_argument("--fast", type=int, default=9)
    parser.add_argument("--slow", type=int, default=21)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--threshold-pct", type=float, default=0.5)
    parser.add_argument("--initial-sol", type=float, default=10.0)
    parser.add_argument("--trade-sol", type=float, default=STRATEGY_DEFAULTS["TRADE_SIZE_SOL"])
    parser.add_argument(
        "--fee-bps", type=float, default=COST_DEFAULTS["amm_fee_bps"], help="AMM/DEX fee on notional (bps)"
    )
    parser.add_argument("--slippage-bps", type=float, default=COST_DEFAULTS["base_bps"], help="entry spread/impact (bps)")
    parser.add_argument(
        "--exit-slippage-bps", type=float, default=COST_DEFAULTS["exit_base_bps"], help="exit spread/impact (bps)"
    )
    parser.add_argument("--impact-coeff", type=float, default=COST_DEFAULTS["impact_coeff"])
    parser.add_argument(
        "--impact-exponent",
        type=float,
        default=COST_DEFAULTS["impact_exponent"],
        help="1.0 is linear impact, 0.5 the square-root AMM curve",
    )
    parser.add_argument("--liquidity-usd", type=float, default=0.0, help="pool liquidity for market impact (0 disables it)")
    parser.add_argument("--priority-fee-sol", type=float, default=COST_DEFAULTS["priority_fee_sol"])
    parser.add_argument("--failed-tx-prob", type=float, default=COST_DEFAULTS["failed_tx_prob"])
    parser.add_argument("--sol-price-usd", type=float, default=COST_DEFAULTS["sol_price_usd"])
    parser.add_argument("--latency", type=int, default=1)
    parser.add_argument("--stop-loss", type=float, default=STRATEGY_DEFAULTS["STOP_LOSS_PCT"])
    parser.add_argument(
        "--take-profit", type=float, default=STRATEGY_DEFAULTS["TP1_PCT"], help="TP1 level (%%); partial by default"
    )
    parser.add_argument(
        "--tp1-fraction",
        type=float,
        default=EXIT_DEFAULTS["tp1_fraction"],
        help="fraction sold at TP1 (1.0 exits the whole position)",
    )
    parser.add_argument("--final-take-profit", type=float, default=0.0, help="hard target for the moonbag (0 disables)")
    parser.add_argument("--trailing-stop", type=float, default=STRATEGY_DEFAULTS["TRAILING_STOP_PCT"])
    parser.add_argument("--max-hold", type=int, default=0)
    parser.add_argument(
        "--max-participation",
        type=float,
        default=0.0,
        help="cap a fill at this share of candle volume (0 disables)",
    )
    parser.add_argument(
        "--time-stop",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="enable the paper book's second-based time stop (tuned for minute data)",
    )
    parser.add_argument("--walk-forward", type=int, default=0, help="number of out-of-sample segments")
    parser.add_argument("--train-ratio", type=float, default=0.6)
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="walk-forward parameter optimization (grid-search each train slice)",
    )
    parser.add_argument(
        "--objective",
        default="pnl",
        choices=("pnl", "return_pct", "profit_factor", "expectancy", "sharpe"),
        help="what the optimizer maximizes on the train slice",
    )
    parser.add_argument(
        "--min-trades", type=int, default=3, help="minimum train trades for a candidate to be eligible"
    )
    parser.add_argument("--fast-grid", default="", help="e.g. 5,9,13 (empty uses the default grid)")
    parser.add_argument("--slow-grid", default="", help="e.g. 13,21,34 (empty uses the default grid)")
    parser.add_argument("--lookback-grid", default="", help="e.g. 10,20,30,55 (empty uses the default grid)")
    parser.add_argument("--threshold-grid", default="", help="e.g. 0.0,0.5,1.0 (empty uses the default grid)")
    parser.add_argument("--output", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--save", help="also write the JSON result to this path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.csv:
        candles = load_candles_csv(args.csv)
    elif args.json:
        candles = load_candles_json(args.json)
    elif args.pool:
        candles = asyncio.run(
            fetch_geckoterminal_candles(
                args.pool,
                network=args.network,
                timeframe=args.timeframe,
                aggregate=args.aggregate,
                limit=args.limit,
            )
        )
    else:
        print("Provide --csv, --json or --pool.", file=sys.stderr)
        return 2

    config = BacktestConfig(
        initial_sol=args.initial_sol,
        trade_size_sol=args.trade_sol,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        exit_slippage_bps=args.exit_slippage_bps,
        impact_coeff=args.impact_coeff,
        impact_exponent=args.impact_exponent,
        liquidity_usd=args.liquidity_usd,
        priority_fee_sol=args.priority_fee_sol,
        failed_tx_prob=args.failed_tx_prob,
        sol_price_usd=args.sol_price_usd,
        latency_candles=args.latency,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        trailing_stop_pct=args.trailing_stop,
        tp1_fraction=args.tp1_fraction,
        final_take_profit_pct=args.final_take_profit,
        max_participation_pct=args.max_participation,
        time_stop_enabled=args.time_stop,
        max_hold_candles=args.max_hold,
    )

    def factory() -> Strategy:
        if args.strategy == "breakout":
            return BreakoutStrategy(lookback=args.lookback, threshold_pct=args.threshold_pct)
        return EmaCrossStrategy(fast=args.fast, slow=args.slow)

    if args.optimize:
        # Imported lazily: the optimizer imports this module.
        from agentictrade.optimizer import (
            default_grid,
            optimize_walk_forward,
            parse_grid,
            render_report,
        )

        defaults = default_grid(args.strategy)
        grid: Dict[str, Any] = {
            "fast": parse_grid(args.fast_grid, int) or defaults.get("fast", []),
            "slow": parse_grid(args.slow_grid, int) or defaults.get("slow", []),
            "lookback": parse_grid(args.lookback_grid, int) or defaults.get("lookback", []),
            "threshold_pct": parse_grid(args.threshold_grid, float)
            or defaults.get("threshold_pct", []),
        }
        if args.strategy == "breakout":
            grid.pop("fast", None)
            grid.pop("slow", None)
        else:
            grid.pop("lookback", None)
            grid.pop("threshold_pct", None)
        if args.strategy == "breakout":
            baseline_params: Dict[str, Any] = {
                "lookback": args.lookback,
                "threshold_pct": args.threshold_pct,
            }
        else:
            baseline_params = {"fast": args.fast, "slow": args.slow}

        optimization = optimize_walk_forward(
            candles,
            args.strategy,
            grid,
            config,
            segments=args.walk_forward or 3,
            train_ratio=args.train_ratio,
            objective=args.objective,
            min_trades=args.min_trades,
            baseline_params=baseline_params,
        )
        payload = {"optimization": optimization}
        if args.output == "json":
            text = json.dumps(payload, indent=2, default=str)
        else:
            text = render_report(optimization)
        print(text)
        if args.save:
            Path(args.save).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return 0

    if args.walk_forward > 0:
        report: Dict[str, Any] = walk_forward(
            candles,
            factory,
            config,
            segments=args.walk_forward,
            train_ratio=args.train_ratio,
        )
        payload = {"walkForward": report}
        print(json.dumps(payload, indent=2, default=str))
        if args.save:
            Path(args.save).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return 0

    result = run_backtest(candles, factory(), config)
    if args.output == "json":
        text = json.dumps(result.to_dict(), indent=2, default=str)
    else:
        text = render_markdown(result)
    print(text)
    if args.save:
        Path(args.save).write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
