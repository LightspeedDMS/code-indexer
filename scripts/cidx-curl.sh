#!/usr/bin/env bash
# cidx-curl.sh — Authorized HTTP-fetch wrapper for the CIDX Research Assistant.
#
# Story #929 Item #2a (Phase 2 remediation): replaces hardcoded RFC1918
# prefix-based Claude CLI allow rules with operator-configurable CIDR validation.
# Pattern matches scripts/cidx-db-query.sh (Story #872 wrapper architecture).
#
# Story #929 Codex Pass 5 escalation: switched from BLACKLIST to WHITELIST architecture.
# curl exposes 200+ flags across 23 protocols, and each blacklist patch closed prior
# findings while revealing new ones (5 consecutive failed Codex review passes).
# A whitelist is a closed set — any flag not explicitly allowed is rejected.
#
# DESIGN:
#   - Loopback (127.0.0.0/8, ::1/128) is ALWAYS allowed by this wrapper.
#     Operators cannot disable loopback (would break local API health-checks).
#   - Operators add additional CIDRs via:
#       claude_integration_config.ra_curl_allowed_cidrs in
#       ${CIDX_SERVER_DATA_DIR:-${HOME}/.cidx-server}/config.json
#   - Validation gate (in order):
#       1. Scan argv against whitelist of explicitly-allowed curl flags.
#          Any flag not in the allowlist causes immediate exit 2.
#          at-sign prefix on -d/-H/--data*/--header rejected (file-read primitive).
#          -o/--output restricted to /dev/null or - (stdout only).
#       2. Find http(s) URL argument; reject multiple URLs.
#       3. Reject non-http(s) scheme
#       4. Reject userinfo (http://10.0.0.1@evil.com/)
#       5. Reject invalid port (ValueError from urlparse)
#       6. Resolve hostname to IP (DNS rebinding mitigation: validate resolved IP)
#       7. Validate IP is in (operator_cidrs ∪ always-on loopback)
#       8. Scrub ambient env vars that could bypass --resolve pin: http_proxy/HTTPS_PROXY/
#          ALL_PROXY/NO_PROXY (attacker proxy), CURL_HOME/XDG_CONFIG_HOME (attacker curlrc),
#          CURL_CA_BUNDLE/SSL_CERT_FILE/SSL_CERT_DIR (attacker CA roots).
#          (Story #929 Codex Review #3: CRIT-NEW-3, CRIT-NEW-4, MED-1)
#       9. Inject --noproxy '*' to defeat any proxy mechanism that survived env scrub.
#      10. Inject -q (first curl arg) to disable default ~/.curlrc loading.
#      11. Pin curl to the validated IP via --resolve so curl connects to exactly
#          the IP that was validated (prevents DNS rebinding between validation and exec).
#          IPv6 addresses are bracketed: --resolve host:port:[::1]
#      12. exec curl with scrubbed env, -q, --noproxy '*', --resolve pin, and original args
#
# Decimal-IP encoding (e.g., http://2130706433/) is NOT accepted as IPv4.
# Python's ipaddress.ip_address() rejects "2130706433" as a string. The host
# falls through to DNS resolution, which fails with gaierror → exit 3.
#
# DNS rebinding mitigation: the Python validator resolves the hostname and validates
# the resulting IP against the CIDR allowlist. The wrapper then injects
# --resolve HOST:PORT:VALIDATED_IP so curl connects to that exact IP, preventing
# the OS from performing a fresh DNS lookup that could return a different address.
# Host header and SNI are preserved because --resolve does not alter them.
# IPv6 addresses are formatted as [::1] in the --resolve argument as curl requires.
#
# Exit codes:
#   0  - success (forwarded from curl)
#   2  - validation rejected (flag not in allowlist, no URL, invalid scheme, userinfo, invalid port)
#   3  - DNS resolution failed
#   4  - resolved IP not in allowed CIDR set
#   5  - Python validator crashed unexpectedly (exit code not in {0,2,3,4})

set -euo pipefail

