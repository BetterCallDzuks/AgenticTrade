#!/usr/bin/env bash
#
# AgenticTrade launcher for Linux / Ubuntu (and macOS).
#
#   ./run.sh              # pre-flight doctor check, then start the bot
#   ./run.sh --skip-doctor
#   ./run.sh --help
#
# Setup (virtualenv + pinned dependencies) is delegated to ./bootstrap.sh, the
# same script CI uses, so there is only one install path to maintain.
# Overrides: PYTHON_BIN=python3.12 VENV_DIR=.venv ./run.sh
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_DIR="${VENV_DIR:-.venv}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

SKIP_DOCTOR=0
for arg in "$@"; do
    case "$arg" in
        --skip-doctor) SKIP_DOCTOR=1 ;;
        -h|--help)
            sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown argument: $arg (try --help)" ;;
    esac
done

# --- 1. Environment + dependencies ---------------------------------------
VENV_DIR="$VENV_DIR" ./bootstrap.sh

if [ -x "$VENV_DIR/bin/python" ]; then
    PY="$VENV_DIR/bin/python"
elif [ -x "$VENV_DIR/Scripts/python.exe" ]; then
    PY="$VENV_DIR/Scripts/python.exe"
else
    die "no interpreter found in $VENV_DIR"
fi

# --- 2. Local configuration ----------------------------------------------
# NOTE: we deliberately do not copy .env.example for you. Its placeholder
# GEMINI_API_KEY ("tvoj_gemini_api_kljuc_ovdje") is a truthy string, which
# would switch the bot into AI mode and fail every call against the API.
if [ ! -f .env ]; then
    warn "no .env found - running with defaults and system env vars."
    warn "create one and edit it before relying on the AI agent:"
    warn "    cp .env.example .env && \$EDITOR .env"
    warn "keep LIVE_TRADING_ENABLED=false."
fi

# --- 3. Pre-flight -------------------------------------------------------
if [ "$SKIP_DOCTOR" -eq 0 ]; then
    log "Running pre-flight checks (python -m agentictrade.doctor)"
    if ! "$PY" -m agentictrade.doctor; then
        die "pre-flight checks reported FAIL - fix them before starting. Re-run with --skip-doctor to bypass."
    fi
fi

# --- 4. Start ------------------------------------------------------------
log "Starting AgenticTrade - Command Center at http://localhost:${COMMAND_CENTER_PORT:-8080}"
exec "$PY" research_bot.py
