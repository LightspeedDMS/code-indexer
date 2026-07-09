#!/bin/bash
# install-cidx-server-test.sh — Validates install-cidx-server.sh argument
# parsing, cluster-mode activation, config.json shape, idempotent merge,
# fstab dedup, and standalone-mode preservation.
#
# Modeled on scripts/cluster-join-test.sh. Does NOT touch a real server,
# PostgreSQL, or NFS mount: full-script invocations use --dry-run (which
# short-circuits every sudo/network/systemctl call before it happens), and
# a handful of tests source the script's functions directly to exercise
# pure file-local logic (config merge, fstab dedup) without root/network.
#
# Exit code: 0 if all tests pass, 1 otherwise.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SCRIPT="${SCRIPT_DIR}/install-cidx-server.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

run_test() {
    local description="$1"
    shift
    if "$@"; then
        pass "${description}"
    else
        fail "${description}"
    fi
}

# ---------------------------------------------------------------------------
# Helper: source the install script inside a fresh bash process and execute
# arbitrary code against its functions/variables. Runs in a SEPARATE bash -c
# process (not the current shell) so any exit()/die() inside only kills that
# nested process — never this test runner. Combines stdout+stderr.
# ---------------------------------------------------------------------------

run_sourced() {
    local code="$1"
    bash -c "set -uo pipefail; source '${INSTALL_SCRIPT}'; ${code}" 2>&1
}

# ===========================================================================
# Group A: default branch fix
# ===========================================================================

test_default_branch_is_master() {
    local output
    output="$(run_sourced 'echo "BRANCH=${BRANCH}"')"
    echo "${output}" | grep -q '^BRANCH=master$'
}
run_test "Default BRANCH is 'master' (not stale epic/408 value)" test_default_branch_is_master

# ===========================================================================
# Group B: cluster-mode activation logic (determine_cluster_mode)
# ===========================================================================

test_cluster_requires_node_id_on_first_run() {
    local output exit_code
    output="$(run_sourced '
        POSTGRES_DSN="postgresql://user:pass@host/db"
        NODE_ID=""
        CONFIG_FILE="/nonexistent-config-file-for-test.json"
        determine_cluster_mode
    ')" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -ne 0 ]] && echo "${output}" | grep -q -- "--node-id"
}
run_test "Cluster mode with only --postgres-dsn (no existing node_id) dies naming --node-id" \
    test_cluster_requires_node_id_on_first_run

test_cluster_not_activated_with_only_node_id() {
    local output exit_code
    output="$(run_sourced '
        POSTGRES_DSN=""
        NODE_ID="my-node"
        CONFIG_FILE="/nonexistent-config-file-for-test.json"
        determine_cluster_mode
        echo "CLUSTER_MODE=${CLUSTER_MODE}"
    ')" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -eq 0 ]] && echo "${output}" | grep -q '^CLUSTER_MODE=false$'
}
run_test "Only --node-id (no --postgres-dsn) => standalone (CLUSTER_MODE=false)" \
    test_cluster_not_activated_with_only_node_id

test_cluster_activated_with_both() {
    local output exit_code
    output="$(run_sourced '
        POSTGRES_DSN="postgresql://user:pass@host/db"
        NODE_ID="staging"
        CONFIG_FILE="/nonexistent-config-file-for-test.json"
        determine_cluster_mode
        echo "CLUSTER_MODE=${CLUSTER_MODE}"
        echo "NODE_ID=${NODE_ID}"
    ')" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q '^CLUSTER_MODE=true$' \
        && echo "${output}" | grep -q '^NODE_ID=staging$'
}
run_test "Both --node-id and --postgres-dsn => cluster mode activates" test_cluster_activated_with_both

test_cluster_reuses_existing_node_id() {
    local tmpdir cfg output exit_code
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"
    cat > "${cfg}" <<JSONEOF
{"cluster": {"node_id": "existing-node-abc"}}
JSONEOF

    output="$(run_sourced "
        POSTGRES_DSN='postgresql://user:pass@host/db'
        NODE_ID=''
        CONFIG_FILE='${cfg}'
        determine_cluster_mode
        echo \"CLUSTER_MODE=\${CLUSTER_MODE}\"
        echo \"NODE_ID=\${NODE_ID}\"
    ")" && exit_code=0 || exit_code=$?

    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q '^CLUSTER_MODE=true$' \
        && echo "${output}" | grep -q '^NODE_ID=existing-node-abc$'
}
run_test "Re-join (--postgres-dsn only, existing config) reuses stored node_id" \
    test_cluster_reuses_existing_node_id

