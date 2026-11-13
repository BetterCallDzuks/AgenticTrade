#!/usr/bin/env bash
#
# Restore BetterCallTrader state from a backup created by ./backup.sh.
#
#   ./restore.sh                 # newest backup under ~/BetterCallTraderBackup
#   ./restore.sh <backup-dir>    # explicit backup directory
#   ./restore.sh -y <backup-dir> # skip the confirmation prompt
#
# Mirrors restore.ps1: overwrites .env, the DB, logs, audit trail and ML model
# in the project directory, plus the Docker Compose `data/` directory when the
# backup contains one.
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

BACKUP_ROOT="$HOME/BetterCallTraderBackup"
ASSUME_YES=0
BACKUP_PATH=""

for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help)
            sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*) printf '[ERROR] unknown option: %s\n' "$arg" >&2; exit 2 ;;
        *) BACKUP_PATH="$arg" ;;
    esac
done

if [ -z "$BACKUP_PATH" ]; then
    [ -d "$BACKUP_ROOT" ] || { printf '[ERROR] backup folder not found: %s\n' "$BACKUP_ROOT" >&2; exit 1; }
    BACKUP_PATH="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\t%p\n' 2>/dev/null \
        | sort -r | head -n 1 | cut -f2)"
    [ -n "$BACKUP_PATH" ] || { printf '[ERROR] no backups found under %s\n' "$BACKUP_ROOT" >&2; exit 1; }
fi

[ -d "$BACKUP_PATH" ] || { printf '[ERROR] backup path not found: %s\n' "$BACKUP_PATH" >&2; exit 1; }

printf 'Restoring from: %s\n' "$BACKUP_PATH"

if [ "$ASSUME_YES" -ne 1 ]; then
    printf 'This overwrites local .env, DB, log, audit trail and ML model. Continue? (y/N) '
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) printf 'Restore cancelled.\n'; exit 0 ;;
    esac
fi

restored=0
for file in paper_portfolio.db paper_portfolio.db-wal paper_portfolio.db-shm \
            .env bot_history.log bot_audit.jsonl ml_model.json; do
    if [ -f "$BACKUP_PATH/$file" ]; then
        cp -p "$BACKUP_PATH/$file" "./$file"
        printf 'Restored %s\n' "$file"
        restored=$((restored + 1))
    fi
done

if [ -d "$BACKUP_PATH/data" ]; then
    mkdir -p "./data"
    for file in paper_portfolio.db paper_portfolio.db-wal paper_portfolio.db-shm \
                bot_history.log bot_audit.jsonl ml_model.json; do
        if [ -f "$BACKUP_PATH/data/$file" ]; then
            cp -p "$BACKUP_PATH/data/$file" "./data/$file"
            printf 'Restored data/%s\n' "$file"
            restored=$((restored + 1))
        fi
    done
fi

if [ "$restored" -eq 0 ]; then
    printf '[ERROR] backup contains none of the expected files: %s\n' "$BACKUP_PATH" >&2
    exit 1
fi

printf 'Restore completed (%s file(s)).\n' "$restored"