# ---------------------------------------------------------------------------
# Allowed curl flags (whitelist)
# Story #929 Codex Pass 5 escalation: blacklist failed 5 review passes.
# curl exposes 200+ flags across 23 protocols; whitelist is closed-set.
# ---------------------------------------------------------------------------
# Long-form flags that take a value (validation must skip the next argv token)
ALLOWED_FLAGS_WITH_VALUE=(
    "--request"
    "--header"
    "--data"
    "--data-urlencode"
    "--data-raw"
    "--user-agent"
    "--referer"
    "--user"
    "--max-time"
    "--connect-timeout"
    "--output"
)
# Long-form flags that take no value
ALLOWED_FLAGS_NO_VALUE=(
    "--silent"
    "--show-error"
    "--include"
    "--head"
    "--fail"
    "--verbose"
    "--compressed"
    "--http1.0"
    "--http1.1"
    "--http2"
)
# Short-form flags that take a value (next arg is the value)
ALLOWED_SHORT_WITH_VALUE=(
    "-X" "-H" "-d" "-A" "-e" "-u" "-o"
)
# Short-form flags that take no value
ALLOWED_SHORT_NO_VALUE=(
    "-s" "-S" "-i" "-I" "-f" "-v"
)
# Long flags whose value must not begin with the at-sign character (file-read primitive)
NO_AT_PREFIX_LONG=("--header" "--data" "--data-urlencode" "--data-raw")
# Short flags whose value must not begin with the at-sign character
NO_AT_PREFIX_SHORT=("-H" "-d")
# Flags for which value must be exactly '/dev/null' or '-'
OUTPUT_FLAGS_LONG=("--output")
OUTPUT_FLAGS_SHORT=("-o")
ALLOWED_OUTPUT_VALUES=("/dev/null" "-")