# ===========================================================================
# Group C: config.json shape (write_config)
# ===========================================================================

test_config_local_backend_shape() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"

    output="$(run_sourced "
        CLUSTER_MODE=true
        NODE_ID='n1'
        POSTGRES_DSN='postgresql://user:pass@host/db'
        CLONE_BACKEND='local'
        PORT=8090
        WORKERS=2
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
        cat '${cfg}'
    ")"

    rm -rf "${tmpdir}"

    echo "${output}" | grep -q '"clone_backend": "local"' \
        && echo "${output}" | grep -q '"node_id": "n1"' \
        && ! echo "${output}" | grep -q "cow_daemon"
}
run_test "write_config (local backend) produces clone_backend=local, no cow_daemon block" \
    test_config_local_backend_shape

test_config_cow_daemon_backend_shape() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"

    output="$(run_sourced "
        CLUSTER_MODE=true
        NODE_ID='staging'
        POSTGRES_DSN='postgresql://user:pass@host/db'
        CLONE_BACKEND='cow-daemon'
        COW_DAEMON_URL='http://192.168.60.23:8081'
        COW_DAEMON_API_KEY='super-secret-key'
        NFS_MOUNT='/mnt/cow-storage'
        PORT=8000
        WORKERS=1
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
        cat '${cfg}'
    ")"

    rm -rf "${tmpdir}"

    echo "${output}" | grep -q '"clone_backend": "cow-daemon"' \
        && echo "${output}" | grep -q "cow_daemon" \
        && echo "${output}" | grep -q '"daemon_url": "http://192.168.60.23:8081"' \
        && echo "${output}" | grep -q '"mount_point": "/mnt/cow-storage"'
}
run_test "write_config (cow-daemon backend) produces clone_backend + cow_daemon fields" \
    test_config_cow_daemon_backend_shape

# ===========================================================================
# Group C.1: Bug #1320 Part B - cow_daemon.daemon_storage_path
# ===========================================================================

test_config_cow_daemon_storage_path_written_when_resolved() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"

    output="$(run_sourced "
        CLUSTER_MODE=true
        NODE_ID='staging'
        POSTGRES_DSN='postgresql://user:pass@host/db'
        CLONE_BACKEND='cow-daemon'
        COW_DAEMON_URL='http://203.0.113.10:8081'
        COW_DAEMON_API_KEY='test-api-key-not-real'
        COW_DAEMON_STORAGE_PATH='/srv/cow-xfs-root'
        NFS_MOUNT='/mnt/cow-storage'
        PORT=8000
        WORKERS=1
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
        cat '${cfg}'
    ")"

    rm -rf "${tmpdir}"

    echo "${output}" | grep -q '"daemon_storage_path": "/srv/cow-xfs-root"'
}
run_test "write_config emits cow_daemon.daemon_storage_path when COW_DAEMON_STORAGE_PATH is resolved" \
    test_config_cow_daemon_storage_path_written_when_resolved

test_config_cow_daemon_storage_path_omitted_when_unresolved() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"

    output="$(run_sourced "
        CLUSTER_MODE=true
        NODE_ID='staging'
        POSTGRES_DSN='postgresql://user:pass@host/db'
        CLONE_BACKEND='cow-daemon'
        COW_DAEMON_URL='http://203.0.113.10:8081'
        COW_DAEMON_API_KEY='test-api-key-not-real'
        COW_DAEMON_STORAGE_PATH=''
        NFS_MOUNT='/mnt/cow-storage'
        PORT=8000
        WORKERS=1
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
        cat '${cfg}'
    ")"

    rm -rf "${tmpdir}"

    # Field must be OMITTED entirely (never written as null or empty string).
    ! echo "${output}" | grep -q "daemon_storage_path"
}
run_test "write_config omits daemon_storage_path (not null) when nothing resolves" \
    test_config_cow_daemon_storage_path_omitted_when_unresolved

