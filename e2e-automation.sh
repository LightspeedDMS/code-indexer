#!/usr/bin/env bash
# e2e-automation.sh — CIDX Comprehensive E2E Test Suite Orchestrator
#
# Runs all 6 E2E phases sequentially:
#   Phase 1: CLI standalone  (tests/e2e/cli_standalone/)
#   Phase 2: CLI daemon      (tests/e2e/cli_daemon/)
#   Phase 3: Server in-proc  (tests/e2e/server/) via FastAPI TestClient
#   Phase 4: CLI remote      (tests/e2e/cli_remote/) against live uvicorn subprocess
#   Phase 5: Resiliency      (tests/e2e/phase5_resiliency/) against fault-injection server
#   Phase 6: PG Parity       (tests/e2e/pg_parity/) against ephemeral PostgreSQL cluster
#
# Phase 6 requires PostgreSQL server utilities (initdb, pg_ctl) to be installed.
# If they are absent the phase is LOUD-SKIPPED with a clear message.
# In CI, install postgresql-server (or equivalent) as a prerequisite.
#
# Usage:
#   ./e2e-automation.sh             # Run all phases
#   ./e2e-automation.sh --phase 1   # Run single phase (1-6)
#
# Configuration:
#   Copy .e2e-automation.template to .e2e-automation and fill in values.
#   Non-sensitive defaults are baked into this script. Credentials (E2E_ADMIN_USER,
#   E2E_ADMIN_PASS) must be supplied via .e2e-automation or environment variables —
#   they have no built-in defaults and the script exits immediately if missing.
#   E2E_VOYAGE_API_KEY is optional at the script level; it falls back to the shell
#   VOYAGE_API_KEY env var. Individual tests that require it will fail if neither is set.
#   Phase 5 additionally requires CO_API_KEY (or E2E_COHERE_API_KEY) for Cohere reranking.
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
: "${E2E_MOCK_REPO_URL:=https://github.com/LightspeedDMS/code-indexer-mock-test-repo.git}"
: "${E2E_MOCK_REPO_TAG:=HEAD}"
: "${E2E_SCIP_MOCK_URL:=https://github.com/LightspeedDMS/scip-python-mock.git}"
: "${E2E_SCIP_MOCK_TAG:=HEAD}"
# Timeout defaults — overridable via .e2e-automation
: "${E2E_SERVER_READINESS_TIMEOUT:=30}"
: "${E2E_SERVER_READINESS_POLL:=1}"

# Phase 5 fault server defaults (separate port/data dir from Phase 4)
: "${E2E_FAULT_SERVER_PORT:=8900}"
: "${E2E_FAULT_SERVER_HOST:=127.0.0.1}"
: "${E2E_FAULT_SERVER_DATA_DIR:=$HOME/.tmp/cidx-e2e-fault-server-data}"
: "${E2E_FAULT_SERVER_READINESS_TIMEOUT:=60}"
# Seconds to wait for dual-provider (VoyageAI + Cohere) golden repo indexing in Phase 5.
# Longer than single-provider Phase 4 timeout because Cohere reranking adds latency.
: "${E2E_FAULT_GOLDEN_REPO_JOB_TIMEOUT:=300}"

# Phase 6 PostgreSQL parity server defaults (separate port/data dirs from Phases 4 & 5)
# E2E_PG_DATA: directory for the ephemeral PostgreSQL cluster (data dir + UNIX socket)
# E2E_PG_SERVER_DATA_DIR: CIDX server data dir for the Phase 6 uvicorn instance
: "${E2E_PG_SERVER_PORT:=8901}"
: "${E2E_PG_SERVER_HOST:=127.0.0.1}"
: "${E2E_PG_SERVER_DATA_DIR:=$HOME/.tmp/cidx-e2e-pg-server-data}"
: "${E2E_PG_DATA:=$HOME/.tmp/cidx-e2e-pg-cluster}"
: "${E2E_PG_DB_NAME:=cidx_e2e}"
: "${E2E_PG_SERVER_READINESS_TIMEOUT:=60}"

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
                echo "ERROR: --phase requires a value (1, 2, 3, 4, 5, or 6)" >&2
                exit 1
            fi
            ONLY_PHASE="$2"
            if [[ ! "$ONLY_PHASE" =~ ^[1-6]$ ]]; then
                echo "ERROR: --phase value must be 1, 2, 3, 4, 5, or 6 (got: '$ONLY_PHASE')" >&2
                exit 1
            fi
            shift 2
            ;;
        --help|-h)
            sed -n '2,30p' "$0" | sed 's/^# *//'
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            echo "Usage: $0 [--phase 1|2|3|4|5|6] [--help]" >&2
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
# Server subprocess state (Phase 4, Phase 5, and Phase 6 use separate PIDs)
# ---------------------------------------------------------------------------
SERVER_PID=""
FAULT_SERVER_PID=""
PG_SERVER_PID=""       # Phase 6 uvicorn (PG-backed)
PG_CLUSTER_STARTED=""  # Set to "yes" when pg_ctl cluster is running

