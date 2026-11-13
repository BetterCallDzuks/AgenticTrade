"""Read-only live-execution readiness audit.

Answers one question before anyone flips ``LIVE_TRADING_ENABLED``: if the live
path were armed, would the guards stop it from spending money it should not?
The audit never places an order and never calls the network unless ``--probe``
is passed explicitly.

Three layers are checked:

1. :func:`check_environment` inspects the effective configuration (mode,
   confirmation flag, trade cap, key presence, RPC host, signing library).
2. :func:`audit_guards` drives the real ``SolanaExecutionAgent.execute_buy``
   against a session that refuses every request, so the fail-closed branches
   are exercised without a socket: paper mode returns ``DRY_RUN_READY``, live
   without confirmation is ``BLOCKED``, live above the cap is ``BLOCKED`` and
   live without a key is ``ERROR``.
3. :func:`run_readiness` combines both into ``PAPER`` / ``READY`` / ``BLOCKED``
   and optionally probes the RPC health endpoint and a Jupiter quote.

``READY`` means "the guards are armed and the pre-flight checks pass". It is not
a recommendation to trade: memecoins remain extremely risky and a live run can
still lose money.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

STATUS_PAPER = "PAPER"
STATUS_READY = "READY"
STATUS_BLOCKED = "BLOCKED"

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off", "")


def _parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _solders_available() -> bool:
    try:
        return importlib.util.find_spec("solders") is not None
    except (ImportError, ValueError):
        return False


def _is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def _issue(level: str, key: str, message: str) -> Dict[str, str]:
    return {"level": level, "key": key, "message": message}


def check_environment(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Summarise the live-trading configuration without touching the network.

    Never returns or logs the private key; only its presence is reported.
    """
    values = dict(os.environ if env is None else env)
    issues: List[Dict[str, str]] = []

    live_raw = values.get("LIVE_TRADING_ENABLED")
    confirmed_raw = values.get("LIVE_TRADING_CONFIRMED")
    live = _parse_bool(live_raw)
    confirmed = _parse_bool(confirmed_raw)
    if live is None and str(live_raw or "").strip():
        issues.append(
            _issue(
                "fail",
                "LIVE_TRADING_ENABLED",
                f"expected true/false, got {live_raw!r}; treated as off",
            )
        )
    if live is None:
        live = False
    if confirmed is None and str(confirmed_raw or "").strip():
        issues.append(
            _issue(
                "fail",
                "LIVE_TRADING_CONFIRMED",
                f"expected true/false, got {confirmed_raw!r}; treated as unconfirmed",
            )
        )
    if confirmed is None:
        confirmed = False

    max_live = _as_float(values.get("MAX_LIVE_TRADE_SOL"), 0.05)
    key_present = bool(str(values.get("SOLANA_PRIVATE_KEY") or "").strip())
    rpc_url = str(values.get("SOLANA_RPC_URL") or DEFAULT_RPC_URL).strip() or DEFAULT_RPC_URL
    slippage_bps = _as_float(values.get("SLIPPAGE_BPS"), 500.0)
    priority_fee_sol = _as_float(values.get("PRIORITY_FEE_SOL"), 0.0015)
    solders = _solders_available()

    if live:
        if not confirmed:
            issues.append(
                _issue(
                    "fail",
                    "LIVE_TRADING_CONFIRMED",
                    "live trading is enabled but not confirmed; orders are blocked",
                )
            )
        if not key_present:
            issues.append(
                _issue(
                    "fail",
                    "SOLANA_PRIVATE_KEY",
                    "live trading is enabled but no private key is configured",
                )
            )
        if max_live <= 0.0:
            issues.append(
                _issue(
                    "fail",
                    "MAX_LIVE_TRADE_SOL",
                    "must be above zero when live trading is enabled",
                )
            )
        if not solders:
            issues.append(
                _issue(
                    "fail",
                    "solders",
                    "the optional signing package is missing; live transactions cannot be signed",
                )
            )
        if _is_loopback(rpc_url):
            issues.append(
                _issue(
                    "info",
                    "SOLANA_RPC_URL",
                    f"RPC is loopback ({rpc_url}); use a devnet/local validator for rehearsals",
                )
            )
        else:
            issues.append(
                _issue(
                    "warn",
                    "SOLANA_RPC_URL",
                    f"live trading points at a non-loopback RPC ({rpc_url}); "
                    "confirm it is the cluster you intend to trade",
                )
            )
        if max_live > 0.25:
            issues.append(
                _issue(
                    "warn",
                    "MAX_LIVE_TRADE_SOL",
                    f"{max_live:g} SOL per trade is high for a first live run",
                )
            )
        issues.append(
            _issue(
                "warn",
                "LIVE_TRADING_ENABLED",
                "live trading is ON; paper trading is the safe default",
            )
        )
    else:
        if confirmed:
            issues.append(
                _issue(
                    "warn",
                    "LIVE_TRADING_CONFIRMED",
                    "confirmation is set while live trading is off; ignored until enabled",
                )
            )
        issues.append(
            _issue(
                "info",
                "LIVE_TRADING_ENABLED",
                "paper trading mode: no on-chain orders are possible",
            )
        )

    fails = sum(1 for issue in issues if issue["level"] == "fail")
    warns = sum(1 for issue in issues if issue["level"] == "warn")
    return {
        "mode": "live" if live else "paper",
        "liveTradingEnabled": bool(live),
        "liveTradingConfirmed": bool(confirmed),
        "maxLiveTradeSol": max_live,
        "privateKeyPresent": key_present,
        "rpcUrl": rpc_url,
        "rpcLoopback": _is_loopback(rpc_url),
        "slippageBps": int(slippage_bps),
        "priorityFeeSol": priority_fee_sol,
        "soldersAvailable": solders,
        "fails": fails,
        "warns": warns,
        "issues": issues,
    }


