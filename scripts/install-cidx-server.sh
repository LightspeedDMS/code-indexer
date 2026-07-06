#!/bin/bash
# install-cidx-server.sh — Install CIDX server on a fresh machine, optionally
# configuring PostgreSQL cluster mode + a CoW-daemon or local clone backend.
#
# Idempotent: safe to re-run. Handles Rocky Linux / RHEL / Ubuntu.
#
# Usage (standalone, unchanged from earlier versions):
#   ./install-cidx-server.sh [--branch BRANCH] [--voyage-key KEY] [--port PORT]
#
# Usage (cluster mode — activated when BOTH --node-id and --postgres-dsn are
# provided; on re-join, --postgres-dsn alone is enough if config.json already
# stores cluster.node_id):
#   ./install-cidx-server.sh \
#     --node-id staging --postgres-dsn "postgresql://user:pass@host/db" \
#     --clone-backend cow-daemon \
#     --cow-daemon-url "http://cow-host:8081" --cow-daemon-api-key KEY \
#     --nfs-server cow-host --nfs-export /srv/cow-storage
#
# --dry-run prints every action this script WOULD take (package installs,
# git clone/pull, pip install, NFS mount, PG migrations, systemd, firewall,
# service restart) without doing any of it — safe to run with no network or
# sudo access. This is also how scripts/install-cidx-server-test.sh exercises
# the script end to end.
#
# What it does (standalone path, same as before):
#   1. Installs system packages (git, nfs-utils, gcc, jq, etc.)
#   2. Clones code-indexer repo (or pulls if already cloned); optionally
#      authenticates against a private repo via --repo-token
#   3. Installs Python dependencies
#   4. Creates ~/.cidx-server data directory + default sqlite config.json
#   5. Creates and enables a systemd service
#   6. Starts the server and verifies GET /docs returns 200
#
# Cluster path additionally (only when --node-id + --postgres-dsn given):
#   - Mounts the CoW-daemon shared NFS export (idempotent fstab entry)
#   - Verifies PostgreSQL connectivity and runs migrations
#   - Merges (never blindly overwrites) config.json to storage_mode=postgres,
#     cluster.node_id, clone_backend, cow_daemon.* — backing up the old file
#   - Opens the firewalld port for this node
#   - Prints the cluster_nodes rows from PostgreSQL after startup

set -euo pipefail

# Refuse to run as root — sudo is used internally for specific commands
if [[ "$EUID" -eq 0 ]]; then
    echo "ERROR: Do not run this script as root or with 'sudo bash'."
    echo "Run as your regular user: bash $0 $@"
    echo "The script will use sudo internally where needed."
    exit 1
fi

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

REPO_URL="https://github.com/LightspeedDMS/code-indexer.git"
BRANCH="master"
INSTALL_DIR="${HOME}/code-indexer"
DATA_DIR="${HOME}/.cidx-server"
CONFIG_FILE="${DATA_DIR}/config.json"
PORT=8000
VOYAGE_KEY=""
PYTHON="python3"
REPO_TOKEN=""

NODE_ID=""
POSTGRES_DSN=""
CLONE_BACKEND="local"
COW_DAEMON_URL=""
COW_DAEMON_API_KEY=""
NFS_SERVER=""
NFS_EXPORT=""
NFS_MOUNT="/mnt/cow-storage"
COW_LOCAL_BIND=false
WORKERS=1
AUTO_UPDATE_BRANCH=""

DRY_RUN=false
CLUSTER_MODE=false
IS_FRESH_INSTALL=0

# ---------------------------------------------------------------------------
# Logging / helpers
# ---------------------------------------------------------------------------

log()  { echo "[install-cidx-server] $*"; }
info() { echo "[install-cidx-server] INFO  $*"; }
warn() { echo "[install-cidx-server] WARN  $*" >&2; }
die()  { echo "[install-cidx-server] ERROR $*" >&2; exit 1; }

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

