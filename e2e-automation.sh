#!/usr/bin/env bash
# e2e-automation.sh — CIDX Comprehensive E2E Test Suite Orchestrator
#
# Runs all 4 E2E phases sequentially:
#   Phase 1: CLI standalone  (tests/e2e/cli_standalone/)
#   Phase 2: CLI daemon      (tests/e2e/cli_daemon/)
#   Phase 3: Server in-proc  (tests/e2e/server/) via FastAPI TestClient
#   Phase 4: CLI remote      (tests/e2e/cli_remote/) against live uvicorn subprocess
#
# Usage:
#   ./e2e-automation.sh             # Run all phases
#   ./e2e-automation.sh --phase 1   # Run single phase (1-4)
#
# Configuration:
#   Copy .e2e-automation.template to .e2e-automation and fill in values.
#   Non-sensitive defaults are baked into this script. Credentials (E2E_ADMIN_USER,
#   E2E_ADMIN_PASS) must be supplied via .e2e-automation or environment variables —
#   they have no built-in defaults and the script exits immediately if missing.
#   E2E_VOYAGE_API_KEY is optional at the script level; it falls back to the shell
#   VOYAGE_API_KEY env var. Individual tests that require it will fail if neither is set.
#
# Exit codes:
#   0  — all phases passed
#   1  — one or more phases failed (or setup error)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Non-sensitive defaults baked into the script.
# All can be overridden in .e2e-automation or the environment.
# ---------------------------------------------------------------------------
: "${E2E_SERVER_PORT:=8899}"
: "${E2E_SERVER_HOST:=127.0.0.1}"
: "${E2E_SEED_CACHE_DIR:=$HOME/.tmp/cidx-e2e-seed-repos}"
: "${E2E_SERVER_DATA_DIR:=$HOME/.tmp/cidx-e2e-server-data}"
: "${E2E_WORK_DIR:=$HOME/.tmp/cidx-e2e-work}"
: "${E2E_MARKUPSAFE_URL:=https://github.com/pallets/markupsafe.git}"
: "${E2E_MARKUPSAFE_TAG:=2.1.5}"
: "${E2E_TYPEFEST_URL:=https://github.com/sindresorhus/type-fest.git}"
: "${E2E_TYPEFEST_TAG:=v4.8.3}"
: "${E2E_TRIES_URL:=https://github.com/LightspeedDMS/tries.git}"
: "${E2E_TRIES_TAG:=HEAD}"
# Timeout defaults — overridable via .e2e-automation
: "${E2E_SERVER_READINESS_TIMEOUT:=30}"
: "${E2E_SERVER_READINESS_POLL:=1}"

# ---------------------------------------------------------------------------
# Source .e2e-automation if present (provides credentials and optional overrides)
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/.e2e-automation" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.e2e-automation"
fi

# ---------------------------------------------------------------------------
# Required credentials — no built-in defaults; must come from .e2e-automation
# or the environment. Script exits immediately if either is missing.
# ---------------------------------------------------------------------------
if [[ -z "${E2E_ADMIN_USER:-}" ]]; then
    echo "ERROR: E2E_ADMIN_USER is not set." >&2
    echo "       Set it in .e2e-automation or export it in your environment." >&2
    echo "       See .e2e-automation.template for the full configuration reference." >&2
    exit 1
fi

if [[ -z "${E2E_ADMIN_PASS:-}" ]]; then
    echo "ERROR: E2E_ADMIN_PASS is not set." >&2
    echo "       Set it in .e2e-automation or export it in your environment." >&2
    echo "       See .e2e-automation.template for the full configuration reference." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# VoyageAI key — intentionally optional at the script level.
# Falls back to the shell VOYAGE_API_KEY if E2E_VOYAGE_API_KEY is not set.
# Individual tests that require it will report a failure if neither is present.
# ---------------------------------------------------------------------------
: "${E2E_VOYAGE_API_KEY:=${VOYAGE_API_KEY:-}}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
ONLY_PHASE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --phase)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --phase requires a value (1, 2, 3, or 4)" >&2
                exit 1
            fi
            ONLY_PHASE="$2"
            if [[ ! "$ONLY_PHASE" =~ ^[1-4]$ ]]; then
                echo "ERROR: --phase value must be 1, 2, 3, or 4 (got: '$ONLY_PHASE')" >&2
                exit 1
            fi
            shift 2
            ;;
        --help|-h)
            sed -n '2,25p' "$0" | sed 's/^# *//'
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            echo "Usage: $0 [--phase 1|2|3|4] [--help]" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
_green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
_red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
_yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
_bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# Server subprocess state
# ---------------------------------------------------------------------------
SERVER_PID=""

