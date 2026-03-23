#!/bin/bash
# cluster-migrate.sh — Migrate standalone CIDX server to cluster mode
#
# Story #426: Cluster Migrate Script (Seed Cluster from Single Server)
#
# Orchestrates SQLite->PG migration, copies golden repos to shared storage,
# and converts the server to cluster mode.
#
# Usage:
#   ./cluster-migrate.sh \
#     --postgres-url "postgresql://user:pass@host/db" \
#     --ontap-mount "/mnt/fsx" \
#     --cidx-data-dir "~/.cidx-server"
#
# Flags:
#   --dry-run        Print what would be done without making changes
#   --rollback       Restore from backups created by a previous run
#   --python PATH    Path to python3 binary (default: python3)
#   --src-dir PATH   Path to CIDX project root containing src/

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

POSTGRES_URL=""
ONTAP_MOUNT=""
CIDX_DATA_DIR="${HOME}/.cidx-server"
DRY_RUN=false
ROLLBACK=false
PYTHON_BIN="python3"
SRC_DIR=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

usage() {
    cat >&2 <<EOF
Usage: $0 --postgres-url URL --ontap-mount PATH [OPTIONS]

Required:
  --postgres-url URL     PostgreSQL connection URL
                         (e.g. postgresql://user:pass@host/db)
  --ontap-mount PATH     Path to shared NFS/ONTAP mount (e.g. /mnt/fsx)

Optional:
  --cidx-data-dir PATH   CIDX server data directory (default: ~/.cidx-server)
  --dry-run              Print what would be done without making changes
  --rollback             Restore from backups created by a previous migration run
  --python PATH          Path to python3 binary (default: python3)
  --src-dir PATH         Path to CIDX project root containing src/
  -h, --help             Show this help message
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --postgres-url)  POSTGRES_URL="$2";      shift 2 ;;
        --ontap-mount)   ONTAP_MOUNT="$2";       shift 2 ;;
        --cidx-data-dir) CIDX_DATA_DIR="$2";     shift 2 ;;
        --dry-run)       DRY_RUN=true;           shift   ;;
        --rollback)      ROLLBACK=true;          shift   ;;
        --python)        PYTHON_BIN="$2";        shift 2 ;;
        --src-dir)       SRC_DIR="$2";           shift 2 ;;
        -h|--help)       usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

# ---------------------------------------------------------------------------
# Resolve paths (before sourcing lib so lib can use them)
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SRC_DIR:-$(dirname "$SCRIPT_DIR")}"
PYTHONPATH="${PROJECT_ROOT}/src"

# Expand tilde
CIDX_DATA_DIR="${CIDX_DATA_DIR/#\~/$HOME}"

DATA_DIR="${CIDX_DATA_DIR}/data"
SQLITE_DB="${DATA_DIR}/cidx_server.db"
GROUPS_DB="${CIDX_DATA_DIR}/groups.db"
CONFIG_JSON="${CIDX_DATA_DIR}/config.json"
GOLDEN_REPOS_DIR="${DATA_DIR}/golden-repos"
ALIASES_DIR="${GOLDEN_REPOS_DIR}/aliases"
VERSIONED_DIR="${GOLDEN_REPOS_DIR}/.versioned"
BACKUP_DIR="${CIDX_DATA_DIR}/migration-backup"
NFS_GOLDEN_REPOS="${ONTAP_MOUNT}/golden-repos"
NFS_VERSIONED="${ONTAP_MOUNT}/golden-repos/.versioned"
NFS_ALIASES="${ONTAP_MOUNT}/golden-repos/aliases"

# ---------------------------------------------------------------------------
# Source shared helpers
# ---------------------------------------------------------------------------

# shellcheck source=cluster-migrate-lib.sh
source "${SCRIPT_DIR}/cluster-migrate-lib.sh"

# ---------------------------------------------------------------------------
# Stop / start server
# ---------------------------------------------------------------------------

