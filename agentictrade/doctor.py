"""Pre-flight diagnostics for AgenticTrade.

Run this before starting the bot, or in CI, to catch configuration mistakes
early instead of discovering them in the middle of a trading cycle::

    python -m agentictrade.doctor
    python -m agentictrade.doctor --json
    python -m agentictrade.doctor --no-port-check

The command is read-only. It never places orders, never enables live trading and
never writes to the portfolio database (it only reads the schema version).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from agentictrade import validate as validation
from agentictrade.llm import LLMConfig, probe as probe_llm

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
REQUIRED_PACKAGES = ("aiohttp", "pydantic", "python-dotenv")
OPTIONAL_PACKAGES = {
    "solders": "live on-chain signing",
    "google-genai": "Gemini research agent",
    "tzdata": "IANA timezones on Windows",
}


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail" | "info"
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def load_env_file(path: Path) -> Dict[str, str]:
    """Load a .env file without mutating os.environ."""
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values

        return {k: str(v) for k, v in dotenv_values(dotenv_path=path).items() if v is not None}
    except ImportError:
        values: Dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values


def _package_version(name: str) -> Optional[str]:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 11):
        return Check("python", "ok", f"Python {version}")
    return Check("python", "fail", f"Python {version} is too old; 3.11+ is required")


def check_packages() -> List[Check]:
    checks = []
    missing = [name for name in REQUIRED_PACKAGES if not _package_version(name)]
    if missing:
        checks.append(
            Check("packages", "fail", f"missing required package(s): {', '.join(missing)}")
        )
    else:
        rendered = ", ".join(f"{name}=={_package_version(name)}" for name in REQUIRED_PACKAGES)
        checks.append(Check("packages", "ok", rendered))

    for name, purpose in OPTIONAL_PACKAGES.items():
        version = _package_version(name)
        if version:
            checks.append(Check(f"package:{name}", "ok", f"{version} ({purpose})"))
        else:
            checks.append(
                Check(f"package:{name}", "info", f"not installed (optional: {purpose})")
            )
    return checks


def check_database(db_path: Path) -> Check:
    parent = db_path.parent
    if not parent.exists():
        return Check("database", "fail", f"directory does not exist: {parent}")
    if not os.access(parent, os.W_OK):
        return Check("database", "fail", f"directory is not writable: {parent}")
    if not db_path.exists():
        return Check("database", "ok", f"{db_path} will be created on first run")

    try:
        from agentictrade.migrations import apply_migrations

        version = apply_migrations(str(db_path))
    except Exception as exc:  # pragma: no cover - depends on real sqlite state
        return Check("database", "fail", f"cannot open {db_path}: {exc}")
    return Check("database", "ok", f"{db_path} (schema {version})")


def check_audit_log(audit_path: Path) -> Check:
    parent = audit_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        return Check("audit_log", "fail", f"cannot append to {audit_path}: {exc}")
    return Check("audit_log", "ok", str(audit_path))


def check_port(host: str, port: int) -> Check:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        return Check(
            "port",
            "warn",
            f"{host}:{port} is busy ({exc}); the bot falls back to terminal mode",
        )
    return Check("port", "ok", f"{host}:{port} is free")


def check_env_permissions(path: Path) -> Check:
    """Warn when a .env file with potential secrets is readable by others."""
    if not path.exists():
        return Check("env_file_mode", "info", "no .env file to check")
    if os.name == "nt":
        return Check("env_file_mode", "info", "permission checks are skipped on Windows")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return Check("env_file_mode", "warn", f"cannot stat {path}: {exc}")
    if mode & 0o077:
        return Check(
            "env_file_mode",
            "warn",
            f"{path} is mode {mode:04o}; it may contain tokens. Run: chmod 600 {path}",
        )
    return Check("env_file_mode", "ok", f"{path} is mode {mode:04o}")


def check_exposure(host: str, token: str, allow_insecure: bool = False) -> Check:
    if not token:
        if host in LOOPBACK_HOSTS:
            return Check("dashboard_auth", "warn", "no token, but the dashboard is loopback-only")
        if allow_insecure:
            return Check(
                "dashboard_auth",
                "warn",
                f"no DASHBOARD_TOKEN and the server binds {host}; ALLOW_INSECURE_DASHBOARD=true "
                "accepts an unauthenticated API",
            )
        return Check(
            "dashboard_auth",
            "fail",
            f"no DASHBOARD_TOKEN and the server binds {host}; the trading API would be "
            "unauthenticated. Set a token or bind 127.0.0.1",
        )
    if len(token) < 12:
        return Check("dashboard_auth", "warn", "DASHBOARD_TOKEN is shorter than 12 characters")
    return Check("dashboard_auth", "ok", f"token set ({len(token)} chars)")


def check_timezone(name: Optional[str]) -> Check:
    if not name:
        return Check("timezone", "ok", "default Europe/Zagreb")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
    except Exception:
        return Check("timezone", "warn", f"unknown timezone {name!r}; using the CET fallback")
    return Check("timezone", "ok", name)


def check_llm(values: Mapping[str, str]) -> Check:
    """Report which LLM backend the research agent will use, and if it is up."""
    try:
        config = LLMConfig.from_env(values)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return Check("llm", "warn", f"could not resolve LLM config: {exc}")
    if not config.providers():
        return Check(
            "llm",
            "info",
            "no provider configured; the research agent uses heuristics "
            "(set GEMINI_API_KEY or LLM_BASE_URL + LLM_MODEL)",
        )
    try:
        result = probe_llm(config, timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        return Check("llm", "warn", f"probe failed: {exc}")
    chain = " → ".join(spec.label for spec in config.providers())
    status = {"ok": "ok", "unconfigured": "info"}.get(result["state"], "warn")
    return Check("llm", status, f"{chain}: {result['detail']}")


def run_checks(
    env: Optional[Mapping[str, str]] = None,
    *,
    env_file: Optional[Path] = None,
    db_path: Optional[Path] = None,
    audit_path: Optional[Path] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    check_socket: bool = True,
) -> List[Check]:
    values = dict(os.environ if env is None else env)
    if env_file is not None:
        file_values = load_env_file(env_file)
        if not file_values:
            checks: List[Check] = [Check("env_file", "warn", f"no readable file at {env_file}")]
        else:
            checks = [Check("env_file", "ok", f"{env_file} ({len(file_values)} keys)")]
        # File values win: this mirrors python-dotenv's override=True usage.
        values.update(file_values)
        checks.append(check_env_permissions(env_file))
    else:
        checks = [Check("env_file", "ok", "using the process environment")]

    checks.append(check_python())
    checks.extend(check_packages())

    issues = validation.validate_env(values)
    for issue in issues:
        checks.append(Check(issue.key, issue.level, issue.message))
    fails, warns, _ = validation.summarize(issues)
    checks.append(
        Check(
            "config",
            "ok" if fails == 0 else "fail",
            f"{len(issues)} issue(s): {fails} fail, {warns} warn",
        )
    )

    resolved_db = db_path or Path(values.get("PAPER_DB_PATH") or "paper_portfolio.db")
    checks.append(check_database(resolved_db))

    resolved_audit = audit_path or Path(values.get("AUDIT_LOG_PATH") or "bot_audit.jsonl")
    checks.append(check_audit_log(resolved_audit))

    resolved_host = host or values.get("COMMAND_CENTER_HOST") or "127.0.0.1"
    checks.append(
        check_exposure(
            resolved_host,
            str(values.get("DASHBOARD_TOKEN") or "").strip(),
            allow_insecure=str(values.get("ALLOW_INSECURE_DASHBOARD") or "").strip().lower()
            in ("1", "true", "yes", "on"),
        )
    )

    try:
        resolved_port = int(port if port is not None else values.get("COMMAND_CENTER_PORT") or 8080)
    except (TypeError, ValueError):
        checks.append(Check("port", "fail", "COMMAND_CENTER_PORT is not an integer"))
    else:
        if check_socket:
            checks.append(check_port(resolved_host, resolved_port))

    checks.append(check_timezone(values.get("BOT_TIMEZONE")))
    checks.append(check_llm(values))
    return checks


_SYMBOLS = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "INFO"}


def render(checks: Sequence[Check], *, show_info: bool = True) -> str:
    lines = []
    for check in checks:
        if check.status == "info" and not show_info:
            continue
        lines.append(f"[{_SYMBOLS.get(check.status, check.status)}] {check.name}: {check.detail}")
    fails = sum(1 for check in checks if check.status == "fail")
    warns = sum(1 for check in checks if check.status == "warn")
    lines.append("")
    lines.append(f"Summary: {fails} failure(s), {warns} warning(s)")
    lines.append("Result: " + ("FAILED" if fails else "OK"))
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentictrade.doctor",
        description="Validate AgenticTrade configuration before starting the bot.",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    parser.add_argument("--db", help="portfolio database path (default: PAPER_DB_PATH or paper_portfolio.db)")
    parser.add_argument("--audit-log", help="audit log path (default: AUDIT_LOG_PATH or bot_audit.jsonl)")
    parser.add_argument("--host", help="host the dashboard binds (default: COMMAND_CENTER_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="dashboard port (default: COMMAND_CENTER_PORT or 8080)")
    parser.add_argument("--no-port-check", action="store_true", help="skip the TCP bind test")
    parser.add_argument("--no-info", action="store_true", help="hide informational checks")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    checks = run_checks(
        env_file=Path(args.env_file),
        db_path=Path(args.db) if args.db else None,
        audit_path=Path(args.audit_log) if args.audit_log else None,
        host=args.host,
        port=args.port,
        check_socket=not args.no_port_check,
    )
    fails = sum(1 for check in checks if check.status == "fail")
    warns = sum(1 for check in checks if check.status == "warn")

    if args.json:
        print(
            json.dumps(
                {
                    "ok": fails == 0,
                    "failures": fails,
                    "warnings": warns,
                    "checks": [check.as_dict() for check in checks],
                },
                indent=2,
            )
        )
    else:
        print(render(checks, show_info=not args.no_info))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
