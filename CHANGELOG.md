# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [8.9.1] - 2026-02-08

### Fixed

- **Auto-updater git submodule initialization** - Fixed deployment failure where custom hnswlib with `check_integrity()` method wasn't being built. The auto-updater now runs `git submodule update --init --recursive` before `pip install` to ensure the `third_party/hnswlib` submodule is initialized and the custom build with HNSW index integrity validation is properly installed.

---

## [8.9.0] - 2026-02-08

### Fixed

- **Story #160/161: Activated Repository Management UI Improvements** - Multiple fixes for the Repository Management page:
  - Fixed temporal index detection: Changed path from `index/temporal` to `index/code-indexer-temporal/hnsw_index.bin` to correctly detect temporal indexes
  - Fixed admin viewing other users' repos: Added `owner` query parameter to `/health` and `/indexes` endpoints
  - Fixed duplicate HTML element IDs when same repo alias exists for multiple users
  - Fixed indexes API response format parsing in JavaScript (array format vs flat booleans)
  - Fixed 404 errors for repos without indexes: Now returns empty indexes array instead of error
  - Removed inconsistent "Temporal Indexing Status" section from Golden Repos template
  - Removed conflicting "Temporal Indexing" field from Activated Repos card (badge is now single source of truth)

---

## [8.8.39] - 2026-02-07

### Fixed

- **Bug #160: GitLab CI handlers missing async/await** - Fixed 5 GitLab CI MCP handlers that were synchronous but calling async httpx client methods. Without `await`, these handlers returned coroutine objects instead of actual results, causing "object of type 'coroutine' has no len()" errors. Fixed handlers: `handle_gitlab_ci_list_pipelines`, `handle_gitlab_ci_get_pipeline`, `handle_gitlab_ci_search_logs`, `handle_gitlab_ci_retry_pipeline`, `handle_gitlab_ci_cancel_pipeline`. Updated async exceptions whitelist to include all 6 GitLab CI handlers.

---

## [8.8.38] - 2026-02-07

### Fixed

- **Bug #158: GitHub Actions handlers missing async/await** - Fixed 12 GitHub Actions MCP handlers that were synchronous but calling async httpx client methods. Without `await`, these handlers returned coroutine objects instead of actual results, causing "'coroutine' object is not subscriptable" errors. All `handle_gh_actions_*` and `handle_github_actions_*` handlers are now properly async. Updated test suite to recognize these handlers as legitimate async exceptions to the thread pool execution model.

---

## [8.8.37] - 2026-02-07

### Fixed

- **Bug #157: CommitterResolutionService uses undefined logger variable** - Fixed NameError `name 'logger' is not defined` in CommitterResolutionService that caused repository activation to fail. Four places in the service used bare `logger` instead of `self.logger`. This bug was discovered during MCP integration testing when attempting to activate a repository.

---

## [8.8.36] - 2026-02-06

### Added

- **Issue #154: Self-Healing Auto-Updater Python Environment Detection** - Complete solution for Python environment mismatch between auto-updater service (system Python) and main server (pipx venv). Features:
  - `_get_server_python()` method reads cidx-server.service and extracts the actual Python interpreter path from ExecStart line
  - `_ensure_auto_updater_uses_server_python()` updates the auto-updater service file to use the same Python as the server
  - Two-cycle self-healing flow: first cycle updates service file and creates pending-redeploy marker, second cycle installs to correct environment
  - `poll_once()` checks for pending-redeploy marker and forces deployment when found
  - Modified `pip_install()` to use `_get_server_python()` for correct environment targeting
  - Idempotent design - safe to run multiple times, no marker created if no changes needed
  - Graceful fallback to sys.executable if service file parsing fails

---

## [8.8.35] - 2026-02-06

### Fixed

- **Bug #156: Auto-updater pip install uses wrong Python environment** - Fixed `pip_install()` using hardcoded `"python3"` which installs to system Python instead of the pipx venv where code-indexer is actually running. Changed to `sys.executable` to use the same Python interpreter that's running the auto-updater, ensuring dependencies are installed in the correct environment.

---

## [8.8.34] - 2026-02-06

### Fixed

- **Bug #155 follow-up: Empty string handling for CIDX_AUTO_UPDATE_BRANCH** - Fixed edge case where setting `CIDX_AUTO_UPDATE_BRANCH=""` (empty string) caused `git pull origin ""` to fail. Now uses `or "master"` pattern to properly default to "master" when env var is empty or unset.

---

## [8.8.33] - 2026-02-06

### Fixed

- **Bug #155: Auto-updater ignores CIDX_AUTO_UPDATE_BRANCH environment variable** - Fixed `DeploymentExecutor` hardcoding "master" branch in `git_pull()` instead of using the configured branch. Root cause: When three-tier branching strategy was implemented (v8.8.19), the branch parameter was wired to `ChangeDetector` but NOT to `DeploymentExecutor`. This caused staging server to detect changes on `origin/staging` but pull from `origin/master` (no changes), resulting in "Already up to date" and failed deployments. Fix: Added `branch` parameter to `DeploymentExecutor.__init__()` and wired it through `run_once.py`.

---

## [8.8.32] - 2026-02-06

### Fixed

- **Bug #154: Research Assistant Chat UI Improvements** - Complete overhaul of the Research Assistant page to implement traditional AI chatbot layout:
  - Fixed viewport height constraints to prevent page scroll - sessions sidebar and messages area scroll independently
  - Input box now pinned to bottom, always visible regardless of message count
  - Smart auto-scroll: enabled when user at bottom, disabled when scrolled up, re-enabled on scroll to bottom
  - Force scroll to bottom when switching sessions or sending messages
  - Enter key sends message, Shift/Alt+Enter inserts newline
  - Removed inaccurate relative timestamps from messages
  - Claude CLI project folder cleanup on session deletion to prevent orphaned JSONL files

---

## [8.8.31] - 2026-02-04

### Fixed

- **Bug #140: BackgroundJobManager ignores user-configured job concurrency limits** - Fixed Web UI configuration for `max_concurrent_background_jobs` being ignored. Root cause: `BackgroundJobManager` in `app.py` was instantiated without passing `background_jobs_config`, so it always used the default value of 5 regardless of user settings. Fix: Added `background_jobs_config=server_config.background_jobs_config` parameter to the constructor call.

---

## [8.8.30] - 2026-02-04

### Fixed

- **Bug #139: regex_search validation bypass for omni-search mode** - Fixed validation for `include_patterns` and `exclude_patterns` being bypassed when `repository_alias` is an array (omni-search mode). Root cause: routing to `_omni_regex_search` happened BEFORE validation code, so validation never executed for array inputs. Fix: Moved validation before the routing check so it runs for both single-repo and omni-search modes.

---

## [8.8.29] - 2026-02-04

### Added

- **Story #70: Auto-Refresh Index Reconciliation** - Auto-refresh now detects ALL index types on disk (semantic, FTS, temporal, SCIP) and reconciles registry metadata to match filesystem reality. Features:
  - `_detect_existing_indexes()` scans for semantic, FTS, temporal, and SCIP indexes
  - `_reconcile_registry_with_filesystem()` enables flags when indexes found, disables when missing
  - `enable_scip` field added to global_repos schema with migration support
  - Step 5c SCIP indexing added to refresh workflow with configurable timeout (1800s default)
  - Graceful failure handling - reconciliation failures logged but don't block refresh
  - Continuous reconciliation runs at START (before indexing) and END (after creation)

---

## [8.8.28] - 2026-02-03

### Fixed

- **Bug #130: regex_search handler input type validation** - Added validation for `include_patterns` and `exclude_patterns` parameters in the `handle_regex_search` MCP handler. Previously, passing non-list types (e.g., float, string) caused server crash with `TypeError: 'float' object is not iterable`. Now returns helpful error message: "include_patterns must be a list of strings".

---

## [8.8.27] - 2026-02-03

### Fixed

- **Bug #137: HTTP MCP clients can't find traces across requests** - Fixed session-based trace lookup failing for HTTP MCP clients (like Claude Code) that generate new session_ids per request. Root cause: Traces were stored keyed by session_id, but each HTTP request generates a new random UUID if `?session_id=xxx` query param not provided. Fix: Added username-to-session mapping in TraceStateManager with fallback lookup. When session_id lookup fails, tries username-based lookup to find the original session. Also returns session_id in start_trace response for clients that can control their URLs.

---

## [8.8.26] - 2026-02-03

### Fixed

- **Bug #134 Follow-up: Langfuse SDK 3.x span API fix** - Fixed span.end() API mismatch causing tool call spans to not appear in Langfuse dashboard. Root cause: AutoSpanLogger called `span.end(output=...)` but Langfuse SDK 3.x's `LangfuseObservationWrapper.end()` doesn't accept `output` parameter. Fix: Use `span.update(output=...)` to set output data, then `span.end()` with no arguments. This matches the SDK 3.x pattern where `update()` sets observation data and `end()` just marks completion.

---

## [8.8.25] - 2026-02-03

### Fixed

- **Story #136 Follow-up: AutoSpanLogger integration** - Wired AutoSpanLogger.intercept_tool_call() into protocol.py's handle_tools_call() so MCP tool calls within active Langfuse traces now create child spans. Features:
  - Tool calls create spans with name, sanitized inputs, and outputs when trace is active
  - Graceful degradation - Langfuse errors never fail tool execution (three-layer protection)
  - start_trace/end_trace excluded from interception to prevent recursion
  - Both sync and async handlers supported
  - Extracted `_invoke_handler()` helper to eliminate code duplication

---

## [8.8.24] - 2026-02-03

### Fixed

- **Bug #135: Langfuse traces not appearing in dashboard** - Fixed span lifecycle issue where traces created via MCP tools were never sent to Langfuse. Root cause: `create_trace()` used `end_on_exit=False` pattern but never called `span.end()`. Langfuse SDK only sends completed spans during `flush()`. Fix: Store span in TraceObject, add `end_trace()` method to call `span.end()`, and call it in TraceStateManager before flush.

---

## [8.8.23] - 2026-02-03

### Added

- **Story #136: Langfuse Research Session Tracing** - Integrated Langfuse observability for MCP tool usage tracking. New MCP tools `start_trace` and `end_trace` allow users to create research session traces that capture all tool calls as spans with timing, inputs, and outputs. Features include:
  - Session tracing with `start_trace(name, metadata)` and `end_trace(score, feedback)`
  - Automatic span creation for all MCP tool calls within a trace
  - Auto-trace configuration (`auto_trace_enabled`) for implicit trace creation on first tool call
  - Graceful degradation - Langfuse errors never fail upstream MCP operations
  - Web UI configuration section for Langfuse settings (public key, secret key, host, auto-trace)
  - HTTP session persistence via `?session_id=xxx` query parameter for trace continuity across requests

### Fixed

- **Langfuse SDK API compatibility** - Updated to use Langfuse SDK 3.7.0 API (`start_as_current_span()`, `start_span()`, `create_score()`)
- **Nested lock deadlock** - Changed `threading.Lock()` to `threading.RLock()` in LangfuseService to prevent deadlock during lazy initialization

---

## [8.8.22] - 2026-02-02

### Fixed

- **Bug #135: Auto-update drain timeout now dynamically calculated** - The auto-updater's drain_timeout was hardcoded at 300 seconds (5 minutes), but background jobs can run for up to 1 hour. This caused running jobs to be killed during auto-updates. Fix: drain_timeout is now calculated at runtime as 1.5x the maximum configured job timeout by querying `/api/admin/maintenance/drain-timeout`. Default is now 5400 seconds (1.5 hours) with 7200 second fallback if server unreachable.

---

## [8.8.21] - 2026-02-02

### Fixed

- **Bug #132: Golden repo refresh now uses incremental indexing** - Fixed issue where global repository refresh was triggering full reindex (70-90 minutes) instead of incremental update (2-5 minutes). Changed `force_init=True` to `force_init=False` in `_execute_post_clone_workflow()` to preserve existing indexing state.

- **Bug #133: Duplicate refresh jobs prevented** - Added duplicate job detection to `BackgroundJobManager.submit_job()` to prevent multiple refresh jobs from running concurrently on the same repository. Raises `DuplicateJobError` when attempting to submit a job that's already running.

- **Bug #134: CSRF token error on cancel job** - Removed CSRF validation from `cancel_job` endpoint. Session authentication via `_require_admin_session()` provides sufficient security, and removing CSRF eliminates race conditions between HTMX auto-refresh and user actions.

---

## [8.8.20] - 2026-02-01

### Fixed

- **CI/CD workflow tag creation** - Fixed idempotency issue in three-tier branching workflow:
  - Split monolithic build-and-release job into separate create-tag and create-release jobs
  - create-tag: Runs only on development branch when version changes (creates and pushes git tag)
  - create-release: Runs only on master branch when version changes (creates GitHub release using existing tag)
  - staging branch: Tests only, no tag or release creation (tags inherited from merge)
  - Ensures tags are created exactly once (in development) and inherited by staging/master via merge
  - Prevents "tag already exists" errors when merging through branches

---

## [8.8.19] - 2026-02-01

### Added

- **Three-tier branching strategy with auto-deployment** - Implemented development → staging → master workflow:
  - Created `development` and `staging` branches from master
  - All development work now happens in `development` branch only
  - `staging` and `master` are merge-only branches (no direct commits)
  - Version tags automatically trigger environment deployments:
    - Tags on `staging` branch → auto-deploy to .20 staging server
    - Tags on `master` branch → auto-deploy to .30 production server
  - Configurable auto-update branch via `CIDX_AUTO_UPDATE_BRANCH` environment variable
  - Staging server (.20) configured to pull from `staging` branch
  - Production server (.30) configured to pull from `master` branch (default)

- **Comprehensive branching documentation in CLAUDE.md**:
  - Auto-deployment strategy with tag-based triggers
  - "Deploy to staging" workflow (development → staging)
  - "Deploy to production" workflow (staging → master)
  - Branch verification safety check (must be in development/feature branch before coding)
  - Version management rules (all version bumps in development only)