test_config_cow_daemon_storage_path_not_clobbered_on_rerun() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"
    cat > "${cfg}" <<JSONEOF
{"clone_backend": "cow-daemon", "cow_daemon": {"daemon_url": "http://203.0.113.10:8081", "mount_point": "/mnt/cow-storage", "daemon_storage_path": "/srv/original-value"}}
JSONEOF

    output="$(run_sourced "
        CLUSTER_MODE=true
        NODE_ID='staging'
        POSTGRES_DSN='postgresql://user:pass@host/db'
        CLONE_BACKEND='cow-daemon'
        COW_DAEMON_URL='http://203.0.113.10:8081'
        COW_DAEMON_API_KEY='test-api-key-not-real'
        COW_DAEMON_STORAGE_PATH='/srv/some-other-value'
        NFS_MOUNT='/mnt/cow-storage'
        PORT=8000
        WORKERS=1
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
        cat '${cfg}'
    ")"

    rm -rf "${tmpdir}"

    echo "${output}" | grep -q '"daemon_storage_path": "/srv/original-value"' \
        && ! echo "${output}" | grep -q "/srv/some-other-value"
}
run_test "write_config never clobbers a pre-existing daemon_storage_path with a different resolved value" \
    test_config_cow_daemon_storage_path_not_clobbered_on_rerun

test_resolve_storage_path_prefers_explicit_var() {
    local output
    output="$(run_sourced '
        COW_DAEMON_STORAGE_PATH="/from/flag"
        unset CIDX_COW_DAEMON_STORAGE_PATH
        resolve_cow_daemon_storage_path
        echo "RESULT=${COW_DAEMON_STORAGE_PATH}"
    ')"
    echo "${output}" | grep -q '^RESULT=/from/flag$'
}
run_test "resolve_cow_daemon_storage_path prefers the explicit --cow-daemon-storage-path value" \
    test_resolve_storage_path_prefers_explicit_var

test_resolve_storage_path_uses_env_var_when_flag_absent() {
    local output
    output="$(run_sourced '
        COW_DAEMON_STORAGE_PATH=""
        export CIDX_COW_DAEMON_STORAGE_PATH="/from/env"
        resolve_cow_daemon_storage_path
        echo "RESULT=${COW_DAEMON_STORAGE_PATH}"
    ')"
    echo "${output}" | grep -q '^RESULT=/from/env$'
}
run_test "resolve_cow_daemon_storage_path falls back to CIDX_COW_DAEMON_STORAGE_PATH env var" \
    test_resolve_storage_path_uses_env_var_when_flag_absent

test_resolve_storage_path_auto_detects_from_co_located_daemon_config() {
    local tmpdir daemon_cfg output
    tmpdir="$(mktemp -d)"
    daemon_cfg="${tmpdir}/cow-storage-daemon-config.json"
    echo '{"base_path": "/srv/auto-detected-xfs"}' > "${daemon_cfg}"

    output="$(run_sourced "
        COW_DAEMON_STORAGE_PATH=''
        unset CIDX_COW_DAEMON_STORAGE_PATH
        COW_DAEMON_HOST_CONFIG_PATH='${daemon_cfg}'
        resolve_cow_daemon_storage_path
        echo \"RESULT=\${COW_DAEMON_STORAGE_PATH}\"
    ")"

    rm -rf "${tmpdir}"

    echo "${output}" | grep -q '^RESULT=/srv/auto-detected-xfs$'
}
run_test "resolve_cow_daemon_storage_path auto-detects base_path from co-located CoW daemon config" \
    test_resolve_storage_path_auto_detects_from_co_located_daemon_config

test_resolve_storage_path_leaves_unset_and_warns_when_no_source() {
    local output exit_code
    output="$(run_sourced '
        COW_DAEMON_STORAGE_PATH=""
        unset CIDX_COW_DAEMON_STORAGE_PATH
        COW_DAEMON_HOST_CONFIG_PATH="/nonexistent-daemon-config-for-test.json"
        resolve_cow_daemon_storage_path
        echo "RESULT=[${COW_DAEMON_STORAGE_PATH}]"
    ')" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q '^RESULT=\[\]$' \
        && echo "${output}" | grep -qi "could not be resolved"
}
run_test "resolve_cow_daemon_storage_path leaves the value unset and warns when no source resolves" \
    test_resolve_storage_path_leaves_unset_and_warns_when_no_source

