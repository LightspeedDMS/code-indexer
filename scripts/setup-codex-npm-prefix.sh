#!/usr/bin/env bash
# setup-codex-npm-prefix.sh — idempotent Codex npm install helper
#
# Fixes EACCES when `npm install -g @openai/codex` fails because the system
# npm prefix (/usr/local, /usr, /opt) is root-owned and cidx-server runs as
# a non-root user (Bug #879 split-user policy).
#
# Algorithm:
#   1. Detect npm; abort with clear error if absent
#   2. Read current npm prefix
#   3. If system prefix, switch to ~/.npm-global (writes ~/.npmrc)
#   4. Add export PATH line to ~/.bashrc if not already present (idempotent)
#   5. npm install -g @openai/codex
#   6. Verify with <prefix>/bin/codex --version; exit non-zero on failure
#   7. Optionally patch cidx-server systemd unit (--update-cidx-server-systemd)
#   8. Print summary block
#
# Flags:
#   --update-cidx-server-systemd   Prepend npm bin dir to the Environment=PATH
#                                  line in the cidx-server systemd unit file and
#                                  run sudo systemctl daemon-reload.
#
# Environment overrides (for testing):
#   CIDX_SYSTEMD_UNIT_PATH   Override systemd unit file path
#                            (default: /etc/systemd/system/cidx-server.service)
#
# Safe to run multiple times — all steps are idempotent.

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

CIDX_UPDATE_SYSTEMD=0

for arg in "$@"; do
    case "${arg}" in
        --update-cidx-server-systemd)
            CIDX_UPDATE_SYSTEMD=1
            ;;
        *)
            echo "ERROR: Unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done

# Unit file path — overridable for tests
CIDX_SYSTEMD_UNIT_PATH="${CIDX_SYSTEMD_UNIT_PATH:-/etc/systemd/system/cidx-server.service}"

# ---------------------------------------------------------------------------
# Step 1: Detect npm
# ---------------------------------------------------------------------------

if ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: npm is not on PATH. Install Node.js/npm first." >&2
    echo "  On RHEL/Rocky: sudo dnf install nodejs npm" >&2
    echo "  On Ubuntu/Debian: sudo apt-get install nodejs npm" >&2
    exit 1
fi

echo "npm found: $(command -v npm)"

# ---------------------------------------------------------------------------
# Step 2: Detect current npm prefix
# ---------------------------------------------------------------------------

CURRENT_PREFIX="$(npm config get prefix)"
echo "Current npm prefix: ${CURRENT_PREFIX}"

