#!/usr/bin/env python3
"""Export supervised fine-tuning data from the bot's own decision history.

GGUF models cannot be trained in-process, so "fine-tune on our data" is not a
button in this repo. What is useful is a clean, de-duplicated JSONL of the
*same* prompts the bot sends live, paired with the verdict an LLM (not the
heuristic) produced and, when the token was traded, what it earned. Feed that to
an external trainer (Unsloth, Axolotl, llama-factory, OpenAI fine-tuning) or use
a few rows as few-shot examples.

Every example is reproducible: the system message is ``llm.system_prompt``, the
user message is ``prompts.build_research_prompt`` and the assistant message is
the recorded ``TokenAnalysisDecision``.

Usage::

    python py/export_training.py                       # AI verdicts only
    python py/export_training.py --include-heuristic    # also heuristic verdicts
    python py/export_training.py --positive-only --format alpaca
    python py/export_training.py --out training_data/sft.jsonl --val-out training_data/val.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
for _path in (ROOT, PROJECT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bettercalltrader.llm import system_prompt  # noqa: E402
from bettercalltrader.models import TokenAnalysisDecision  # noqa: E402
from bettercalltrader.prompts import build_research_prompt  # noqa: E402

AI_RULES = ("AI_VERDICT", "LOCAL_AI_VERDICT")
DECISION_FIELDS = ("decision", "risk_score", "hype_score", "summary_hr", "red_flags", "green_flags")


def load_examples(db_path: str) -> List[Dict[str, Any]]:
    """Join recorded scans, the verdict journal and realized outcomes."""
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT s.mint, s.symbol, s.source, s.market_json, s.security_json,
                   s.verdict_json, s.scanned_at, s.rowid AS scan_id,
                   j.rule AS rule, j.risk_score AS j_risk, j.hype_score AS j_hype,
                   (SELECT SUM(t.net_pnl_sol) FROM trade_history t WHERE t.mint = s.mint) AS net_pnl,
                   (SELECT COUNT(*) FROM trade_history t WHERE t.mint = s.mint) AS trades
            FROM recent_scans s
            LEFT JOIN decision_journal j ON j.id = (
                SELECT MAX(id) FROM decision_journal
                WHERE mint = s.mint AND rule LIKE '%VERDICT'
            )
            ORDER BY s.rowid DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def to_example(row: Dict[str, Any], include_system: bool = True) -> Optional[Dict[str, Any]]:
    """Turn one database row into an SFT example, or None when it is unusable."""
    try:
        market = json.loads(row["market_json"] or "")
        security = json.loads(row["security_json"] or "")
        raw_verdict = json.loads(row["verdict_json"] or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_verdict, dict):
        return None
    verdict = {key: raw_verdict.get(key) for key in DECISION_FIELDS}
    if verdict["decision"] not in ("BUY", "WATCH", "SKIP"):
        return None
    if not isinstance(verdict["red_flags"], list) or not isinstance(verdict["green_flags"], list):
        return None
    try:
        # Validate the exact contract the live parser enforces, then serialise it
        # canonically so the assistant message is always the same shape.
        answer = TokenAnalysisDecision.model_validate(verdict).model_dump_json()
    except Exception:  # noqa: BLE001 - pydantic validation is the filter
        return None

    rule = str(row.get("rule") or "")
    source_kind = "ai" if any(rule.endswith(name) for name in AI_RULES) else (
        "hard_filter" if rule == "HARD_FILTER" else "heuristic"
    )
    net = row.get("net_pnl") if row.get("trades") else None
    metadata = {
        "mint": row["mint"],
        "symbol": row["symbol"],
        "source": row["source"],
        "scannedAt": row["scanned_at"],
        "decision": verdict["decision"],
        "labelSource": source_kind,
        "recordedRule": rule or None,
        "trades": int(row.get("trades") or 0),
        "netPnlSol": None if net is None else round(float(net), 9),
    }
    messages = []
    if include_system:
        messages.append({"role": "system", "content": system_prompt(TokenAnalysisDecision)})
    messages.append(
        {
            "role": "user",
            "content": build_research_prompt(market, security),
        }
    )
    messages.append({"role": "assistant", "content": answer})
    return {"messages": messages, "metadata": metadata}


def to_alpaca(example: Dict[str, Any]) -> Dict[str, Any]:
    system = next((m["content"] for m in example["messages"] if m["role"] == "system"), "")
    user = next((m["content"] for m in example["messages"] if m["role"] == "user"), "")
    assistant = next((m["content"] for m in example["messages"] if m["role"] == "assistant"), "")
    return {"instruction": system, "input": user, "output": assistant, "metadata": example["metadata"]}


def build_dataset(
    rows: List[Dict[str, Any]],
    *,
    include_heuristic: bool = False,
    include_hard_filter: bool = False,
    decisions: Optional[set[str]] = None,
    only_outcomes: bool = False,
    positive_only: bool = False,
    dedupe: bool = True,
    include_system: bool = True,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    stats: Dict[str, Any] = {"scanned": len(rows), "skipped": 0, "byDecision": {}, "byLabelSource": {}, "withOutcome": 0}
    seen: set[str] = set()
    examples: List[Dict[str, Any]] = []
    for row in rows:
        example = to_example(row, include_system=include_system)
        if example is None:
            stats["skipped"] += 1
            continue
        meta = example["metadata"]
        label = meta["labelSource"]
        if label == "heuristic" and not include_heuristic:
            stats["skipped"] += 1
            continue
        if label == "hard_filter" and not include_hard_filter:
            stats["skipped"] += 1
            continue
        if decisions and meta["decision"] not in decisions:
            stats["skipped"] += 1
            continue
        if dedupe and meta["mint"] in seen:
            stats["skipped"] += 1
            continue
        if only_outcomes and meta["netPnlSol"] is None:
            stats["skipped"] += 1
            continue
        if positive_only and (meta["netPnlSol"] is None or meta["netPnlSol"] <= 0):
            stats["skipped"] += 1
            continue
        seen.add(meta["mint"])
        stats["byDecision"][meta["decision"]] = stats["byDecision"].get(meta["decision"], 0) + 1
        stats["byLabelSource"][label] = stats["byLabelSource"].get(label, 0) + 1
        if meta["netPnlSol"] is not None:
            stats["withOutcome"] += 1
        examples.append(example)
    stats["kept"] = len(examples)
    return examples, stats


def write_jsonl(path: Path, examples: List[Dict[str, Any]], fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for example in examples:
            payload = to_alpaca(example) if fmt == "alpaca" else example
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export SFT data from recorded scans and verdicts.")
    parser.add_argument("--db", default=os.environ.get("PAPER_DB_PATH") or "paper_portfolio.db")
    parser.add_argument("--out", default="training_data/sft.jsonl")
    parser.add_argument("--val-out", default="", help="optional validation split path (last 10%% of rows)")
    parser.add_argument("--format", choices=("chat", "alpaca"), default="chat")
    parser.add_argument("--include-heuristic", action="store_true", help="also export heuristic verdicts")
    parser.add_argument("--include-hard-filter", action="store_true", help="also export automatic hard-filter SKIPs")
    parser.add_argument(
        "--decision",
        default="",
        help="comma-separated decisions to keep, e.g. BUY,WATCH (default: all)",
    )
    parser.add_argument("--only-outcomes", action="store_true", help="keep tokens that were actually traded")
    parser.add_argument("--positive-only", action="store_true", help="keep only profitable trades")
    parser.add_argument("--no-dedupe", action="store_true", help="keep every scan instead of the latest per mint")
    parser.add_argument("--no-system", action="store_true", help="omit the system message (chat format)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if not Path(args.db).is_file():
        print(f"{args.db} not found")
        return 1
    decisions = {item.strip().upper() for item in args.decision.split(",") if item.strip()} or None
    rows = load_examples(args.db)
    examples, stats = build_dataset(
        rows,
        include_heuristic=args.include_heuristic,
        include_hard_filter=args.include_hard_filter,
        decisions=decisions,
        only_outcomes=args.only_outcomes,
        positive_only=args.positive_only,
        dedupe=not args.no_dedupe,
        include_system=not args.no_system,
    )
    if not examples:
        print(
            "No exportable examples. AI verdicts are required unless you pass "
            "--include-heuristic/--include-hard-filter; run the bot with an LLM first."
        )
        print(json.dumps(stats, indent=2))
        return 2

    train, val = examples, []
    if args.val_out and len(examples) >= 10:
        cut = max(1, int(len(examples) * 0.9))
        train, val = examples[:cut], examples[cut:]
    write_jsonl(Path(args.out), train, args.format)
    print(f"Wrote {len(train)} example(s) to {args.out} ({args.format})")
    if val:
        write_jsonl(Path(args.val_out), val, args.format)
        print(f"Wrote {len(val)} validation example(s) to {args.val_out}")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