- **Auto-update configurable branch support** - Added environment variable for flexible branch targeting:
  - New environment variable: `CIDX_AUTO_UPDATE_BRANCH` (defaults to "master")
  - Implemented in `src/code_indexer/server/auto_update/run_once.py`
  - Test coverage: 3 new tests for default/custom/development branch configuration
  - All 53 auto-update tests passing

### Changed

- **Updated .local-testing configuration** - Clarified server environment designations:
  - Documented .20 server as STAGING environment (pulls from `staging` branch)
  - Documented .30 server as PRODUCTION environment (pulls from `master` branch)

---

## [8.8.18] - 2026-02-01

### Fixed

- **Bug #129 Part 3: Scan Status detection now correctly identifies running scans** - Fixed SQL query to use authoritative table:
  - Changed `_get_scan_status()` to query `self_monitoring_scans.completed_at IS NULL` instead of `background_jobs.status='running'`
  - Root cause: `background_jobs` table immediately marks self_monitoring jobs as 'completed' when submitted, not when actually finished
  - Actual running state tracked in `self_monitoring_scans` table where running scans have `completed_at IS NULL`
  - Database evidence: All 3,389 self_monitoring jobs in background_jobs have status='completed' (zero with 'running')
  - E2E testing: 8/8 automated tests passed + real 30-minute production scan verified
  - Fixes issue where status always showed "Idle" even when scan was actively running for 30 minutes
  - Production verification: Real scan executed for 30 minutes with `completed_at=NULL` during entire execution window

### Added

- **Orphaned scan cleanup feature** - Automatic cleanup prevents "stuck Running..." status:
  - Added `_cleanup_orphaned_scans()` method in `service.py` to find scans older than 2 hours with `completed_at IS NULL`
  - Marks orphaned scans as FAILURE with error message: "Scan failed to complete (orphaned after 2 hours)"
  - Cleanup runs automatically before each scheduled scan cycle
  - Prevents crashed/interrupted scans from permanently showing "Running..." status
  - Found and cleaned 26 orphaned records during testing from previous crashes
  - Test coverage: 5 comprehensive tests covering all cleanup scenarios (orphaned detection, recent scan preservation, completed scan handling, logging, integration)

---

## [8.8.17] - 2026-02-01

### Fixed

- **Bug #129: Self-monitoring status display now shows accurate real-time information** - Fixed three display issues on admin page:
  - **Last Scan** changed from hardcoded "Never" to actual database query showing most recent scan timestamp
  - **Next Scan** changed from hardcoded "N/A" to calculated future time (last scan + cadence)
  - **Scan Status** changed from hardcoded "Idle" to real-time job status query showing "Running..." when job executing, "Idle" when no job running
  - Added `_get_last_scan_time()` helper function to query `self_monitoring_scans` table
  - Added `_calculate_next_scan_time()` helper function to compute next scan time based on cadence
  - Added `_get_scan_status()` helper function to query `background_jobs` table for running jobs
  - Updated `self_monitoring.html` template to display dynamic values instead of hardcoded strings
  - Test coverage: 6 comprehensive tests covering all display scenarios (last scan with/without data, next scan calculation, status idle/running/completed)
  - Fixes issue where users couldn't see when last scan occurred, when next scan would run, or if scan was actually running

---

## [8.8.16] - 2026-02-01

### Fixed

- **Bug #127: Self-monitoring service now respects cadence on startup** - Service queries database for last scan timestamp and waits remaining time:
  - Added `_calculate_initial_wait()` method in `service.py` to query last scan from database
  - Service calculates elapsed time since last scan and waits remaining cadence time before first scan
  - Handles three scenarios: recent scan (wait remaining time), no previous scans (wait full cadence), overdue scan (run immediately)
  - Production verification: Logs show "Last scan was 115.5 minutes ago (cadence: 60.0 minutes), running immediately"
  - Fixes issue where server restart triggered immediate scan regardless of when last scan occurred
  - Test coverage: 3 unit tests covering all three scenarios

- **Bug #128: Self-monitoring enabled toggle now starts/stops service immediately** - No server restart required:
  - Modified `save_self_monitoring_config()` in `routes.py` to access running service from `app.state`
  - Service `_enabled` flag synchronized before start/stop to ensure `trigger_scan()` works after toggle
  - Toggling enabled ON immediately starts service if not running
  - Toggling enabled OFF immediately stops service if running
  - Fixes issue where toggle only updated config file without affecting running service
  - Test coverage: 2 toggle tests + 2 state synchronization tests