cleanup_server() {
    if [[ -n "${SERVER_PID:-}" ]]; then
        _yellow "Stopping server subprocess (PID $SERVER_PID)..."
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        SERVER_PID=""
    fi
}

trap cleanup_server EXIT

# ---------------------------------------------------------------------------
# Helper: clone seed repo into cache (idempotent — skips if .git exists)
# ---------------------------------------------------------------------------
clone_seed_repo() {
    local name="$1"
    local url="$2"
    local tag="$3"
    local dest="$E2E_SEED_CACHE_DIR/$name"

    if [[ -d "$dest/.git" ]]; then
        _yellow "  Cache hit — $name already at $dest"
        return 0
    fi

    _yellow "  Cloning $name from $url ..."
    if ! git clone "$url" "$dest"; then
        _red "ERROR: Failed to clone $name from $url"
        exit 1
    fi

    if [[ "$tag" != "HEAD" ]]; then
        _yellow "  Checking out tag $tag in $name ..."
        if ! git -C "$dest" -c advice.detachedHead=false checkout "tags/$tag"; then
            _red "ERROR: Failed to checkout tag $tag in $name"
            exit 1
        fi
    fi

    _green "  Cloned $name OK"
}

# ---------------------------------------------------------------------------
# Helper: copy seed repo to per-run working copy (always fresh)
# ---------------------------------------------------------------------------
copy_seed_repo() {
    local name="$1"
    local dest="$E2E_WORK_DIR/$name"

    rm -rf "$dest"
    cp -r "$E2E_SEED_CACHE_DIR/$name" "$dest"
    _yellow "  Copied $name -> $dest"
}

# ---------------------------------------------------------------------------
# Helper: wait for server readiness by polling GET /health
# ---------------------------------------------------------------------------
wait_for_server() {
    local url="http://${E2E_SERVER_HOST}:${E2E_SERVER_PORT}/health"
    local elapsed=0

    _yellow "  Waiting for server at $url (timeout ${E2E_SERVER_READINESS_TIMEOUT}s)..."
    while [[ $elapsed -lt $E2E_SERVER_READINESS_TIMEOUT ]]; do
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
        # Any non-5xx HTTP response (including 401) means the server is bound
        # and accepting connections. 000 = curl failed to connect.
        if [[ "$code" != "000" ]] && [[ "$code" -ge 100 ]] && [[ "$code" -lt 500 ]]; then
            _green "  Server ready after ${elapsed}s (HTTP $code)"
            return 0
        fi
        sleep "$E2E_SERVER_READINESS_POLL"
        elapsed=$((elapsed + E2E_SERVER_READINESS_POLL))
    done

    _red "ERROR: Server did not become ready within ${E2E_SERVER_READINESS_TIMEOUT}s"
    return 1
}

# ---------------------------------------------------------------------------
# Helper: run pytest for a phase directory; returns pytest exit code
# ---------------------------------------------------------------------------
run_phase() {
    local phase_num="$1"
    local phase_name="$2"
    local test_dir="$3"

    _bold "=== Phase $phase_num: $phase_name ==="

    if [[ ! -d "$SCRIPT_DIR/$test_dir" ]]; then
        _yellow "  Directory $test_dir does not exist — skipping phase $phase_num"
        return 0
    fi

    local pytest_exit=0
    PYTHONPATH="$SCRIPT_DIR/src" \
    CIDX_TEST_FAST_SQLITE=1 \
    E2E_SERVER_PORT="$E2E_SERVER_PORT" \
    E2E_SERVER_HOST="$E2E_SERVER_HOST" \
    E2E_ADMIN_USER="$E2E_ADMIN_USER" \
    E2E_ADMIN_PASS="$E2E_ADMIN_PASS" \
    E2E_SEED_CACHE_DIR="$E2E_SEED_CACHE_DIR" \
    E2E_SERVER_DATA_DIR="$E2E_SERVER_DATA_DIR" \
    E2E_WORK_DIR="$E2E_WORK_DIR" \
    E2E_VOYAGE_API_KEY="$E2E_VOYAGE_API_KEY" \
    VOYAGE_API_KEY="${E2E_VOYAGE_API_KEY:-${VOYAGE_API_KEY:-}}" \
        python3 -m pytest "$SCRIPT_DIR/$test_dir" -v --tb=short || pytest_exit=$?

    if [[ $pytest_exit -eq 5 ]]; then
        _yellow "  No tests collected in $test_dir — treating as success (exit 5)"
        return 0
    fi
    return $pytest_exit
}