cleanup_all_servers() {
    # Stop Phase 4 server if running
    if [[ -n "${SERVER_PID:-}" ]]; then
        _yellow "Stopping Phase 4 server subprocess (PID $SERVER_PID)..."
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        SERVER_PID=""
    fi
    # Stop Phase 5 fault server if running
    if [[ -n "${FAULT_SERVER_PID:-}" ]]; then
        _yellow "Stopping Phase 5 fault server subprocess (PID $FAULT_SERVER_PID)..."
        kill "$FAULT_SERVER_PID" 2>/dev/null || true
        wait "$FAULT_SERVER_PID" 2>/dev/null || true
        FAULT_SERVER_PID=""
    fi
    # Stop Phase 6 PG-backed uvicorn if running
    if [[ -n "${PG_SERVER_PID:-}" ]]; then
        _yellow "Stopping Phase 6 PG server subprocess (PID $PG_SERVER_PID)..."
        kill "$PG_SERVER_PID" 2>/dev/null || true
        wait "$PG_SERVER_PID" 2>/dev/null || true
        PG_SERVER_PID=""
    fi
    # Stop Phase 6 ephemeral PostgreSQL cluster if running
    if [[ "${PG_CLUSTER_STARTED:-}" == "yes" ]] && [[ -d "${E2E_PG_DATA:-}" ]]; then
        _yellow "Stopping ephemeral PostgreSQL cluster at $E2E_PG_DATA..."
        pg_ctl -D "$E2E_PG_DATA/pgdata" -m immediate stop 2>/dev/null || true
        PG_CLUSTER_STARTED=""
    fi
    # Wipe Phase 6 ephemeral PG data dir (eliminate leaked cluster data)
    if [[ -d "${E2E_PG_DATA:-}" ]]; then
        _yellow "Wiping ephemeral PG data dir: $E2E_PG_DATA"
        rm -rf "$E2E_PG_DATA"
    fi
}

# Number of newest pytest temp dirs to keep when pruning (older ones are removed).
PYTEST_DIRS_TO_KEEP=3

