#!/bin/bash
# cluster-config-migrate.sh — Migrate runtime config to PostgreSQL
#
# Story #578: Centralize Runtime Configuration in PostgreSQL
#
# Idempotent: safe to run multiple times on multiple nodes.
# Run on each cluster node after upgrading to v9.6.4+.
#
# Usage:
#   ./cluster-config-migrate.sh [--dry-run] [--rollback]

set -euo pipefail

CIDX_DATA_DIR="${HOME}/.cidx-server"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHONPATH="${PROJECT_ROOT}/src"
PYTHON_BIN="python3"
DRY_RUN=false
ROLLBACK=false

CONFIG_JSON="${CIDX_DATA_DIR}/config.json"
BACKUP_DIR="${CIDX_DATA_DIR}/config-migration-backup"
BACKUP_FILE="${BACKUP_DIR}/config.json.pre-centralization"
HELPER="${SCRIPT_DIR}/config_migration_helper.py"

# Argument parsing
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cidx-data-dir) CIDX_DATA_DIR="$2"; CONFIG_JSON="${2}/config.json"; BACKUP_DIR="${2}/config-migration-backup"; BACKUP_FILE="${BACKUP_DIR}/config.json.pre-centralization"; shift 2 ;;
        --dry-run)       DRY_RUN=true;  shift ;;
        --rollback)      ROLLBACK=true; shift ;;
        -h|--help)       echo "Usage: $0 [--dry-run] [--rollback] [--cidx-data-dir PATH]"; exit 0 ;;
        *) echo "Unknown: $1" >&2; exit 1 ;;
    esac
done

log_info()  { echo "[INFO]  $*"; }
log_error() { echo "[ERROR] $*" >&2; }

# Rollback
if $ROLLBACK; then
    if [[ ! -f "$BACKUP_FILE" ]]; then
        log_error "No backup found: $BACKUP_FILE"
        exit 1
    fi
    cp "$BACKUP_FILE" "$CONFIG_JSON"
    log_info "Restored config.json from backup"
    log_info "Restart server: sudo systemctl restart cidx-server"
    exit 0
fi

echo ""
echo "CIDX Config Centralization Migration"
echo "====================================="
$DRY_RUN && echo "[DRY RUN]" && echo ""

# Validate
[[ ! -f "$CONFIG_JSON" ]] && log_error "Config not found: $CONFIG_JSON" && exit 1
[[ ! -f "$HELPER" ]] && log_error "Helper not found: $HELPER" && exit 1

storage_mode=$("$PYTHON_BIN" -c "import json; print(json.load(open('${CONFIG_JSON}')).get('storage_mode','sqlite'))" 2>/dev/null)
if [[ "$storage_mode" != "postgres" ]]; then
    log_error "Not in cluster mode (storage_mode=$storage_mode). Use cluster-migrate.sh first."
    exit 1
fi
log_info "Storage mode: postgres (cluster)"

# Backup (idempotent — skip if exists)
if [[ ! -f "$BACKUP_FILE" ]]; then
    if $DRY_RUN; then
        echo "[DRY]   Would backup: $CONFIG_JSON -> $BACKUP_FILE"
    else
        mkdir -p "$BACKUP_DIR"
        cp "$CONFIG_JSON" "$BACKUP_FILE"
        log_info "Backup created: $BACKUP_FILE"
    fi
else
    log_info "Backup already exists: $BACKUP_FILE"
fi

# Migrate
if $DRY_RUN; then
    echo "[DRY]   Would migrate runtime config to PostgreSQL"
    echo "[DRY]   Would strip config.json to bootstrap-only"
else
    if ! PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" "$HELPER" migrate --config "$CONFIG_JSON"; then
        log_error "Migration failed. Run --rollback to restore."
        exit 1
    fi
fi

# Verify
if ! $DRY_RUN; then
    echo ""
    if ! PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" "$HELPER" verify --config "$CONFIG_JSON"; then
        log_error "Verification failed. Run --rollback to restore."
        exit 1
    fi
fi

echo ""
log_info "Migration complete. Restart: sudo systemctl restart cidx-server"
log_info "Then run this script on other cluster nodes (idempotent)."
