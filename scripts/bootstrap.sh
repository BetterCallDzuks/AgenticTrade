#!/usr/bin/env bash
#
# Create/refresh the project virtualenv and install the pinned dependencies.
#
# Shared by ./run.sh and CI so local and CI use exactly one setup path.
#
#   ./scripts/bootstrap.sh          # runtime deps from requirements.txt
#   ./scripts/bootstrap.sh --dev    # runtime + requirements-dev.txt (ruff, mypy, pip-audit)
#
# Environment overrides:
#   PYTHON_BIN=python3.12    interpreter used to create the venv
#   VENV_DIR=.venv           venv location
#
# When GITHUB_ENV is set (GitHub Actions) the resolved interpreter is exported
# as VENV_PY so later steps can call "$VENV_PY" on every OS.
#
set -euo pipefail

# The scripts/ directory holds this helper; the project root (venv, source and
# .env) is its parent.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VENV_DIR="${VENV_DIR:-.venv}"
WITH_DEV=0

for arg in "$@"; do
    case "$arg" in
        --dev) WITH_DEV=1 ;;
        -h|--help)
            sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) printf '[ERROR] unknown argument: %s (try --help)\n' "$arg" >&2; exit 2 ;;
    esac
done

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

# Pick an interpreter: explicit override > python3 > python (Windows Git Bash).
if [ -n "${PYTHON_BIN:-}" ]; then
    :
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
else
    printf '[ERROR] no python3/python on PATH\n' >&2
    exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf '[ERROR] %s not found. Install Python 3.11+: sudo apt install python3 python3-venv\n' "$PYTHON_BIN" >&2
    exit 1
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || { printf '[ERROR] Python 3.11+ required, found %s\n' "$("$PYTHON_BIN" -V 2>&1)" >&2; exit 1; }

# --- venv -----------------------------------------------------------------
if [ ! -x "$VENV_DIR/bin/python" ] && [ ! -x "$VENV_DIR/Scripts/python.exe" ]; then
    log "Creating virtual environment in $VENV_DIR ($("$PYTHON_BIN" -V 2>&1))"
    "$PYTHON_BIN" -m venv "$VENV_DIR" \
        || { printf '[ERROR] could not create %s. On Debian/Ubuntu: sudo apt install python3-venv\n' "$VENV_DIR" >&2; exit 1; }
fi

if [ -x "$VENV_DIR/bin/python" ]; then
    PY="$VENV_DIR/bin/python"
elif [ -x "$VENV_DIR/Scripts/python.exe" ]; then
    PY="$VENV_DIR/Scripts/python.exe"
else
    printf '[ERROR] no interpreter found in %s\n' "$VENV_DIR" >&2
    exit 1
fi

# --- dependencies ---------------------------------------------------------
file_hash() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        wc -c < "$1" | tr -d ' '
    fi
}

CACHE_DIR="$VENV_DIR/.cache"
mkdir -p "$CACHE_DIR"

install_if_changed() {
    local reqs="$1" stamp
    stamp="$CACHE_DIR/$(basename "$reqs").sha256"
    local hash
    hash="$(file_hash "$reqs")"
    if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$hash" ]; then
        return 0
    fi
    log "Installing $reqs"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r "$reqs"
    printf '%s\n' "$hash" > "$stamp"
}

install_if_changed requirements.txt
if [ "$WITH_DEV" -eq 1 ]; then
    install_if_changed requirements-dev.txt
fi

# --- hand the interpreter to CI ------------------------------------------
if [ -n "${GITHUB_ENV:-}" ]; then
    printf 'VENV_PY=%s\n' "$PY" >> "$GITHUB_ENV"
fi

log "Ready: $("$PY" -V 2>&1) at $PY"
