import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from agentictrade.backtest import build_report, to_markdown


def snapshot_database(src: Path, target: Path) -> None:
    shutil.copy2(src, target)
    # Preserve uncheckpointed WAL data so replay sees the same trades as the bot.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(src) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(target) + suffix))
    conn = sqlite3.connect(str(target), timeout=15)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="AgenticTrade v10 realized-trade replay")
    parser.add_argument("--db", default="paper_portfolio.db", help="path to the paper trading database")
    parser.add_argument("--initial-sol", type=float, default=10.0, help="starting equity in SOL")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--out", help="write the markdown report to this path")
    args = parser.parse_args()

    src = Path(args.db)
    if not src.exists():
        print(f"{args.db} not found")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "replay_snapshot.db"
        snapshot_database(src, copy)
        report = build_report(str(copy), initial_sol=args.initial_sol)

    report["db"] = str(src)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        markdown = to_markdown(report)
        print(markdown)

    if args.out:
        Path(args.out).write_text(to_markdown(report), encoding="utf-8")
        print(f"report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
