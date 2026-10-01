# shellcheck shell=bash
# Phase 7 (SIEM delivery) helpers, sourced by e2e-automation.sh.
#
# Starts the mock Google SecOps receiver sidecar (tests/fixtures/secops_sidecar/,
# loopback only: ingest = Chronicle API + token endpoint, control = test API)
# and a server with the non-production fault-injection gate ON -- the ONLY way
# a server may target a loopback SIEM destination.  Mirrors the Phase 5
# fault-server helpers.  Every wait here is bounded.
#
# Relies on e2e-automation.sh for: SCRIPT_DIR, E2E_ADMIN_USER/PASS,
# E2E_SERVER_READINESS_POLL, _red/_green/_yellow/_bold, run_phase,
# handle_phase_result.

: "${E2E_SECOPS_SIDECAR_PORT:=8902}"
: "${E2E_SECOPS_SIDECAR_CONTROL_PORT:=8903}"
: "${E2E_SECOPS_SIDECAR_READINESS_TIMEOUT:=15}"
: "${E2E_SECOPS_PROJECT:=example-project}"
: "${E2E_SECOPS_LOCATION:=us}"
: "${E2E_SECOPS_INSTANCE:=00000000-0000-0000-0000-000000000000}"
: "${E2E_SECOPS_API_VERSION:=v1}"
: "${E2E_SIEM_SERVER_PORT:=8904}"
: "${E2E_SIEM_SERVER_HOST:=127.0.0.1}"
: "${E2E_SIEM_SERVER_READINESS_TIMEOUT:=60}"
# The two per-run scratch dirs (sidecar keys, server data) are always created
# fresh by make_phase7_scratch_dirs; only such dirs are ever deleted.
PHASE7_SCRATCH_ROOT="$HOME/.tmp"
PHASE7_SCRATCH_PREFIX="cidx-e2e-phase7-"
PHASE7_LAST_SERVER_LOG="$PHASE7_SCRATCH_ROOT/${PHASE7_SCRATCH_PREFIX}last-server.log"
: "${E2E_STOP_TIMEOUT:=10}"          # seconds between SIGTERM and SIGKILL
: "${E2E_CURL_MAX_TIME:=5}"          # per-request bound inside readiness loops

E2E_SECOPS_SIDECAR_DIR="${E2E_SECOPS_SIDECAR_DIR:-}"      # set by make_phase7_scratch_dirs
E2E_SIEM_SERVER_DATA_DIR="${E2E_SIEM_SERVER_DATA_DIR:-}"  # set by make_phase7_scratch_dirs
SIEM_SERVER_PID=""  # Phase 7 uvicorn (fault-injection gate ON)
SIDECAR_PID=""      # Phase 7 mock SecOps receiver sidecar

# Create this run's two owned scratch dirs (mktemp, Phase 7 prefix, ~/.tmp).
make_phase7_scratch_dirs() {
    mkdir -p "$PHASE7_SCRATCH_ROOT"
    E2E_SECOPS_SIDECAR_DIR=$(mktemp -d "$PHASE7_SCRATCH_ROOT/${PHASE7_SCRATCH_PREFIX}sidecar.XXXXXX") || return 1
    E2E_SIEM_SERVER_DATA_DIR=$(mktemp -d "$PHASE7_SCRATCH_ROOT/${PHASE7_SCRATCH_PREFIX}server.XXXXXX") || return 1
}

# Recursively delete $1 ONLY if it is an owned Phase 7 scratch dir: non-empty,
# directly under ~/.tmp, named with the Phase 7 prefix.  Refuses loudly otherwise.
safe_rm_phase7_dir() {
    local dir="$1"
    local resolved=""
    [[ -n "$dir" ]] && resolved=$(realpath -m -- "$dir")
    if [[ -z "$resolved" || "$(dirname -- "$resolved")" != "$(realpath -m -- "$PHASE7_SCRATCH_ROOT")" \
          || "$(basename -- "$resolved")" != "$PHASE7_SCRATCH_PREFIX"* ]]; then
        _red "ERROR: refusing to delete '$dir': not an owned Phase 7 scratch dir (~/.tmp/${PHASE7_SCRATCH_PREFIX}*)"
        return 1
    fi
    rm -rf -- "$resolved"
}

