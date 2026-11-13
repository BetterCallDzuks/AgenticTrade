#!/usr/bin/env bash
#
# Back up the mutable BetterCallTrader state (DB, secrets, logs, audit, ML model).
#
#   ./scripts/backup.sh                          # -> ~/BetterCallTraderBackup/<timestamp>/
#   ./scripts/backup.sh /media/usb/at-backups    # custom destination root
#   ./scripts/backup.sh --no-secrets             # omit .env (recommended when sharing)
#
# Handles both layouts: files in the project root (local runs) and the Docker
# Compose `data/` directory, which is copied as `data/` inside the backup.
# Mirrors backup.ps1, plus the SQLite -wal/-shm sidecars: copying only the .db
# while the bot is running can capture a stale or torn snapshot.
#
set -euo pipefail

# The scripts/ directory holds this helper; the project root (state files and
# data/) is its parent.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SRC_DIR="$PWD"
DEST_ROOT=""
INCLUDE_ENV=1

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --no-secrets) INCLUDE_ENV=0 ;;
        -*) printf '[ERROR] unknown option: %s (try --help)\n' "$arg" >&2; exit 2 ;;
        *)
            [ -z "$DEST_ROOT" ] || { printf '[ERROR] only one destination may be given\n' >&2; exit 2; }
            DEST_ROOT="$arg"
            ;;
    esac
done

DEST_ROOT="${DEST_ROOT:-$HOME/BetterCallTraderBackup}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
TARGET="$DEST_ROOT/$TIMESTAMP"

FILES=(
    paper_portfolio.db
    paper_portfolio.db-wal
    paper_portfolio.db-shm
    .env
    bot_history.log
    bot_audit.jsonl
    ml_model.json
)

mkdir -p "$TARGET"

copied=0
for file in "${FILES[@]}"; do
    if [ "$file" = ".env" ] && [ "$INCLUDE_ENV" -eq 0 ]; then
        continue
    fi
    if [ -f "$SRC_DIR/$file" ]; then
        cp -p "$SRC_DIR/$file" "$TARGET/"
        copied=$((copied + 1))
    fi
done

# Docker Compose keeps the same files under ./data (see docker-compose.yml).
DATA_FILES=(
    paper_portfolio.db
    paper_portfolio.db-wal
    paper_portfolio.db-shm
    bot_history.log
    bot_audit.jsonl
    ml_model.json
)
if [ -d "$SRC_DIR/data" ]; then
    for file in "${DATA_FILES[@]}"; do
        if [ -f "$SRC_DIR/data/$file" ]; then
            mkdir -p "$TARGET/data"
            cp -p "$SRC_DIR/data/$file" "$TARGET/data/"
            copied=$((copied + 1))
        fi
    done
fi

if [ -f "$SRC_DIR/.env" ] && [ "$INCLUDE_ENV" -eq 1 ]; then
    printf '[WARN] .env was copied into the backup; it may contain DASHBOARD_TOKEN, API keys\n' >&2
    printf '       or a GitHub token. Use --no-secrets to exclude it.\n' >&2
fi

if [ "$copied" -eq 0 ]; then
    rmdir "$TARGET" 2>/dev/null || true
    printf '[WARN] nothing to back up in %s (no DB, .env or logs yet)\n' "$SRC_DIR" >&2
    exit 1
fi

db_wal_present=0
for db_dir in "$SRC_DIR" "$SRC_DIR/data"; do
    if [ -f "$db_dir/paper_portfolio.db" ] && { [ -f "$db_dir/paper_portfolio.db-wal" ] || [ -f "$db_dir/paper_portfolio.db-shm" ]; }; then
        db_wal_present=1
    fi
done
if [ "$db_wal_present" -eq 1 ]; then
    if pgrep -f "research_bot.py" >/dev/null 2>&1; then
        printf '[WARN] the bot appears to be running (SQLite WAL files present).\n' >&2
        printf '       Stop it first for a guaranteed-consistent snapshot.\n' >&2
    else
        printf '[NOTE] included SQLite -wal/-shm sidecars (stale WAL from a previous run).\n' >&2
    fi
fi

chmod 700 "$TARGET" 2>/dev/null || true
printf 'Backup created at: %s (%s file(s))\n' "$TARGET" "$copied"
