#!/usr/bin/env bash
#
# CI Script for Git/File Operations Tests
#
# Runs the git/file operations test suite with SSH-dependent tests skipped.
# This script is designed for CI/CD environments without SSH access.
#
# Usage:
#   ./run_git_file_ops_tests.sh [pytest options]
#
# Examples:
#   ./run_git_file_ops_tests.sh              # Run all tests (SSH tests skipped)
#   ./run_git_file_ops_tests.sh -v           # Verbose output
#   ./run_git_file_ops_tests.sh -k "status"  # Run only tests matching "status"
#
# Exit codes:
#   0 - All tests passed
#   1 - Some tests failed
#   2 - Script error

set -euo pipefail

# Script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(cd "$TEST_DIR/../.." && pwd)"

# Set environment variable to skip SSH-dependent tests
export CIDX_SKIP_SSH_TESTS=1

echo "========================================"
echo "CIDX Git/File Operations Tests (CI Mode)"
echo "========================================"
echo ""
echo "Project root: $PROJECT_ROOT"
echo "Test directory: $TEST_DIR"
echo "CIDX_SKIP_SSH_TESTS: $CIDX_SKIP_SSH_TESTS (SSH tests will be skipped)"
echo ""

# Change to project root for proper pytest execution
cd "$PROJECT_ROOT"

# Run pytest with the git_file_operations tests
# Skip tests marked with requires_ssh when env var is set
echo "Running tests..."
echo ""

python3 -m pytest tests/git_file_operations/ \
    -v \
    --tb=short \
    "$@"

exit_code=$?

echo ""
echo "========================================"
if [ $exit_code -eq 0 ]; then
    echo "All tests PASSED"
else
    echo "Some tests FAILED (exit code: $exit_code)"
fi
echo "========================================"

exit $exit_code