# Redacts the credentials portion (user:pass@) of a DSN/URI, keeping the
# scheme, host, and database name visible for troubleshooting. Never put
# the return value in the real config.json — it is for console/log/dry-run
# display only; the real value must never appear in logs.
mask_secret_uri() {
    local uri="$1"
    if [[ -z "${uri}" ]]; then
        echo ""
        return 0
    fi
    echo "${uri}" | sed -E 's#(://)[^/@]*@#\1***:***@#'
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: install-cidx-server.sh [OPTIONS]

Standalone (unchanged):
  --branch BRANCH            Git branch to install (default: master)
  --voyage-key KEY            VoyageAI API key (written into the systemd unit)
  --port PORT                  Server port (default: 8000)
  --repo-url URL              Repo to clone (default: LightspeedDMS/code-indexer)
  --install-dir PATH          Where to clone the repo (default: ~/code-indexer)
  --repo-token TOKEN           GitHub token for cloning a private repo. Stored
                                in ~/.git-credentials (chmod 600) via
                                credential.helper=store; never embedded in
                                the .git/config remote URL.

Cluster mode (activated when --node-id AND --postgres-dsn are both given;
on re-join, --postgres-dsn alone suffices if config.json already has
cluster.node_id):
  --node-id ID                 cluster.node_id for this node
  --postgres-dsn DSN           libpq DSN, e.g. postgresql://user:pass@host/db
  --clone-backend NAME         local|cow-daemon (default: local)
  --cow-daemon-url URL         CoW daemon REST base URL (required if cow-daemon)
  --cow-daemon-api-key KEY     CoW daemon bearer token (required if cow-daemon)
  --nfs-server IP              NFS server for the shared CoW mount (not
                                required when --cow-local-bind is set)
  --nfs-export PATH            NFS export path (also doubles as the local
                                source directory when --cow-local-bind is set)
  --nfs-mount PATH              Local mount point (default: /mnt/cow-storage)
  --cow-local-bind              This node is co-located with the CoW daemon
                                on the same host: bind-mount --nfs-export onto
                                --nfs-mount (mount --bind) instead of an NFS
                                mount. See docs/cow-storage-setup.md "Bind
                                Mount on the Daemon Host".
  --workers N                   uvicorn workers (default: 1)
  --auto-update-branch BRANCH   CIDX_AUTO_UPDATE_BRANCH env var (default: --branch)

Other:
  --dry-run                    Print what would happen; make no changes
  --help                        Show this help

EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --branch) BRANCH="$2"; shift 2 ;;
            --voyage-key) VOYAGE_KEY="$2"; shift 2 ;;
            --port) PORT="$2"; shift 2 ;;
            --repo-url) REPO_URL="$2"; shift 2 ;;
            --install-dir) INSTALL_DIR="$2"; shift 2 ;;
            --repo-token) REPO_TOKEN="$2"; shift 2 ;;
            --node-id) NODE_ID="$2"; shift 2 ;;
            --postgres-dsn) POSTGRES_DSN="$2"; shift 2 ;;
            --clone-backend) CLONE_BACKEND="$2"; shift 2 ;;
            --cow-daemon-url) COW_DAEMON_URL="$2"; shift 2 ;;
            --cow-daemon-api-key) COW_DAEMON_API_KEY="$2"; shift 2 ;;
            --nfs-server) NFS_SERVER="$2"; shift 2 ;;
            --nfs-export) NFS_EXPORT="$2"; shift 2 ;;
            --nfs-mount) NFS_MOUNT="$2"; shift 2 ;;
            --cow-local-bind) COW_LOCAL_BIND=true; shift ;;
            --workers) WORKERS="$2"; shift 2 ;;
            --auto-update-branch) AUTO_UPDATE_BRANCH="$2"; shift 2 ;;
            --dry-run) DRY_RUN=true; shift ;;
            --help) usage; exit 0 ;;
            *) echo "Unknown argument: $1"; exit 1 ;;
        esac
    done
}

validate_args() {
    if [[ "${CLONE_BACKEND}" != "local" && "${CLONE_BACKEND}" != "cow-daemon" ]]; then
        die "Invalid --clone-backend '${CLONE_BACKEND}'. Must be 'local' or 'cow-daemon'."
    fi
    if [[ "${CLONE_BACKEND}" == "cow-daemon" ]]; then
        if [[ -z "${COW_DAEMON_URL}" ]]; then
            die "--clone-backend cow-daemon requires --cow-daemon-url"
        fi
        if [[ -z "${COW_DAEMON_API_KEY}" ]]; then
            die "--clone-backend cow-daemon requires --cow-daemon-api-key"
        fi
        if [[ "${COW_LOCAL_BIND}" != "true" && -z "${NFS_SERVER}" ]]; then
            die "--clone-backend cow-daemon requires --nfs-server (unless --cow-local-bind is set)"
        fi
        if [[ -z "${NFS_EXPORT}" ]]; then
            die "--clone-backend cow-daemon requires --nfs-export"
        fi
    fi
}

resolve_defaults() {
    if [[ -z "${AUTO_UPDATE_BRANCH}" ]]; then
        AUTO_UPDATE_BRANCH="${BRANCH}"
    fi
}

# ---------------------------------------------------------------------------
# Cluster-mode activation (idempotent node_id reuse on re-join)
# ---------------------------------------------------------------------------