# Returns 0 if the prefix is a system location (not user-writable by default).
# Matches /usr, /usr/local, /usr/*, /opt, and /opt/* as system locations.
_is_system_prefix() {
    local p="$1"
    case "$p" in
        /usr|/usr/*|/opt|/opt/*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Step 3: Switch to user-writable prefix if on a system location
# ---------------------------------------------------------------------------

if _is_system_prefix "${CURRENT_PREFIX}"; then
    NEW_PREFIX="${HOME}/.npm-global"
    echo "System prefix detected. Switching to user-writable prefix: ${NEW_PREFIX}"
    mkdir -p "${NEW_PREFIX}"
    npm config set prefix "${NEW_PREFIX}"
    NPM_PREFIX="${NEW_PREFIX}"
    echo "npm prefix updated to: ${NPM_PREFIX}"
else
    NPM_PREFIX="${CURRENT_PREFIX}"
    echo "Prefix is already user-writable: ${NPM_PREFIX}"
fi

NPM_BIN="${NPM_PREFIX}/bin"

# ---------------------------------------------------------------------------
# Step 4: Add export PATH line to ~/.bashrc (idempotent)
# ---------------------------------------------------------------------------

EXPORT_LINE="export PATH=\"${NPM_BIN}:\$PATH\"  # added by setup-codex-npm-prefix.sh"

if grep -qF "${NPM_BIN}" "${HOME}/.bashrc" 2>/dev/null; then
    echo "PATH already configured in ~/.bashrc — no change needed."
else
    echo "" >> "${HOME}/.bashrc"
    echo "# npm global bin — added by setup-codex-npm-prefix.sh" >> "${HOME}/.bashrc"
    echo "${EXPORT_LINE}" >> "${HOME}/.bashrc"
    echo "Added PATH export to ~/.bashrc"
    echo ""
    echo "ACTION REQUIRED: activate in your current shell with one of:"
    echo "  source ~/.bashrc"
    echo "  OR start a new shell session"
fi

# ---------------------------------------------------------------------------
# Step 5: Install Codex
# ---------------------------------------------------------------------------

echo ""
echo "Installing @openai/codex globally..."
npm install -g @openai/codex

# ---------------------------------------------------------------------------
# Step 6: Verify with codex --version (fail-fast — no silent suppression)
# ---------------------------------------------------------------------------

CODEX_BIN="${NPM_BIN}/codex"
echo ""
echo "Verifying Codex installation..."

if ! CODEX_VERSION="$("${CODEX_BIN}" --version 2>&1)"; then
    echo "ERROR: codex binary not found or failed at ${CODEX_BIN}" >&2
    echo "  Check that npm install -g @openai/codex completed successfully." >&2
    exit 1
fi

echo "codex version: ${CODEX_VERSION}"

# ---------------------------------------------------------------------------
# Step 7: Summary block
# ---------------------------------------------------------------------------

echo ""
echo "================================================================"
echo "  setup-codex-npm-prefix.sh — SUMMARY"
echo "================================================================"
echo "  npm prefix   : ${NPM_PREFIX}"
echo "  codex binary : ${CODEX_BIN}"
echo "  PATH line for cidx-server systemd unit:"
echo "    Environment=\"PATH=${NPM_BIN}:/usr/local/bin:/usr/bin:/bin\""
echo "================================================================"

# ---------------------------------------------------------------------------
# Step 8: Optionally patch cidx-server systemd unit (--update-cidx-server-systemd)
# ---------------------------------------------------------------------------

update_cidx_server_systemd_path() {
    local unit_path="${CIDX_SYSTEMD_UNIT_PATH}"
    local bin_dir="${NPM_BIN}"

    if [ ! -f "${unit_path}" ]; then
        echo "WARNING: cidx-server systemd unit not found at ${unit_path} — skipping"
        return 0
    fi

    # Extract current PATH line; guard if absent
    local current_path_line
    current_path_line="$(grep -m1 'Environment="PATH=' "${unit_path}" || true)"

    if [ -z "${current_path_line}" ]; then
        echo "WARNING: no Environment=PATH line found in ${unit_path} — skipping"
        return 0
    fi

    if echo "${current_path_line}" | grep -qF "${bin_dir}"; then
        echo "PATH already configured in cidx-server systemd unit — no change needed."
        return 0
    fi

    # Strip Environment="PATH= prefix and trailing " to get the existing colon-list
    local existing_path
    existing_path="$(echo "${current_path_line}" | sed 's/.*Environment="PATH=//;s/"$//')"

    # Build replacement line
    local new_line
    new_line="Environment=\"PATH=${bin_dir}:${existing_path}\""

    # Stage 1: generate updated content — fail fast if sed errors
    local tmp_content
    if ! tmp_content="$(sed "s|Environment=\"PATH=.*\"|${new_line}|" "${unit_path}")"; then
        echo "ERROR: failed to render updated unit file content from ${unit_path}" >&2
        return 1
    fi

    # Stage 2: write updated content — fail fast if tee errors
    if ! echo "${tmp_content}" | sudo tee "${unit_path}" > /dev/null; then
        echo "ERROR: failed to write updated unit file at ${unit_path}" >&2
        return 1
    fi

    echo "Updated cidx-server systemd unit: prepended ${bin_dir} to PATH"

    # Stage 3: reload systemd — fail fast if daemon-reload errors
    if ! sudo systemctl daemon-reload; then
        echo "ERROR: sudo systemctl daemon-reload failed" >&2
        return 1
    fi
    echo "Ran sudo systemctl daemon-reload"
}

if [ "${CIDX_UPDATE_SYSTEMD}" -eq 1 ]; then
    update_cidx_server_systemd_path
fi