# The server is started with the non-production fault gate ON: it may only
# ever listen on loopback.
require_loopback_siem_host() {
    case "$E2E_SIEM_SERVER_HOST" in
        127.0.0.1|::1) return 0 ;;
    esac
    _red "ERROR: E2E_SIEM_SERVER_HOST='$E2E_SIEM_SERVER_HOST': the Phase 7 server runs with the fault-injection gate ON and must bind to loopback (127.0.0.1 or ::1)"
    return 1
}

# SIGTERM, wait at most E2E_STOP_TIMEOUT seconds, then SIGKILL; always reaps.
stop_pid_bounded() {
    local pid="$1" label="$2"
    [[ -z "$pid" ]] && return 0
    kill -TERM "$pid" 2>/dev/null || true
    local deadline=$(( $(date +%s) + E2E_STOP_TIMEOUT ))
    while kill -0 "$pid" 2>/dev/null && [[ $(date +%s) -lt $deadline ]]; do
        sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
        _yellow "  $label (PID $pid) ignored SIGTERM for ${E2E_STOP_TIMEOUT}s; sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true  # the process is gone now: this only reaps
}

# Called from cleanup_all_servers (EXIT trap and after the phase).
cleanup_phase7_servers() {
    if [[ -n "${SIEM_SERVER_PID:-}" ]]; then
        _yellow "Stopping Phase 7 SIEM server subprocess (PID $SIEM_SERVER_PID)..."
        stop_pid_bounded "$SIEM_SERVER_PID" "SIEM server"
        SIEM_SERVER_PID=""
    fi
    if [[ -n "${SIDECAR_PID:-}" ]]; then
        _yellow "Stopping Phase 7 SecOps sidecar (PID $SIDECAR_PID)..."
        stop_pid_bounded "$SIDECAR_PID" "SecOps sidecar"
        SIDECAR_PID=""
    fi
    # Per-run key material and server data are never kept (the server log is
    # copied out first for post-mortems).  Only owned scratch dirs are removed.
    if [[ -n "${E2E_SIEM_SERVER_DATA_DIR:-}" && -f "$E2E_SIEM_SERVER_DATA_DIR/server.log" ]]; then
        cp -- "$E2E_SIEM_SERVER_DATA_DIR/server.log" "$PHASE7_LAST_SERVER_LOG" || true
    fi
    local dir
    for dir in "${E2E_SECOPS_SIDECAR_DIR:-}" "${E2E_SIEM_SERVER_DATA_DIR:-}"; do
        if [[ -n "$dir" && -d "$dir" ]]; then
            safe_rm_phase7_dir "$dir" || true  # a refusal is already reported loudly
        fi
    done
}

write_siem_bootstrap_config() {
    mkdir -p "$E2E_SIEM_SERVER_DATA_DIR"
    cat > "$E2E_SIEM_SERVER_DATA_DIR/config.json" <<CONFIG_EOF
{
  "server_dir": "$E2E_SIEM_SERVER_DATA_DIR",
  "host": "$E2E_SIEM_SERVER_HOST",
  "port": $E2E_SIEM_SERVER_PORT,
  "fault_injection_enabled": true,
  "fault_injection_nonprod_ack": true
}
CONFIG_EOF
    _yellow "  Wrote SIEM bootstrap config.json to $E2E_SIEM_SERVER_DATA_DIR/config.json"
}