determine_cluster_mode() {
    CLUSTER_MODE=false
    if [[ -n "${POSTGRES_DSN}" ]]; then
        if [[ -z "${NODE_ID}" ]]; then
            if [[ -f "${CONFIG_FILE}" ]] && command -v jq >/dev/null 2>&1; then
                local existing
                existing="$(jq -r '.cluster.node_id // empty' "${CONFIG_FILE}" 2>/dev/null || true)"
                if [[ -n "${existing}" ]]; then
                    NODE_ID="${existing}"
                    info "Re-joining cluster with existing node_id: ${NODE_ID}"
                fi
            fi
        fi
        if [[ -z "${NODE_ID}" ]]; then
            die "Cluster mode requires --node-id on first run (no existing config.json cluster.node_id found to reuse). Provide --node-id."
        fi
        CLUSTER_MODE=true
    else
        if [[ -n "${NODE_ID}" ]]; then
            warn "--node-id given without --postgres-dsn; ignoring (standalone sqlite mode)."
        fi
    fi
}

# ---------------------------------------------------------------------------
# Step: package manager detection
# ---------------------------------------------------------------------------

detect_pkg_mgr() {
    if command -v dnf &>/dev/null; then
        PKG_MGR="dnf"
        PKG_INSTALL="sudo dnf install -y"
    elif command -v yum &>/dev/null; then
        PKG_MGR="yum"
        PKG_INSTALL="sudo yum install -y"
    elif command -v apt-get &>/dev/null; then
        PKG_MGR="apt"
        PKG_INSTALL="sudo apt-get install -y"
    else
        die "No supported package manager found (dnf/yum/apt)"
    fi
}

# ---------------------------------------------------------------------------
# Step: system packages
# ---------------------------------------------------------------------------

install_system_packages() {
    info "--- System dependencies ---"
    local packages

    if [[ "${PKG_MGR}" == "apt" ]]; then
        packages="git nfs-common gcc g++ python3-pip python3-dev libpq-dev jq"
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] sudo apt-get update -qq && ${PKG_INSTALL} ${packages}"
        else
            sudo apt-get update -qq
            $PKG_INSTALL $packages
        fi
    else
        packages="git nfs-utils gcc gcc-c++ python3-pip python3-devel jq"
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] sudo dnf install -y epel-release; sudo dnf config-manager --set-enabled crb"
            echo "  [dry-run] ${PKG_INSTALL} ${packages}"
        else
            sudo dnf install -y epel-release 2>/dev/null || true
            sudo dnf config-manager --set-enabled crb 2>/dev/null || true
            $PKG_INSTALL $packages
        fi
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] ${PYTHON} -m pip install --upgrade pip"
    else
        $PYTHON -m pip install --upgrade pip 2>/dev/null || true
    fi
    info "System packages step complete."
}

# ---------------------------------------------------------------------------
# Step: git auth for private repo (token stored in ~/.git-credentials only)
# ---------------------------------------------------------------------------

setup_git_auth() {
    if [[ -z "${REPO_TOKEN}" ]]; then
        return 0
    fi

    info "Configuring git credential storage for private repo access..."
    local git_creds_file="${HOME}/.git-credentials"
    local repo_host
    repo_host="$(echo "${REPO_URL}" | sed -E 's#https?://([^/]+)/.*#\1#')"

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] Write masked credential line to ${git_creds_file} for host ${repo_host} (chmod 600)"
        echo "  [dry-run] git config --global credential.helper store"
        return 0
    fi

    local cred_line="https://${REPO_TOKEN}@${repo_host}"
    touch "${git_creds_file}"
    chmod 600 "${git_creds_file}"

    if grep -qF "@${repo_host}" "${git_creds_file}" 2>/dev/null; then
        grep -vF "@${repo_host}" "${git_creds_file}" > "${git_creds_file}.tmp" || true
        mv "${git_creds_file}.tmp" "${git_creds_file}"
    fi
    echo "${cred_line}" >> "${git_creds_file}"
    chmod 600 "${git_creds_file}"

    git config --global credential.helper store
    info "Git credentials configured (${git_creds_file}, chmod 600). Remote URL stays token-free."
}

# ---------------------------------------------------------------------------
# Step: clone or update repository
# ---------------------------------------------------------------------------

