#!/bin/bash
# cluster-join.sh — Configure a CIDX server to join an existing cluster
#
# Story #425
#
# Usage:
#   ./scripts/cluster-join.sh \
#     --postgres-url "postgresql://user:pass@host/db" \
#     --ontap-endpoint "100.99.60.248" \
#     --ontap-export "/" \
#     --ontap-mount "/mnt/fsx" \
#     --ontap-admin-user "fsxadmin" \
#     --ontap-admin-password "password" \
#     --ontap-svm "sebaV2" \
#     --ontap-parent-volume "seba_vol1" \
#     --nfs-data-lif "100.99.60.204"
#
# Optional flags:
#   --dry-run           Show what would be done without doing it
#   --node-id ID        Override the generated node_id
#
# Idempotent: safe to re-run on the same server.
# If a node_id already exists in config.json it is reused (re-join).
# Existing config.json is backed up before any modification.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults / globals
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CIDX_SERVER_DIR="${HOME}/.cidx-server"
CONFIG_FILE="${CIDX_SERVER_DIR}/config.json"
DRY_RUN=false

PG_URL=""
ONTAP_ENDPOINT=""
ONTAP_EXPORT=""
ONTAP_MOUNT=""
ONTAP_ADMIN_USER=""
ONTAP_ADMIN_PASSWORD=""
ONTAP_SVM=""
ONTAP_PARENT_VOLUME=""
NFS_DATA_LIF=""
OVERRIDE_NODE_ID=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo "[cluster-join] $*"; }
info() { echo "[cluster-join] INFO  $*"; }
warn() { echo "[cluster-join] WARN  $*" >&2; }
die()  { echo "[cluster-join] ERROR $*" >&2; exit 1; }

dry_run_or_exec() {
    # Execute a command, or just print it when --dry-run is active.
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

require_command() {
    local cmd="$1"
    command -v "${cmd}" >/dev/null 2>&1 || die "Required command not found: ${cmd}"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --postgres-url)          PG_URL="$2";              shift 2 ;;
            --ontap-endpoint)        ONTAP_ENDPOINT="$2";      shift 2 ;;
            --ontap-export)          ONTAP_EXPORT="$2";        shift 2 ;;
            --ontap-mount)           ONTAP_MOUNT="$2";         shift 2 ;;
            --ontap-admin-user)      ONTAP_ADMIN_USER="$2";    shift 2 ;;
            --ontap-admin-password)  ONTAP_ADMIN_PASSWORD="$2"; shift 2 ;;
            --ontap-svm)             ONTAP_SVM="$2";           shift 2 ;;
            --ontap-parent-volume)   ONTAP_PARENT_VOLUME="$2"; shift 2 ;;
            --nfs-data-lif)          NFS_DATA_LIF="$2";        shift 2 ;;
            --node-id)               OVERRIDE_NODE_ID="$2";    shift 2 ;;
            --dry-run)               DRY_RUN=true;             shift   ;;
            -h|--help)               usage; exit 0 ;;
            *) die "Unknown argument: $1" ;;
        esac
    done
}

usage() {
    cat <<'EOF'
Usage: cluster-join.sh [OPTIONS]

Required:
  --postgres-url URL          PostgreSQL connection string
                              e.g. "postgresql://user:pass@host/db"
  --ontap-endpoint IP         ONTAP management endpoint IP
  --ontap-export PATH         NFS export path on the ONTAP volume (e.g. "/")
  --ontap-mount PATH          Local mount point (e.g. "/mnt/fsx")
  --ontap-admin-user USER     ONTAP admin username (e.g. "fsxadmin")
  --ontap-svm NAME            ONTAP SVM name (e.g. "sebaV2")
  --ontap-parent-volume VOL   ONTAP parent volume name (e.g. "seba_vol1")
  --nfs-data-lif IP           NFS data LIF IP address

Optional:
  --ontap-admin-password PASS  ONTAP admin password (prompted if omitted)
  --node-id ID                 Override auto-generated node_id (for re-join)
  --dry-run                    Show what would be done without doing it

EOF
}

