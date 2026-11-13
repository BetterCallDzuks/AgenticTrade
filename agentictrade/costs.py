"""One transaction-cost model for every simulated fill.

The paper book, the OHLCV backtester and the ML labels must agree on what a
trade costs. This module is the single source of truth: a :class:`CostModel`
turns a mid price, a trade size and (optionally) pool liquidity into a
:class:`Fill` with an explicit breakdown.

Cost components, per side:

- ``base_bps`` — spread / expected price impact charged by the venue.
- ``impact_coeff`` — linear market impact: ``size_sol / liquidity_sol`` times
  this coefficient. Zero when liquidity is unknown.
- ``amm_fee_bps`` — DEX/AMM fee on the traded notional (charged on both sides).
- ``priority_fee_sol`` — flat SOL paid per transaction (priority fee / Jito tip).
- ``failed_tx_prob`` — probability that an attempt reverts and the fee is paid
  again. Modelled as a flat cost of ``priority_fee_sol * (1 + p)`` per side.

Flat fees (priority + failed) are taken out of the deployed amount on a buy and
out of the proceeds on a sell, so ``invested_sol`` and ``net_sol`` already
include every cost. That keeps ``net_pnl = returned - invested`` honest without
needing a separate fee ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

COST_MODEL_VERSION = "1"


@dataclass(frozen=True)
class CostModel:
    base_bps: float = 50.0
    amm_fee_bps: float = 25.0
    impact_coeff: float = 1.0
    priority_fee_sol: float = 0.0015
    failed_tx_prob: float = 0.02
    sol_price_usd: float = 185.0
    max_slippage: float = 0.5

    def __post_init__(self) -> None:
        for name in ("base_bps", "amm_fee_bps", "impact_coeff", "priority_fee_sol", "sol_price_usd"):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be non-negative")
        if not 0.0 <= float(self.failed_tx_prob) <= 1.0:
            raise ValueError("failed_tx_prob must be between 0 and 1")
        if not 0.0 < float(self.max_slippage) < 1.0:
            raise ValueError("max_slippage must be between 0 and 1")

    @property
    def amm_fee_rate(self) -> float:
        return self.amm_fee_bps / 10000.0

    def impact_fraction(self, size_sol: float, liquidity_usd: Optional[float]) -> float:
        """Return the liquidity-dependent part of slippage (0 when unknown)."""
        if not liquidity_usd or liquidity_usd <= 0:
            return 0.0
        liquidity_sol = float(liquidity_usd) / max(float(self.sol_price_usd), 1e-9)
        return (max(0.0, float(size_sol)) / max(liquidity_sol, 1.0)) * max(0.0, self.impact_coeff)

    def slippage_fraction(self, size_sol: float, liquidity_usd: Optional[float]) -> float:
        raw = self.base_bps / 10000.0 + self.impact_fraction(size_sol, liquidity_usd)
        return min(float(self.max_slippage), max(0.0, raw))

    def _flat_cost_sol(self) -> float:
        # Expected priority fee including the retries that revert.
        return max(0.0, self.priority_fee_sol) * (1.0 + max(0.0, self.failed_tx_prob))

    def entry(self, mid_price: float, size_sol: float, liquidity_usd: Optional[float] = None) -> Fill:
        """Return the fill for spending ``size_sol`` (all-in) at ``mid_price``."""
        mid = float(mid_price)
        budget = float(size_sol)
        if mid <= 0 or budget <= 0:
            return Fill.empty(mid)
        slip = self.slippage_fraction(budget, liquidity_usd)
        flat = self._flat_cost_sol()
        deployable = budget - flat
        if deployable <= 0:
            return Fill.empty(mid)
        all_in_price = mid * (1.0 + slip) * (1.0 + self.amm_fee_rate)
        qty = deployable / all_in_price
        notional = qty * mid
        amm_fee = deployable - deployable / (1.0 + self.amm_fee_rate)
        total_cost = budget - notional
        return Fill(
            price=(budget / qty) if qty > 0 else 0.0,
            qty=qty,
            notional_sol=notional,
            net_sol=budget,
            slippage_fraction=slip,
            amm_fee_sol=amm_fee,
            priority_fee_sol=self.priority_fee_sol,
            failed_tx_sol=self.priority_fee_sol * self.failed_tx_prob,
            total_cost_sol=max(0.0, total_cost),
        )

    def exit_value(self, mid_value_sol: float, mid_price: float, liquidity_usd: Optional[float] = None) -> Fill:
        """Return the fill for selling a slice worth ``mid_value_sol`` at mid."""
        mid = float(mid_price)
        value = float(mid_value_sol)
        if mid <= 0 or value <= 0:
            return Fill.empty(mid)
        qty = value / mid
        slip = self.slippage_fraction(value, liquidity_usd)
        gross = qty * mid * (1.0 - slip)
        amm_fee = gross * self.amm_fee_rate
        flat = self._flat_cost_sol()
        net = max(0.0, gross - amm_fee - flat)
        return Fill(
            price=(net / qty) if qty > 0 else 0.0,
            qty=qty,
            notional_sol=value,
            net_sol=net,
            slippage_fraction=slip,
            amm_fee_sol=amm_fee,
            priority_fee_sol=self.priority_fee_sol,
            failed_tx_sol=self.priority_fee_sol * self.failed_tx_prob,
            total_cost_sol=max(0.0, value - net),
        )

    def as_dict(self) -> dict:
        return {
            "version": COST_MODEL_VERSION,
            "baseBps": self.base_bps,
            "ammFeeBps": self.amm_fee_bps,
            "impactCoeff": self.impact_coeff,
            "priorityFeeSol": self.priority_fee_sol,
            "failedTxProb": self.failed_tx_prob,
            "solPriceUsd": self.sol_price_usd,
        }


@dataclass(frozen=True)
class Fill:
    """A simulated fill with its full cost breakdown."""

    price: float
    qty: float
    notional_sol: float
    net_sol: float
    slippage_fraction: float
    amm_fee_sol: float
    priority_fee_sol: float
    failed_tx_sol: float
    total_cost_sol: float

    @classmethod
    def empty(cls, mid_price: float = 0.0) -> "Fill":
        return cls(
            price=0.0,
            qty=0.0,
            notional_sol=0.0,
            net_sol=0.0,
            slippage_fraction=0.0,
            amm_fee_sol=0.0,
            priority_fee_sol=0.0,
            failed_tx_sol=0.0,
            total_cost_sol=0.0,
        )

    @property
    def total_cost_fraction(self) -> float:
        """Cost as a fraction of mid notional (the value ``paper_fill`` returned)."""
        if self.notional_sol <= 0:
            return 0.0
        return self.total_cost_sol / self.notional_sol

    @property
    def explicit_fee_sol(self) -> float:
        """Fee components only (excludes spread/impact)."""
        return self.amm_fee_sol + self.priority_fee_sol + self.failed_tx_sol


def replay_round_trip(
    entry_model: CostModel,
    exit_model: CostModel,
    entry_mid: float,
    exit_mid: float,
    size_sol: float,
    liquidity_usd: Optional[float] = None,
) -> dict:
    """Reference round trip used to cross-check the paper book and backtester."""
    entry = entry_model.entry(entry_mid, size_sol, liquidity_usd)
    exit_fill = exit_model.exit_value(entry.qty * exit_mid, exit_mid, liquidity_usd)
    return {
        "qty": entry.qty,
        "entryPrice": entry.price,
        "exitPrice": exit_fill.price,
        "investedSol": size_sol,
        "returnedSol": exit_fill.net_sol,
        "pnlSol": exit_fill.net_sol - size_sol,
        "entryCostSol": entry.total_cost_sol,
        "exitCostSol": exit_fill.total_cost_sol,
    }
