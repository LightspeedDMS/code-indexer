#!/bin/bash
#
# Comprehensive linting script that runs ruff, black, and mypy on all code including tests.
#

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to run a command and report results
run_command() {
    local cmd="$1"
    local description="$2"

    echo -e "${BLUE}Running ${description}...${NC}"

    if $cmd; then
        echo -e "${GREEN}✅ ${description} passed${NC}"
        return 0
    else
        echo -e "${RED}❌ ${description} failed${NC}"
        return 1
    fi
}

# Main function
main() {
    echo -e "${BLUE}🔍 Running comprehensive linting...${NC}"

    # Define paths to lint
    local src_path="src"
    local tests_path="tests"
    # Feature distributions (modular plan phase 4b): their sources and tests.
    local feature_paths="packages/features/cidx-xray packages/features/cidx-scip packages/features/cidx-wiki packages/features/cidx-memory packages/features/cidx-depmap"

    # Check if paths exist
    if [[ ! -d "$src_path" ]]; then
        echo -e "${RED}❌ Source path $src_path not found${NC}"
        exit 1
    fi

    if [[ ! -d "$tests_path" ]]; then
        echo -e "${RED}❌ Tests path $tests_path not found${NC}"
        exit 1
    fi

    local all_passed=true

    # Run ruff check
    if ! run_command "ruff check $src_path $tests_path tools/docs $feature_paths" "ruff check"; then
        all_passed=false
    fi

    # Run ruff format check
    if ! run_command "ruff format --check $src_path $tests_path tools/docs $feature_paths" "ruff format check"; then
        all_passed=false
    fi

    # Run mypy with explicit package bases (moderate strictness) - exclude problematic test file
    if ! run_command "mypy --explicit-package-bases --check-untyped-defs $src_path $tests_path --exclude=tests/unit/services/test_vector_data_structures_thread_safety.py" "mypy type check"; then
        all_passed=false
    fi

    # The X-Ray feature's sources and tests, as their own mypy run: its test
    # tree has no __init__.py (a "tests" package would collide with the
    # repository's), so it is checked on its own; the core it imports is checked
    # by the run above (--follow-imports=silent).
    if ! run_command "mypy --explicit-package-bases --check-untyped-defs --follow-imports=silent packages/features/cidx-xray/src/cidx_xray packages/features/cidx-xray/tests" "mypy type check (cidx-xray)"; then
        all_passed=false
    fi

    # The wiki feature's sources and tests, the same way.
    if ! run_command "mypy --explicit-package-bases --check-untyped-defs --follow-imports=silent packages/features/cidx-wiki/src/cidx_wiki packages/features/cidx-wiki/tests" "mypy type check (cidx-wiki)"; then
        all_passed=false
    fi

    # The memory feature's sources and tests, the same way.
    if ! run_command "mypy --explicit-package-bases --check-untyped-defs --follow-imports=silent packages/features/cidx-memory/src/cidx_memory packages/features/cidx-memory/tests" "mypy type check (cidx-memory)"; then
        all_passed=false
    fi

    # The dependency map feature's sources and tests, the same way.
    if ! run_command "mypy --explicit-package-bases --check-untyped-defs --follow-imports=silent packages/features/cidx-depmap/src/cidx_depmap packages/features/cidx-depmap/tests" "mypy type check (cidx-depmap)"; then
        all_passed=false
    fi

    # Story #1034 AC15: anti-orphan check — no direct cp+reflink in production code
    echo -e "${BLUE}Running Story #1034 AC15 anti-orphan check...${NC}"
    if python3 scripts/check_no_direct_cp_reflink.py; then
        echo -e "${GREEN}✅ AC15 anti-orphan check passed${NC}"
    else
        echo -e "${RED}❌ AC15 anti-orphan check failed${NC}"
        all_passed=false
    fi

    # Package dependency contracts (.importlinter) against their ratchet baseline
    echo -e "${BLUE}Running import contracts ratchet...${NC}"
    if python3 scripts/check_import_contracts.py; then
        echo -e "${GREEN}✅ Import contracts ratchet passed${NC}"
    else
        echo -e "${RED}❌ Import contracts ratchet failed${NC}"
        all_passed=false
    fi

    # Story #2079: no dangling doc references (Markdown links, docs/ paths, include_str!)
    echo -e "${BLUE}Running Story #2079 doc reference check...${NC}"
    if python3 scripts/check_doc_references.py; then
        echo -e "${GREEN}✅ Doc reference check passed${NC}"
    else
        echo -e "${RED}❌ Doc reference check failed${NC}"
        all_passed=false
    fi

    # Story #2082: generated reference docs (CLI, MCP tools, error codes) match the code
    echo -e "${BLUE}Running Story #2082 generated reference doc check...${NC}"
    local reference_docs_current=true
    for generator in cli_reference mcp_tools error_codes; do
        if ! PYTHONPATH=src python3 -m "tools.docs.${generator}" --check; then
            reference_docs_current=false
        fi
    done
    if $reference_docs_current; then
        echo -e "${GREEN}✅ Generated reference doc check passed${NC}"
    else
        echo -e "${RED}❌ Generated reference doc check failed${NC}"
        all_passed=false
    fi

    if $all_passed; then
        echo -e "${GREEN}🎉 All linting checks passed!${NC}"
        exit 0
    else
        echo -e "${RED}💥 Some linting checks failed${NC}"
        exit 1
    fi
}

# Run main function
main "$@"