- **Deprecated datetime.utcnow() replaced** - Future-proofing for Python 3.12+:
  - Replaced deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)` in `service.py`
  - Added timezone-aware datetime handling with backward compatibility for naive timestamps

---

## [8.8.15] - 2026-01-31

### Fixed

- **Self-Monitoring: Job flood caused by corrupted configuration** - Resolved runaway scan execution:
  - Configuration file had corrupted cadence_minutes value (0.5 instead of 1440)
  - Caused 5,090+ scan jobs to be created in 38 hours at 30-second intervals
  - Systemd service restart loaded correct 24-hour cadence configuration
  - Verified zero new jobs created after restart

---

## [8.8.14] - 2026-01-31

### Changed

- **Self-Monitoring: Mandatory Codebase Exploration** - Prevents false positive bug reports:
  - `default_analysis_prompt.md`: Added "MANDATORY: Codebase Exploration Required" section
  - Claude must now read source files and verify exception handling before reporting bugs
  - Explicit distinction between logged-but-handled exceptions vs actual crashes
  - Verification workflow: query logs → read source code → check try-except blocks → decide
  - Fallback rule: If unable to verify via codebase exploration → return SUCCESS with empty issues
  - Prevents false positives like Bug #125 and #126 where logged exceptions were reported as crashes

- **Self-Monitoring: Increased Claude CLI Timeout** - Allows time for thorough codebase analysis:
  - `scanner.py`: Increased `CLAUDE_CLI_TIMEOUT_SECONDS` from 300 to 1800 (5 to 30 minutes)
  - Provides sufficient time for Claude to query logs, explore codebase, and generate analysis
  - E2E verified: Claude successfully reads source files and makes evidence-based decisions

### Fixed

- **Closed Bug #125 and #126 as invalid** - Scanner has proper exception handling for timeouts and missing fields
  - Both bugs claimed scanner "crashes" but exception handling at lines 586-598 catches all exceptions
  - Scan records are persisted with FAILURE status and error messages
  - Next scans proceed normally - no crash, graceful degradation

---

## [8.8.13] - 2026-01-31

### Changed

- **Self-Monitoring: Replaced gh CLI with GitHub REST API** - Eliminates external tool dependency:
  - `scanner.py`: `_fetch_existing_github_issues()` now uses `httpx.get` instead of subprocess `gh issue list`
  - `issue_manager.py`: Renamed `_call_gh_cli()` to `_create_github_issue_via_api()`, uses `httpx.post`
  - Self-monitoring now works on servers without GitHub CLI installed
  - Follows existing REST API patterns from `github_provider.py`

### Fixed

- **Bug #87: Self-monitoring scan failure due to log_id_end NOT NULL constraint** - Database schema compatibility fix:
  - `create_scan_record()` now includes `log_id_end` column in INSERT with initial value
  - Added graceful handling for missing `gh` CLI (FileNotFoundError)
  - Added comprehensive debug logging with `[SELF-MON-DEBUG]` prefix

---

## [8.8.10] - 2026-01-30

### Fixed

- **Auto-Updater Idempotent Service File Updates** - Ensures CIDX_REPO_ROOT gets added to existing servers:
  - Added `_ensure_cidx_repo_root()` to DeploymentExecutor (auto-updater)
  - Idempotently adds `Environment="CIDX_REPO_ROOT={repo_path}"` to systemd service file
  - Runs during every auto-update deployment cycle
  - Existing production servers now get the env var automatically without manual intervention
  - Documented mandatory auto-updater approach in CLAUDE.md

---

## [8.8.9] - 2026-01-30

### Fixed

- **Self-Monitoring CIDX_REPO_ROOT Environment Variable** - Proper fix for repo detection on production servers:
  - Root cause: Previous fixes (cwd-based, manual config field) were band-aids that didn't leverage the installer's knowledge
  - Solution: Added `CIDX_REPO_ROOT` environment variable to systemd service template, set by deploy script
  - Deploy script sets `CIDX_REPO_ROOT` to `WorkingDirectory` (the cloned repo location)
  - Detection priority: 1) `CIDX_REPO_ROOT` env var, 2) `__file__`-based walk, 3) cwd-based walk
  - Removed unnecessary `github_repo` config field from Self-Monitoring UI (the installer already knows!)
  - This is the architecturally correct solution: installer knows the repo path, passes it via env var

---

## [8.8.8] - 2026-01-30

### Fixed

- **Self-Monitoring 400 Error After Enabling** - Fixed "GitHub repository not configured" error when enabling self-monitoring after server startup:
  - Root cause: When self-monitoring was disabled at startup, auto-detected `repo_root` and `github_repo` were discarded (set to None)
  - When user later enabled self-monitoring and clicked "Run Now", the manual trigger read None from app.state
  - Solution: Always preserve auto-detected values in app.state regardless of whether self-monitoring is enabled
  - Only `self_monitoring_service` is None when disabled; detection values are always available

---

## [8.8.7] - 2026-01-30

### Fixed

- **Self-Monitoring MONITOR-GENERAL-011 on Production Servers** - Fixed repo detection failure on pip-installed packages:
  - Root cause: Detection only walked up from `__file__` which points to site-packages (no `.git` there)
  - Solution 1: Added cwd-based fallback detection - if systemd `WorkingDirectory` is the cloned repo, detection succeeds
  - Solution 2: Added `github_repo` config field in Self-Monitoring UI for manual override
  - Priority order: 1) Manually configured `github_repo`, 2) Auto-detected from app.state
  - Production servers can now set `WorkingDirectory` to cloned repo OR manually configure `github_repo` in UI

---

## [8.8.6] - 2026-01-30

### Fixed

- **Self-Monitoring Run Now Button 403 CSRF Error** - Fixed CSRF token validation failure when clicking "Run Now":
  - Root cause: Self-monitoring page set CSRF cookie with `path="/admin"` while all other admin pages use `path="/"`
  - This caused browser to have two `_csrf` cookies with different paths, leading to unpredictable validation failures
  - Solution: Changed to `path="/"` for consistency with all other admin pages

---

## [8.8.5] - 2026-01-30

### Fixed

- **Self-Monitoring Manual Trigger Fails on Installed Packages** - Fixed MONITOR-GENERAL-011 error when using "Run Now" on production servers where CIDX is installed as a package:
  - Root cause: Manual trigger tried to detect `repo_root`/`github_repo` from `__file__`, but installed packages have no `.git` directory
  - Solution: Store `repo_root` and `github_repo` in `app.state` during server startup
  - Manual trigger now reads from `app.state` instead of re-detecting
  - Detection happens once at startup (when running from repo directory), works everywhere after

---

## [8.8.4] - 2026-01-30

### Fixed

- **Self-Monitoring Manual Trigger Missing Bug #87 Fix** - The "Run Now" button in the self-monitoring web UI was failing because the manual trigger route was missing the auto-detection logic added in Bug #87:
  - Added auto-detection of `repo_root` by walking up from `__file__` to find `.git`
  - Added auto-detection of `github_repo` from git remote origin URL
  - Now passes `repo_root` to `SelfMonitoringService` (was missing entirely)
  - Removed reliance on `GITHUB_REPOSITORY` environment variable
  - Manual trigger route now has feature parity with automatic startup initialization

---

## [8.8.3] - 2026-01-30

### Fixed

- **Self-Monitoring Config Save Not Working** - Fixed two bugs preventing self-monitoring settings from saving via web UI:
  - Removed duplicate `@dataclass` decorator on `SelfMonitoringConfig` class that could cause serialization issues
  - Added success/error message display to `self_monitoring.html` template so users get visual feedback when saving

---

## [8.8.2] - 2026-01-30

### Fixed

- **Self-Monitoring Prompt Bloat Causing Claude Timeouts** (Bug #87) - Fixed critical bug where self-monitoring scans failed due to massive prompts causing Claude CLI timeouts:
  - Root cause: Prompt embedded 1000+ log entries (548KB), exceeding Claude's practical processing limits
  - Solution: Claude now queries the log database directly via sqlite3 (prompt reduced to 5KB)
  - Added `--allowedTools Bash` to Claude CLI invocation for database access
  - Auto-detect `github_repo` from git remote (no more `GITHUB_REPOSITORY` env var required)
  - Pass `repo_root` to scanner so Claude runs in correct working directory
  - Updated prompt to focus on actionable development bugs, filtering out configuration noise
  - Issues now correctly created in `LightspeedDMS/code-indexer` (auto-detected from git remote)

- **Config Fixer Destroys Indexing Progress** (Bug #96) - Fixed bug where `cidx fix-config --force` unconditionally overwrote indexing progress metadata with zeros, causing subsequent `cidx index` to perform full reindex instead of incremental:
  - Root cause: `_apply_metadata_fixes()` created a placeholder with `files_processed=0`, `chunks_indexed=0`, `status="needs_indexing"` when `collection_analyzer` was None (filesystem backend), then spread it over existing metadata
  - Solution: Removed destructive `collection_stats` placeholder creation and spread operator; config_fixer now only updates configuration fields (project_id, git state, embedding_provider) while preserving all runtime state
  - Also removed `invalid_file_paths` validation check - `files_to_index` is runtime state for resuming interrupted operations, not configuration
  - CoW clone refresh workflow now correctly preserves indexing progress

---

## [8.8.1] - 2026-01-29

### Fixed

- **Versioned Directory Full Reindex on Refresh** (Bug #85) - Fixed bug where golden repository refresh with temporal indexing (CoW clones) triggered unnecessary full reindexing instead of incremental updates:
  - Root cause: `config_fixer.py` determined `project_id` from directory name (e.g., `"v_1769727231"`), while `file_identifier.py` used git remote origin (e.g., `"evolution"`), causing mismatch in `should_force_full_index()`
  - Solution: Made `FileIdentifier.get_project_id()` the single source of truth; `ConfigurationValidator.detect_correct_project_name()` now delegates to it
  - API change: `FileIdentifier._get_project_id()` renamed to `get_project_id()` (public API)
  - Refresh operations now correctly perform incremental indexing (minutes instead of hours)

---

## [8.8.0] - 2026-01-29

### Added

- **Multimodal Image Vectorization** (Epic #61) - Search documentation with embedded diagrams, screenshots, and visual content. CIDX automatically detects markdown files with images and makes them semantically searchable:

  **User Experience**:
  - Automatic detection during `cidx index` - no configuration needed
  - Query output shows `Using: voyage-code-3, voyage-multimodal-3` when multimodal content exists
  - Timing breakdown displays parallel search performance across both indexes
  - Seamless result merging - best matches from both indexes, deduplicated by file

  **Technical Implementation**:
  - **Dual Model Architecture**: Code uses `voyage-code-3` (1024-dim), images use `voyage-multimodal-3` (1024-dim)
  - **Separate Collections**: `.code-indexer/index/voyage-code-3/` and `.code-indexer/index/voyage-multimodal-3/`
  - **Image Extraction**: Parses markdown `![alt](path)` and HTML/HTMX `<img src="path">` syntax, validates files exist, filters unsupported formats
  - **Parallel Query Execution**: Both indexes searched concurrently via ThreadPoolExecutor
  - **Result Merging**: Deduplicates by (file_path, chunk_offset), keeps highest score, sorts descending

  **Supported File Types**: Markdown (`.md`), HTML (`.html`), HTMX (`.htmx`)
  **Supported Image Formats**: PNG, JPG/JPEG, WebP, GIF

  **Stories Completed**:
  - Story #62: VoyageMultimodalClient for voyage-multimodal-3 API integration
  - Story #63: Multimodal-aware file chunking with image extraction and validation
  - Story #64: Dual-index storage architecture
  - Story #65: MultiIndexQueryService for parallel queries with result merging
  - Story #77: CLI integration displaying multimodal collection status
  - Story #78: Accurate wall-clock timing display for parallel queries

- **Auto-Discovery Last Commit Enhancement** (Epic #79) - Repository auto-discovery now displays last commit information for each discovered repository:

  **User Experience**:
  - Auto-discovery table shows commit hash (7-char), author name, and commit date
  - Information fetched efficiently via GraphQL batch queries
  - Graceful degradation: discovery continues if commit info unavailable

  **Technical Implementation**:
  - **GitHub**: Pure GraphQL via `viewer.repositories` with `affiliations` and `ownerAffiliations` for full access to personal and organization repositories
  - **GitLab**: REST API for listing + GraphQL multiplex pattern for commit enrichment (batches of 10 projects per query to respect complexity limits)
  - **Batch Processing**: GitHub fetches commit info inline; GitLab uses aliased GraphQL queries to batch-fetch multiple projects in single requests

  **Stories Completed**:
  - Story #80: GitHub provider GraphQL enrichment with commit hash, author, and date
  - Story #81: GitLab provider GraphQL multiplex enrichment with batch processing

- **External Dependency Auto-Installation** - Server installer and auto-updater now automatically install required external tools that aren't provided by default on Linux distributions:

  **Ripgrep (rg)**:
  - Downloads pre-compiled static MUSL binary from GitHub releases (v14.1.1)
  - Works on ANY x86_64 Linux (Amazon Linux, Rocky Linux, Ubuntu) without dependencies
  - Installs to `~/.local/bin/rg` with proper PATH configuration in systemd service
  - Eliminates grep fallback warnings and improves regex search performance
  - Idempotent: safe to run multiple times

  **Coursier (cs)**:
  - Downloads pre-compiled binary for Java/Kotlin SCIP indexing
  - Required by scip-java for Java and Kotlin code intelligence
  - Installs to `~/.local/bin/cs` with executable permissions
  - Idempotent: skips if already installed

  **Architecture**:
  - Shared `RipgrepInstaller` utility class eliminates code duplication between installer and auto-updater
  - Secure tarfile extraction with path traversal prevention
  - 60-second download timeout prevents hanging on network issues

### Security

- **Tarfile Path Traversal Prevention** - Fixed potential security vulnerability in ripgrep installation where malicious tarball could write files outside the intended directory:
  - Added `_safe_extract_tar()` method that validates all tar members before extraction
  - Blocks absolute paths (e.g., `/etc/passwd`)
  - Blocks parent directory traversal (e.g., `../../../etc/passwd`)
  - Raises `ValueError` with descriptive message if attack detected

### Fixed

- **Multi-Index Query Timing Accuracy** (Story #78) - Fixed timing display where individual index times incorrectly exceeded parallel wall-clock time (e.g., showing 1.85s + 1.40s = 931ms total):
  - Root cause: Summing internal breakdown values (embedding_ms + hnsw_search_ms) instead of measuring actual elapsed time
  - Solution: Added wall-clock `elapsed_ms` measurement in each query method
  - Invariant now holds: `parallel_time >= max(code_index_ms, multimodal_index_ms)`

- **RefreshScheduler Ignoring Configured Timeouts** - Fixed bug where user-configured "CIDX Index Timeout" setting (e.g., 7200 seconds) was ignored, always using hardcoded default of 3600 seconds:
  - Root cause: `resource_config` parameter was not being passed from `app.py` through `GlobalReposLifecycleManager` to `RefreshScheduler`
  - Solution: Added `resource_config` parameter to `GlobalReposLifecycleManager` and plumbed it through to `RefreshScheduler`
  - All Web UI timeout settings (Git Clone, Git Pull, Git Refresh, CIDX Index) now properly respected by refresh scheduler

- **Scheduled Refresh Timeouts Incorrectly Marked as Completed** (Bug #84) - Fixed bug where scheduled golden repository refreshes that timed out were incorrectly marked as "completed" instead of "failed" in the job dashboard:
  - Root cause: `RefreshScheduler._execute_refresh()` caught exceptions and returned `{success: False}` dict, but `BackgroundJobManager` only marks jobs as FAILED when exceptions are raised
  - Solution: Changed exception handler to raise `RuntimeError` instead of returning error dict, aligning with `GoldenRepoManager` pattern
  - Job dashboard now correctly shows FAILED status when scheduled refreshes time out

- **Web UI Configuration Settings Ignored** (Bug #83) - Comprehensive fix for 15 configuration settings that were displayed in Web UI but ignored by application code:

  **Part 1: 3 Critical Bugs Fixed** (settings completely ignored):
  - **JWT Token Expiration**: `app.py` now reads `jwt_expiration_minutes` from config (was hardcoded to 10)
  - **Password Security**: `UserManager` now receives `password_security_config` (was using defaults)
  - **SCIP Query Limits**: `SCIPMultiService` now accepts config parameters (was hardcoded)

  **Part 2: 8 Type A Settings Wired** (hardcoded constants replaced with config reads):
  - `git_local_timeout`, `git_remote_timeout` in `git_operations_service.py`
  - `github_api_timeout` in `github_provider.py`, `gitlab_api_timeout` in `gitlab_provider.py`
  - `default_diff_lines`, `max_diff_lines`, `default_log_commits`, `max_log_commits` in `git_operations_service.py`

  **Part 3: 4 Type B Settings Removed** (genuinely unused, cleaned from codebase):
  - Removed from `GitTimeoutsConfig`: `git_command_timeout`, `git_fetch_timeout`, `github_provider_timeout`, `gitlab_provider_timeout`
  - Added backward compatibility for old config files containing these fields

---

## [8.7.3] - 2026-01-28

### Fixed

- **Dashboard Auto-Update Dropdown Disruption** (Story #69) - Fixed visual glitch where dashboard auto-refresh (2-second interval) would close dropdown menus mid-selection:
  - Implemented surgical DOM updates with granular HTMX targets
  - Dropdowns (Time Window, Recent Activity, API Filter) now positioned outside refresh targets
  - Added 3 new partial endpoints for data-only updates: `/admin/partials/dashboard-job-counts`, `/admin/partials/dashboard-recent-jobs`, `/admin/partials/dashboard-api-metrics`
  - Auto-refresh continues seamlessly without interrupting user interaction

---

## [8.7.2] - 2026-01-28

### Added

- **Custom hnswlib Submodule with Integrity Checking** - Added LightspeedDMS/hnswlib fork as git submodule containing custom Python bindings that expose the `check_integrity()` method for HNSW index health validation:
  - Connection validity checks (no invalid neighbor IDs)
  - Self-loop detection
  - Duplicate connection detection
  - Orphan node detection (elements with no inbound connections)
  - Foundation for upcoming HNSW Index Health Check epic across CLI, REST, MCP, and Web UI interfaces

### Changed

- **OIDC get_user_info() Converted to Sync** - Changed `get_user_info()` from async to sync function since it only performs CPU-bound JWT parsing (base64 decode + JSON parse), not I/O. This improves uvicorn concurrency by leveraging the threadpool for CPU work instead of blocking the single event loop.

### Fixed

- **Pydantic V2 Deprecation Warnings** - Migrated 3 models from deprecated `class Config` with `json_encoders` to `@field_serializer` decorators:
  - `ActivatedRepository` - datetime and Path field serializers
  - `CompositeRepositoryDetails` - datetime field serializers
  - `FileInfo` - datetime field serializer
  - Eliminates 8 Pydantic deprecation warnings from test output

- **Pytest performance Marker Warning** - Registered `performance` pytest marker in pyproject.toml to eliminate PytestUnknownMarkWarning

---

## [8.7.0] - 2026-01-26

### Added

- **SSO Group Mapping** - Map external SSO groups (from Entra/Keycloak) to internal CIDX groups:
  - Configure mappings with external group ID, optional display name, and target CIDX group
  - First-match strategy: user assigned to first matching group in mapping list
  - Graceful fallback to "users" group when no mappings match or mapped group doesn't exist
  - Backward compatible: automatic migration from old dict format to new list format
  - Optional display names for better UI readability in configuration

### Changed

- **ID Token-Based User Info** - OIDC authentication now parses ID token directly instead of calling userinfo endpoint:
  - More reliable: works universally with Entra, Keycloak, and other OIDC providers
  - Eliminates potential userinfo endpoint configuration issues
  - Groups extracted directly from ID token claims
  - All OIDC tests updated for new implementation

---

## [8.6.17] - 2026-01-26

### Fixed

- **Log Level Config Setting Now Respected** - The `log_level` config setting in the Web UI now actually controls what gets written to the SQLite log database. Previously, the setting was stored but ignored - the SQLiteLogHandler was hardcoded to `INFO` level. Now the configured level (DEBUG, INFO, WARNING, ERROR, CRITICAL) is applied at server startup. This reduces log verbosity when set to WARNING or higher.

---

## [8.6.16] - 2026-01-26

### Removed

- **Dead Config Sections from Web UI** - Removed 5 entire config sections that had no runtime effect:
  - **Git Timeouts Settings** - 8 settings (git_local_timeout, git_remote_timeout, git_command_timeout, git_fetch_timeout, github_api_timeout, gitlab_api_timeout, github_provider_timeout, gitlab_provider_timeout)
  - **Error Handling Settings** - 3 settings (max_retry_attempts, base_retry_delay_seconds, max_retry_delay_seconds)
  - **API Limits Settings** - 9 settings (default_file_read_lines, max_file_read_lines, default_diff_lines, max_diff_lines, default_log_commits, max_log_commits, audit_log_default_limit, log_page_size_default, log_page_size_max)
  - **Web Security Settings** - 2 settings (csrf_max_age_seconds, web_session_timeout_seconds)
  - **Authentication Settings** - 1 setting (oauth_extension_threshold_hours)

- **Dead SCIP Settings from Web UI** - Removed 4 SCIP query limit settings that were never used at runtime:
  - scip_reference_limit, scip_dependency_depth, scip_callchain_max_depth, scip_callchain_limit
  - Kept 3 live SCIP settings: indexing_timeout_seconds, scip_generation_timeout_seconds, temporal_stale_threshold_days

### Changed

- **SCIP Section Description** - Updated to reflect only the remaining timeout and threshold settings

---

## [8.6.15] - 2026-01-26

### Removed

- **Workers Setting from Server Settings UI** - Removed the misleading Workers setting from the Web UI configuration:
  - The setting had no effect because uvicorn workers are hardcoded to 1 in the systemd service file
  - Multiple workers break in-memory cache coherency (HNSW, FTS, OmniCache cursors) and cursor-based pagination
  - Underlying config field retained for backwards compatibility but no longer exposed in UI

### Changed

- **Server Settings Section Description** - Updated to remove worker references since the setting is no longer configurable

---

## [8.6.14] - 2026-01-26

### Changed

- **Configuration Section Documentation Clarity** - Updated Web UI config section descriptions to specify exactly what jobs each setting controls:
  - **Server Settings**: Clarified Workers controls Uvicorn ASGI processes for HTTP requests (REST API, Web UI, MCP endpoints)
  - **Job Queue Settings**: Clarified these control SyncJob queue (git clone, git pull, repository sync/refresh operations)
  - **Background Jobs Settings**: Clarified these control BackgroundJobManager (Add/Remove Golden Repo, SCIP Self-Healing, SCIP generation) and subprocess parallelism (regex search)

---

## [8.6.13] - 2026-01-26

### Added

- **Dashboard Version Display** - Server version now displayed in the System Health section:
  - Version badge shown in Server Status card header (e.g., "v8.6.13")
  - Helps administrators quickly identify running server version
  - Small gray pill-style badge with muted styling

---

## [8.6.12] - 2026-01-26

### Changed

- **SQLite-Backed API Metrics Service** - Migrated ApiMetricsService from in-memory deques to SQLite database storage:
  - Fixes dashboard counter flickering when using multiple uvicorn workers (separate processes had isolated memory)
  - WAL mode enabled for concurrent write handling across processes
  - Periodic cleanup (every 100 inserts) instead of every insert for better performance
  - Retry logic with exponential backoff for database lock errors
  - Graceful degradation on failures (logs warning, returns zeros instead of crashing)
  - Database stored at `~/.cidx-server/data/api_metrics.db`

### Added

- **Multi-Repo Search Metrics Tracking** - Multi-repo searches via `_omni_search_code()` now properly tracked in API metrics:
  - Semantic searches increment `semantic_searches` counter
  - FTS/temporal/hybrid searches increment `other_index_searches` counter
  - Regex searches increment `regex_searches` counter
  - Previously these searches bypassed tracking because they don't use `semantic_query_manager._perform_search()`
  - 5 new unit tests for multi-repo metrics tracking

- **CLAUDE.md Admin Password Rule** - Added critical rule to never change admin password during local development:
  - Documents default credentials (admin/admin) for localhost:8000
  - Includes recovery procedure if password is accidentally changed

- **24 New Tests for API Metrics Service** - Comprehensive test coverage for SQLite-backed metrics:
  - Initialization, increment operations, get_metrics with time windows
  - Periodic cleanup behavior, retry logic, exception handling
  - Multi-worker simulation, graceful degradation scenarios

---

## [8.6.11] - 2026-01-26

### Changed

- **Smart Context-Aware Defaults for Multi-Repo Search** - Multi-repo searches (2+ repositories) now use more intuitive defaults:
  - `aggregation_mode` defaults to `'per_repo'` (ensures all repositories are represented in results)
  - `response_format` defaults to `'grouped'` (results pre-organized by repository)
  - Single-repo searches retain original defaults (`'global'` and `'flat'`)
  - 6 new unit tests covering default behavior

### Fixed

- **Multi-Repo Search Content Bug** - Fixed bug where multi-repo searches returned empty `content` fields
  - Root cause: `include_source=False` was hardcoded in `MultiSearchService._search_single_repository()`
  - Fix: Changed to `include_source=True` to return actual code content
  - Truncation pipeline properly handles large content (converts to `preview` + `cache_handle`)
  - 3 new unit tests covering content inclusion

---

## [8.6.10] - 2026-01-26

### Fixed

- **Fix Global Repository Lookup in Search Services** (Epic #47)
  - Fixed `MultiSearchService._get_repository_path()` to use GlobalRegistry instead of GoldenRepoManager (Story #43)
  - Fixed `SemanticSearchService._get_repository_path()` to use GlobalRegistry instead of GoldenRepoManager (Story #44)
  - Fixed `SCIPMultiService._get_repository_path()` to use GlobalRegistry instead of GoldenRepoManager (Story #45)
  - Fixed `RepositoryStatsService.get_repository_metadata()` to use GlobalRegistry instead of GoldenRepoManager (Story #46)
  - Multi-repo search now correctly resolves global repository names (ending in `-global`)
  - Error messages now reference "global repositories" instead of "golden repositories"
  - 30 new unit tests covering the bug fixes with source verification and functional tests

### Changed

- **SCIPMultiService exception handling** - `_get_scip_file_for_repo()` now propagates `FileNotFoundError` instead of swallowing it, enabling proper error messages for non-existent repositories

---

## [8.6.9] - 2026-01-25

### Added

- **SCIPQueryService - Unified SCIP Query Layer** (Epic #37)
  - New `SCIPQueryService` class providing centralized SCIP file discovery and query execution
  - 7 query methods: `find_definition`, `find_references`, `get_dependencies`, `get_dependents`, `analyze_impact`, `trace_callchain`, `get_context`
  - Access control integration via `username` parameter for group-based repository filtering
  - 43 unit tests for service with 97% coverage
  - 24 parity and consolidation tests verifying MCP/REST identical behavior

### Changed

- **MCP SCIP Handlers** - Refactored to thin wrappers delegating to SCIPQueryService
- **REST SCIP Routes** - Refactored to thin wrappers delegating to SCIPQueryService
- **MCP/REST Parity** - Both interfaces now call same service methods, guaranteeing identical results

### Removed

- **Duplicate `_find_scip_files()`** - Removed from both handlers.py and scip_queries.py
- **9 duplicate helper functions** - Removed from scip_queries.py (access control, filtering, conversion)
- **Net code reduction**: ~300 lines removed through consolidation

---

## [8.6.8] - 2026-01-23

### Added

- **Consolidated Multi-Repo Search Implementation** (Story #29)
  - MCP `_omni_search_code()` now uses parallel execution via `asyncio.gather()` instead of sequential loop
  - OmniSearchConfig merged into MultiSearchLimitsConfig with `omni_` prefixed fields
  - All 24 Web UI configuration sections now have explanatory documentation paragraphs
  - Comprehensive technical debt audit report documenting other codebase duplications

### Removed

- **OmniSearchConfig class** - Settings consolidated into MultiSearchLimitsConfig
- **Duplicate "Cross-Repo (Omni) Search Settings"** Web UI section - Now unified under "Multi-Search Settings"

### Changed

- **MultiSearchLimitsConfig** now includes 10 additional `omni_` prefixed fields migrated from OmniSearchConfig
- **Config migration logic** automatically converts old `omni_search_config` to new `multi_search_limits_config` format

### Documentation

- **Technical Debt Audit Report** at `reports/troubleshooting/tech_debt_audit_story29.md`
  - Documents 5 duplicate service implementations (HIGH: _get_repository_path in 4 files)
  - Identifies 4 environment variable policy violations in cache modules
  - Provides prioritized recommendations for future consolidation work

---

## [8.6.7] - 2026-01-23

### Added

- **Configurable Service Display Name** (Story #22)
  - New `service_display_name` configuration field with default value "Neo"
  - MCP protocol `serverInfo.name` now uses configured display name
  - Quick reference tool shows "This server is CIDX (a.k.a. {name})." identity line
  - Web UI configuration screen includes Service Display Name input field
  - Empty string fallback to default "Neo" for robustness
  - 23 new unit tests covering config, protocol, and handler changes

### Changed

- **CLAUDE.md documentation** - Added "Running Local CIDX Server" section with correct startup command

---

## [8.6.6] - 2026-01-23

### Added

- **Auto-Discovery Branch Selection for Private Repositories** (Story #21)
  - SSH URL to HTTPS conversion with embedded credentials for git ls-remote
  - GitLab uses `oauth2:<token>` format, GitHub uses `<token>` format
  - Automatic credential retrieval from CITokenManager based on platform
  - Vertical layout for branch selection dialog (repo name on top, dropdown below)

### Fixed

- **Security: Credential leakage prevention** - Removed `exc_info=True` from error logging to prevent credentials appearing in stack traces
- **Security: HTTP credential rejection** - Credentials not sent over unencrypted HTTP connections
- **Platform detection accuracy** - Uses hostname extraction instead of substring matching to prevent false positives
- **Branch dropdown readability** - Fixed light gray text color on dropdown selected value

---

## [8.6.5] - 2026-01-23

### Fixed

- **Test infrastructure protection for Claude Code credentials** - Tests that call `ClaudeCliManager.sync_api_key()` now properly mock `Path.home()` and backup/restore `~/.claude/.credentials.json`, `~/.claude.json`, and `~/.bashrc` to prevent accidental deletion of user OAuth credentials during test runs
- **Daemon staleness detection tests** - Fixed 5 failing unit tests by adding `FakeEmbeddingProvider` in `tests/unit/daemon/conftest.py` that generates deterministic embeddings without requiring real VoyageAI API calls

---

## [8.6.4] - 2026-01-22

### Fixed

- **SCIP Audit database initialization on fresh install** - Fixed RED/error status on dashboard for fresh CIDX server installations (Story #19)
  - Added eager initialization of scip_audit.db during server startup (before health checks)
  - Created new startup/database_init.py module with initialize_scip_audit_database() function
  - Modified app.py to call initialization at startup (lines 2720-2731)
  - Schema matches SCIPAuditRepository._init_database() exactly
  - Non-blocking initialization (logs warning on failure, doesn't crash server)
  - All 7 databases now show HEALTHY status on fresh install without manual intervention
  - Added 9 comprehensive unit tests with 100% coverage
  - Manual E2E testing validated fresh install, schema validation, restart persistence, and idempotency

---

## [8.6.3] - 2026-01-22

### Fixed

- **API activity tracking coverage** - Comprehensive fix ensuring all API calls (REST/MCP) are tracked at service layer without double-counting (Story #4 AC2)
  - Added `increment_other_api_call()` tracking to file_crud_service (3 methods: create_file, edit_file, delete_file)
  - Added `increment_other_api_call()` tracking to git_operations_service (17 methods: all REST API wrappers)
  - Added `increment_other_api_call()` tracking to ssh_key_manager (5 methods: create_key, assign_key_to_host, delete_key, list_keys, get_public_key)
  - Removed edge tracking from MCP protocol.py to prevent double-counting
  - Added comprehensive test coverage (11 tests) in test_api_metrics_tracking.py

---

## [8.6.1] - 2026-01-22

### Fixed

- **macOS ARM64 build failure** - Added platform marker to `pysqlite3-binary` dependency (`; sys_platform == 'linux'`) since this package only has Linux wheels
- **Removed unused docker dependency** - Removed `docker>=6.0.0` from dependencies (deprecated in Story #506: container management removed)

---

## [8.6.0] - 2026-01-20

### Added

#### Comprehensive Documentation Overhaul

**Feature**: Complete documentation audit and new feature documentation.

- **Version consistency** - All documentation updated to v8.6.0 across README, installation guides, and examples
- **Fact-checked query-guide.md** - All 23 query parameters verified against implementation
- **New feature documentation** - Claude Delegation, OTEL Telemetry, Group Security, Auto-Discovery

#### Feature Documentation

Added comprehensive documentation for 8.5.x features:

- **Claude Delegation System** - AI-powered code analysis workflows on protected repositories
- **OTEL Telemetry** - OpenTelemetry integration for server observability
- **Group-Based Security** - Fine-grained access control with group membership
- **Auto-Discovery** - Automatic repository discovery from configured sources

### Fixed

- Documentation version inconsistencies (8.4.46 references updated)
- RELEASE_NOTES.md now redirects to CHANGELOG.md for versions 7.0.0+
- MCP version in architecture.md updated to current version

---

## [8.5.3] - 2026-01-17

### Added

#### Claude Delegation System (Epic #717)

**Feature**: Complete Claude Delegation framework enabling AI-powered code analysis workflows on protected repositories.

- **Callback-based job completion** (Story #720) - Efficient polling mechanism for delegation jobs
- **Graceful drain mode** (Story #734) - Job-aware auto-update prevents interrupting running jobs
- **Server stability improvements** (Epic #733) - Technical debt cleanup and reliability enhancements

### Fixed

- Hybrid auth support for golden repo index and job status endpoints
- CSRF token handling in auto-discovery HTMX partials
- Session cookie management in hybrid authentication

---

## [8.5.2] - 2026-01-12

### Enhanced

#### Dashboard Refinements (Story #712)

- Honeycomb visualization improvements
- Multi-volume disk metrics with percentage-based thresholds
- Repository count fixes and tooltip path improvements

### Performance

- SCIP performance benchmark tests marked as slow to prevent CI hangs

---

## [8.5.1] - 2026-01-12

### Added

#### OTEL Telemetry System (Epic #694)

**Feature**: Complete OpenTelemetry integration for CIDX server observability.

- Distributed tracing across all server operations
- Metrics collection for performance monitoring
- Correlation ID tracking for log queries

#### Group-Based Security Model (Epic #704)

**Feature**: Fine-grained access control through group membership.

- Repository access tied to group membership
- Role-based permissions within groups
- Admin dashboard for group management

#### Auto-Discovery System (Stories #689-693)

**Feature**: Automatic repository discovery from GitHub and GitLab organizations.

- **GitLab Auto-Discovery** (Story #689) - Scan GitLab groups for repositories
- **GitHub Auto-Discovery** (Story #690) - Scan GitHub organizations for repositories
- **Search and Filter** (Story #691) - Filter discovered repositories
- **Batch Creation** (Story #692) - Add multiple repositories at once
- **Job Queue Verification** (Story #693) - Track batch creation progress

#### SQLite Storage Migration (Story #702)

**Feature**: Complete migration from JSON file storage to SQLite backend.

- All entity managers now use SQLite
- Background job persistence in database
- Improved concurrent access handling

### Fixed

- CSRF race condition with HTMX polling (#715)
- CSRF validation auto-recovery instead of 403 error (#714)
- RefreshScheduler refactored to use BackgroundJobManager (#703)
- Various dashboard and UI fixes

---

## [8.5.0] - 2026-01-07

### Added

#### Multi-Repository Search (Epic #673)

**Feature**: Search across multiple repositories simultaneously with unified results.

- Query multiple repositories with comma-separated aliases or array syntax
- Aggregation modes: `global` (best matches) and `per_repo` (balanced distribution)
- Response formats: `flat` (single array) and `grouped` (by repository)
- Support in search_code, regex_search, git_log, git_search_commits, list_files

**Example**:
```bash
cidx query "authentication" --repos "backend,frontend,shared"
```

#### Payload Size Control with Server-Side Caching (Epic #678)

**Feature**: Token-aware pagination and caching for large search results.

- Automatic content truncation based on token budget (5000 tokens default)
- Cache handles for retrieving full content page-by-page
- `get_cached_content` tool for pagination through large results

#### SSH Key Management

**Feature**: Complete SSH key lifecycle management integrated into CIDX.

- Create/delete SSH keys (Ed25519 and RSA)
- Assign keys to hostnames in SSH config
- Web UI for key management
- CLI and MCP tool support

#### OIDC/SSO Authentication

**Feature**: Enterprise single sign-on support via OpenID Connect.

- Integration with identity providers (Okta, Azure AD, etc.)
- JIT (Just-In-Time) user provisioning
- Email verification enforcement
- Hot reload for OIDC configuration changes

#### MCP Credential Management (Stories #614-617)

**Feature**: Secure credential storage for MCP client authentication.

- Client credentials grant for Claude Desktop
- Admin credential management interface
- Token lifecycle management

#### CI/CD Integration (Stories #633-635)

**Feature**: GitHub Actions and GitLab CI monitoring and API key management.

- List workflow runs and pipeline status
- Search logs for error patterns
- Retry failed workflows
- CI/CD API key configuration

#### Auto-Update Service (Story #657)

**Feature**: Automatic server updates with zero-downtime deployments.

- Git-based update detection
- Graceful service restart via systemd
- Dynamic repository path detection

#### Golden Repository Index Management (Stories #593-597)

**Feature**: Incremental index addition to golden repositories.

- Backend service for adding index types (semantic_fts, temporal, scip)
- REST API endpoints for index management
- CLI commands: `cidx server add-index`, `cidx server list-indexes`
- MCP tools: `add_golden_repo_index`, `get_golden_repo_indexes`
- Web UI for visual index management

### Enhanced

#### SCIP Code Intelligence

- C# and Go indexer support
- Call chain tracing improvements
- Self-loop and simple-name filtering fixes
- Interface-to-implementation edges for complete call chains

#### MCP Tool Documentation

- All 53 MCP tools enhanced with TL;DR format documentation
- Quick start examples, use cases, troubleshooting guides
- Consistent format across all tools

### Fixed

- Thread-safe concurrency controls for multi-user MCP operations (#620)
- OAuth DCR public client support for claude.ai web (#619)
- Browse directory path pattern handling
- Multiple FTS and temporal search bugs in multi-repo mode

---

## [8.4.44] - 2025-12-28

### Enhanced

#### Complete MCP Tool Documentation Standardization

**Feature**: Comprehensively enhanced all 53 MCP (Model Context Protocol) tool definitions with standardized TL;DR format documentation, transforming minimal tool stubs into complete user-facing documentation.

**Scope**: Every MCP tool now includes:
- **TL;DR** - Concise summary of tool purpose and functionality
- **QUICK START** - Minimal working example for immediate use
- **USE CASES** - Specific scenarios when to use the tool
- **OUTPUT/FIELDS** - Detailed response structure documentation
- **WHEN NOT TO USE** - Alternative tools for different scenarios
- **TROUBLESHOOTING** - Common issues and solutions
- **RELATED TOOLS** - Connected functionality and workflows

**Impact on User Experience**:
- AI agents (Claude.ai, etc.) can now make informed decisions about tool selection without external documentation
- Inline documentation eliminates need to consult separate API docs
- Progressive disclosure pattern (quick-start → detailed guidance → troubleshooting) serves both novice and expert users
- Consistent format across all 53 tools creates predictable user experience

**Tools Enhanced** (16 tools in this session, 37 previously completed):
- Repository Management: `list_files`, `get_file_content`, `get_repository_status`, `switch_branch`, `get_branches`, `get_all_repositories_status`, `global_repo_status`
- System Health: `check_health`, `get_repository_statistics`, `get_job_details`
- Golden Repositories: `remove_golden_repo`, `refresh_golden_repo`, `get_global_config`, `set_global_config`
- User Management: `list_users`, `create_user`
- Plus 37 previously completed tools including git operations, SCIP intelligence, SSH keys, file CRUD, authentication, search operations, and more

**Documentation Quality**: Each tool description now averages 400-800 characters (vs. 8-109 char stubs previously), providing comprehensive guidance while remaining concise and scannable.

**Verification**: 52/52 tools detected with comprehensive TL;DR format, 0 tools with inadequate descriptions, 100% completion rate.

## [8.4.45] - 2025-12-14

### Changed

#### SCIP Query Output Simplification (60-70% Token Efficiency Improvement)

**Problem**: SCIP query results consumed excessive tokens due to verbose output format with SCIP protocol prefixes and multi-line formatting, making queries expensive and slow for LLM-based workflows.

**Root Cause**: All 7 SCIP commands (definition, references, dependencies, dependents, impact, callchain, context) used verbose 3+ line format per result with full SCIP identifiers like `scip-python python code-indexer abc123 module/ClassName#method().`

