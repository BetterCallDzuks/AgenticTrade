"""Event-driven OHLCV backtester.

This is deliberately separate from `agentictrade.backtest`, which replays the
trades the bot actually recorded. Here you supply historical candles (CSV,
JSON, or GeckoTerminal) and a strategy, and the engine simulates entries and
exits with configurable fees, slippage and signal latency.

The engine is long-only, uses one position at a time and fills signals at the
open of a later candle, so it cannot peek at the close that generated the
signal. Exit checks use candle high/low (stop first, then trailing, then take
profit), which is conservative on wide candles.

Examples:
    python -m agentictrade.ohlcv --csv candles.csv --strategy momentum
    python -m agentictrade.ohlcv --json candles.json --walk-forward 3
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

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


@dataclass
class BacktestConfig:
    initial_sol: float = 10.0
    trade_size_sol: float = 0.5
    fee_bps: float = 25.0
    slippage_bps: float = 50.0
    latency_candles: int = 1
    stop_loss_pct: float = -25.0
    take_profit_pct: float = 80.0
    trailing_stop_pct: float = 20.0
    max_hold_candles: int = 0

    def __post_init__(self) -> None:
        if self.initial_sol <= 0:
            raise ValueError("initial_sol must be positive")
        if self.trade_size_sol <= 0:
            raise ValueError("trade_size_sol must be positive")
        if self.trade_size_sol > self.initial_sol:
            raise ValueError("trade_size_sol cannot exceed initial_sol")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("fee_bps and slippage_bps must be non-negative")
        self.latency_candles = max(1, int(self.latency_candles))


@dataclass
class Position:
    entry_index: int
    entry_ts: float
    entry_price: float
    qty: float
    invested_sol: float
    entry_fee_sol: float
    peak_price: float


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

    fee_rate = cfg.fee_bps / 10000.0
    slippage = cfg.slippage_bps / 10000.0
    cash = float(cfg.initial_sol)
    position: Optional[Position] = None
    pending: Optional[Dict[str, Any]] = None
    trades: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    exposure_candles = 0

    def close_position(index: int, timestamp: float, raw_price: float, reason: str) -> None:
        nonlocal cash, position
        assert position is not None
        fill_price = raw_price * (1.0 - slippage)
        gross_sol = position.qty * fill_price
        exit_fee = gross_sol * fee_rate
        returned_sol = gross_sol - exit_fee
        cash += returned_sol
        pnl = returned_sol - position.invested_sol
        invested = position.invested_sol
        trades.append(
            {
                "entryIndex": position.entry_index,
                "exitIndex": index,
                "entryTs": position.entry_ts,
                "exitTs": timestamp,
                "entryPrice": position.entry_price,
                "exitPrice": fill_price,
                "qty": position.qty,
                "investedSol": invested,
                "returnedSol": returned_sol,
                "pnlSol": pnl,
                "returnPct": (pnl / invested * 100.0) if invested else 0.0,
                "feeSol": position.entry_fee_sol + exit_fee,
                "holdCandles": index - position.entry_index,
                "exitReason": reason,
            }
        )
        position = None

    for index, candle in enumerate(data):
        if pending is not None and pending["index"] <= index:
            action = pending["action"]
            expected_index = pending["index"]
            pending = None
            # Fill at the first available open at or after the requested candle.
            if action == "BUY" and position is None and expected_index <= index and cash >= cfg.trade_size_sol:
                fill_price = candle.open * (1.0 + slippage)
                entry_fee = cfg.trade_size_sol * fee_rate
                qty = (cfg.trade_size_sol - entry_fee) / fill_price if fill_price > 0 else 0.0
                if qty > 0:
                    cash -= cfg.trade_size_sol
                    position = Position(
                        entry_index=index,
                        entry_ts=candle.ts,
                        entry_price=fill_price,
                        qty=qty,
                        invested_sol=cfg.trade_size_sol,
                        entry_fee_sol=entry_fee,
                        peak_price=fill_price,
                    )
            elif action == "SELL" and position is not None:
                close_position(index, candle.ts, candle.open, "SIGNAL")

        if position is not None:
            exposure_candles += 1
            position.peak_price = max(position.peak_price, candle.high)
            stop_price = (
                position.entry_price * (1.0 + cfg.stop_loss_pct / 100.0)
                if cfg.stop_loss_pct < 0
                else None
            )
            trailing_price = (
                position.peak_price * (1.0 - cfg.trailing_stop_pct / 100.0)
                if cfg.trailing_stop_pct > 0
                else None
            )
            take_profit_price = (
                position.entry_price * (1.0 + cfg.take_profit_pct / 100.0)
                if cfg.take_profit_pct > 0
                else None
            )
            if stop_price is not None and candle.low <= stop_price:
                close_position(index, candle.ts, stop_price, "STOP_LOSS")
            elif trailing_price is not None and candle.low <= trailing_price:
                close_position(index, candle.ts, trailing_price, "TRAILING_STOP")
            elif take_profit_price is not None and candle.high >= take_profit_price:
                close_position(index, candle.ts, take_profit_price, "TAKE_PROFIT")
            elif cfg.max_hold_candles > 0 and index - position.entry_index >= cfg.max_hold_candles:
                close_position(index, candle.ts, candle.close, "TIME_STOP")

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
        close_position(last_index, data[last_index].ts, data[last_index].close, "END_OF_DATA")
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
    holds = [int(trade["holdCandles"]) for trade in trades]
    summary = {
        **base,
        "initialSol": cfg.initial_sol,
        "finalEquitySol": final_equity,
        "returnPct": ((final_equity / cfg.initial_sol) - 1.0) * 100.0,
        "maxDrawdownPct": _max_drawdown_from_curve(equity_values),
        "exposurePct": (exposure_candles / len(data) * 100.0) if data else 0.0,
        "feesPaidSol": fees,
        "sharpePerTrade": sharpe,
        "avgHoldCandles": (sum(holds) / len(holds)) if holds else 0.0,
        "bestTradeSol": max(pnls) if pnls else 0.0,
        "worstTradeSol": min(pnls) if pnls else 0.0,
        "exitReasons": _count_by(trades, "exitReason"),
    }
    return BacktestResult(
        strategy=strategy.name,
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
        result = run_backtest(test, strategy_factory(), config)
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

    base = summarize(all_pnls, config.initial_sol if config else 10.0)
    return {
        "segments": results,
        "aggregate": {
            **base,
            "segments": len(results),
            "testCandles": sum(item["testCandles"] for item in results),
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
        f"- Fees: `{result.config.fee_bps:g} bps` | Slippage: `{result.config.slippage_bps:g} bps` | Latency: `{result.config.latency_candles}` candle(s)",
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
    parser.add_argument("--trade-sol", type=float, default=0.5)
    parser.add_argument("--fee-bps", type=float, default=25.0)
    parser.add_argument("--slippage-bps", type=float, default=50.0)
    parser.add_argument("--latency", type=int, default=1)
    parser.add_argument("--stop-loss", type=float, default=-25.0)
    parser.add_argument("--take-profit", type=float, default=80.0)
    parser.add_argument("--trailing-stop", type=float, default=20.0)
    parser.add_argument("--max-hold", type=int, default=0)
    parser.add_argument("--walk-forward", type=int, default=0, help="number of out-of-sample segments")
    parser.add_argument("--train-ratio", type=float, default=0.6)
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
        latency_candles=args.latency,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        trailing_stop_pct=args.trailing_stop,
        max_hold_candles=args.max_hold,
    )

    def factory() -> Strategy:
        if args.strategy == "breakout":
            return BreakoutStrategy(lookback=args.lookback, threshold_pct=args.threshold_pct)
        return EmaCrossStrategy(fast=args.fast, slow=args.slow)

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