test_help_documents_cow_daemon_storage_path() {
    local output exit_code
    output="$(bash "${INSTALL_SCRIPT}" --help 2>&1)" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -eq 0 ]] && echo "${output}" | grep -q -- "--cow-daemon-storage-path"
}
run_test "--help documents --cow-daemon-storage-path" test_help_documents_cow_daemon_storage_path

test_config_merge_preserves_existing_key() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"
    cat > "${cfg}" <<JSONEOF
{"pace_maker_clone_path": "/home/user/claude-pace-maker"}
JSONEOF

    output="$(run_sourced "
        CLUSTER_MODE=true
        NODE_ID='n1'
        POSTGRES_DSN='postgresql://user:pass@host/db'
        CLONE_BACKEND='local'
        PORT=8000
        WORKERS=1
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
        cat '${cfg}'
    ")"

    rm -rf "${tmpdir}"

    echo "${output}" | grep -q "pace_maker_clone_path" \
        && echo "${output}" | grep -q "/home/user/claude-pace-maker" \
        && echo "${output}" | grep -q '"node_id": "n1"'
}
run_test "Cluster config merge preserves pre-existing keys (e.g. pace_maker_clone_path)" \
    test_config_merge_preserves_existing_key

test_config_merge_backs_up_existing_file() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"
    echo '{"storage_mode": "sqlite"}' > "${cfg}"

    output="$(run_sourced "
        CLUSTER_MODE=true
        NODE_ID='n1'
        POSTGRES_DSN='postgresql://user:pass@host/db'
        CLONE_BACKEND='local'
        PORT=8000
        WORKERS=1
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
    ")"

    local backup_count
    backup_count="$(find "${tmpdir}" -maxdepth 1 -name 'config.json.bak.*' | wc -l)"
    rm -rf "${tmpdir}"

    [[ "${backup_count}" -ge 1 ]]
}
run_test "Cluster config merge backs up the pre-existing config.json before overwriting" \
    test_config_merge_backs_up_existing_file

# ===========================================================================
# Group D: standalone mode preservation
# ===========================================================================

test_standalone_config_written_when_missing() {
    local tmpdir cfg output
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"

    output="$(run_sourced "
        CLUSTER_MODE=false
        PORT=9000
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
        cat '${cfg}'
    ")"

    rm -rf "${tmpdir}"

    echo "${output}" | grep -q '"storage_mode": "sqlite"' \
        && echo "${output}" | grep -q '"port": 9000'
}
run_test "Standalone mode writes default sqlite config.json when absent" \
    test_standalone_config_written_when_missing

test_standalone_config_not_overwritten() {
    local tmpdir cfg before after
    tmpdir="$(mktemp -d)"
    cfg="${tmpdir}/config.json"
    cat > "${cfg}" <<JSONEOF
{"storage_mode": "sqlite", "custom_marker": "KEEP_ME_UNCHANGED"}
JSONEOF
    before="$(cat "${cfg}")"

    run_sourced "
        CLUSTER_MODE=false
        PORT=9000
        DATA_DIR='${tmpdir}'
        CONFIG_FILE='${cfg}'
        DRY_RUN=false
        write_config
    " >/dev/null

    after="$(cat "${cfg}")"
    rm -rf "${tmpdir}"

    [[ "${before}" == "${after}" ]]
}
run_test "Standalone mode NEVER overwrites an existing config.json (unchanged bytes)" \
    test_standalone_config_not_overwritten

# ===========================================================================
# Group E: fstab idempotency (add_fstab_entry)
# ===========================================================================

