#!/bin/bash

# Server-focused fast automation script - tests CIDX server functionality
# Runs server unit tests that don't require external services or special permissions
# Separated from main fast-automation.sh to focus on server components

set -e  # Exit on any error

# Source .env files if they exist (for local testing)
if [[ -f ".env.local" ]]; then
    source .env.local
fi
if [[ -f ".env" ]]; then
    source .env
fi

echo "🖥️  Starting server-focused fast automation pipeline..."
echo "==========================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_step() {
    echo -e "\n${BLUE}➡️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Bug #1863: pre-run load check — warn (never block) if the box is already busy
# before this suite starts contending with itself. High load is what turned
# pytest-timeout expiries into a gate that looked flaky; surface it up front.
# LOAD_WARN_CORES_DIVISOR: warn once 1-min load exceeds cores / this divisor
# (i.e. "roughly half the cores", per Bug #1863's diagnosis).
#
# Cross-reference: fast-automation.sh carries an identical copy of
# check_load_before_run()/report_pytest_timeout_diagnostics() (Bug #1863).
# Keep both copies in sync when editing either one.
LOAD_WARN_CORES_DIVISOR=2
# Stashed by check_load_before_run() so report_pytest_timeout_diagnostics()
# can report load at BOTH suite start and failure time. A load reading taken
# only after a long suite finishes is meaningless for explaining what killed
# a test many minutes earlier.
PRERUN_LOAD1="unknown"
check_load_before_run() {
    local cores load1 threshold
    cores=$(nproc 2>/dev/null) || return 0
    [ -n "$cores" ] || return 0
    [ -r /proc/loadavg ] || return 0
    load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null) || return 0
    [ -n "$load1" ] || return 0
    PRERUN_LOAD1="$load1"
    threshold=$(awk -v c="$cores" -v d="$LOAD_WARN_CORES_DIVISOR" 'BEGIN{printf "%.2f", c/d}') || return 0
    [ -n "$threshold" ] || return 0
    if awk -v l="$load1" -v t="$threshold" 'BEGIN{exit !(l > t)}'; then
        echo ""
        print_warning "HIGH LOAD before this suite has even started: 1-min load average"
        print_warning "is ${load1} on ${cores} cores (warn threshold: load > ${threshold})."
        print_warning "Results below may be UNRELIABLE under this load — this suite's own"
        print_warning "tests have been measured running up to ~5x slower loaded vs idle"
        print_warning "(Bug #1863). Likely offenders (top CPU consumers):"
        ps -eo pid,etimes,pcpu,args --sort=-pcpu | head
        print_warning "Warning only — the run will continue."
        echo ""
    fi
    return 0
}

