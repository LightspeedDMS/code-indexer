# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v9.3.76

### Features

- feat: Dependency Map Repair Mode with Smart Health Detection (Story #342). Five-check health detector inspects dependency map output for missing/zero-char/undersized domain files, orphan files, count mismatches, stale index, and incomplete/malformed domains. Conditional repair button appears when anomalies detected, triggering 5-phase repair: Claude CLI re-analysis with retry, orphan removal, JSON reconciliation, programmatic index regeneration, and post-validation. New API endpoints GET /admin/dependency-map/health and POST /admin/dependency-map/repair with admin auth. Shared dep_map_file_utils module eliminates cross-service duplication.

### Bug Fixes

- fix: Phase 1 repair weak success check -- now re-validates via health detector after Claude CLI re-analysis to confirm anomaly is actually resolved, retries if structure still invalid (Story #342)
- fix: Phase 1 repair broken content fed back to Claude -- deletes broken domain file before calling analyzer so Claude starts fresh instead of preserving malformed structure from previous analysis (Story #342)

## v9.3.75

### Bug Fixes

- fix: Activity journal panel lifecycle -- panel now hides after analysis completes, polling stops via X-Journal-Active header instead of perpetual 3s refresh (Story #329)
- fix: Journal init ordering -- moved _activity_journal.init() after staging dir cleanup to prevent journal file deletion (Story #329)
- fix: Progress bar stuck at 20% -- pass tracked_job_id to _execute_analysis_passes() for per-domain progress updates across 30-90% range (Story #329)
- fix: 0-char domain analysis retry -- when Pass 2 domain produces empty output, retry up to 3 times before marking failed; applies to both full and delta analysis (Bug #341)

## v9.3.74

### Features

- feat: Dependency Map live activity journal and progress bar (Story #329). Real-time visibility into analysis progress via ActivityJournalService with thread-safe byte-offset tailing, granular per-domain progress tracking replacing coarse 3-step progress, HTMX-polled journal panel with auto-scroll, and Claude CLI prompt appendix for activity logging.

### Maintenance

- fix: Add --follow-imports=silent to mypy pre-commit hook to prevent cascade into pre-existing errors
- fix: Silence 12 pre-existing mypy type errors in dependency_map_analyzer.py, dependency_map_service.py, dependency_map_routes.py

## v9.3.73

### Bug Fixes

- fix: _resolve_repo_path() now consults alias JSON target_path as highest-priority resolution, fixing regex_search and directory_tree on versioned/local repos like cidx-meta (Bug #340)
- fix: handle_regex_search error message no longer hardcodes '.*' pattern — uses actual repository_alias
- refactor: handle_directory_tree unified to use _resolve_repo_path() for both global and non-global repos, removing duplicate AliasManager code

## [9.3.72] - 2026-02-28

### Fixed

- Bug #330: Git commit date parsing now handles non-ISO format with space before timezone offset (`2026-02-02 14:37:25 -0600`). Replaced fragile `.replace()` hack with proper `_parse_git_date()` function supporting git `%ci` format, ISO 8601, and timezone-less dates.

## [9.3.71] - 2026-02-28

### Security

- Bug #337: regex_search on cidx-meta now filters unauthorized repo description content. Non-admin users only see regex matches from repo files they have access to. Admin users retain full access.

### Performance

- Bug #338: _get_all_repo_aliases() results cached with 60s TTL. Cache auto-invalidated via observer callback in GroupAccessManager when repos are granted/revoked from groups. Eliminates N+1 DB queries on every cidx-meta file access.

## [9.3.70] - 2026-02-28

### Security

- Bug #336: cidx-meta file-level access filtering. Non-admin users browsing cidx-meta via `list_files`, `get_file_content`, `browse_directory`, or `directory_tree` now only see description files for repos they have access to. Repo-description `.md` files are identified by matching their stem against known repo aliases; non-repo files (e.g., README.md) pass through unconditionally. Admin users retain full access.

## [9.3.69] - 2026-02-28

### Security

- Epic #327: Repository Access Control Enforcement - Complete Coverage. Closed all side-channel leaks (Story #331) ensuring restricted users are fully blind to repos they cannot access. Changes include:
  - Centralized guard now checks `repo_alias` parameter (AC3, protects `enter_write_mode`, `exit_write_mode`, `wiki_article_analytics`)
  - Guard fails closed when access filtering service unavailable (AC9)
  - `_get_available_repos()` filters error suggestions by user access (AC1, 15 call sites)
  - `_expand_wildcard_patterns()` filters wildcard expansion by user access (AC2, 5 call sites)
  - Omni-search handlers pass user through wildcard expansion (AC4, defense-in-depth)
  - Composite repo creation validates component repo access (AC5)
  - cidx-meta query results filtered for referenced repo cross-references (AC6)
  - Omni-search error dicts filtered to hide unauthorized repo aliases (AC7, 5 handlers)
  - Cache handles documented as accepted risk (AC8, UUID4 + short TTL)
  - Repo categories documented as accepted risk (AC10, generic labels)
  - REST API group endpoints restricted to admin-only (Story #318)
  - Metadata listing endpoints filtered by group access (Story #316)

## [9.3.68] - 2026-02-27

### Fixed

- regex_search fails with KeyError 'text' when ripgrep returns binary/non-UTF8 context lines (Bug #320). Added `_extract_line_text()` helper to handle both `text` and `bytes` (base64-encoded) formats in ripgrep JSON output at all three vulnerable locations: path extraction, match lines, and context lines.
- Dashboard Recent Jobs sort order now shows running/pending jobs above completed jobs regardless of timestamps (Story #328). Added `_status_priority_sort_key()` function that sorts by status priority (running > pending > completed), then by most-recently-active timestamp within each group.

## [9.3.67] - 2026-02-27

### Fixed

- Dependency map scheduler never triggers on fresh init (Bug #326). The `_scheduler_loop()` had a chicken-and-egg bug where `next_run` is NULL on fresh init and only set after successful analysis. Added bootstrap branch that triggers first delta analysis when `next_run` is NULL and no analysis is currently running.
- Dependency map jobs (full and delta) now show "server" instead of "Unknown" in Recent Activity dashboard. Added `repo_alias="server"` to both `register_job()` calls.

## [9.3.66] - 2026-02-27

### Fixed

- Dashboard auto-refresh now works for all 4 sections (Bug #321). Replaced `htmx.ajax()` calls with `fetch()` API in `refreshAll()` to fix HTMX 1.9.10 silently dropping 2 of 4 concurrent requests. Added re-entrancy guard, `_cidxRedirecting` 401 redirect guard, shared `applyResponse()` helper, and fixed `<p>` vs `<tbody>` mismatch for empty recent-jobs table.

## [9.3.65] - 2026-02-27

### Fixed

- Dashboard "Unknown" repository for server-wide operations (langfuse_sync, startup_reconcile, index_cleanup) now shows "server" instead. Added `repo_alias="server"` to job registration calls.
- Dashboard null duration for index_cleanup jobs. Added missing `update_status(status="running")` call after job registration so `started_at` timestamp is set.

## [9.3.64] - 2026-02-27

### Added

- Unified Job Tracking Subsystem (Epic #261): Standalone JobTracker class with hybrid memory+SQLite architecture providing 100% dashboard visibility for all background operations. Covers 11 new operation types (description_refresh, catchup_processing, dependency_map_full, dependency_map_delta, langfuse_sync, research_assistant_chat, scheduled_catchup, startup_reconcile, scip_resolution, immediate_catchup, index_cleanup) with defensive try/except wrapping, retention policy for high-frequency jobs, dual tracking for research assistant, and skip_tracking coordination to prevent double-counting. 199 new unit tests.

## [9.3.63] - 2026-02-27

### Added

- Self-monitoring prompt now detects repeating warnings as unrecoverable state signals (#309). Adds frequency-based escalation (5+ occurrences override ignore list), SQL query template for warning pattern detection, stuck-state examples (git packfile corruption, dead daemon, stale locks), and prompt version tracking.

## [9.3.62] - 2026-02-26

### Fixed

- FTS `get_all_indexed_paths()` now uses `searcher.search(Query.all_query())` instead of `segment_readers()` which does not exist in tantivy-py v0.25.0. Fixes silent FTS branch cleanup failure on staging.
- Added explicit HNSW branch cleanup step (`_cb_hnsw_branch_cleanup`) in `change_branch()` flow. Rebuilds HNSW filtered index directly on CoW snapshot, providing defense-in-depth for branch isolation independent of the `cidx index` subprocess.
- `_cb_cidx_index` now logs subprocess stdout (DEBUG) and stderr (WARNING) instead of discarding them, making branch-change indexing failures visible in server logs.

## [9.3.61] - 2026-02-26

### Fixed

- HNSW background rebuilder now respects branch isolation (#306). Query-time rebuild reads `current_branch` from HNSW metadata and filters vectors by `hidden_branches` payload, preventing ghost vectors from reappearing after CoW snapshot.
- FTS branch isolation cleanup now persists through CoW snapshot (#307). Tantivy document deletions run POST-CoW on the versioned snapshot directory instead of pre-CoW on the base clone.
- `is_stale()` uses `visible_count` for filtered HNSW indexes, preventing false-positive staleness detection after branch isolation rebuilds (#306).

### Changed

- Branch change is now asynchronous (#308). Web UI and MCP tool return HTTP 202 with job_id; client polls `/api/jobs/{job_id}` for status. Duplicate branch changes blocked with HTTP 409. Removed blocking `confirm()`/`alert()` dialogs from web UI.

## [9.3.60] - 2026-02-26

### Added

- Branch isolation for Semantic (HNSW), FTS, and SCIP indexes (#305). HNSW filtered rebuild eliminates ghost vectors without destroying VoyageAI embeddings (~300-700ms vs 6-7 min with --clear). FTS branch isolation deletes stale Tantivy documents on branch switch. SCIP orphan cleanup removes .scip.db files for projects no longer in codebase.

### Fixed

- Ghost vectors appearing in semantic search after branch switch: HNSW index now rebuilt with only branch-visible vectors.
- is_stale() false positive after filtered HNSW rebuild: metadata flag prevents unnecessary full rebuilds.
- end_indexing() overwriting filtered HNSW with full rebuild: ordering flag preserves branch isolation.
- fts_manager parameter not threaded to all 4 branch isolation call sites (was dead code).
- Golden repo branch change using --clear unnecessarily: switched to incremental + HNSW filter.

## [9.3.59] - 2026-02-26

### Added

- MCP group-based access control: tools now enforce group membership permissions via `access_filtering_service` (#300).
- MCP system credentials management: admin endpoints and MCP tools for managing system-wide MCP credentials (#302).
- Change active branch for golden repositories: MCP tool, web route, and UI dropdown for switching tracked branch (#303).
- Event-driven wiki sidebar cache invalidation: replaced filesystem polling (rglob) with 9 mutation hooks for instant cache coherence (#304).

### Fixed

- Wiki search/nav duplicate badge deduplication: category badge suppressed when it duplicates visibility badge (#301).

## [9.3.58] - 2026-02-25

### Fixed

- File descriptor leak in `CleanupManager._delete_index()` causing cascading failures during index cleanup (#297). Added robust deletion with EMFILE-aware fallback, exponential backoff for retries (1s-60s cap), circuit breaker after 5 consecutive failures, and FD usage monitoring (80% threshold skip).

## [9.3.57] - 2026-02-25

### Improved

- Wiki toolbar and breadcrumbs now show the repo name (e.g., "code-indexer Wiki Home" instead of "Wiki Home") for clear identification of which wiki is being viewed.

## [9.3.56] - 2026-02-25

### Improved

- Wiki root page now renders with the full wiki UI (sidebar, toolbar, search, dark/light mode toggle) when no `home.md` exists, instead of showing a bare list of links. The article listing is rendered through the `article.html` template for a consistent experience from first click.

## [9.3.55] - 2026-02-25

### Fixed

- Added missing `python-frontmatter` dependency to `pyproject.toml`. Wiki article rendering crashed with `ModuleNotFoundError: No module named 'frontmatter'` on servers where the package was not manually installed.

## [9.3.54] - 2026-02-25

### Fixed

- Wiki access control: `_check_wiki_access()` and `_check_user_wiki_access()` now check both group membership (groups.db) AND user role (users table) for admin determination. Fixes 404 for users with `role=admin` who are not in the "admins" group, making wiki auth consistent with the admin UI's auth pattern.

## [9.3.53] - 2026-02-25

### Fixed

- Golden repos metadata schema migration: `ensure_table_exists()` now auto-migrates existing databases by detecting missing columns via `PRAGMA table_info` and adding them with `ALTER TABLE`. Fixes `sqlite3.OperationalError: no such column: wiki_enabled` crash on servers with databases created before the wiki feature was added. Also adds missing `category_id` and `category_auto_assigned` columns to CREATE TABLE for new databases.

## [9.3.52] - 2026-02-25

### Fixed

- API key seeding startup sync: config API keys (Anthropic, VoyageAI) now always synced to os.environ on server startup, not just when config is blank. Fixes silent semantic search failure when shell environment has a stale/invalid key while server config has the valid one.
- Semantic search error surfacing: `_perform_search()` now tracks per-repo errors and re-raises when ALL repos fail, instead of silently returning empty results. Wiki search route surfaces actual error messages (e.g., "Invalid VoyageAI API key") to the frontend.

### Added

- Wiki client-side navigation: sidebar article clicks now use AJAX content swap with `history.pushState()` instead of full page reloads. Eliminates TOC flicker/rebuild when navigating between articles -- sidebar stays untouched, only main content and active highlight change. Browser back/forward supported via `popstate` handler.
- Wiki search defaults to Semantic mode instead of Full Text Search

### Changed

- Wiki search box CSS: input and mode dropdown now render on a single compact row with matched heights, reducing vertical space consumed by the search area

## [9.3.51] - 2026-02-23

### Fixed

- Watch handler race conditions during MCP write mode file editing (#274):
  - handle_edit_file, handle_create_file, handle_delete_file no longer start auto-watch when write mode is active (checks marker file existence)
  - _write_mode_run_refresh() stops auto-watch before executing refresh to prevent racing
  - GitAwareWatchHandler and SimpleWatchHandler now filter .tmp_ prefixed temp files created by _atomic_write_file()
- Research assistant issue_manager.py symlink: prefers bundled copy in CIDX codebase over ~/.claude fallback, fixes broken symlinks on production servers

### Added

- Dependency map code mass bubble sizing: graph nodes include total_file_count aggregated from _journal.json repo_sizes, enabling bubble size to reflect actual code mass (#273)
- Bundled issue_manager.py in src/code_indexer/server/scripts/ for production deployment without ~/.claude dependency
- MCP tool documentation: expanded dependency map discovery guidance in cidx_quick_reference, search_code, and first_time_user_guide with two result types explanation and workflow steps

## [9.3.50] - 2026-02-23

### Added

- Golden repo divergent branch auto-recovery: `GitPullUpdater.update()` detects "divergent branches" errors from `git pull` and automatically recovers via `git fetch origin` + `git reset --hard origin/{branch}` (#272)
- Manual "Force Re-sync" button in golden repo details card UI with confirmation dialog, CSRF-protected POST endpoint at `/admin/golden-repos/{alias}/force-resync` (#272)
- `force_reset` parameter propagation through `RefreshScheduler.trigger_refresh_for_repo()` → `_submit_refresh_job()` → `_execute_refresh()` → `GitPullUpdater.update()` (#272)
- Branch detection fallback: uses `git rev-parse --abbrev-ref HEAD` with safe fallback to "main" (#272)
- Force Re-sync button conditionally hidden for local:// repos (git-backed repos only) (#272)

## [9.3.49] - 2026-02-23

### Fixed

- Jobs tab database-driven queries: fix regression where completed/failed jobs were invisible after memory optimization (#271)
- `_get_all_jobs()` in routes.py now delegates to `BackgroundJobManager.get_jobs_for_display()` which queries SQLite for historical jobs and merges with memory-resident active jobs
- Added `list_jobs_filtered()` on `BackgroundJobsSqliteBackend` with status/type/search/exclude/pagination support
- Job dict normalization bridges template key mismatches between memory BackgroundJob objects and SQLite rows

## [9.3.48] - 2026-02-22

### Fixed

- Local repo indexing lifecycle: `register_local_repo()` now runs `cidx init` idempotently so Langfuse and cidx-meta repos get `.code-indexer/` initialized, enabling the CoW snapshot pipeline (#270)
- `meta_description_hook.on_repo_added()`/`on_repo_removed()` now trigger `trigger_refresh_for_repo("cidx-meta-global")` after writing/deleting `.md` files, so golden repo descriptions are indexed and searchable (#270)
- Wired refresh scheduler to `meta_description_hook` at server startup via `set_refresh_scheduler()` (#270)

## [9.3.47] - 2026-02-22

### Added

- Externalize indexable_extensions to server config: new IndexingConfig with 60 default extensions, Web UI "Indexing Settings" section, cascade to all golden repos on save, seed on registration, sync on refresh (#223)
- Enhanced dependency graph bubble sizing: nodes include incoming_dep_count and outgoing_dep_count fields, _nodeRadius() combines repo count + dependency connectivity with synergy bonus, increased spacing and collision padding (#260)

### Fixed

- Config page 500 error: _get_current_config() missing "indexing" section in template context

## [9.3.46] - 2026-02-22

### Changed

- Remove 6 unjustified SQLite performance indexes that had no backing SQL query patterns; keep only 7 justified indexes with migration to drop stale indexes from existing databases (#269)
- Replace research_messages composite index (session_id, created_at) with single-column (session_id) to match actual query pattern (#269)
- MCP credential lookup: O(users x credentials) Python iteration replaced with O(1) indexed SQL query via new get_mcp_credential_by_client_id() method (#269)

### Fixed

- 2 pre-existing stale unit tests in test_database_manager.py (table count 11->22, missing enable_scip column)

## [9.3.45] - 2026-02-22

### Fixed

- BackgroundJobManager threading deadlock: single-job persist, lock separation (snapshot under lock, I/O outside), startup cleanup of stale jobs (#267)
- Dashboard initial load blocking on uncached health check (55-165s): return placeholder HealthCheckResponse(DEGRADED), HTMX lazy-loads real data (#266)
- RefreshScheduler attempting refresh on uninitialized per-user Langfuse local repos (#268)
- Local repos (local://, bare filesystem paths) no longer auto-refreshed by scheduler; only explicit triggers from writer services
- ApiKeySyncService: removed dangerous legacy credentials file deletion that was breaking Claude CLI OAuth sessions

## [9.3.43] - 2026-02-21

### Fixed

- Dependency map analyzer: pass prompts to Claude CLI via stdin (input=) instead of command-line argument (-p) to avoid E2BIG (ARG_MAX) errors with large prompts on repositories with many files

## [9.3.42] - 2026-02-21

### Changed

- Dependency map settings: removed arbitrary upper-bound limits from Web UI and backend validation (interval hours, pass timeout, max turns) so users can set their own values freely
- Removed dependency_map_pass3_max_turns setting entirely (pass 3 is deterministic, not AI-based; setting was dead code)

## [9.3.41] - 2026-02-21

### Fixed

- Auto-updater cross-version restart: legacy redeploy marker at `/var/lib/cidx-pending-redeploy` (v8.15.0) is now migrated to `~/.cidx-server/pending-redeploy` (v9.x), fixing production servers that skip intermediate versions
- Auto-updater forced deployment now checks `restart_server()` return value and logs error on failure instead of silently ignoring restart failures
- Auto-updater `_ensure_sudoers_restart()` return value now logged as warning when sudoers rule creation fails, instead of silent fire-and-forget

## [9.3.40] - 2026-02-21

### Fixed

- Langfuse sync per-user repo refresh: sync_project() no longer acquires a project-level write lock on non-existent alias (e.g., "langfuse_Claude_Code-global"), eliminating ValueError every 60 seconds on staging; instead triggers refresh for each per-user repo that received writes

## [9.3.39] - 2026-02-20

### Fixed

- Reconciliation/scheduler startup race condition: write locks now initialized before reconciliation runs, preventing "lock not found" errors on first boot (Bug #239, P0)
- Stale write mode marker cleanup: orphaned `.write_mode` files from crashed sessions are detected and removed on startup, preventing permanent read-path redirection (Bug #240, P0)
- SCIP composite repositories gracefully handle missing `call_graph` table instead of raising unhandled exceptions (Bug #238)
- File mtime comparison uses integer-truncated timestamps to avoid sub-second precision mismatches across filesystems (Bug #241)
- XSS sanitization fallback: golden repo HTML templates escape untrusted content server-side when DOMPurify client library is not loaded (Bug #242)
- JWT token caching removed from DeploymentExecutor auto-updater; tokens are now fetched fresh per request to avoid using expired credentials (Bug #243)
- Write lock alias resolution normalizes `-global` suffix consistently across acquire/release/check operations
- Write lock `acquire()` return value correctly propagated to callers; `.git` directory validation added for git repo detection
- Write mode check is now fail-closed: raises `PermissionError` when `golden_repos_dir` is not configured, instead of silently allowing writes
- Generate-missing-descriptions endpoint protected by concurrency guard (`threading.Lock`) to prevent duplicate parallel runs
- `_discover_and_assign_new_repos()` return type annotation corrected from `Set[str]` to `Tuple[Set[str], bool]`
- Research assistant session setup detects and removes broken symlinks before recreating them

## [9.3.38] - 2026-02-20

### Fixed

- RefreshScheduler no longer deletes master golden repos on first refresh; cleanup guard only schedules versioned snapshots for deletion (Story #236)
- Git pull always targets master golden repo path, never versioned snapshots (Story #236)
- CoW snapshots always created from master golden repo, not from previous versioned snapshots (Story #236)
- Startup reconciliation restores missing master golden repos via reverse CoW clone from latest versioned snapshot (Story #236)
- Delta analysis no longer overwrites full domain documentation with change summaries; truncation guard rejects results below 50% of original size (Story #234)
- Delta analysis no longer falsely claims dependency removal for repos not in the analysis scope (Story #235)
- Description filename in reconciliation correctly strips -global suffix from alias names

### Added

- POST /admin/diagnostics/generate-missing-descriptions endpoint for bulk description generation (Story #233)
- Generate Missing Descriptions button in diagnostics UI (Story #233)
- Path traversal protection on cidx-meta description file paths

## [9.3.37] - 2026-02-20

### Fixed

- Refresh jobs submitted via MCP, REST API, and Web UI now use the requesting user's username as submitter, making jobs visible in Recent Activity and via get_job_details
- Previously all user-triggered refresh jobs were owned by "system", preventing the triggering user from tracking their own jobs

## [9.3.37] - 2026-02-20

### Fixed

- User-triggered refresh jobs (MCP, REST, Web UI) now correctly attribute submitter_username to the actual user instead of defaulting to "system", making jobs visible in Recent Activity and get_job_details

## [9.3.36] - 2026-02-20

### Changed

- All golden repo refresh entry points (MCP, REST API, Web UI) now route through RefreshScheduler index-source-first pipeline
- Removed legacy GoldenRepoManager.refresh_golden_repo() method (108 lines) that bypassed versioned snapshot pipeline
- RefreshScheduler._resolve_global_alias() encapsulates -global suffix convention, accepts bare or suffixed alias names
- RefreshScheduler.trigger_refresh_for_repo() returns job_id and accepts submitter_username for attribution

### Fixed

- MCP refresh_golden_repo tool was using old in-place refresh path instead of index-source-first pipeline
- Golden repo removal endpoint now cancels both legacy and scheduler-submitted refresh jobs
- Double-suffix edge case in alias resolution (my-repo-global-global) prevented by endswith check

## [9.3.35] - 2026-02-19

### Added

- Index-source-first refresh pipeline (Story #229): FTS/temporal/SCIP indexes built on golden repo source before CoW clone, eliminating duplicate indexing
- File-based named write locks via WriteLockManager (Story #230): atomic lock creation with owner identity, PID staleness detection, TTL expiry
- MCP enter_write_mode/exit_write_mode tools (Story #231): write mode redirects read_alias to source for immediate visibility, CRUD gating requires active write mode, synchronous refresh on exit
- Lock-leak protection via try/finally in write mode handlers

### Changed

- RefreshScheduler split into _index_source() and _create_snapshot() phases for index-source-first ordering
- WriteLockManager extracted from RefreshScheduler as standalone module with file-based persistence
- DependencyMapService and LangfuseTraceSyncService pass owner identity to write lock facade
- AliasManager.read_alias() checks .write_mode marker before normal alias resolution
- FileCRUDService enforces write-mode gating for write-exception repos

## [9.3.34] - 2026-02-19

### Added

- Write-lock coordination for local golden repo writers (Story #227): prevents RefreshScheduler from snapshotting cidx-meta and Langfuse folders mid-write
- Write-lock registry on RefreshScheduler with non-blocking acquire/release/check semantics
- RefreshScheduler skips CoW clone for write-locked local repos, writers trigger explicit refresh after completion
- DependencyMapService and LangfuseTraceSyncService acquire write locks during analysis/sync operations

### Fixed

- Delta analysis refresh trigger was missing on main success path (P0 code review finding)
- write_lock() context manager now raises on failed acquire instead of silently proceeding
- release_write_lock() gracefully handles releasing unheld locks instead of raising RuntimeError

## [9.3.33] - 2026-02-19

### Fixed

- New repo domain discovery no longer aborts when `_domains.json` is missing - starts with empty domain list, enabling bootstrap of domain assignments from scratch (first-time or after versioned snapshot rebuild)

## [9.3.32] - 2026-02-19

### Fixed

- Delta refresh new repo discovery no longer silently skipped when `_index.md` is missing - `identify_affected_domains()` now returns the `__NEW_REPO_DISCOVERY__` sentinel for new repos even without `_index.md`, so `_discover_and_assign_new_repos()` is correctly triggered

## [9.3.31] - 2026-02-19

### Fixed

- Delta refresh now creates new domains when Claude assigns repos to domains not yet in _domains.json
- Delta refresh tracking no longer finalizes new repos when _domains.json write fails (prevents permanent data loss)
- Wired P1/P2 pass timing into Recent Run Metrics (was always showing 0.0s)

## [9.3.30] - 2026-02-19

### Fixed

- **Delta analysis write-path failures for versioned cidx-meta** - `_update_affected_domains()` now reads existing domain `.md` files from the versioned path (via `read_file` parameter) while writing updates to the live path. `_discover_and_assign_new_repos()` and `run_delta_analysis()` ensure the live `dependency-map/` directory exists before writing. Fixes `[Errno 2] No such file or directory` for `_domains.json` writes and "Domain file not found" warnings during delta refresh.

## [9.3.29] - 2026-02-19

### Fixed

- **Domain Explorer empty on staging: versioned path resolution for local repos** - `_get_cidx_meta_read_path()` now checks `.versioned/cidx-meta/v_*/` directly instead of relying on `get_actual_repo_path()`. The latter always returned the live clone_path for local repos (because the directory exists as a write sentinel), causing it to miss the versioned content where `_domains.json`, `_index.md`, and domain `.md` files actually live.

## [9.3.28] - 2026-02-19

### Fixed

- **Domain Explorer reads from versioned cidx-meta path** - All read operations in `DependencyMapService`, `DependencyMapDomainService`, and `DependencyMapDashboardService` now resolve the versioned cidx-meta path via `_get_cidx_meta_read_path()` and `cidx_meta_read_path` property. Previously, these services read from the live `golden-repos/cidx-meta/` directory which is mostly empty after Story #224 promoted cidx-meta to the versioned golden repo platform.

## [9.3.27] - 2026-02-19

### Changed

- **Langfuse repos now use versioned golden repo platform (Story #226)** - Langfuse trace repos (`langfuse_*`) now use the same RefreshScheduler-based versioned snapshot pipeline as cidx-meta and git repos. Eliminates three competing indexing systems (in-place subprocess, SimpleWatchHandler, RefreshScheduler) that caused ghost vectors, HNSW cache staleness, and Tantivy FTS errors.

### Removed

- **In-place indexing from `register_langfuse_golden_repos()`** - Removed `subprocess.run()` calls to `cidx init` and `cidx index --fts` that ran in the live Langfuse folder. Registration now only calls `register_local_repo()`. RefreshScheduler handles indexing via versioned snapshots.
- **Watch-mode startup for Langfuse folders** - Removed `auto_watch_manager.start_watch()` calls from `_on_langfuse_sync_complete()`. No watchdog observers are created for Langfuse directories.
- **`LangfuseWatchIntegration` class** - Deleted `langfuse_watch_integration.py` (116 lines). Auto-start watches, reset timeouts, and status reporting for Langfuse folders are all superseded by RefreshScheduler.
- **`_create_simple_watch_handler()` from DaemonWatchManager** - Deleted the non-git folder watch handler factory (~80 lines) and the `_is_git_folder()` helper. `_create_watch_handler()` now always creates `GitAwareWatchHandler`.
- **Bug #177 VectorStoreConfig workaround** - Removed the non-git folder fallback that initialized default filesystem backend config. No longer needed without non-git watch paths.

## [9.3.26] - 2026-02-19

### Fixed

- **Local repo git detection bypassed by stale paths** - `_resolve_git_repo_path()` now checks the repo URL from the registry first. If the URL starts with `local://`, git operations are blocked immediately regardless of filesystem state. Previously, a stale pre-migration directory with `.git` at `.cidx-server/golden-repos/cidx-meta/` caused `_resolve_repo_path()` to find it via its cascading path search, allowing git operations on `local://` repos that should not support them.
- **First-gen git handlers missing `.git` validation** - All 8 first-gen handlers (`handle_git_log`, `handle_git_show_commit`, `handle_git_file_at_revision`, `handle_git_diff`, `handle_git_blame`, `handle_git_file_history`, `handle_git_search_commits`, `handle_git_search_diffs`) now use `_resolve_git_repo_path()` instead of `_resolve_repo_path()` directly, ensuring consistent `.git` validation and local repo detection across all git operations.

## [9.3.25] - 2026-02-18

### Fixed

- **FTS ghost vectors in versioned snapshots** - CoW clone inherited the tantivy index from the previous version, causing `cidx index --fts` to open it incrementally and preserve stale entries for deleted/renamed files. Now deletes `tantivy_index/` after CoW clone so FTS rebuilds from scratch, matching the semantic index behavior.
- **Timezone timestamp bug in versioned directory names** - `datetime.utcnow().timestamp()` produces a future timestamp on non-UTC servers (e.g. 6 hours ahead on UTC-6) because Python interprets the naive datetime as local time. Replaced with `int(time.time())` which returns the correct UTC epoch. This was causing `_has_local_changes()` to always return False, breaking automatic change detection for local repos.
- **Git tools error for global repos** - All git handlers (first-gen and second-gen) now use `_resolve_git_repo_path()` helper that validates `.git` directory existence before attempting git operations. Local repos (e.g. cidx-meta-global backed by `local://`) return a clear error message instead of crashing with filesystem errors. Also distinguishes "repo not found" from "repo is local" in error messages.

## [9.3.24] - 2026-02-18

### Changed

- **Promote cidx-meta to versioned golden repo platform (Story #224)** - cidx-meta now participates in the same immutable versioned snapshot pipeline as git-based golden repos. Each refresh cycle creates a CoW clone, indexes from scratch, and atomically swaps the alias. Eliminates ghost vectors, HNSW cache staleness, and concurrent read/write conflicts that plagued in-place reindexing.
- **Non-git change detection for local repos** - RefreshScheduler uses mtime-based change detection (`_has_local_changes()`) for `local://` repos instead of `git pull`. Only creates new versioned snapshot when source files are newer than the latest version directory.
- **Removed all special-case cidx-meta reindex code** - Deleted `reindex_cidx_meta()` from meta_description_hook.py, `_reindex_cidx_meta()` from dependency_map_service.py, `_reindex_cidx_meta_background()` from app.py, and reindex call from description_refresh_scheduler.py. Writers (meta description hook, description refresh scheduler, dependency map service) now only modify files; RefreshScheduler handles versioning on its regular cycle.
- **Removed `_cidx_meta_index_lock`** - No longer needed since concurrent subprocess `cidx index` calls on the same directory are eliminated by the versioned snapshot approach.

### Fixed

- **Temporal indexing guard for non-git repos** - RefreshScheduler skips `--index-commits` for `local://` repos even when `enable_temporal=1` in database, preventing failures on repos without `.git` directory.
- **Refresh path resolution for local repos** - `refresh_golden_repo()` now uses `clone_path` (source directory) instead of `get_actual_repo_path()` (versioned CoW path) when refreshing local repos, ensuring source files are copied correctly.
- **Redundant `cidx init` on existing repos** - `_execute_post_clone_workflow()` skips `cidx init` when configuration already exists and `force_init=False`, avoiding unnecessary re-initialization during versioned refresh.

## [9.3.23] - 2026-02-18

### Fixed

- Use `--clear` for cidx-meta reindex after dependency map stage-then-swap to eliminate ghost vectors from renamed/deleted domain files. The previous `--detect-deletions` approach was bypassed by progressive metadata resume logic when a prior indexing operation was interrupted by server restart.

## [9.3.22] - 2026-02-18

### Fixed

- **cidx-meta stale vector cleanup** - Added `--detect-deletions` flag to all cidx-meta reindex calls in `dependency_map_service.py` and `meta_description_hook.py`. Previously, when dependency map domain files were renamed or removed during stage-then-swap, old vectors remained as ghost entries in the HNSW index. The `--detect-deletions` flag triggers `SmartIndexer._detect_and_handle_deletions()` which reconciles disk state against the index and removes vectors for files that no longer exist.

## [9.3.21] - 2026-02-18

### Fixed

- **cidx-meta reindex race condition** - Centralized all `cidx index` calls for cidx-meta through a single `reindex_cidx_meta()` function with `threading.Lock`. Prevents concurrent indexing corruption when background startup reindex overlaps with golden repo registration or periodic description refresh. Three callers (app.py startup, meta_description_hook, description_refresh_scheduler) now serialize through one lock.

## [9.3.20] - 2026-02-18

### Changed

- **cidx-meta background reindex** - Extracted `_reindex_cidx_meta_background()` helper and runs `cidx index` in a daemon thread instead of blocking server startup. Newly-added repo descriptions are picked up without delaying bootstrap.
- **Correlation ID for background thread** - Background reindex thread now generates a fresh correlation ID since Python `contextvars` are not inherited by `threading.Thread`.

### Fixed

- **Removed ANTHROPIC_API_KEY gate from dependency map analyzer** - `_invoke_claude_cli()` no longer requires `ANTHROPIC_API_KEY` env var. Claude CLI works with both API keys and Claude subscriptions. The hard gate was blocking subscription users.

## [9.3.19] - 2026-02-18

### Changed

- **Structured Cross-Domain Dependency Schema (Story #217)** - Replaced regex-based cross-domain dependency inference with deterministic structured table parsing. All 4 prompt templates (output-first, standard, delta merge, new domain) now use a structured table schema with columns: This Repo, Depends On, Target Domain, Type, Why, Evidence. Eliminates phantom edges from text-matching heuristics.
- **NEGATION_INDICATORS removed** - Deleted the 17-phrase negation filter constant entirely. No longer needed with structured table input.
- **_index.md enriched** - Cross-Domain Dependency Graph table now includes Type and Why columns from structured declarations.
- **Web UI dependency evidence** - Domain detail panel shows dependency Type badge and Why description for each outgoing and incoming connection.
- **Graph data dep_type** - Graph data endpoint includes dependency type in edge data for D3 visualization tooltips.

## [9.3.18] - 2026-02-18

### Fixed

- **AC9 edge count still zero** - Edge counting parser broke out of the loop too early when encountering descriptive text between the "Cross-Domain Dependencies" heading and the actual table. Now only breaks on new section headers (`#`).

## [9.3.17] - 2026-02-18

### Fixed

- **AC9 metrics recording zero values** - `_record_run_metrics` was reading from the staging directory after stage-then-swap had already moved files to the final directory. Now reads from `final_dir` (the live output directory).
- **AC9 edge count always zero** - Edge counting regex matched mermaid-style arrows (`A --> B`) but `_index.md` now uses markdown table format. Replaced with markdown table row parser for the Cross-Domain Dependencies section.

## [9.3.16] - 2026-02-18

### Fixed

- **Claude CLI auth broken on standalone servers** - Conditional env stripping now only removes `ANTHROPIC_API_KEY` from subprocess environment when running inside Claude Code (`CLAUDECODE` present). Standalone servers retain the API key for Claude CLI authentication. Affects `dependency_map_analyzer.py`, `repo_analyzer.py`, and `description_refresh_scheduler.py`.

## [9.3.15] - 2026-02-18

### Added

- **Dependency map pipeline hardening (Story #216)** - Fix critical bugs and establish self-improvement loop:
  - Programmatic `_index.md` generation replaces Claude-based Pass 3, eliminating heading instability and saving an expensive CLI call (AC2)
  - Domain stability anchor feeds previous `_domains.json` to Pass 1 for consistent domain assignments across runs (AC5)
  - New repo discovery in delta refresh replaces `__NEW_REPO_DISCOVERY__` stub with real Claude-based domain assignment (AC6)
  - Delta merge prompts include `clone_path` and MCP `search_code` tool access for source-verified updates (AC7)
  - Quality metrics tracking with SQLite `dependency_map_run_history` table and dashboard display (AC9)

### Fixed

- **Cross-domain graph edges not rendering** - Rewrote `_parse_cross_domain_deps` with pipe-splitting to handle both 3-column and 4-column markdown tables (AC1)
- **Delta refresh heading mismatch** - Programmatic headings are now deterministic, fixing `_parse_repo_to_domain_mapping` regex match (AC3)
- **Ghost domains in `_domains.json`** - Added `_reconcile_domains_json` to remove entries without corresponding `.md` analysis files (AC4)
- **Empty repos wasting analysis time** - `_enrich_repo_sizes` now filters repos with `file_count=0` before analysis (AC8)

## [9.3.14] - 2026-02-17

### Added

- **Dependency Map admin page (Epic #211)** - New admin dashboard for visualizing cross-repository domain dependencies:
  - D3.js interactive force-directed graph with zoom, pan, drag, and BFS depth control (Story #215)
  - Two-panel domain explorer with searchable/filterable domain list and detail panel (Story #214)
  - Job status dashboard with health badge, schedule card, and repo coverage table (Stories #212-#213)
  - Graph viewport vertical resize via drag bar below SVG container

### Fixed

- **Claude CLI subprocess auth failures** - Strip `CLAUDECODE` and `ANTHROPIC_API_KEY` environment variables from subprocess calls to prevent nested session errors and session token overriding valid subscription auth. Affects `dependency_map_analyzer.py`, `description_refresh_scheduler.py`, and `repo_analyzer.py`.
- **Repo coverage showing CHANGED after fresh analysis** - Dashboard `_get_current_commit()` used different fallback values (`None`) than the pipeline `_get_commit_hashes()` (`"local"`/`"unknown"`) for repos without metadata.json, causing perpetual status mismatch.

### Improved

- Domain selection highlight in left panel uses four visual cues: left accent bar, background wash, accent text color, and bold name
- Graph node labels positioned below bubbles for better contrast, full domain names without truncation
- Schedule card datetime formatting: `white-space: nowrap` prevents wrapping, shortened to HH:MM
- Actions card: replaced dropdown with two explicit side-by-side buttons (Full Analysis / Delta Refresh)

---

## [9.3.13] - 2026-02-16

### Improved

- **MCP discovery documentation for cidx-meta-global (#210)** - Expanded tool descriptions and handler responses so MCP clients understand what cidx-meta-global is, how to interpret its search results (file_path-to-alias mapping), and what to do when it is unavailable. Changes across 5 files:
  - `cidx_quick_reference.md` - Expanded from 6 lines to comprehensive discovery workflow documentation
  - `handlers.py` quick_reference - Added `discovery` section with meta_repo, workflow, result_mapping, and fallback fields
  - `handlers.py` first_time_user_guide - Inserted step 3 "Discover which repository has your topic", renumbered to 9 steps, added cidx-meta-global fallback error
  - `search_code.md` - Expanded MANDATORY REPOSITORY DISCOVERY from 4 to 6 steps with result interpretation guidance
  - `list_global_repos.md` - Added ABOUT cidx-meta-global paragraph explaining it is a synthetic discovery repository

---

## [9.3.12] - 2026-02-16

### Changed

- Version bump to verify ripgrep install path detection fix (#207) on staging via auto-deployment bootstrap cycle.

---

## [9.3.11] - 2026-02-16

### Fixed

- **Ripgrep installer fails to detect rg under systemd minimal PATH (#207)** - Added install path check (`~/.local/bin/rg`) to `is_installed()` between the `shutil.which` and subprocess checks. Systemd services use a minimal PATH that excludes `~/.local/bin`, causing both detection and post-install verification to fail even though the binary exists and is executable.

---

## [9.3.10] - 2026-02-16

### Fixed

- **Auto-updater JWT token uses non-existent username (#208)** - Changed JWT username from "cidx-auto-updater" to "admin" since the server's auth middleware looks up the user in the database after validating the token signature. The "cidx-auto-updater" user doesn't exist, causing 401 "User not found" errors on maintenance mode API calls.

---

## [9.3.9] - 2026-02-16

### Changed

- **Auto-updater uses direct JWT token generation instead of HTTP login (#208)** - Replaced HTTP-based `_get_auth_token()` (which required admin password via env vars) with direct JWT minting using the server's `~/.cidx-server/.jwt_secret` file. Eliminates password dependency, works on all servers regardless of admin password, and removes need for `CODE_INDEXER_ADMIN_USER`/`CODE_INDEXER_ADMIN_PASSWORD` environment variables.

---

## [9.3.8] - 2026-02-16

### Fixed

- **Ripgrep installer fails verification when system rg already installed (#207)** - Added `shutil.which("rg")` pre-check to detect system-installed ripgrep before subprocess fallback. Also added `filter='data'` to tarfile extraction for Python 3.12+ compatibility.
- **Maintenance mode API returns 401 unauthenticated (#208)** - Auto-updater maintenance mode API calls (enter, exit, drain-status, drain-timeout) now authenticate with JWT Bearer token. Added `_get_auth_token()` method with token caching and credential support via environment variables.
- **Auto-updater can't read service file without sudo (#209)** - Changed `cat` to `sudo cat` in `_read_service_file()` and `_get_server_python()` for reading systemd service files with restricted permissions.

---

## [9.3.7] - 2026-02-16

### Changed

- Validate auto-updater self-restart + marker + forced-redeploy cycle on staging

---

## [9.3.6] - 2026-02-16

### Changed

- Version bump to validate auto-deployment pipeline end-to-end on staging

---

## [9.3.5] - 2026-02-16

### Fixed

- **Auto-updater marker/status files use non-writable /var/lib/ path** - The `PENDING_REDEPLOY_MARKER` and `AUTO_UPDATE_STATUS_FILE` were at `/var/lib/` which is not writable by the service user on production servers. Moved both to `~/.cidx-server/` (user-writable config directory). This was the root cause of the auto-updater failing to restart cidx-server after self-updating its own code.

---

## [9.3.4] - 2026-02-16

### Fixed

- **Sudoers rule check fails on non-root servers** - The `_ensure_sudoers_restart()` method used `Path.exists()` and `Path.read_text()` to check `/etc/sudoers.d/cidx-server`, but `/etc/sudoers.d/` is not readable by non-root users on Rocky Linux. Fixed by using `sudo cat` subprocess call instead. Also fixed tests that were passing with false positives due to shifted subprocess mock indices.

---

## [9.3.3] - 2026-02-16

### Fixed

- **HOTFIX: Restore _should_retry_on_startup() removed in 9.3.2** - The method was incorrectly identified as dead code and removed, but `run_once.py` calls it on every auto-updater startup. Caused `AttributeError` crash on every poll cycle.

---

## [9.3.2] - 2026-02-16

### Fixed

- **Auto-updater self-restart doesn't restart cidx-server** - When the auto-updater detected its own code changed and self-restarted, the restarted instance found no new git changes and never called `execute()`, leaving cidx-server running old code. Fixed by creating a `PENDING_REDEPLOY_MARKER` before self-restart so the new instance forces a full deployment cycle including server restart. Also improved the forced deploy path with proper exception handling, restart validation, and marker cleanup.

---

## [9.3.1] - 2026-02-16

### Fixed

- **Server restart requires sudo on staging/production** - The diagnostics tab restart feature failed on deployed servers because `systemctl restart` requires root privileges. Fixed by adding `sudo` with full path (`/usr/bin/systemctl`) and a new idempotent `_ensure_sudoers_restart()` step in the deployment executor that creates a minimal NOPASSWD sudoers rule for the service user.

---

## [9.3.0] - 2026-02-16

### Added

- **Server Restart from Diagnostics Tab (Story #205)** - Admin users can now restart the CIDX server directly from the Diagnostics tab without SSH access. Includes confirmation dialog, CSRF protection, rate limiting, and graceful delayed restart (systemd mode via systemctl, dev mode via os.execv).

- **AJAX Toggle for Repository Access (Story #199)** - Grant/revoke repository access on the Groups page now uses AJAX with optimistic UI updates instead of full page reloads. Includes double-click guard, rollback on failure, and dual-mode support (AJAX + form POST fallback).

- **GitHub Bug Report Integration (Story #202)** - Research Assistant handlers now pass GitHub tokens to enable automated bug report creation on GitHub. Includes 5-minute TTL caching for token retrieval.

### Changed

- **Recent Activity fixed to 24h (Story #201)** - Dashboard Recent Activity section now uses a fixed 24-hour window instead of a configurable dropdown, simplifying the UI and matching the most common use case.

---

## [9.2.1] - 2026-02-15

### Changed

- Added critical staging server admin password protection policy to CLAUDE.md

## [9.2.0] - 2026-02-15

### Added

- **Cross-Domain Dependency Graph (Iteration 16)** - Pass 3 index generation now parses all domain files to build a directed cross-domain dependency graph appended to `_index.md`. The graph shows which domains reference other domains' repositories in their Cross-Domain Connections sections, with a summary of edge count, standalone domains, and the specific repos that create each connection. Enables MCP clients to understand the full domain topology at a glance.

### Fixed

- **False positive elimination in cross-domain graph (Iteration 16b)** - Added paragraph-level negation filtering to prevent isolation confirmation text (e.g., "FTS searches across repo-x returned zero results") from being counted as real cross-domain connections. Uses 17 negation indicator phrases to distinguish genuine references from negative-result mentions.

---

## [9.1.0] - 2026-02-14

### Added

- **MCP Self-Registration for Claude CLI Explorations (Story #203)** - CIDX server now auto-registers itself as an MCP server in Claude Code before the first Claude CLI job launch. This enables dependency map explorations to leverage CIDX semantic search tools, reducing API cost and improving analysis quality for inter-repository dependency mapping.

  - MCPSelfRegistrationService with idempotent check-and-register logic
  - MCPSelfRegistrationConfig dataclass for credential persistence across restarts
  - Integration with DependencyMapAnalyzer and ClaudeCliManager worker pool
  - Thread-safe double-check locking for concurrent worker access
  - Graceful degradation when Claude CLI not installed (log + proceed)
  - ConfigService.save_config() for credential persistence

- **Config UI restart indicators** - Config section template now shows restart-required indicators for settings that need server restart to take effect.

### Fixed

- **Diagnostics health check false warnings (Bug #200)** - Empty collection detection now handles three indicators: vector_count==0, unique_file_count==0, or missing hnsw_index section in metadata. Prevents false health warnings for newly added repos.

---

## [9.0.0] - 2026-02-13

### Added

- **Inter-Repository Semantic Dependency Map (Epic #191)** - Multi-pass Claude CLI analysis pipeline that examines source code across all registered golden repos, identifies domain-level relationships (imports, API contracts, shared types, config references, message queues), and produces queryable documents in cidx-meta/dependency-map/. MCP clients can now determine the relevant repo set for cross-repo tasks by reading the dependency map instead of performing exploratory searches.

  - **Full Analysis Pipeline (Story #192)**: Three-pass Claude CLI pipeline (Synthesis -> Per-domain Deep Dive -> Index Generation) with stage-then-swap atomicity. Produces _index.md (domain catalog + repo-to-domain matrix) and per-domain .md files with YAML frontmatter. New DependencyMapAnalyzer and DependencyMapService classes. SQLite tracking table for run state and commit hashes.

  - **Incremental Delta Refresh (Story #193)**: Scheduled daemon thread with configurable interval (default: weekly). Three-way change detection (changed/new/removed repos) via metadata.json current_commit comparison. Updates only affected domain files in-place. Self-correction mandate in merge prompts prevents stale dependency accumulation.

  - **MCP Quick Reference Integration (Story #194)**: New _build_dependency_map_section() in handlers.py. Dynamically shown when dependency-map/ exists with _index.md. Directs MCP clients to check the dependency map FIRST before exploratory searching. Includes domain count and step-by-step workflow.

  - **Manual Trigger MCP Tool (Story #195)**: New trigger_dependency_analysis tool supporting full and delta modes. Returns job ID for progress tracking. Non-blocking lock prevents concurrent runs. Validates configuration (dependency_map_enabled) before execution.

  - **Direct cidx-meta Editing (Story #197)**: Generic global repo write exceptions map allowing power users to edit cidx-meta-global directly via MCP file CRUD tools. Pre-seeded at bootstrap. Auto-watch triggers re-indexing on edits. Quick reference includes correction workflow guidance.

- **New configuration fields**: dependency_map_enabled (default: False), dependency_map_interval_hours (default: 168/weekly), dependency_map_pass_timeout_seconds (default: 600). Accessible via Web UI Config Screen.

---

## [8.17.0] - 2026-02-13

### Improved

- **Reduced list_repositories MCP response size (Story #196)** - Applied field whitelist to strip internal-only fields from list_repositories responses. Removes index_path, created_at, username, path, git_committer_email, ssh_key_used, last_accessed, activated_at, and discovered_repos from both activated and global repo entries. Reduces per-repo payload by ~40%, lowering context window consumption when many repos are registered. Updated tool documentation schema to match.

---

## [8.16.0] - 2026-02-13

### Added

- **Description Refresh Scheduler (Story #190)** - Background scheduler that periodically regenerates stale golden repository descriptions using Claude CLI. Features hash-based bucket scheduling for even distribution, commit-aware change detection, and configurable refresh intervals via Web UI. Includes output validation to detect error messages masquerading as descriptions, comprehensive ANSI terminal escape cleanup (CSI, OSC, bare ESC bytes), and chain-of-thought stripping before YAML frontmatter.

---

## [8.15.0] - 2026-02-13

### Added

- **Repository categories system (Story #186)** - Full CRUD for repo categories with auto-assign based on repo URL patterns, manual override, bulk evaluate. Includes SQLite backend, REST API router, MCP handler support (list_repo_categories tool), and Web UI management page with category listing and assignment.

### Fixed

- **HNSW health check false positive for empty collections** - Diagnostics no longer flags empty but properly initialized collections (0 vectors, no HNSW file) as broken. Reads collection_meta.json vector_count to distinguish empty (valid) from missing (broken) HNSW indexes.

- **Local repos excluded from refresh scheduler** - Local:// repos (like cidx-meta-global) are now filtered in the scheduler loop before job submission, preventing phantom Running/Pending dashboard entries. The local:// check in _execute_refresh() was also moved earlier to avoid unnecessary filesystem scanning.

---

## [8.14.0] - 2026-02-12

### Added

- **Full prompt observability for start_trace/end_trace (Story #185)** - Renamed `topic` to `name` and `feedback` to `summary` for clarity. Added `input` (user prompt), `output` (Claude response), `tags` (categorization), and `intel` (prompt intelligence metadata with frustration, specificity, task_type, quality, iteration fields) parameters. New `update_current_trace_in_context()` method in LangfuseClient updates traces via SDK 3.7.0 context API. Intel fields stored with `intel_` prefix in Langfuse metadata for dashboard filtering.

- **MCP protocol compliance tests** - New test suite validates that `filter_tools_by_role()` strips internal fields and only returns MCP-spec fields (name, description, inputSchema). Tests cover permission filtering and `requires_config` conditional visibility.

### Fixed

- **Diagnostics bugs #186, #187, #188** - Fixed NoneType error in Claude delegation check when config file missing, corrected SQLite schema check to exclude non-existent groups tables, and fixed vector storage health check for temporal collections.

---

## [8.13.0] - 2026-02-11

### Added

- **Sequential trace file naming** - Langfuse trace files now use chronological sequential naming (`001_turn_5544ab00.json`, `002_subagent-code-reviewer_7e3f91c2.json`) instead of raw trace IDs. Files are numbered in ascending chronological order within each session (001=oldest), enabling MCP users to read conversation history in order.

- **Staging directory for trace sync** - New traces are written to a `.langfuse_staging/` directory during sync, then sorted by timestamp and moved to golden-repos with final sequential names. This prevents premature watch handler indexing of incomplete syncs and keeps memory usage low (only lightweight metadata tuples in RAM, not full trace JSON).

- **FTS watch handler for non-git folders** - Langfuse and other non-git watched folders now get an FTS (Tantivy) watch handler attached alongside the semantic indexer. Previously only semantic search was updated on file changes; now FTS indexes are updated in real-time too.

- **Langfuse quick reference documentation** - Enhanced `_build_langfuse_section()` with comprehensive documentation: what Langfuse repos are, file naming conventions, trace JSON structure (trace + observations), and prompt intelligence metadata fields (intel_frustration, intel_specificity, intel_task_type, intel_quality, intel_iteration).

---

## [8.12.2] - 2026-02-11

### Fixed

- **Langfuse repos invisible in Web UI despite being in SQLite (Bug #176)** - `list_golden_repos()` and `get_golden_repo()` read from a stale in-memory dict instead of SQLite, making repos registered after server startup invisible to MCP endpoints and the Web UI. Changed both methods to read directly from `GoldenRepoMetadataSqliteBackend`. Also fixed 11 external `golden_repos` dict accesses in `activated_repo_manager.py`, `git_operations_service.py`, and `app.py`. Eliminated double `GoldenRepoManager` instantiation in `app.py` where migration/bootstrap used a temporary instance whose registrations were lost.

---

## [8.12.1] - 2026-02-11

### Fixed

- **Langfuse sync skips writing traces when files deleted but state persists** - The sync service uses a content hash state file to track which traces have been synced. When trace folders were deleted from disk (e.g., during cleanup) but the state file persisted, the sync permanently skipped re-writing those traces because the hashes matched. Now verifies trace files exist on disk before returning "unchanged", forcing re-write when files are missing.

### Added

- **Operational logging for Langfuse sync service** - Added INFO-level logging for sync iteration start, project sync count, and per-project metrics (traces checked/new/updated/unchanged/errors/duration). Previously the sync thread ran completely silently, making debugging impossible.

---

## [8.12.0] - 2026-02-11

### Changed

- **SQLite single source of truth for golden repo metadata** - Eliminated dual-storage pattern (metadata.json + SQLite). GoldenRepoManager now always uses SQLite backend, fixing Bug #176 where Langfuse repos registered via `register_local_repo()` were invisible to MCP endpoints like `list_repositories`. The `use_sqlite` parameter has been removed from `GoldenRepoManager.__init__()`. When `db_path` is not provided, it auto-computes from `data_dir`. Includes one-time migration from metadata.json to SQLite with per-repo error handling.

---

## [8.11.3] - 2026-02-11

### Fixed

- **Langfuse folders not re-indexed after cleanup or failed indexing** - `register_langfuse_golden_repos()` only ran `cidx init` + `cidx index` for newly registered folders. If a folder was already registered in SQLite but its CIDX index was missing or empty (e.g., after cleanup or failed initial indexing), the function silently skipped re-indexing. Now always checks index existence independently of registration status and rebuilds when needed.

---

## [8.11.2] - 2026-02-11

### Fixed

- **Langfuse search regression** - Fixed three bugs causing MCP search_code to return empty file_path and content for Langfuse trace repositories:
  - `_batch_update_points` was replacing payload instead of merging, losing existing fields (path, language, content) during branch isolation updates
  - `process_files_incrementally` passed only changed files to branch isolation, causing `hide_files_not_in_branch` to hide all files since the subset didn't match the full branch file list
  - Branch isolation (`hide_files_not_in_branch`) was running on non-git folders (Langfuse trace folders have no branches), incorrectly hiding all indexed content
- **Missed branch isolation guard in `_do_incremental_index`** - Added `is_git_available()` check before `hide_files_not_in_branch_thread_safe` call, preventing the same regression through the incremental indexing code path

### Changed

- **search_code tool documentation** - Improved hybrid search mode description with RRF scoring details and added `match_text` field to output schema

---

## [8.11.1] - 2026-02-10

### Fixed

- **Refresh scheduler crash on incremental indexing** - Fixed `NameError: name 'VOYAGE_MULTIMODAL_MODEL' is not defined` in `process_branch_changes_high_throughput()`. The multimodal model constant was only imported locally in a sibling method, causing the refresh scheduler to crash when finalizing indexes after incremental processing.

---

## [8.11.0] - 2026-02-10

### Added

- **register_local_repo() method** (Story #175) - New synchronous registration method on GoldenRepoManager for local folder repos. Encapsulates idempotency, thread safety, SQLite/JSON persistence, GlobalActivator, and lifecycle hooks. Replaces three one-off registration patterns in app.py startup functions.
- **api_metrics.db health monitoring** - Added API Metrics database to the dashboard health honeycomb, completing the 8-database monitoring grid.

### Changed

- **Refactored local repo registration** - `migrate_legacy_cidx_meta()`, `bootstrap_cidx_meta()`, and `register_langfuse_golden_repos()` now delegate to `register_local_repo()` instead of directly manipulating GoldenRepoManager internals. Net reduction of ~150 lines in app.py.

---

## [8.10.0] - 2026-02-10

### Added

- **Langfuse Trace Sync** (Epic #162) - Background service that pulls AI conversation traces from Langfuse projects and makes them semantically searchable. Includes overlap window + content hash deduplication strategy, automatic golden repo registration, watch integration for incremental indexing, per-project sync metrics, and dashboard monitoring with health status and manual sync trigger.

### Changed

- **Trace JSON format**: Trace files now store the trace object first (with user prompt and AI response) followed by observations sorted chronologically by startTime. Previously observations were sorted by ID and alphabetical key sorting placed them before the trace content.
- **Dashboard: Langfuse section repositioned** above Recent Activity for better visibility.
- **Dashboard: "Efficiency" column renamed to "Change Rate"** in Langfuse project metrics table, accurately reflecting what the metric measures (percentage of traces that changed per sync cycle).

### Fixed

- **Dashboard: Langfuse trace count always showed zero** - Folder stats used non-recursive glob (`*.json`) which missed trace files inside session subdirectories. Fixed to use recursive glob (`**/*.json`).
- **Dashboard: Storage Size value wrapping** in the Langfuse storage card on narrow layouts.

---

## [8.9.18] - 2026-02-09

### Added

- **Auto-updater: Fallback clone approach for custom hnswlib** - When the git submodule fails to initialize (due to persistent lock file permission errors), the auto-updater now clones hnswlib directly to `/var/tmp/cidx-hnswlib/` and builds from there. This bypasses all submodule-related issues while still providing the custom hnswlib with `check_integrity()` method.

---

## [8.9.17] - 2026-02-09

### Fixed

- **Auto-updater: Fixed pattern matching for lock file errors** - Changed error pattern from `"config.lock"` to `"could not lock"` to match actual git error message format (`"error: could not lock config file"`). The v8.9.16 cleanup/retry code was not triggering because the pattern didn't match the real error.

---

## [8.9.16] - 2026-02-09

### Added

- **Auto-updater: Resilient submodule update with cleanup & retry** - When `git submodule update` fails due to partial initialization state (lock files, "already exists", worktree configuration errors), the auto-updater now automatically cleans up the corrupted state and retries. This handles scenarios where a previous failed deployment left the submodule in an inconsistent state. Non-recoverable errors (network, authentication) fail immediately without retry.

---

## [8.9.15] - 2026-02-09

### Fixed

- **Auto-updater: Only initialize required submodule** - Changed `git submodule update --init --recursive` to `git submodule update --init third_party/hnswlib`. The `--recursive` flag attempted to initialize ALL submodules (including test-fixtures), each requiring its own safe.directory entry. Since production only needs the custom hnswlib build, we now initialize only that specific submodule.

---

## [8.9.14] - 2026-02-09

### Fixed

- **Auto-updater: Fixed submodule "dubious ownership" error** - Git's `safe.directory` check applies to each repository independently. Submodules like `third_party/hnswlib` need their own safe.directory entries. Added `_ensure_submodule_safe_directory()` method that configures git safe.directory for submodule paths before running `git submodule update`.

---

## [8.9.13] - 2026-02-09

### Changed

- **Version bump to trigger re-deployment** - Triggers auto-updater on staging and production servers to deploy v8.9.12 PrivateTmp fix with new `/var/lib/` status file paths.

---

## [8.9.12] - 2026-02-09

### Fixed

- **Auto-updater: Fixed PrivateTmp isolation issue** - The auto-updater status files were being written to `/tmp/` which is isolated by systemd's `PrivateTmp=yes` setting. Status files written by the service were invisible to external processes. Moved status file locations from `/tmp/` to `/var/lib/`:
  - `PENDING_REDEPLOY_MARKER`: `/tmp/cidx-pending-redeploy` → `/var/lib/cidx-pending-redeploy`
  - `AUTO_UPDATE_STATUS_FILE`: `/tmp/cidx-auto-update-status.json` → `/var/lib/cidx-auto-update-status.json`

---

## [8.9.11] - 2026-02-09

### Fixed

- **Auto-updater: Removed sudo from git commands** - The auto-updater service runs as root, so sudo is unnecessary and was causing issues. Removed sudo from `git pull` and `git submodule update` commands. The v8.9.10 fix (adding sudo) was incorrect - the service already runs as root.

---

## [8.9.10] - 2026-02-09

### Fixed

- **Auto-updater: Added sudo to git_pull** - Fixed permission denied errors when auto-updater runs `git pull` on production servers where `/opt/code-indexer-repo/` is owned by root. The git_pull() method now uses `sudo git pull` to match other sudo-enabled commands.

---

## [8.9.9] - 2026-02-09

### Fixed

- **Research Assistant: Removed timestamps entirely** - Removed the clock/timestamp display from all Research Assistant message templates per user request. Timestamps were appearing inconsistently during HTMX swaps causing confusing UI behavior.
- **Research Assistant: Alt-Enter now inserts newline** - Fixed Alt-Enter and Shift-Enter keyboard shortcuts to explicitly insert newline characters in the textarea. Previously relied on browser default behavior which was unreliable.

### Changed

- **FTS documentation: Added anti-pattern warnings** - Added explicit "COMMON MISTAKES" section to CLAUDE.md and SKILL.md documentation showing WRONG vs CORRECT usage patterns. Prevents confusion between semantic search (for concepts) and FTS mode (for exact identifiers).

---

## [8.9.8] - 2026-02-09

### Changed

- **Self-restart validation test** - This release triggers the self-restart mechanism on staging by modifying the auto-updater code. When deployed, the staging auto-updater (running v8.9.7) should detect the code change, write status file with `pending_restart`, restart its service, and complete deployment with the new code.

---

## [8.9.7] - 2026-02-09

### Added

- **Auto-updater self-restart mechanism** - Solves the "bootstrap problem" where the auto-updater's own code needs to be updated. When the auto-updater detects changes to its own code after `git pull`:
  1. Calculates SHA256 hash of all `auto_update/*.py` files before and after git pull
  2. If hash changed, writes status file (`/tmp/cidx-auto-update-status.json`) with `pending_restart` state
  3. Restarts `cidx-auto-update` systemd service and exits
  4. On service restart, checks status file and retries deployment with the new code
- **Retry-on-startup logic** - If the status file shows `pending_restart` or `failed` state, the auto-updater automatically retries deployment on startup. This ensures deployments eventually succeed even after failures.
- **Deployment state tracking** - Status file tracks deployment state (`pending_restart`, `in_progress`, `success`, `failed`) with version and timestamp information for debugging.

---

## [8.9.6] - 2026-02-09

### Changed

- **Version bump to verify auto-updater sudo fix** - This release tests that the v8.9.5 auto-updater fix (sudo for root-owned directories) works end-to-end. When deployed, the auto-updater should successfully initialize git submodules and build custom hnswlib without manual intervention.

---

## [8.9.5] - 2026-02-09

### Fixed

- **Auto-updater permissions for root-owned directories** - Fixed permission denied errors when auto-updater runs on production servers where `/opt/code-indexer-repo/` and `/opt/pipx/venvs/code-indexer/` are owned by root. Added `sudo` to:
  - `git submodule update --init --recursive` command
  - `pip install pybind11` command
  - `pip install --force-reinstall` for custom hnswlib
  - `pip install -e .` for main package installation

---

## [8.9.4] - 2026-02-08

### Fixed

- **pybind11 pre-installation for hnswlib build** - The hnswlib setup.py imports pybind11 at module level (not as a build dependency), so it must be installed before pip can even parse setup.py. Added explicit `pip install pybind11` step before building custom hnswlib.

---

## [8.9.3] - 2026-02-08

### Added

- **Idempotent build dependency installation** - Added `_ensure_build_dependencies()` method that automatically installs C++ build tools (gcc-c++, python3-devel, libgomp) required for compiling custom hnswlib. Works on both Rocky Linux (dnf) and Amazon Linux (yum) with automatic package manager detection. This ensures clean production servers get the required build tools automatically.

---

## [8.9.2] - 2026-02-08

### Fixed

- **Auto-updater custom hnswlib build** - v8.9.1 only initialized the git submodule but didn't build and install the custom hnswlib. Added `build_custom_hnswlib()` method that runs `pip install --force-reinstall` from `third_party/hnswlib` to compile and install the custom version with `check_integrity()` method for HNSW index validation.

---

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
