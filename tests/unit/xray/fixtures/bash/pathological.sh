#!/usr/bin/env bash
# Pathological Bash: deeply nested conditions and complex expansions

set -euo pipefail

readonly MIN_VALUE=1
readonly MAX_VALUE=1000
readonly DEFAULT_TIMEOUT=30

# Long single-line with chained parameter expansions
process_string() { local s="${1:-}"; echo "${s//[[:space:]]/_}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_]//g' | cut -c1-64; }

# Deeply nested if/elif/else
classify_exit_code() {
    local code="$1"
    if (( code == 0 )); then
        echo "success"
    elif (( code == 1 )); then
        echo "general_error"
    elif (( code == 2 )); then
        echo "misuse_of_shell"
    elif (( code >= 3 && code <= 9 )); then
        if (( code <= 5 )); then
            echo "app_error_low"
        else
            echo "app_error_high"
        fi
    elif (( code == 126 )); then
        echo "permission_denied"
    elif (( code == 127 )); then
        echo "command_not_found"
    elif (( code == 128 )); then
        echo "invalid_exit_arg"
    elif (( code > 128 && code <= 165 )); then
        local sig=$(( code - 128 ))
        if (( sig == 1 )); then echo "killed_HUP"
        elif (( sig == 2 )); then echo "killed_INT"
        elif (( sig == 9 )); then echo "killed_KILL"
        elif (( sig == 15 )); then echo "killed_TERM"
        else echo "killed_sig${sig}"
        fi
    elif (( code == 255 )); then
        echo "exit_out_of_range"
    else
        echo "unknown_${code}"
    fi
}

# Complex parameter expansion chains
transform_path() {
    local path="${1:-}"
    local base="${path##*/}"
    local dir="${path%/*}"
    local ext="${base##*.}"
    local stem="${base%.*}"
    local normalized_dir="${dir//\/\///}"
    echo "${normalized_dir}/${stem}.${ext:-txt}"
}

# Nested loops with arrays
build_matrix() {
    local -a rows=("$@")
    local size="${#rows[@]}"
    for (( i=0; i<size; i++ )); do
        for (( j=0; j<size; j++ )); do
            local val=$(( (i + 1) * (j + 1) ))
            printf "%4d" "$val"
        done
        echo
    done
}

# While loop with complex condition
wait_for_condition() {
    local check_cmd="$1"
    local timeout="${2:-$DEFAULT_TIMEOUT}"
    local interval=2
    local elapsed=0

    while (( elapsed < timeout )); do
        if eval "$check_cmd" &>/dev/null; then
            echo "condition met after ${elapsed}s"
            return 0
        fi
        sleep "$interval"
        (( elapsed += interval ))
    done
    echo "timeout after ${timeout}s"
    return 1
}

# Heredoc with variable interpolation
generate_config() {
    local host="$1"
    local port="${2:-8080}"
    local env="${3:-production}"
    cat <<EOF
# Generated configuration
# Environment: ${env}

[server]
host = ${host}
port = ${port}
workers = $(nproc 2>/dev/null || echo 4)

[logging]
level = $([ "$env" = "production" ] && echo "warn" || echo "debug")
file = /var/log/app-${env}.log

[limits]
max_connections = $(( port > 1024 ? 1000 : 100 ))
timeout = ${DEFAULT_TIMEOUT}
EOF
}

main() {
    for code in 0 1 2 5 126 127 130 143 255; do
        echo "exit $code -> $(classify_exit_code "$code")"
    done
    transform_path "/usr/local/bin/myapp.sh"
    build_matrix a b c
}

main "$@"
