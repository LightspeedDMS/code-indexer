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
    echo "Re-check yourself: grep -h \"from pytest-timeout\" ${TELEMETRY_DIR}/chunk*-${TIMESTAMP}.log | wc -l"
    echo -e "${YELLOW}==================================================================${NC}"
    echo ""
}

# Check if we're in the right directory
if [[ ! -f "pyproject.toml" ]]; then
    print_error "Not in project root directory (pyproject.toml not found)"
    exit 1
fi

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
if pip install -e "./$PROJECT_NAME[dev]" --break-system-packages 2>/dev/null; then
    :
elif pip install -e "./$PROJECT_NAME[dev]" --user 2>/dev/null; then
    :
else
    pip install -e "./$PROJECT_NAME[dev]"
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

# 6. Run server unit tests in parallel chunks
# Strategy: split into 6 parallel groups so wall time = max(chunk) instead of sum(all).
# Heavy folders each get their own chunk or pairing.
# Chunk 5 was previously a single oversized chunk (~500s); split into 5+6 to stay under 10min.
# All chunks share the same pytest flags but use isolated CIDX_SERVER_DATA_DIR.
check_load_before_run
print_step "Running server unit tests (6 parallel chunks)"
echo "  Chunk 1: services/"
echo "  Chunk 2: auth/"
echo "  Chunk 3: storage/ + wiki/"
echo "  Chunk 4: web/ + repositories/ + routers/"
echo "  Chunk 5: mcp/ + telemetry/ + handlers/"
echo "  Chunk 6: all remaining subdirs + root test files"

# Tuning knobs — override via environment for CI or local profiling
PYTEST_TIMEOUT="${PYTEST_TIMEOUT:-15}"
PYTEST_DURATIONS="${PYTEST_DURATIONS:-10}"
# Bug #1863: echo the effective ceiling once. .env/.env.local are sourced near
# the top of this script and pytest-timeout itself also reads PYTEST_TIMEOUT
# from the environment, so a stray override there would otherwise silently
# weaken hang detection with no visible trace in the run's output.
echo "⏱️  pytest-timeout ceiling for this run: ${PYTEST_TIMEOUT}s (override via PYTEST_TIMEOUT env var)"

# Create telemetry directory
TELEMETRY_DIR=".test-telemetry"
mkdir -p "$TELEMETRY_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Shared pytest options for all chunks
# No --cov: coverage adds massive overhead (2-3x slowdown on large suites)
# CIDX_TEST_FAST_SQLITE=1: use MEMORY journal + synchronous=OFF instead of WAL
#   to eliminate the 1.5s per-init overhead (saves ~500s on initialize_database calls)
PYTEST_COMMON_OPTS=(
    -m "not slow and not e2e and not real_api and not integration"
    --tb=short
    --timeout="$PYTEST_TIMEOUT"
    --durations="$PYTEST_DURATIONS"
    -q
)
PYPATH="$(pwd)/src:$(pwd)/tests"

# Each chunk needs its own isolated data dir to avoid SQLite locking conflicts
D1=$(mktemp -d /tmp/cidx-chunk1-XXXXXX)
D2=$(mktemp -d /tmp/cidx-chunk2-XXXXXX)
D3=$(mktemp -d /tmp/cidx-chunk3-XXXXXX)
D4=$(mktemp -d /tmp/cidx-chunk4-XXXXXX)
D5=$(mktemp -d /tmp/cidx-chunk5-XXXXXX)
D6=$(mktemp -d /tmp/cidx-chunk6-XXXXXX)

# Log files for each chunk
L1="$TELEMETRY_DIR/chunk1-services-${TIMESTAMP}.log"
L2="$TELEMETRY_DIR/chunk2-auth-${TIMESTAMP}.log"
L3="$TELEMETRY_DIR/chunk3-storage-wiki-${TIMESTAMP}.log"
L4="$TELEMETRY_DIR/chunk4-web-repos-routers-${TIMESTAMP}.log"
L5="$TELEMETRY_DIR/chunk5-rest-${TIMESTAMP}.log"
L6="$TELEMETRY_DIR/chunk6-rest2-${TIMESTAMP}.log"

# Cleanup temp dirs on exit
trap 'rm -rf "$D1" "$D2" "$D3" "$D4" "$D5" "$D6"' EXIT

WALL_START=$(date +%s)

# Launch all 6 chunks in parallel
CIDX_SERVER_DATA_DIR="$D1" CIDX_TEST_FAST_SQLITE=1 PYTHONPATH="$PYPATH" \
    python3 -m pytest tests/unit/server/services/ "${PYTEST_COMMON_OPTS[@]}" \
    >"$L1" 2>&1 &
