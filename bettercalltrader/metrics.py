import sqlite3
from typing import Dict


def collect_basic_metrics(db_path: str) -> Dict[str, float]:
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        total = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0] or 0
        wins = conn.execute("SELECT COUNT(*) FROM trade_history WHERE net_pnl_sol > 0").fetchone()[0] or 0
        pnl = conn.execute("SELECT COALESCE(SUM(net_pnl_sol), 0.0) FROM trade_history").fetchone()[0] or 0.0
        open_positions = conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'").fetchone()[0] or 0
        return {
            "totalTrades": float(total),
            "winningTrades": float(wins),
            "winRate": (wins / total) if total else 0.0,
            "netPnlSol": float(pnl),
            "openPositions": float(open_positions),
        }
    finally:
        conn.close()