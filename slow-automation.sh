#!/bin/bash

# Slow automation script - runs the @pytest.mark.slow unit-test population (Bug #1798)
#
# Both fast gates explicitly exclude "slow"-marked tests:
#   fast-automation.sh:255   -m "not slow and not e2e and not real_api and not integration
#                                and not requires_server and not requires_containers and not performance"
#   server-fast-automation.sh:141  -m "not slow and not e2e and not real_api and not integration"
#
# and e2e-automation.sh selects tests purely by PATH (tests/e2e/*), which never reaches
# anything under tests/unit/. As of Bug #1798, 447 individual @pytest.mark.slow marks exist
# across 266 files, essentially all under tests/unit/ -- and NOTHING runs any of them.
#
# CLAUDE.md prescribes @pytest.mark.slow as the remedy for a test that exceeds 30s. This
# script is what makes that prescription true: it is the complementary lane the marker
# routes into, so "mark it slow" means "runs here" instead of "never runs again".
#
# Selection expression (the exact complement of the two fast gates' own "-m" selection --
# e2e/real_api/integration/requires_server/requires_containers/performance keep their own
# separate homes and are deliberately still excluded here):
#
#   -m "slow and not e2e and not real_api and not integration
#       and not requires_server and not requires_containers and not performance"
#
# Timeout: fast-automation.sh's --timeout=15 is precisely what forced these tests out of
# the fast lane (Bug #1784's regression test measured 15.00s -- sitting exactly on that
# boundary). This lane needs a materially larger, but still BOUNDED, per-test budget: 120s
# (8x the fast-lane budget) gives generous headroom for legitimately expensive real-work
# tests (real subprocess spawns, real timeout-bounded retries) while still failing loud on
# a genuine hang rather than blocking the lane forever (Anti-Unbounded-Loop). Override via
# --timeout=N or the SLOW_PYTEST_TIMEOUT env var if a specific test population needs more.
#
# Two phases, mirroring the fast-automation.sh / server-fast-automation.sh split:
#   Phase 1 (non-server): tests/unit/, server tree excluded -- no special env needed.
#   Phase 2 (server):     tests/unit/server/ -- REQUIRES CIDX_SERVER_DATA_DIR pointed at an
#                          isolated temp dir + CIDX_TEST_FAST_SQLITE=1, exactly as
#                          server-fast-automation.sh:171-181 sets them. Without
#                          CIDX_SERVER_DATA_DIR, server tests write into ~/.cidx-server/ --
#                          the LIVE dev server's data directory (Bug #1776). This script
#                          NEVER runs a server-tree pytest invocation without that env var.
#                          Phase 1 UNCONDITIONALLY appends --ignore=tests/unit/server/ to
#                          every invocation, regardless of --paths -- this closes every
#                          ancestor/traversal bypass (e.g. --paths tests/unit, --paths tests/,
#                          --paths .) in one place instead of trying to enumerate them via
#                          path canonicalization. Phase 2's temp CIDX_SERVER_DATA_DIR is
#                          removed via an EXIT/INT/TERM trap, not just a post-pytest rm, so
#                          an interrupted or hung run cannot leak it under /tmp.
#
# tests/unit/infrastructure/ is ignored in Phase 1 for the same reason
# fast-automation.sh:135 ignores it: a pre-existing stale relative import
# (tests/unit/infrastructure/test_progress_debug.py -> tests.unit.services.test_vector_
# calculation_manager, a module that does not exist) breaks pytest COLLECTION entirely for
# that directory, independent of any marker selection. This is not new exclusion policy --
# it is the same defect the fast gate already carries around.
#
# Each phase writes its OWN telemetry log under .test-telemetry/ BEFORE the phase's pytest
# process exits, so results survive even if a process wedges at interpreter teardown after
# all its tests finished (Bug #1800: exactly this happened to a fast-automation.sh chunk,
# and only the per-chunk log preserved the results). tee's own exit code is checked
# separately from pytest's (via a single PIPESTATUS snapshot) -- a telemetry-write failure
# is treated as a phase failure even if pytest itself passed, since a missing/incomplete
# log defeats the point of logging.
#
# Usage:
#   ./slow-automation.sh                 # Both phases
#   ./slow-automation.sh --phase 1       # Non-server unit tests only
#   ./slow-automation.sh --phase 2       # Server unit tests only
#   ./slow-automation.sh --timeout 300   # Override the per-test pytest timeout (positive integer seconds)
#   ./slow-automation.sh --paths tests/unit/xray/   # Restrict Phase 1 to one subset (bounded runs)
#
# Exit codes:
#   0 - all selected phases passed (or collected zero tests)
#   1 - one or more phases failed, or a setup/argument error

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_step() {
    echo -e "\n${BLUE}\xe2\x9e\xa1\xef\xb8\x8f  $1${NC}"
}