test_fstab_entry_not_duplicated() {
    local tmpdir fstab_file line_count
    tmpdir="$(mktemp -d)"
    fstab_file="${tmpdir}/fstab"
    touch "${fstab_file}"

    run_sourced "
        DRY_RUN=false
        add_fstab_entry '192.168.60.23:/home/jsbattig/cow-storage' '/mnt/cow-storage' '${fstab_file}'
        add_fstab_entry '192.168.60.23:/home/jsbattig/cow-storage' '/mnt/cow-storage' '${fstab_file}'
    " >/dev/null

    line_count="$(grep -cF '192.168.60.23:/home/jsbattig/cow-storage' "${fstab_file}")"
    rm -rf "${tmpdir}"

    [[ "${line_count}" -eq 1 ]]
}
run_test "add_fstab_entry does not duplicate the entry on a second run" test_fstab_entry_not_duplicated

test_fstab_entry_dry_run_writes_nothing() {
    local tmpdir fstab_file output size
    tmpdir="$(mktemp -d)"
    fstab_file="${tmpdir}/fstab"
    touch "${fstab_file}"

    output="$(run_sourced "
        DRY_RUN=true
        add_fstab_entry '192.168.60.23:/export' '/mnt/cow-storage' '${fstab_file}'
    ")"

    size="$(wc -l < "${fstab_file}")"
    rm -rf "${tmpdir}"

    [[ "${size}" -eq 0 ]] && echo "${output}" | grep -q '\[dry-run\]'
}
run_test "add_fstab_entry under --dry-run prints intent and writes nothing" \
    test_fstab_entry_dry_run_writes_nothing

test_fstab_bind_entry_not_duplicated() {
    local tmpdir fstab_file line_count
    tmpdir="$(mktemp -d)"
    fstab_file="${tmpdir}/fstab"
    touch "${fstab_file}"

    run_sourced "
        DRY_RUN=false
        add_fstab_bind_entry '/home/jsbattig/cow-storage' '/mnt/cow-storage' '${fstab_file}'
        add_fstab_bind_entry '/home/jsbattig/cow-storage' '/mnt/cow-storage' '${fstab_file}'
    " >/dev/null

    line_count="$(grep -cF '/home/jsbattig/cow-storage  /mnt/cow-storage' "${fstab_file}")"
    rm -rf "${tmpdir}"

    [[ "${line_count}" -eq 1 ]]
}
run_test "add_fstab_bind_entry does not duplicate the entry on a second run" \
    test_fstab_bind_entry_not_duplicated

test_fstab_bind_entry_no_false_match_on_mount_point_substring() {
    local tmpdir fstab_file line_count
    tmpdir="$(mktemp -d)"
    fstab_file="${tmpdir}/fstab"
    touch "${fstab_file}"

    run_sourced "
        DRY_RUN=false
        add_fstab_bind_entry '/srv/other-source' '/mnt/cow-storage-2' '${fstab_file}'
        add_fstab_bind_entry '/home/jsbattig/cow-storage' '/mnt/cow-storage' '${fstab_file}'
    " >/dev/null

    line_count="$(grep -cF '/home/jsbattig/cow-storage  /mnt/cow-storage ' "${fstab_file}")"
    rm -rf "${tmpdir}"

    # Regression guard for L1: an unanchored substring dedup on mount_point
    # alone would have matched '/mnt/cow-storage' inside the pre-existing
    # '/mnt/cow-storage-2' entry and silently skipped writing this one.
    [[ "${line_count}" -eq 1 ]]
}
run_test "add_fstab_bind_entry does not false-match /mnt/cow-storage against a pre-existing /mnt/cow-storage-2 entry" \
    test_fstab_bind_entry_no_false_match_on_mount_point_substring

# ===========================================================================
# Group F: validate_args (cow-daemon required sub-args)
# ===========================================================================

test_validate_args_cow_daemon_requires_url() {
    local output exit_code
    output="$(run_sourced '
        CLONE_BACKEND="cow-daemon"
        COW_DAEMON_URL=""
        COW_DAEMON_API_KEY="key"
        NFS_SERVER="host"
        NFS_EXPORT="/export"
        validate_args
    ')" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -ne 0 ]] && echo "${output}" | grep -q -- "--cow-daemon-url"
}
run_test "validate_args rejects cow-daemon backend missing --cow-daemon-url" \
    test_validate_args_cow_daemon_requires_url

