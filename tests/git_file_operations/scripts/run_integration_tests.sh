#!/usr/bin/env bash
#
# Integration Test Script for Git/File Operations Tests
#
# Runs the complete git/file operations test suite INCLUDING SSH-dependent tests.
# This script requires SSH keys configured for the test repository.
#
# Prerequisites:
#   - SSH key access to git@github.com:LightspeedDMS/VivaGoals-to-pptx.git
#   - SSH agent running with key loaded (ssh-add)
#
# Usage:
#   ./run_integration_tests.sh [pytest options]
#
# Examples:
#   ./run_integration_tests.sh              # Run all tests including SSH tests
#   ./run_integration_tests.sh -v           # Verbose output
#   ./run_integration_tests.sh -k "push"    # Run only tests matching "push"
#
# Exit codes:
#   0 - All tests passed
#   1 - Some tests failed
#   2 - SSH not configured (pre-flight check failed)

set -euo pipefail

# Script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(cd "$TEST_DIR/../.." && pwd)"

# Test repository for SSH access check
TEST_REPO="git@github.com:LightspeedDMS/VivaGoals-to-pptx.git"

echo "========================================"
echo "CIDX Git/File Operations Integration Tests"
echo "========================================"
echo ""
echo "Project root: $PROJECT_ROOT"
echo "Test directory: $TEST_DIR"
echo "Test repository: $TEST_REPO"
echo ""

# Pre-flight check: Verify SSH access
echo "Checking SSH access to test repository..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    # GitHub returns exit code 1 even on success, check output instead
    if ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com 2>&1 | grep -qi "permission denied\|authentication failed"; then
        echo ""
        echo "ERROR: SSH access to GitHub is not configured."
        echo ""
        echo "To run integration tests with SSH-dependent tests, you need:"
        echo "  1. An SSH key with access to $TEST_REPO"
        echo "  2. The SSH agent running with your key loaded"
        echo ""
        echo "Quick setup:"
        echo "  eval \"\$(ssh-agent -s)\""
        echo "  ssh-add ~/.ssh/your_key"
        echo ""
        echo "To run tests WITHOUT SSH-dependent tests, use:"
        echo "  ./run_git_file_ops_tests.sh"
        echo ""
        exit 2
    fi
fi
echo "SSH access verified."
echo ""

# Ensure CIDX_SKIP_SSH_TESTS is NOT set (run all tests)
unset CIDX_SKIP_SSH_TESTS 2>/dev/null || true

# Change to project root for proper pytest execution
cd "$PROJECT_ROOT"

# Run pytest with all git_file_operations tests
echo "Running all tests (including SSH-dependent tests)..."
echo ""

python3 -m pytest tests/git_file_operations/ \
    -v \
    --tb=short \
    "$@"

exit_code=$?

echo ""
echo "========================================"
if [ $exit_code -eq 0 ]; then
    echo "All integration tests PASSED"
else
    echo "Some tests FAILED (exit code: $exit_code)"
fi
echo "========================================"

exit $exit_code
