#!/bin/bash
# cluster-migrate-lib.sh — Shared helper functions for cluster-migrate.sh
#
# Story #426: Cluster Migrate Script (Seed Cluster from Single Server)
#
# Source this file from cluster-migrate.sh. Do not execute directly.

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

log_info()  { echo "[INFO]  $*"; }
log_warn()  { echo "[WARN]  $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }
log_step()  { echo ""; echo "=== $* ==="; }
log_dry()   { echo "[DRY]   $*"; }

# ---------------------------------------------------------------------------
# Dry-run aware execution
# ---------------------------------------------------------------------------

# Run a command, or print it in dry-run mode
run_cmd() {
    if $DRY_RUN; then
        log_dry "$*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Prerequisite validation helpers
# ---------------------------------------------------------------------------

# Global error counter used by validate_* sub-functions.
# Each sub-function increments _PREREQ_ERRORS directly (avoids subshell capture issues).
_PREREQ_ERRORS=0

_prereq_error() {
    log_error "$1"
    _PREREQ_ERRORS=$((_PREREQ_ERRORS + 1))
}

validate_required_args() {
    [[ -z "$POSTGRES_URL" ]]  && _prereq_error "--postgres-url is required"
    [[ -z "$ONTAP_MOUNT" ]]   && _prereq_error "--ontap-mount is required"
}

validate_local_paths() {
    [[ ! -d "$CIDX_DATA_DIR" ]]        && _prereq_error "CIDX data directory does not exist: $CIDX_DATA_DIR"
    [[ ! -f "$SQLITE_DB" ]]            && _prereq_error "SQLite database not found: $SQLITE_DB"
    [[ ! -f "$GROUPS_DB" ]]            && _prereq_error "Groups database not found: $GROUPS_DB"
    [[ ! -f "$CONFIG_JSON" ]]          && _prereq_error "Server config not found: $CONFIG_JSON"
    if [[ ! -d "${PROJECT_ROOT}/src" ]]; then
        _prereq_error "CIDX source directory not found: ${PROJECT_ROOT}/src (use --src-dir)"
    fi
}

validate_nfs_mount() {
    if [[ ! -d "$ONTAP_MOUNT" ]]; then
        _prereq_error "NFS mount point does not exist or is not mounted: $ONTAP_MOUNT"
        return
    fi
    local test_file="${ONTAP_MOUNT}/.cidx-migrate-test-$$"
    if ! touch "$test_file" 2>/dev/null; then
        _prereq_error "NFS mount is not writable: $ONTAP_MOUNT"
    else
        rm -f "$test_file"
        log_info "NFS mount is accessible and writable: $ONTAP_MOUNT"
    fi
}

validate_python_bin() {
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        _prereq_error "Python binary not found: $PYTHON_BIN"
    fi
}

validate_postgres_connection() {
    if $DRY_RUN; then
        log_dry "Would check PostgreSQL connectivity to: $POSTGRES_URL"
        return
    fi

    log_info "Checking PostgreSQL connectivity..."
    if ! PYTHONPATH="$PYTHONPATH" "$PYTHON_BIN" -c "
import sys
try:
    import psycopg
    conn = psycopg.connect('${POSTGRES_URL}')
    conn.close()
    print('PostgreSQL connection: OK')
except ImportError:
    print('ERROR: psycopg not installed. Run: pip install psycopg', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'ERROR: Cannot connect to PostgreSQL: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
        _prereq_error "PostgreSQL connectivity check failed"
    fi
}

validate_prerequisites() {
    log_step "Validating prerequisites"

    _PREREQ_ERRORS=0
    validate_required_args
    validate_local_paths
    validate_nfs_mount
    validate_python_bin
    validate_postgres_connection

    if [[ $_PREREQ_ERRORS -gt 0 ]]; then
        log_error "Prerequisite validation failed with $_PREREQ_ERRORS error(s). Aborting."
        exit 1
    fi

    log_info "All prerequisites satisfied."
}

# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

do_rollback() {
    log_step "Rolling back from backup at $BACKUP_DIR"

    if [[ ! -d "$BACKUP_DIR" ]]; then
        log_error "No backup directory found at $BACKUP_DIR"
        log_error "Cannot roll back — no previous migration backup exists."
        exit 1
    fi

    local backed_up_db="${BACKUP_DIR}/cidx_server.db.bak"
    local backed_up_groups="${BACKUP_DIR}/groups.db.bak"
    local backed_up_config="${BACKUP_DIR}/config.json.bak"

    if [[ ! -f "$backed_up_db" ]] || [[ ! -f "$backed_up_groups" ]] || [[ ! -f "$backed_up_config" ]]; then
        log_error "Backup is incomplete. Found:"
        ls -la "$BACKUP_DIR/" >&2
        log_error "Rollback aborted to avoid partial state."
        exit 1
    fi

    log_info "Stopping CIDX server for rollback..."
    systemctl stop cidx-server || true

    log_info "Restoring config.json..."
    run_cmd cp "$backed_up_config" "$CONFIG_JSON"

    log_info "Restoring SQLite databases..."
    run_cmd cp "$backed_up_db" "$SQLITE_DB"
    run_cmd cp "$backed_up_groups" "$GROUPS_DB"

    # Restore alias JSONs if backed up
    if [[ -d "${BACKUP_DIR}/aliases" ]] && [[ -d "$ALIASES_DIR" ]]; then
        log_info "Restoring alias JSON files..."
        run_cmd rsync -a "${BACKUP_DIR}/aliases/" "${ALIASES_DIR}/"
    fi

    log_info "Restarting CIDX server after rollback..."
    run_cmd systemctl start cidx-server

    log_info "Rollback complete. Server is running in standalone SQLite mode."
    log_warn "Files copied to NFS (${ONTAP_MOUNT}) were NOT removed."
}