test_validate_args_rejects_invalid_clone_backend() {
    local output exit_code
    output="$(run_sourced '
        CLONE_BACKEND="totally-bogus"
        validate_args
    ')" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -ne 0 ]] && echo "${output}" | grep -q "clone-backend"
}
run_test "validate_args rejects an invalid --clone-backend value" \
    test_validate_args_rejects_invalid_clone_backend

# ===========================================================================
# Group G: full-script end-to-end smoke tests (--dry-run, subprocess)
# ===========================================================================

test_help_exits_zero() {
    local output exit_code
    output="$(bash "${INSTALL_SCRIPT}" --help 2>&1)" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -eq 0 ]] && echo "${output}" | grep -q -- "--node-id"
}
run_test "--help exits 0 and documents --node-id" test_help_exits_zero

test_standalone_dry_run_writes_no_file() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    local file_exists=0
    [[ -f "${tmpdir}/.cidx-server/config.json" ]] && file_exists=1
    rm -rf "${tmpdir}"
    [[ ${exit_code} -eq 0 && ${file_exists} -eq 0 ]]
}
run_test "Standalone --dry-run (no cluster args) exits 0 and writes no config.json" \
    test_standalone_dry_run_writes_no_file

test_standalone_dry_run_shows_master_branch() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"
    [[ ${exit_code} -eq 0 ]] && echo "${output}" | grep -q "Branch: master"
}
run_test "Full-script dry-run banner shows default Branch: master" \
    test_standalone_dry_run_shows_master_branch

test_fresh_clone_dry_run_skips_recurse_submodules() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && ! echo "${output}" | grep -q -- "--recurse-submodules" \
        && echo "${output}" | grep -q "submodule update --init third_party/hnswlib"
}
run_test "Fresh-clone dry-run does NOT recurse all submodules, only inits third_party/hnswlib" \
    test_fresh_clone_dry_run_skips_recurse_submodules

test_cluster_dry_run_end_to_end() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" \
        --node-id staging \
        --postgres-dsn "postgresql://cidx:secretpw@192.168.68.43/cidx_server" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    local file_exists=0
    [[ -f "${tmpdir}/.cidx-server/config.json" ]] && file_exists=1
    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 && ${file_exists} -eq 0 ]] \
        && echo "${output}" | grep -q "Cluster mode: ENABLED" \
        && echo "${output}" | grep -q "node_id=staging" \
        && ! echo "${output}" | grep -q "secretpw"
}
run_test "Cluster --dry-run end-to-end: activates cluster mode, writes nothing, masks password" \
    test_cluster_dry_run_end_to_end

test_cow_daemon_dry_run_end_to_end() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" \
        --node-id staging \
        --postgres-dsn "postgresql://cidx:secretpw@192.168.68.43/cidx_server" \
        --clone-backend cow-daemon \
        --cow-daemon-url "http://192.168.60.23:8081" \
        --cow-daemon-api-key "daemon-key-xyz" \
        --nfs-server "192.168.60.23" \
        --nfs-export "/home/jsbattig/cow-storage" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q "cow-daemon" \
        && echo "${output}" | grep -q "/mnt/cow-storage" \
        && ! echo "${output}" | grep -q "daemon-key-xyz"
}
run_test "CoW-daemon --dry-run end-to-end mentions mount/daemon, masks api key" \
    test_cow_daemon_dry_run_end_to_end

test_cow_local_bind_uses_bind_mount() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" \
        --node-id node-23 \
        --postgres-dsn "postgresql://cidx:secretpw@192.168.68.43/cidx_server" \
        --clone-backend cow-daemon \
        --cow-daemon-url "http://192.168.60.23:8081" \
        --cow-daemon-api-key "daemon-key-xyz" \
        --cow-local-bind \
        --nfs-export "/home/jsbattig/cow-storage" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q "mount --bind" \
        && echo "${output}" | grep -q "none  bind" \
        && ! echo "${output}" | grep -q "mount -t nfs4"
}
run_test "--cow-local-bind uses bind mount (mount --bind + fstab bind form), no NFS mount" \
    test_cow_local_bind_uses_bind_mount

