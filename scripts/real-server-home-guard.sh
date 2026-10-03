#!/usr/bin/env bash
# Real-server-home guard for the test lanes (sourced, not executed).
#
# A test run must never change the developer's REAL ~/.cidx-server -- the home
# of a running dev server.  The lanes snapshot the watched files before the run
# and verify after it; any change (content, mtime, appearance, removal) fails
# the lane loudly.  real_home_lane_env isolates the lane's environment so no
# test process can even resolve the real home (data-dir variables and HOME).
#
# Only names, mtimes and sha256 hashes are recorded or printed: config.json
# may hold secrets, so its contents are hashed and never shown.
#
# The real home comes from the password database, never $HOME (the lane
# points HOME at a scratch dir).  CIDX_REAL_HOME_GUARD_DIR overrides the
# guarded dir for the guard's own unit tests only.
#
#   source scripts/real-server-home-guard.sh
#   real_home_guard_snapshot "$state_file"
#   real_home_lane_env "$lane_scratch_dir"   # isolate HOME + server data dirs
#   ... run the tests ...
#   real_home_guard_verify "$state_file" || exit 1

REAL_HOME_GUARD_FILES=(launch.json config.json)

_real_home_account_home() {
    local entry account_home
    entry="$(getent passwd "$(id -u)")" || {
        echo "real-server-home-guard: cannot read the password database" >&2
        return 1
    }
    account_home="$(printf '%s\n' "$entry" | cut -d: -f6)"
    if [[ -z "$account_home" ]]; then
        echo "real-server-home-guard: cannot resolve the account home" >&2
        return 1
    fi
    printf '%s\n' "$account_home"
}

real_home_guard_dir() {
    if [[ -n "${CIDX_REAL_HOME_GUARD_DIR:-}" ]]; then
        printf '%s\n' "$CIDX_REAL_HOME_GUARD_DIR"
        return 0
    fi
    local account_home
    account_home="$(_real_home_account_home)" || return 1
    printf '%s/.cidx-server\n' "$account_home"
}

real_home_guard_state() {
    local dir name path out hash mtime
    dir="$(real_home_guard_dir)" || return 1
    for name in "${REAL_HOME_GUARD_FILES[@]}"; do
        path="$dir/$name"
        if [[ ! -e "$path" ]]; then
            printf '%s absent\n' "$name"
            continue
        fi
        out="$(sha256sum -- "$path")" || {
            echo "real-server-home-guard: cannot hash $name" >&2
            return 1
        }
        hash="${out%% *}"
        mtime="$(stat -c %y -- "$path")" || {
            echo "real-server-home-guard: cannot stat $name" >&2
            return 1
        }
        printf '%s mtime=%s sha256=%s\n' "$name" "$mtime" "$hash"
    done
}

real_home_guard_snapshot() {
    [[ -n "${1:-}" ]] || {
        echo "real-server-home-guard: STATE file argument required" >&2
        return 1
    }
    local state
    state="$(real_home_guard_state)" || return 1
    printf '%s\n' "$state" > "$1"
}

real_home_guard_verify() {
    [[ -n "${1:-}" ]] || {
        echo "real-server-home-guard: STATE file argument required" >&2
        return 1
    }
    local state_file="$1" before now
    [[ -r "$state_file" ]] || {
        echo "real-server-home-guard: snapshot '$state_file' missing" >&2
        return 1
    }
    before="$(cat -- "$state_file")" || return 1
    now="$(real_home_guard_state)" || return 1
    if [[ "$now" == "$before" ]]; then
        return 0
    fi
    {
        echo "=================================================================="
        echo "REAL SERVER HOME CHANGED: this run modified $(real_home_guard_dir)"
        echo "A test or harness process wrote to the developer's real server home."
        echo "--- before"
        printf '%s\n' "$before"
        echo "--- after"
        printf '%s\n' "$now"
        echo "=================================================================="
    } >&2
    return 1
}

# Isolate the lane: the server data dirs (CIDX_SERVER_DATA_DIR / CIDX_DATA_DIR,
# kept when the caller already points them outside the real server home) and
# HOME (code that builds Path.home()/.cidx-server paths) all point into
# SCRATCH.  The real Python user base and git config stay reachable.
real_home_lane_env() {
    [[ -n "${1:-}" ]] || {
        echo "real-server-home-guard: SCRATCH dir argument required" >&2
        return 1
    }
    local scratch="$1" account_home real_server_home user_base data_dir
    account_home="$(_real_home_account_home)" || return 1
    real_server_home="$account_home/.cidx-server"
    # from the ACCOUNT home: the current HOME may already be redirected
    user_base="$(HOME="$account_home" python3 -c 'import site; print(site.getuserbase())')" || {
        echo "real-server-home-guard: cannot resolve the Python user base" >&2
        return 1
    }
    mkdir -p -- "$scratch/home" "$scratch/server-home" || return 1
    data_dir="${CIDX_SERVER_DATA_DIR:-}"
    if [[ -z "$data_dir" || "$data_dir" == "$real_server_home" \
        || "$data_dir" == "$real_server_home"/* ]]; then
        data_dir="$scratch/server-home"
    fi
    export CIDX_SERVER_DATA_DIR="$data_dir"
    export CIDX_DATA_DIR="$data_dir"
    export PYTHONUSERBASE="$user_base"
    if [[ -f "$account_home/.gitconfig" ]]; then
        export GIT_CONFIG_GLOBAL="$account_home/.gitconfig"
    fi
    # rustup/cargo default to $HOME/.rustup and $HOME/.cargo: a fresh HOME
    # makes the first X-Ray call sync a toolchain (~23 s, a pytest-timeout)
    if [[ -z "${RUSTUP_HOME:-}" && -d "$account_home/.rustup" ]]; then
        export RUSTUP_HOME="$account_home/.rustup"
    fi
    if [[ -z "${CARGO_HOME:-}" && -d "$account_home/.cargo" ]]; then
        export CARGO_HOME="$account_home/.cargo"
    fi
    export HOME="$scratch/home"
}