stop_server() {
    log_step "Stopping CIDX server"
    if systemctl is-active --quiet cidx-server 2>/dev/null; then
        run_cmd systemctl stop cidx-server
        log_info "Server stopped."
    else
        log_info "Server is not running (skipping stop)."
    fi
}

restart_server() {
    log_step "Restarting CIDX server"
    if $DRY_RUN; then
        log_dry "Would run: systemctl start cidx-server"
        return
    fi
    if ! systemctl start cidx-server; then
        log_error "Failed to start cidx-server. Check: journalctl -u cidx-server -n 50"
        exit 1
    fi
    sleep 3
    log_info "Server started."
}

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

backup_databases() {
    log_step "Backing up databases and config"

    if $DRY_RUN; then
        log_dry "Would create backup directory: $BACKUP_DIR"
        log_dry "Would backup: $SQLITE_DB, $GROUPS_DB, $CONFIG_JSON"
        [[ -d "$ALIASES_DIR" ]] && log_dry "Would backup alias JSONs: $ALIASES_DIR"
        return
    fi

    mkdir -p "$BACKUP_DIR"
    cp "$SQLITE_DB"  "${BACKUP_DIR}/cidx_server.db.bak"
    cp "$GROUPS_DB"  "${BACKUP_DIR}/groups.db.bak"
    cp "$CONFIG_JSON" "${BACKUP_DIR}/config.json.bak"
    [[ -d "$ALIASES_DIR" ]] && cp -r "$ALIASES_DIR" "${BACKUP_DIR}/aliases"

    log_info "Backups created in: $BACKUP_DIR"
}

# ---------------------------------------------------------------------------
# PostgreSQL schema migrations
# ---------------------------------------------------------------------------

run_schema_migrations() {
    log_step "Running PostgreSQL schema migrations"

    if $DRY_RUN; then
        log_dry "Would run: python3 -m code_indexer.server.storage.postgres.migrations.runner --connection-string '...'"
        return
    fi

    log_info "Applying schema migrations..."
    if ! PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" -m \
        code_indexer.server.storage.postgres.migrations.runner \
        --connection-string "$POSTGRES_URL"; then
        log_error "Schema migrations failed. Aborting."
        exit 1
    fi
    log_info "Schema migrations complete."
}

# ---------------------------------------------------------------------------
# SQLite-to-PostgreSQL data migration
# ---------------------------------------------------------------------------

migrate_data_to_postgres() {
    log_step "Migrating SQLite data to PostgreSQL"

    if $DRY_RUN; then
        log_dry "Would run: python3 -m code_indexer.server.tools.migrate_to_postgres --sqlite-path ... --groups-path ... --pg-url ..."
        return
    fi

    log_info "Running SQLite-to-PostgreSQL data migration..."
    if ! PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" -m \
        code_indexer.server.tools.migrate_to_postgres \
        --sqlite-path "$SQLITE_DB" \
        --groups-path "$GROUPS_DB" \
        --pg-url "$POSTGRES_URL"; then
        log_error "Data migration failed. Aborting."
        log_info "SQLite databases are untouched. Backups at: $BACKUP_DIR"
        exit 1
    fi
    log_info "Data migration complete."
}

# ---------------------------------------------------------------------------
# Copy golden-repos and .versioned to NFS
# ---------------------------------------------------------------------------

copy_golden_repos_to_nfs() {
    log_step "Copying golden-repos to NFS mount"

    if [[ ! -d "$GOLDEN_REPOS_DIR" ]]; then
        log_warn "golden-repos directory not found: $GOLDEN_REPOS_DIR — skipping."
        return
    fi

    if $DRY_RUN; then
        log_dry "Would rsync (excluding .versioned): $GOLDEN_REPOS_DIR/ -> $NFS_GOLDEN_REPOS/"
        return
    fi

    mkdir -p "$NFS_GOLDEN_REPOS"
    log_info "Copying golden-repos (excluding .versioned)..."
    rsync -a --progress --exclude=".versioned" "${GOLDEN_REPOS_DIR}/" "${NFS_GOLDEN_REPOS}/"
    log_info "golden-repos copy complete."
}