clone_or_update_repo() {
    info "--- Clone/update repository ---"
    IS_FRESH_INSTALL=0

    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        info "Repository exists at ${INSTALL_DIR}, pulling latest..."
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] git -C ${INSTALL_DIR} fetch origin && checkout ${BRANCH} && pull origin ${BRANCH} && submodule update"
        else
            (cd "${INSTALL_DIR}" \
                && git fetch origin \
                && git checkout "${BRANCH}" \
                && git pull origin "${BRANCH}" \
                && git submodule update --init third_party/hnswlib)
        fi
    else
        IS_FRESH_INSTALL=1
        info "Cloning repository..."
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] git clone --branch ${BRANCH} ${REPO_URL} ${INSTALL_DIR} && git -C ${INSTALL_DIR} submodule update --init third_party/hnswlib"
        else
            git clone --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
            git -C "${INSTALL_DIR}" submodule update --init third_party/hnswlib
        fi
    fi
}

# ---------------------------------------------------------------------------
# Step: python dependencies
# ---------------------------------------------------------------------------

install_python_deps() {
    info "--- Python dependencies ---"
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] cd ${INSTALL_DIR} && ${PYTHON} -m pip install --break-system-packages -e ."
        echo "  [dry-run] ${PYTHON} -m pip install --break-system-packages \"psycopg[binary]\" psycopg-pool requests numpy"
        return 0
    fi

    cd "${INSTALL_DIR}"
    $PYTHON -m pip install --break-system-packages -e . 2>&1 | tail -5
    $PYTHON -m pip install --break-system-packages "psycopg[binary]" psycopg-pool requests numpy 2>&1 | tail -3

    $PYTHON -c "import code_indexer; print(f'code-indexer v{code_indexer.__version__} installed')"
    $PYTHON -c "import psycopg; print(f'psycopg v{psycopg.__version__} installed')"
    $PYTHON -c "import psycopg_pool; print('psycopg-pool installed')"
}

# ---------------------------------------------------------------------------
# Cluster step: fstab entry (idempotent, testable — fstab_file is injectable)
# ---------------------------------------------------------------------------

add_fstab_entry() {
    local mount_source="$1"
    local mount_point="$2"
    local fstab_file="${3:-/etc/fstab}"
    local entry="${mount_source} ${mount_point} nfs4 _netdev,soft,timeo=30,retrans=3 0 0"

    if grep -qF "${mount_source} ${mount_point}" "${fstab_file}" 2>/dev/null; then
        info "fstab entry already exists for ${mount_source} -> ${mount_point}. Skipping."
        return 0
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] echo '${entry}' >> ${fstab_file}"
        return 0
    fi

    if [[ "${fstab_file}" == "/etc/fstab" ]]; then
        echo "${entry}" | sudo tee -a "${fstab_file}" >/dev/null
    else
        echo "${entry}" >> "${fstab_file}"
    fi
    info "Added fstab entry: ${entry}"
}

# ---------------------------------------------------------------------------
# Cluster step: CoW-daemon NFS mount
# ---------------------------------------------------------------------------

setup_nfs_mount() {
    local mount_source="${NFS_SERVER}:${NFS_EXPORT}"
    info "--- CoW NFS mount: ${mount_source} -> ${NFS_MOUNT} ---"

    if [[ ! -d "${NFS_MOUNT}" ]]; then
        dry_run_or_exec sudo mkdir -p "${NFS_MOUNT}"
    fi

    if mountpoint -q "${NFS_MOUNT}" 2>/dev/null; then
        info "NFS already mounted at ${NFS_MOUNT}. Skipping mount."
    else
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] sudo mount -t nfs4 -o _netdev,soft,timeo=30,retrans=3 ${mount_source} ${NFS_MOUNT}"
        else
            if ! sudo mount -t nfs4 -o "_netdev,soft,timeo=30,retrans=3" "${mount_source}" "${NFS_MOUNT}"; then
                die "NFS mount command failed for ${mount_source} -> ${NFS_MOUNT}. Check connectivity/export and re-run."
            fi
        fi
    fi

    add_fstab_entry "${mount_source}" "${NFS_MOUNT}"

    if [[ "${DRY_RUN}" != "true" ]]; then
        if ! mountpoint -q "${NFS_MOUNT}" 2>/dev/null; then
            die "NFS mount at ${NFS_MOUNT} is not active after mount. Check connectivity/export and re-run."
        fi

        # This node is a CoW cluster node — the whole point of the shared
        # mount is writing to it. A read-only check is not enough; prove
        # writability with a real write+read+remove probe and fail loud.
        local probe_file="${NFS_MOUNT}/.cidx-write-probe.$$"
        if ! { echo "cidx-write-probe" > "${probe_file}" 2>/dev/null && cat "${probe_file}" >/dev/null 2>&1; }; then
            rm -f "${probe_file}" 2>/dev/null || true
            die "NFS mount at ${NFS_MOUNT} failed the write probe (mounted but not writable). This node cannot use the shared CoW storage. Check export permissions and re-run."
        fi
        rm -f "${probe_file}"
        info "NFS mount validated at ${NFS_MOUNT} (write probe succeeded)."
    fi
}

