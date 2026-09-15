#!/bin/bash

# Fast automation script - CIDX fast unit tests only
# Runs pure unit tests that don't require external dependencies:
# - No real servers or API calls
# - No containers (Docker, Qdrant, Ollama)
# - No external APIs (VoyageAI, auth servers)
# - No special permissions (/var/lib access)
# Use server-fast-automation.sh for tests with dependencies

set -e  # Exit on any error

# TELEMETRY: Create telemetry directory for test performance tracking
TELEMETRY_DIR=".test-telemetry"
mkdir -p "$TELEMETRY_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY_FILE="$TELEMETRY_DIR/fast-automation-${TIMESTAMP}.log"
DURATION_FILE="$TELEMETRY_DIR/test-durations-${TIMESTAMP}.txt"

# Source .env files if they exist (for local testing)
if [[ -f ".env.local" ]]; then
    source .env.local
fi
if [[ -f ".env" ]]; then
    source .env
fi

echo "🖥️  Starting CLI-focused fast automation pipeline..."
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

# Bug #1863: pre-run load check — warn (never block) if the box is already busy
# before this suite starts. This is a single pytest process with no self-
# contention, but it still slows down (and can cross the pytest-timeout
# ceiling) under load from anything else running on the box.
# LOAD_WARN_CORES_DIVISOR: warn once 1-min load exceeds cores / this divisor
# (i.e. "roughly half the cores", per Bug #1863's diagnosis).
#
# Cross-reference: server-fast-automation.sh carries an identical copy of
# check_load_before_run()/report_pytest_timeout_diagnostics() (Bug #1863).
# Keep both copies in sync when editing either one.
LOAD_WARN_CORES_DIVISOR=2
# Stashed by check_load_before_run() so report_pytest_timeout_diagnostics()
# can report load at BOTH suite start and failure time. A load reading taken
# only after a long suite finishes is meaningless for explaining what killed
# a test many minutes earlier.
PRERUN_LOAD1="unknown"
check_load_before_run() {
    local cores load1 threshold
    cores=$(nproc 2>/dev/null) || return 0
    [ -n "$cores" ] || return 0
    [ -r /proc/loadavg ] || return 0
    load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null) || return 0
    [ -n "$load1" ] || return 0
    PRERUN_LOAD1="$load1"
    threshold=$(awk -v c="$cores" -v d="$LOAD_WARN_CORES_DIVISOR" 'BEGIN{printf "%.2f", c/d}') || return 0
    [ -n "$threshold" ] || return 0
    if awk -v l="$load1" -v t="$threshold" 'BEGIN{exit !(l > t)}'; then
        echo ""
        print_warning "HIGH LOAD before this suite has even started: 1-min load average"
        print_warning "is ${load1} on ${cores} cores (warn threshold: load > ${threshold})."
        print_warning "Results below may be UNRELIABLE under this load — this suite's own"
        print_warning "tests have been measured running up to ~5x slower loaded vs idle"
        print_warning "(Bug #1863). Likely offenders (top CPU consumers):"
        ps -eo pid,etimes,pcpu,args --sort=-pcpu | head
        print_warning "Warning only — the run will continue."
        echo ""
    fi
    return 0
}