print_success() {
    echo -e "${GREEN}\xe2\x9c\x85 $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}\xe2\x9a\xa0\xef\xb8\x8f  $1${NC}"
}

print_error() {
    echo -e "${RED}\xe2\x9d\x8c $1${NC}"
}

# Check if we're in the right directory
if [[ ! -f "pyproject.toml" ]]; then
    print_error "Not in project root directory (pyproject.toml not found)"
    exit 1
fi

# Source .env files if they exist (for local testing) -- same as the other gates.
# Done BEFORE argument defaults/parsing so a SLOW_PYTEST_TIMEOUT set here is picked up
# by the "${SLOW_PYTEST_TIMEOUT:-120}" default below and still validated at the end,
# same as the CLI --timeout path.
if [[ -f ".env.local" ]]; then
    source .env.local
fi
if [[ -f ".env" ]]; then
    source .env
fi

# ---------------------------------------------------------------------------
# Argument parsing (mirrors e2e-automation.sh's --phase convention)
# ---------------------------------------------------------------------------
ONLY_PHASE=""
SLOW_PYTEST_TIMEOUT="${SLOW_PYTEST_TIMEOUT:-120}"
PHASE1_PATHS="tests/unit/"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --phase)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --phase requires a value (1 or 2)" >&2
                exit 1
            fi
            ONLY_PHASE="$2"
            if [[ ! "$ONLY_PHASE" =~ ^[12]$ ]]; then
                echo "ERROR: --phase value must be 1 or 2 (got: '$ONLY_PHASE')" >&2
                exit 1
            fi
            shift 2
            ;;
        --timeout)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --timeout requires a value in seconds" >&2
                exit 1
            fi
            SLOW_PYTEST_TIMEOUT="$2"
            shift 2
            ;;
        --paths)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --paths requires a single path value" >&2
                exit 1
            fi
            PHASE1_PATHS="$2"
            if [[ ! -e "$PHASE1_PATHS" ]]; then
                echo "ERROR: --paths target does not exist: $PHASE1_PATHS" >&2
                exit 1
            fi
            # Reject a target that IS (or is nested under) tests/unit/server/. This is
            # NOT redundant with Phase 1's unconditional --ignore=tests/unit/server/
            # below: --ignore only prunes recursion when the ignored path is a strict
            # descendant of an ANCESTOR target (e.g. --paths tests/unit/). Verified
            # empirically that --ignore has ZERO effect when the ignored path equals,
            # or is an ancestor of, the explicitly-specified positional target itself
            # (pytest tests/unit/server/ --ignore=tests/unit/server/ still collected
            # every server test). realpath closes relative traversal (tests/unit/x/../
            # server) and absolute-path variants of the same bypass.
            RESOLVED_PHASE1_PATH="$(realpath "$PHASE1_PATHS")"
            RESOLVED_SERVER_TREE="$(realpath "tests/unit/server")"
            case "$RESOLVED_PHASE1_PATH" in
                "$RESOLVED_SERVER_TREE"|"$RESOLVED_SERVER_TREE"/*)
                    echo "ERROR: --paths must not target tests/unit/server/ or a path under it (resolved: $RESOLVED_PHASE1_PATH) -- pytest's --ignore does not exclude an explicitly-specified target or its descendants, only ancestor paths. Use --phase 2 for server tests (it sets the required CIDX_SERVER_DATA_DIR)." >&2
                    exit 1
                    ;;
            esac
            shift 2
            ;;
        --help|-h)
            sed -n '2,58p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            echo "Usage: $0 [--phase 1|2] [--timeout N] [--paths PATH] [--help]" >&2
            exit 1
            ;;
    esac
done

# Validate the FINAL timeout value regardless of its source (default, .env file, or
# CLI --timeout override, in that ascending precedence) -- validated exactly once,
# here, after every source has had a chance to set it.
if [[ ! "$SLOW_PYTEST_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: timeout must be a positive integer number of seconds (got: '$SLOW_PYTEST_TIMEOUT')" >&2
    exit 1
fi

echo "\xf0\x9f\x90\x8c Starting CIDX slow-lane automation pipeline (Bug #1798)..."
echo "==========================================="
echo "Per-test timeout: ${SLOW_PYTEST_TIMEOUT}s"

# PYTHONPATH: src + tests, exactly as server-fast-automation.sh computes it. A bare
# `pytest` here (no PYTHONPATH) resolves `code_indexer` from the sibling repo at
# /home/jsbattig/Dev/code-indexer/ instead of this project -- always set PYPATH explicitly.
PYPATH="$(pwd)/src:$(pwd)/tests"

SLOW_MARKER_EXPR="slow and not e2e and not real_api and not integration and not requires_server and not requires_containers and not performance"

TELEMETRY_DIR=".test-telemetry"
mkdir -p "$TELEMETRY_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

OVERALL_EXIT_CODE=0

# Sets PHASE_EXTRA_ENV (array) and PHASE_SERVER_DATA_DIR (string, empty for phase 1).
# Only Phase 2 (server tree) needs an isolated CIDX_SERVER_DATA_DIR -- without it,
# server tests write into ~/.cidx-server/, the live dev server's data directory
# (Bug #1776). Installs an EXIT/INT/TERM cleanup trap so an interrupted/hung pytest
# run cannot leak the temp dir under /tmp.
_phase_setup_server_env() {
    local phase_num="$1"
    PHASE_EXTRA_ENV=()
    PHASE_SERVER_DATA_DIR=""
    if [[ "$phase_num" == "2" ]]; then
        PHASE_SERVER_DATA_DIR=$(mktemp -d /tmp/cidx-slow-phase2-XXXXXX)
        PHASE_EXTRA_ENV=(CIDX_SERVER_DATA_DIR="$PHASE_SERVER_DATA_DIR" CIDX_TEST_FAST_SQLITE=1)
        # shellcheck disable=SC2064
        trap 'rm -rf "$PHASE_SERVER_DATA_DIR"' EXIT INT TERM
    fi
}

# Runs pytest piped through tee, capturing BOTH exit codes into PHASE_PYTEST_EXIT and
# PHASE_TEE_EXIT via one PIPESTATUS snapshot (reading PIPESTATUS across two separate
# `local` statements is wrong -- the first `local` itself becomes the new foreground
# command and resets PIPESTATUS before the second line can read index 1).
_phase_exec_pytest() {
    local log_file="$1"
    shift
    local paths=("$@")

    set +e
    env "${PHASE_EXTRA_ENV[@]}" PYTHONPATH="$PYPATH" python3 -m pytest \
        "${paths[@]}" \
        -m "$SLOW_MARKER_EXPR" \
        --timeout="$SLOW_PYTEST_TIMEOUT" \
        --durations=0 \
        --tb=short \
        -q \
        2>&1 | tee "$log_file"
    local pipeline_status=("${PIPESTATUS[@]}")
    set -e

    PHASE_PYTEST_EXIT=${pipeline_status[0]}
    PHASE_TEE_EXIT=${pipeline_status[1]}
}

# Cleans up the temp server data dir (if any), interprets both exit codes, prints the
# phase result, and folds it into OVERALL_EXIT_CODE.
_phase_report_result() {
    local phase_num="$1"
    local phase_name="$2"
    local log_file="$3"

    if [[ -n "$PHASE_SERVER_DATA_DIR" ]]; then
        rm -rf "$PHASE_SERVER_DATA_DIR"
        trap - EXIT INT TERM
    fi

    local summary
    summary=$(grep -E "passed|failed|error|no tests ran" "$log_file" 2>/dev/null | tail -1 || echo "no output")

    # pytest exit code 5 = no tests collected for this selection; not a failure of the
    # runner itself (e.g. --phase 1 restricted to a slow-free custom --paths target).
    local phase_exit=$PHASE_PYTEST_EXIT
    if [[ $phase_exit -eq 5 ]]; then
        phase_exit=0
    fi

    if [[ $PHASE_TEE_EXIT -ne 0 ]]; then
        print_error "Phase ${phase_num} (${phase_name}): tee failed writing telemetry log (exit $PHASE_TEE_EXIT) -- treating as failure regardless of pytest result"
        phase_exit=$PHASE_TEE_EXIT
    fi

    if [[ $phase_exit -eq 0 ]]; then
        print_success "Phase ${phase_num} (${phase_name}): $summary"
    else
        print_error "Phase ${phase_num} (${phase_name}) FAILED (exit $phase_exit): $summary"
    fi

    OVERALL_EXIT_CODE=$(( OVERALL_EXIT_CODE | phase_exit ))
    ln -sf "$(basename "$log_file")" "$TELEMETRY_DIR/latest-slow-phase${phase_num}.log"
}

run_phase() {
    local phase_num="$1"
    local phase_name="$2"
    shift 2
    local paths=("$@")
    local log_file="$TELEMETRY_DIR/slow-automation-phase${phase_num}-${phase_name}-${TIMESTAMP}.log"

    print_step "Phase ${phase_num}: ${phase_name} (${paths[*]})"
    echo "  Log: $log_file"

    _phase_setup_server_env "$phase_num"
    _phase_exec_pytest "$log_file" "${paths[@]}"
    _phase_report_result "$phase_num" "$phase_name" "$log_file"
}

if [[ -z "$ONLY_PHASE" || "$ONLY_PHASE" == "1" ]]; then
    # --ignore=tests/unit/server/ is UNCONDITIONAL here, regardless of PHASE1_PATHS.
    # This is what actually guarantees Phase 1 never touches the server tree (and
    # therefore never needs CIDX_SERVER_DATA_DIR) -- it closes every ancestor/traversal
    # bypass (--paths tests/unit, --paths tests/, --paths .) in one place, rather than
    # trying to enumerate them via path canonicalization on caller-supplied input.
    PHASE1_PATH_ARR=("$PHASE1_PATHS")
    run_phase 1 "non-server" "${PHASE1_PATH_ARR[@]}" \
        --ignore=tests/unit/server/ \
        --ignore=tests/unit/infrastructure/
fi

if [[ -z "$ONLY_PHASE" || "$ONLY_PHASE" == "2" ]]; then
    run_phase 2 "server" tests/unit/server/
fi

echo ""
echo "==========================================="
if [[ $OVERALL_EXIT_CODE -eq 0 ]]; then
    print_success "Slow-lane automation completed successfully"
else
    print_error "Slow-lane automation FAILED (exit $OVERALL_EXIT_CODE)"
fi
echo "Telemetry: $TELEMETRY_DIR/slow-automation-phase*-${TIMESTAMP}.log"

exit $OVERALL_EXIT_CODE