# ---------------------------------------------------------------------------
# Cluster step: bind-mount fstab entry (idempotent, testable — fstab_file is
# injectable). Separate from add_fstab_entry because the line shape differs
# (source + target, "none  bind" filesystem/options, no _netdev/nfs4).
# ---------------------------------------------------------------------------

add_fstab_bind_entry() {
    local source_dir="$1"
    local mount_point="$2"
    local fstab_file="${3:-/etc/fstab}"
    local entry="${source_dir}  ${mount_point}  none  bind  0  0"

    if grep -qF "${source_dir}  ${mount_point}" "${fstab_file}" 2>/dev/null; then
        info "fstab bind entry already exists for ${source_dir} -> ${mount_point}. Skipping."
        return 0
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] echo '${entry}' >> ${fstab_file}"
        return 0
    fi

    if [[ "${fstab_file}" == "/etc/fstab" ]]; then
        echo "${entry}" | sudo tee -a "${fstab_file}" >/dev/null
    else
        echo "${entry}" >> "${fstab_file}"
    fi
    info "Added fstab bind entry: ${entry}"
}

# ---------------------------------------------------------------------------
# Cluster step: CoW-daemon LOCAL BIND mount (this node co-located with the
# daemon on the same host — it cannot NFS-mount its own export). See
# docs/cow-storage-setup.md "Bind Mount on the Daemon Host". --nfs-export is
# used as the local source directory; --nfs-server is not required/used here.
# ---------------------------------------------------------------------------

setup_local_bind_mount() {
    local source_dir="${NFS_EXPORT}"
    local mount_point="${NFS_MOUNT}"
    info "--- CoW local bind mount: ${source_dir} -> ${mount_point} ---"

    if [[ ! -d "${mount_point}" ]]; then
        dry_run_or_exec sudo mkdir -p "${mount_point}"
    fi

    if mountpoint -q "${mount_point}" 2>/dev/null; then
        info "Bind mount already active at ${mount_point}. Skipping mount."
    else
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] sudo mount --bind ${source_dir} ${mount_point}"
        else
            if ! sudo mount --bind "${source_dir}" "${mount_point}"; then
                die "Bind mount command failed for ${source_dir} -> ${mount_point}. Check the source directory exists and re-run."
            fi
        fi
    fi

    add_fstab_bind_entry "${source_dir}" "${mount_point}"

    if [[ "${DRY_RUN}" != "true" ]]; then
        if ! mountpoint -q "${mount_point}" 2>/dev/null; then
            die "Bind mount at ${mount_point} is not active after mount. Verify ${source_dir} exists and re-run."
        fi

        # This node is a CoW cluster node — the whole point of the shared
        # mount is writing to it. A read-only check is not enough; prove
        # writability with a real write+read+remove probe and fail loud.
        local probe_file="${mount_point}/.cidx-write-probe.$$"
        if ! { echo "cidx-write-probe" > "${probe_file}" 2>/dev/null && cat "${probe_file}" >/dev/null 2>&1; }; then
            rm -f "${probe_file}" 2>/dev/null || true
            die "Bind mount at ${mount_point} failed the write probe (mounted but not writable). This node cannot use the shared CoW storage. Check permissions on ${source_dir} and re-run."
        fi
        rm -f "${probe_file}"
        info "Bind mount validated at ${mount_point} (write probe succeeded)."
    fi
}

# ---------------------------------------------------------------------------
# Cluster step: CoW mount dispatcher — bind mount when this node is
# co-located with the daemon host (--cow-local-bind), NFS mount otherwise.
# ---------------------------------------------------------------------------

setup_cow_mount() {
    if [[ "${COW_LOCAL_BIND}" == "true" ]]; then
        setup_local_bind_mount
    else
        setup_nfs_mount
    fi
}

# ---------------------------------------------------------------------------
# Cluster step: PostgreSQL connectivity + migrations
# ---------------------------------------------------------------------------

test_postgres_connectivity() {
    info "Testing PostgreSQL connectivity..."
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] python3 -c \"import psycopg; psycopg.connect('$(mask_secret_uri "${POSTGRES_DSN}")')\""
        return 0
    fi

    python3 - <<PYEOF
import sys
try:
    import psycopg
except ImportError:
    print("ERROR: psycopg (v3) not installed. Run: pip install \"psycopg[binary]\"", file=sys.stderr)
    sys.exit(1)
try:
    conn = psycopg.connect("${POSTGRES_DSN}", connect_timeout=10)
    conn.close()
    print("PostgreSQL connectivity: OK")