# ---------------------------------------------------------------------------
# Helper: exit 2 if value begins with the at-sign character (file-read primitive).
# $1 = flag name, $2 = value
# ---------------------------------------------------------------------------
_reject_at_prefix() {
    local flag="$1"
    local value="$2"
    if [[ "${value:0:1}" == "@" ]]; then
        echo "ERROR: cidx-curl.sh: $flag: at-sign prefix rejected (file-read primitive)" >&2
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Helper: exit 2 unless value is exactly '/dev/null' or '-' (stdout).
# $1 = flag name, $2 = value
# ---------------------------------------------------------------------------
_restrict_output_value() {
    local flag="$1"
    local value="$2"
    local ok=0
    local allowed
    for allowed in "${ALLOWED_OUTPUT_VALUES[@]}"; do
        if [[ "$value" == "$allowed" ]]; then
            ok=1
            break
        fi
    done
    if [[ $ok -eq 0 ]]; then
        echo "ERROR: cidx-curl.sh: $flag value must be /dev/null or - (got: $value)" >&2
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Helper: return 0 if needle is in the remaining positional args, 1 otherwise.
# $1 = needle, $2..N = array elements (pass array with "${arr[@]}")
# ---------------------------------------------------------------------------
_in_array() {
    local needle="$1"
    shift
    local element
    for element in "$@"; do
        if [[ "$element" == "$needle" ]]; then
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Scan args: enforce whitelist, count and locate URLs
# ---------------------------------------------------------------------------
URL=""
URL_COUNT=0
i=0
ARGS=("$@")
while [[ $i -lt ${#ARGS[@]} ]]; do
    arg="${ARGS[$i]}"

    # Detect URL (positional arg starting with http:// or https://)
    if [[ "$arg" == http://* ]] || [[ "$arg" == https://* ]]; then
        URL_COUNT=$((URL_COUNT + 1))
        if [[ -z "$URL" ]]; then
            URL="$arg"
        fi
        i=$((i + 1))
        continue
    fi

    # Strip =value form for matching (e.g. --request=POST → --request)
    arg_name="${arg%%=*}"

    # --- Long flags with value ---
    if _in_array "$arg_name" "${ALLOWED_FLAGS_WITH_VALUE[@]}"; then
        # Get the value (either --flag=value form or next arg)
        if [[ "$arg" == *=* ]]; then
            value="${arg#*=}"
            advance=1
        else
            if [[ $((i + 1)) -ge ${#ARGS[@]} ]]; then
                echo "ERROR: cidx-curl.sh: $arg requires a value" >&2
                exit 2
            fi
            value="${ARGS[$((i + 1))]}"
            advance=2
        fi
        # at-sign prefix check for long flags
        if _in_array "$arg_name" "${NO_AT_PREFIX_LONG[@]}"; then
            _reject_at_prefix "$arg_name" "$value"
        fi
        # Output restriction check for long flags
        if _in_array "$arg_name" "${OUTPUT_FLAGS_LONG[@]}"; then
            _restrict_output_value "$arg_name" "$value"
        fi
        i=$((i + advance))
        continue
    fi

    # --- Long flags without value ---
    if _in_array "$arg" "${ALLOWED_FLAGS_NO_VALUE[@]}"; then
        i=$((i + 1))
        continue
    fi

    # --- Short flags with value ---
    if _in_array "$arg" "${ALLOWED_SHORT_WITH_VALUE[@]}"; then
        if [[ $((i + 1)) -ge ${#ARGS[@]} ]]; then
            echo "ERROR: cidx-curl.sh: $arg requires a value" >&2
            exit 2
        fi
        value="${ARGS[$((i + 1))]}"
        # at-sign prefix check for short flags
        if _in_array "$arg" "${NO_AT_PREFIX_SHORT[@]}"; then
            _reject_at_prefix "$arg" "$value"
        fi
        # Output restriction check for short flags
        if _in_array "$arg" "${OUTPUT_FLAGS_SHORT[@]}"; then
            _restrict_output_value "$arg" "$value"
        fi
        i=$((i + 2))
        continue
    fi

    # --- Short flags without value ---
    if _in_array "$arg" "${ALLOWED_SHORT_NO_VALUE[@]}"; then
        i=$((i + 1))
        continue
    fi

    # Unrecognized arg — REJECT (whitelist closes the bypass class)
    echo "ERROR: cidx-curl.sh: argument not in allowlist: $arg" >&2
    exit 2
done

# Multi-URL gate
if [[ $URL_COUNT -gt 1 ]]; then
    echo "ERROR: cidx-curl.sh: multiple URLs not allowed (got ${URL_COUNT})" >&2
    exit 2
fi
if [[ -z "$URL" ]]; then
    echo "ERROR: cidx-curl.sh: no http(s) URL argument found" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Locate config
# ---------------------------------------------------------------------------
CIDX_DATA_DIR="${CIDX_SERVER_DATA_DIR:-${HOME}/.cidx-server}"
CONFIG_PATH="${CIDX_DATA_DIR}/config.json"

# ---------------------------------------------------------------------------
# Embedded Python validator
# Prints three space-separated tokens to stdout on success:
#   VALIDATED_IP VALIDATED_PORT VALIDATED_HOST
# where VALIDATED_IP is the raw IP string (no brackets — shell adds them for IPv6).
# Warnings and errors go to stderr (flow naturally to terminal when stdout is captured).
# ---------------------------------------------------------------------------
_VALIDATE='
import ipaddress
import json
import socket
import sys
from urllib.parse import urlparse

config_path = sys.argv[1]
url = sys.argv[2]

# Parse URL
try:
    parsed = urlparse(url)
except Exception as exc:
    sys.stderr.write(f"ERROR: cidx-curl.sh: invalid URL: {exc}\n")
    sys.exit(2)

if parsed.scheme not in ("http", "https"):
    sys.stderr.write(f"ERROR: cidx-curl.sh: scheme must be http or https, got: {parsed.scheme}\n")
    sys.exit(2)

# Reject userinfo (http://10.0.0.1@evil.com/)
if parsed.username is not None or parsed.password is not None:
    sys.stderr.write("ERROR: cidx-curl.sh: userinfo bypass rejected\n")
    sys.exit(2)

host = parsed.hostname
if not host:
    sys.stderr.write("ERROR: cidx-curl.sh: no host in URL\n")
    sys.exit(2)

# Determine port — urlparse raises ValueError for invalid ports (e.g. http://host:abc/)
default_port = 443 if parsed.scheme == "https" else 80
try:
    port = parsed.port if parsed.port is not None else default_port
except ValueError as exc:
    sys.stderr.write(f"ERROR: cidx-curl.sh: invalid port in URL: {exc}\n")
    sys.exit(2)

# Load operator-configured CIDRs (best-effort — empty on parse failure)
cidrs_raw = []
try:
    with open(config_path) as f:
        cfg = json.load(f)
    cidrs_raw = cfg.get("claude_integration_config", {}).get("ra_curl_allowed_cidrs", []) or []
except FileNotFoundError:
    sys.stderr.write(f"WARNING: cidx-curl.sh: config not found ({config_path}); loopback-only mode\n")
except Exception as exc:
    sys.stderr.write(f"WARNING: cidx-curl.sh: config parse failed ({exc}); loopback-only mode\n")

# Build effective CIDR set
cidrs = []
for c in cidrs_raw:
    try:
        cidrs.append(ipaddress.ip_network(c, strict=False))
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"WARNING: cidx-curl.sh: invalid CIDR in config (skipped): {c}: {exc}\n")
# Always-on loopback — operators cannot disable
cidrs.append(ipaddress.ip_network("127.0.0.0/8"))
cidrs.append(ipaddress.ip_network("::1/128"))

# Resolve host to IP
try:
    ip = ipaddress.ip_address(host)
except ValueError:
    try:
        addrinfo = socket.getaddrinfo(host, None)
        ip_str = addrinfo[0][4][0]
        ip = ipaddress.ip_address(ip_str)
    except (socket.gaierror, IndexError, ValueError) as exc:
        sys.stderr.write(f"ERROR: cidx-curl.sh: DNS resolution failed for {host}: {exc}\n")
        sys.exit(3)

# Check membership
if not any(ip in c for c in cidrs):
    sys.stderr.write(f"ERROR: cidx-curl.sh: resolved IP {ip} not in allowed CIDR set\n")
    sys.exit(4)

# Print validated IP, port, host to stdout for --resolve pin injection.
# IP is printed as raw string; shell wraps IPv6 in brackets for curl --resolve format.
print(f"{ip} {port} {host}")
sys.exit(0)
'

# Run validator: stdout is captured for the --resolve pin; stderr flows to terminal.
# On non-zero exit, remap known codes 2/3/4 through as-is; anything else → 5 (crash).
VALIDATE_OUT=""
VALIDATE_STATUS=0
VALIDATE_OUT=$(python3 -c "$_VALIDATE" "$CONFIG_PATH" "$URL") || VALIDATE_STATUS=$?

if [[ $VALIDATE_STATUS -ne 0 ]]; then
    if [[ $VALIDATE_STATUS -eq 2 || $VALIDATE_STATUS -eq 3 || $VALIDATE_STATUS -eq 4 ]]; then
        exit "$VALIDATE_STATUS"
    fi
    exit 5
fi

# Parse validated IP, port, and hostname from validator output
VALIDATED_IP=$(echo "$VALIDATE_OUT" | awk '{print $1}')
VALIDATED_PORT=$(echo "$VALIDATE_OUT" | awk '{print $2}')
VALIDATED_HOST=$(echo "$VALIDATE_OUT" | awk '{print $3}')

# Format the IP for curl --resolve: IPv6 addresses must be bracketed ([::1]),
# IPv4 addresses are used as-is.
if [[ "$VALIDATED_IP" == *:* ]]; then
    # IPv6: wrap in brackets for curl --resolve host:port:[::1]
    RESOLVE_IP="[${VALIDATED_IP}]"
else
    RESOLVE_IP="$VALIDATED_IP"
fi

# ---------------------------------------------------------------------------
# All gates passed — exec curl pinned to the validated IP.
# --resolve HOST:PORT:IP tells curl to use exactly VALIDATED_IP for HOST:PORT,
# preserving Host header and SNI so TLS verification still uses the original hostname.
# This closes the DNS rebinding window between our validation and curl's connection.
#
# Story #929 Codex Review #3: scrub ambient env that bypasses --resolve pin.
# - http_proxy / HTTPS_PROXY / ALL_PROXY: would route through attacker proxy (CRIT-NEW-3)
# - CURL_HOME / XDG_CONFIG_HOME: would point ~/.curlrc to attacker config (CRIT-NEW-4)
# - CURL_CA_BUNDLE / SSL_CERT_FILE / SSL_CERT_DIR: would substitute attacker CA (MED-1)
# Also pass `-q` (must be first curl arg) to disable default ~/.curlrc loading (CRIT-NEW-4).
# Also pass `--noproxy '*'` to defeat any proxy mechanism that survived env scrub (CRIT-NEW-3).
# ---------------------------------------------------------------------------
exec env \
  -u http_proxy -u HTTP_PROXY \
  -u https_proxy -u HTTPS_PROXY \
  -u all_proxy -u ALL_PROXY \
  -u no_proxy -u NO_PROXY \
  -u CURL_HOME -u XDG_CONFIG_HOME \
  -u CURL_CA_BUNDLE -u SSL_CERT_FILE -u SSL_CERT_DIR \
  curl -q --noproxy '*' \
  --resolve "${VALIDATED_HOST}:${VALIDATED_PORT}:${RESOLVE_IP}" \
  "$@"