copy_versioned_to_nfs() {
    log_step "Copying .versioned snapshots to NFS mount"

    if [[ ! -d "$VERSIONED_DIR" ]]; then
        log_warn ".versioned directory not found: $VERSIONED_DIR — skipping."
        return
    fi

    if $DRY_RUN; then
        log_dry "Would rsync: $VERSIONED_DIR/ -> $NFS_VERSIONED/"
        return
    fi

    mkdir -p "$NFS_VERSIONED"
    log_info "Copying .versioned snapshots..."
    rsync -a --progress "${VERSIONED_DIR}/" "${NFS_VERSIONED}/"
    log_info ".versioned copy complete."
}

# ---------------------------------------------------------------------------
# Update alias JSON files to NFS paths
# ---------------------------------------------------------------------------

# Update a single alias JSON file's target_path from local to NFS path.
# Returns 0 on success, 1 if skipped.
update_single_alias_file() {
    local alias_file="$1"

    local current_target
    current_target=$(PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" -c "
import json, sys
try:
    data = json.load(open('${alias_file}'))
    print(data.get('target_path', ''))
except Exception:
    sys.exit(1)
" 2>/dev/null || echo "")

    if [[ -z "$current_target" ]]; then
        log_warn "Could not read target_path from: $alias_file"
        return 1
    fi

    # Only update paths that are under the local golden-repos directory
    if [[ "$current_target" != "${GOLDEN_REPOS_DIR}"* ]]; then
        return 1
    fi

    local new_target="${current_target/${GOLDEN_REPOS_DIR}/${NFS_GOLDEN_REPOS}}"
    if [[ "$new_target" == "$current_target" ]]; then
        return 1
    fi

    if $DRY_RUN; then
        log_dry "Would update $(basename "$alias_file"): ${current_target} -> ${new_target}"
        return 0
    fi

    PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" - <<PYEOF
import json, os, tempfile
alias_file = '${alias_file}'
new_target = '${new_target}'
aliases_dir = os.path.dirname(alias_file)
base_name = os.path.splitext(os.path.basename(alias_file))[0]

with open(alias_file, 'r') as f:
    data = json.load(f)

data['target_path'] = new_target

tmp_fd, tmp_path = tempfile.mkstemp(
    dir=aliases_dir, prefix=f'.{base_name}_migrate_', suffix='.tmp'
)
try:
    with os.fdopen(tmp_fd, 'w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, alias_file)
except Exception as e:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise RuntimeError(f'Failed to update alias: {e}') from e
PYEOF

    log_info "Updated $(basename "$alias_file"): ${current_target} -> ${new_target}"
    return 0
}

update_alias_json_files() {
    log_step "Updating alias JSON files to reflect NFS paths"

    if [[ ! -d "$ALIASES_DIR" ]]; then
        log_warn "Aliases directory not found: $ALIASES_DIR — skipping."
        return
    fi

    local updated=0
    local skipped=0

    while IFS= read -r -d '' alias_file; do
        if update_single_alias_file "$alias_file"; then
            updated=$((updated + 1))
        else
            skipped=$((skipped + 1))
        fi
    done < <(find "$ALIASES_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    log_info "Alias update complete: $updated updated, $skipped skipped."

    # Sync updated aliases to NFS
    if $DRY_RUN; then
        log_dry "Would rsync aliases: $ALIASES_DIR/ -> $NFS_ALIASES/"
    else
        mkdir -p "$NFS_ALIASES"
        rsync -a "${ALIASES_DIR}/" "${NFS_ALIASES}/"
    fi
}

# ---------------------------------------------------------------------------
# Update config.json for cluster mode
# ---------------------------------------------------------------------------

update_config_for_cluster() {
    log_step "Updating config.json for cluster mode"

    if $DRY_RUN; then
        log_dry "Would set storage_mode=postgres and postgres_dsn in $CONFIG_JSON"
        return
    fi

    PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" - <<PYEOF
import json, os, tempfile

config_file = '${CONFIG_JSON}'
postgres_url = '${POSTGRES_URL}'
config_dir = os.path.dirname(config_file)

with open(config_file, 'r') as f:
    config = json.load(f)

if config.get('storage_mode') == 'postgres' and config.get('postgres_dsn') == postgres_url:
    print('config.json already in postgres mode — no changes needed.')
else:
    config['storage_mode'] = 'postgres'
    config['postgres_dsn'] = postgres_url

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=config_dir, prefix='.config_migrate_', suffix='.tmp'
    )
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(config, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_file)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise RuntimeError(f'Failed to update config: {e}') from e
    print('config.json updated: storage_mode=postgres')
PYEOF

    log_info "Config update complete."
}

# ---------------------------------------------------------------------------
# Health validation
# ---------------------------------------------------------------------------

validate_health() {
    log_step "Validating server health"

    if $DRY_RUN; then
        log_dry "Would GET http://localhost:8000/health and verify HTTP 200"
        return
    fi

    local max_attempts=10
    local attempt=0

    log_info "Waiting for server health at http://localhost:8000/health..."
    while [[ $attempt -lt $max_attempts ]]; do
        attempt=$((attempt + 1))
        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000/health" 2>/dev/null || echo "000")
        if [[ "$http_code" == "200" ]]; then
            log_info "Server health check passed (HTTP 200) on attempt $attempt."
            return 0
        fi
        log_info "Attempt $attempt/$max_attempts: HTTP $http_code — waiting 2s..."
        sleep 2
    done

    log_error "Server did not become healthy after $max_attempts attempts."
    log_error "Check logs: journalctl -u cidx-server -n 100"
    exit 1
}

# ---------------------------------------------------------------------------
# Migration report
# ---------------------------------------------------------------------------

print_report() {
    log_step "Migration Report"
    echo ""
    echo "  Source (standalone):"
    echo "    SQLite DB:    $SQLITE_DB"
    echo "    Groups DB:    $GROUPS_DB"
    echo "    Golden repos: $GOLDEN_REPOS_DIR"
    echo ""
    echo "  Target (cluster):"
    echo "    PostgreSQL:   $POSTGRES_URL"
    echo "    NFS golden:   $NFS_GOLDEN_REPOS"
    echo "    NFS versioned:$NFS_VERSIONED"
    echo ""
    if $DRY_RUN; then
        echo "  [DRY RUN] No changes were made."
    elif $ROLLBACK; then
        echo "  [ROLLBACK] Server restored to SQLite standalone mode."
    else
        echo "  [SUCCESS] Server is now running in cluster (PostgreSQL) mode."
        echo "  Backups at: $BACKUP_DIR"
        echo ""
        echo "  Next steps:"
        echo "    - Verify golden repos are accessible via NFS on other nodes"
        echo "    - Run cluster-join.sh on additional nodes"
        echo "    - Monitor: journalctl -u cidx-server -f"
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    echo ""
    echo "CIDX Cluster Migration Script"
    echo "=============================="
    $DRY_RUN && echo "[DRY RUN MODE] No changes will be made." && echo ""

    if $ROLLBACK; then
        stop_server
        do_rollback
        print_report
        exit 0
    fi

    validate_prerequisites
    stop_server
    backup_databases
    run_schema_migrations
    migrate_data_to_postgres
    copy_golden_repos_to_nfs
    copy_versioned_to_nfs
    update_alias_json_files
    update_config_for_cluster
    restart_server
    validate_health
    print_report
}

main "$@"