validate_required_args() {
    local missing=()
    [[ -z "${PG_URL}" ]]              && missing+=("--postgres-url")
    [[ -z "${ONTAP_ENDPOINT}" ]]      && missing+=("--ontap-endpoint")
    [[ -z "${ONTAP_EXPORT}" ]]        && missing+=("--ontap-export")
    [[ -z "${ONTAP_MOUNT}" ]]         && missing+=("--ontap-mount")
    [[ -z "${ONTAP_ADMIN_USER}" ]]    && missing+=("--ontap-admin-user")
    [[ -z "${ONTAP_SVM}" ]]           && missing+=("--ontap-svm")
    [[ -z "${ONTAP_PARENT_VOLUME}" ]] && missing+=("--ontap-parent-volume")
    [[ -z "${NFS_DATA_LIF}" ]]        && missing+=("--nfs-data-lif")

    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required arguments: ${missing[*]}"
    fi
}

# ---------------------------------------------------------------------------
# Step 1: Prompt for password if not provided
# ---------------------------------------------------------------------------

maybe_prompt_password() {
    if [[ -z "${ONTAP_ADMIN_PASSWORD}" ]]; then
        if [[ "${DRY_RUN}" == "true" ]]; then
            ONTAP_ADMIN_PASSWORD="<will-be-prompted>"
            return
        fi
        read -rsp "Enter ONTAP admin password for ${ONTAP_ADMIN_USER}@${ONTAP_ENDPOINT}: " ONTAP_ADMIN_PASSWORD
        echo
    fi
}

# ---------------------------------------------------------------------------
# Step 2: Check prerequisites
# ---------------------------------------------------------------------------

check_prerequisites() {
    info "Checking prerequisites..."

    require_command python3
    require_command jq

    # Detect package manager and check nfs-utils / nfs-common
    if command -v rpm >/dev/null 2>&1; then
        # RHEL / Rocky / CentOS
        if ! rpm -q nfs-utils >/dev/null 2>&1; then
            warn "nfs-utils not installed. Attempting to install via yum..."
            dry_run_or_exec sudo yum install -y nfs-utils
        fi
    elif command -v dpkg >/dev/null 2>&1; then
        # Debian / Ubuntu
        if ! dpkg -s nfs-common >/dev/null 2>&1; then
            warn "nfs-common not installed. Attempting to install via apt..."
            dry_run_or_exec sudo apt-get install -y nfs-common
        fi
    else
        warn "Could not detect package manager. Ensure nfs-utils/nfs-common is installed."
    fi

    # Network reachability check for PostgreSQL host
    local pg_host
    pg_host="$(echo "${PG_URL}" | sed -E 's|.*@([^/:]+).*|\1|')"
    if [[ -n "${pg_host}" ]]; then
        info "Checking network reachability to PostgreSQL host: ${pg_host}"
        if ! ping -c 1 -W 3 "${pg_host}" >/dev/null 2>&1; then
            warn "Cannot ping PostgreSQL host ${pg_host}. Connectivity may still work via DNS/routing."
        fi
    fi

    # Network reachability check for ONTAP
    info "Checking network reachability to ONTAP endpoint: ${ONTAP_ENDPOINT}"
    if ! ping -c 1 -W 3 "${ONTAP_ENDPOINT}" >/dev/null 2>&1; then
        warn "Cannot ping ONTAP endpoint ${ONTAP_ENDPOINT}. Connectivity may still work."
    fi

    info "Prerequisite check complete."
}

# ---------------------------------------------------------------------------
# Step 3: Generate or reuse node_id
# ---------------------------------------------------------------------------

resolve_node_id() {
    if [[ -n "${OVERRIDE_NODE_ID}" ]]; then
        NODE_ID="${OVERRIDE_NODE_ID}"
        info "Using provided node_id: ${NODE_ID}"
        return
    fi

    # Check for existing node_id in config.json (re-join idempotency)
    if [[ -f "${CONFIG_FILE}" ]]; then
        local existing_node_id
        existing_node_id="$(jq -r '.cluster.node_id // empty' "${CONFIG_FILE}" 2>/dev/null || true)"
        if [[ -n "${existing_node_id}" ]]; then
            NODE_ID="${existing_node_id}"
            info "Re-joining cluster with existing node_id: ${NODE_ID}"
            return
        fi
    fi

    # Generate new node_id: hostname + short UUID suffix
    local hostname short_uuid
    hostname="$(hostname -s 2>/dev/null || hostname)"
    short_uuid="$(cat /proc/sys/kernel/random/uuid 2>/dev/null | cut -d- -f1 || python3 -c "import uuid; print(str(uuid.uuid4()).split('-')[0])")"
    NODE_ID="${hostname}-${short_uuid}"
    info "Generated new node_id: ${NODE_ID}"
}

