#!/bin/bash
# cluster-join-test.sh — Validates cluster-join.sh argument parsing and
# config generation without mounting NFS or connecting to PostgreSQL.
#
# Story #425
#
# Tests:
#   1. Missing required args causes exit 1
#   2. --dry-run with all args outputs expected config keys without writing files
#   3. --node-id override is preserved in dry-run output
#   4. --help exits cleanly

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOIN_SCRIPT="${SCRIPT_DIR}/cluster-join.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

run_test() {
    local description="$1"
    shift
    # Returns: 0 = test logic passed, non-zero = assertion failed
    if "$@"; then
        pass "${description}"
    else
        fail "${description}"
    fi
}

# ---------------------------------------------------------------------------
# Shared valid argument set (used for tests that need all required args)
# ---------------------------------------------------------------------------

VALID_ARGS=(
    --postgres-url    "postgresql://user:pass@pg-host/cidxdb"
    --ontap-endpoint  "100.99.60.248"
    --ontap-export    "/"
    --ontap-mount     "/mnt/fsx"
    --ontap-admin-user "fsxadmin"
    --ontap-admin-password "secret"
    --ontap-svm       "sebaV2"
    --ontap-parent-volume "seba_vol1"
    --nfs-data-lif    "100.99.60.204"
)

# ---------------------------------------------------------------------------
# Test 1: Missing required args => exit 1
# ---------------------------------------------------------------------------

test_missing_args() {
    local output exit_code
    output="$(bash "${JOIN_SCRIPT}" 2>&1)" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -ne 0 ]] && echo "${output}" | grep -q "Missing required"
}
run_test "Missing required args exits non-zero with helpful message" test_missing_args

# ---------------------------------------------------------------------------
# Test 2: Missing single required arg => exit 1 and names the missing arg
# ---------------------------------------------------------------------------

test_missing_one_arg() {
    local output exit_code
    # Omit --nfs-data-lif
    output="$(bash "${JOIN_SCRIPT}" \
        --postgres-url    "postgresql://user:pass@pg-host/cidxdb" \
        --ontap-endpoint  "100.99.60.248" \
        --ontap-export    "/" \
        --ontap-mount     "/mnt/fsx" \
        --ontap-admin-user "fsxadmin" \
        --ontap-admin-password "secret" \
        --ontap-svm       "sebaV2" \
        --ontap-parent-volume "seba_vol1" \
        2>&1)" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -ne 0 ]] && echo "${output}" | grep -q "\-\-nfs-data-lif"
}
run_test "Missing --nfs-data-lif is identified in error message" test_missing_one_arg

# ---------------------------------------------------------------------------
# Test 3: Unknown argument => exit 1
# ---------------------------------------------------------------------------

test_unknown_arg() {
    local exit_code
    bash "${JOIN_SCRIPT}" --not-a-real-arg foo 2>/dev/null && exit_code=0 || exit_code=$?
    [[ ${exit_code} -ne 0 ]]
}
run_test "Unknown argument exits non-zero" test_unknown_arg

# ---------------------------------------------------------------------------
# Test 4: --help exits 0
# ---------------------------------------------------------------------------

test_help() {
    local output exit_code
    output="$(bash "${JOIN_SCRIPT}" --help 2>&1)" && exit_code=0 || exit_code=$?
    [[ ${exit_code} -eq 0 ]] && echo "${output}" | grep -q "postgres-url"
}
run_test "--help exits 0 and shows usage" test_help

# ---------------------------------------------------------------------------
# Test 5: --dry-run prints config keys without writing any files
# ---------------------------------------------------------------------------

test_dry_run_no_files() {
    local tmpdir output exit_code

    # Run with HOME pointing to a temp dir so no real ~/.cidx-server is touched
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${JOIN_SCRIPT}" \
        "${VALID_ARGS[@]}" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?

    # Config file must NOT have been written
    if [[ -f "${tmpdir}/.cidx-server/config.json" ]]; then
        rm -rf "${tmpdir}"
        return 1
    fi

    rm -rf "${tmpdir}"

    # Exit code must be 0 in dry-run
    [[ ${exit_code} -eq 0 ]]
}
run_test "--dry-run exits 0 and does not write config.json" test_dry_run_no_files

# ---------------------------------------------------------------------------
# Test 6: --dry-run output contains expected config keys
# ---------------------------------------------------------------------------

test_dry_run_output_content() {
    local tmpdir output exit_code

    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${JOIN_SCRIPT}" \
        "${VALID_ARGS[@]}" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?

    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q "storage_mode" \
        && echo "${output}" | grep -q "postgres" \
        && echo "${output}" | grep -q "ontap" \
        && echo "${output}" | grep -q "cluster"
}
run_test "--dry-run output mentions storage_mode, postgres, ontap, cluster" test_dry_run_output_content

# ---------------------------------------------------------------------------
# Test 7: --node-id override appears in dry-run output
# ---------------------------------------------------------------------------

test_node_id_override() {
    local tmpdir output exit_code custom_node_id

    custom_node_id="my-custom-node-abc123"
    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${JOIN_SCRIPT}" \
        "${VALID_ARGS[@]}" \
        --node-id "${custom_node_id}" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?

    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] && echo "${output}" | grep -q "${custom_node_id}"
}
run_test "--node-id override appears in dry-run output" test_node_id_override

# ---------------------------------------------------------------------------
# Test 8: Re-join reuses existing node_id from config.json
# ---------------------------------------------------------------------------

test_rejoin_reuses_node_id() {
    local tmpdir output exit_code existing_node_id

    existing_node_id="existing-node-xyz789"
    tmpdir="$(mktemp -d)"
    mkdir -p "${tmpdir}/.cidx-server"

    # Write a pre-existing config with a node_id
    cat > "${tmpdir}/.cidx-server/config.json" <<JSONEOF
{
  "cluster": {
    "node_id": "${existing_node_id}"
  }
}
JSONEOF

    output="$(HOME="${tmpdir}" bash "${JOIN_SCRIPT}" \
        "${VALID_ARGS[@]}" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?

    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] && echo "${output}" | grep -q "${existing_node_id}"
}
run_test "Re-join reuses existing node_id from config.json" test_rejoin_reuses_node_id

# ---------------------------------------------------------------------------
# Test 9: NFS mount point in dry-run output
# ---------------------------------------------------------------------------

test_nfs_mount_in_output() {
    local tmpdir output exit_code

    tmpdir="$(mktemp -d)"
    output="$(HOME="${tmpdir}" bash "${JOIN_SCRIPT}" \
        "${VALID_ARGS[@]}" \
        --dry-run 2>&1)" && exit_code=0 || exit_code=$?

    rm -rf "${tmpdir}"

    [[ ${exit_code} -eq 0 ]] \
        && echo "${output}" | grep -q "100.99.60.204" \
        && echo "${output}" | grep -q "/mnt/fsx"
}
run_test "NFS data LIF and mount point appear in dry-run output" test_nfs_mount_in_output

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"

if [[ ${FAIL} -gt 0 ]]; then
    exit 1
fi
exit 0