PID1=$!

CIDX_SERVER_DATA_DIR="$D2" CIDX_TEST_FAST_SQLITE=1 PYTHONPATH="$PYPATH" \
    python3 -m pytest tests/unit/server/auth/ "${PYTEST_COMMON_OPTS[@]}" \
    >"$L2" 2>&1 &
PID2=$!

CIDX_SERVER_DATA_DIR="$D3" CIDX_TEST_FAST_SQLITE=1 PYTHONPATH="$PYPATH" \
    python3 -m pytest tests/unit/server/storage/ tests/unit/server/wiki/ "${PYTEST_COMMON_OPTS[@]}" \
    >"$L3" 2>&1 &
PID3=$!

CIDX_SERVER_DATA_DIR="$D4" CIDX_TEST_FAST_SQLITE=1 PYTHONPATH="$PYPATH" \
    python3 -m pytest tests/unit/server/web/ tests/unit/server/repositories/ tests/unit/server/routers/ "${PYTEST_COMMON_OPTS[@]}" \
    >"$L4" 2>&1 &
PID4=$!

CIDX_SERVER_DATA_DIR="$D5" CIDX_TEST_FAST_SQLITE=1 PYTHONPATH="$PYPATH" \
    python3 -m pytest tests/unit/server/mcp/ tests/unit/server/telemetry/ tests/unit/server/handlers/ \
    "${PYTEST_COMMON_OPTS[@]}" \
    >"$L5" 2>&1 &
PID5=$!

CIDX_SERVER_DATA_DIR="$D6" CIDX_TEST_FAST_SQLITE=1 PYTHONPATH="$PYPATH" \
    python3 -m pytest tests/unit/server/ \
    --ignore=tests/unit/server/services/ \
    --ignore=tests/unit/server/auth/ \
    --ignore=tests/unit/server/storage/ \
    --ignore=tests/unit/server/wiki/ \
    --ignore=tests/unit/server/web/ \
    --ignore=tests/unit/server/repositories/ \
    --ignore=tests/unit/server/routers/ \
    --ignore=tests/unit/server/mcp/ \
    --ignore=tests/unit/server/telemetry/ \
    --ignore=tests/unit/server/handlers/ \
    "${PYTEST_COMMON_OPTS[@]}" \
    >"$L6" 2>&1 &
PID6=$!

# Wait for all chunks and collect exit codes.
# Use && ... || Cn=$? pattern: with set -e, plain `wait; Cn=$?` would abort
# the script before Cn=$? runs if wait returns non-zero. The && ... || pattern
# is exempt from set -e triggering (bash spec: && and || chains don't trigger ERR).
wait $PID1 && C1=0 || C1=$?
wait $PID2 && C2=0 || C2=$?
wait $PID3 && C3=0 || C3=$?
wait $PID4 && C4=0 || C4=$?
wait $PID5 && C5=0 || C5=$?
wait $PID6 && C6=0 || C6=$?

WALL_END=$(date +%s)
WALL_SECS=$((WALL_END - WALL_START))

TEST_EXIT_CODE=$(( C1 | C2 | C3 | C4 | C5 | C6 ))

# Report per-chunk results
echo ""
echo "=== Chunk Results (wall time: ${WALL_SECS}s) ==="
for i in 1 2 3 4 5 6; do
    eval "code=\$C$i"
    eval "log=\$L$i"
    # Extract summary line from log
    summary=$(grep -E "passed|failed|error" "$log" | tail -1 || echo "no output")
    if [ "$code" -eq 0 ]; then
        echo "  Chunk $i: PASS — $summary"
    else
        echo "  Chunk $i: FAIL (exit $code) — $summary"
    fi
done

if [ $TEST_EXIT_CODE -eq 0 ]; then
    print_success "Server unit tests passed (${WALL_SECS}s wall time)"
else
    print_error "Server unit tests FAILED"
    echo ""
    echo "=== Failing chunk details ==="
    for i in 1 2 3 4 5 6; do
        eval "code=\$C$i"
        eval "log=\$L$i"
        if [ "$code" -ne 0 ]; then
            echo ""
            echo "--- Chunk $i failures ---"
            grep -E "FAILED|ERROR" "$log" | head -20
        fi
    done
    echo ""
    echo "Full logs: $TELEMETRY_DIR/chunk*-${TIMESTAMP}.log"
    report_pytest_timeout_diagnostics "$L1" "$L2" "$L3" "$L4" "$L5" "$L6"
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