# ---------------------------------------------------------------------------
# Helper: reap stale test daemons whose cmdline is rooted under /tmp/.
#
# SAFETY SCOPE: only processes whose command line contains "code_indexer.daemon"
# AND references a path under /tmp/ are killed.  A developer's real daemon
# (rooted in ~/... or any non-/tmp path) is NEVER touched.  This is an explicit
# design invariant — do NOT change this filter to a blanket pkill.
#
# Sets caller-scoped variable _REAP_COUNT to the number of processes reaped.
# Idempotent: silent no-op when no matching processes exist.
# Never aborts the suite (every kill path uses || true).
# ---------------------------------------------------------------------------
_reap_tmp_test_daemons() {
    _REAP_COUNT=0
    local pids=()
    local line pid rest

    # pgrep -af prints "PID full-cmdline" — may produce no output if none running.
    # Use process substitution to avoid a subshell that would discard the array.
    while IFS= read -r line; do
        pid="${line%% *}"
        rest="${line#* }"
        # Only reap if the cmdline references a path under /tmp/
        if [[ "$rest" == */tmp/* ]]; then
            pids+=("$pid")
        fi
    done < <(pgrep -af 'code_indexer\.daemon' 2>/dev/null || true)

    _REAP_COUNT="${#pids[@]}"
    if [[ "$_REAP_COUNT" -eq 0 ]]; then
        return 0
    fi

    _yellow "  Reaping $_REAP_COUNT stale test daemon(s) with /tmp/ paths..."
    # SIGTERM first
    for pid in "${pids[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    # Brief grace period, then SIGKILL any survivors
    sleep 1
    for pid in "${pids[@]}"; do
        # kill -0 checks if process still exists; if so, escalate
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
}

# ---------------------------------------------------------------------------
# Helper: idempotent environment reset — call at STARTUP and via EXIT trap.
#
# 1. Reaps stale code_indexer.daemon processes whose paths are under /tmp/.
# 2. Prunes old pytest temp dirs to reclaim space (keeps newest PYTEST_DIRS_TO_KEEP).
#
# Safe to call repeatedly; never aborts the suite on failure.
# ---------------------------------------------------------------------------
reset_test_environment() {
    # --- Step 1: reap stale test daemons (count returned in _REAP_COUNT) ---
    _reap_tmp_test_daemons
    local reaped="$_REAP_COUNT"

    # --- Step 2: prune old pytest temp dirs (keep newest PYTEST_DIRS_TO_KEEP) ---
    local pruned=0
    local pytest_base="/tmp/pytest-of-${USER:-jsbattig}"
    if [[ -d "$pytest_base" ]]; then
        # List pytest-NN dirs sorted newest-first, skip the N newest, remove the rest
        local dirs_to_remove=()
        local idx=0
        while IFS= read -r dir; do
            idx=$((idx + 1))
            if [[ $idx -gt $PYTEST_DIRS_TO_KEEP ]]; then
                dirs_to_remove+=("$dir")
            fi
        done < <(ls -dt "${pytest_base}"/pytest-* 2>/dev/null || true)

        pruned="${#dirs_to_remove[@]}"
        for dir in "${dirs_to_remove[@]}"; do
            # Extra safety: only remove paths that are genuinely under pytest_base
            if [[ "$dir" == "${pytest_base}/"* ]]; then
                rm -rf "$dir" 2>/dev/null || true
            fi
        done
    fi

    _bold "Resetting test environment (reaped $reaped stale daemon(s), pruned $pruned old temp dir(s))"
}

# Single composite EXIT trap — bash EXIT traps are global, not per-phase.
# Calls reset_test_environment so THIS run's daemons are reaped on exit,
# preventing accumulation for the next run.
cleanup_all_servers_and_reset() {
    cleanup_all_servers
    reset_test_environment
}
trap cleanup_all_servers_and_reset EXIT

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
# Helper: wait for server readiness by probing /health AND /auth/login.
#
# Story #1123 AC2: a server that responds on /health but cannot authenticate
# (persistent 503 on auth, broken startup) MUST fail readiness.
# Readiness requires BOTH:
#   1. GET /health returns HTTP < 500
#   2. POST /auth/login (JSON body) returns HTTP 200 with an access_token
# ---------------------------------------------------------------------------
wait_for_server() {
    local health_url="http://${E2E_SERVER_HOST}:${E2E_SERVER_PORT}/health"
    local login_url="http://${E2E_SERVER_HOST}:${E2E_SERVER_PORT}/auth/login"
    local elapsed=0

    _yellow "  Waiting for server at $health_url (timeout ${E2E_SERVER_READINESS_TIMEOUT}s)..."
    _yellow "  Readiness requires: /health non-5xx AND /auth/login returns 200+token"
    while [[ $elapsed -lt $E2E_SERVER_READINESS_TIMEOUT ]]; do
        # Step 1: health check
        local health_code
        health_code=$(curl -s -o /dev/null -w "%{http_code}" "$health_url" 2>/dev/null || echo "000")
        if [[ "$health_code" == "000" ]] || [[ "$health_code" -ge 500 ]]; then
            sleep "$E2E_SERVER_READINESS_POLL"
            elapsed=$((elapsed + E2E_SERVER_READINESS_POLL))
            continue
        fi

        # Step 2: authenticated login probe (JSON body per CLAUDE.md E2E gotchas)
        local login_response
        login_response=$(curl -s -w "\n%{http_code}" \
            -X POST "$login_url" \
            -H "Content-Type: application/json" \
            -d "{\"username\":\"${E2E_ADMIN_USER}\",\"password\":\"${E2E_ADMIN_PASS}\"}" \
            2>/dev/null || echo -e "\n000")
        local login_code
        login_code=$(echo "$login_response" | tail -n1)
        local login_body
        login_body=$(echo "$login_response" | head -n-1)

        if [[ "$login_code" == "200" ]] && echo "$login_body" | grep -q "access_token"; then
            _green "  Server ready after ${elapsed}s (health=$health_code, auth=200+token)"
            return 0
        fi

        logger_hint="health=$health_code auth=$login_code"
        _yellow "    Not ready yet (${logger_hint}) — retrying..."
        sleep "$E2E_SERVER_READINESS_POLL"
        elapsed=$((elapsed + E2E_SERVER_READINESS_POLL))
    done

    _red "ERROR: Server did not become ready within ${E2E_SERVER_READINESS_TIMEOUT}s"
    _red "       (Required: /health non-5xx AND /auth/login returns 200+token)"
    return 1
}

# ---------------------------------------------------------------------------
# Helper: run pytest for a phase directory; returns pytest exit code.
# Accumulates skip lines into SKIP_LINES for the end-of-run SKIP SUMMARY.
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

    # Capture pytest output to a temp file so we can extract skip lines
    # while still streaming to stdout (-v --tb=short for normal visibility).
    local phase_output_file
    phase_output_file=$(mktemp)

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
    CO_API_KEY="${E2E_COHERE_API_KEY:-${CO_API_KEY:-}}" \
    E2E_FAULT_SERVER_PORT="$E2E_FAULT_SERVER_PORT" \
    E2E_FAULT_SERVER_HOST="$E2E_FAULT_SERVER_HOST" \
    E2E_FAULT_SERVER_DATA_DIR="$E2E_FAULT_SERVER_DATA_DIR" \
    E2E_FAULT_GOLDEN_REPO_JOB_TIMEOUT="$E2E_FAULT_GOLDEN_REPO_JOB_TIMEOUT" \
        python3 -m pytest "$SCRIPT_DIR/$test_dir" -v --tb=short -rs 2>&1 \
        | tee "$phase_output_file" \
        || pytest_exit=$?

    # Collect skip lines (lines starting with "SKIPPED" in -rs short-test-summary
    # output, or "SKIP" marker lines) into the global SKIP_LINES accumulator.
    while IFS= read -r line; do
        case "$line" in
            SKIPPED*|"  SKIPPED"*|"SKIP "*|"s "*)
                SKIP_LINES+=("Phase $phase_num ($phase_name): $line")
                ;;
        esac
    done < "$phase_output_file"
    rm -f "$phase_output_file"

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
# Helper: write Phase 5 bootstrap config.json with fault injection enabled
# Both fault_injection_enabled AND fault_injection_nonprod_ack are required:
# startup.py Scenario 4 calls sys.exit(1) if either flag is missing.
# ---------------------------------------------------------------------------
write_fault_bootstrap_config() {
    mkdir -p "$E2E_FAULT_SERVER_DATA_DIR"
    cat > "$E2E_FAULT_SERVER_DATA_DIR/config.json" <<CONFIG_EOF
{
  "server_dir": "$E2E_FAULT_SERVER_DATA_DIR",
  "host": "$E2E_FAULT_SERVER_HOST",
  "port": $E2E_FAULT_SERVER_PORT,
  "fault_injection_enabled": true,
  "fault_injection_nonprod_ack": true
}
CONFIG_EOF
    _yellow "  Wrote fault bootstrap config.json to $E2E_FAULT_SERVER_DATA_DIR/config.json"
}

# ---------------------------------------------------------------------------
# Helper: start Phase 5 fault injection server subprocess
# ---------------------------------------------------------------------------
start_fault_server() {
    _yellow "  Starting fault server on ${E2E_FAULT_SERVER_HOST}:${E2E_FAULT_SERVER_PORT}..."
    PYTHONPATH="$SCRIPT_DIR/src" \
    CIDX_SERVER_DATA_DIR="$E2E_FAULT_SERVER_DATA_DIR" \
    VOYAGE_API_KEY="${E2E_VOYAGE_API_KEY:-${VOYAGE_API_KEY:-}}" \
    CO_API_KEY="${E2E_COHERE_API_KEY:-${CO_API_KEY:-}}" \
        python3 -m uvicorn code_indexer.server.app:app \
            --host "$E2E_FAULT_SERVER_HOST" \
            --port "$E2E_FAULT_SERVER_PORT" \
            --log-level warning \
            --workers 1 > "$E2E_FAULT_SERVER_DATA_DIR/server.log" 2>&1 &
    FAULT_SERVER_PID=$!
    _yellow "  Fault server PID: $FAULT_SERVER_PID"
}

# ---------------------------------------------------------------------------
# Helper: wait for fault server readiness by probing /health AND /auth/login.
#
# Story #1123 AC2 consistency: same hardening as wait_for_server so Phase 5
# also requires a functional authenticated endpoint, not just /health non-5xx.
# ---------------------------------------------------------------------------
wait_for_fault_server() {
    local health_url="http://${E2E_FAULT_SERVER_HOST}:${E2E_FAULT_SERVER_PORT}/health"
    local login_url="http://${E2E_FAULT_SERVER_HOST}:${E2E_FAULT_SERVER_PORT}/auth/login"
    local elapsed=0

    _yellow "  Waiting for fault server at $health_url (timeout ${E2E_FAULT_SERVER_READINESS_TIMEOUT}s)..."
    _yellow "  Readiness requires: /health non-5xx AND /auth/login returns 200+token"
    while [[ $elapsed -lt $E2E_FAULT_SERVER_READINESS_TIMEOUT ]]; do
        # Step 1: health check
        local health_code
        health_code=$(curl -s -o /dev/null -w "%{http_code}" "$health_url" 2>/dev/null || echo "000")
        if [[ "$health_code" == "000" ]] || [[ "$health_code" -ge 500 ]]; then
            sleep "$E2E_SERVER_READINESS_POLL"
            elapsed=$((elapsed + E2E_SERVER_READINESS_POLL))
            continue
        fi

        # Step 2: authenticated login probe (JSON body per CLAUDE.md E2E gotchas)
        local login_response
        login_response=$(curl -s -w "\n%{http_code}" \
            -X POST "$login_url" \
            -H "Content-Type: application/json" \
            -d "{\"username\":\"${E2E_ADMIN_USER}\",\"password\":\"${E2E_ADMIN_PASS}\"}" \
            2>/dev/null || echo -e "\n000")
        local login_code
        login_code=$(echo "$login_response" | tail -n1)
        local login_body
        login_body=$(echo "$login_response" | head -n-1)

        if [[ "$login_code" == "200" ]] && echo "$login_body" | grep -q "access_token"; then
            _green "  Fault server ready after ${elapsed}s (health=$health_code, auth=200+token)"
            return 0
        fi

        _yellow "    Not ready yet (health=$health_code auth=$login_code) — retrying..."
        sleep "$E2E_SERVER_READINESS_POLL"
        elapsed=$((elapsed + E2E_SERVER_READINESS_POLL))
    done

    _red "ERROR: Fault server did not become ready within ${E2E_FAULT_SERVER_READINESS_TIMEOUT}s"
    _red "       (Required: /health non-5xx AND /auth/login returns 200+token)"
    _red "       Check log: $E2E_FAULT_SERVER_DATA_DIR/server.log"
    return 1
}

# ---------------------------------------------------------------------------
# Helper: provision an ephemeral PostgreSQL cluster over a UNIX socket.
#
# Uses initdb + pg_ctl -w.  The cluster listens ONLY on a UNIX socket inside
# $E2E_PG_DATA (-k "$E2E_PG_DATA" -h '') to eliminate TCP port races.
# Creates the $E2E_PG_DB_NAME database and configures trust auth.
# Sets PG_CLUSTER_STARTED="yes" so the EXIT trap can stop it.
# ---------------------------------------------------------------------------
provision_pg_cluster() {
    _yellow "  Provisioning ephemeral PostgreSQL cluster at $E2E_PG_DATA..."

    # E2E_PGDATA is the actual PostgreSQL data directory — a SUBDIR of the
    # container ($E2E_PG_DATA).  The container holds logs and the UNIX socket.
    # Keeping logs OUTSIDE the data dir is mandatory: writing initdb.log into
    # the target directory before initdb runs makes initdb see a non-empty
    # directory and fail with "directory ... exists but is not empty".
    local E2E_PGDATA="$E2E_PG_DATA/pgdata"

    # Wipe any leftover data dir from a previous failed run
    rm -rf "$E2E_PG_DATA"
    mkdir -p "$E2E_PG_DATA"
    # pgdata subdir must NOT be pre-created — initdb creates it itself

    # initdb: create a fresh cluster (log in container, data in subdir)
    if ! initdb -D "$E2E_PGDATA" --auth=trust --no-locale -E UTF8 \
            > "$E2E_PG_DATA/initdb.log" 2>&1; then
        _red "ERROR: initdb failed. Log: $E2E_PG_DATA/initdb.log"
        return 1
    fi
    _yellow "  initdb OK"

    # Configure trust auth for local socket connections only (no TCP)
    cat > "$E2E_PGDATA/pg_hba.conf" <<HBA_EOF
# TYPE  DATABASE  USER  ADDRESS  METHOD
local   all       all            trust
host    all       all  127.0.0.1/32  reject
host    all       all  ::1/128       reject
HBA_EOF

    # Start the cluster: UNIX socket only (-k = socket dir in container, -h '' = no TCP)
    if ! pg_ctl -D "$E2E_PGDATA" -w \
            -o "-k '$E2E_PG_DATA' -h ''" \
            -l "$E2E_PG_DATA/postgres.log" start; then
        _red "ERROR: pg_ctl start failed. Log: $E2E_PG_DATA/postgres.log"
        return 1
    fi
    PG_CLUSTER_STARTED="yes"
    _yellow "  PostgreSQL cluster started (UNIX socket only)"

    # Create the cidx_e2e database
    local pg_dsn_base="postgresql:///postgres?host=$E2E_PG_DATA"
    if ! python3 -c "
import psycopg, sys
conn = psycopg.connect('$pg_dsn_base', autocommit=True)
conn.execute('CREATE DATABASE $E2E_PG_DB_NAME')
conn.close()
print('Database $E2E_PG_DB_NAME created')
" 2>&1; then
        _red "ERROR: Failed to create database $E2E_PG_DB_NAME"
        return 1
    fi
    _green "  Database $E2E_PG_DB_NAME created OK"
}

# ---------------------------------------------------------------------------
# Helper: write Phase 6 bootstrap config.json with storage_mode=postgres
# ---------------------------------------------------------------------------
write_pg_bootstrap_config() {
    local pg_dsn="postgresql:///${E2E_PG_DB_NAME}?host=${E2E_PG_DATA}"
    mkdir -p "$E2E_PG_SERVER_DATA_DIR"
    cat > "$E2E_PG_SERVER_DATA_DIR/config.json" <<CONFIG_EOF
{
  "server_dir": "$E2E_PG_SERVER_DATA_DIR",
  "host": "$E2E_PG_SERVER_HOST",
  "port": $E2E_PG_SERVER_PORT,
  "storage_mode": "postgres",
  "postgres_dsn": "$pg_dsn"
}
CONFIG_EOF
    _yellow "  Wrote PG bootstrap config.json to $E2E_PG_SERVER_DATA_DIR/config.json"
    _yellow "  DSN: $pg_dsn"
}

# ---------------------------------------------------------------------------
# Helper: start Phase 6 PG-backed uvicorn subprocess
# ---------------------------------------------------------------------------
start_pg_server() {
    _yellow "  Starting PG-backed uvicorn on ${E2E_PG_SERVER_HOST}:${E2E_PG_SERVER_PORT}..."
    PYTHONPATH="$SCRIPT_DIR/src" \
    CIDX_SERVER_DATA_DIR="$E2E_PG_SERVER_DATA_DIR" \
    VOYAGE_API_KEY="${E2E_VOYAGE_API_KEY:-${VOYAGE_API_KEY:-}}" \
        python3 -m uvicorn code_indexer.server.app:app \
            --host "$E2E_PG_SERVER_HOST" \
            --port "$E2E_PG_SERVER_PORT" \
            --log-level warning \
            --workers 1 > "$E2E_PG_SERVER_DATA_DIR/server.log" 2>&1 &
    PG_SERVER_PID=$!
    _yellow "  PG server PID: $PG_SERVER_PID"
}

# ---------------------------------------------------------------------------
# Helper: wait for Phase 6 PG-backed server readiness (same hardening as
# wait_for_server: /health non-5xx AND /auth/login returns 200+token).
# ---------------------------------------------------------------------------
wait_for_pg_server() {
    local health_url="http://${E2E_PG_SERVER_HOST}:${E2E_PG_SERVER_PORT}/health"
    local login_url="http://${E2E_PG_SERVER_HOST}:${E2E_PG_SERVER_PORT}/auth/login"
    local elapsed=0

    _yellow "  Waiting for PG server at $health_url (timeout ${E2E_PG_SERVER_READINESS_TIMEOUT}s)..."
    _yellow "  Readiness requires: /health non-5xx AND /auth/login returns 200+token"
    while [[ $elapsed -lt $E2E_PG_SERVER_READINESS_TIMEOUT ]]; do
        # Step 1: health check
        local health_code
        health_code=$(curl -s -o /dev/null -w "%{http_code}" "$health_url" 2>/dev/null || echo "000")
        if [[ "$health_code" == "000" ]] || [[ "$health_code" -ge 500 ]]; then
            sleep "$E2E_SERVER_READINESS_POLL"
            elapsed=$((elapsed + E2E_SERVER_READINESS_POLL))
            continue
        fi

        # Step 2: authenticated login probe (JSON body per CLAUDE.md E2E gotchas)
        local login_response
        login_response=$(curl -s -w "\n%{http_code}" \
            -X POST "$login_url" \
            -H "Content-Type: application/json" \
            -d "{\"username\":\"${E2E_ADMIN_USER}\",\"password\":\"${E2E_ADMIN_PASS}\"}" \
            2>/dev/null || echo -e "\n000")
        local login_code
        login_code=$(echo "$login_response" | tail -n1)
        local login_body
        login_body=$(echo "$login_response" | head -n-1)

        if [[ "$login_code" == "200" ]] && echo "$login_body" | grep -q "access_token"; then
            _green "  PG server ready after ${elapsed}s (health=$health_code, auth=200+token)"
            return 0
        fi

        _yellow "    Not ready yet (health=$health_code auth=$login_code) — retrying..."
        sleep "$E2E_SERVER_READINESS_POLL"
        elapsed=$((elapsed + E2E_SERVER_READINESS_POLL))
    done

    _red "ERROR: PG server did not become ready within ${E2E_PG_SERVER_READINESS_TIMEOUT}s"
    _red "       (Required: /health non-5xx AND /auth/login returns 200+token)"
    _red "       Check log: $E2E_PG_SERVER_DATA_DIR/server.log"
    return 1
}

# ---------------------------------------------------------------------------
# Phase definitions: "<num>|<label>|<test_dir>"
# ---------------------------------------------------------------------------
PHASE_DEFS=(
    "1|CLI Standalone|tests/e2e/cli_standalone"
    "2|CLI Daemon|tests/e2e/cli_daemon"
    "3|Server In-Process (TestClient)|tests/e2e/server"
    "4|CLI Remote (live server)|tests/e2e/cli_remote"
    "5|Resiliency|tests/e2e/phase5_resiliency"
    "6|PostgreSQL Parity|tests/e2e/pg_parity"
)

# ---------------------------------------------------------------------------
# Main — guarded so the script is SOURCE-SAFE: sourcing only defines functions
# (lets tests source wait_for_server); the suite runs only on direct execution.
# ---------------------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
_bold "======================================"
_bold " CIDX E2E Automation Suite"
_bold "======================================"
echo ""

# Ensure base directories exist
mkdir -p "$E2E_SEED_CACHE_DIR"
mkdir -p "$E2E_WORK_DIR"

# Reset environment: reap stale test daemons from prior runs + prune old temp dirs.
# This runs BEFORE any phase so a polluted environment left by a crashed/killed
# prior run is cleaned to a known-good state.  Also runs on EXIT (see trap above).
reset_test_environment

# Wipe server data dir (clean slate each run)
_yellow "Wiping server data dir: $E2E_SERVER_DATA_DIR"
rm -rf "$E2E_SERVER_DATA_DIR"
mkdir -p "$E2E_SERVER_DATA_DIR"

# Clone seed repos into persistent cache
_bold "--- Seed Repo Cache ---"
clone_seed_repo "markupsafe"    "$E2E_MARKUPSAFE_URL"  "$E2E_MARKUPSAFE_TAG"
clone_seed_repo "type-fest"     "$E2E_TYPEFEST_URL"    "$E2E_TYPEFEST_TAG"
clone_seed_repo "tries"         "$E2E_TRIES_URL"        "$E2E_TRIES_TAG"
clone_seed_repo "mock-test-repo" "$E2E_MOCK_REPO_URL"  "$E2E_MOCK_REPO_TAG"
clone_seed_repo "scip-python-mock" "$E2E_SCIP_MOCK_URL" "$E2E_SCIP_MOCK_TAG"
echo ""

# Copy fresh working copies for this run
_bold "--- Copying Working Copies ---"
copy_seed_repo "markupsafe"
copy_seed_repo "type-fest"
copy_seed_repo "tries"
copy_seed_repo "mock-test-repo"
copy_seed_repo "scip-python-mock"
echo ""

# Run phases
OVERALL_EXIT=0
# Accumulator for skip lines from all phases (populated by run_phase)
SKIP_LINES=()

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

        cleanup_all_servers
        handle_phase_result "$phase_num" "$phase4_exit"
    elif [[ "$phase_num" == "5" ]]; then
        # Phase 5 requires a fault-injection server: write config, start it, run tests, stop it
        _bold "=== Phase 5: $phase_label ==="
        _yellow "  Wiping fault server data dir: $E2E_FAULT_SERVER_DATA_DIR"
        rm -rf "$E2E_FAULT_SERVER_DATA_DIR"
        write_fault_bootstrap_config
        start_fault_server

        phase5_exit=0
        if ! wait_for_fault_server; then
            _red "Phase 5 FAILED — fault server did not start"
            _red "       Check log: $E2E_FAULT_SERVER_DATA_DIR/server.log"
            phase5_exit=1
        else
            run_phase "$phase_num" "$phase_label" "$phase_dir" || phase5_exit=$?
        fi

        cleanup_all_servers
        handle_phase_result "$phase_num" "$phase5_exit"
    elif [[ "$phase_num" == "6" ]]; then
        # Phase 6: PostgreSQL parity — requires initdb/pg_ctl (PostgreSQL server utilities).
        # LOUD-SKIP the whole phase if they are absent; in CI install postgresql-server first.
        _bold "=== Phase 6: $phase_label ==="
        if ! command -v initdb > /dev/null 2>&1 || ! command -v pg_ctl > /dev/null 2>&1; then
            _yellow "  SKIP: Phase 6 (PostgreSQL Parity) — initdb/pg_ctl not found on PATH."
            _yellow "        Install postgresql-server to enable this phase."
            _yellow "        In CI, add 'apt-get install postgresql' (or equivalent) as a prerequisite."
            SKIP_LINES+=("Phase 6 ($phase_label): SKIPPED — initdb/pg_ctl not on PATH (install postgresql-server)")
            continue
        fi

        _yellow "  Wiping PG server data dir: $E2E_PG_SERVER_DATA_DIR"
        rm -rf "$E2E_PG_SERVER_DATA_DIR"
        mkdir -p "$E2E_PG_SERVER_DATA_DIR"

        phase6_exit=0
        # Provision ephemeral PG cluster (initdb + pg_ctl + createdb)
        if ! provision_pg_cluster; then
            _red "Phase 6 FAILED — could not provision PostgreSQL cluster"
            _red "       Check: $E2E_PG_DATA/initdb.log or $E2E_PG_DATA/postgres.log"
            phase6_exit=1
        else
            # Write PG-pointed config.json and start uvicorn
            write_pg_bootstrap_config
            start_pg_server

            if ! wait_for_pg_server; then
                _red "Phase 6 FAILED — PG-backed server did not start"
                _red "       Check log: $E2E_PG_SERVER_DATA_DIR/server.log"
                phase6_exit=1
            else
                # Run the Phase 6 tests with the PG server env vars passed through
                PYTHONPATH="$SCRIPT_DIR/src" \
                E2E_PG_SERVER_HOST="$E2E_PG_SERVER_HOST" \
                E2E_PG_SERVER_PORT="$E2E_PG_SERVER_PORT" \
                E2E_PG_DATA="$E2E_PG_DATA" \
                E2E_PG_DB_NAME="$E2E_PG_DB_NAME" \
                E2E_PG_SERVER_DATA_DIR="$E2E_PG_SERVER_DATA_DIR" \
                E2E_ADMIN_USER="$E2E_ADMIN_USER" \
                E2E_ADMIN_PASS="$E2E_ADMIN_PASS" \
                E2E_SEED_CACHE_DIR="$E2E_SEED_CACHE_DIR" \
                E2E_GOLDEN_REPO_JOB_TIMEOUT="${E2E_FAULT_GOLDEN_REPO_JOB_TIMEOUT}" \
                VOYAGE_API_KEY="${E2E_VOYAGE_API_KEY:-${VOYAGE_API_KEY:-}}" \
                E2E_VOYAGE_API_KEY="${E2E_VOYAGE_API_KEY:-${VOYAGE_API_KEY:-}}" \
                    python3 -m pytest "$SCRIPT_DIR/$phase_dir" -v --tb=short -rs 2>&1 \
                    | tee /tmp/cidx-phase6-output.tmp \
                    || phase6_exit=$?

                # Accumulate skip lines
                while IFS= read -r line; do
                    case "$line" in
                        SKIPPED*|"  SKIPPED"*|"SKIP "*|"s "*)
                            SKIP_LINES+=("Phase 6 ($phase_label): $line")
                            ;;
                    esac
                done < /tmp/cidx-phase6-output.tmp
                rm -f /tmp/cidx-phase6-output.tmp

                if [[ $phase6_exit -eq 5 ]]; then
                    _yellow "  No tests collected in $phase_dir — treating as success (exit 5)"
                    phase6_exit=0
                fi
            fi
        fi

        cleanup_all_servers
        handle_phase_result "$phase_num" "$phase6_exit"
    else
        phase_exit=0
        run_phase "$phase_num" "$phase_label" "$phase_dir" || phase_exit=$?
        handle_phase_result "$phase_num" "$phase_exit"
    fi

    echo ""
done

# ---------------------------------------------------------------------------
# SKIP SUMMARY — Story #1123 AC1
# Emit a consolidated, loud summary of every test skipped across all phases
# and the reason for the skip.  No skipped coverage may be silently presented
# as passing.  This section appears BEFORE the final PASS/FAIL banner so it
# is impossible to miss.
# ---------------------------------------------------------------------------
echo ""
_bold "======================================"
_bold " SKIP SUMMARY"
_bold "======================================"
if [[ ${#SKIP_LINES[@]} -eq 0 ]]; then
    _green " No tests were skipped."
else
    _yellow " ${#SKIP_LINES[@]} skip(s) detected across all phases:"
    echo ""
    for skip_line in "${SKIP_LINES[@]}"; do
        _yellow "  SKIP: $skip_line"
    done
    echo ""
    _yellow " Review the skips above to ensure no required coverage was silently omitted."
    _yellow " To hard-fail on any skip in CI, set: CIDX_E2E_REQUIRE_ALL=true"
fi
_bold "======================================"

# Final summary
echo ""
_bold "======================================"
if [[ $OVERALL_EXIT -eq 0 ]]; then
    _green " ALL PHASES PASSED"
else
    _red " ONE OR MORE PHASES FAILED"
fi
_bold "======================================"

exit $OVERALL_EXIT
fi