# ---------------------------------------------------------------------------
# Step 4: Set up NFS mount
# ---------------------------------------------------------------------------

setup_nfs_mount() {
    info "Setting up NFS mount: ${NFS_DATA_LIF}:${ONTAP_EXPORT} -> ${ONTAP_MOUNT}"

    # Create mount point if it doesn't exist
    if [[ ! -d "${ONTAP_MOUNT}" ]]; then
        dry_run_or_exec sudo mkdir -p "${ONTAP_MOUNT}"
    fi

    # Check if already mounted
    if mountpoint -q "${ONTAP_MOUNT}" 2>/dev/null; then
        info "NFS already mounted at ${ONTAP_MOUNT}. Skipping mount."
    else
        dry_run_or_exec sudo mount -t nfs \
            -o "rw,hard,intr,rsize=65536,wsize=65536,timeo=600" \
            "${NFS_DATA_LIF}:${ONTAP_EXPORT}" \
            "${ONTAP_MOUNT}"
    fi

    # Add fstab entry (idempotent)
    local fstab_entry="${NFS_DATA_LIF}:${ONTAP_EXPORT} ${ONTAP_MOUNT} nfs rw,hard,intr,rsize=65536,wsize=65536,timeo=600 0 0"
    if grep -qF "${NFS_DATA_LIF}:${ONTAP_EXPORT}" /etc/fstab 2>/dev/null; then
        info "fstab entry already exists for ${NFS_DATA_LIF}:${ONTAP_EXPORT}. Skipping."
    else
        info "Adding fstab entry for persistent mount..."
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] echo '${fstab_entry}' | sudo tee -a /etc/fstab"
        else
            echo "${fstab_entry}" | sudo tee -a /etc/fstab >/dev/null
        fi
    fi
}

# ---------------------------------------------------------------------------
# Step 5: Validate NFS mount
# ---------------------------------------------------------------------------

validate_nfs_mount() {
    info "Validating NFS mount at ${ONTAP_MOUNT}..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] mountpoint -q ${ONTAP_MOUNT} && ls ${ONTAP_MOUNT}"
        return
    fi

    if ! mountpoint -q "${ONTAP_MOUNT}"; then
        die "NFS mount at ${ONTAP_MOUNT} is not mounted. Setup failed."
    fi

    # Write a test file to verify read/write access
    local test_file="${ONTAP_MOUNT}/.cidx-join-test-$$"
    if ! touch "${test_file}" 2>/dev/null; then
        die "Cannot write to NFS mount at ${ONTAP_MOUNT}. Check permissions."
    fi
    rm -f "${test_file}"

    info "NFS mount validated successfully."
}

# ---------------------------------------------------------------------------
# Step 6: Test PostgreSQL connectivity
# ---------------------------------------------------------------------------

