#!/usr/bin/env bash
# cidx-meta-cleanup.sh - Safe deletion helper for cidx-meta directory files.
#
# Story #554: Research Assistant Security Hardening
#
# Deletes a file only if it is inside the cidx-meta directory.
# Refuses to delete the cidx-meta root itself, nonexistent paths,
# and any path outside cidx-meta (including path traversal attacks).
#
# Usage:
#   CIDX_META_BASE=/path/to/cidx-meta cidx-meta-cleanup.sh <target_path>
#
# Environment:
#   CIDX_META_BASE  Absolute path to the cidx-meta base directory.
#                   If not set or directory does not exist, exits 1.
#
# Exit codes:
#   0  Target deleted successfully
#   1  Error (path outside cidx-meta, missing base, traversal, etc.)

set -euo pipefail

TARGET_RAW="${1:-}"

if [[ -z "${TARGET_RAW}" ]]; then
    echo "ERROR: No target path provided." >&2
    echo "Usage: $(basename "$0") <target_path>" >&2
    exit 1
fi

# Validate CIDX_META_BASE is set and exists
CIDX_META_BASE="${CIDX_META_BASE:-}"
if [[ -z "${CIDX_META_BASE}" ]]; then
    echo "ERROR: cidx-meta base directory not configured or not found (CIDX_META_BASE is unset)" >&2
    exit 1
fi

if [[ ! -d "${CIDX_META_BASE}" ]]; then
    echo "ERROR: cidx-meta base directory does not exist: ${CIDX_META_BASE}" >&2
    exit 1
fi

# Resolve canonical paths (defeats symlink-based and ../ traversal attacks)
CIDX_META_CANONICAL="$(readlink -f "${CIDX_META_BASE}")"

# Resolve target: readlink -f resolves even if path has .. components.
# We use a Python fallback if the file doesn't exist yet (readlink -f still works
# on Linux even for nonexistent paths by resolving the existing prefix).
TARGET_CANONICAL="$(readlink -f "${TARGET_RAW}")"

# Check that target exists
if [[ ! -e "${TARGET_CANONICAL}" ]]; then
    echo "ERROR: Path does not exist: ${TARGET_CANONICAL}" >&2
    exit 1
fi

# Refuse to delete cidx-meta root itself
if [[ "${TARGET_CANONICAL}" == "${CIDX_META_CANONICAL}" ]]; then
    echo "ERROR: Cannot delete cidx-meta root directory: ${CIDX_META_CANONICAL}" >&2
    exit 1
fi

# Check that canonical target starts with cidx-meta canonical path + /
# The trailing / prevents /foo/cidx-meta-other from matching /foo/cidx-meta
if [[ "${TARGET_CANONICAL}" != "${CIDX_META_CANONICAL}/"* ]]; then
    echo "ERROR: Path is outside cidx-meta directory. Refusing to delete: ${TARGET_CANONICAL}" >&2
    exit 1
fi

# MEDIUM-3: Verify target is a regular file before deleting.
# Refuse directories to prevent accidental subtree operations.
if [[ ! -f "${TARGET_CANONICAL}" ]]; then
    echo "ERROR: Target is not a regular file: ${TARGET_CANONICAL}" >&2
    exit 1
fi

# All checks passed - delete the target
rm -f "${TARGET_CANONICAL}"
echo "Deleted: ${TARGET_CANONICAL}"
exit 0
