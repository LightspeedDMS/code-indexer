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
#   7. Print summary block
#
# Safe to run multiple times — all steps are idempotent.

set -euo pipefail

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