test_postgres_connectivity() {
    info "Testing PostgreSQL connectivity..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] python3 -c \"import psycopg; psycopg.connect('${PG_URL}')\""
        return
    fi

    python3 - <<PYEOF
import sys
try:
    import psycopg
except ImportError:
    print("[cluster-join] ERROR: psycopg (v3) not installed. Run: pip install psycopg", file=sys.stderr)
    sys.exit(1)
try:
    conn = psycopg.connect("${PG_URL}", connect_timeout=10)
    conn.close()
    print("[cluster-join] INFO  PostgreSQL connectivity: OK")
except Exception as e:
    print(f"[cluster-join] ERROR: Cannot connect to PostgreSQL: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# Step 7: Run database migrations
# ---------------------------------------------------------------------------

run_migrations() {
    info "Running database migrations..."

    local migration_cmd=(
        python3 -m code_indexer.server.storage.postgres.migrations.runner
        --connection-string "${PG_URL}"
    )

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] PYTHONPATH=${PROJECT_ROOT}/src ${migration_cmd[*]}"
        return
    fi

    PYTHONPATH="${PROJECT_ROOT}/src" "${migration_cmd[@]}"
    info "Database migrations complete."
}

# ---------------------------------------------------------------------------
# Step 8: Update config.json
# ---------------------------------------------------------------------------

backup_config() {
    if [[ -f "${CONFIG_FILE}" ]]; then
        local backup_path="${CONFIG_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
        info "Backing up existing config to ${backup_path}"
        dry_run_or_exec cp "${CONFIG_FILE}" "${backup_path}"
    fi
}

write_config() {
    info "Writing cluster configuration to ${CONFIG_FILE}..."

    dry_run_or_exec mkdir -p "${CIDX_SERVER_DIR}"

    if [[ "${DRY_RUN}" == "true" ]]; then
        cat <<EOF
  [dry-run] Config to be written to ${CONFIG_FILE}:
  {
    "storage_mode": "postgres",
    "postgres_dsn": "${PG_URL}",
    "ontap": {
      "endpoint": "${ONTAP_ENDPOINT}",
      "svm_name": "${ONTAP_SVM}",
      "parent_volume": "${ONTAP_PARENT_VOLUME}",
      "mount_point": "${ONTAP_MOUNT}",
      "admin_user": "${ONTAP_ADMIN_USER}",
      "admin_password": "<redacted>",
      "nfs_data_lif": "${NFS_DATA_LIF}",
      "nfs_export": "${ONTAP_EXPORT}"
    },
    "cluster": {
      "node_id": "${NODE_ID}"
    }
  }
EOF
        return
    fi

    # Read existing config or start with empty object
    local existing_config="{}"
    if [[ -f "${CONFIG_FILE}" ]]; then
        existing_config="$(cat "${CONFIG_FILE}")"
    fi

    # Merge cluster configuration into existing config using jq
    local new_config
    new_config="$(echo "${existing_config}" | jq \
        --arg storage_mode "postgres" \
        --arg pg_dsn "${PG_URL}" \
        --arg ontap_endpoint "${ONTAP_ENDPOINT}" \
        --arg ontap_svm "${ONTAP_SVM}" \
        --arg ontap_parent_volume "${ONTAP_PARENT_VOLUME}" \
        --arg ontap_mount "${ONTAP_MOUNT}" \
        --arg ontap_admin_user "${ONTAP_ADMIN_USER}" \
        --arg ontap_admin_password "${ONTAP_ADMIN_PASSWORD}" \
        --arg nfs_data_lif "${NFS_DATA_LIF}" \
        --arg nfs_export "${ONTAP_EXPORT}" \
        --arg node_id "${NODE_ID}" \
        '. + {
            storage_mode: $storage_mode,
            postgres_dsn: $pg_dsn,
            ontap: {
                endpoint: $ontap_endpoint,
                svm_name: $ontap_svm,
                parent_volume: $ontap_parent_volume,
                mount_point: $ontap_mount,
                admin_user: $ontap_admin_user,
                admin_password: $ontap_admin_password,
                nfs_data_lif: $nfs_data_lif,
                nfs_export: $nfs_export
            },
            cluster: {
                node_id: $node_id
            }
        }'
    )"

    echo "${new_config}" > "${CONFIG_FILE}"
    chmod 600 "${CONFIG_FILE}"
    info "Config written to ${CONFIG_FILE}"
}

# ---------------------------------------------------------------------------
# Step 9: Print summary
# ---------------------------------------------------------------------------

print_summary() {
    cat <<EOF

============================================================
  Cluster Join Summary
============================================================

  Node ID        : ${NODE_ID}
  PostgreSQL     : ${PG_URL%@*}@<host>  (credentials hidden)
  ONTAP Endpoint : ${ONTAP_ENDPOINT}
  NFS Mount      : ${NFS_DATA_LIF}:${ONTAP_EXPORT} -> ${ONTAP_MOUNT}
  Config File    : ${CONFIG_FILE}
  Dry Run        : ${DRY_RUN}

Next steps:
  1. Restart the CIDX server:
       sudo systemctl restart cidx-server
  2. Verify cluster health:
       curl -s http://localhost:8000/health | jq .
  3. Check that this node appears in cluster nodes:
       curl -s -H "Authorization: Bearer <token>" http://localhost:8000/api/cluster/nodes

============================================================
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"
    validate_required_args
    maybe_prompt_password

    if [[ "${DRY_RUN}" == "true" ]]; then
        warn "DRY RUN MODE — no changes will be made"
    fi

    check_prerequisites
    resolve_node_id
    setup_nfs_mount
    validate_nfs_mount
    test_postgres_connectivity
    run_migrations
    backup_config
    write_config
    print_summary

    if [[ "${DRY_RUN}" == "true" ]]; then
        info "Dry run complete. No changes were made."
    else
        info "Cluster join complete. Restart cidx-server to apply."
    fi
}

main "$@"