except Exception as e:
    print(f"ERROR: Cannot connect to PostgreSQL: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

run_migrations() {
    info "Running database migrations..."
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] PYTHONPATH=${INSTALL_DIR}/src ${PYTHON} -m code_indexer.server.storage.postgres.migrations.runner --connection-string $(mask_secret_uri "${POSTGRES_DSN}")"
        return 0
    fi

    # NOTE (secret-in-argv tradeoff): migrations/runner.py declares
    # --connection-string as a required argparse flag with no environment
    # variable or stdin alternative (checked directly against its source,
    # src/code_indexer/server/storage/postgres/migrations/runner.py), so the
    # DSN unavoidably appears in this process's argv — and therefore in
    # /proc/<pid>/cmdline — for the short lifetime of the migration run.
    # Exposure is local-user-only (no remote surface) and time-bounded to
    # this one subprocess; fixing it properly requires adding an env-var/
    # stdin input mode to the runner itself, which is out of scope here.
    PYTHONPATH="${INSTALL_DIR}/src" "${PYTHON}" -m code_indexer.server.storage.postgres.migrations.runner \
        --connection-string "${POSTGRES_DSN}"
    info "Database migrations complete."
}

# ---------------------------------------------------------------------------
# Step: write config.json — standalone (create-if-missing) or cluster (merge)
# ---------------------------------------------------------------------------

write_config() {
    dry_run_or_exec mkdir -p "${DATA_DIR}/data/golden-repos" "${DATA_DIR}/logs" "${DATA_DIR}/locks"

    if [[ "${CLUSTER_MODE}" != "true" ]]; then
        if [[ -f "${CONFIG_FILE}" ]]; then
            info "config.json already exists, not overwriting (standalone mode)"
            return 0
        fi

        local standalone_json
        standalone_json="$(cat <<JSONEOF
{
  "host": "0.0.0.0",
  "port": ${PORT},
  "log_level": "INFO",
  "storage_mode": "sqlite"
}
JSONEOF
)"
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] Would write to ${CONFIG_FILE}:"
            echo "${standalone_json}"
            return 0
        fi

        mkdir -p "${DATA_DIR}"
        echo "${standalone_json}" > "${CONFIG_FILE}"
        info "Created default config.json (standalone/sqlite) at ${CONFIG_FILE}"
        return 0
    fi

    # Cluster mode: always merge into any existing config, backing it up first.
    require_command jq

    local existing_config="{}"
    if [[ -f "${CONFIG_FILE}" ]]; then
        existing_config="$(cat "${CONFIG_FILE}")"
    fi

    local pg_dsn_display="${POSTGRES_DSN}"
    local api_key_display="${COW_DAEMON_API_KEY}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        pg_dsn_display="$(mask_secret_uri "${POSTGRES_DSN}")"
        api_key_display="***REDACTED***"
    fi

    local new_config
    new_config="$(echo "${existing_config}" | jq \
        --argjson port "${PORT}" \
        --arg storage_mode "postgres" \
        --arg pg_dsn "${pg_dsn_display}" \
        --argjson workers "${WORKERS}" \
        --arg node_id "${NODE_ID}" \
        --arg clone_backend "${CLONE_BACKEND}" \
        '. + {
            host: "0.0.0.0",
            port: $port,
            log_level: "INFO",
            storage_mode: $storage_mode,
            postgres_dsn: $pg_dsn,
            workers: $workers,
            cluster: { node_id: $node_id },
            clone_backend: $clone_backend
        }'
    )"

    if [[ "${CLONE_BACKEND}" == "cow-daemon" ]]; then
        new_config="$(echo "${new_config}" | jq \
            --arg daemon_url "${COW_DAEMON_URL}" \
            --arg api_key "${api_key_display}" \
            --arg mount_point "${NFS_MOUNT}" \
            '. + {
                cow_daemon: {
                    daemon_url: $daemon_url,
                    api_key: $api_key,
                    mount_point: $mount_point,
                    poll_interval_seconds: 2,
                    timeout_seconds: 600
                }
            }'
        )"
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] Config that WOULD be merged/written to ${CONFIG_FILE}:"
        echo "${new_config}"
        return 0
    fi

    mkdir -p "${DATA_DIR}"
    if [[ -f "${CONFIG_FILE}" ]]; then
        local backup_path="${CONFIG_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
        cp "${CONFIG_FILE}" "${backup_path}"
        info "Backed up existing config to ${backup_path}"
    fi

    echo "${new_config}" > "${CONFIG_FILE}"
    chmod 600 "${CONFIG_FILE}"
    info "Cluster config written to ${CONFIG_FILE} (mode 600)"
}