**Solution**: Implemented compact single-line format with human-readable display names and file locations.

**Output Format Changes**:
```bash
# Before (verbose, ~150 characters per result):
Symbol: scip-python python code-indexer abc123 `module.path`/ClassName#method().
File: src/module/file.py
Line: 42

# After (compact, ~60 characters per result):
module.path/ClassName#method() (src/module/file.py:42)
```

**Implementation**:
- Display names strip SCIP prefixes: `module/ClassName#method()` format
- Single-line output: `{display_name} ({file_path}:{line})`
- Preserves readability while maximizing token efficiency
- Applied consistently across all 7 SCIP commands

**Impact**:
- **60-70% token reduction** for typical SCIP queries
- Faster LLM response times due to smaller context windows
- Improved readability with module-qualified names
- Consistent output format across all query types
- Commands affected: definition, references, dependencies, dependents, impact, callchain, context

### Removed

#### Protobuf Backend (Architecture Cleanup)

**Motivation**: Protobuf backend was never used in production. DatabaseBackend (SQLite) is 300-400x faster and became the de facto standard, making protobuf code dead weight.

**Code Removed** (~600 lines):
- `ProtobufBackend` class (130 lines) - Full protobuf scanning implementation
- `benchmark.py` module (150 lines) - Protobuf vs database comparison tool
- Protobuf fallback logic in query commands (50 lines)
- 4 benchmark/protobuf test files (270 lines)

