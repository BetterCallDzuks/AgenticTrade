"""One exit policy for the paper book and the OHLCV backtester.

Before this module the stop/take-profit/trailing order existed twice: once as a
tick-by-tick ``if/elif`` chain in ``PaperTradingManager`` and once as a
candle-by-candle chain in ``agentictrade.ohlcv``. The two drifted (the
backtester exited 100% at take profit while the paper book sold half), so a
research run measured a policy the bot never traded.

Everything that decides *when* to sell now lives here:

- :class:`ExitPolicy` holds the thresholds and the effective-stop ladder
  (stop loss, trailing, breakeven, step-up floors).
- :meth:`ExitPolicy.classify_tick` is the paper-book decision for one observed
  price.
- :meth:`ExitPolicy.intrabar` is the backtester decision for one OHLC candle,
  including gap fills and the stop-vs-target ambiguity flag.

Neither method mutates anything: callers own the position and the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from agentictrade.defaults import EXIT_DEFAULTS, STRATEGY_DEFAULTS

# Exit reasons. Keep these strings stable: they are written to
# ``trade_history.exit_reason`` and counted in analytics reports.
STOP_LOSS = "STOP_LOSS"
FAST_INVALIDATION = "FAST_INVALIDATION"
TRAILING_STOP = "TRAILING_STOP"
BREAKEVEN_STOP = "BREAKEVEN_STOP"
STEP_UP_STOP = "STEP_UP_STOP"
TAKE_PROFIT_1 = "TAKE_PROFIT_1"
TAKE_PROFIT = "TAKE_PROFIT"
TIME_STOP = "TIME_STOP"
FLOW_EXIT = "FLOW_EXIT"
WHALE_EXIT = "WHALE_EXIT"
SIGNAL = "SIGNAL"
END_OF_DATA = "END_OF_DATA"

FULL = 1.0


@dataclass(frozen=True)
class ExitIntent:
    """What the policy wants to do with a position right now."""

    reason: Optional[str] = None
    fraction: float = FULL

    @property
    def is_exit(self) -> bool:
        return self.reason is not None

    @property
    def is_partial(self) -> bool:
        return self.reason is not None and self.fraction < 1.0


@dataclass(frozen=True)
class BarExit:
    """An exit resolved against one candle, with its fill price."""

    reason: str
    price: float
    fraction: float = FULL
    ambiguous: bool = False


@dataclass(frozen=True)
class ExitPolicy:
    """Thresholds shared by the paper book and the backtester.

    The defaults mirror the historical ``balanced`` profile. Only the fields a
    candle can observe are used by :meth:`intrabar`; the event-driven rules
    (flow, whale) are paper-only and live in :meth:`classify_tick`.
    """

    stop_loss_pct: float = STRATEGY_DEFAULTS["STOP_LOSS_PCT"]
    tp1_pct: float = STRATEGY_DEFAULTS["TP1_PCT"]
    tp1_fraction: float = EXIT_DEFAULTS["tp1_fraction"]
    trailing_stop_pct: float = STRATEGY_DEFAULTS["TRAILING_STOP_PCT"]
    trailing_stop_pct_after_tp1: float = STRATEGY_DEFAULTS["TRAILING_STOP_PCT_AFTER_TP1"]
    trailing_arm_gain_pct: float = EXIT_DEFAULTS["trailing_arm_gain_pct"]
    move_stop_to_breakeven_after_tp1: bool = True
    breakeven_buffer_pct: float = EXIT_DEFAULTS["breakeven_buffer_pct"]
    step1_trigger_pct: float = EXIT_DEFAULTS["step1_trigger_pct"]
    step1_floor_pct: float = EXIT_DEFAULTS["step1_floor_pct"]
    step2_trigger_pct: float = EXIT_DEFAULTS["step2_trigger_pct"]
    step2_floor_pct: float = EXIT_DEFAULTS["step2_floor_pct"]
    fast_invalidation_pct: float = STRATEGY_DEFAULTS["FAST_INVALIDATION_PCT"]
    fast_invalidation_window_sec: float = EXIT_DEFAULTS["fast_invalidation_window_sec"]
    time_stop_enabled: bool = True
    max_hold_no_pump_sec: float = EXIT_DEFAULTS["max_hold_no_pump_sec"]
    time_stop_min_profit_pct: float = EXIT_DEFAULTS["time_stop_min_profit_pct"]
    flow_exit_enabled: bool = True
    flow_exit_min_sells: int = int(EXIT_DEFAULTS["flow_exit_min_sells"])
    flow_exit_sell_ratio: float = EXIT_DEFAULTS["flow_exit_sell_ratio"]
    whale_exit_enabled: bool = True
    final_take_profit_pct: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 < float(self.tp1_fraction) <= 1.0:
            raise ValueError("tp1_fraction must be in (0, 1]")
        for name in ("trailing_stop_pct", "trailing_stop_pct_after_tp1", "trailing_arm_gain_pct"):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.fast_invalidation_window_sec < 0:
            raise ValueError("fast_invalidation_window_sec must be non-negative")

    # -- effective stop ladder ------------------------------------------

    def effective_stop(
        self, entry_price: float, highest_price: float, tp1_hit: bool
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Return ``(effective_stop, trailing_stop, floor_stop)``.

        The trailing stop only arms once the position is up
        ``trailing_arm_gain_pct``; the floor stop is the breakeven/step-up
        ladder. The effective stop is the tighter (higher) of the two.
        """
        entry_price = float(entry_price or 0.0)
        highest_price = float(highest_price or 0.0)
        if entry_price <= 0:
            return None, None, None

        trail_pct = self.trailing_stop_pct_after_tp1 if tp1_hit else self.trailing_stop_pct
        armed = highest_price >= entry_price * (1.0 + self.trailing_arm_gain_pct / 100.0)
        trail_stop = highest_price * (1.0 - trail_pct / 100.0) if (armed and trail_pct > 0) else None

        floors = []
        if tp1_hit and self.move_stop_to_breakeven_after_tp1:
            floors.append(entry_price * (1.0 + self.breakeven_buffer_pct / 100.0))
        peak_gain_pct = ((highest_price / entry_price) - 1.0) * 100.0
        if peak_gain_pct >= self.step2_trigger_pct:
            floors.append(entry_price * (1.0 + self.step2_floor_pct / 100.0))
        elif peak_gain_pct >= self.step1_trigger_pct:
            floors.append(entry_price * (1.0 + self.step1_floor_pct / 100.0))

        floor_stop = max(floors) if floors else None
        candidates = [value for value in (trail_stop, floor_stop) if value is not None]
        return (max(candidates) if candidates else None), trail_stop, floor_stop

    def stop_reason(
        self, entry_price: float, trail_stop: Optional[float], floor_stop: Optional[float]
    ) -> str:
        """Label an effective-stop hit the same way the paper book always has."""
        if trail_stop is not None and (floor_stop is None or trail_stop >= floor_stop):
            return TRAILING_STOP
        if floor_stop is not None and floor_stop >= float(entry_price):
            return BREAKEVEN_STOP
        if floor_stop is not None:
            return STEP_UP_STOP
        return STOP_LOSS

    # -- paper book: one observed price ---------------------------------

    def classify_tick(
        self,
        *,
        entry_price: float,
        current_price: float,
        highest_price: float,
        tp1_hit: bool,
        age_sec: float,
        fresh: bool = True,
        flow: Optional[Dict[str, Any]] = None,
        whale_selling: Optional[Dict[str, Any]] = None,
    ) -> ExitIntent:
        """Decide what to do at the current tick.

        Order matters and matches the live paper book: a whale exit wins over
        everything, then TP1 de-risks, then the protective stops, then the
        time stop and flow exit, and finally the effective stop ladder.
        """
        entry_price = float(entry_price or 0.0)
        current_price = float(current_price or 0.0)
        if entry_price <= 0 or current_price <= 0:
            return ExitIntent()

        pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
        peak_pnl_pct = ((max(float(highest_price or entry_price), current_price) - entry_price) / entry_price) * 100.0

        if whale_selling and self.whale_exit_enabled:
            return ExitIntent(WHALE_EXIT)

        if fresh and not tp1_hit and self.tp1_pct > 0 and pnl_pct >= self.tp1_pct:
            return ExitIntent(TAKE_PROFIT_1, self.tp1_fraction)

        if age_sec <= self.fast_invalidation_window_sec and pnl_pct <= self.fast_invalidation_pct:
            return ExitIntent(FAST_INVALIDATION)

        if pnl_pct <= self.stop_loss_pct:
            return ExitIntent(STOP_LOSS)

        if (
            self.time_stop_enabled
            and not tp1_hit
            and age_sec >= self.max_hold_no_pump_sec
            and peak_pnl_pct < self.time_stop_min_profit_pct
        ):
            return ExitIntent(TIME_STOP)

        if (
            fresh
            and self.flow_exit_enabled
            and flow is not None
            and flow.get("sells", 0) >= self.flow_exit_min_sells
            and flow.get("sells", 0) > flow.get("buys", 0)
            and flow.get("sells", 0) >= flow.get("buys", 0) * self.flow_exit_sell_ratio
        ):
            return ExitIntent(FLOW_EXIT)

        stop_px, trail_px, floor_px = self.effective_stop(entry_price, highest_price, tp1_hit)
        if stop_px is not None and current_price <= stop_px * (1.0 + 1e-9):
            return ExitIntent(self.stop_reason(entry_price, trail_px, floor_px))

        if tp1_hit and self.final_take_profit_pct > 0 and pnl_pct >= self.final_take_profit_pct:
            return ExitIntent(TAKE_PROFIT, FULL)

        return ExitIntent()

    # -- backtester: one candle ------------------------------------------

    def intrabar(
        self,
        *,
        entry_price: float,
        peak_price: float,
        tp1_hit: bool,
        open_price: float,
        high: float,
        low: float,
        close: float,
        age_sec: float,
    ) -> Optional[BarExit]:
        """Resolve one candle, or return ``None`` when the position survives.

        ``peak_price`` is the high *before* this candle, so a candle that makes
        a new high cannot arm and trigger its own trailing stop.

        Gaps are respected: a stop that gaps fills at ``min(open, stop)`` and a
        target that gaps fills at ``max(open, target)``. When a candle contains
        both a stop and a target we cannot know the intrabar order, so the
        conservative stop is taken and the result is flagged ``ambiguous``.
        """
        entry_price = float(entry_price or 0.0)
        if entry_price <= 0:
            return None

        stop_px, trail_px, floor_px = self.effective_stop(entry_price, peak_price, tp1_hit)
        stop_reason = self.stop_reason(entry_price, trail_px, floor_px)

        # The fast invalidation is a tighter, earlier stop inside its window.
        fast_px = (
            entry_price * (1.0 + self.fast_invalidation_pct / 100.0)
            if age_sec <= self.fast_invalidation_window_sec and self.fast_invalidation_pct < 0
            else None
        )
        # The first adverse level a falling price meets is the highest one. The
        # hard stop loss is always armed; the trailing/floor ladder only arms
        # once the position has moved in our favour.
        adverse: Optional[Tuple[float, str]] = None
        if self.stop_loss_pct < 0:
            adverse = (entry_price * (1.0 + self.stop_loss_pct / 100.0), STOP_LOSS)
        if stop_px is not None and (adverse is None or stop_px > adverse[0]):
            adverse = (stop_px, stop_reason)
        if fast_px is not None and (adverse is None or fast_px > adverse[0]):
            adverse = (fast_px, FAST_INVALIDATION)

        tp_px = (
            entry_price * (1.0 + self.tp1_pct / 100.0)
            if self.tp1_pct > 0 and not tp1_hit
            else None
        )
        final_px = (
            entry_price * (1.0 + self.final_take_profit_pct / 100.0)
            if tp1_hit and self.final_take_profit_pct > 0
            else None
        )

        # 1. Gap through an armed adverse level: it filled at the open.
        if adverse is not None and open_price <= adverse[0]:
            return BarExit(adverse[1], min(open_price, adverse[0]))

        # 2. Gap through TP1: the target filled at the open, on the good side.
        if tp_px is not None and open_price >= tp_px:
            return BarExit(TAKE_PROFIT_1, max(open_price, tp_px), self.tp1_fraction)

        # 3. Both sides inside one candle: unknown order, take the stop.
        adverse_hit = adverse is not None and low <= adverse[0]
        tp_hit = tp_px is not None and high >= tp_px
        if adverse_hit and tp_hit:
            assert adverse is not None
            return BarExit(adverse[1], min(open_price, adverse[0]), FULL, ambiguous=True)
        if adverse_hit:
            assert adverse is not None
            return BarExit(adverse[1], min(open_price, adverse[0]))
        if tp_hit and tp_px is not None:
            return BarExit(TAKE_PROFIT_1, max(open_price, tp_px), self.tp1_fraction)

        # 4. Final target for the moonbag.
        if final_px is not None and high >= final_px:
            return BarExit(TAKE_PROFIT, max(open_price, final_px))

        # 5. Time stop is close-based: there was no pump and the clock ran out.
        if (
            self.time_stop_enabled
            and not tp1_hit
            and age_sec >= self.max_hold_no_pump_sec
            and ((max(float(peak_price or entry_price), close) - entry_price) / entry_price) * 100.0
            < self.time_stop_min_profit_pct
        ):
            return BarExit(TIME_STOP, close)

        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stopLossPct": self.stop_loss_pct,
            "tp1Pct": self.tp1_pct,
            "tp1Fraction": self.tp1_fraction,
            "finalTakeProfitPct": self.final_take_profit_pct,
            "trailingStopPct": self.trailing_stop_pct,
            "trailingStopPctAfterTp1": self.trailing_stop_pct_after_tp1,
            "trailingArmGainPct": self.trailing_arm_gain_pct,
            "breakevenBufferPct": self.breakeven_buffer_pct,
            "fastInvalidationPct": self.fast_invalidation_pct,
            "fastInvalidationWindowSec": self.fast_invalidation_window_sec,
            "timeStopEnabled": self.time_stop_enabled,
            "maxHoldNoPumpSec": self.max_hold_no_pump_sec,
            "timeStopMinProfitPct": self.time_stop_min_profit_pct,
        }
