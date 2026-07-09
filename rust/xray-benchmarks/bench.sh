#!/usr/bin/env bash
# xray-benchmarks/bench.sh — Benchmark runner for xray evaluator performance.
#
# Usage: ./bench.sh <target-directory> [evaluator-name]
#
# Runs each evaluator in evaluators/ against the target directory.
# Reports cold run (first invocation, forces recompile) and warm run
# (cached .so) timings from the xray-cli JSON output.
#
# Optional second arg limits to a single evaluator (without .rs extension).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EVAL_DIR="${SCRIPT_DIR}/evaluators"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
XRAY_CLI="${PROJECT_ROOT}/rust/target/release/xray-cli"
CACHE_DIR="${HOME}/.cidx-server/xray-cache"

TARGET="${1:?Usage: $0 <target-directory> [evaluator-name]}"
FILTER="${2:-}"

if [[ ! -d "$TARGET" ]]; then
    echo "ERROR: Target directory not found: $TARGET" >&2
    exit 1
fi

if [[ ! -x "$XRAY_CLI" ]]; then
    echo "ERROR: xray-cli not found at $XRAY_CLI" >&2
    echo "Run: cd ${PROJECT_ROOT}/rust && cargo build --release" >&2
    exit 1
fi

# Temp file for JSON output (avoids bash variable size limits)
JSON_OUT=$(mktemp)
trap 'rm -f "$JSON_OUT"' EXIT

run_eval() {
    local name="$1"
    local eval_file="$2"
    local run_label="$3"

    # Pass target directory directly — xray-cli walks it via collect_files()
    "$XRAY_CLI" --dynlib "$eval_file" --json "$TARGET" > "$JSON_OUT" 2>/dev/null

    local parse_scan_ms compile_ms cached files_parsed files_errored finding_count error_field
    parse_scan_ms=$(python3 -c "import json; d=json.load(open('$JSON_OUT')); print(d.get('parse_scan_ms',0))")
    compile_ms=$(python3 -c "import json; d=json.load(open('$JSON_OUT')); print(d.get('compile_ms',0))")
    cached=$(python3 -c "import json; d=json.load(open('$JSON_OUT')); print(d.get('cached',False))")
    files_parsed=$(python3 -c "import json; d=json.load(open('$JSON_OUT')); print(d.get('files_parsed',0))")
    files_errored=$(python3 -c "import json; d=json.load(open('$JSON_OUT')); print(d.get('files_errored',0))")
    finding_count=$(python3 -c "import json; d=json.load(open('$JSON_OUT')); print(len(d.get('findings',[])))")
    error_field=$(python3 -c "import json; d=json.load(open('$JSON_OUT')); print(d.get('error','') or '')")

    if [[ -n "$error_field" ]]; then
        echo "  ${run_label}: ERROR — $error_field"
        return 1
    fi

    printf "  %-6s  scan=%5sms  compile=%5sms  cached=%-5s  parsed=%s  errored=%s  findings=%s\n" \
        "$run_label" "$parse_scan_ms" "$compile_ms" "$cached" "$files_parsed" "$files_errored" "$finding_count"
}

# Count target files for display
FILE_COUNT=$(find "$TARGET" -type f \( -name "*.java" -o -name "*.kt" -o -name "*.py" -o -name "*.ts" -o -name "*.js" -o -name "*.go" -o -name "*.cs" -o -name "*.sh" -o -name "*.html" -o -name "*.css" \) | wc -l)

# Run benchmarks
echo "================================================================"
echo "XRAY BENCHMARK — $(date '+%Y-%m-%d %H:%M:%S')"
echo "Target: $TARGET (~$FILE_COUNT source files)"
echo "xray-cli: $XRAY_CLI"
echo "================================================================"
echo ""

for eval_file in "${EVAL_DIR}"/*.rs; do
    name=$(basename "$eval_file" .rs)

    if [[ -n "$FILTER" && "$name" != "$FILTER" ]]; then
        continue
    fi

    echo "--- $name ---"

    # Cold run: purge cached .so for this evaluator
    hash=$(sha256sum < "$eval_file" | cut -d' ' -f1)
    rm -f "${CACHE_DIR}/${hash}.so" "${CACHE_DIR}/${hash}.meta" "${CACHE_DIR}/${hash}.rs" 2>/dev/null || true

    run_eval "$name" "$eval_file" "COLD" || true

    # Warm run: .so should now be cached
    run_eval "$name" "$eval_file" "WARM" || true

    # Extra warm run for consistency check
    run_eval "$name" "$eval_file" "WARM2" || true

    echo ""
done

echo "================================================================"
echo "Benchmark complete."