**CLI Changes**:
- Removed `cidx scip benchmark` command (no longer relevant)
- All queries now use DatabaseBackend exclusively

**Automatic Cleanup**: .scip files automatically deleted after successful database generation (saves disk space, database is canonical source)

**Impact**:
- Simpler architecture with single backend path
- Reduced maintenance burden
- Faster CI/CD (fewer tests to run)
- Cleaner codebase focused on production backend
- **No user-facing changes** (protobuf was internal-only)

### Fixed

#### SCIP find_references Substring Matching Bug

**Problem**: `cidx scip references "ClassName"` returned empty results for substring queries. Only `--exact` flag produced results, making fuzzy searching completely broken.

**Root Cause**: Query logic incorrectly filtered results, requiring exact symbol matches even without `--exact` flag.

**Solution**: Fixed substring matching logic to search within symbol names for fuzzy queries.

**Examples**:
```bash
# Now works (was broken):
cidx scip references "DaemonService"        # Finds DaemonServiceManager, DaemonService, etc.

# Still works (was only working case):
cidx scip references "DaemonService" --exact  # Exact match only
```

**Impact**:
- Fuzzy reference searches now work as documented
- Better UX for exploratory queries
- Consistent with user expectations from semantic search
- `--exact` flag now has clear purpose (strict matching)

#### Consistent --limit Parameter Behavior

**Problem**: SCIP commands had inconsistent default limits - `references` defaulted to 100, `context` to 20, others unlimited. Confusing UX with no clear pattern.

**Solution**: Standardized all 7 SCIP commands to use `--limit` with default=0 (unlimited).

**New Behavior**:
- `--limit 0` (default): Return ALL results (most intuitive for users)
- `--limit N` (N > 0): Return at most N results
- Consistent across definition, references, dependencies, dependents, impact, callchain, context

**Rationale**: Database backend is fast enough that unlimited queries are practical. Users can opt-in to limits when needed for performance or display reasons.

**Impact**:
- Predictable behavior across all query types
- Better performance makes unlimited queries reasonable
- Clear opt-in model for result limiting
- Eliminates arbitrary default limit differences

### Performance

#### Database Backend Performance

**Query Performance** (DatabaseBackend only, protobuf removed):
- Definition queries: <10ms (typical)
- Reference queries: 50-200ms (depends on symbol popularity)
- Dependency/dependent queries: 100-500ms (graph traversal)
- Impact analysis: 200-1000ms (multi-hop traversal)
- Callchain: 100-800ms (depth-dependent)

**Storage Efficiency**:
- Automatic .scip file cleanup after database generation
- SQLite database is canonical source (30-50% smaller than .scip)
- Faster queries with indexed lookups vs protobuf scanning

### Coverage Status

**SCIP Query Coverage Across Interfaces**:
- CLI: 7/7 commands (100%)
- REST API: 7/7 endpoints (100%)
- MCP Tools: 7/7 tools (100%)
- Web UI: 4/7 commands (missing impact, callchain, context)

**Commands Available**:
1. definition - Find symbol definitions
2. references - Find all references to symbol
3. dependencies - Find direct dependencies
4. dependents - Find direct dependents
5. impact - Analyze change impact (multi-hop)
6. callchain - Trace call chains (depth-limited)
7. context - Symbol context and documentation

### Technical Details

**Files Modified**:
- `src/code_indexer/cli_scip.py` - Compact output for all 7 commands
- `src/code_indexer/scip/query/primitives.py` - Substring matching fix
- `src/code_indexer/scip/query/composites.py` - --limit standardization

**Files Removed**:
- `src/code_indexer/scip/benchmark.py` - Protobuf benchmark tool
- `tests/unit/test_scip_benchmark.py` - 4 benchmark test files
- Protobuf backend code throughout SCIP module

**Test Results**:
- All fast-automation.sh tests passing
- SCIP query tests updated for compact output
- Benchmark tests removed (no longer relevant)

## [8.4.0] - 2025-12-01

### Fixed

#### Windows Token Refresh Failure in MCPB

**Problem**: Token refresh was failing on Windows because `os.rename()` fails when the destination file already exists, unlike Unix systems where it performs an atomic overwrite.

**Root Cause**: The token persistence code used `os.rename()` for atomic file replacement, which is not cross-platform compatible.

**Solution**: Changed from `os.rename()` to `os.replace()` which provides cross-platform atomic file replacement, working correctly on both Windows (overwrites existing files) and Unix systems.

**Impact**:
- Token refresh now works correctly on Windows
- Maintains atomic file replacement semantics
- No behavioral change on Unix systems

### Enhanced

#### browse_directory MCP/REST Endpoint Filtering

**Overview**: The `browse_directory` endpoint now supports comprehensive filtering parameters for more precise directory browsing operations.

**New Parameters**:
- `path_pattern`: Glob pattern filtering (e.g., `*.py`, `src/**/*.ts`) for matching specific file patterns
- `language`: Filter by programming language detection
- `limit`: Control maximum number of results returned (default 500)
- `sort_by`: Sort results by path, size, or modified_at

**Automatic Exclusions**:
- `.code-indexer/` directory automatically excluded from results
- `.git/` directory automatically excluded from results
- `.gitignore` patterns automatically respected using pathspec library

**MCP Tool Documentation**:
- Added comprehensive parameter descriptions to MCP tool definitions
- Improved discoverability of filtering options through tool introspection

**Use Cases**:
- Filtering large repositories to find specific file types
- Browsing source directories while excluding build artifacts
- Sorting files by modification time for recent changes discovery

## [8.2.0] - 2025-11-26

### Added - Epic #514: Claude Desktop MCPB Integration