class _ForbiddenSession:
    """Session stand-in that records and refuses every network attempt."""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def _refuse(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError(f"readiness guard attempted network access via {name}")

        return _refuse


@contextlib.contextmanager
def _isolated_audit_log(research_bot: Any):
    """Point the execution agent's audit writes at a throwaway directory."""
    original = research_bot.AUDIT_LOG_PATH
    directory = tempfile.mkdtemp(prefix="agentictrade-readiness-")
    research_bot.AUDIT_LOG_PATH = os.path.join(directory, "audit.jsonl")
    try:
        yield research_bot.AUDIT_LOG_PATH
    finally:
        research_bot.AUDIT_LOG_PATH = original
        shutil.rmtree(directory, ignore_errors=True)


_SCENARIOS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "paper_pumpfun",
        "live": False,
        "confirmed": False,
        "maxLiveTradeSol": 0.05,
        "amountSol": 0.05,
        "expect": "DRY_RUN_READY",
        "detail": "paper mode returns a dry-run plan without a key or a socket",
    },
    {
        "name": "live_unconfirmed",
        "live": True,
        "confirmed": False,
        "maxLiveTradeSol": 0.05,
        "amountSol": 0.05,
        "expect": "BLOCKED",
        "detail": "live without LIVE_TRADING_CONFIRMED is blocked before routing",
    },
    {
        "name": "live_over_max",
        "live": True,
        "confirmed": True,
        "maxLiveTradeSol": 0.01,
        "amountSol": 0.05,
        "expect": "BLOCKED",
        "detail": "live above MAX_LIVE_TRADE_SOL is blocked before routing",
    },
    {
        "name": "live_missing_key",
        "live": True,
        "confirmed": True,
        "maxLiveTradeSol": 0.05,
        "amountSol": 0.05,
        "expect": "ERROR",
        "detail": "live without a key fails before any request is sent",
    },
)


async def _run_guards() -> Dict[str, Any]:
    # Imported lazily: research_bot reads .env and configures logging at import.
    # Its loader prints a banner to stdout; keep that out of --json output.
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        import research_bot

    results: List[Dict[str, Any]] = []
    with _isolated_audit_log(research_bot):
        for scenario in _SCENARIOS:
            session: Any = _ForbiddenSession()
            agent = research_bot.SolanaExecutionAgent(
                rpc_url="http://127.0.0.1:9",
                private_key_b58="",
                live_trading_enabled=scenario["live"],
                live_trading_confirmed=scenario["confirmed"],
                max_live_trade_sol=scenario["maxLiveTradeSol"],
                slippage_bps=500,
                priority_fee_sol=0.001,
            )
            outcome = await agent.execute_buy(
                session,
                SOL_MINT,
                scenario["amountSol"],
                dex_id="pumpfun",
            )
            observed = str(outcome.get("status", "MISSING"))
            network_calls = session.calls
            passed = observed == scenario["expect"] and network_calls == 0
            results.append(
                {
                    "name": scenario["name"],
                    "expected": scenario["expect"],
                    "observed": observed,
                    "networkCalls": network_calls,
                    "passed": passed,
                    "detail": scenario["detail"],
                    "reason": outcome.get("reason"),
                }
            )

    failed = [item for item in results if not item["passed"]]
    return {
        "ok": not failed,
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "networkCalls": sum(item["networkCalls"] for item in results),
        "scenarios": results,
    }


def audit_guards() -> Dict[str, Any]:
    """Exercise the fail-closed branches of ``execute_buy`` with no network."""
    return asyncio.run(_run_guards())