# ---------------------------------------------------------------------------
# Step: pace-maker (credit throttling), non-fatal
# ---------------------------------------------------------------------------

install_pace_maker() {
    info "--- Installing pace-maker (credit throttling) ---"
    local pace_dir="${HOME}/claude-pace-maker"
    local pace_repo="https://github.com/LightspeedDMS/claude-pace-maker.git"

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] clone/pull ${pace_repo} to ${pace_dir}; run install.sh; record pace_maker_clone_path in config"
        return 0
    fi

    if [[ -d "${pace_dir}/.git" ]]; then
        git -C "${pace_dir}" pull || warn "pace-maker git pull failed (non-fatal)"
    else
        if ! git clone "${pace_repo}" "${pace_dir}"; then
            warn "pace-maker clone failed (non-fatal)"
            pace_dir=""
        fi
    fi

    if [[ -n "${pace_dir}" && -d "${pace_dir}" ]]; then
        NONINTERACTIVE=1 bash "${pace_dir}/install.sh" || warn "pace-maker install.sh failed (non-fatal)"

        if [[ ${IS_FRESH_INSTALL} -eq 1 ]] && command -v pace-maker &>/dev/null; then
            pace-maker off || warn "pace-maker off failed (non-fatal)"
            info "pace-maker installed and set to dormant (master OFF)"
        fi

        if [[ -f "${CONFIG_FILE}" ]]; then
            $PYTHON -c "
import json, sys
config_path = '${CONFIG_FILE}'
try:
    with open(config_path) as f:
        config = json.load(f)
    config['pace_maker_clone_path'] = '${pace_dir}'
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
        f.write('\n')
except Exception as e:
    print(f'WARNING: Failed to record pace_maker_clone_path: {e}', file=sys.stderr)
" || warn "Failed to record pace_maker_clone_path (non-fatal)"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Step: systemd service
# ---------------------------------------------------------------------------

create_systemd_service() {
    info "--- Systemd service ---"
    local service_file="/etc/systemd/system/cidx-server.service"

    local voyage_key_display="${VOYAGE_KEY}"
    if [[ "${DRY_RUN}" == "true" && -n "${VOYAGE_KEY}" ]]; then
        voyage_key_display="***REDACTED***"
    fi

    local unit_content
    unit_content="$(cat <<SERVICEEOF
[Unit]
Description=CIDX Server - Code Indexer Server with Semantic Search
Documentation=https://github.com/LightspeedDMS/code-indexer
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${INSTALL_DIR}

Environment="PATH=${HOME}/.cargo/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"
Environment="PYTHONPATH=${INSTALL_DIR}/src"
Environment="CIDX_SERVER_MODE=1"
Environment="CIDX_ISSUER_URL=http://localhost:${PORT}"
Environment="CIDX_REPO_ROOT=${INSTALL_DIR}"
Environment="CIDX_AUTO_UPDATE_BRANCH=${AUTO_UPDATE_BRANCH}"
$(if [[ -n "${VOYAGE_KEY}" ]]; then echo "Environment=\"VOYAGE_API_KEY=${voyage_key_display}\""; fi)

ExecStart=${PYTHON} -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port ${PORT} --log-level info --workers ${WORKERS}

Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=cidx-server

[Install]
WantedBy=multi-user.target
SERVICEEOF
)"

    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] Would write systemd unit to ${service_file}:"
        echo "${unit_content}"
        echo "  [dry-run] sudo systemctl daemon-reload && sudo systemctl enable cidx-server"
        return 0
    fi

    echo "${unit_content}" | sudo tee "${service_file}" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable cidx-server
    info "Systemd service created and enabled"
}

# ---------------------------------------------------------------------------
# Cluster step: open firewalld port (idempotent, non-fatal if absent)
# ---------------------------------------------------------------------------

open_firewall_port() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] Would check/open firewalld port ${PORT}/tcp (if firewalld is active)"
        return 0
    fi

    if ! command -v firewall-cmd >/dev/null 2>&1; then
        warn "firewalld not found; skipping firewall configuration. Ensure port ${PORT}/tcp is reachable."
        return 0
    fi

    if ! sudo firewall-cmd --state >/dev/null 2>&1; then
        warn "firewalld installed but not active; skipping firewall configuration."
        return 0
    fi

    if sudo firewall-cmd --query-port="${PORT}/tcp" >/dev/null 2>&1; then
        info "Firewall port ${PORT}/tcp already open."
        return 0
    fi

    sudo firewall-cmd --permanent --add-port="${PORT}/tcp"
    sudo firewall-cmd --reload
    info "Opened firewall port ${PORT}/tcp"
}