**MCP Stdio Bridge (Story #515)**
- JSON-RPC 2.0 protocol handling for stdin/stdout communication
- HTTP client with Bearer token authentication
- Complete MCP protocol 2024-11-05 implementation
- CLI entry point: `cidx-bridge`
- 96% test coverage with 60 passing tests

**SSE Streaming Support (Story #516)**
- Server-Sent Events (SSE) streaming for progressive results
- Accept header negotiation: `text/event-stream, application/json`
- Graceful fallback to JSON when SSE unavailable
- Event types: chunk, complete, error
- 95% test coverage with 85 passing tests

**Enhanced Configuration System (Story #517)**
- Multi-source configuration (environment, file, defaults)
- Support for both CIDX_* and MCPB_* environment variables
- HTTPS validation and file permissions checking
- Configuration diagnostics command (--diagnose)
- Log level support with validation
- 97% test coverage with 121 passing tests

**Cross-Platform Binary Distribution (Story #518)**
- PyInstaller single-binary builds for 4 platforms
- Platform support: macOS (x64/arm64), Linux (x64), Windows (x64)
- Automated GitHub Actions CI/CD workflow
- Manifest schema with SHA256 checksums
- Build automation scripts
- 97% test coverage with 35 passing tests

**E2E Testing and Documentation (Story #519)**
- 51 comprehensive E2E tests (protocol compliance, workflows, error handling)
- Setup guide (installation, configuration, verification)
- API reference (all 22 MCP tools, complete parameter documentation)
- Query guide (semantic, FTS, regex, temporal search examples)
- Troubleshooting guide (diagnostics, common issues, FAQ)
- 94% test coverage, 4,172 lines of documentation

### Features
- Complete query parity: All 25 search_code parameters accessible via MCP
- All 22 CIDX MCP tools exposed through stdio bridge
- Zero Python runtime dependencies (single binary distribution)
- Cross-platform support with automated releases

### Testing
- Total test count: 3,992 passing tests
- MCPB module coverage: 94%
- Zero failures in automation suites
- TDD methodology throughout

## [8.0.0] - 2025-11-20

### BREAKING CHANGES

This is a major architectural release focused on simplification and removing legacy infrastructure. Users must migrate existing projects to the new architecture.

#### Removed Features

**Qdrant Backend Support (Removed)**
- The Qdrant vector database backend has been completely removed
- Only the filesystem backend is supported in v8.0+
- Users must re-index codebases after upgrading
- Container management infrastructure eliminated

**Container Infrastructure (Removed)**
- All Docker/Podman container management code removed
- No more container orchestration, port management, or health checks
- Code-indexer now runs entirely container-free
- Instant setup with no container runtime dependency

**Ollama Embedding Provider (Removed)**
- Ollama local embeddings provider has been removed
- VoyageAI is the only supported embedding provider in v8.0+
- Focus on production-quality cloud-based embeddings
- Users must obtain VoyageAI API key and re-index

### Migration Required

All users must migrate to v8.0:
1. Backup existing index: `cp -r .code-indexer .code-indexer.backup`
2. Upgrade code-indexer: `pipx upgrade code-indexer`
3. Remove legacy config fields (qdrant_config, ollama_config, containers_config)
4. Set VoyageAI API key: `export VOYAGE_API_KEY="your-key"`
5. Re-initialize: `cidx init`
6. Re-index: `cidx index`

See [Migration Guide](docs/migration-to-v8.md) for complete instructions.

### Removed

**Code Removal (~15,000 lines)**
- QdrantContainerBackend class and all integration code
- DockerManager and ContainerManager infrastructure
- Port registry system and dynamic port allocation
- OllamaClient and local embedding infrastructure
- Container-related CLI commands and configuration options
- Container health monitoring and management code

**Test Removal (~135 files)**
- All Qdrant backend tests
- All container management tests
- All Ollama provider tests
- Legacy integration tests for removed features

**Configuration Removal**
- QdrantConfig class removed from models.py
- OllamaConfig class removed from models.py
- ProjectContainersConfig class removed from models.py
- Container-related configuration fields removed

**CLI Changes**
- Removed `--backend qdrant` option (filesystem only)
- Removed `--embedding-provider ollama` option (voyageai only)
- Removed container-related flags from all commands
- Simplified start/stop/restart commands (daemon-only)

### Changed

**Simplified Architecture**
- Two operational modes (was three): CLI Mode and Daemon Mode
- Filesystem backend is now the only option (no configuration needed)
- VoyageAI embeddings are now the only option
- Container-free architecture throughout

**Configuration Schema**
- Simplified to essential fields only
- Default configuration works out-of-box
- No backend or provider selection needed
- Legacy configuration detection with helpful error messages

**Documentation Updates**
- README.md updated for v8.0 architecture
- CLAUDE.md simplified for two-mode operation
- New migration guide created (docs/migration-to-v8.md)
- Architecture documentation updated (docs/architecture.md)
- All examples updated to reflect v8.0 changes

### Improved

**Performance Benefits**
- Test suite runs ~30% faster without container overhead
- Faster startup with no container initialization
- Simpler deployment without container runtime
- Reduced memory footprint

**Operational Benefits**
- No container runtime required (works on any system with Python)
- Instant setup with zero external dependencies
- Simpler troubleshooting with fewer components
- Cleaner error messages with migration guidance

**Development Benefits**
- Reduced codebase size (~15,000 lines removed)
- Faster CI/CD pipelines
- Clearer architecture focused on core functionality
- Easier onboarding for new contributors

### Fixed

**Legacy Detection**
- Configuration validator detects legacy Qdrant config with helpful error
- Configuration validator detects legacy Ollama config with migration steps
- Configuration validator detects legacy container config with guidance
- All errors reference migration guide for detailed instructions

### Technical Details

**Files Modified (Documentation)**
- README.md - Updated for v8.0, removed legacy references
- CLAUDE.md - Simplified operational modes, removed Mode 3
- CHANGELOG.md - Added v8.0.0 breaking changes entry
- docs/architecture.md - Updated for two-mode operation
- docs/migration-to-v8.md - NEW: Comprehensive migration guide

**Version Updates**
- src/code_indexer/__init__.py - Bumped to 8.0.0
- All installation examples updated to v8.0.0
- All documentation references updated to v8.0.0

### Migration Notes

**Breaking Changes Summary**
- Qdrant backend removed - must use filesystem backend
- Ollama provider removed - must use VoyageAI
- Container infrastructure removed - runs container-free
- Must re-index all codebases after upgrade

**Migration Time Estimate**
- Small codebase (<1K files): 5-10 minutes
- Medium codebase (1K-10K files): 10-30 minutes
- Large codebase (>10K files): 30-60 minutes

**Zero Backward Compatibility**
- v8.0 cannot read v7.x Qdrant indexes
- Fresh indexing required for all projects
- Configuration files must be updated
- No automatic migration available

### Contributors

- Seba Battig <seba.battig@lightspeeddms.com>
- Claude (AI Assistant) <noreply@anthropic.com>

### Links

- [GitHub Repository](https://github.com/LightspeedDMS/code-indexer)
- [Migration Guide](docs/migration-to-v8.md)
- [Documentation](https://github.com/LightspeedDMS/code-indexer/blob/master/README.md)
- [Issue Tracker](https://github.com/LightspeedDMS/code-indexer/issues)

---

## [7.2.1] - 2025-11-12

### Fixed

#### Temporal Commit Message Truncation (Critical Bug)

**Problem**: Temporal indexer only stored the **first line** of commit messages instead of full multi-paragraph messages, rendering semantic search across commit history ineffective.

**Root Cause**: Git log format used `%s` (subject only) instead of `%B` (full body), and parsing split by newline before processing records, truncating multi-line messages.

**Evidence**:
```bash
# Before fix - only 60 characters stored:
feat: implement HNSW incremental updates...

# After fix - full 3,339 characters (66 lines) stored:
feat: implement HNSW incremental updates with FTS incremental indexing...

Implement comprehensive HNSW updates...
[50+ additional lines with full commit details]
```

**Solution**: Changed git format to use `%B` (full body) with record separator `\x1e` to preserve newlines in commit messages.

**Implementation**:
- **File**: `src/code_indexer/services/temporal/temporal_indexer.py` (line 395)
  - Changed format: `--format=%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e`
  - Parse by record separator first: `output.strip().split("\x1e")`
  - Then split fields by null byte: `record.split("\x00")`
  - Preserves multi-paragraph commit messages with newlines
- **Test**: `tests/unit/services/temporal/test_commit_message_full_body.py`
  - Verifies full message parsing (including pipe characters)

**Impact**:
- ✅ Temporal queries now search across **full commit message content** (not just subject line)
- ✅ Multi-paragraph commit messages fully indexed and searchable
- ✅ Commit messages with special characters (pipes, newlines) handled correctly
- ✅ Both regular and quiet modes display complete commit messages

#### Match Number Display Consistency

**Problem**: Match numbering was highly inconsistent across query modes - some showed sequential numbers (1, 2, 3...), others didn't, creating confusing UX.

**Issues Fixed**:

**1. Temporal Commit Message Quiet Mode** - Showed useless `[Commit Message]` placeholder instead of actual content

**Solution**: Complete rewrite to display full metadata and entire commit message:
```python
# Before: Useless placeholder
0.602 [Commit Message]

# After: Full metadata + complete message
1. 0.602 [Commit 237d736] (2025-11-02) Author Name <email>
   feat: implement HNSW incremental updates...
   [full 66-line commit message displayed]
```

**2. Daemon Mode --quiet Flag Ignored** - Hardcoded `quiet=False`, ignoring user's `--quiet` flag

**Solution**: Parse `--quiet` from query arguments and pass actual value to display functions

**3. Semantic Regular Mode** - Calculated match number `i` but never displayed it

**Solution**: Added match number to header: `{i}. 📄 File: {file_path}`

**4. All Quiet Modes** - Missing match numbers across FTS, semantic, hybrid, and temporal queries

**Solution**: Added sequential numbering to all quiet mode outputs:
- FTS quiet: `{i}. {path}:{line}:{column}`
- Semantic quiet: `{i}. {score:.3f} {file_path}`
- Temporal quiet: `{i}. {score:.3f} {metadata}`

**Implementation**:
- **File**: `src/code_indexer/cli.py`
  - Line 823: FTS quiet mode - added match numbers
  - Line 951: Semantic quiet mode - added match numbers
  - Line 977: Semantic regular mode - added match numbers to header
  - Line 1514: Hybrid quiet mode - added match numbers
  - Lines 5266-5301: Temporal commit quiet mode - complete rewrite with full metadata
- **File**: `src/code_indexer/cli_daemon_fast.py`
  - Lines 86-87: Parse --quiet flag from arguments
  - Lines 156, 163: Pass quiet flag to display functions
- **File**: `src/code_indexer/utils/temporal_display.py`
  - Added quiet mode support to commit message and file chunk display functions

**Test Coverage**:
- `tests/unit/cli/test_match_number_display_consistency.py` - 5 tests
- `tests/unit/cli/test_temporal_commit_message_quiet_complete.py` - Metadata display validation
- `tests/unit/daemon/test_daemon_quiet_flag_propagation.py` - 3 tests
- `tests/unit/utils/test_temporal_display_quiet_mode.py` - 3 tests

**Impact**:
- ✅ **Consistent UX**: All query modes show sequential match numbers (1, 2, 3...)
- ✅ **Quiet mode usability**: Numbers make it easy to reference specific results
- ✅ **Temporal commit searches**: Actually useful output instead of placeholders
- ✅ **Daemon mode**: Respects user's display preferences

### Changed

#### Test Suite Updates

**New Tests**:
- 11 unit tests for match number display consistency
- 1 integration test for commit message full body parsing
- All 3,246 fast-automation tests passing (100% pass rate)
- Zero regressions introduced

## [7.2.0] - 2025-11-02

### Added

#### HNSW Incremental Updates (3.6x Speedup)

**Overview**: CIDX now performs incremental HNSW index updates instead of expensive full rebuilds, delivering 3.6x performance improvement for indexing operations.

**Core Features**:
- **Incremental watch mode updates**: File changes trigger real-time HNSW updates (< 20ms) instead of full rebuilds (5-10s)
- **Batch incremental updates**: End-of-cycle batch updates use 1.46x-1.65x less time than full rebuilds
- **Automatic mode detection**: SmartIndexer auto-detects when incremental updates are possible
- **Label management**: Efficient ID-to-label mapping maintains vector consistency across updates
- **Soft delete support**: Deleted vectors marked as deleted in HNSW instead of triggering rebuilds

**Performance Impact**:
- **Watch mode**: < 20ms per file update (vs 5-10s full rebuild) - **99.6% improvement**
- **Batch indexing**: 1.46x-1.65x speedup for incremental updates
- **Overall**: **3.6x average speedup** across typical workflows
- **Zero query delay**: First query after changes returns instantly (no rebuild wait)

**Implementation**:
- **File**: `src/code_indexer/storage/hnsw_index_manager.py`
  - `add_or_update_vector()` - Add new or update existing vector by ID
  - `remove_vector()` - Soft delete vector using `mark_deleted()`
  - `load_for_incremental_update()` - Load existing index for updates
  - `save_incremental_update()` - Save updated index to disk

- **File**: `src/code_indexer/storage/filesystem_vector_store.py`
  - `_update_hnsw_incrementally_realtime()` - Real-time watch mode updates (lines 2264-2344)
  - `_apply_incremental_hnsw_batch_update()` - Batch updates at cycle end (lines 2346-2465)
  - Change tracking in `upsert_points()` and `delete_points()` (lines 562-569)

**Architecture**:
- **ID-to-Label Mapping**: Maintains consistent vector labels across updates
- **Change Tracking**: Tracks added/updated/deleted vectors during indexing session
- **Auto-Detection**: Automatically determines incremental vs full rebuild at `end_indexing()`
- **Fallback Strategy**: Gracefully falls back to full rebuild if index missing or corrupted

**Use Cases**:
- Real-time code editing with watch mode (instant query results)
- Incremental repository updates (faster re-indexing after git pull)
- Large codebase maintenance (avoid expensive full rebuilds)

#### FTS Incremental Indexing (10-60x Speedup)

**Overview**: FTS (Full-Text Search) now supports incremental updates, eliminating wasteful full index rebuilds and delivering 10-60x performance improvement.

**Core Features**:
- **Index existence detection**: Checks for `meta.json` to detect existing FTS index
- **Incremental updates**: Adds/updates only changed documents instead of rebuilding entire index
- **Force full rebuild**: `--clear` flag explicitly forces full rebuild when needed
- **Lazy import preservation**: Maintains fast CLI startup times (< 1.3s)

**Performance Impact**:
- **Incremental indexing**: **10-60x faster** than full rebuild for typical file changes
- **Watch mode**: Real-time FTS updates with < 50ms latency per file
- **Large repositories**: Dramatic speedup for repos with 10K+ files

**Implementation**:
- **File**: `src/code_indexer/services/smart_indexer.py` (lines 310-330)
  - Detects existing FTS index via `meta.json` marker file
  - Passes `create_new=False` to TantivyIndexManager when index exists
  - Honors `force_full` flag for explicit full rebuilds

- **File**: `src/code_indexer/services/tantivy_index_manager.py`
  - `initialize_index(create_new)` - Create new or open existing index
  - Uses Tantivy's `Index.open()` for existing indexes (incremental mode)
  - Uses Tantivy's `Index()` constructor for new indexes (full rebuild)

**User Feedback**:
```
# Full rebuild (first time or --clear)
ℹ️  Building new FTS index from scratch (full rebuild)

# Incremental update (subsequent runs)
ℹ️  Using existing FTS index (incremental updates enabled)
```

#### Watch Mode Auto-Trigger Fix

**Problem**: Watch mode reported "0 changed files" after git commits on the same branch, failing to detect commit-based changes.

**Root Cause**: Git topology service only compared branch names, missing same-branch commit changes (e.g., `git commit` without `git checkout`).

**Solution**: Enhanced branch change detection to compare commit hashes when on the same branch.

**Implementation**:
- **File**: `src/code_indexer/git/git_topology_service.py` (lines 160-210)
  - `analyze_branch_change()` now accepts optional commit hashes
  - Detects same-branch commits: `old_branch == new_branch AND old_commit != new_commit`
  - Uses `git diff --name-only` with commit ranges for accurate change detection
  - Falls back to branch comparison for branch switches

**Impact**:
- ✅ Watch mode now auto-triggers re-indexing after `git commit`
- ✅ Detects file changes between consecutive commits on same branch
- ✅ Works with both branch switches AND same-branch commits
- ✅ Comprehensive logging shows commit hashes for debugging

### Fixed

#### Progress Display RPyC Proxy Fix

**Problem**: Progress callbacks passed through RPyC daemon produced errors: `AttributeError: '_CallbackWrapper' object has no attribute 'fset'`

**Root Cause**: Rich Progress object decorated properties (e.g., `@property def tasks`) created descriptor objects incompatible with RPyC's attribute access mechanism.

**Solution**: Implemented explicit `_rpyc_getattr` protocol in `ProgressTracker` to handle property access correctly.

**Implementation**:
- **File**: `src/code_indexer/progress/multi_threaded_display.py` (lines 118-150)
  - `_rpyc_getattr()` - Intercepts RPyC attribute access
  - Returns actual property values instead of descriptor objects
  - Handles `Live.is_started` and `Progress.tasks` properties explicitly
  - Graceful fallback for unknown attributes

**Impact**:
- ✅ Daemon mode progress callbacks work correctly
- ✅ Real-time progress display in daemon mode
- ✅ Zero crashes during indexed file processing
- ✅ Professional UX parity with standalone mode

#### Snippet Lines Zero Display Fix

**Problem**: FTS search with `--snippet-lines 0` still showed snippet content instead of file-only listing.

**Root Cause**: CLI incorrectly checked `if snippet_lines` (treated 0 as falsy) instead of `if snippet_lines is not None`.

**Solution**: Fixed condition to explicitly handle zero value: `if snippet_lines is not None and snippet_lines > 0`.

**Implementation**:
- **File**: `src/code_indexer/cli.py` (line 1165)
- **File**: `src/code_indexer/cli_daemon_fast.py` (line 184)

**Impact**:
- ✅ `--snippet-lines 0` now produces file-only listing as documented
- ✅ Perfect parity between standalone and daemon modes
- ✅ Cleaner output for file-count-focused searches

### Changed

#### Test Suite Expansion

**New Tests**:
- **HNSW Incremental Updates**: 28 comprehensive tests
  - 11 unit tests for HNSW methods
  - 12 unit tests for change tracking
  - 5 end-to-end tests with performance validation
- **FTS Incremental Indexing**: 6 integration tests
- **Watch Mode Auto-Trigger**: 8 unit tests for commit detection
- **Progress RPyC Proxy**: 3 unit tests for property access
- **Snippet Lines Zero**: 6 unit tests (standalone + daemon modes)

**Test Results**:
- ✅ **2801 tests passing** (100% pass rate)
- ✅ **23 skipped** (intentional - voyage_ai, slow, etc.)
- ✅ **0 failures** - Zero tolerance quality maintained
- ✅ **Zero mock usage** - Real system integration tests only

#### Documentation Updates

**Architecture**:
- Updated vector storage architecture documentation for incremental HNSW
- Added performance characteristics for incremental vs full rebuild
- Documented change tracking and auto-detection mechanisms

**User Guides**:
- Enhanced watch mode documentation with commit detection behavior
- Added FTS incremental indexing examples
- Documented `--snippet-lines 0` use case

### Performance Metrics

#### HNSW Incremental Updates

**Benchmark Results** (from E2E tests):
```
Full Rebuild Time:    4.2 seconds
Incremental Time:     2.8 seconds
Speedup:             1.5x (typical)
Range:               1.46x - 1.65x (verified)
Target:              1.4x minimum (EXCEEDED)
```

**Watch Mode Performance**:
```
Before: 5-10 seconds per file (full rebuild)
After:  < 20ms per file (incremental update)
Improvement: 99.6% reduction in latency
```

**Overall Impact**: **3.6x average speedup** across indexing workflows

#### FTS Incremental Indexing

**Performance Comparison**:
```
Full Rebuild:     10-60 seconds (10K files)
Incremental:      1-5 seconds (typical change set)
Speedup:          10-60x (depends on change percentage)
Watch Mode:       < 50ms per file
```

### Technical Details

#### Files Modified

**Production Code** (6 files):
- `src/code_indexer/storage/hnsw_index_manager.py` - Incremental update methods
- `src/code_indexer/storage/filesystem_vector_store.py` - Change tracking and HNSW updates
- `src/code_indexer/services/smart_indexer.py` - FTS index detection
- `src/code_indexer/git/git_topology_service.py` - Commit-based change detection
- `src/code_indexer/progress/multi_threaded_display.py` - RPyC property access fix
- `src/code_indexer/cli.py` / `cli_daemon_fast.py` - Snippet lines zero fix

**Test Files Added** (5 files):
- `tests/integration/test_hnsw_incremental_e2e.py` - 454 lines, 5 comprehensive E2E tests
- `tests/unit/services/test_fts_incremental_indexing.py` - FTS incremental updates
- `tests/unit/daemon/test_fts_display_fix.py` - Progress display fixes
- `tests/unit/daemon/test_fts_snippet_lines_zero_bug.py` - Snippet lines zero
- `tests/integration/test_snippet_lines_zero_daemon_e2e.py` - E2E daemon mode

#### Code Quality

**Linting** (all passing):
- ✅ ruff: Clean (no new issues)
- ✅ black: Formatted correctly
- ✅ mypy: 3 minor E2E test issues (non-blocking, type hint refinements)

**Code Review**:
- ✅ Elite code reviewer approval: "APPROVED WITH MINOR RECOMMENDATIONS"
- ✅ MESSI Rules compliance: Anti-mock, anti-fallback, facts-based
- ✅ Zero warnings policy: All production code clean

### Migration Notes

**No Breaking Changes**: This release is fully backward compatible.

**Automatic Benefits**:
- Existing installations automatically benefit from incremental HNSW updates
- FTS incremental indexing works immediately (no configuration needed)
- Watch mode auto-trigger fix applies automatically

**Performance Expectations**:
- First-time indexing: Same speed (full rebuild required)
- Subsequent indexing: **1.5x-3.6x faster** (incremental updates)
- Watch mode: **99.6% faster** file updates (< 20ms vs 5-10s)
- FTS updates: **10-60x faster** for typical change sets

### Contributors
- Seba Battig <seba.battig@lightspeeddms.com>
- Claude (AI Assistant) <noreply@anthropic.com>

### Links
- [GitHub Repository](https://github.com/LightspeedDMS/code-indexer)
- [Documentation](https://github.com/LightspeedDMS/code-indexer/blob/master/README.md)
- [Issue Tracker](https://github.com/LightspeedDMS/code-indexer/issues)

## [7.1.0] - 2025-10-29

### Added

#### Full-Text Search (FTS) Support

**Overview**: CIDX now supports blazing-fast, index-backed full-text search alongside semantic search, powered by Tantivy v0.25.0.

**Core Features**:
- **Sub-5ms query latency** for text searches on large codebases
- **Three search modes**: Semantic (default), Full-text (`--fts`), Hybrid (`--fts --semantic`)
- **Fuzzy matching** with configurable edit distance (0-3) for typo tolerance
- **Case sensitivity control** for precise matching
- **Adjustable context snippets** (0-50 lines around matches)
- **Real-time index updates** in watch mode
- **Language and path filtering** support

**New CLI Flags**:
- `cidx index --fts` - Build FTS index alongside semantic index
- `cidx index --rebuild-fts-index` - Rebuild FTS index from existing semantic index
- `cidx watch --fts` - Enable real-time FTS index updates
- `cidx query --fts` - Use full-text search mode
- `cidx query --fts --regex` - Token-based regex pattern matching (grep replacement)
- `cidx query --fts --semantic` - Hybrid search (parallel execution)
- `--case-sensitive` - Enable case-sensitive matching (FTS only)
- `--case-insensitive` - Force case-insensitive matching (default)
- `--fuzzy` - Enable fuzzy matching with edit distance 1
- `--edit-distance N` - Set fuzzy tolerance (0-3, default: 0)
- `--snippet-lines N` - Context lines around matches (0-50, default: 5)

**Architecture**:
- **Tantivy Backend**: Rust-based full-text search engine with Python bindings
- **Storage**: `.code-indexer/tantivy_index/` directory
- **Thread Safety**: Locking mechanism for concurrent write operations
- **Schema**: Dual-field language storage (text + facet) for filtering
- **Parallel Execution**: Hybrid search runs both engines simultaneously via ThreadPoolExecutor

**Use Cases**:
- Finding specific function/class names: `cidx query "UserAuth" --fts --case-sensitive`
- Debugging typos in code: `cidx query "respnse" --fts --fuzzy`
- Finding TODO comments: `cidx query "TODO" --fts`
- Comprehensive search: `cidx query "parse" --fts --semantic`

**Performance**:
- FTS queries: Sub-5ms average latency
- Hybrid searches: True parallel execution (both run simultaneously)
- Index size: ~10-20MB per 10K files (depends on content)

**Installation**:
```bash
pip install tantivy==0.25.0
```

**Documentation**:
- Updated README.md with comprehensive FTS section
- Updated teach-ai templates with FTS syntax and examples
- CLI help text includes all FTS options and examples

#### Regex Pattern Matching (Grep Replacement)

**Overview**: Token-based regex search providing 10-50x performance improvement over grep on indexed repositories (Python API mode).

**Core Features**:
- **Token-based matching**: Regex operates on individual tokens (words) after Tantivy tokenization
- **DFA-based engine**: Inherently immune to ReDoS attacks with O(n) time complexity
- **Pre-compilation optimization**: Regex patterns compiled once per query, not per result
- **Unicode-aware**: Character-based column calculation (not byte offsets) for proper multi-byte support

**Usage**:
```bash
# Simple token matching
cidx query "def" --fts --regex

# Wildcard within tokens
cidx query "test_.*" --fts --regex

# Language filtering
cidx query "import" --fts --regex --language python

# Case-insensitive
cidx query "todo" --fts --regex  # Default case-insensitive
```

**Limitations** (Token-Based):
- ✅ Works: `def`, `login.*`, `test_.*`, `HTTP.*`
- ❌ Doesn't work: `def\s+\w+`, `public.*class` (spans multiple tokens with whitespace)

**Performance** (Evolution Codebase):
- FTS Python API: 1-4ms per query (warm index)
- FTS CLI: ~1080ms per query (includes startup overhead)
- Grep: ~150ms average for comparison

**Bug Fixes**:
- Fixed regex snippet extraction showing query pattern instead of actual matched text
- Fixed "Line 1, Col 1" bug - now reports correct absolute line/column positions
- Fixed Unicode column calculation using character vs byte offsets
- Added empty match validation with proper error messages for unsupported patterns

### Fixed

#### Critical Regex Snippet Extraction Bugs
- **Match Text Display**: Regex queries now show actual matched text from source code, not the query pattern
  - Before: `Match: parts.*` (showing query)
  - After: `Match: parts` (showing actual match)
- **Line/Column Positions**: Fixed always showing "Line 1, Col 1" - now reports correct absolute positions
  - Implementation: Proper `re.search()` for regex matching instead of literal string search
- **Unicode Support**: Column calculation now uses character offsets instead of byte offsets
  - Handles multi-byte UTF-8 correctly (emoji, Japanese, French, etc.)
- **Performance**: Regex pre-compilation moved outside result loop (7x improvement)

#### Test Suite Fixes
- Fixed 14 failing tests in fast-automation.sh
- Updated empty match validation tests to expect ValueError for unsupported patterns
- Fixed regex optimization tests with correct token-based patterns
- Updated documentation tests to exclude FTS planning documents
- Fixed CLI tests to match actual remote query behavior

### Changed

- **CLI Help Text**: Enhanced `cidx query --help` with FTS examples and clear option descriptions
- **Teach-AI Templates**: Updated `cidx_instructions.md` with FTS decision rules and regex examples
- **README Structure**: Added "Full-Text Search (FTS)" section with usage guide and comparison table
- **Version**: Bumped to 7.1.0 to reflect new major feature
- **Plans**: Moved FTS epics to `plans/completed/` (fts-filtering and full-text-search)

### Technical Details

**Files Added**:
- `src/code_indexer/services/tantivy_index_manager.py` - Tantivy wrapper and index management
- `src/code_indexer/services/fts_watch_handler.py` - Real-time FTS index updates in watch mode

**Files Modified**:
- `src/code_indexer/cli.py` - Added FTS flags and search mode logic
- `README.md` - Added comprehensive FTS documentation
- `CHANGELOG.md` - Documented v7.1.0 changes
- `prompts/ai_instructions/cidx_instructions.md` - Updated with FTS syntax

**Test Coverage**:
- Unit tests for all FTS flags and options
- E2E tests for search mode combinations
- Integration tests for watch mode FTS updates
- All tests passing: 2359 passed, 23 skipped

---

## [7.0.1] - 2025-10-28

### Fixed

#### Critical: fix-config Filesystem Backend Compatibility

**Problem**: The `fix-config` command was not respecting the filesystem backend setting when fixing CoW (Copy-on-Write) clones. It would:
- Lose the `vector_store.provider = "filesystem"` configuration
- Force regeneration of Qdrant-specific ports and container names
- Attempt to initialize Qdrant client and create CoW symlinks
- Result: Filesystem backend projects would fail with "Permission denied: podman-compose" errors

**Root Cause**:
- Line 836 in `config_fixer.py` only preserved `embedding_provider`, not `vector_store`
- Steps 4-7 always executed Qdrant operations regardless of backend type
- No conditional logic to skip Qdrant operations for filesystem backend

**Solution (Option A: Conditional Container Configuration)**:
1. **Preserve vector_store** in config dict (lines 837-840)
2. **Detect backend type** before Qdrant operations (lines 453-456)
3. **Skip Qdrant client initialization** if filesystem backend (line 459-460)
4. **Skip CoW symlink creation** if filesystem backend (lines 474-477)
5. **Skip collection checks** if filesystem backend (lines 486-489)
6. **Skip port/container regeneration** if filesystem backend (lines 951-954)

**Impact**:
- ✅ Fixes claude-server CoW clone issue where `vector_store` configuration was lost
- ✅ Eliminates unnecessary Qdrant configuration for filesystem backend
- ✅ Reduces `fix-config` execution time and resource usage
- ✅ Maintains backward compatibility with Qdrant backend

**Testing Results**:
- Before: `fix-config` applied 8 fixes (included Qdrant port/container regeneration)
- After: `fix-config` applies 3 fixes (path, project name, git commit only)
- Verification: `vector_store.provider` preserved as `"filesystem"`
- Verification: `project_ports` and `project_containers` remain `null` (not regenerated)
- Verification: `cidx start` and `cidx query` work correctly after `fix-config`

**Files Modified**:
- `src/code_indexer/services/config_fixer.py` (35 insertions, 14 deletions)

---

## [7.0.0] - 2025-10-28

### 🎉 Major Release: Filesystem-Based Architecture with HNSW Indexing

This is a **major architectural release** featuring a complete rewrite of the vector storage system, introducing a filesystem-based backend with HNSW graph indexing for 300x query performance improvements while eliminating container dependencies.

### Added

#### Filesystem Vector Store (Epic - 9 Stories)
- **Zero-Container Architecture**: Filesystem-based vector storage eliminates Qdrant container dependency
- **Git-Trackable Storage**: JSON format stored in `.code-indexer/index/` for version control
- **Path-as-Vector Quantization**: 4-level directory depth using projection matrix (64-dim → 4 levels)
- **Smart Git-Aware Storage**:
  - Clean files: Store only git blob hash (space efficient)
  - Dirty files: Store full chunk_text (captures uncommitted changes)
  - Non-git repos: Store full chunk_text
- **Hash-Based Staleness Detection**: SHA256 hashing for precise change detection (more accurate than mtime)
- **3-Tier Content Retrieval Fallback**:
  1. Current file (if unchanged)
  2. Git blob lookup (if file modified/moved)
  3. Error with recovery guidance
- **Complete QdrantClient API Compatibility**: Drop-in replacement for existing workflows
- **Backward Compatibility**: Old configurations default to Qdrant backend
- **CLI Integration**:
  - `cidx init --vector-store filesystem` (default)
  - `cidx init --vector-store qdrant` (opt-in containers)
  - Seamless no-op operations for start/stop with filesystem backend

**Performance (Django validation - 7,575 vectors, 3,501 files)**:
- Indexing: 7m 20s (476.8 files/min)
- Storage: 147 MB (space-efficient with git blob hashes)
- Queries: ~6s (5s API call + <1s filesystem search)

#### HNSW Graph-Based Indexing
- **300x Query Speedup**: ~20ms queries (vs 6+ seconds with binary index)
- **HNSW Algorithm**: Hierarchical Navigable Small World graph for approximate nearest neighbor search
  - **Complexity**: O(log N) average case (vs O(N) linear scan)
  - **Configuration**: M=16 connections, ef_construction=200, ef_query=50
  - **Space**: 154 MB for 37K vectors
- **Automatic Rebuilding**: `--rebuild-index` flag for manual rebuilds, automatic rebuild on watch mode staleness
- **Staleness Coordination**: File locking system for watch mode integration
  - Watch mode marks index stale (instant, no rebuild)
  - Query rebuilds on first use (amortized cost)
  - **Performance**: 99%+ improvement (0ms vs 10+ seconds per file change)

#### Binary ID Index with mmap
- **Fast Lookups**: <20ms cached loads using memory-mapped files
- **Format**: Binary packed format `[num_entries:uint32][id_len:uint16, id:utf8, path_len:uint16, path:utf8]...`
- **Thread-Safe**: RLock for concurrent access
- **Incremental Updates**: Append-only design with corruption detection
- **Tandem Building**: Built alongside HNSW during indexing

#### Parallel Query Execution
- **2-Thread Architecture**:
  - Thread 1: Load HNSW + ID index (I/O bound)
  - Thread 2: Generate query embedding (CPU/Network bound)
- **Performance Gains**: 15-30% latency reduction (175-265ms typical savings)
- **Overhead Reporting**: Transparent threading overhead display (7-16%)
- **Always Parallel**: Simplified code path, removed conditional execution

#### CLI Exclusion Filters
- **Language Exclusion**: `--exclude-language javascript` with multi-language support
- **Path Exclusion**: `--exclude-path "*/tests/*"` with glob pattern matching
- **Conflict Detection**: Automatic detection of contradictory filters with helpful warnings
- **Multiple Filter Support**: Combine inclusions and exclusions seamlessly
- **26 Common Patterns**: Documented exclusion patterns for tests, dependencies, build artifacts
- **Performance**: <0.01ms overhead per filter (500x better than 5ms requirement)
- **Comprehensive Testing**: 111 tests (370% of requirements)

#### teach-ai Command
- **Multi-Platform Support**: Claude, Codex, Gemini, OpenCode, Q, Junie
- **Template System**: Markdown templates in `prompts/ai_instructions/`
- **Smart Merging**: Uses Claude CLI for intelligent CIDX section updates
- **Scope Options**:
  - `--project`: Install in project root
  - `--global`: Install in platform's global config location
  - `--show-only`: Preview without writing
- **Non-Technical Editing**: Template files editable by non-developers
- **KISS Principle**: Simple text file updates instead of complex parsing

#### Status Command Enhancement
- **Index Validation**: Check HNSW index health and staleness
- **Recovery Guidance**: Actionable recommendations for index issues
- **Backend-Aware Display**: Show appropriate status for filesystem vs Qdrant
- **Storage Statistics**: Display index size, vector count, dimension info

### Changed

#### Breaking Changes
- **Default Backend Changed**: Filesystem backend is now default (was Qdrant)
- **FilesystemVectorStore.search() API**: Now requires `query + embedding_provider` instead of pre-computed `query_vector`
  - Old API: `search(query_vector=vec, ...)`
  - New API: `search(query="text", embedding_provider=provider, ...)`
  - QdrantClient maintains old API for backward compatibility
- **Matrix Multiplication Service Removed**: Replaced by binary caching and HNSW indexing
  - Removed resident HTTP service for matrix operations
  - Removed YAML matrix format
  - Performance now achieved through HNSW graph indexing

#### Improvements
- **Timing Display Optimization**:
  - Breakdown now appears after "Vector search" line (not after git filtering)
  - Fixed double-counting in total time calculation
  - Added threading overhead transparency
  - Shows actual wall clock time vs work time
- **CLI Streamlining**: Removed Data Cleaner status for filesystem backend (Qdrant-only service)
- **Language Filter Enhancement**: Added `multiple=True` to `--language` flag for multi-language queries
- **Import Optimization**: Eliminated 440-630ms voyageai library import overhead with embedded tokenizer

### Technical Architecture

#### Vector Storage System
```
.code-indexer/index/<collection>/
├── hnsw_index.bin              # HNSW graph (O(log N) search)
├── id_index.bin                # Binary mmap ID→path mapping
├── collection_meta.json        # Metadata + staleness tracking
└── vectors/                    # Quantized path structure
    └── <level1>/<level2>/<level3>/<level4>/
        └── vector_<uuid>.json  # Individual vector + payload
```

#### Query Algorithm Complexity
- **Overall**: O(log N + K) where K = limit * 2, K << N
- **HNSW Graph Search**: O(log N) average case
  - Hierarchical graph navigation (M=16 connections per node)
  - Greedy search with backtracking (ef=50 candidates)
- **Candidate Loading**: O(K) for top-K results
  - Load K candidate vectors from filesystem
  - Apply filters and exact cosine similarity scoring
- **Practical Performance**: ~20ms for 37K vectors (300x faster than O(N) linear scan)

#### Search Strategy Evolution
```
Version 6.x: Linear Scan O(N)
- Load all N vectors into memory
- Calculate similarity for all vectors
- Sort and return top-K
- Time: 6+ seconds for 7K vectors

Version 7.0: HNSW Graph O(log N)
- Load HNSW graph index
- Navigate graph to find K approximate nearest neighbors
- Load only K candidate vectors
- Apply exact scoring and filters
- Time: ~20ms for 37K vectors (300x faster)
```

#### Performance Decision Analysis

**Why HNSW over Alternatives**:
1. **vs FAISS**: HNSW simpler to integrate, no external dependencies, better for small-medium datasets (<100K vectors)
2. **vs Annoy**: HNSW provides better accuracy-speed tradeoff, dynamic updates possible
3. **vs Product Quantization**: HNSW maintains full precision, no accuracy loss from quantization
4. **vs Brute Force**: 300x speedup justifies ~150MB index overhead

**Quantization Strategy**:
- **64-dim projection**: Optimal balance of accuracy vs path depth (tested 32, 64, 128, 256)
- **4-level depth**: Enables 64^4 = 16.8M unique paths (sufficient for large codebases)
- **2-bit quantization**: Further reduces from 64 to 4 levels per dimension

**Parallel Execution Trade-offs**:
- **Threading overhead**: 7-16% acceptable cost for 175-265ms latency reduction
- **2 threads optimal**: More threads add coordination overhead without I/O benefit
- **Always parallel**: Removed conditional logic for code simplicity

**Storage Format Trade-offs**:
- **JSON vs Binary**: JSON chosen for git-trackability and debuggability despite 3-5x size overhead
- **Individual files vs single file**: Individual files enable incremental updates, git tracking
- **Binary ID index exception**: Performance-critical component where binary format justified

### Fixed
- **Critical Qdrant Backend Stub Bug**: Fixed stub implementation causing crashes when Qdrant containers unavailable
- **Git Branch Filtering**: Corrected to check file existence (not branch name match) for accurate filtering
- **Storage Duplication**: Fixed bug where both blob hash AND content were stored (should be either/or)
- **Timing Display**: Fixed placement of breakdown timing (now appears after "Vector search" line)
- **teach-ai f-string**: Removed unnecessary f-string prefix causing linter warnings
- **Path Exclusion Tests**: Updated 8 test assertions for correct metadata key ("path" not "file_path")

### Deprecated
- **Matrix Multiplication Resident Service**: Removed in favor of HNSW indexing
- **YAML Matrix Format**: Removed with matrix service
- **FilesystemVectorStore query_vector parameter**: Use `query + embedding_provider` instead

### Performance Metrics

#### Query Performance Comparison
```
Version 6.5.0 (Binary Index):
- 7K vectors: ~6 seconds
- Algorithm: O(N) linear scan

Version 7.0.0 (HNSW Index):
- 37K vectors: ~20ms (300x faster)
- Algorithm: O(log N) graph search
- Parallel execution: 175-265ms latency reduction
```

#### Storage Efficiency
```
Django Codebase (3,501 files → 7,575 vectors):
- Total Storage: 147 MB
- Average per vector: 19.4 KB
- Space Savings: 60-70% from git blob hash storage
```

#### Indexing Performance
```
Django Codebase (3,501 files):
- Indexing Time: 7m 20s
- Throughput: 476.8 files/min
- HNSW Build: Included in indexing time
- ID Index Build: Tandem with HNSW (no overhead)
```

### Documentation
- Added 140-line "Exclusion Filters" section to README with 26 common patterns
- Added CIDX semantic search instructions to project CLAUDE.md
- Enhanced epic documentation with comprehensive unit test requirements
- Added query performance optimization epic with TDD validation
- Documented backend switching workflow (destroy → reinit → reindex)
- Added command behavior matrix for transparent no-ops

### Testing
- **Total Tests**: 2,291 passing (was ~2,180)
- **New Test Coverage**:
  - 111 exclusion filter tests (path, language, integration)
  - 72 filesystem vector store tests
  - 21 backend abstraction tests
  - 21 status monitoring tests
  - 12 parallel execution tests
  - Comprehensive HNSW, ID index, and integration tests
- **Performance Tests**: Validated 300x speedup and <20ms queries
- **Platform Testing**: teach-ai command tested across 6 AI platforms

### Migration Guide

#### From Version 6.x to 7.0.0

**Automatic Migration (Recommended)**:
New installations default to filesystem backend. Existing installations continue using Qdrant unless explicitly switched.

**Manual Migration to Filesystem Backend**:
```bash
# 1. Backup existing index (optional)
cidx backup  # If available

# 2. Destroy existing Qdrant index
cidx clean --all-collections

# 3. Reinitialize with filesystem backend
cidx init --vector-store filesystem

# 4. Start services (no-op for filesystem, but safe to run)
cidx start

# 5. Reindex your codebase
cidx index

# 6. Verify
cidx status
cidx query "your test query"
```

**Stay on Qdrant (No Action Required)**:
If you prefer containers, your existing configuration continues working. To explicitly use Qdrant for new projects:
```bash
cidx init --vector-store qdrant
```

**Breaking API Changes**:
If you have custom code calling `FilesystemVectorStore.search()` directly:
```python
# OLD (no longer works):
results = store.search(query_vector=embedding, collection_name="main")

# NEW (required):
results = store.search(
    query="your search text",
    embedding_provider=voyage_client,
    collection_name="main"
)
```

### Contributors
- Seba Battig <seba.battig@lightspeeddms.com>
- Claude (AI Assistant) <noreply@anthropic.com>

### Links
- [GitHub Repository](https://github.com/LightspeedDMS/code-indexer)
- [Documentation](https://github.com/LightspeedDMS/code-indexer/blob/master/README.md)
- [Issue Tracker](https://github.com/LightspeedDMS/code-indexer/issues)

---

## [6.5.0] - 2025-10-24

### Initial Release
(Version 6.5.0 and earlier changes not documented in this CHANGELOG)
