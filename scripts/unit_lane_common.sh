#!/usr/bin/env bash
# Lifecycle shared by fast-, server-fast- and slow-automation.sh (sourced, not executed).
#
#   source scripts/unit_lane_common.sh
#   unit_lane_start LANE [LOCK_FILE]     # lane lock + traps + real-home snapshot + tmpfs TMPDIR root; refusal => exit 1
#   unit_lane_spawn SUMMARY RUN_ARGS...  # background chunk runner => UNIT_LANE_PID
#   unit_lane_wait PID                   # => UNIT_LANE_EXIT
#   unit_lane_guard_verify               # 0 when the real ~/.cidx-server is unchanged

_UNIT_LANE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_LANE_TOOL="$_UNIT_LANE_DIR/unit_lane_chunks.py"
UNIT_LANE_GUARD="$_UNIT_LANE_DIR/real-server-home-guard.sh"
UNIT_LANE_SCRATCH=""
UNIT_LANE_TMP_ROOT=""
UNIT_LANE_PIDS=()
UNIT_LANE_SPAWNS=0
# shellcheck source=scripts/real-server-home-guard.sh
source "$UNIT_LANE_GUARD"

_unit_lane_cleanup() {
    # ^ order capture the exit status before any command here resets $?
    local rc=$?
    # ^ hazard a second TERM/INT here => cleanup aborted, chunk groups and scratch survive
    trap '' TERM INT
    local pid pgid file
    for pid in "${UNIT_LANE_PIDS[@]}"; do
        if kill -TERM "$pid" 2>/dev/null; then
            # ^ hazard set -e + the runner's non-zero exit here => cleanup aborted before rm
            wait "$pid" 2>/dev/null || true
        fi
    done
    # ^ invariant a runner deletes its pgid file after reaping its chunks; a file left => runner died abnormally
    for file in "$UNIT_LANE_SCRATCH"/chunk-pgids-*; do
        [[ -f "$file" ]] || continue
        while read -r pgid; do
            [[ "$pgid" =~ ^[0-9]+$ ]] || continue
            kill -TERM -- "-$pgid" 2>/dev/null || true
        done < "$file"
    done
    [[ -z "$UNIT_LANE_SCRATCH" ]] || rm -rf "$UNIT_LANE_SCRATCH"
    if [[ -n "$UNIT_LANE_TMP_ROOT" ]]; then
        rm -rf "$UNIT_LANE_TMP_ROOT" || true
        if [[ -e "$UNIT_LANE_TMP_ROOT" ]]; then
            echo "FAILED: lane TMPDIR root $UNIT_LANE_TMP_ROOT could not be removed (tmpfs RAM stays used)" >&2
            # ^ why only a passing run turns into 1; a failing or interrupted run keeps its own exit code
            [[ $rc -ne 0 ]] || exit 1
        fi
    fi
}

unit_lane_start() {
    local lane="${1:?LANE required}" lock="${2:-$HOME/.tmp/cidx-unit-lanes.lock}"
    mkdir -p "$HOME/.tmp" "$(dirname "$lock")"
    # ^ invariant fd 9 stays open in this shell and every chunk; the kernel drops the flock with the last holder
    exec 9<>"$lock"
    python3 "$UNIT_LANE_TOOL" lock --fd 9 --lane "$lane" --pid $$ || exit 1
    trap _unit_lane_cleanup EXIT
    # ^ hazard untrapped TERM/INT => bash dies inside wait and _unit_lane_cleanup never finishes
    trap 'exit 143' TERM
    trap 'exit 130' INT
    unset CIDX_REAL_HOME_GUARD_DIR
    UNIT_LANE_SCRATCH=$(mktemp -d "$HOME/.tmp/cidx-lane-XXXXXX")
    real_home_guard_snapshot "$UNIT_LANE_SCRATCH/real-home-guard.state"
    # ^ see scripts/unit_lane_chunks.py:sweep_stale_tmp_roots; roots of dead lanes (SIGKILLed shells) are freed here
    UNIT_LANE_TMP_ROOT=$(python3 "$UNIT_LANE_TOOL" tmp-root --owner-pid $$) || {
        echo "ERROR: cannot create the lane TMPDIR root under /dev/shm; the lanes need a writable tmpfs there" >&2
        exit 1
    }
}

unit_lane_spawn() {
    local summary="${1:?SUMMARY required}"
    shift
    UNIT_LANE_SPAWNS=$((UNIT_LANE_SPAWNS + 1))
    python3 "$UNIT_LANE_TOOL" run \
        --lock-fd 9 \
        --pgid-file "$UNIT_LANE_SCRATCH/chunk-pgids-$UNIT_LANE_SPAWNS" \
        --scratch-root "$UNIT_LANE_SCRATCH" \
        --tmp-root "$UNIT_LANE_TMP_ROOT" \
        --guard "$UNIT_LANE_GUARD" \
        "$@" >"$summary" 2>&1 &
    UNIT_LANE_PID=$!
    UNIT_LANE_PIDS+=("$UNIT_LANE_PID")
}

unit_lane_wait() {
    local pid="${1:?PID required}" kept=() other
    # shellcheck disable=SC2034  # UNIT_LANE_EXIT is read by the sourcing lane script
    wait "$pid" && UNIT_LANE_EXIT=0 || UNIT_LANE_EXIT=$?
    for other in "${UNIT_LANE_PIDS[@]}"; do
        [[ "$other" == "$pid" ]] || kept+=("$other")
    done
    UNIT_LANE_PIDS=("${kept[@]}")
}

unit_lane_guard_verify() {
    if ! real_home_guard_verify "$UNIT_LANE_SCRATCH/real-home-guard.state"; then
        echo "ERROR: this run changed the developer's real ~/.cidx-server (see above)" >&2
        return 1
    fi
}
