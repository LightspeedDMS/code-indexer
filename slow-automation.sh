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
# Two phases, mirroring the fast-automation.sh / server-fast-automation.sh split, run AT
# THE SAME TIME, each as N parallel chunks split by file from the previous run's
# --durations=0 logs (scripts/unit_lane_chunks.py, shared with the two fast lanes):
#   Phase 1 (non-server): tests/unit/ minus tests/unit/infrastructure/ and, UNCONDITIONALLY,
#                          tests/unit/server/ -- regardless of --paths, closing every
#                          ancestor/traversal bypass (e.g. --paths tests/unit, --paths tests/,
#                          --paths .) in one place. NOT fast's scripts/unit_lane_ignores.txt:
#                          slow tests in files the fast lane ignores still run here.
#   Phase 2 (server):     tests/unit/server/ with CIDX_TEST_FAST_SQLITE=1, as
#                          server-fast-automation.sh sets it.
# Every chunk of both phases gets its OWN scratch HOME and CIDX_SERVER_DATA_DIR /
# CIDX_DATA_DIR under ~/.tmp (scripts/real-server-home-guard.sh real_home_lane_env), so no
# test writes into ~/.cidx-server/ -- the LIVE dev server's data directory (Bug #1776) --
# and the lane fails if the real launch.json / config.json changed. The runner removes the
# scratch dirs when its chunks end, including on interrupt.
#
# tests/unit/infrastructure/ is ignored in Phase 1: a pre-existing stale relative
# import (tests/unit/infrastructure/test_progress_debug.py -> tests.unit.services.test_vector_
# calculation_manager, a module that does not exist) breaks pytest COLLECTION entirely for
# that directory, independent of any marker selection.
#
# Each chunk writes its OWN log .test-telemetry/slow-automation-phase<P>-<ts>-chunk<k>.log
# (pytest writes it directly, so results survive a process that wedges at interpreter
# teardown, Bug #1800); each phase's plan and per-chunk summary go to
# .test-telemetry/slow-automation-phase<P>-<ts>-summary.log.
#
# Usage:
#   ./slow-automation.sh                 # Both phases
#   ./slow-automation.sh --phase 1       # Non-server unit tests only
#   ./slow-automation.sh --phase 2       # Server unit tests only
#   ./slow-automation.sh --timeout 300   # Override the per-test pytest timeout (positive integer seconds)
#   ./slow-automation.sh --paths tests/unit/xray/   # Restrict Phase 1 to one subset (bounded runs)
#   ./slow-automation.sh --chunks 4      # Parallel chunks per phase (default 4)
#   ./slow-automation.sh --dry-run       # Print each phase's chunk plan and exit
#
# Exit codes:
#   0 - every selected phase ran tests and passed
#   1 - a phase failed or selected no test at all, or a setup/argument error

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
CHUNKS=4
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --chunks)
            if [[ $# -lt 2 || ! "$2" =~ ^[1-9][0-9]*$ ]]; then
                echo "ERROR: --chunks requires a positive integer" >&2
                exit 1
            fi
            CHUNKS="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
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
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            echo "Usage: $0 [--phase 1|2] [--timeout N] [--paths PATH] [--chunks N] [--dry-run] [--help]" >&2
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
# `pytest` here (no PYTHONPATH) can resolve `code_indexer` from another checkout
# instead of this project -- always set PYPATH explicitly.
PYPATH="$(pwd)/src:$(pwd)/tests"

SLOW_MARKER_EXPR="slow and not e2e and not real_api and not integration and not requires_server and not requires_containers and not performance"

PROJECT_DIR=$(pwd)
TELEMETRY_DIR=".test-telemetry"
LANE_TOOL="$PROJECT_DIR/scripts/unit_lane_chunks.py"
PHASES=()
[[ -n "$ONLY_PHASE" && "$ONLY_PHASE" != "1" ]] || PHASES+=(1)
[[ -n "$ONLY_PHASE" && "$ONLY_PHASE" != "2" ]] || PHASES+=(2)

_phase_plan_args() {
    PHASE_PLAN_ARGS=(--root "$PROJECT_DIR" --chunks "$CHUNKS" --history-dir "$TELEMETRY_DIR")
    if [[ "$1" == "1" ]]; then
        # ^ invariant tests/unit/server/ ignored whatever --paths says; never needs the server env
        # ^ why own ignores, not scripts/unit_lane_ignores.txt; slow tests in fast-ignored files must run
        PHASE_PLAN_ARGS+=(
            --paths "$PHASE1_PATHS"
            --ignore tests/unit/server/
            --ignore tests/unit/infrastructure/
            --history-glob 'slow-automation-phase1-*.log'
        )
    else
        PHASE_PLAN_ARGS+=(--paths tests/unit/server/ --history-glob 'slow-automation-phase2-*.log')
    fi
}

if [[ $DRY_RUN -eq 1 ]]; then
    for phase in "${PHASES[@]}"; do
        echo "Phase ${phase}:"
        _phase_plan_args "$phase"
        python3 "$LANE_TOOL" plan "${PHASE_PLAN_ARGS[@]}" --show-files
    done
    exit 0
fi

mkdir -p "$TELEMETRY_DIR"
# ^ see scripts/unit_lane_common.sh; one lane at a time, per-chunk HOME isolation, real-home guard
# shellcheck source=scripts/unit_lane_common.sh
source "$PROJECT_DIR/scripts/unit_lane_common.sh"
unset CIDX_REAL_HOME_GUARD_DIR  # a developer-shell export must not redirect it
unit_lane_start slow

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SUMMARIES=()
PHASE_PIDS=()
for phase in "${PHASES[@]}"; do
    _phase_plan_args "$phase"
    PHASE_ENV=(--env PYTHONPATH="$PYPATH")
    [[ "$phase" != "2" ]] || PHASE_ENV+=(--env CIDX_TEST_FAST_SQLITE=1)
    summary="$TELEMETRY_DIR/slow-automation-phase${phase}-${TIMESTAMP}-summary.log"
    print_step "Phase ${phase}: ${CHUNKS} chunks (summary: $summary)"
    unit_lane_spawn "$summary" "${PHASE_PLAN_ARGS[@]}" \
        --log-prefix "slow-automation-phase${phase}" \
        --timestamp "$TIMESTAMP" \
        "${PHASE_ENV[@]}" \
        -- \
        -m "$SLOW_MARKER_EXPR" \
        --timeout="$SLOW_PYTEST_TIMEOUT" \
        --durations=0 \
        --tb=short \
        -q
    PHASE_PIDS+=("$UNIT_LANE_PID")
    SUMMARIES+=("$summary")
done

OVERALL_EXIT_CODE=0
for i in "${!PHASE_PIDS[@]}"; do
    phase="${PHASES[$i]}"
    unit_lane_wait "${PHASE_PIDS[$i]}"
    cat "${SUMMARIES[$i]}"
    if [[ $UNIT_LANE_EXIT -eq 0 ]]; then
        print_success "Phase ${phase} passed"
    else
        # ^ invariant exit 5 = the phase selected no test => failure, never a pass
        print_error "Phase ${phase} FAILED (exit $UNIT_LANE_EXIT)"
        OVERALL_EXIT_CODE=1
    fi
    ln -sf "$(basename "${SUMMARIES[$i]}")" "$TELEMETRY_DIR/latest-slow-phase${phase}.log"
done

unit_lane_guard_verify && REAL_HOME_OK=0 || REAL_HOME_OK=$?
OVERALL_EXIT_CODE=$(real_home_guard_exit_code "$OVERALL_EXIT_CODE" "$REAL_HOME_OK")

echo ""
echo "==========================================="
if [[ $OVERALL_EXIT_CODE -eq 0 ]]; then
    print_success "Slow-lane automation completed successfully"
else
    print_error "Slow-lane automation FAILED (exit $OVERALL_EXIT_CODE)"
fi
echo "Telemetry: $TELEMETRY_DIR/slow-automation-phase*-${TIMESTAMP}-*.log"

exit "$OVERALL_EXIT_CODE"
