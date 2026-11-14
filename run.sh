#!/usr/bin/env bash
#
# BetterCallTrader launcher for Linux / Ubuntu (and macOS).
#
#   ./run.sh              # pre-flight doctor check, then start the bot
#   ./run.sh --skip-doctor
#   ./run.sh --help
#
# Setup (virtualenv + pinned dependencies) is delegated to ./scripts/bootstrap.sh, the
# same script CI uses, so there is only one install path to maintain.
# Overrides: PYTHON_BIN=python3.12 VENV_DIR=.venv ./run.sh
#
# When LLM_BASE_URL points at a local LM Studio server (127.0.0.1/localhost),
# this script starts it with `lms server start` and stops it again when the bot
# exits. A server that was already running is left untouched.
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_DIR="${VENV_DIR:-.venv}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. LM Studio lifecycle ----------------------------------------------
# Only touch LM Studio when the bot is configured to talk to a local server.
LMS_BIN=""
LMS_STARTED=0
LMS_PORT=1234
LMS_ENV_AUTOSTART=""
if [ -f .env ]; then
    LMS_ENV_AUTOSTART="$(sed -n 's/^[[:space:]]*LMS_AUTOSTART[[:space:]]*=[[:space:]]*//p' .env | tail -n1)"
fi
LMS_AUTOSTART="${LMS_ENV_AUTOSTART:-${LMS_AUTOSTART:-auto}}"

detect_lms() {
    if command -v lms >/dev/null 2>&1; then
        LMS_BIN="$(command -v lms)"
    elif [ -x "$HOME/.lmstudio/bin/lms" ]; then
        LMS_BIN="$HOME/.lmstudio/bin/lms"
    fi
}

llm_base_url() {
    local url=""
    # The bot loads .env with override=True, so .env is authoritative here too.
    if [ -f .env ]; then
        url="$(sed -n 's/^[[:space:]]*LLM_BASE_URL[[:space:]]*=[[:space:]]*//p' .env | tail -n1)"
        url="${url%\"}"; url="${url#\"}"
        url="${url%\'}"; url="${url#\'}"
    fi
    [ -n "$url" ] || url="${LLM_BASE_URL:-}"
    printf '%s' "$url"
}

lms_running() {
    local out
    out="$("$LMS_BIN" server status 2>&1 || true)"
    case "$out" in
        *"is running"*) return 0 ;;
    esac
    return 1
}

start_lms() {
    detect_lms
    [ -n "$LMS_BIN" ] || { log "lms not found; skipping LM Studio lifecycle."; return 0; }
    local url port
    url="$(llm_base_url)"
    [ -n "$url" ] || return 0
    case "$url" in
        *127.0.0.1*|*localhost*|*"\[::1\]"*) ;;
        *) log "LLM_BASE_URL is remote; not managing LM Studio."; return 0 ;;
    esac
    port="$(printf '%s' "$url" | sed -nE 's#^[a-zA-Z]+://[^/:]+:([0-9]+).*#\1#p')"
    [ -n "$port" ] && LMS_PORT="$port"
    case "$LMS_AUTOSTART" in
        false|0|no|off|none)
            log "LM Studio autostart disabled (LMS_AUTOSTART=${LMS_AUTOSTART})."
            return 0
            ;;
        true|1|yes|on) ;;
        *)
            # auto: only the default LM Studio port, so Ollama/llama.cpp/vLLM
            # endpoints on other ports are never hijacked.
            if [ "$LMS_PORT" != "1234" ]; then
                log "LLM_BASE_URL port ${LMS_PORT} is not LM Studio's default; not managing LM Studio."
                return 0
            fi
            ;;
    esac
    if lms_running; then
        log "LM Studio server already running; leaving it up."
        return 0
    fi
    log "Starting LM Studio server on port ${LMS_PORT}"
    if "$LMS_BIN" server start --port "$LMS_PORT" >/dev/null 2>&1; then
        LMS_STARTED=1
    else
        warn "could not start LM Studio - continuing without the local model fallback."
    fi
}

stop_lms() {
    [ "$LMS_STARTED" -eq 1 ] || return 0
    LMS_STARTED=0
    log "Stopping the LM Studio server started by this run"
    "$LMS_BIN" server stop >/dev/null 2>&1 || true
}

CHILD_PID=""
cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    stop_lms
    exit "$rc"
}
forward_signal() {
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'forward_signal; exit 143' TERM
trap 'forward_signal; exit 130' INT

SKIP_DOCTOR=0
for arg in "$@"; do
    case "$arg" in
        --skip-doctor) SKIP_DOCTOR=1 ;;
        -h|--help)
            sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown argument: $arg (try --help)" ;;
    esac
done

# --- 1. Environment + dependencies ---------------------------------------
VENV_DIR="$VENV_DIR" ./scripts/bootstrap.sh

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

# --- 3. Local model server ------------------------------------------------
start_lms

# --- 4. Pre-flight --------------------------------------------------------
if [ "$SKIP_DOCTOR" -eq 0 ]; then
    log "Running pre-flight checks (python -m bettercalltrader.doctor)"
    if ! "$PY" -m bettercalltrader.doctor; then
        die "pre-flight checks reported FAIL - fix them before starting. Re-run with --skip-doctor to bypass."
    fi
fi

# --- 5. Start ------------------------------------------------------------
log "Starting BetterCallTrader - Command Center at http://localhost:${COMMAND_CENTER_PORT:-8080}"
set +e
"$PY" py/research_bot.py &
CHILD_PID=$!
wait "$CHILD_PID"
rc=$?
set -e
exit "$rc"