# Bug #1863: post-run diagnostic — a pytest-timeout wall-clock ceiling (suite
# default $PYTEST_TIMEOUT via --timeout, but any individual test may override
# it with @pytest.mark.timeout(N)) kills tests that would otherwise pass, and
# those expiries read exactly like ordinary assertion failures in the summary
# above, which has repeatedly cost investigation time chasing a phantom
# regression. This labels each timeout with its ACTUAL per-test ceiling.
# It does NOT change the exit code and is NOT A WAIVER: a timeout still fails
# the gate, and a failed gate still needs a real resolution — this only
# explains what kind of failure occurred so it can be triaged correctly.
# Scoped to THIS run's own log file only.
report_pytest_timeout_diagnostics() {
    local logs=("$@")
    local existing=()
    local log
    for log in "${logs[@]}"; do
        [ -f "$log" ] && existing+=("$log")
    done
    [ ${#existing[@]} -gt 0 ] || return 0

    local timeout_count
    timeout_count=$(grep -h "from pytest-timeout" "${existing[@]}" 2>/dev/null | wc -l | tr -d ' ') || true
    [ -n "$timeout_count" ] || timeout_count=0
    [ "$timeout_count" -gt 0 ] || return 0

    local timeout_names
    timeout_names=$(awk '
        /^_+ .+ _+$/ {
            line = $0
            sub(/^_+ /, "", line)
            sub(/ _+$/, "", line)
            current = line
        }
        /from pytest-timeout/ {
            secs = "?"
            if (match($0, /Timeout \(>[0-9.]+s\)/))
                secs = substr($0, RSTART+10, RLENGTH-11)
            print "  - " current "   [ceiling " secs "]   (" FILENAME ")"
        }
    ' "${existing[@]}" 2>/dev/null) || timeout_names="(timeout test names unavailable)"

    local cores load1
    cores=$(nproc 2>/dev/null) || cores="unknown"
    load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null) || load1="unknown"

    echo ""
    echo -e "${YELLOW}=================================================================="
    echo "⏱️  PYTEST-TIMEOUT DIAGNOSTIC (Bug #1863)"
    echo -e "==================================================================${NC}"
    echo "THE GATE FAILED. $timeout_count of the failure(s) above were pytest-timeout"
    echo "wall-clock expiries rather than failed assertions. This is an EXPLANATION,"
    echo "NOT a waiver — the gate is still red and still requires a real resolution."
    echo ""
    echo "This suite's default ceiling is ${PYTEST_TIMEOUT}s (via --timeout), but any"
    echo "individual test can override it with @pytest.mark.timeout(N). The ceiling"
    echo "that actually killed each test below is shown per line:"
    echo ""
    echo "$timeout_names"
    echo ""
    echo "Load average (1m): at suite start ${PRERUN_LOAD1}   |   now ${load1}   |   cores: ${cores}"
    echo ""
    echo "How to read the ceiling column:"
    echo "  - A timeout AT the suite default (${PYTEST_TIMEOUT}s) under real load is often"
    echo "    the headroom lottery — this suite's tests have been measured running up"
    echo "    to ~5x slower loaded vs idle (Bug #1863)."
    echo "  - A timeout WELL ABOVE the suite default came from an explicit"
    echo "    @pytest.mark.timeout(N) marker (commonly a deadlock/concurrency guard)"
    echo "    and usually is NOT a load artifact — treat it as a real failure that"
    echo "    needs investigation."
    echo ""
    echo "Re-check yourself: grep -c \"from pytest-timeout\" ${TELEMETRY_FILE}"
    echo -e "${YELLOW}==================================================================${NC}"
    echo ""
}

# Check if we're in the right directory
if [[ ! -f "pyproject.toml" ]]; then
    print_error "Not in project root directory (pyproject.toml not found)"
    exit 1
fi

# Check Python version (GitHub Actions tests multiple versions, we'll use current)
print_step "Checking Python version"
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d " " -f 2)
echo "Using Python $PYTHON_VERSION"
print_success "Python version checked"

# 1. Install dependencies (same as GitHub Actions)
print_step "Installing dependencies"
# Workaround for pip editable install path resolution issue:
# Install from parent directory to avoid path doubling bug
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

# 2. Lint CLI-related code with ruff
print_step "Running ruff linter on CLI code"
if ruff check src/code_indexer/cli.py src/code_indexer/mode_* src/code_indexer/remote/ src/code_indexer/api_clients/ tests/unit/cli/ tests/unit/remote/ tests/unit/api_clients/; then
    print_success "CLI ruff linting passed"
else
    print_error "CLI ruff linting failed"
    exit 1
fi

# 3. Check CLI code formatting with ruff format
print_step "Checking CLI code formatting with ruff format"
if ruff format --check src/code_indexer/cli.py src/code_indexer/mode_* src/code_indexer/remote/ src/code_indexer/api_clients/ tests/unit/cli/ tests/unit/remote/ tests/unit/api_clients/; then
    print_success "CLI ruff formatting check passed"
else
    print_error "CLI ruff formatting check failed"
    print_warning "Run 'ruff format' on the CLI-related files to fix formatting"
    exit 1
fi

# 4. Type check CLI code with mypy
print_step "Running mypy type checking on CLI code"
if mypy src/code_indexer/cli.py src/code_indexer/mode_* src/code_indexer/remote/ src/code_indexer/api_clients/ --ignore-missing-imports; then
    print_success "CLI MyPy type checking passed"
else
    print_error "CLI MyPy type checking failed"
    exit 1
fi

# 5. Run FAST unit tests only (excluding external dependencies)
print_step "Running fast unit tests (no external services)"
echo "ℹ️  Testing FAST unit test functionality including:"
echo "   • Command-line interface parsing and validation"
echo "   • Configuration and mode detection"
echo "   • Core business logic (without API calls)"
echo "   • Text processing and chunking"
echo "   • Progress reporting and display"
echo "   • Error handling and validation"
echo ""
echo "⚠️  EXCLUDED: Tests requiring real servers, containers, or external APIs"

# Tuning knob — override via environment for CI or local profiling. Shared with
# report_pytest_timeout_diagnostics() (Bug #1863), which prints this value when
# labeling a timeout-caused failure.
PYTEST_TIMEOUT="${PYTEST_TIMEOUT:-15}"
# Bug #1863: echo the effective ceiling once. .env/.env.local are sourced near
# the top of this script and pytest-timeout itself also reads PYTEST_TIMEOUT
# from the environment, so a stray override there would otherwise silently
# weaken hang detection with no visible trace in the run's output.
echo "⏱️  pytest-timeout ceiling for this run: ${PYTEST_TIMEOUT}s (override via PYTEST_TIMEOUT env var)"

check_load_before_run

# Run ONLY fast unit tests that don't require external services
# TELEMETRY: Add --durations=0 to capture ALL test durations
echo "📊 Telemetry enabled: Results will be saved to $TELEMETRY_FILE"
echo "⏱️  Duration report: $DURATION_FILE"
#
# --deselect audit (GitHub issue #1685): every --deselect below MUST be either
# a documented live-masked failure with its own tracking issue (listed here),
# or it should not exist -- a stale/dead deselect is a silent gate weakener.
python3 -m pytest \
    tests/unit/ \
    --durations=0 \
    --ignore=tests/unit/server/ \
    --ignore=tests/unit/infrastructure/ \
    --ignore=tests/unit/api_clients/test_base_cidx_remote_api_client_real.py \
    --ignore=tests/unit/api_clients/test_remote_query_client_real.py \
    --ignore=tests/unit/api_clients/test_repository_linking_client_real.py \
    --ignore=tests/unit/api_clients/test_jwt_token_manager_real.py \
    --ignore=tests/unit/api_clients/test_real_api_integration_required.py \
    --ignore=tests/unit/api_clients/test_messi_rule2_compliance.py \
    --ignore=tests/unit/api_clients/test_admin_api_client.py \
    --ignore=tests/unit/api_clients/test_admin_client_golden_repos_maintenance.py \
    --ignore=tests/unit/api_clients/test_jobs_cancel_status_real_integration.py \
    --ignore=tests/unit/api_clients/test_base_cidx_remote_api_client.py \
    --ignore=tests/unit/api_clients/test_jobs_api_client_tdd.py \
    --ignore=tests/unit/api_clients/test_isolation_utils.py \
    --ignore=tests/unit/api_clients/test_jobs_api_client_cancel_tdd.py \
    --ignore=tests/unit/api_clients/test_remote_query_client.py \
    --ignore=tests/unit/api_clients/test_repos_client_tdd.py \
    --ignore=tests/unit/cli/test_admin_commands.py \
    --ignore=tests/unit/cli/test_explicit_authentication_commands.py \
    --ignore=tests/unit/cli/test_jobs_cli_e2e_tdd.py \
    --ignore=tests/unit/cli/test_password_security_validation.py \
    --ignore=tests/unit/cli/test_server_lifecycle_commands.py \
    --ignore=tests/unit/cli/test_sync_command_structure.py \
    --ignore=tests/unit/cli/test_cli_init_segment_size.py \
    --ignore=tests/unit/cli/test_cli_issues_tdd_fix.py \
    --ignore=tests/unit/cli/test_cli_response_parsing_errors.py \
    --ignore=tests/unit/cli/test_cli_error_propagation_fixes.py \
    --ignore=tests/unit/cli/test_jobs_cancel_status_command_tdd.py \
    --ignore=tests/unit/cli/test_jobs_command_tdd.py \
    --ignore=tests/unit/cli/test_repos_commands_tdd.py \
    --ignore=tests/unit/cli/test_repository_activation_lifecycle.py \
    --ignore=tests/unit/cli/test_repository_branch_switching.py \
    --ignore=tests/unit/cli/test_repository_info_command.py \
    --ignore=tests/unit/cli/test_resource_cleanup_verification.py \
    --ignore=tests/unit/cli/test_authentication_status_management.py \
    --ignore=tests/unit/cli/test_admin_repos_integration_validation.py \
    --ignore=tests/unit/cli/test_daemon_delegation.py \
    --ignore=tests/unit/cli/test_query_fts_flags.py \
    --ignore=tests/unit/cli/test_staleness_display_integration.py \
    --ignore=tests/unit/cli/test_start_stop_backend_integration.py \
    --ignore=tests/unit/cli/test_cli_clear_temporal_progress.py \
    --ignore=tests/unit/cli/test_cli_fast_path.py \
    --ignore=tests/unit/cli/test_cli_temporal_display_comprehensive.py \
    --ignore=tests/unit/cli/test_cli_temporal_display_story2_1.py \
    --ignore=tests/unit/cli/test_improved_remote_query_experience.py \
    --ignore=tests/unit/cli/test_path_pattern_performance.py \
    --ignore=tests/unit/cli/test_status_temporal_performance.py \
    --ignore=tests/unit/cli/test_index_commits_clear_bug.py \
    --ignore=tests/unit/storage/test_filesystem_git_batch_limits.py \
    --ignore=tests/unit/storage/test_hnsw_incremental_batch.py \
    --ignore=tests/unit/remote/test_timeout_management.py \
    --ignore=tests/unit/performance/test_exclusion_filter_performance.py \
    --ignore=tests/unit/integration/ \
    --ignore=tests/unit/documentation/test_fixed_size_chunking_documentation.py \
    --ignore=tests/unit/cli/test_status_temporal_storage_size_bug.py \
    --ignore=tests/unit/services/test_tantivy_language_filter.py \
    --ignore=tests/unit/cli/test_index_delegation_progress.py \
    --ignore=tests/unit/cli/test_cli_option_conflict_fix.py \
    --ignore=tests/unit/test_codebase_audit_story9.py \
    --ignore=tests/unit/daemon/test_display_timing_fix.py \
    --ignore=tests/unit/services/test_clean_file_chunking_manager.py \
    --ignore=tests/unit/services/test_file_chunk_batching_optimization.py \
    --ignore=tests/unit/services/test_daemon_fts_cache_performance.py \
    --ignore=tests/unit/services/test_rpyc_daemon.py \
    --ignore=tests/unit/services/test_voyage_threadpool_elimination.py \
    --ignore=tests/unit/services/test_tantivy_regex_optimization.py \
    --ignore=tests/unit/services/test_tantivy_path_filter.py \
    --ignore=tests/unit/services/test_tantivy_limit_zero.py \
    --ignore=tests/unit/services/test_tantivy_search.py \
    --ignore=tests/unit/services/test_tantivy_regex_snippet_extraction.py \
    --ignore=tests/unit/cli/test_admin_repos_functionality_verification.py \
    --ignore=tests/unit/cli/test_admin_repos_maintenance_commands.py \
    --ignore=tests/unit/cli/test_admin_repos_add_simple.py \
    --ignore=tests/unit/cli/test_admin_repos_delete_command.py \
    --ignore=tests/unit/cli/test_admin_repos_delete_integration_e2e.py \
    --ignore=tests/unit/cli/test_password_management_commands.py \
    --ignore=tests/unit/test_scip_database_queries.py \
    --ignore=tests/unit/test_scip_generator.py \
    --ignore=tests/unit/clients/test_gitlab_ci_client.py \
    --ignore=tests/unit/clients/test_github_actions_client.py \
    --ignore=tests/unit/global_repos/test_git_operations.py \
    --ignore=tests/unit/global_repos/test_refresh_scheduler_locking.py \
    --ignore=tests/unit/services/test_tantivy_incremental_updates.py \
    --ignore=tests/unit/tools/test_convert_tool_docs.py \
    --ignore=tests/unit/cli/test_admin_password_change_command.py \
    --ignore=tests/unit/cli/test_repos_list_fix_verification.py \
    --ignore=tests/unit/cli/test_system_health_commands.py \
    --ignore=tests/unit/remote/test_network_error_handling.py \
    --ignore=tests/unit/global_repos/test_global_registry_locking.py \
    --ignore=tests/unit/routers/test_repo_categories_api.py \
    --ignore=tests/unit/services/test_git_credential_manager.py \
    --ignore=tests/unit/services/test_repo_category_service_auto_assign.py \
    --ignore=tests/unit/services/test_repo_category_service_bulk_evaluate.py \
    --ignore=tests/unit/services/test_repo_category_service_manual_override.py \
    --ignore=tests/unit/services/test_repo_category_service_map.py \
    --ignore=tests/unit/services/test_repo_category_service.py \
    --ignore=tests/unit/services/test_tantivy_regex_dfa_safety.py \
    --ignore=tests/unit/services/test_tantivy_regex.py \
    --ignore=tests/unit/services/test_tantivy_unicode_columns.py \
    --ignore=tests/unit/services/test_token_authenticator.py \
    --ignore=tests/unit/storage/test_repo_category_backend.py \
    --ignore=tests/unit/storage/test_sqlite_backends_category.py \
    --ignore=tests/unit/test_scip_audit_api.py \
    --deselect=tests/unit/query/test_query_parameter_parity.py::TestQueryParameterParity::test_no_extra_mcp_parameters \
    --deselect=tests/unit/query/test_query_parameter_parity.py::TestQueryParameterParity::test_parameter_name_consistency_rest_mcp \
    --deselect=tests/unit/services/temporal/test_temporal_worker_exception_logging.py::test_worker_exception_is_logged_and_propagated \
    --deselect=tests/unit/services/test_cohere_embedding.py::TestCohereErrorHandling401Bug595Issue2::test_401_error_message_mentions_api_key \
    --deselect=tests/unit/services/test_cohere_embedding.py::TestCohereErrorHandling401Bug595Issue2::test_401_response_raises_value_error \
    --deselect=tests/unit/services/test_cohere_embedding.py::TestCohereRetryLoopBug595Issue1::test_network_error_does_not_propagate_as_raw_exception \
    --deselect=tests/unit/services/test_cohere_embedding.py::TestCohereRetryLoopBug595Issue1::test_network_error_on_last_attempt_raises_runtime_error \
    --deselect=tests/unit/services/test_cohere_embedding.py::TestCohereRetryLoopBug595Issue1::test_runtime_error_mentions_attempt_count \
    --deselect=tests/unit/services/test_cohere_embedding.py::TestConnectReadTimeoutSplit::test_cohere_uses_split_timeout \
    --deselect=tests/unit/services/test_single_embedding_wrapper.py::TestSingleEmbeddingWrapperIntegration::test_cli_compatibility_preserved \
    --deselect=tests/unit/services/test_single_embedding_wrapper.py::TestSingleEmbeddingWrapperIntegration::test_integration_single_embedding_via_batch \
    --deselect=tests/unit/services/test_single_embedding_wrapper.py::TestSingleEmbeddingWrapper::test_get_embedding_method_signature_unchanged \
    --deselect=tests/unit/storage/test_api_metrics_backend.py::TestBackendRegistryApiMetricsField::test_storage_factory_api_metrics_backend_is_functional \
    --deselect=tests/unit/storage/test_description_refresh_tracking_schema.py::TestDescriptionRefreshTrackingSchema::test_status_has_default_value_pending \
    --deselect=tests/unit/storage/test_filesystem_hnsw_integration.py::TestHNSWSearchPath::test_search_uses_hnsw_index \
    --deselect=tests/unit/storage/test_parallel_index_loading.py::TestParallelExecutionMechanism::test_search_accepts_query_parameter_for_parallel_execution \
    --deselect=tests/unit/test_scip_database_schema.py::TestIndexCreation::test_create_indexes_creates_all_indexes \
    --deselect=tests/unit/tools/perf_suite/test_report.py::TestHardwareProfileSection::test_hardware_capture_with_invalid_host_returns_none \
    -m "not slow and not e2e and not real_api and not integration and not requires_server and not requires_containers and not performance" \
    --timeout="$PYTEST_TIMEOUT" \
    2>&1 | tee "$TELEMETRY_FILE"

PYTEST_EXIT_CODE=${PIPESTATUS[0]}

# TELEMETRY: Extract duration data
grep -E "^[0-9]+\.[0-9]+s (call|setup|teardown)" "$TELEMETRY_FILE" | sort -rn > "$DURATION_FILE"

# TELEMETRY: Summary
TOTAL_TIME=$(grep "passed in" "$TELEMETRY_FILE" | grep -oE "[0-9]+\.[0-9]+s" | head -1)
SLOW_TESTS=$(awk '$1 > 5.0' "$DURATION_FILE" | wc -l)

echo ""
echo "📊 TELEMETRY: Total=$TOTAL_TIME, Slow(>5s)=$SLOW_TESTS"
echo "   Log: $TELEMETRY_FILE"
echo "   Durations: $DURATION_FILE"

ln -sf "$(basename $TELEMETRY_FILE)" "$TELEMETRY_DIR/latest.log"
ln -sf "$(basename $DURATION_FILE)" "$TELEMETRY_DIR/latest-durations.txt"

if [ $PYTEST_EXIT_CODE -eq 0 ]; then
    print_success "Fast unit tests passed"
else
    print_error "Fast unit tests failed with exit code $PYTEST_EXIT_CODE"
    report_pytest_timeout_diagnostics "$TELEMETRY_FILE"
    exit $PYTEST_EXIT_CODE
fi

# Note: GitHub Actions also has version checking and publishing steps
# but those are only relevant for actual GitHub runs

# Summary
echo -e "\n${GREEN}🎉 Fast automation completed successfully!${NC}"
echo "==========================================="
echo "✅ Linting passed"
echo "✅ Formatting checked"
echo "✅ Type checking passed"
echo "✅ Fast unit tests passed"
echo ""
echo "🖥️  FAST test coverage (no external dependencies):"
echo "   ✅ Core CLI parsing and validation"
echo "   ✅ Configuration management and mode detection"
echo "   ✅ Business logic without API calls"
echo "   ✅ Text processing and chunking"
echo "   ✅ Error handling and validation"
echo "   ✅ Progress reporting and display logic"
echo ""
echo "🚫 EXCLUDED (for speed):"
echo "   • Tests requiring real servers (test_*_real.py)"
echo "   • Tests requiring containers (infrastructure, services)"
echo "   • Tests requiring external APIs (VoyageAI, auth servers)"
echo "   • Tests requiring special permissions (/var/lib access)"
echo "   • Slow integration and e2e tests"
echo ""
echo "⚡ Fast automation focuses on pure unit tests only!"
echo "ℹ️  Run 'server-fast-automation.sh' for server tests with dependencies"
echo "ℹ️  Run 'full-automation.sh' for complete integration testing"
echo "CIDX core logic validated! 🚀"
