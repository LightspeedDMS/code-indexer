#!/usr/bin/env bash
# Advanced Bash: arrays, associative arrays, parameter expansion, process substitution

set -euo pipefail

# Indexed arrays
declare -a FRUITS=("apple" "banana" "cherry" "date" "elderberry")
declare -a PRIMES=(2 3 5 7 11 13 17 19 23 29)

# Associative arrays
declare -A CONFIG=(
    [host]="localhost"
    [port]="8080"
    [debug]="false"
    [log_level]="info"
)

declare -A COLORS=(
    [red]="#FF0000"
    [green]="#00FF00"
    [blue]="#0000FF"
    [white]="#FFFFFF"
    [black]="#000000"
)

# Array slicing and expansion
print_array_info() {
    local -n arr="$1"
    local name="$1"
    echo "Array: $name"
    echo "  Length: ${#arr[@]}"
    echo "  Elements: ${arr[*]}"
    echo "  First: ${arr[0]}"
    echo "  Last: ${arr[-1]}"
    echo "  Slice [1..3]: ${arr[@]:1:3}"
}

# Parameter expansion patterns
expand_demo() {
    local value="${1:-default_value}"
    local required="${2:?'parameter 2 is required'}"

    # String operations
    local upper="${value^^}"
    local lower="${value,,}"
    local length="${#value}"
    local trimmed="${value##*( )}"
    local no_suffix="${value%.*}"
    local replaced="${value//old/new}"

    echo "value=$value upper=$upper lower=$lower length=$length"
    echo "no_suffix=$no_suffix replaced=$replaced"
}

# Process substitution and here documents
compare_files() {
    local file1="$1"
    local file2="$2"
    diff <(sort "$file1") <(sort "$file2") || true
}

# Here-string
count_words() {
    wc -w <<< "$1"
}

# Nameref (Bash 4.3+)
set_config_value() {
    local key="$1"
    local -n target="CONFIG"
    target["$key"]="${2:-}"
}

# Arithmetic with arrays
sum_array() {
    local -n nums="$1"
    local total=0
    for n in "${nums[@]}"; do
        (( total += n ))
    done
    echo "$total"
}

product_array() {
    local -n nums="$1"
    local result=1
    for n in "${nums[@]}"; do
        (( result *= n ))
    done
    echo "$result"
}

# mapfile / readarray
load_lines() {
    local file="$1"
    local -a lines
    mapfile -t lines < "$file"
    echo "Loaded ${#lines[@]} lines from $file"
    printf '  %s\n' "${lines[@]}"
}

# Brace expansion
generate_dirs() {
    local base="$1"
    echo "Would create: ${base}/{src,tests,docs,scripts}/{main,util}"
}

# Regex matching with =~
validate_email() {
    local email="$1"
    local pattern='^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if [[ "$email" =~ $pattern ]]; then
        echo "valid: $email"
        return 0
    else
        echo "invalid: $email"
        return 1
    fi
}

# coprocess
start_coprocess() {
    coproc WORKER (while IFS= read -r line; do echo "processed: $line"; done)
    echo "task1" >&"${WORKER[1]}"
    echo "task2" >&"${WORKER[1]}"
    IFS= read -r result1 <&"${WORKER[0]}"
    IFS= read -r result2 <&"${WORKER[0]}"
    echo "$result1"
    echo "$result2"
}

# Main
main() {
    print_array_info FRUITS
    echo "Sum of primes: $(sum_array PRIMES)"
    echo "Product first 5: $(product_array PRIMES)"

    for key in "${!CONFIG[@]}"; do
        echo "config[$key]=${CONFIG[$key]}"
    done

    validate_email "user@example.com" || true
    validate_email "not-an-email" || true
    generate_dirs "/opt/myapp"
}

main "$@"
