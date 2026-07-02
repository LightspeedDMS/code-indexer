#!/usr/bin/env bash
# migrate-claude-memory.sh
#
# Migrates Claude Code's project memory directory to a symlink pointing at
# the repo's .claude-memory/ folder, so memories are versioned in git.
#
# What it does:
#   1. Detects the Claude Code memory path for this repo clone
#   2. If it's already a symlink pointing to .claude-memory/, does nothing
#   3. If it's a real directory, merges any existing memories into .claude-memory/
#   4. Replaces the directory with a symlink to .claude-memory/
#
# Usage:
#   ./scripts/migrate-claude-memory.sh
#
# Run from the repo root (or any subdirectory -- it finds the root via git).

set -euo pipefail

# Find repo root
REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
REPO_MEMORY_DIR="${REPO_ROOT}/.claude-memory"

# Claude Code encodes the project path by replacing / with -
# e.g., /home/user/Dev/code-indexer-master -> -home-user-Dev-code-indexer-master
ENCODED_PATH=$(echo "$REPO_ROOT" | sed 's|/|-|g')
CLAUDE_MEMORY_DIR="${HOME}/.claude/projects/${ENCODED_PATH}/memory"
CLAUDE_PROJECT_DIR="${HOME}/.claude/projects/${ENCODED_PATH}"

echo "Repo root:           ${REPO_ROOT}"
echo "Repo memory dir:     ${REPO_MEMORY_DIR}"
echo "Claude memory dir:   ${CLAUDE_MEMORY_DIR}"
echo ""

# Ensure .claude-memory/ exists in the repo
if [ ! -d "${REPO_MEMORY_DIR}" ]; then
    echo "ERROR: ${REPO_MEMORY_DIR} does not exist."
    echo "This directory should be checked into git with the memory files."
    exit 1
fi

# Ensure Claude project directory exists
if [ ! -d "${CLAUDE_PROJECT_DIR}" ]; then
    echo "Creating Claude project directory: ${CLAUDE_PROJECT_DIR}"
    mkdir -p "${CLAUDE_PROJECT_DIR}"
fi

# Check if already a symlink pointing to the right place
if [ -L "${CLAUDE_MEMORY_DIR}" ]; then
    TARGET=$(readlink -f "${CLAUDE_MEMORY_DIR}")
    EXPECTED=$(readlink -f "${REPO_MEMORY_DIR}")
    if [ "${TARGET}" = "${EXPECTED}" ]; then
        echo "Already migrated. Symlink is correct."
        exit 0
    else
        echo "Symlink exists but points to ${TARGET} (expected ${EXPECTED})"
        echo "Removing stale symlink..."
        rm "${CLAUDE_MEMORY_DIR}"
    fi
fi

# If it's a real directory, merge contents into .claude-memory/
if [ -d "${CLAUDE_MEMORY_DIR}" ]; then
    echo "Found existing Claude memory directory with contents:"
    ls -1 "${CLAUDE_MEMORY_DIR}"
    echo ""

    # Copy any files that don't already exist in repo memory
    MERGED=0
    for f in "${CLAUDE_MEMORY_DIR}"/*; do
        [ -e "$f" ] || continue
        BASENAME=$(basename "$f")
        if [ ! -e "${REPO_MEMORY_DIR}/${BASENAME}" ]; then
            echo "  Merging: ${BASENAME}"
            cp -a "$f" "${REPO_MEMORY_DIR}/"
            MERGED=$((MERGED + 1))
        fi
    done

    if [ ${MERGED} -gt 0 ]; then
        echo "Merged ${MERGED} file(s) into ${REPO_MEMORY_DIR}/"
    else
        echo "No new files to merge (repo already has all memories)."
    fi

    # Remove the original directory
    echo "Removing original directory..."
    rm -rf "${CLAUDE_MEMORY_DIR}"
fi

# Create the symlink
echo "Creating symlink: ${CLAUDE_MEMORY_DIR} -> ${REPO_MEMORY_DIR}"
ln -s "${REPO_MEMORY_DIR}" "${CLAUDE_MEMORY_DIR}"

echo ""
echo "Done. Claude Code memories are now versioned in git at .claude-memory/"