test_dry_run_installs_auto_update_units() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q "cidx-auto-update.service" \
        && echo "${output}" | grep -q "cidx-auto-update.timer" \
        && echo "${output}" | grep -q "enable cidx-auto-update.timer" \
        && echo "${output}" | grep -q "start cidx-auto-update.timer"
}
run_test "Dry-run installs and enables/starts cidx-auto-update.service/.timer (Bug: auto-updater never installed)" \
    test_dry_run_installs_auto_update_units

test_dry_run_threads_auto_update_branch() {
    local tmpdir output exit_code occurrences
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" --branch staging --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"

    # Must appear TWICE: once in the pre-existing cidx-server.service unit and
    # once in the NEW cidx-auto-update.service unit rendered from the
    # {BRANCH}-parameterized template. A single occurrence means only the
    # pre-existing (misplaced) env line fired and the auto-update unit itself
    # was never rendered with the branch substituted.
    occurrences="$(echo "${output}" | grep -o "CIDX_AUTO_UPDATE_BRANCH=staging" | wc -l)"

    [[ ${exit_code} -eq 0 ]] && [[ "${occurrences}" -eq 2 ]]
}
run_test "Dry-run threads --branch staging into CIDX_AUTO_UPDATE_BRANCH for the auto-update unit (not just the pre-existing cidx-server.service line)" \
    test_dry_run_threads_auto_update_branch

test_dry_run_no_unrendered_branch_placeholder() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" --branch staging --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] && ! echo "${output}" | grep -qF '{BRANCH}'
}
run_test "Dry-run never leaks an unrendered {BRANCH} placeholder" \
    test_dry_run_no_unrendered_branch_placeholder

test_repo_token_never_echoed() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" \
        --repo-token "ghp_SuperSecretToken123" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && ! echo "${output}" | grep -q "ghp_SuperSecretToken123" \
        && echo "${output}" | grep -qi "credential"
}
run_test "--repo-token value is never echoed to output" test_repo_token_never_echoed

test_git_auth_writes_real_credentials_file() {
    local tmpdir creds_file output perms exit_code line_count
    tmpdir="$(mktemp -d)"
    creds_file="${tmpdir}/.git-credentials"

    output="$(HOME="${tmpdir}" run_sourced "
        REPO_TOKEN='ghp_RealTokenForTest456'
        REPO_URL='https://github.com/LightspeedDMS/code-indexer.git'
        DRY_RUN=false
        setup_git_auth
        git config --global --get credential.helper
        cat '${creds_file}'
    ")" && exit_code=0 || exit_code=$?

    perms="$(stat -c '%a' "${creds_file}" 2>/dev/null || echo 'MISSING')"

    # Second run against the same HOME must not duplicate the @host line.
    HOME="${tmpdir}" run_sourced "
        REPO_TOKEN='ghp_RealTokenForTest456'
        REPO_URL='https://github.com/LightspeedDMS/code-indexer.git'
        DRY_RUN=false
        setup_git_auth
    " >/dev/null

    line_count="$(grep -cF '@github.com' "${creds_file}" 2>/dev/null || echo 0)"

    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && [[ "${perms}" == "600" ]] \
        && echo "${output}" | grep -q "https://ghp_RealTokenForTest456@github.com" \
        && echo "${output}" | grep -q "^store$" \
        && [[ "${line_count}" -eq 1 ]]
}
run_test "setup_git_auth (real, non-dry-run) writes .git-credentials (600) with token entry, sets credential.helper store, no dup on re-run" \
    test_git_auth_writes_real_credentials_file

test_cow_daemon_missing_url_fails_full_script() {
    local tmpdir output exit_code
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${INSTALL_SCRIPT}" \
        --node-id staging \
        --postgres-dsn "postgresql://cidx:pw@host/db" \
        --clone-backend cow-daemon \
        --cow-daemon-api-key "key" \
        --nfs-server "host" \
        --nfs-export "/export" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?
    rm -rf "${tmpdir}"
    [[ ${exit_code} -ne 0 ]] && echo "${output}" | grep -q -- "--cow-daemon-url"
}
run_test "Full script rejects cow-daemon backend missing --cow-daemon-url" \
    test_cow_daemon_missing_url_fails_full_script

# ===========================================================================
# Results
# ===========================================================================

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"

if [[ ${FAIL} -gt 0 ]]; then
    exit 1
fi
exit 0
