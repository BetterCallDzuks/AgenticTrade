"""Replay analytics over the recorded paper-trading book.

This is intentionally a *realized* replay: it evaluates the trades that the bot
actually recorded (positions, exit reasons, slippage, PnL). It does not invent
historical prices, so it cannot pretend to be a full OHLCV backtest. The report
is still useful for finding bad exit rules, weak sources and drawdown clusters.
"""

import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional

from agentictrade.risk_analytics import summarize


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _group(rows: List[sqlite3.Row], key: str) -> Dict[str, dict]:
    buckets: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        label = row[key] if row[key] not in (None, "") else "unknown"
        buckets[str(label)].append(float(row["net_pnl_sol"] or 0.0))
    return {label: summarize(values) for label, values in sorted(buckets.items())}


def build_report(db_path: str, initial_sol: float = 10.0) -> dict:
    report = {
        "db": db_path,
        "initialSol": float(initial_sol),
        "summary": summarize([], float(initial_sol)),
        "byExitReason": {},
        "bySource": {},
        "byConviction": {},
        "equityCurve": [],
        "dailyPnl": {},
        "bestTrade": None,
        "worstTrade": None,
        "trades": [],
    }
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "trade_history"):
            return report
        rows = conn.execute(
            """
            SELECT mint, symbol, opened_at, closed_at, invested_sol, returned_sol,
                   net_pnl_sol, exit_reason, tp1_hit, slippage_sol, conviction, source
            FROM trade_history
            ORDER BY COALESCE(closed_at, opened_at) ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return report

    pnls = [float(r["net_pnl_sol"] or 0.0) for r in rows]
    report["summary"] = summarize(pnls, float(initial_sol))
    report["byExitReason"] = _group(rows, "exit_reason")
    report["bySource"] = _group(rows, "source")
    report["byConviction"] = _group(rows, "conviction")

    equity = float(initial_sol)
    daily: Dict[str, float] = defaultdict(float)
    for index, row in enumerate(rows, start=1):
        pnl = float(row["net_pnl_sol"] or 0.0)
        equity += pnl
        trade = {
            "index": index,
            "mint": row["mint"],
            "symbol": row["symbol"],
            "openedAt": row["opened_at"],
            "closedAt": row["closed_at"],
            "exitReason": row["exit_reason"],
            "source": row["source"],
            "conviction": row["conviction"],
            "pnlSol": pnl,
            "equitySol": equity,
        }
        report["equityCurve"].append({"index": index, "equitySol": equity, "pnlSol": pnl, "closedAt": row["closed_at"]})
        report["trades"].append(trade)
        if row["closed_at"]:
            daily[str(row["closed_at"])[:10]] += pnl

    report["dailyPnl"] = {day: round(value, 9) for day, value in sorted(daily.items())}
    best = max(report["trades"], key=lambda t: t["pnlSol"])
    worst = min(report["trades"], key=lambda t: t["pnlSol"])
    report["bestTrade"] = best
    report["worstTrade"] = worst
    return report


def _fmt(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def to_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# AgenticTrade v10 backtest report",
        "",
        f"Database: `{report['db']}`",
        f"Initial equity: {report['initialSol']:.4f} SOL",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Trades | {s['trades']} |",
        f"| Net PnL | {_fmt(s['netPnlSol'])} SOL |",
        f"| Win rate | {s['winRate'] * 100:.1f}% |",
        f"| Profit factor | {s['profitFactor'] if s['profitFactor'] is not None else 'n/a'} |",
        f"| Expectancy | {_fmt(s['expectancySol'])} SOL |",
        f"| Max drawdown | {s['maxDrawdownPct']:.2f}% |",
        f"| VaR 95% | {_fmt(s['var95Sol'])} SOL |",
        f"| Expected shortfall 95% | {_fmt(s['expectedShortfall95Sol'])} SOL |",
        f"| Average win | {_fmt(s['avgWinSol'])} SOL |",
        f"| Average loss | {_fmt(s['avgLossSol'])} SOL |",
        "",
    ]

    def group_table(title: str, groups: dict):
        if not groups:
            return
        lines.extend([f"## {title}", "", "| Group | Trades | Net PnL | Win rate |", "|---|---:|---:|---:|"])
        for label, stats in groups.items():
            lines.append(
                f"| {label} | {stats['trades']} | {_fmt(stats['netPnlSol'])} | {stats['winRate'] * 100:.1f}% |"
            )
        lines.append("")

    group_table("By exit reason", report["byExitReason"])
    group_table("By source", report["bySource"])
    group_table("By conviction", report["byConviction"])

    best = report["bestTrade"]
    worst = report["worstTrade"]
    if best and worst:
        lines.extend(
            [
                "## Extremes",
                "",
                f"- Best trade: `${best['symbol']}` {_fmt(best['pnlSol'])} SOL ({best['exitReason']})",
                f"- Worst trade: `${worst['symbol']}` {_fmt(worst['pnlSol'])} SOL ({worst['exitReason']})",
                "",
            ]
        )
    lines.append("_Realized replay only; no synthetic historical prices._")
    return "\n".join(lines)