generate_secops_sidecar_keys() {
    # Per-run RSA keypair + service-account key file (token_uri =
    # http://127.0.0.1:$E2E_SECOPS_SIDECAR_PORT/token).  Never committed.
    # Written into the fresh owned dir from make_phase7_scratch_dirs.
    if [[ -z "$E2E_SECOPS_SIDECAR_DIR" || ! -d "$E2E_SECOPS_SIDECAR_DIR" ]]; then
        _red "ERROR: no sidecar scratch dir; call make_phase7_scratch_dirs first"
        return 1
    fi
    ( cd "$SCRIPT_DIR" && python3 -m tests.fixtures.secops_sidecar.harness keygen \
        --dir "$E2E_SECOPS_SIDECAR_DIR" \
        --ingest-port "$E2E_SECOPS_SIDECAR_PORT" \
        --project "$E2E_SECOPS_PROJECT" > /dev/null )
}

start_secops_sidecar() {
    _yellow "  Starting SecOps sidecar on 127.0.0.1:${E2E_SECOPS_SIDECAR_PORT} (control ${E2E_SECOPS_SIDECAR_CONTROL_PORT})..."
    ( cd "$SCRIPT_DIR" && exec python3 -m tests.fixtures.secops_sidecar \
        --ingest-port "$E2E_SECOPS_SIDECAR_PORT" \
        --control-port "$E2E_SECOPS_SIDECAR_CONTROL_PORT" \
        --project "$E2E_SECOPS_PROJECT" \
        --location "$E2E_SECOPS_LOCATION" \
        --instance "$E2E_SECOPS_INSTANCE" \
        --api-version "$E2E_SECOPS_API_VERSION" \
        --token-public-key "$E2E_SECOPS_SIDECAR_DIR/pub.pem" \
        > "$E2E_SECOPS_SIDECAR_DIR/sidecar.log" 2>&1 ) &
    SIDECAR_PID=$!
    _yellow "  Sidecar PID: $SIDECAR_PID"
}

wait_for_secops_sidecar() {
    local log="$E2E_SECOPS_SIDECAR_DIR/sidecar.log"
    local health_url="http://127.0.0.1:${E2E_SECOPS_SIDECAR_CONTROL_PORT}/_control/health"
    local deadline=$(( $(date +%s) + E2E_SECOPS_SIDECAR_READINESS_TIMEOUT ))
    while [[ $(date +%s) -lt $deadline ]]; do
        if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
            _red "ERROR: SecOps sidecar exited during startup. Log: $log"
            return 1
        fi
        if grep -q "^SIDECAR READY " "$log" 2>/dev/null \
            && [[ "$(curl -s --max-time "$E2E_CURL_MAX_TIME" -o /dev/null -w '%{http_code}' "$health_url" 2>/dev/null)" == "200" ]]; then
            _green "  SecOps sidecar ready ($(grep '^SIDECAR READY ' "$log"))"
            return 0
        fi
        sleep 0.2
    done
    _red "ERROR: SecOps sidecar not ready within ${E2E_SECOPS_SIDECAR_READINESS_TIMEOUT}s. Log: $log"
    return 1
}

start_siem_server() {
    require_loopback_siem_host || return 1
    _yellow "  Starting SIEM server on ${E2E_SIEM_SERVER_HOST}:${E2E_SIEM_SERVER_PORT}..."
    PYTHONPATH="$SCRIPT_DIR/src" \
    CIDX_TEST_FAST_SQLITE=1 \
    CIDX_SERVER_DATA_DIR="$E2E_SIEM_SERVER_DATA_DIR" \
    CIDX_DATA_DIR="$E2E_SIEM_SERVER_DATA_DIR" \
    SYSTEMD_UNIT_DIR="$E2E_SIEM_SERVER_DATA_DIR/no-systemd-units" \
        python3 -m uvicorn code_indexer.server.app:app \
            --host "$E2E_SIEM_SERVER_HOST" \
            --port "$E2E_SIEM_SERVER_PORT" \
            --log-level warning \
            --workers 1 > "$E2E_SIEM_SERVER_DATA_DIR/server.log" 2>&1 &
    SIEM_SERVER_PID=$!
    _yellow "  SIEM server PID: $SIEM_SERVER_PID"
}