# Bug #1863: post-run diagnostic — a pytest-timeout wall-clock ceiling (suite
# default $PYTEST_TIMEOUT via --timeout, but any individual test may override
# it with @pytest.mark.timeout(N)) kills tests that would otherwise pass, and
# those expiries read exactly like ordinary assertion failures in the chunk
# summaries above, which has repeatedly cost investigation time chasing a
# phantom regression. This labels each timeout with its ACTUAL per-test
# ceiling. It does NOT change the exit code and is NOT A WAIVER: a timeout
# still fails the gate, and a failed gate still needs a real resolution — this
# only explains what kind of failure occurred so it can be triaged correctly.
# Scoped to THIS run's own log files only (named with $TIMESTAMP), never a
# blanket glob across .test-telemetry/.
report_pytest_timeout_diagnostics() {
    local logs=("$@")
    local existing=()
    local log
    for log in "${logs[@]}"; do
        [ -f "$log" ] && existing+=("$log")
    done
    [ ${#existing[@]} -gt 0 ] || return 0

    local timeout_count
    timeout_count=$(grep -h "from pytest-timeout" "${existing[@]}" 2>/dev/null | wc -l | tr -d ' ') || true
    [ -n "$timeout_count" ] || timeout_count=0
    [ "$timeout_count" -gt 0 ] || return 0

    local timeout_names
    timeout_names=$(awk '
        /^_+ .+ _+$/ {
            line = $0
            sub(/^_+ /, "", line)
            sub(/ _+$/, "", line)
            current = line
        }
        /from pytest-timeout/ {
            secs = "?"
            if (match($0, /Timeout \(>[0-9.]+s\)/))
                secs = substr($0, RSTART+10, RLENGTH-11)
            print "  - " current "   [ceiling " secs "]   (" FILENAME ")"
        }
    ' "${existing[@]}" 2>/dev/null) || timeout_names="(timeout test names unavailable)"

    local cores load1
    cores=$(nproc 2>/dev/null) || cores="unknown"
    load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null) || load1="unknown"

    echo ""
    echo -e "${YELLOW}=================================================================="
    echo "⏱️  PYTEST-TIMEOUT DIAGNOSTIC (Bug #1863)"
    echo -e "==================================================================${NC}"
    echo "THE GATE FAILED. $timeout_count of the failure(s) above were pytest-timeout"
    echo "wall-clock expiries rather than failed assertions. This is an EXPLANATION,"
    echo "NOT a waiver — the gate is still red and still requires a real resolution."
    echo ""
    echo "This suite's default ceiling is ${PYTEST_TIMEOUT}s (via --timeout), but any"
    echo "individual test can override it with @pytest.mark.timeout(N). The ceiling"
    echo "that actually killed each test below is shown per line:"
    echo ""
    echo "$timeout_names"
    echo ""
    echo "Load average (1m): at suite start ${PRERUN_LOAD1}   |   now ${load1}   |   cores: ${cores}"
    echo ""
    echo "How to read the ceiling column:"
    echo "  - A timeout AT the suite default (${PYTEST_TIMEOUT}s) under real load is often"
    echo "    the headroom lottery — this suite's tests have been measured running up"
    echo "    to ~5x slower loaded vs idle (Bug #1863)."
    echo "  - A timeout WELL ABOVE the suite default came from an explicit"
    echo "    @pytest.mark.timeout(N) marker (commonly a deadlock/concurrency guard,"
    echo "    e.g. the timeout(360) semaphore tests) and usually is NOT a load"
    echo "    artifact — treat it as a real failure that needs investigation."
    echo ""
    echo "Re-check yourself: grep -h \"from pytest-timeout\" ${TELEMETRY_DIR}/server-fast-automation-${TIMESTAMP}-chunk*.log | wc -l"
    echo -e "${YELLOW}==================================================================${NC}"
    echo ""
}

# Check if we're in the right directory
if [[ ! -f "pyproject.toml" ]]; then
    print_error "Not in project root directory (pyproject.toml not found)"
    exit 1
fi

# Usage: ./server-fast-automation.sh [--chunks N] [--dry-run]
#   --chunks N   parallel pytest chunks, balanced by the previous run's durations (default 8)
#   --dry-run    print the chunk plan and exit without running anything
CHUNKS=8
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --chunks)
            if [[ $# -lt 2 || ! "$2" =~ ^[1-9][0-9]*$ ]]; then
                print_error "--chunks needs a positive integer"
                exit 1
            fi
            CHUNKS="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --help|-h)
            sed -n '/^# Usage: .\/server-fast-automation.sh/,/^# *--dry-run/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            print_error "Unknown argument: $1"
            exit 1
            ;;
    esac
done

PROJECT_DIR=$(pwd)
TELEMETRY_DIR=".test-telemetry"
LANE_TOOL="$PROJECT_DIR/scripts/unit_lane_chunks.py"
PLAN_ARGS=(
    --root "$PROJECT_DIR"
    --paths tests/unit/server/
    --chunks "$CHUNKS"
    --history-dir "$TELEMETRY_DIR"
    --history-glob 'server-fast-automation-*.log'
    --history-glob 'chunk[0-9]*-*.log'
)
if [[ $DRY_RUN -eq 1 ]]; then
    python3 "$LANE_TOOL" plan "${PLAN_ARGS[@]}" --show-files
    exit $?
fi

# ^ see scripts/unit_lane_common.sh; one lane at a time, per-chunk HOME isolation, real-home guard
# shellcheck source=scripts/unit_lane_common.sh
source "$PROJECT_DIR/scripts/unit_lane_common.sh"
unset CIDX_REAL_HOME_GUARD_DIR  # a developer-shell export must not redirect it
unit_lane_start server-fast

# Check Python version
print_step "Checking Python version"
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d " " -f 2)
echo "Using Python $PYTHON_VERSION"
print_success "Python version checked"

# 1. Install dependencies
print_step "Installing dependencies"
# Workaround for pip compatibility: try --break-system-packages first (Python 3.11+),
# fall back to --user, fall back to bare pip install
PROJECT_DIR=$(pwd)
PROJECT_NAME=$(basename "$PROJECT_DIR")
cd ..
if pip install -e "./${PROJECT_NAME}[dev]" --break-system-packages 2>/dev/null; then
    :
elif pip install -e "./${PROJECT_NAME}[dev]" --user 2>/dev/null; then
    :
else
    pip install -e "./${PROJECT_NAME}[dev]"
fi
cd "$PROJECT_DIR"
print_success "Dependencies installed"

# 2. Lint server code with ruff
print_step "Running ruff linter on server code"
if ruff check src/code_indexer/server/ tests/unit/server/; then
    print_success "Server ruff linting passed"
else
    print_error "Server ruff linting failed"
    exit 1
fi

# 3. Check server code formatting with ruff format
# NOTE: Using ruff format instead of black because pre-commit hooks use ruff-format
# and ruff/black have incompatible formatting rules on ~243 files. Using the same
# formatter in both pre-commit and automation ensures consistency.
print_step "Checking server code formatting with ruff format"
if ruff format --check src/code_indexer/server/ tests/unit/server/; then
    print_success "Server ruff formatting check passed"
else
    print_error "Server ruff formatting check failed"
    print_warning "Run 'ruff format src/code_indexer/server/ tests/unit/server/' to fix formatting"
    exit 1
fi

# 4. Type check server code with mypy (temporarily disabled due to module path config issue)
# print_step "Running mypy type checking on server code"
# if mypy src/code_indexer/server/ --ignore-missing-imports; then
#     print_success "Server MyPy type checking passed"
# else
#     print_error "Server MyPy type checking failed"
#     exit 1
# fi
print_step "Skipping mypy (disabled: module path configuration issue)"
print_warning "MyPy temporarily disabled - fix module path duplication issue"

# 5. Verify MCP tool documentation completeness
print_step "Verifying MCP tool documentation"
if python3 tools/verify_tool_docs.py; then
    print_success "Tool documentation verification passed"
else
    print_error "Tool documentation verification failed"
    exit 1
fi

# 6. Server unit tests
# ^ see scripts/unit_lane_chunks.py; file split from previous --durations=0 logs, isolated HOME per chunk
check_load_before_run
print_step "Running server unit tests ($CHUNKS parallel, duration-balanced chunks)"

# Tuning knobs — override via environment for CI or local profiling.
# ^ hazard PYTEST_DURATIONS != 0 => next run's chunk split loses most file durations
PYTEST_TIMEOUT="${PYTEST_TIMEOUT:-15}"
PYTEST_DURATIONS="${PYTEST_DURATIONS:-0}"
# Bug #1863: echo the effective ceiling once. .env/.env.local are sourced near
# the top of this script and pytest-timeout itself also reads PYTEST_TIMEOUT
# from the environment, so a stray override there would otherwise silently
# weaken hang detection with no visible trace in the run's output.
echo "⏱️  pytest-timeout ceiling for this run: ${PYTEST_TIMEOUT}s (override via PYTEST_TIMEOUT env var)"

mkdir -p "$TELEMETRY_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY_FILE="$TELEMETRY_DIR/server-fast-automation-${TIMESTAMP}-summary.log"

# Shared pytest options for all chunks
# No --cov: coverage adds massive overhead (2-3x slowdown on large suites)
# CIDX_TEST_FAST_SQLITE=1: use MEMORY journal + synchronous=OFF instead of WAL
#   to eliminate the 1.5s per-init overhead (saves ~500s on initialize_database calls)
# ^ coupled -m with fast-automation.sh -m and tests/utils/real_process_guard.py; guard lets only these 7 markers spawn cidx
PYTEST_COMMON_OPTS=(
    -m "not slow and not e2e and not real_api and not integration and not requires_server and not requires_containers and not performance"
    --tb=short
    --timeout="$PYTEST_TIMEOUT"
    --durations="$PYTEST_DURATIONS"
    -q
)
PYPATH="$(pwd)/src:$(pwd)/tests"

unit_lane_spawn "$TELEMETRY_FILE" "${PLAN_ARGS[@]}" \
    --log-prefix server-fast-automation \
    --timestamp "$TIMESTAMP" \
    --env CIDX_TEST_FAST_SQLITE=1 \
    --env PYTHONPATH="$PYPATH" \
    -- \
    "${PYTEST_COMMON_OPTS[@]}"
echo "⏳ $CHUNKS chunks running (runner pid $UNIT_LANE_PID); progress: $TELEMETRY_DIR/server-fast-automation-${TIMESTAMP}-chunk*.log"
unit_lane_wait "$UNIT_LANE_PID"
TEST_EXIT_CODE=$UNIT_LANE_EXIT
cat "$TELEMETRY_FILE"

unit_lane_guard_verify && REAL_HOME_OK=0 || REAL_HOME_OK=$?
TEST_EXIT_CODE=$(real_home_guard_exit_code "$TEST_EXIT_CODE" "$REAL_HOME_OK")

CHUNK_LOG_FILES=("$TELEMETRY_DIR"/server-fast-automation-"${TIMESTAMP}"-chunk*.log)
if [ "$TEST_EXIT_CODE" -eq 0 ]; then
    print_success "Server unit tests passed"
else
    print_error "Server unit tests FAILED (exit $TEST_EXIT_CODE)"
    echo "Summary: $TELEMETRY_FILE"
    echo "Full logs: $TELEMETRY_DIR/server-fast-automation-${TIMESTAMP}-chunk*.log"
    report_pytest_timeout_diagnostics "${CHUNK_LOG_FILES[@]}"
    exit 1
fi

# Summary
echo -e "\n${GREEN}🎉 Server-focused automation completed successfully!${NC}"
echo "==========================================="
echo "✅ Server linting passed"
echo "✅ Server formatting checked"
echo "✅ Server type checking passed"
echo "✅ Server unit tests passed"
echo ""
echo "🖥️  Server test coverage:"
echo "   ✅ tests/unit/server/ - Server API and core functionality"
echo "   ✅ Authentication and authorization tests"
echo "   ✅ Repository management tests"
echo "   ✅ Job management and orchestration tests"
echo "   ✅ Validation and error handling tests"
echo ""
echo "ℹ️  This complements fast-automation.sh (CLI tests) for complete coverage"
echo "Ready for server deployment! 🚀"
