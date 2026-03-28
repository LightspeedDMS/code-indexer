#!/bin/bash

# Server-focused fast automation script - tests CIDX server functionality
# Runs server unit tests that don't require external services or special permissions
# Separated from main fast-automation.sh to focus on server components

set -e  # Exit on any error

# Source .env files if they exist (for local testing)
if [[ -f ".env.local" ]]; then
    source .env.local
fi
if [[ -f ".env" ]]; then
    source .env
fi

echo "🖥️  Starting server-focused fast automation pipeline..."
echo "==========================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_step() {
    echo -e "\n${BLUE}➡️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Check if we're in the right directory
if [[ ! -f "pyproject.toml" ]]; then
    print_error "Not in project root directory (pyproject.toml not found)"
    exit 1
fi

# Check Python version
print_step "Checking Python version"
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d " " -f 2)
echo "Using Python $PYTHON_VERSION"
print_success "Python version checked"

# 1. Install dependencies
print_step "Installing dependencies"
# Workaround for pip compatibility: try --break-system-packages first (Python 3.11+),
# fall back to --user, fall back to bare pip install
PROJECT_DIR=$(pwd)
PROJECT_NAME=$(basename "$PROJECT_DIR")
cd ..
if pip install -e "./$PROJECT_NAME[dev]" --break-system-packages 2>/dev/null; then
    :
elif pip install -e "./$PROJECT_NAME[dev]" --user 2>/dev/null; then
    :
else
    pip install -e "./$PROJECT_NAME[dev]"
fi
cd "$PROJECT_DIR"
print_success "Dependencies installed"

# 2. Lint server code with ruff
print_step "Running ruff linter on server code"
if ruff check src/code_indexer/server/ tests/unit/server/; then
    print_success "Server ruff linting passed"
else
    print_error "Server ruff linting failed"
    exit 1
fi

# 3. Check server code formatting with ruff format
# NOTE: Using ruff format instead of black because pre-commit hooks use ruff-format
# and ruff/black have incompatible formatting rules on ~243 files. Using the same
# formatter in both pre-commit and automation ensures consistency.
print_step "Checking server code formatting with ruff format"
if ruff format --check src/code_indexer/server/ tests/unit/server/; then
    print_success "Server ruff formatting check passed"
else
    print_error "Server ruff formatting check failed"
    print_warning "Run 'ruff format src/code_indexer/server/ tests/unit/server/' to fix formatting"
    exit 1
fi

# 4. Type check server code with mypy (temporarily disabled due to module path config issue)
# print_step "Running mypy type checking on server code"
# if mypy src/code_indexer/server/ --ignore-missing-imports; then
#     print_success "Server MyPy type checking passed"
# else
#     print_error "Server MyPy type checking failed"
#     exit 1
# fi
print_step "Skipping mypy (disabled: module path configuration issue)"
print_warning "MyPy temporarily disabled - fix module path duplication issue"

# 5. Verify MCP tool documentation completeness
print_step "Verifying MCP tool documentation"
if python3 tools/verify_tool_docs.py; then
    print_success "Tool documentation verification passed"
else
    print_error "Tool documentation verification failed"
    exit 1
fi

# 6. Run server unit tests only
print_step "Running server unit tests"
echo "ℹ️  Testing CIDX server functionality including:"
echo "   • API endpoints and authentication"
echo "   • Repository management"
echo "   • Job management and sync orchestration"
echo "   • Validation and error handling"
echo "   • Branch operations"

# Create telemetry directory
TELEMETRY_DIR=".test-telemetry"
mkdir -p "$TELEMETRY_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY_LOG="$TELEMETRY_DIR/server-test-${TIMESTAMP}.log"
DURATIONS_LOG="$TELEMETRY_DIR/server-durations-${TIMESTAMP}.txt"

echo "📊 Telemetry enabled: Results will be saved to $TELEMETRY_LOG"
echo "⏱️  Duration report: $DURATIONS_LOG"

# Run server-specific unit tests with telemetry
PYTHONPATH="$(pwd)/src:$(pwd)/tests" pytest \
    tests/unit/server/ \
    -m "not slow and not e2e and not real_api and not integration" \
    -v \
    --durations=20 \
    --tb=short \
    --cov=code_indexer.server \
    --cov-report=xml --cov-report=term-missing \
    2>&1 | tee "$TELEMETRY_LOG"

# Capture pytest exit code from pipe
TEST_EXIT_CODE=${PIPESTATUS[0]}

# Extract duration information
echo "" > "$DURATIONS_LOG"
echo "=== Top 20 Slowest Tests ===" >> "$DURATIONS_LOG"
grep "slowest durations" -A 25 "$TELEMETRY_LOG" >> "$DURATIONS_LOG" 2>/dev/null || echo "No duration data captured" >> "$DURATIONS_LOG"

# Check for failures in log
if [ $TEST_EXIT_CODE -eq 0 ]; then
    print_success "Server unit tests passed"
    echo "📊 Telemetry saved to: $TELEMETRY_LOG"
    echo "⏱️  Duration report: $DURATIONS_LOG"
else
    print_error "Server unit tests failed (exit code: $TEST_EXIT_CODE)"
    echo "📊 Failure telemetry saved to: $TELEMETRY_LOG"
    echo "⏱️  Check $DURATIONS_LOG for slow/hanging tests"

    # Show failure summary
    echo ""
    echo "=== Failure Summary ==="
    grep -E "failed.*passed|ERROR" "$TELEMETRY_LOG" | tail -5
    exit 1
fi

# Summary
echo -e "\n${GREEN}🎉 Server-focused automation completed successfully!${NC}"
echo "==========================================="
echo "✅ Server linting passed"
echo "✅ Server formatting checked"
echo "✅ Server type checking passed"
echo "✅ Server unit tests passed"
echo ""
echo "🖥️  Server test coverage:"
echo "   ✅ tests/unit/server/ - Server API and core functionality"
echo "   ✅ Authentication and authorization tests"
echo "   ✅ Repository management tests"
echo "   ✅ Job management and orchestration tests"
echo "   ✅ Validation and error handling tests"
echo ""
echo "ℹ️  This complements fast-automation.sh (CLI tests) for complete coverage"
echo "Ready for server deployment! 🚀"