wait_for_siem_server() {
    local base="http://${E2E_SIEM_SERVER_HOST}:${E2E_SIEM_SERVER_PORT}"
    local deadline=$(( $(date +%s) + E2E_SIEM_SERVER_READINESS_TIMEOUT ))
    _yellow "  Waiting for SIEM server at $base (timeout ${E2E_SIEM_SERVER_READINESS_TIMEOUT}s)..."
    while [[ $(date +%s) -lt $deadline ]]; do
        # Our process must still be alive: if it died (e.g. the port was
        # taken), whatever answers on the port is NOT the server under test.
        if ! kill -0 "$SIEM_SERVER_PID" 2>/dev/null; then
            _red "ERROR: SIEM server exited during startup. Log: $E2E_SIEM_SERVER_DATA_DIR/server.log"
            return 1
        fi
        local health_code login_code
        health_code=$(curl -s --max-time "$E2E_CURL_MAX_TIME" -o /dev/null -w "%{http_code}" "$base/health" 2>/dev/null || echo "000")
        if [[ "$health_code" != "000" ]] && [[ "$health_code" -lt 500 ]]; then
            login_code=$(curl -s --max-time "$E2E_CURL_MAX_TIME" -o /dev/null -w "%{http_code}" -X POST "$base/auth/login" \
                -H "Content-Type: application/json" \
                -d "{\"username\":\"${E2E_ADMIN_USER}\",\"password\":\"${E2E_ADMIN_PASS}\"}" \
                2>/dev/null || echo "000")
            if [[ "$login_code" == "200" ]]; then
                _green "  SIEM server ready (health=$health_code, auth=200)"
                return 0
            fi
        fi
        sleep "$E2E_SERVER_READINESS_POLL"
    done
    _red "ERROR: SIEM server did not become ready. Log: $E2E_SIEM_SERVER_DATA_DIR/server.log"
    return 1
}

# The Phase 7 body of the main phase loop.
run_phase7() {
    local phase_num="$1" phase_label="$2" phase_dir="$3"
    _bold "=== Phase 7: $phase_label ==="
    local phase7_exit=0
    if ! require_loopback_siem_host || ! make_phase7_scratch_dirs \
        || ! generate_secops_sidecar_keys || ! { start_secops_sidecar && wait_for_secops_sidecar; }; then
        _red "Phase 7 FAILED — SecOps sidecar did not start"
        phase7_exit=1
    else
        write_siem_bootstrap_config
        if ! start_siem_server || ! wait_for_siem_server; then
            _red "Phase 7 FAILED — SIEM server did not start"
            phase7_exit=1
        else
            E2E_SIEM_SERVER_HOST="$E2E_SIEM_SERVER_HOST" \
            E2E_SIEM_SERVER_PORT="$E2E_SIEM_SERVER_PORT" \
            E2E_SIEM_SERVER_DATA_DIR="$E2E_SIEM_SERVER_DATA_DIR" \
            E2E_SECOPS_SIDECAR_PORT="$E2E_SECOPS_SIDECAR_PORT" \
            E2E_SECOPS_SIDECAR_CONTROL_PORT="$E2E_SECOPS_SIDECAR_CONTROL_PORT" \
            E2E_SECOPS_SIDECAR_DIR="$E2E_SECOPS_SIDECAR_DIR" \
            E2E_SECOPS_PROJECT="$E2E_SECOPS_PROJECT" \
            E2E_SECOPS_LOCATION="$E2E_SECOPS_LOCATION" \
            E2E_SECOPS_INSTANCE="$E2E_SECOPS_INSTANCE" \
            E2E_SECOPS_API_VERSION="$E2E_SECOPS_API_VERSION" \
                run_phase "$phase_num" "$phase_label" "$phase_dir" || phase7_exit=$?
        fi
    fi
    cleanup_all_servers
    handle_phase_result "$phase_num" "$phase7_exit"
}
