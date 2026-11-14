#!/usr/bin/env python3
"""Offline evaluation of the LLM models the bot can choose between.

Choosing a local model from the dashboard is only meaningful if there is
evidence behind the choice. This harness replays the *real* research prompt over
the payloads the bot already recorded (``recent_scans``), once per candidate
model, and reports:

- JSON validity (raw, repaired, schema-invalid, transport errors)
- latency (p50/p95), because a slow model starves the scan loop
- decision distribution and agreement with the decision the bot recorded
- outcome evidence for the tokens a model wanted to BUY (thin until trades accrue)

It never trades and never writes to the database. Local models are unloaded
between candidates so a 27B and a 14B never compete for VRAM.

Usage::

    python py/eval_models.py                        # every model in LLM_MODEL(S)
    python py/eval_models.py --models a,b --limit 8
    python py/eval_models.py --baseline gemini      # score Gemini on the same cases
    python py/eval_models.py --out docs/MODEL_EVAL.md --json eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
for _path in (ROOT, PROJECT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bettercalltrader.llm import JSONParseError, LLMClient, LLMConfig  # noqa: E402
from bettercalltrader.models import TokenAnalysisDecision  # noqa: E402
from bettercalltrader.prompts import build_research_prompt  # noqa: E402

DECISIONS = ("BUY", "WATCH", "SKIP")


def load_env(path: Path) -> None:
    """Load .env without overriding variables already set in the environment."""
    if not path.is_file():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - optional dependency
        return
    for key, value in dotenv_values(dotenv_path=path).items():
        if value is not None:
            os.environ.setdefault(key, str(value))


def load_cases(db_path: str, limit: int) -> List[Dict[str, Any]]:
    """Read the recorded market/security payloads the live prompt used."""
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT mint, symbol, source, market_json, security_json, verdict_json, scanned_at
            FROM recent_scans
            WHERE market_json IS NOT NULL AND security_json IS NOT NULL
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        outcomes: Dict[str, float] = {}
        try:
            for row in conn.execute(
                "SELECT mint, SUM(net_pnl_sol) AS net FROM trade_history GROUP BY mint"
            ):
                if row["net"] is not None:
                    outcomes[str(row["mint"])] = float(row["net"])
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    cases: List[Dict[str, Any]] = []
    for row in reversed(rows):  # oldest first: the prompt order matches the journal
        try:
            market = json.loads(row["market_json"])
            security = json.loads(row["security_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        recorded = ""
        try:
            recorded = str(json.loads(row["verdict_json"] or "{}").get("decision") or "").upper()
        except (TypeError, json.JSONDecodeError):
            recorded = ""
        cases.append(
            {
                "mint": row["mint"],
                "symbol": row["symbol"],
                "source": row["source"],
                "scannedAt": row["scanned_at"],
                "market": market,
                "security": security,
                "recorded": recorded if recorded in DECISIONS else "",
                "outcomeSol": outcomes.get(str(row["mint"])),
            }
        )
    return cases


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def _model_config(base: LLMConfig, model: str, kind: str) -> LLMConfig:
    if kind == "gemini":
        return replace(
            base,
            provider="gemini",
            model=base.gemini_model,
            local_model="",
            local_models=(),
            base_url="",
            temperature=0.0,
        )
    return replace(
        base,
        provider="openai",
        model=model,
        local_model=model,
        local_models=(model,),
        gemini_api_key="",
        temperature=0.0,
        timeout_sec=max(float(base.timeout_sec), 240.0),
    )


async def evaluate(model: str, cases: List[Dict[str, Any]], base: LLMConfig, kind: str = "local") -> Dict[str, Any]:
    config = _model_config(base, model, kind)
    client = LLMClient(config, json_retry=False)
    label = client.label
    latencies: List[float] = []
    decisions = {name: 0 for name in DECISIONS}
    result: Dict[str, Any] = {
        "model": label,
        "kind": kind,
        "cases": len(cases),
        "ok": 0,
        "jsonInvalid": 0,
        "schemaInvalid": 0,
        "transportErrors": 0,
        "repaired": 0,
        "agree": 0,
        "agreeable": 0,
        "buyOutcomes": [],
        "riskScores": [],
        "hypeScores": [],
        "latencyP50Ms": None,
        "latencyP95Ms": None,
        "decisions": decisions,
        "score": 0.0,
        "failures": [],
        "valid": False,
    }
    if not client.available:
        result["error"] = "provider not configured (missing base URL or API key)"
        return result

    for case in cases:
        prompt = build_research_prompt(case["market"], case["security"])
        try:
            decision = await client.complete_json(prompt, TokenAnalysisDecision)
        except JSONParseError as exc:
            result["jsonInvalid"] += 1
            if len(result["failures"]) < 3:
                result["failures"].append({"mint": case["mint"], "error": str(exc)[:200]})
            print(f"  ✗ {case['symbol']}: json {str(exc)[:80]}")
            continue
        except Exception as exc:  # noqa: BLE001 - classified, not raised
            message = str(exc)
            if "validation error" in message.lower() or type(exc).__name__ == "ValidationError":
                result["schemaInvalid"] += 1
            else:
                result["transportErrors"] += 1
            if len(result["failures"]) < 3:
                result["failures"].append({"mint": case["mint"], "error": message[:200]})
            print(f"  ✗ {case['symbol']}: {message[:80]}")
            continue

        latency = float(client.last_latency_ms or 0.0)
        latencies.append(latency)
        result["ok"] += 1
        if client.last_parse_mode == "repaired":
            result["repaired"] += 1
        choice = str(getattr(decision, "decision", "") or "").upper()
        if choice not in decisions:
            choice = "SKIP"
        decisions[choice] += 1
        result["riskScores"].append(int(getattr(decision, "risk_score", 0) or 0))
        result["hypeScores"].append(int(getattr(decision, "hype_score", 0) or 0))
        if case.get("recorded"):
            result["agreeable"] += 1
            if choice == case["recorded"]:
                result["agree"] += 1
        if choice == "BUY" and case.get("outcomeSol") is not None:
            result["buyOutcomes"].append(float(case["outcomeSol"]))
        print(f"  ✓ {case['symbol']}: {choice} ({latency:.0f} ms)")

    total = max(1, len(cases))
    # A schema-invalid reply still parsed as JSON: real, if unusable. Counting
    # the two separately is what makes the score readable.
    validity = (result["ok"] + result["schemaInvalid"]) / total
    schema = result["ok"] / total
    p50 = _percentile(latencies, 50)
    speed = 0.0 if p50 is None else max(0.0, 1.0 - min(1.0, p50 / 15000.0))
    result["latencyP50Ms"] = None if p50 is None else round(p50, 1)
    result["latencyP95Ms"] = None if not latencies else round(float(_percentile(latencies, 95)), 1)
    result["avgRiskScore"] = round(statistics.fmean(result["riskScores"]), 1) if result["riskScores"] else None
    result["avgHypeScore"] = round(statistics.fmean(result["hypeScores"]), 1) if result["hypeScores"] else None
    result["agreementPct"] = (
        round(100.0 * result["agree"] / result["agreeable"], 1) if result["agreeable"] else None
    )
    wins = sum(1 for value in result["buyOutcomes"] if value > 0)
    result["buyWinRatePct"] = (
        round(100.0 * wins / len(result["buyOutcomes"]), 1) if result["buyOutcomes"] else None
    )
    result["score"] = round(100.0 * (0.45 * validity + 0.45 * schema + 0.10 * speed), 1)
    result["valid"] = True
    return result


def render_markdown(results: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    lines = [
        "# Local model evaluation",
        "",
        f"- Generated: {meta['generated']}",
        f"- Cases per model: {meta['cases']} (from `{meta['db']}`, real recorded payloads)",
        "- Prompt: `bettercalltrader.prompts.build_research_prompt` (the live research prompt)",
        "- JSON retry: disabled, so the table measures raw reliability",
        "- Temperature: 0.0",
        "",
        "| Model | OK | JSON invalid | Schema invalid | Repaired | p50 ms | p95 ms | Agreement | BUY wins | Score |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in results:
        if not entry.get("valid"):
            lines.append(f"| `{entry['model']}` | — | — | — | — | — | — | — | — | not configured |")
            continue
        agreement = "—" if entry["agreementPct"] is None else f"{entry['agreementPct']:.1f}%"
        buy_wins = (
            "—"
            if entry["buyWinRatePct"] is None
            else f"{entry['buyWinRatePct']:.0f}% of {len(entry['buyOutcomes'])}"
        )
        lines.append(
            f"| `{entry['model']}` | {entry['ok']}/{entry['cases']} | {entry['jsonInvalid']} | "
            f"{entry['schemaInvalid']} | {entry['repaired']} | {_fmt(entry['latencyP50Ms'])} | "
            f"{_fmt(entry['latencyP95Ms'])} | {agreement} | {buy_wins} | **{entry['score']:.1f}** |"
        )
    lines += [
        "",
        "Score = 45% JSON validity + 45% schema validity + 10% speed "
        "(p50 latency, full marks under 1.5s, zero at 15s).",
        "",
        "Decision mix:",
        "",
        "| Model | BUY | WATCH | SKIP | avg risk | avg hype |",
        "|---|---|---|---|---|---|",
    ]
    for entry in results:
        if not entry.get("valid"):
            continue
        decisions = entry["decisions"]
        lines.append(
            f"| `{entry['model']}` | {decisions['BUY']} | {decisions['WATCH']} | {decisions['SKIP']} | "
            f"{_fmt(entry['avgRiskScore'])} | {_fmt(entry['avgHypeScore'])} |"
        )
    failures = [entry for entry in results if entry.get("failures")]
    if failures:
        lines += ["", "## Failure samples", ""]
        for entry in failures:
            lines.append(f"**`{entry['model']}`**")
            for failure in entry["failures"]:
                lines.append(f"- `{failure['mint'][:12]}…`: {failure['error']}")
            lines.append("")
    lines += [
        "",
        "_Agreement compares the model with the decision the bot recorded for that scan; "
        "it is not ground truth. BUY wins only covers tokens that were also traded._",
    ]
    return "\n".join(lines)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.1f}"


async def main_async(args: argparse.Namespace) -> int:
    load_env(Path(args.env_file))
    base = LLMConfig.from_env()
    cases = load_cases(args.db, args.limit)
    if not cases:
        print(f"No recorded scans in {args.db}; run the bot once (or use --db) so there is data to evaluate.")
        return 2

    models = [item.strip() for item in (args.models or "").split(",") if item.strip()]
    if not models:
        models = list(base.local_models) or ([base.local_model] if base.local_model else [])
    targets: List[tuple[str, str]] = [(model, "local") for model in models]
    if args.baseline == "gemini":
        targets.append((base.gemini_model, "gemini"))
    if not targets:
        print("No models to evaluate: set LLM_MODEL/LLM_MODELS, pass --models, or use --baseline gemini.")
        return 2

    print(f"Evaluating {len(targets)} model(s) on {len(cases)} recorded case(s) from {args.db}")
    results: List[Dict[str, Any]] = []
    for model, kind in targets:
        if kind == "local" and not args.keep_loaded:
            from bettercalltrader.lmstudio import unload

            unload(all_models=True)
        print(f"\n▶ {model} ({kind})")
        started = time.time()
        result = await evaluate(model, cases, base, kind)
        result["elapsedSec"] = round(time.time() - started, 1)
        results.append(result)
        if result.get("valid"):
            print(f"  score {result['score']:.1f} · p50 {_fmt(result['latencyP50Ms'])} ms")

    ranking = sorted(
        (entry for entry in results if entry.get("valid")),
        key=lambda entry: (-float(entry["score"]), float(entry["latencyP50Ms"] or 9e9)),
    )
    meta = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": os.path.basename(args.db),
        "cases": len(cases),
        "winner": ranking[0]["model"] if ranking else None,
    }
    payload = {"meta": meta, "results": results, "ranking": [entry["model"] for entry in ranking]}
    report = render_markdown(results, meta)
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {args.out}")
    print()
    print(report)
    return 0 if ranking else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate local/Gemini models on recorded research prompts.")
    parser.add_argument("--db", default=os.environ.get("PAPER_DB_PATH") or "paper_portfolio.db")
    parser.add_argument("--models", default="", help="comma-separated model IDs (default: LLM_MODEL + LLM_MODELS)")
    parser.add_argument("--limit", type=int, default=10, help="recorded cases to replay (default 10)")
    parser.add_argument("--baseline", choices=("none", "gemini"), default="none")
    parser.add_argument("--keep-loaded", action="store_true", help="do not unload local models between candidates")
    parser.add_argument("--json", default="", help="write the raw results as JSON")
    parser.add_argument("--out", default="", help="write a markdown report (e.g. docs/MODEL_EVAL.md)")
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