# ---------------------------------------------------------------------------
# Step: start server + health check (GET /docs, matches HAProxy httpchk)
# ---------------------------------------------------------------------------

start_and_verify_server() {
    info "--- Starting server ---"
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] sudo systemctl restart cidx-server"
        echo "  [dry-run] poll http://localhost:${PORT}/docs for HTTP 200 (up to 30s)"
        return 0
    fi

    sudo systemctl restart cidx-server

    local waited=0 http_code="000"
    while (( waited < 30 )); do
        http_code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/docs" || echo "000")"
        if [[ "${http_code}" == "200" ]]; then
            info "Health check PASS: GET /docs returned 200 (after ${waited}s)"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done

    die "Health check FAIL: GET http://localhost:${PORT}/docs did not return 200 within 30s (last code: ${http_code}). Check: journalctl -u cidx-server --no-pager -n 30"
}

# ---------------------------------------------------------------------------
# Cluster step: print cluster_nodes rows so the operator can confirm join
# ---------------------------------------------------------------------------

print_cluster_nodes() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] Would query cluster_nodes from PostgreSQL"
        return 0
    fi

    info "Querying cluster_nodes from PostgreSQL..."
    python3 - <<PYEOF || warn "Could not query cluster_nodes (non-fatal)"
import psycopg
conn = psycopg.connect("${POSTGRES_DSN}", connect_timeout=10)
with conn.cursor() as cur:
    cur.execute("SELECT node_id, hostname, status, role, last_heartbeat FROM cluster_nodes ORDER BY registered_at")
    for row in cur.fetchall():
        print(row)
conn.close()
PYEOF
}

# ---------------------------------------------------------------------------
# Step: summary
# ---------------------------------------------------------------------------

print_summary() {
    cat <<EOF

=== Installation Complete ===
  Server         : http://localhost:${PORT}
  Status         : systemctl status cidx-server
  Logs           : journalctl -u cidx-server -f
  Config         : ${CONFIG_FILE}
  Cluster mode   : ${CLUSTER_MODE}
EOF
    if [[ "${CLUSTER_MODE}" == "true" ]]; then
        cat <<EOF
  Node ID        : ${NODE_ID}
  PostgreSQL     : $(mask_secret_uri "${POSTGRES_DSN}")
  Clone backend  : ${CLONE_BACKEND}
EOF
        if [[ "${CLONE_BACKEND}" == "cow-daemon" ]]; then
            echo "  CoW Daemon     : ${COW_DAEMON_URL}"
            if [[ "${COW_LOCAL_BIND}" == "true" ]]; then
                echo "  Local Bind     : ${NFS_EXPORT} -> ${NFS_MOUNT}"
            else
                echo "  NFS Mount      : ${NFS_SERVER}:${NFS_EXPORT} -> ${NFS_MOUNT}"
            fi
        fi
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"
    validate_args
    resolve_defaults
    detect_pkg_mgr

    echo "=== CIDX Server Installation ==="
    echo "  Package manager: ${PKG_MGR}"
    echo "  Branch: ${BRANCH}"
    echo "  Install dir: ${INSTALL_DIR}"
    echo "  Data dir: ${DATA_DIR}"
    echo "  Port: ${PORT}"
    echo "  Workers: ${WORKERS}"
    echo "  Clone backend: ${CLONE_BACKEND}"
    echo "  Dry run: ${DRY_RUN}"
    echo ""

    install_system_packages
    determine_cluster_mode

    if [[ "${CLUSTER_MODE}" == "true" ]]; then
        echo "  Cluster mode: ENABLED (node_id=${NODE_ID})"
        echo "  PostgreSQL: $(mask_secret_uri "${POSTGRES_DSN}")"
    else
        echo "  Cluster mode: disabled (standalone sqlite)"
    fi
    echo ""

    setup_git_auth
    clone_or_update_repo
    install_python_deps

    if [[ "${CLUSTER_MODE}" == "true" ]]; then
        if [[ "${CLONE_BACKEND}" == "cow-daemon" ]]; then
            setup_cow_mount
        fi
        test_postgres_connectivity
        run_migrations
    fi

    write_config
    install_pace_maker
    create_systemd_service

    if [[ "${CLUSTER_MODE}" == "true" ]]; then
        open_firewall_port
    fi

    start_and_verify_server

    if [[ "${CLUSTER_MODE}" == "true" && "${DRY_RUN}" != "true" ]]; then
        print_cluster_nodes
    fi

    print_summary

    if [[ "${DRY_RUN}" == "true" ]]; then
        info "Dry run complete. No changes were made."
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