# ---------------------------------------------------------------------------
# Helper: record phase result and apply stop-on-failure logic.
# Updates OVERALL_EXIT. Exits immediately when running all phases and phase fails.
# ---------------------------------------------------------------------------
handle_phase_result() {
    local phase_num="$1"
    local phase_exit="$2"

    if [[ $phase_exit -ne 0 ]]; then
        _red "Phase $phase_num FAILED"
        OVERALL_EXIT=1
        if [[ -z "$ONLY_PHASE" ]]; then
            _red "Stopping — subsequent phases skipped."
            exit 1
        fi
    else
        _green "Phase $phase_num PASSED"
    fi
}

# ---------------------------------------------------------------------------
# Helper: start Phase 4 live server subprocess
# ---------------------------------------------------------------------------
start_phase4_server() {
    _yellow "  Starting uvicorn on ${E2E_SERVER_HOST}:${E2E_SERVER_PORT}..."
    PYTHONPATH="$SCRIPT_DIR/src" \
    CIDX_TEST_FAST_SQLITE=1 \
    CIDX_SERVER_DATA_DIR="$E2E_SERVER_DATA_DIR" \
    VOYAGE_API_KEY="${E2E_VOYAGE_API_KEY:-${VOYAGE_API_KEY:-}}" \
        python3 -m uvicorn code_indexer.server.app:app \
            --host "$E2E_SERVER_HOST" \
            --port "$E2E_SERVER_PORT" \
            --log-level warning \
            --workers 1 > "$E2E_SERVER_DATA_DIR/server.log" 2>&1 &
    SERVER_PID=$!
    _yellow "  Server PID: $SERVER_PID"
}

# ---------------------------------------------------------------------------
# Phase definitions: "<num>|<label>|<test_dir>"
# ---------------------------------------------------------------------------
PHASE_DEFS=(
    "1|CLI Standalone|tests/e2e/cli_standalone"
    "2|CLI Daemon|tests/e2e/cli_daemon"
    "3|Server In-Process (TestClient)|tests/e2e/server"
    "4|CLI Remote (live server)|tests/e2e/cli_remote"
)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
_bold "======================================"
_bold " CIDX E2E Automation Suite"
_bold "======================================"
echo ""

# Ensure base directories exist
mkdir -p "$E2E_SEED_CACHE_DIR"
mkdir -p "$E2E_WORK_DIR"

# Wipe server data dir (clean slate each run)
_yellow "Wiping server data dir: $E2E_SERVER_DATA_DIR"
rm -rf "$E2E_SERVER_DATA_DIR"
mkdir -p "$E2E_SERVER_DATA_DIR"

# Clone seed repos into persistent cache
_bold "--- Seed Repo Cache ---"
clone_seed_repo "markupsafe" "$E2E_MARKUPSAFE_URL" "$E2E_MARKUPSAFE_TAG"
clone_seed_repo "type-fest"  "$E2E_TYPEFEST_URL"   "$E2E_TYPEFEST_TAG"
clone_seed_repo "tries"      "$E2E_TRIES_URL"       "$E2E_TRIES_TAG"
echo ""

# Copy fresh working copies for this run
_bold "--- Copying Working Copies ---"
copy_seed_repo "markupsafe"
copy_seed_repo "type-fest"
copy_seed_repo "tries"
echo ""

# Run phases
OVERALL_EXIT=0

for phase_def in "${PHASE_DEFS[@]}"; do
    IFS='|' read -r phase_num phase_label phase_dir <<< "$phase_def"

    # Skip if --phase was specified and this is not the target phase
    if [[ -n "$ONLY_PHASE" && "$ONLY_PHASE" != "$phase_num" ]]; then
        continue
    fi

    if [[ "$phase_num" == "4" ]]; then
        # Phase 4 requires a live server: start it, run tests, then stop it
        _bold "=== Phase 4: $phase_label ==="
        start_phase4_server

        phase4_exit=0
        if ! wait_for_server; then
            _red "Phase 4 FAILED — server did not start"
            phase4_exit=1
        else
            run_phase "$phase_num" "$phase_label" "$phase_dir" || phase4_exit=$?
        fi

        cleanup_server
        handle_phase_result "$phase_num" "$phase4_exit"
    else
        phase_exit=0
        run_phase "$phase_num" "$phase_label" "$phase_dir" || phase_exit=$?
        handle_phase_result "$phase_num" "$phase_exit"
    fi

    echo ""
done

# Summary
echo ""
_bold "======================================"
if [[ $OVERALL_EXIT -eq 0 ]]; then
    _green " ALL PHASES PASSED"
else
    _red " ONE OR MORE PHASES FAILED"
fi
_bold "======================================"

exit $OVERALL_EXIT