def _probe_rpc(rpc_url: str, timeout: float = 5.0) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(rpc_url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "detail": f"unsupported RPC scheme {parsed.scheme!r}"}
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []}
    ).encode("utf-8")
    request = urllib.request.Request(
        rpc_url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        result = body.get("result")
        return {"ok": result == "ok", "detail": str(result or body.get("error"))}
    except Exception as exc:  # noqa: BLE001 - probes must never raise
        return {"ok": False, "detail": str(exc)}


def _probe_jupiter(timeout: float = 5.0) -> Dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "inputMint": SOL_MINT,
            "outputMint": USDC_MINT,
            "amount": "1000000",
            "slippageBps": "500",
        }
    )
    url = f"https://quote-api.jup.ag/v6/quote?{query}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return {
            "ok": bool(body.get("outAmount")),
            "detail": f"outAmount={body.get('outAmount')}",
        }
    except Exception as exc:  # noqa: BLE001 - probes must never raise
        return {"ok": False, "detail": str(exc)}


def run_probes(rpc_url: str) -> Dict[str, Any]:
    """Opt-in network probes; never called unless ``probe=True``."""
    return {"rpc": _probe_rpc(rpc_url), "jupiter": _probe_jupiter()}


def run_readiness(
    env: Optional[Mapping[str, str]] = None,
    *,
    probe: bool = False,
) -> Dict[str, Any]:
    """Combine the environment audit, the guard harness and optional probes."""
    environment = check_environment(env)
    guards = audit_guards()
    if not guards["ok"] or environment["fails"]:
        status = STATUS_BLOCKED
    elif environment["liveTradingEnabled"]:
        status = STATUS_READY
    else:
        status = STATUS_PAPER

    report: Dict[str, Any] = {
        "status": status,
        "environment": environment,
        "guards": guards,
        "probes": run_probes(environment["rpcUrl"]) if probe else None,
        "networkUsed": bool(probe),
    }
    return report


def _fmt_bool(value: Any) -> str:
    return "yes" if value else "no"


def render_report(report: Mapping[str, Any]) -> str:
    """Markdown rendering of a :func:`run_readiness` report."""
    environment = report.get("environment") or {}
    guards = report.get("guards") or {}
    status = report.get("status", STATUS_BLOCKED)
    lines = [
        "# Live-execution readiness",
        "",
        f"- Status: **{status}**",
        f"- Mode: `{environment.get('mode', 'unknown')}`",
        f"- Live trading enabled: {_fmt_bool(environment.get('liveTradingEnabled'))}",
        f"- Explicit confirmation: {_fmt_bool(environment.get('liveTradingConfirmed'))}",
        f"- Max live trade: {environment.get('maxLiveTradeSol')} SOL",
        f"- Private key: {'set' if environment.get('privateKeyPresent') else 'not set'}",
        f"- RPC: {environment.get('rpcUrl')}",
        f"- solders available: {_fmt_bool(environment.get('soldersAvailable'))}",
        "",
        "## Configuration",
        "",
    ]
    issues = environment.get("issues") or []
    if issues:
        lines += ["| Level | Key | Detail |", "|---|---|---|"]
        for issue in issues:
            lines.append(
                f"| {issue.get('level')} | `{issue.get('key')}` | {issue.get('message')} |"
            )
    else:
        lines.append("No configuration issues.")

    lines += [
        "",
        "## Guard scenarios",
        "",
        f"{guards.get('passed', 0)} passed, {guards.get('failed', 0)} failed, "
        f"{guards.get('networkCalls', 0)} network call(s).",
        "",
        "| Scenario | Expected | Observed | Network calls | Result |",
        "|---|---|---|---|---|",
    ]
    for scenario in guards.get("scenarios") or []:
        result = "pass" if scenario.get("passed") else "FAIL"
        lines.append(
            f"| {scenario.get('name')} | {scenario.get('expected')} | "
            f"{scenario.get('observed')} | {scenario.get('networkCalls', 0)} | {result} |"
        )

    probes = report.get("probes")
    if probes:
        lines += ["", "## Probes", ""]
        for name, result in probes.items():
            lines.append(
                f"- {name}: {'ok' if result.get('ok') else 'failed'} "
                f"({result.get('detail')})"
            )

    lines += [
        "",
        "`PAPER` means on-chain orders are disabled. `READY` means live trading is "
        "armed and the fail-closed guards passed; it is not a recommendation to "
        "trade. `BLOCKED` means the configuration or a guard failed.",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentictrade.live_readiness",
        description=(
            "Read-only live-execution readiness audit. Never places an order; "
            "the network is only used with --probe."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="path to the .env file to merge over the process environment (default: .env)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="also probe RPC getHealth and a Jupiter quote (uses the network)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    values = dict(os.environ)
    resolved_env = Path(args.env_file)
    env_note: Optional[str] = None
    if args.env_file and resolved_env.exists():
        from agentictrade.doctor import load_env_file

        values.update(load_env_file(resolved_env))
        env_note = str(resolved_env)
    elif args.env_file:
        env_note = f"{resolved_env} (missing; using the process environment)"

    report = run_readiness(values, probe=args.probe)
    report["envFile"] = env_note
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_report(report))
    return 0 if report["status"] in (STATUS_PAPER, STATUS_READY) else 1


if __name__ == "__main__":
    raise SystemExit(main())
