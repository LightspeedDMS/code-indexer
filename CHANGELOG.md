# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v9.19.2

### Bug Fixes

- fix(#749): Fixed `cidx admin groups create` returning "a coroutine was expected, got dict". Root cause: `GroupAPIClient`, `AdminAPIClient`, and `CredentialAPIClient` methods were plain `def` (synchronous) but the CLI calls them via `run_async()` which requires coroutines. Converted 27 affected methods across three client files to `async def`. Also fixed a bare `temp_client.get_user()` call in cli.py that was not wrapped in `run_async()` (exposed by mypy). Added 27 parametrized regression tests using `inspect.iscoroutinefunction()` to enforce the async contract.

## v9.19.1

### Bug Fixes

- fix(#724): Removed the "content unchanged" postcondition from the verification-pass retry loop. Discovered during staging E2E: when Claude verified a domain document and found every claim correct, it printed `FILE_EDIT_COMPLETE` without editing — the content-unchanged check then treated this legitimate success as a retry-triggering failure, leading to spurious `VerificationFailed` on well-formed documents. The remaining 4 postconditions (subprocess timeout / non-zero exit / missing sentinel on last non-empty line / empty file) are sufficient proof Claude completed its work.

## v9.19.0

### Story #724 v2 — Post-Generation Verification Pass: file-edit contract, no fallbacks

**Behavioral change**: verification failure now **propagates** → enclosing refresh job **FAILS**. No silent "fallback to original" passthrough anywhere.

**Nuked from v1** (v9.18.x):
- `VerificationResult` dataclass + `fallback_reason` field
- Evidence filter + `discovery_mode` parameter
- Three safety guards (length ratio, removed ratio, empty counts)
- 30-second delay retry machinery
- Two-layer JSON envelope parser (`--output-format json` flag)
- Three `except Exception` swallowers around `_run_verification_and_log` in `dependency_map_service.py` (Pass 2 loop + delta merge path) and `meta_description_hook.py` (description path)
- All related AC9 structured-log helpers (`_format_journal_summary`, `_build_bounded_diff`, `_AC9_DIFF_MAX_LINES`)

**Added**:
- `class VerificationFailed(RuntimeError)` at top of `dependency_map_analyzer.py`
- `invoke_verification_pass(document_path, repo_list, config) -> None` — simple 2-attempt retry loop with re-seed-from-original-content before each attempt. Any of 5 failure conditions triggers retry; persistent failure raises:
  1. `subprocess.TimeoutExpired`
  2. `subprocess.CalledProcessError` (non-zero exit)
  3. `FILE_EDIT_COMPLETE` sentinel missing from stdout
  4. Temp file mtime unchanged (Claude printed sentinel but didn't edit)
  5. Temp file empty or whitespace-only
- Rewritten `prompts/fact_check.md` — tight imperative: Claude uses Read/Glob/Grep/Edit tools directly on the temp file, prints `FILE_EDIT_COMPLETE` sentinel when done. `_build_file_based_instructions` appends the file-path hint.
- Single WARNING log per failed attempt. No pre-raise ERROR. No structured `extra=` payload. No activity-journal gating.

**Kept unchanged from v1**:
- Shared `threading.Semaphore` (inherited via `_invoke_claude_cli`, no new code)
- `dep_map_fact_check_enabled` and `fact_check_timeout_seconds` config fields (default off / 600s)
- Rolling-upgrade pop-unknown-keys deserializer
- Web UI Config Screen fields
- Scheduler INFO log on refresh collision

**Why**: v1 staging E2E showed every verification call fell into the silent-fallback branch (CLI exit 1 because `--dangerously-skip-permissions` was missing when using `--max-turns`). Even after fixing that, the inner JSON-parse layer failed because Claude wasn't reliably emitting pure JSON in tool-use mode. Codex's original pressure test flagged the design as over-engineered; v2 follows the codebase's existing file-edit idiom (`invoke_delta_merge_file`, `invoke_refinement_file`) instead of inventing a parallel JSON-return-and-parse flow.

**Tests**: removed ~80 v1 fallback/evidence/guard/discovery-mode tests; added 5 retry-loop tests (`test_retry_subprocess_exception_first_fails_second_succeeds`, `test_retry_postcondition_first_fails_second_succeeds`, `test_retry_both_attempts_subprocess_fail_raises`, `test_retry_both_attempts_postcondition_fail_raises`, `test_retry_reseeds_temp_file_between_attempts`). Rolling-upgrade and Web UI config-field tests retained.

## v9.18.2

### Bug Fixes

- fix(#724): Story #724 verification pass Claude CLI invocation was failing with exit code 1 on every call because the command array was missing `--dangerously-skip-permissions` and was using a hardcoded `--max-turns 1` that's too low for real verification. Discovered during staging E2E — every verification invocation exited 1 with empty stderr, causing the fallback path to fire on every call (preserving the original document but producing no verification benefit). Fixed: now uses `--max-turns {dependency_map_delta_max_turns}` (default 30, same as delta merge) AND includes `--dangerously-skip-permissions`. Added three regression tests (`TestVerificationCliArgs`) asserting exact cmd construction — previous tests mocked subprocess.run without inspecting the cmd array and therefore could not catch this class of bug.

## v9.18.1

### Bug Fixes

- fix(#748): Bug #748 (P1) — Delta dep-map analysis crashed immediately on every invocation with `'RefreshScheduler' object has no attribute '_tracking_backend'`. The `_queue_lifecycle_backfill_if_needed` method (Story #728 AC8) and its `_backfill_*` helpers erroneously accessed `self._refresh_scheduler._tracking_backend` when the attribute lives on `DependencyMapService` itself (`self._tracking_backend`). Corrected all affected accesses. Added wiring-level regression test (`test_dependency_map_service_lifecycle_backfill.py`) that constructs the real service and invokes `_queue_lifecycle_backfill_if_needed` without patching it out, proving no `AttributeError` is raised. Unblocks delta-mode dep-map refreshes on all deployments.

## v9.18.0

### Features

- feat: Story #724 — Post-Generation Verification Pass for Dependency Map and Description Generation. Adds an optional second Claude CLI pass that re-reads generated dependency-map domain markdown and repository descriptions against the actual source code, producing a corrected document with per-claim source evidence citations. Defaults to **off** (`dep_map_fact_check_enabled = false`); enable via Admin Web UI Config Screen → Claude Integration. Controlled timeout via `fact_check_timeout_seconds` (default 600, bounds [60, 3600]).
  - Evidence filter: CORRECTED and ADDED items without concrete source evidence (file+line_range OR symbol+definition_location) are discarded; any discard triggers conservative fallback to the original document.
  - `discovery_mode=false` (current pipeline default) unconditionally discards ADDED items.
  - Safety guards: corrected_document under 50% of original length, REMOVED ratio ≥ 50%, or zero classified claims on a non-empty original all trigger fallback.
  - Timeout handling: single retry after a fixed 30-second delay with semaphore released during the wait; double-timeout falls back to original.
  - Shared `threading.Semaphore` caps concurrent Claude subprocess invocations across verification AND existing generation paths using `max_concurrent_claude_cli` from runtime config.
  - Structured `logger.info` payload on every run: `domain_or_repo`, `counts`, `evidence`, `diff_summary` (bounded unified diff), `duration_ms`, `fallback_reason`. Activity journal summary only when session active AND document actually changed.
  - Rolling-upgrade safety: `_dict_to_server_config` now uses `dataclasses.fields()` to pop unknown keys before constructing `ClaudeIntegrationConfig`, preventing startup crash when a new-code node writes a future-version `server_config` blob to the DB and an old-code node reads it during cluster rolling restart.
  - Wired into Pass 2 per-domain, delta merge, description generation (before `atomic_write_description` — no double-lock), and repair Phase 1 (inherits Pass 2 wiring).
  - Scheduler: refresh collisions from verification-extended cycles now log at INFO (not DEBUG) with a contextual message so operators can detect cycle-overrun.
  - Verification prompt externalized to `src/code_indexer/global_repos/prompts/fact_check.md` (Messi Rule #11).
  - 107 new unit tests covering parser, evidence filter, discovery-mode rules, safety guards at 49/50/51% boundaries, timeout + 30s delay, concurrency gate (cap=2 with 3 callers, cold-start compatibility), AC9 log payload shape, AC10 journal gating, rolling-upgrade deserialization, Web UI template fields, and POST validation bounds.

## v9.17.10

### Test Hygiene

- fix: Integration test timing flake in `test_session_refresh_integration_bug726.py` — replaced `ADMIN_TIMEOUT=4s` + `ADMIN_EXPIRY_DELAY=4.5s` (0.5 s margin, inside itsdangerous integer-second rounding boundary) with `ADMIN_TIMEOUT=10s`, `USER_TIMEOUT=20s`, and named sleep constants that each give a full 1+ second margin: `ADMIN_REFRESH_DELAY=6s` (1 s past 5 s threshold), `USER_REFRESH_DELAY=12s` (2 s past 10 s threshold), `ADMIN_EXPIRY_DELAY=11s` (1 s past 10 s timeout). No mocks, no production code changes — real system exercised throughout.

## v9.17.9

### Bug Fixes

- fix: Bug #743 (P1) -- HNSW "ef or M is too small" errors on semantic search across multiple golden repos. Index construction now raises the HNSW parameters past the point at which the library emits the spurious warning/error for corpora above a small threshold, eliminating the noisy log on routine searches.
- fix: Bug #741 (P2) -- Golden repo alias ending in '-global' produced phantom '*-global-global' entries and a refresh storm. Alias validation now rejects (or normalizes) the '-global' suffix at ingestion so the downstream global-name derivation cannot double-suffix.
- fix: Bug #737 (P2) -- Server status reported RAM usage twice (once under top-level system resources, again duplicated under Storage). Health service now emits RAM exactly once.
- fix: Bug #739 (P2) -- `RerankerSinbinnedException` was treated as 'failed' instead of 'skipped'. Sinbin is a normal degraded-mode path, not a failure; reporting it as failed triggered operator alerts for self-healing conditions.
- fix: Bug #740 (P2) -- Reranker Voyage/Cohere 4xx errors logged status code only, discarding the response body. Response body is now captured and logged so 4xx debugging does not require ad-hoc instrumentation.
- fix: Bug #744 (P2) -- Reranker `_attempt_provider_rerank` checked 'down' signal but not 'sin-binned' signal; the two health states were evaluated independently by different callers, causing uncoordinated routing decisions. Both signals are now consulted in the same gate.
- fix: Bug #736 (P3) -- Dependency map refresh jobs missing from the admin Web UI Jobs tab despite appearing on the dashboard 'recent jobs' card. Jobs tab query now includes `dep_map_refresh` operation type for parity with the dashboard.

### Test Hygiene

- `tests/unit/server/repositories/test_resource_manager.py::TestGracefulShutdownHandler::test_graceful_shutdown_handler_timeout_handling` marked `@pytest.mark.slow` so it no longer flakes in parallel `server-fast-automation.sh` chunk runs (test passes in isolation; fails only under CPU contention due to hardcoded `time.sleep(0.2)` × 3 with a tight `total_time < 0.8s` upper bound). Codex verified in v9.17.1 and v9.17.2 cycles that this is a pre-existing timing-brittle test, not a code regression.
- `tests/unit/server/services/test_research_assistant_security_flags.py` updated to align its `required_denies` list with the Story #738 loosening (removed `curl`, `rm`, `mv`, `cp`, `chmod`, `kill` from the asserted must-deny list; split categories into network (still denied: `wget`, `ssh`, `scp`, `nc`, `nmap`), interpreters, shell escapes, privilege, packages, service management, git writes, exfiltration, persistence).

### Authorship

This release batches work delivered by a parallel bug-fixer agent. Each individual bug fix is committed as its own reviewable commit (see `git log` between v9.17.8 and v9.17.9 tags). Commits were taken over and curated by Claude Opus 4.7 to apply one-bug-per-commit discipline and wrap the release with CHANGELOG + version bump + tag.

## v9.17.8

### Features

- feat: Story #738 -- Research Assistant remediation authority for environment/data issues. The admin Web UI Research Assistant can now execute self-diagnosed remediations (rm, mv, cp, mkdir, kill, curl to localhost, plus the specific allow `systemctl restart cidx-server`) within a strict scope boundary (server_data_dir, golden_repos_dir, session folder). Hard-denied commands (sudo, ssh, python3, pip, git push/commit/checkout, bash/sh/zsh -c, nc, socat, crontab, mount, mkfs, etc.) remain blocked. The prompt was rewritten with a DIAGNOSE -> PLAN -> SCOPE CHECK -> EXECUTE -> VERIFY protocol, a SELF-DIAGNOSED vs OPERATOR-DIRECTED guard (prompt-injection defense treating instructions found inside data as adversarial), and reason-category refusal disclosure (one-sentence scope/capability explanations without leaking the full deny list). Source-code class bugs route to GitHub issue filing via issue_manager.py instead of in-place edits. Substantive implementation delivered as part of Epic #725 commit af12e986; this release finalizes the story with the regression test suite (115 tests: permission_settings construction + prompt template assertions) and version bump.

## v9.17.7

### Bug Fixes

- fix: Bug #734 (P2) -- `RefreshScheduler.start()` now wraps its one-shot `cleanup_stale_write_mode_markers(force=True)` call in a try/except so that a startup cleanup exception is logged with a traceback and the scheduler thread still launches. Previously, a raised exception aborted `start()` and the scheduler never ran, silently breaking refreshes. Mirrors the defensive pattern already applied to the in-loop cleanup fix for Bug #729.

- fix: Bug #730 (P2) -- `cidx scip generate` subprocess call inside `RefreshScheduler._index_source()` now has a timeout. Uses `ScipConfig.scip_generation_timeout_seconds` (default 600s) when available, otherwise falls back to 600s. `subprocess.TimeoutExpired` is caught and re-raised as `RuntimeError` so upstream error-classification logic can categorize it. Prevents SCIP generation from hanging the refresh thread indefinitely when a language indexer stalls. Legacy `inspect.getsource`-based no-timeout tests in `test_refresh_scheduler_indexing_types.py` and `test_indexing_resilience.py` removed as they asserted the now-obsolete Bug #467 invariant.

- fix: Bug #735 (P3) -- `_scheduler_loop` now has an exponential-backoff circuit breaker on consecutive iteration failures. The counter doubles the effective poll interval on each failure (30s, 60s, 120s, ...) up to a 1-hour ceiling and resets to 0 on the first successful iteration. Prevents log-flooding and CPU waste when an upstream dependency (corrupt SQLite, unreadable config, permission error) fails every iteration.

Regression tests: `tests/unit/global_repos/test_refresh_scheduler_bugs_734_730_735.py` (3 tests, all pass).

## v9.17.6

### Bug Fixes

- fix: Bug #726 -- Admin Web UI sessions expired after the hard 1-hour limit regardless of user activity because `SessionManager.get_session()` never re-issued the cookie. Added `_should_refresh_session()` helper (fires when elapsed time >= 50% of session lifetime) and `get_and_refresh_session()` method that re-signs and sets a new cookie with a reset `created_at`, preserving CSRF token, httponly, secure, and samesite flags. Also added `session_timeout` field to `SessionData` so the refresh method can re-sign with the original role-specific timeout. Seven regression tests in `tests/unit/server/web/test_session_refresh_bug726.py` verify: refresh fires past 50% threshold, no refresh before threshold, expired sessions return None, CSRF token preserved, security flags correct for localhost and production, and continuous activity keeps admin alive past the hard timeout.

## v9.17.5

### Bug Fixes

- fix: Bug #732 -- CLAUDE.md Section 11 post-E2E log audit referenced wrong database file (`~/.cidx-server/data/cidx_server.db`) and wrong identifiers (`server_logs` table, `logger` column). The actual logs are written to `~/.cidx-server/logs.db` in a table named `logs` with a column named `source`. Both SQLiteLogHandler and LogsSqliteBackend correctly create the `logs` table in `logs.db` at startup; the bug was documentation-only. Corrected the post-E2E audit query in CLAUDE.md Section 11 to use the right file, table, and column names. Regression tests in `tests/unit/server/services/test_sqlite_log_table_creation.py` (17 tests) confirm correct table creation and query patterns.

## v9.17.4

### Bug Fixes

- fix: Bug #731 (strengthened) -- DatabaseConnectionManager._lock changed from threading.Lock to threading.RLock (primary fix). Plain Lock is not re-entrant: if `_cleanup_stale_connections()` logs while holding `_lock`, and SQLiteLogHandler.emit() calls `get_connection()` which tries to re-acquire `_lock` on the same thread, a plain Lock deadlocks. RLock allows same-thread re-acquisition, eliminating the root cause. Validated by `TestDatabaseManagerRLockFix`: asserts isinstance(_lock, RLock) and confirms cleanup-logging path completes within 5s timeout with SQLiteLogHandler at root logger.

- fix: Bug #731 (defence-in-depth) -- SQLiteLogHandler._emit_guard confirmed as per-instance threading.local (not module-level). Instance-owned so multiple SQLiteLogHandler instances installed at the root logger do not share guard state and cannot accidentally suppress each other's legitimate emit() calls. Pattern matches Codex review recommendations.

- fix: Bug #731 sibling risk (Part C, Codex review finding) -- OTELLogHandler gains per-instance threading.local re-entry guard (_emit_guard). get_trace_context() calls logger.debug() on exception; if OTELLogHandler is the root handler that debug call re-enters emit() on the same thread, causing infinite recursion. Guard silently drops recursive calls. Validated by TestOTELLogHandlerReentryGuard: structural check (isinstance _emit_guard threading.local) and no-deadlock check under 5s timeout with patched get_trace_context raising RuntimeError.

## v9.17.3

### Bug Fixes

- fix: Bug #731 -- SQLiteLogHandler recursive emit deadlock. `emit()` calls `DatabaseConnectionManager.get_connection()`, which can invoke `_cleanup_stale_connections()`, which logs via `logger.info()`, which re-enters `emit()` on the same thread. With the root logger as the only handler, the second `emit()` tried to acquire the root-logger lock already held by the first call, causing a deadlock that froze the background logging thread permanently. Fixed by adding a thread-local re-entry guard (`_emit_guard`): if `emit()` is already active on the current thread, the recursive call returns immediately (silent drop). The outer call always completes and the outer record is always persisted. Validated by two regression tests: one asserts no deadlock under a 5-second timeout, one asserts the inner record is silently dropped while the outer record is persisted.

## v9.17.2

### Bug Fixes

- fix: Bug #729 -- UnboundLocalError in RefreshScheduler background thread. `_scheduler_loop` assigned `refresh_interval` inside the `try` block (line 812) but used it outside at line 898 (`_calculate_poll_interval`). If any exception fired before that assignment (e.g., `registry.list_global_repos()` raising), the `except` clause caught the error but execution fell through to the unbound variable, crashing the background thread with `UnboundLocalError: local variable 'refresh_interval' referenced before assignment`. Fixed by initializing `refresh_interval = DEFAULT_REFRESH_INTERVAL` (imported from `shared_operations`) before the `while` loop so the variable is always bound regardless of where the `try` block throws.

## v9.17.1

### Bug Fixes

- fix: ServerResourceConfig regression -- global_repo_refresh jobs (including critical cidx-meta-global) crashed with `AttributeError: 'ServerResourceConfig' object has no attribute 'git_update_index_timeout'`. Story #683 incorrectly removed `git_update_index_timeout` and `git_restore_timeout` from ServerResourceConfig as "dead" code, but `refresh_scheduler._create_snapshot()` actively wired them and operators could tune them via config.json. Restored both attributes as real dataclass fields (default 300s), removed the loader `.pop()` calls that silently stripped operator-tuned values, and re-wired the reads in `_create_snapshot()` using the `cfg = self.resource_config or ServerResourceConfig()` single-source-of-truth pattern. Deleted dead test for truly-removed `cidx_scip_generate_timeout`. Fix validated via codex code review, full unit/regression test suites, and direct Python E2E exercising real subprocess/filesystem/git against the live `_create_snapshot()` method.

### Bugs Filed During This Cycle (Unrelated Follow-ups)

- Bug #729 (P1): UnboundLocalError on `refresh_interval` in RefreshScheduler background thread (refresh_scheduler.py:898)
- Bug #730 (P2): `cidx scip generate` subprocess invoked without timeout in `_index_source()` despite `ScipConfig.scip_generation_timeout_seconds` existing
- Bug #731 (P0): CIDX server logging deadlock -- recursive `SQLiteLogHandler.emit` via `database_manager._cleanup_stale_connections` freezes async jobs, HTTP handlers, and graceful shutdown
- Bug #732 (P0): SQLite `logs` table never created -- breaks mandated post-E2E log audit and amplifies Bug #731

## v9.17.0

### Features

- feat: Epic #714 -- Dependency map file-based output and coverage gaps. Converts all Claude CLI document operations (delta merge, Pass 2, refinement, repair Phase 1) from stdout-based to file-based I/O using dangerously_skip_permissions=True. Eliminates stdout truncation for large domain documents (17K+ chars). New methods: invoke_delta_merge_file(), invoke_refinement_file(), invoke_new_domain_generation(). Old stdout methods and truncation guard constants removed.

- feat: Story #716 -- Delta refresh discovers uncovered repos. Delta refresh now queries DepMapHealthDetector for uncovered_repo anomalies before processing, runs domain discovery for unassigned repos, and includes discovered domains in the affected set. Adds uncovered_repo to REPAIRABLE_ANOMALY_TYPES and repair Phase 0 with discovery_callback.

- feat: Story #717 -- Stale repo cleanup in repair. Adds repair Phase 1.5 that removes stale participating repo references from _domains.json and domain .md files, then regenerates _index.md. Adds stale_participating_repo to REPAIRABLE_ANOMALY_TYPES. Repair phase ordering: 0, 1, 1.5, 2, 3, 3.5, 4, 5.

- feat: Story #719 -- Hide repositories from auto-discovery view. Adds HiddenDiscoveryReposSqliteBackend with add/remove/list/get_hidden_set/is_repo_hidden operations. PostgreSQL migration 018_hidden_discovery_repos.sql. GitLab and GitHub providers accept hidden_identifiers parameter to filter hidden repos. POST /admin/api/discovery/hide and /unhide endpoints with CSRF and admin auth. Show hidden toggle and Hide/Unhide buttons in both GitLab and GitHub discovery partials. partial_due_to_cap banner removed from both partials.

- feat: Bug #723 -- GitLab discovery search_namespaces parameter. Adds search_namespaces=true to GitLab API calls when search is provided, enabling discovery of repos whose namespace path matches the search term.

### Fixes

- fix: Bug #720 -- Refresh fails for aliases containing -global substring. Replaced all 16 occurrences of .replace("-global", "") with .removesuffix("-global") across 6 files. Python's str.replace() removed ALL occurrences of -global; removesuffix() correctly strips only the trailing suffix.

- fix: Bug #722 -- FTS index status incorrectly shown as Not Present. Fixed repository_health.py to check .code-indexer/tantivy_index/ (correct path) instead of .code-indexer/index/tantivy (wrong path).

- fix: Starlette 0.49+ cookie deprecation. Updated admin_session_cookie fixture in 7 web test files to set cookies on the client instance instead of per-request, fixing 401 errors from deprecated per-request cookies= parameter.

- fix: Depmap running state bug tests. Updated TestJobStatusRunningBadgeNotOverriddenByContentHealth tests to work with the refactored cache-based state machine route (Story #684).

## v9.16.0

### Features

- feat: Story #684 -- Dependency Map dashboard background-job progress UI. Adds three HTMX partial templates: depmap_job_status_computing.html polls every 2s and renders a progress bar with percentage and progress_info label; depmap_job_status_complete.html renders cached HTML results with an optional stale-warning banner when a prior run failed; depmap_job_status_error.html shows an error banner with the failure message and a Retry Analysis button that POSTs to the retry endpoint.

- feat: Story #680 -- External dependency latency observability. Adds end-to-end pipeline for tracking, aggregating, and evaluating latency for external HTTP dependencies (VoyageAI, Cohere, Langfuse, etc.). DependencyLatencyBackend persists raw latency samples and per-minute aggregated buckets to SQLite with a 7-day retention window. DependencyLatencyTracker provides thread-safe in-process sample recording with configurable buffer flush. DependencyLatencyAggregator computes p50/p95/p99 percentiles, request rate, and error rate from the stored buckets. DependencyHealthEvaluator applies threshold-based rules to classify dependency status as healthy, degraded, or unhealthy. LatencyTrackingHttpxTransport wraps any httpx.AsyncBaseTransport to record timing and status for every outbound request transparently. Dashboard dependency latency section styled with new CSS classes: .dependency-latency-section, .dependency-row, .dep-name, .dep-metrics, .dep-trend, .dependency-status-healthy, .dependency-status-degraded, .dependency-status-unhealthy. 73 new tests across all five components.

- feat: Story #686 -- Grouped-by-Category view toggle on Groups page repo access table. Handler enriches golden repos with category_id, category_name, category_priority from RepoCategoryService with graceful degradation on failure. Template adds toggle button (Group by Category / Flat View) and data-repo-alias, data-category-name, data-category-priority attributes on repo rows. repo_categories.js extended: _getGroupedStorageKey, toggleGroupedView, applyStoredGroupedView all detect .repo-access-table with dedicated localStorage key cidx-groups-repo-access-grouped. Bootstrap call applyStoredGroupedView() injected at end of partial for HTMX refresh persistence. 15 new tests.

## v9.15.4

### Fixes

- fix: Bug #699 -- `add_golden_repo` and web batch-create silently hardcoded `default_branch="main"` when caller omitted `branch`, passing `--branch main` to `git clone`. Any upstream repo with a non-`main` default branch (e.g., `master`) failed with exit code 128. Fix: all call sites now pass `None` when caller omits branch, and `_clone_remote_repository()` omits `--branch` from the clone command when branch is `None`, letting git resolve the remote's HEAD naturally. Affected sites fixed: MCP `add_golden_repo` handler, web `_batch_create_repos` helper, web single-repo add endpoint, `AddGoldenRepoRequest` model default, and `GoldenRepoManager._clone_remote_repository()`. 7 new tests (2 fast command-construction tests + 5 slow local-bare-repo integration tests).

## v9.15.3

### Fixes

- fix: `git_diff` response metadata `files_changed` counter was always 0 when `stat_only=true`. Root cause: `git_operations_service.git_diff()` computed `files_changed` by counting `"diff --git"` markers in the raw output, but git's `--stat` format emits no such markers -- only a summary line like "N files changed, K insertions(+), L deletions(-)". Fix: when the marker count is zero and output is non-empty, fall back to parsing the stat summary line via regex `r"(\d+) files? changed"`. Unified-diff path is unchanged; only stat/stat_only paths get the new fallback. The actual diff_text content was always correct -- only the counter metadata was wrong, so MCP clients that read `diff_text` saw the right data but clients that read `files_changed` saw 0. 1 new regression test.

## v9.15.2

### Fixes

- fix: Bug #696 -- `git_diff` MCP handler silently discarded every revision-related parameter advertised in its tool schema. `from_revision`, `to_revision`, `path`, `context_lines`, and `stat_only` were accepted by the handler but never forwarded to `git_operations_service.git_diff()`, so every invocation degenerated into a working-tree diff against a clean golden repo and returned empty. Handler now reads all five parameters from `args` and forwards them to the service. `from_revision` is validated as required per the schema. Invalid revisions now produce `success:false` errors via the existing `GitCommandError` branch. 7 new unit tests with real git repos (no MagicMock).

- fix: Bug #697 -- `git_log` MCP handler silently discarded every filter parameter (`path`, `author`, `until`, `branch`) AND suffered from a key-name mismatch where it read `args.get("since_date")` but the schema parameter is named `since`. Every filter was dead across the MCP surface. Handler now reads the correct schema key `since` and forwards it as `since_date=` to the service (kept service parameter name stable to avoid touching other callers). All five missing filter parameters now read from `args` and forwarded. 7 new unit tests including an explicit regression guard (`test_git_log_reads_since_not_since_date`) that locks in the key-name fix.

- fix: Bug #698 -- `git_show_commit` stats always reported `insertions: 0, deletions: 0` for every file because combining `--numstat` and `--name-status` in a single `git show` command causes git to silently suppress numstat output (only name-status is emitted). `_get_commit_stats()` now runs the two git commands separately and merges results by path, matching the two-command pattern already used by `get_diff()`. Also fixed a latent rename-path resolution bug: numstat emits paths like `"old => new"` and `"dir/{old => new}/file.py"` which didn't match the name-status keys. Added `_resolve_numstat_rename_path()` helper that handles both simple and brace forms. 8 new unit tests covering add/modify/delete/rename/binary/multi-file/empty-commit cases.

## v9.15.1

### Fixes

- fix: Bug #685 -- Auto-discovery pagination dead-end. Replace broken page-number pagination with opaque cursor model and filter-fill loop. Fixes sparse pages after filtering already-indexed repos and missing Next button when all repos on a page are indexed. Cursor-based navigation with JS Back button stack. Safety cap of 5 source batches per page. Invalid cursors silently restart from beginning. 45 new tests.

## v9.15.0

### Refactoring

- refactor: Story #496 -- Modularize MCP handlers.py into domain-specific handler modules. Split the monolithic handlers.py (17,180 lines, 255 functions) into 13 domain modules under a handlers/ package: search.py, repos.py, files.py, admin.py, guides.py, scip.py, delegation.py, pull_requests.py, git_read.py, git_write.py, cicd.py, ssh_keys.py. Backward-compatible re-export layer in __init__.py with ForwardingModule for mock-patch transparency. _legacy.py retained for shared path resolution helpers. 5 new forwarding module tests added. Zero behavioral changes -- pure structural decomposition.

## v9.14.11

### Fixes

- fix: Bug #678 -- Add passive sin-bin (circuit breaker) with exponential backoff to ProviderHealthMonitor. Quarantines failing providers for configurable cooldown (30s to 300s cap). Gates all online-query dispatch paths behind sin-bin check. Externalizes all hardcoded numeric tunables (timeouts, thresholds, retry counts) to config. Adds config seeding helper that overlays server provider config onto CLI subprocess config.json before each cidx index launch. Implements cross-process health telemetry bridge (provider_health.jsonl write/drain). Updates reranker client timeouts from 5s to 15s with sin-bin check. Adds RerankerSinbinnedException for graceful degradation. 141 new tests.

- fix: Bug #679 -- Add per-provider exception isolation in CLI temporal indexing loop so one provider failure does not abort others. Introduces COMPLETED_PARTIAL job status for partial-success runs. Writes per-provider provider_results.json with mtime lifecycle guard (prevents reading stale data from prior runs). Exit code semantics: 0=all success, 1=all fail, 2=partial. Adds GET /admin/provider-health endpoint with sin-bin state. Surfaces reranker status in MCP query response metadata. Updates dashboard with provider dots and COMPLETED_PARTIAL badge. 93 new tests.

- fix: Dashboard per-user and chart API metrics now auto-refresh every 2 seconds alongside totals. Previously they only updated on page load or dropdown change, causing stale numbers.

## v9.14.9

### Improvements

- docs: Repositioned reranking as the default workflow in `search_code` and `cidx_quick_reference` tool documentation. Added RERANKING SMELL TEST heuristic (2+ word semantic/hybrid query = add rerank_query), SKIP RERANKING ONLY WHEN guard (identifier lookup / <=3 results / positional order), and STANDARD CALL TEMPLATE with rerank_query and rerank_instruction as visible fill-in fields. Updated canonical example to include reranking. Flipped section framing from "Optional" add-on to "DEFAULT: Use reranking" with concrete cost/benefit (~200-500ms vs 2-4 re-searches). Added matching RERANKING summary line to cidx_quick_reference guide.

## v9.14.8

### Fixes

- fix: Bug #669 — `_query_multi_provider_fusion` crashed with `"Temporal query failed: 2 (of 3) futures unfinished"` when the `as_completed(..., timeout=15)` timer fired. The inner `try/except` was inside the loop body and only caught `.result()` errors on completed futures — the iteration-level `TimeoutError` propagated uncaught. Compounding issue: the exception exited the `with ThreadPoolExecutor` block triggering `shutdown(wait=True)`, blocking until all in-flight VoyageAI/Cohere embedding threads finished. Fix: (1) catch `concurrent.futures.TimeoutError` at the loop level; (2) cancel remaining futures and call `executor.shutdown(wait=False, cancel_futures=True)` to prevent blocking; (3) call `record_temporal_failure` for each timed-out provider so health monitor can gate them; (4) return partial results (or empty with warning) — never raise an exception. 4 new unit tests covering all timeout paths.

## v9.14.7

### Fixes

- fix: Bug #667 Gate 3 — dual-provider parallel strategy bypass of temporal routing. When both VoyageAI and Cohere providers are configured, `_search_single_repository` auto-set `query_strategy = "parallel"` and the parallel block returned early at line ~1354, before reaching the temporal routing gate at line ~1387. Queries with `chunk_type`, `diff_type`, or `author` were silently routed through parallel semantic search instead of temporal execution. Fixed by checking `_has_temporal_for_strategy` before allowing `query_strategy = "parallel"` — if any temporal param is present, falls back to `"primary_only"` which reaches the temporal gate. 4 new unit tests in `TestGate3TemporalRoutingWithDualProviders` covering all three temporal params and the control case.

- fix: Bug #668 — `FilesystemVectorStore.search()` rebuilt HNSW index inline during queries when the index was stale. The watch-mode optimization deferred HNSW rebuilds to "query time" (via `skip_hnsw_rebuild=True`), causing the first temporal query after indexing to block for 16+ seconds while rebuilding the index. This is never acceptable during a query. New policy: if HNSW is stale and the bin file exists, query it as-is with a warning; if stale and missing, return empty results with a warning. Rebuilding the index is the indexer's responsibility, not the query path's. 3 new unit tests in `TestStaleHNSWQueryNeverRebuilds` verifying `rebuild_from_vectors` is never called during search under any staleness condition.

## v9.14.6

### Fixes

- fix: Bug #667 — temporal routing gates incomplete: `chunk_type`, `diff_type`, and `author` parameters did not trigger the temporal query path. When claude.ai calls `search_code` with `chunk_type="commit_diff"` without a `time_range`, the query was silently routed to regular semantic search, returning no temporal metadata. Fixed three routing gates: (1) `_is_temporal_query()` in `handlers/_utils.py` — used by all MCP tools (`search_code`, `regex_search`, `git_search_commits`, `git_search_diffs`); (2) `has_temporal_params` routing condition in `semantic_query_manager.py` main search path; (3) same condition in the no-results warning path. 26 new unit tests covering all three gates. Root cause of previous testing failure: E2E was run through CLI path which has its own `chunk_type` guard, masking the server/MCP routing bug.

## v9.14.5

### Fixes

- fix: Bug #666 — CLI standalone temporal path silently dropped `chunk_type`, `author`, and `diff_types` filters. The `_execute_temporal_fusion` call in `cli.py` only forwarded 7 of 18 available parameters, so `--chunk-type`, `--author`, and `--diff-type` CLI options were accepted by the parser but had zero effect. Fixed by adding `diff_types=list(diff_types) if diff_types else None`, `author=author`, and `chunk_type=chunk_type` to the dispatch call. Discovered during E2E validation of Bug #664/#665. Verified: `--chunk-type commit_message` now returns commit message content, distinct from the unfiltered result set.

## v9.14.4

### Fixes

- fix: Bug #664 — `chunk_type` temporal filter silently dropped. `execute_temporal_query_with_fusion` accepted `chunk_type` from no callers and never forwarded it through the dispatch chain. Added `chunk_type: Optional[str] = None` to `execute_temporal_query_with_fusion`, `_query_single_provider`, and `_query_multi_provider_fusion` in `temporal_fusion_dispatch.py`, and forwarded it to every `service.query_temporal()` call.

- fix: Bug #665 — `author` and `diff_type` temporal filters silently dropped in server/MCP path. `_execute_temporal_query` in `semantic_query_manager.py` was missing `diff_type`, `author`, and `chunk_type` from its signature and never passed them to `execute_temporal_query_with_fusion`. Fixed by adding all three parameters, converting `diff_type` (string or list) to a normalized `diff_types` list, and forwarding all three to the fusion dispatch call. Also wired these parameters from the outer search method call site.

## v9.14.3

### Fixes

- fix: Temporal CLI anti-fallback (second path) — discovered during E2E variation testing that `cli.py` had a second fallback at the `_has_temporal` check (line ~5190). When no temporal collection directory existed at all, the CLI printed "Falling back to space-only search (current code state only)" and set `time_range = None`, causing a full regular semantic search to execute silently. Fixed by replacing `time_range = None` fall-through with an early `return` after printing the honest "No results returned" message. Added test `test_cli_no_temporal_index_returns_immediately` that verifies `execute_temporal_query_with_fusion` is never called when the temporal index is absent.

## v9.14.2

### Fixes

- fix: Temporal index anti-fallback — removed dishonest "Showing results from current code only" fallback messaging when a temporal index is unavailable in the server/MCP path (`semantic_query_manager.py`) and in the CLI warning display when `execute_temporal_query_with_fusion` returns empty results with a warning. The corrected behavior returns an empty result set and an honest message stating "No results returned. Build the temporal index with 'cidx index --index-commits' to enable time-range queries." Rule: graceful failure over forced success (Messi Rule #2).

- fix: Temporal query `temporal_context` field mapping — `_execute_temporal_query` was reading legacy fields (`first_seen`, `last_seen`, `appearance_count`, `commits`) from `TemporalSearchResult.temporal_context` that no longer exist in the current diff-based implementation, causing all temporal_context fields to be `None` in query responses. Fixed by mapping the actual current fields: `commit_hash`, `commit_date`, `commit_message`, `author_name`, `commit_timestamp`, `diff_type`. Verified end-to-end with a real git repo, temporal index, and CLI query confirming all fields populated with real data.

## v9.14.1

### Fixes

- fix: Bug #663 — Race condition in `FilesystemVectorStore.upsert_points` orphan deletion silently dropped shared content chunks from the HNSW index. When two source files contained identical chunks (same `point_id`), one file's orphan cleanup would unconditionally delete the `_id_index` entry and vector file even when another file still referenced the same point. Fix: STEP 1 now removes the file's path mapping first, then calls `PathIndex.has_other_owner(point_id)` to skip deletion scheduling for any point still owned by another file. STEP 3 retains a path-equality guard as defense-in-depth for the concurrent-write race window. Symptom was `WARNING:code_indexer.storage.filesystem_vector_store:Vector file not found for point 'xxxx', skipping` on large codebases with code duplication, resulting in those chunks being unsearchable.

## v9.14.0

### Features

- feat: Epic #649 — Cross-encoder reranking integration for all four MCP search tools (`search_code`, `regex_search`, `git_search_commits`, `git_search_diffs`). Callers may pass `rerank_query` and optional `rerank_instruction` to trigger post-retrieval reranking. Provider chain: Voyage `rerank-2.5` (primary) → Cohere `rerank-v3.5` (fallback). Reranker fires after dual-provider RRF coalescing and before truncation, using 5x overfetch to ensure quality candidates. New `VoyageRerankerClient` and `CohereRerankerClient` in `server/clients/reranker_clients.py`; reranking pipeline in `server/mcp/reranking.py`. Config managed via Web UI (reranking section in config screen). Response `query_metadata` includes `reranker_used`, `reranker_provider`, and `rerank_time_ms` fields. 182 unit tests, 15 E2E test calls (12 Voyage + 3 Cohere failover) all passing.

## v9.13.8

### Refactoring

- refactor: Story #496 Phase 1+2 — split monolithic `handlers.py` (17,243 lines) into a `handlers/` package. `handlers/_legacy.py` preserves the full original implementation (git history intact). `handlers/_utils.py` extracts 30 shared utility functions (931 lines). `handlers/ssh_keys.py` extracts the SSH key domain (286 lines). `handlers/__init__.py` provides a fully backward-compatible namespace with `_ForwardingModule` that transparently forwards mock patches to `_legacy.py`, keeping all 1,500+ existing tests passing without modification. Remaining 11 domain modules to be extracted in follow-up commits (Story #496 in progress).

## v9.13.7

### Fixes

- fix: Bug #647 — `_index_exists` temporal stub always returned `False`, causing `get_golden_repo_indexes` to report `temporal.exists: false` even when a temporal collection was present on disk. AI agents seeing `exists: false` would trigger `add_golden_repo_index` which ran `--clear`, destroying hours of indexing work. Fixed by replacing the stub with a real filesystem scan using `get_temporal_collections()`.
- fix: Bug #646 — `BackgroundJobManager._complete_job` unconditionally set job status to `COMPLETED` regardless of whether the job function returned `{"success": False}`. Jobs that failed (e.g. provider indexing CLI failure with `IndexingSubprocessError`) were silently marked as completed in the UI and database. Fixed by inspecting the result dict and setting `JobStatus.FAILED` when `result.get("success") is False`. Also fixed a memory leak: in-memory job dict was not cleaned up for `FAILED` jobs.
- fix: Bug #648 — multi-provider temporal and semantic index adds each submitted N independent background jobs (one per provider), causing concurrent HNSW and SQLite temporal metadata races that could corrupt the index with `FileNotFoundError` on atomic rename. Fixed in `inline_admin_ops.py`: all providers are appended to config first, then a single job is submitted that runs the CLI once in sequence. Also fixed Bug #1 (enable_temporal flag not persisted via provider path) and Bug #6 (snapshot ValueError not cleaned up on concurrent runs).

## v9.13.6

### Fixes

- fix: Bug #645 — regression in v9.13.3: `_make_offset_callback` wrapper dropped the `path` positional argument, breaking all server-triggered temporal indexing. The inner `_cb` function was defined as `(current, total, **kwargs)` but `temporal_indexer.py` calls `progress_callback(current, total, path, ...)` with 3 positional args, causing `TypeError: _cb() takes 2 positional arguments but 3 were given` on every temporal index job. Fixed by adding `path: Any = None` as 3rd positional param and forwarding it through both branches of `_cb`. Added `TestPathArgIsPreserved` test class that reproduces the exact 3-positional-arg crash scenario.

## v9.13.4

### Fixes

- fix: Bug #644 — defensive k-cap in HNSWIndexManager.query() prevents hnswlib crash when id_mapping metadata diverges from the binary index. Added `index.get_current_count()` as a third bound in the k_actual calculation (`min(k, queryable_count, index.get_current_count())`), so hnswlib never receives k > the number of vectors it actually contains. Previously, a transient mismatch between collection_meta.json id_mapping and the hnsw_index.bin (e.g. during a CoW snapshot creation) caused hnswlib to throw "Cannot return the results in a contiguous 2D array. Probably ef or M is too small", which propagated as MCP-GENERAL-170 errors and logged "Parallel query provider 'voyage-ai' failed" warnings.

## v9.13.3

### Fixes

- fix: Bug #642 — temporal indexing max_commits and since_date parameters are now persisted to temporal_meta.json. Previously, running cidx --index-commits --max-commits N would apply the limit during initial indexing but the value was never written to the metadata file, so background refresh jobs had no way to re-apply the original limit and would silently re-index the full commit history on every refresh.
- fix: Bug #643 — dual-provider temporal indexing no longer freezes job progress at 99%. When two embedding providers run sequential temporal indexing, the second provider's progress_callback emitted backward progress (resetting to 0 after provider 1 completed), which the server-side ProgressPhaseAllocator clamped, freezing display at ~99% for the entire second provider's run (up to 10+ hours on large repos). Fixed by wrapping each provider's callback with _make_offset_callback in cli.py to normalize per-provider 0-to-N progress into a globally monotonic range across all N providers.

## v9.13.2

### Refactoring

- refactor: Dead code removal — issues #608-#612. Removed ~22,000 lines of orphaned production code with zero production callers, confirmed via SCIP reference analysis. Deleted packages: server/validation/ (~3,899 lines), business_logic/ (~460 lines), server/feedback/, server/cli/. Deleted dead services: ra_session_router, job_phase_detector, claude_code_response, search_limits_validator, search_result_file_manager (~672 lines). Deleted orphan modules: description_generator, test_reorganizer, hnswlib_verification (~700 lines). Fixed broken MetaDirectoryInitializer import (crash bug on cidx global init-meta). Removed deprecated setup_global_registry CLI command. Cleaned status_display.py dead helper functions. Removed server/scripts/__init__.py empty init.
- style: Switch fast-automation.sh formatting check from black to ruff format to align with pre-commit hook and server-fast-automation.sh.

## v9.13.0

### Features

- feat: Multi-Provider Temporal Index Parity (Epic #627) -- Provider-aware temporal collection naming, legacy migration, dual-provider temporal indexing (VoyageAI + Cohere), RRF fusion for temporal queries, circuit breaker failover, per-provider progress metadata with format v2, force re-index clears all provider collections, CLI/status/MCP integration with provider attribution display, Web UI temporal indexing with provider selection. 11 stories, ~200 new tests, 5 new modules.

## v9.12.1

### Fixes

- fix: GitHub token no longer deleted on server restart (Bug #639). Root cause: CITokenManager instantiated with different encryption keys across 5 code sites (Web UI used hostname key, lifespan used jwt_secret key). Created single `create_token_manager()` factory used by all sites. Removed destructive `delete_token` on decrypt failure — tokens preserved in DB for recovery.

## v9.12.0

### Features

- feat: Cohere multimodal embedding support (Story #637) -- CohereMultimodalClient embeds text+image chunks via Cohere embed-v4.0 `inputs` parameter into separate `embed-v4.0-multimodal` collection. Provider-agnostic routing in high_throughput_processor detects Cohere vs VoyageAI. Query service detects multimodal collection on disk and creates matching provider. Shared multimodal_utils.py eliminates image encoding duplication. Dynamic semantic index detection in server endpoints replaces hard-coded `voyage-code-3`. Dual-constraint batch splitting (128K tokens + 96 images). 5MB per-image size enforcement. Configurable output dimensions via CohereConfig.

### Fixes

- fix: Server endpoint index status badges now detect Cohere collections (not just VoyageAI) via dynamic collection scanning in repository_health.py and activated_repos.py.
- fix: HNSW finalization for multimodal collections in all 5 finalization sites (4 in smart_indexer.py, 1 in high_throughput_processor.py) now iterates over both VoyageAI and Cohere multimodal models.
- fix: fast-automation.sh added --timeout=3 per-test guard and excluded 15 tests with heavy fixtures (DB init, tantivy, crypto) that don't belong in the fast suite. Prevents indefinite hangs.
- fix: server-fast-automation.sh reduced per-test timeout from 30s to 3s.

## v9.11.0

### Features

- feat: Dual-provider fusion quality improvements (Story #638) -- over-fetch dispatch (2x multiplier, cap 40) provides larger candidate pool for fusion; symmetric score-gated filtering removes low-confidence results from weaker provider (ratio 0.80, floor 0.70); global normalization for multiply/average fusion preserves cross-provider calibration gap; parallel timeout increased from 15s to 20s. RRF unchanged, single-provider fallback unaffected.

## v9.10.0

### Features

- feat: Multi-line regex search (Story #621) -- added multiline and pcre2 parameters to regex_search MCP tool and RegexSearchService. Patterns can span multiple lines using ripgrep --multiline --multiline-dotall flags. PCRE2 engine enables lookahead/lookbehind with --pcre2 flag. Python re.DOTALL fallback when ripgrep unavailable. PCRE2 availability detection with caching. Fully backward-compatible (defaults: multiline=false, pcre2=false).
- feat: Repo category regex match against URL (Story #622) -- auto_assign() now matches category patterns against both repository alias (re.match) and repository URL (re.search). bulk_re_evaluate passes repo_url. Web UI hint text and MCP docs updated. Also fixed pre-existing repo_backend unbound variable bug in bulk_re_evaluate.

### Bug Fixes

- fix: OAuth /authorize accepts PKCE requests without state parameter (Bug #624) -- state made Optional[str] on all OAuth endpoints (GET/POST authorize, consent, MFA verify, OIDC callback). Redirect URLs conditionally include state only when provided. Consent template conditionally renders state hidden field.
- fix: Remove dead ApiKeySyncService systemd env file writes (Bug #626) -- added 10 regression tests to lock in prior removal of _update_systemd_env_file(), APP-GENERAL-043/044 error codes, and /etc/cidx-server/env references.

## v9.9.5

### Documentation

- docs: Improve search_code MCP tool documentation -- score_fusion now explains RRF as rank-position-based (immune to score-scale differences between providers) vs multiply/average as raw arithmetic; query_strategy now documents contributing_providers and fusion_score output fields; failover clarified as API error or timeout (not empty results)

## v9.9.4

### Bug Fixes

- fix: Replace metadata-voyage-ai.json symlink with independent copy (Bug #625 Fix 4/M1) -- symlink caused Cohere's clear() to wipe VoyageAI watermarks through shared file; shutil.copy2 creates true independent copy; existing symlinks on servers need manual conversion

## v9.9.3

### Bug Fixes

- fix: Wire _append_provider_to_config into inline_admin_ops.py Web UI path (Bug #625 Fix 3/M1) -- the Web UI "Add Provider Index" button routes through inline_admin_ops.py which was missing the config write, causing the provider to never appear in embedding_providers

## v9.9.2

### Bug Fixes

- fix: Wire _append_provider_to_config into REST provider_indexes.py paths (Bug #625 Fix 2/M1) -- _submit_index_job and bulk_add were missing the base clone resolution and config write, resulting in 3-second jobs with no actual index

## v9.9.1

### Bug Fixes

- fix: Multi-provider indexing broken -- 14 bugs fixed (Bug #625). Write operations (bulk_add, manage add/remove, REST add/remove) now use base clone path instead of immutable versioned snapshot. Per-provider metadata files (metadata-{provider}.json) prevent cross-provider watermark contamination. _remove_provider_from_config cleans up embedding_providers on remove. All write paths hard-fail when base clone unresolvable (anti-fallback).

## v9.9.0

### Features

- feat: Auto-parallel fusion default -- when both VoyageAI and Cohere embedding providers are configured, the default query strategy is automatically "parallel" with "rrf" fusion; pass query_strategy="primary_only" to override (Story #618, resolves #616 and #617)
- feat: Fusion score and provider provenance transparency -- fused search results now include "fusion_score" (the RRF/multiply/average rank score used for ordering) and "contributing_providers" (list of which providers found each result) fields; search_code MCP tool doc updated (Story #618)
- feat: Embedding provider circuit breaker -- health-gated parallel dispatch skips "down" providers to avoid 30s+ latency penalties; as_completed() timeout guard (15s) cancels hanging providers; background recovery probe restores providers to rotation; connect/read timeout split for httpx; embedding dimension and NaN/Inf validation; failover strategy wired (was a no-op) (Story #619)
- feat: Multi-provider awareness -- automatic index build and refresh for all configured providers; config.json gains "embedding_providers" list; cidx index loops all providers without config mutation; bulk_add_provider_index permanently writes provider to config; backward compatible with single-provider repos (Story #620)

## v9.8.13

### Bug Fixes

- fix: remove hardcoded 1-hour timeout from _provider_index_job; the previous _PROVIDER_INDEX_TIMEOUT_SECONDS=3600 constant (added unrequested by a code reviewer in Story #490) sent SIGKILL to the cidx index subprocess after 3600s, silently failing provider indexing on large production repos that take several hours; timeout=None is now passed so the job runs to completion

## v9.8.12

### Bug Fixes

- fix: lower default min_score from 0.5 to 0.3 in MCP search_code handler; Cohere embed-v4.0 produces cosine similarity scores in the ~0.42-0.48 range so the previous 0.5 default silently eliminated all Cohere results when callers omitted min_score; VoyageAI results (0.5-0.8 range) are unaffected

## v9.8.11

### Bug Fixes

- fix: parallel query strategy (query_strategy="parallel") now actually queries both voyage-ai and cohere providers concurrently via ThreadPoolExecutor; previously the implementation was never committed so staging returned primary_only results with source_provider="primary" on every result (Bug #614)
- fix: min_score is now applied after fusion in parallel query mode instead of per-provider before fusion; previously Cohere results (scores ~0.42-0.48) were silently eliminated when min_score=0.5 (the default), making parallel mode appear to return only VoyageAI results (Bug #615)
- fix: _provider_index_job now uses run_with_popen_progress (Popen-based) with --progress-json flag, enabling real-time progress forwarding to BackgroundJobManager progress_callback; previously used subprocess.run which blocked until completion with no intermediate progress (Story #613)

## v9.8.10

### Bug Fixes

- fix: provider index job for versioned golden repos (e.g. claude-server-global) now indexes the base clone instead of the versioned snapshot; the versioned snapshot is immutable per architecture, so indexing it caused the cohere index to be invisible to the health check and lost on next refresh; after indexing the base clone, a new versioned snapshot is created and the alias is swapped so queries reflect the new provider index immediately (Bug #604)

## v9.8.9

### Bug Fixes

- fix: preferred_provider parameter now correctly routes search to the named provider's collection even without query_strategy="specific"; previously preferred_provider was silently ignored unless query_strategy="specific" was also set

## v9.8.8

### Bug Fixes

- fix: Cohere embed-v4.0 now uses 4096-char chunks (matching voyage-ai) instead of falling back to 1000-char default in FixedSizeChunker

### Removed

- Deleted dead code: `TextChunker._get_language_splitters()` and `TextChunker._smart_split()` — language-aware splitter never used in the indexing pipeline

## v9.8.7

### Bug Fixes

- fix: Bug #608 ext -- BackendFactory.create() crashed with AttributeError on ServerConfig (no .vector_store); added hasattr guard defaulting to FilesystemBackend (only supported backend)

## v9.8.6

### Bug Fixes

- fix: Bug #608 -- EmbeddingProviderFactory.create() and get_provider_model_info() crashed with AttributeError on ServerConfig (no .voyage_ai/.cohere nested objects); added hasattr guards falling back to VoyageAIConfig()/CohereConfig() defaults

## v9.8.5

### Bug Fixes

- fix: Bug #607 -- _provider_index_job and cli_provider_index add/recreate commands used non-existent --provider flag on cidx index; fixed to temporarily set embedding_provider in .code-indexer/config.json and inject API key as env var, then restore original config in finally block

## v9.8.4

### Bug Fixes

- fix: Bug #604 ext -- get_configured_providers now checks voyageai_api_key and cohere_api_key ServerConfig DB fields so API keys stored via Web UI Config screen are recognized without env vars

## v9.8.3

### Bug Fixes

- fix: Bug #606 -- Provider checkboxes in Add/Rebuild Index form rendered as two blue vertical lines instead of native checkboxes; added .provider-list input[type="checkbox"] CSS rules with same Pico CSS override as .index-checkboxes

## v9.8.2

### Bug Fixes

- fix: Bug #605 -- _resolve_golden_repo_path now tries alias + "-global" suffix when base alias not found, fixing 404/500 on provider index add/status when Web UI passes base alias (e.g. "click" instead of "click-global")
- fix: Bug #605 -- add_golden_repo_index() no longer swallows HTTPException(404) as HTTP 500; added explicit except HTTPException: raise before catch-all

## v9.8.1

### Bug Fixes

- fix: Bug #604 -- Guard config.cohere attribute access in EmbeddingProviderFactory.get_configured_providers() for ServerConfig objects; falls back to CO_API_KEY env var check, fixing HTTP 500 on GET /api/admin/provider-indexes/providers (Web UI "Load providers" 500 when Semantic checkbox checked in Add/Rebuild Index form)

## v9.8.0

### Features

**Epic #485 -- Multi-Provider Embedding Support**

**Cohere Embedding Provider (Stories #486, #487)**
- feat: CohereEmbeddingProvider full implementation -- embedded tokenizer (no API client library dependency), retry logic with exponential backoff, 2048-dim vectors (Story #486)
- feat: EmbeddingProviderFactory for provider selection and lifecycle management (Story #486)
- feat: DualVectorCalculationManager for multi-provider parallel indexing -- both providers indexed simultaneously per file (Story #487)
- feat: Per-provider metadata on indexed chunks and correct collection name resolution per provider (Story #487)
- feat: Cohere API key management service layer with encrypted storage and config service integration

**Provider Health Monitor (Story #491)**
- feat: ProviderHealthMonitor singleton tracking per-provider success/failure rates, latency, and uptime (Phase 1)
- feat: Health monitor configuration and integration with server config service (Phase 2)
- feat: CLI command `cidx provider-health` for live provider health status (Phase 2)
- feat: MCP tool `get_provider_health` for health status in agentic workflows (Phase 3)
- feat: REST endpoint `GET /admin/provider-health` for programmatic health queries (Phase 4)
- feat: Auto-routing integration -- queries and indexing operations automatically routed away from unhealthy providers (Phase 5)

**Provider Index Management (Story #490)**
- feat: ProviderIndexService for per-provider index operations (list, build, delete) (Phase 1)
- feat: MCP tools `manage_provider_indexes` and `bulk_add_provider_index` (Phase 2)
- feat: REST endpoints `GET/POST/DELETE /admin/repos/{alias}/provider-indexes` (Phase 3)
- feat: CLI command group `cidx provider-index list/build/delete` (Phase 4)
- feat: Web UI provider checkboxes on Add/Rebuild Index form (Story #489)

**Multi-Provider Query Strategies (Story #488)**
- feat: `query_strategy` parameter with four modes: `primary_only` (default), `failover` (primary then secondary on failure), `parallel` (both providers, fused results), `specific` (explicit provider selection) (Phase 1)
- feat: `score_fusion` parameter: `rrf` (Reciprocal Rank Fusion), `multiply`, `average` (Phase 1)
- feat: `preferred_provider` parameter for `specific` strategy (Phase 1)
- feat: `source_provider` field annotated on all query results for provenance tracking (Phase 1)
- feat: CLI flags `--strategy` and `--score-fusion` wired through full query path (Phases 2+3)
- feat: `query_strategy`, `score_fusion`, and `preferred_provider` exposed on MCP and REST API (Phase 4)

**Multi-Provider Query Controls in Web UI (Story #593)**
- feat: `query_strategy` selector in Web UI search interface
- feat: `score_fusion` and `preferred_provider` controls in Web UI
- feat: `source_provider` badge displayed on search results

**Langfuse README Generator (Story #592)**
- feat: Auto-generates root README.md and per-session README.md files for Langfuse trace repos on every sync
- feat: Atomic file writes with fingerprint-based skip logic to avoid redundant regeneration
- feat: Session tables with timestamps and prompt snippets for navigability

**API Key Management Infrastructure**
- feat: VoyageAI and Cohere API key management service with encrypted storage
- feat: REST endpoints for per-provider API key create, update, delete, and status

### Fixes

- fix: Bug #595 -- Cohere error handling improvements (structured error parsing, user-facing messages)
- fix: Bug #596 -- httpx context manager correctness in Cohere client (resource leak prevention)
- fix: Bug #600 -- ProviderHealthMonitor instrumentation wired into VoyageAI and Cohere clients
- fix: Bug #601 -- Cohere None value validation in embeddings response + DEFAULT_COLLECTION_NAME resolution
- fix: Cap Cohere retry delays at 300s maximum and honour `exponential_backoff` flag
- fix: Guard VoyageMultimodalClient creation behind VOYAGE_API_KEY environment variable check
- fix: config_service PostgreSQL dict_row access pattern correction

## v9.7.0

### Features

**Epic #556 -- Admin Account Security Hardening**
- feat: TOTP MFA core engine with QR code generation, recovery codes, and encrypted secret storage (Stories #558/#559)
- feat: MFA login enforcement for Web UI and REST API (Stories #560/#561)
- feat: MFA enforcement for SSO/OIDC login flow (Story #562)
- feat: User-facing MFA setup routes at /user/mfa/ (setup, verify, disable, recovery codes)
- feat: Login rate limiting with automatic account lockout (Story #557)
- feat: Non-SSO admin REST/MCP access restriction (Story #563)
- feat: Configurable admin session timeout (Story #564)
- feat: Password expiry enforcement for non-SSO accounts (Story #565)
- feat: Emergency MFA recovery CLI commands (Story #571)

**Story #462 -- Collaborative and Competitive Delegation Modes**
- feat: Collaborative mode (DAG-based orchestrated jobs) via POST /jobs/orchestrated
- feat: Competitive mode (decompose-compete-judge pipeline) via POST /jobs/competitive
- feat: DAG validation with cycle detection, terminal node check, max 10 steps
- feat: Competitive validation (engines, distribution strategy, approach count, decomposer/judge config)

**Story #568 -- MCP Acting Users**
- feat: Optional acting_users parameter on MCP tool calls for scoped repository access
- feat: Email-to-user-to-group-to-repo resolution with intersection (never elevates)
- feat: Admin-only enforcement (non-admin silently ignores), input type validation

**Story #578 -- Centralized Runtime Configuration**
- feat: config.json split into bootstrap (file) and runtime (database)
- feat: Unified model -- SQLite for solo mode, PostgreSQL for cluster mode, same code path
- feat: Auto-migration on first boot (seeds DB from config.json, strips file, creates backup)
- feat: 30-second version polling for cross-node config propagation
- feat: Config change callbacks for service notification (ApiKeySyncService)
- feat: Migration scripts: cluster-config-migrate.sh, verify_config_migration.py

**Story #588 -- HNSW Max Elements Configurable**
- feat: hnsw_max_elements added to ServerResourceConfig (default 1,000,000)
- feat: Configurable via Web UI config screen under "Timeouts and Indexing Limit Settings"

### Fixes (Cluster Compatibility -- Epic #408 Audit)
- fix: Bug #572 -- Delta analysis runs now record metrics in dependency_map_run_history
- fix: Bug #573/#574 -- All 4 rate limiters (password change, refresh token, OAuth token, OAuth register) cluster-aware with PG tables
- fix: Bug #575 -- PasswordChangeSessionManager uses DB backend (SQLite/PG) instead of JSON file
- fix: Bug #576 -- OIDC StateManager thread safety (threading.Lock) + PG-backed state tokens
- fix: Bug #577 -- DelegationJobTracker uses delegation_job_results DB table for cross-node results
- fix: Bug #579 -- RefreshTokenManager uses PG advisory locks for cross-node token rotation safety
- fix: Bug #580 -- JobReconciliationService and SelfMonitoringService gated behind leader election
- fix: Bug #581 -- SSHKeySyncService started during cluster startup (was never started)
- fix: Bug #582 -- DistributedJobWorkerService polls for reclaimed jobs and re-executes them
- fix: Bug #583 -- Token blacklist uses DB (SQLite/PG) for cluster-wide JWT revocation
- fix: Bug #584 -- Background job cancellation synchronized across nodes via DB polling
- fix: Bug #585 -- SelfMonitoringService receives PG backend in cluster mode
- fix: Bug #586 -- ApiKeySyncService syncs keys to local files on cross-node config change
- fix: Bug #587 -- Activated repos metadata stored in PG for cross-node visibility
- fix: All ServerConfigManager().load_config() bypasses replaced with get_config_service()
- fix: OIDC init reads from ConfigService (merged config) not bootstrap-only file
- fix: JobTracker save_job uses INSERT OR IGNORE to prevent UNIQUE constraint violation

### Migrations
- 010_server_config.sql -- runtime configuration centralization table
- 011_activated_repos.sql -- activated repo metadata for cluster mode
- 012_fix_activated_repos_ssh_key.sql -- ssh_key_used column type BOOLEAN to TEXT
- 013_rate_limiting_tables.sql -- generic rate_limit_failures and rate_limit_lockouts tables
- 014_oidc_state_tokens.sql -- OIDC CSRF state tokens for cluster mode
- 015_token_blacklist.sql -- JWT token blacklist for cluster-wide revocation
- 016_delegation_job_results.sql -- delegation job results for cross-node tracking

### Scripts
- scripts/cluster-config-migrate.sh -- idempotent config migration for existing clusters
- scripts/config_migration_helper.py -- Python helper for config split and PG insertion
- scripts/verify_config_migration.py -- verify migration accuracy (backup vs DB+file)

## v9.6.3

### Fixes (Epic #556 -- Admin Account Security Hardening, Cluster Compatibility)
- fix: TOTPService cluster compatibility -- shared MFA encryption key via cluster_secrets table, dual-mode PG/SQLite for all MFA data operations
- fix: MfaChallengeManager cluster compatibility -- challenge tokens stored in PostgreSQL mfa_challenges table for cross-node verification
- fix: LoginRateLimiter cluster compatibility -- failure tracking and lockouts in PostgreSQL login_failures/login_lockouts tables
- fix: Wire cluster connection pools for all MFA/security services in lifespan.py
- fix: Remove duplicate LoginRateLimiter instance in web routes (was bypassing cluster pool wiring)
- fix: PostgreSQL boolean type mismatch in TOTP MFA queries (FALSE/TRUE instead of 0/1)
- fix: psycopg3 dict_row for MFA PG queries (tuple vs dict row access)
- fix: Mock is_password_expired in login lockout/rate-limit tests (Story #565 regression)

### Migrations
- 006_mfa_tables.sql -- user_mfa, user_recovery_codes tables
- 008_mfa_challenges.sql -- mfa_challenges table
- 009_login_rate_limiting.sql -- login_failures, login_lockouts tables

## v9.5.37

### Features (Epic #408 -- CIDX Server Clusterization, Junction 1)
- feat: Protocol interfaces for all 14 storage backends (Story #410)
- feat: Database migration system with numbered SQL files and 001_initial_schema.sql (Story #416)
- feat: PostgreSQL backend for Users and Sessions with connection pool (Story #411)
- feat: PostgreSQL backend for GlobalRepos and GoldenRepoMetadata (3-table) (Story #412)
- feat: PostgreSQL backend for BackgroundJobs and SyncJobs (Story #413)
- feat: PostgreSQL backend for 6 remaining backends (CI tokens, SSH keys, git credentials, etc.) (Story #414)
- feat: PostgreSQL backend for Groups and AuditLog, merged from separate groups.db (Story #415)
- feat: StorageFactory with BackendRegistry for config-driven SQLite vs PostgreSQL selection (Story #417)

All new files, zero modifications to existing code. SQLite standalone mode unchanged.
PostgreSQL backends only loaded when storage_mode="postgres" in config.json.

## v9.5.36

### Bug Fixes
- fix: monotonic progress guard prevents progress bar dips during clear+index phases
- fix: progress, current_phase, phase_detail persisted to SQLite — completed jobs now show 100% instead of 0%
- fix: SQLite migration adds current_phase/phase_detail columns to background_jobs table

## v9.5.35

### Features
- feat: extend real-time progress reporting to all user-facing indexing paths (Story #482)
  - PATH A (golden repo registration): progress during post-clone indexing
  - PATH C (refresh scheduler): progress during _index_source semantic/temporal
  - PATH D (change branch): coarse progress markers for 4-step workflow
  - PATH E (activated repo reindex): ProgressPhaseAllocator replaces hardcoded 10/50/90%
  - Shared utility: extracted run_with_popen_progress and gather_repo_metrics to progress_subprocess_runner.py
  - IndexingSubprocessError eliminates circular import between progress utility and golden_repo_manager

### Bug Fixes
- fix: progress regression 1% to 0% during composite rebuild — moved hardcoded progress_callback(25) to else block (Bug #483)
- fix: temporal indexer sqlite3 database is locked — enabled WAL mode, busy_timeout, and retry with exponential backoff (Bug #484)

## v9.5.34

### Features
- feat: real-time progress reporting for index rebuild jobs (Story #480)
  - ProgressPhaseAllocator with dynamic weights from repo metrics (file count, commit count)
  - --progress-json CLI flag for machine-parseable progress output
  - subprocess.Popen + line reader for semantic/temporal (replaces blocking subprocess.run)
  - Job status endpoint includes current_phase and phase_detail fields
  - Coarse start/end markers for FTS and SCIP phases
  - Progress advances incrementally instead of 25% to 100% jump

### Bug Fixes
- fix: stderr pipe deadlock prevention with background drain thread (Story #480)
- fix: console output redirected to stderr in --progress-json mode (Story #480)
- fix: no-op callback attributes attached in --progress-json mode to prevent AttributeError (Story #480)
- fix: flaky debouncer test timing with thread join before assertion

## v9.5.33

### Bug Fixes
- fix: temporal index rebuild now uses --clear for full rebuild instead of incremental (Story #478)
- fix: removed hardcoded max_commits=1000 default -- no cap when not configured (Story #478)
- fix: --since flag corrected to --since-date in temporal rebuild command (Story #478)
- fix: diff_context=0 no longer silently dropped by RefreshScheduler (Story #478)
- fix: since_date validated as YYYY-MM-DD format before saving (Story #478)
- fix: max_commits validated as positive integer server-side (Story #478)

### Features
- feat: temporal indexing options (max_commits, diff_context, since_date, all_branches) configurable per golden repo via Web UI (Story #478)
- feat: added all_branches field to TemporalIndexOptions model (Story #478)
- feat: RefreshScheduler reads and applies stored temporal_options including all_branches (Story #478)
- feat: API endpoint POST /admin/golden-repos/{alias}/temporal-options for saving temporal options (Story #478)

## v9.5.32

### Refactoring
- refac: modularize inline_routes.py (5,264 lines) into 9 domain-specific modules (Story #409)
  - inline_auth.py (8 routes: login, register, reset, refresh, api keys)
  - inline_mcp_creds.py (7 routes: MCP credential CRUD and admin ops)
  - inline_admin_users.py (6 routes: user CRUD and password management)
  - inline_admin_ops.py (12 routes: golden repos, scip admin, job admin)
  - inline_jobs.py (3 routes: job status, list, cancel)
  - inline_misc.py (7 routes: health, cache, oauth, favicon, system)
  - inline_query.py (1 route: semantic query)
  - inline_repos.py (14 routes: repos activation, sync, branches, golden)
  - inline_repos_v2.py (6 routes: repositories v2 API)
  - inline_routes.py reduced to 435-line delegation hub

### Tests
- test: add 140 route coverage tests covering 100% of 158 routes as safety net for modularization

## v9.5.31

### Bug Fixes
- fix: refresh scheduler enforces default_branch before git pull, preventing branch contamination (Bug #469 Fix 1)
- fix: block git_branch_switch on golden repo -global aliases (Bug #469 Fix 2)
- fix: change_golden_repo_branch rolls back git checkout on partial failure (Bug #469 Fix 3)
- fix: SmartIndexer writes correct 'current_branch' key instead of 'branch' (Bug #469 Fix 4)
- fix: incremental indexing converts relative paths from git diff to absolute (Bug #469 Fix 5)
- fix: temporal indexer handles mock config file_extensions gracefully (Bug #469 regression)
- fix: --reconcile uses single batched git diff instead of per-file subprocess (Bug #471)
- fix: Research Assistant sanitizes dash-prefixed user messages to prevent CLI arg injection (Bug #472)
- fix: golden repo index rebuild acquires write lock and creates CoW snapshot (Bug #473)

### Features
- feat: smart embedding cache skips VoyageAI API calls for unchanged chunks via content hash matching (Story #470)

## v9.5.30

### Bug Fixes

- fix: temporal indexer indexed binary files (.jar, .zip, .exe) — no file_extensions check (Bug #469)
  - TemporalDiffScanner._should_include_file() set base_result=True for ALL files without checking file_extensions
  - Every binary file in every git commit was embedded by temporal indexing
  - Fix: Added file_extensions filtering before override logic, matching FileFinder behavior

### Tests

- Added 45 TDD tests for temporal diff scanner binary filtering + E2E verification

## v9.5.29

### Bug Fixes

- fix: branch change path indexed binary files (.jar, .zip, .exe, .psd) bypassing extension filter (Bug #469)
  - Root cause: GitTopologyService.analyze_branch_change() returned raw git diff output without filtering by file_extensions
  - Every binary file differing between branches was sent to VoyageAI for embedding, burning API credits and causing multi-hour timeouts
  - Fix: analyze_branch_change() now filters files_to_reindex by config.file_extensions
- fix: _should_index_file() dot mismatch broke git-aware incremental indexing since v2.15.0 (Bug #469)
  - path.suffix.lower() returns ".java" (with dot) but config.file_extensions stores "java" (without dot)
  - Git delta TRACK 1 rejected ALL source files, falling back entirely to mtime-based detection
- fix: _should_index_file() exclude_dirs used substring match instead of path components (Bug #469)
  - "build" excluded "src/builder/App.java", "bin" excluded "src/bindings/ffi.rs", etc.
  - Now uses Path.parts for proper directory boundary matching

### Tests

- Added 96 TDD tests for Bug #469 covering binary file exclusion, git delta filtering, branch change scenarios, exclude_dirs substring collisions, and production-scale mixed Java/Kotlin repos

## v9.5.28

### Bug Fixes

- fix: add_index semantic rebuild now uses --clear flag for full re-index (Bug #468)
  - Previously ran bare `cidx index` (incremental) which was a no-op on already-indexed repos
  - Now runs `cidx index --clear` to force full semantic vector regeneration
- fix: multi-select add_index (semantic+fts) no longer triggers false DuplicateJobError (Bug #468)
  - Root cause: all index types shared operation_type="add_index", so second submission conflicted with first
  - Fix: operation_type now includes index type (e.g., "add_index_semantic", "add_index_fts")
- fix: DuplicateJobError returns 409 Conflict instead of 500 Internal Server Error
- fix: JS handles multi-select job_ids response (was only reading job_id, null for multi-select)

## v9.5.26

### Bug Fixes

- fix: removed cidx_index_timeout entirely from codebase (Bug #467 continued)
  - Removed from config model, config service, Web UI, golden_repo_manager, maintenance_service
  - Indexing subprocess calls in golden_repo_manager no longer have timeout
  - Background job staleness threshold changed from timeout-based to fixed 24h
  - Backward compatible: old config files with cidx_index_timeout load without error
- fix: DuplicateJobError silently skipped instead of logged as ERROR with full traceback

## v9.5.25

### Bug Fixes

- fix: Bug #467 -- Golden repo refresh indexing resilience (critical)
  - Removed subprocess timeout from _index_source() for semantic+FTS, temporal, and SCIP indexing
  - Interruptions (timeout, kill, SIGTERM) no longer poison metadata with status="failed"
  - Progressive metadata now accepts "failed" status for resume (can_resume_interrupted_operation, get_resume_timestamp)
  - Conditional --reconcile: uses reconcile mode only when metadata shows interrupted state (in_progress/failed), normal incremental otherwise
  - Prevents wasting VoyageAI API credits re-embedding identical content after interrupted indexing
- fix: .NET 7-digit fractional seconds in Claude Server timestamps (v9.5.24)
- fix: dateutil dependency replaced with stdlib datetime.fromisoformat (v9.5.23)

## v9.5.21

### Bug Fixes

- fix: Bug #464 -- get_cached_content page parameter not converted from string to int, causing TypeError
- fix: Bug #443 -- GitLab CI client does not follow HTTP 301 redirects for renamed/moved projects
- fix: 94 undefined name errors in inline_routes.py from v9.5.16 app.py modularization
- fix: Langfuse sync status showing Unknown -- stale module-level global after lifespan refactor
- fix: OAuth discovery endpoint broken import path after modularization
- fix: file_service missing from app.py globals (broke browse_directory, get_file_content, list_files)
- fix: 8 missing Pydantic model re-exports in app.py (broke 100+ test files)
- fix: non-blocking poll_delegation_job with repeatable result retrieval
- fix: large delegation results use PayloadCache for chunked retrieval
- fix: 7 failing unit tests -- fixture, expectation, and categorization fixes

### Features

- feat: MCP route smoke test regression suite (Story #463) -- 39 TestClient tests catching refactor bug class
- test: normal_user denial tests for cs_list_repositories and cs_check_health

### Documentation

- docs: improved delegation MCP tool descriptions for accuracy and completeness

## v9.5.17

### Features

- feat: open-ended delegation for Claude Server integration (Epic #455, v9.5.17)
  - New MCP tool `execute_open_delegation` -- submit any free-form coding objective with engine/mode selection
  - New `delegate_open` permission for power_user and admin roles
  - Safety guardrails system with configurable golden repo for authorized packages and safety prompts
  - Convention: `guardrails/system-prompt.md` + `packages/<language>/approved.txt`
  - Default guardrails template covering 6 safety categories (filesystem, process, git, system, package, secrets)
  - Audit trail for every delegation call (action_type=open_delegation_executed)
  - Web UI config: guardrails repo dropdown, guardrails toggle, default engine/mode dropdowns
  - Server-side validation for engine, mode, and guardrails repo alias
  - Claude Server proxy tools: `cs_register_repository`, `cs_list_repositories`, `cs_check_health`
  - Repo readiness polling with configurable timeout (accepts both cloneStatus "completed" and "success")
  - Forward-compatible config loading (unknown JSON keys silently ignored)
  - Documentation: `docs/guardrails-repo-convention.md`

## v9.5.16

### Refactoring

- refact: app.py modularization -- 8,800 lines decomposed to 196-line composition root (Story #409, Epic #408, v9.5.16). Extracted into 6 focused modules:
  - `startup/service_init.py` -- `initialize_services()` returns all managers/services as typed dict
  - `startup/lifespan.py` -- `make_lifespan()` factory for FastAPI lifespan async context manager (15+ subsystem init)
  - `startup/app_wiring.py` -- `create_fastapi_app()` wires middleware, app.state, and route registration
  - `startup/bootstrap.py` -- `_detect_repo_root()`, `migrate_legacy_cidx_meta()`, `bootstrap_cidx_meta()`, `register_langfuse_golden_repos()`
  - `routers/inline_routes.py` -- `register_inline_routes()` with all 63+ route handlers
  - `models/` subpackage -- `auth.py`, `repos.py`, `jobs.py`, `query.py` with re-exports from `__init__.py`
  - `app_state.py` -- Typed `AppState` class replacing ad-hoc `app.state` attributes
  - Fixed 3 silent NameErrors (missing imports masked by try/except), 1 duplicate model class pair
  - 100% backward compatible -- all `from code_indexer.server.app import X` imports preserved via re-exports
  - 570 new tests (test_app_state.py, test_models_package.py). 5,209 tests pass, zero modifications to existing tests. fast-automation.sh passes.

## v9.5.15

### Features

- feat: MR/PR lifecycle and git workflow completion for remote development (Epic #444, v9.5.15). 10 new MCP tools completing the remote dev review loop:
  - Story #445: Fixed git_push for new branches -- auto-detects current branch, uses explicit refspec HEAD:refs/heads/<branch>, sets upstream tracking after push, guards against detached HEAD state.
  - Story #446: list_pull_requests -- List open/closed/merged PRs/MRs with state, author, and limit filters. GitHub merged state filters closed PRs with non-null merged_at.
  - Story #447: get_pull_request -- Full PR/MR details including description, labels, reviewers, CI status, mergeable state, and diff stats.
  - Story #448: list_pull_request_comments -- Read review comments and conversation threads. Merges GitHub's two comment endpoints (review + issue) into unified format. Filters GitLab system notes.
  - Story #449: comment_on_pull_request -- Add general or inline file comments. Fetches commit SHA (GitHub) or diff_refs (GitLab) for inline positioning.
  - Story #450: update_pull_request -- Update title, description, labels, assignees, reviewers. GitHub reviewers use separate endpoint.
  - Story #451: merge_pull_request -- Merge with method selection (merge/squash/rebase). Fetches head SHA for safety. Optional source branch deletion.
  - Story #452: close_pull_request -- Close PR/MR without merging.
  - Story #453: git_stash -- Push/pop/apply/list/drop stash operations via single tool with action dispatch.
  - Story #454: git_amend -- Amend last commit with PAT credential identity attribution.
  Both GitHub and GitLab supported across all forge-related tools. 225+ new tests. fast-automation.sh passes.

## v9.5.14

### Performance

- feat: Scalable index maintenance for high-frequency incremental repos (Epic #438, v9.5.14). Four surgical fixes addressing critical performance problems on repos with >10K vectors:
  - Story #439: Removed O(N) rglob count-mismatch fallback from HNSW is_stale(), eliminating 25-98 second query-time rebuilds after every refresh cycle. Staleness now relies solely on the explicit is_stale flag in metadata (O(1) JSON read).
  - Story #440: HNSW search now uses distances directly (similarity = 1.0 - distance) instead of re-reading each candidate's 22KB JSON file to recalculate cosine similarity. JSON files read only for top-limit results (not all candidates), cutting query I/O by ~50%.
  - Story #441: Langfuse trace sync now acquires per-user write lock before triggering refresh, preventing periodic RefreshScheduler from racing with explicitly triggered refreshes. Lock released in finally block for exception safety.
  - Story #442: Added wait_merging_threads() after Tantivy commit() to ensure FTS segment merges complete before writer is released. Extracted _commit_inner() so update_document(), delete_document(), and close() also benefit. Handles Tantivy binding behavior where wait_merging_threads() consumes the writer (re-creates it automatically).
  24 new tests across 4 test files. fast-automation.sh passes.

## v9.5.13

### Bug Fixes

- fix: Stale error_message in dependency_map_tracking after orphan recovery (Bug #437, v9.5.13). Three update_tracking() call sites in dependency_map_service.py were not clearing error_message when transitioning to running or completing with no changes. After server restart orphaned a running job, the stale 'orphaned - server restarted' message persisted indefinitely. Fixed by passing error_message=None in all status transition paths: run_full_analysis() running transition, run_delta_analysis() no-changes completion, and run_delta_analysis() running transition. 9 new tests.

## v9.5.12

### Bug Fixes

- fix: git_log handler no longer crashes when repository_alias is passed as a list (Bug #432, v9.5.12). Added type validation at the start of _resolve_git_repo_path() to return a clear error message instead of an AttributeError. 4 new tests.

## v9.5.11

### Security

- fix: Research assistant prompt hardened to prohibit source code modifications on deployed servers (v9.5.11). Added "NO SOURCE CODE MODIFICATIONS" as absolute prohibition in both the template prompt and hardcoded fallback. Removed git operations (pull, checkout, reset) from allowed remediation operations. Research assistant previously edited Python source files directly on staging, blocking the auto-updater from pulling updates.

## v9.5.10

### Bug Fixes

- fix: Orphaned jobs persist as "running" after server restart (Bug #436, v9.5.10). Three gaps fixed: (1) SyncJobsSqliteBackend now has cleanup_orphaned_jobs_on_startup() mirroring BackgroundJobsSqliteBackend pattern -- marks running/pending sync_jobs as failed on startup. (2) LangfuseTraceSyncService.stop() now fails tracked job via JobTracker when thread doesn't finish within join timeout, with thread-safe _sync_lock access to _current_tracked_job_id. (3) Cooperative shutdown checks added in _do_sync_all_projects() between project iterations and before post-sync callback. Code review fixes: deferred _current_tracked_job_id assignment until after successful registration, differentiated clean vs timed-out shutdown log messages. 22 new tests, fast-automation passes.

## v9.5.9

### Bug Fixes

- fix: SQLite connection leak completion -- remaining 11 direct sqlite3.connect() calls migrated to DatabaseConnectionManager (Bug #435, v9.5.9). Five server-side files still bypassing the singleton after v9.5.8: database_health_service.py (5 calls), sqlite_log_handler.py (1), health_service.py (1), data_retention_scheduler.py (1), diagnostics_service.py (1). All write operations now use execute_atomic() for proper transaction isolation on shared thread-local connections. _check_not_locked() retains isolated connection (BEGIN IMMEDIATE would corrupt shared connection state). Dead _PatchableConnection class removed. Regression test allowlist tightened from 7 to 3 files. 7 new TDD tests, 5209 tests pass.

## v9.5.8

### Bug Fixes

- fix: SQLite connection leak -- 75+ direct sqlite3.connect() calls migrated to DatabaseConnectionManager (Bug #434, v9.5.8). All server-side SQLite operations now use thread-local connection pooling via DatabaseConnectionManager.get_instance(db_path), eliminating FD accumulation (38 to 73+ and growing) and native memory growth (2.4GB to 3.2GB over ~2 hours). Write operations use execute_atomic() for consistent transaction handling. Cursor-level row_factory prevents shared connection state mutation. 7 justified exceptions documented (health probes, bootstrap, log handler). Regression test guards against reintroduction. 23 files changed, 5209 tests pass, E2E validated on live server.

## v9.5.7

### Features

- feat: Debug memory snapshot endpoints for runtime memory diagnostics (Story #405). Two new localhost-only endpoints: `GET /debug/memory-snapshot` returns object counts and sizes by type (top 100) with module-qualified names and self-monitoring overhead reporting; `GET /debug/memory-compare?baseline={timestamp}` diffs current memory against a prior snapshot. Secured by network restriction (127.0.0.1/::1 only, no auth required, 403 for external IPs). 57 new unit tests across 4 test files.

## v9.5.6

### Security

- feat: CI/CD credential security and resilience -- group access control, per-user write tokens, and global PAT fallback (Story #404). All 12 CI/CD MCP handlers (6 GitHub Actions, 6 GitLab CI) now enforce group-based repository access with invisible repo pattern, use per-user PAT exclusively for write operations (retry/cancel), produce INFO-level audit log entries for all write mutations, and fall back from global CI token to personal PAT for read operations. 5 new helper functions, 55 new unit tests, E2E validated against live server.

## v9.5.5

### Bug Fixes

- fix: MCP parameter audit -- 11 schema/handler mismatches resolved (Story #403). P0: All 6 GitLab CI handlers now read and pass `base_url` to GitLabCIClient, enabling self-hosted GitLab instances. P1: Removed `aggregation_mode` from git_log, git_search_commits, regex_search schemas (only used by search_code). P3: Added `case_sensitive` to gitlab_ci_search_logs schema (handler already reads it). 12 new unit tests.

## v9.5.4

### Bug Fixes

- fix: create_pull_request MCP tool no longer requires `token` parameter (Bug #392). The handler already auto-fetches the PAT from stored credentials via `_get_pat_credential_for_remote()`, but the MCP schema listed `token` as required, causing MCP clients to reject requests without it. Removed `token` from the required array and updated the example. Users with registered PAT credentials can now create PRs/MRs without providing a raw token.

## v9.5.3

### Bug Fixes

- fix: git_reset handler reads MCP parameter `commit_hash` instead of non-existent `target`. Bug #397 (v9.5.1) fixed the handler-to-service call but missed the args extraction: `args.get("target")` should have been `args.get("commit_hash")` to match the MCP tool schema. Result: `commit_hash` parameter was silently ignored, every reset defaulted to HEAD (no-op).

## v9.5.2

### Bug Fixes

- fix: git_commit committer identity now uses user's registered PAT credential (Story #402). Previously, git_commit only set GIT_AUTHOR_* from the credential, leaving GIT_COMMITTER_* to fall back to the repo's local git config (set during activation via SSH key discovery). This caused push rejections on identity-strict forges like GitLab where committer email must match the pusher's verified emails. Now mirrors the git_push pattern: both author and committer are set from the PAT credential when available, with graceful fallback to author identity when no credential exists. Validation reordered to run before I/O.

## v9.5.1

### Bug Fixes

- fix: git_reset MCP handler parameter name mismatch (Bug #397). Handler passed `target=target` but service expects `commit_hash=commit_hash`, causing every git_reset call to silently ignore the target commit and default to HEAD. Fixed to `commit_hash=target`. Corrected 2 pre-existing test assertions that were validating the buggy call signature.

## v9.5.0

### Features

- feat: Audit log consolidation -- AuditLogService extracted from GroupAccessManager via delegation pattern (Epic #398, Story #399). Flat-file password_audit.log migrated to SQLite (groups.db audit_logs table) at server startup. MCP handlers and REST endpoints now query AuditLogService directly. GroupAccessManager retains log_audit()/get_audit_logs() API but delegates to AuditLogService when injected.
- feat: Configurable data retention periods for 5 data stores via Web UI config screen (Epic #398, Story #400). Replaces the single cleanup_max_age_hours with granular retention settings: operational logs (7d), audit logs (90d), sync jobs (30d), dependency map history (90d), background jobs (30d). All configurable from /admin/config without server restart.
- feat: DataRetentionScheduler periodic cleanup job as daemon thread (Epic #398, Story #401). Batched DELETEs (1000 rows/batch) across 3 SQLite databases (logs.db, cidx_server.db, groups.db). Tracked via JobTracker for dashboard visibility. Re-reads config each cycle for hot-reload of retention settings.

## v9.4.4

### Bug Fixes

- fix: Delta analysis early-return path now also cleans stale repos from _domains.json (Bug #396 follow-up). When a deleted repo's _index.md was stale, identify_affected_domains() returned empty, triggering an early return before the Bug #396 cleanup code could execute. The stale repo cleanup now runs in both the early-return and normal code paths.

## v9.4.3

### Bug Fixes

- fix: Git write operations bug fixes (Epic #385, Bugs #391-#396). Fixed activated repo write detection for git push/commit operations (Bug #391). Made PAT token optional in create_pull_request by auto-resolving from stored git credentials (Bug #392). Added git credential CRUD UI to admin and user dashboards (Bug #393). Implemented forge client for GitHub/GitLab identity validation during credential storage (Bug #394). Implemented AES-256 encrypted credential storage with SQLite backend (Bug #395).
- fix: Delta analysis now removes stale repo aliases from _domains.json participating_repos when golden repos are deleted (Bug #396). Previously, deleting a golden repo left phantom references in domain participating_repos lists, causing downstream inconsistencies in domain coverage reporting.

### Improvements

- feat: Dependency map anomaly detection Check 7 -- stale participating repos (complements Bug #396). DepMapHealthDetector now detects repo aliases in _domains.json participating_repos that no longer exist as golden repos. This is the symmetric inverse of existing Check 6 (repos in DB but not in any domain). Anomaly type: stale_participating_repo, severity: needs_repair.

## v9.4.2

### Performance

- perf: Cache-first HF tokenizer loading eliminates unnecessary network calls (Story #384). `Tokenizer.from_pretrained()` was contacting HuggingFace Hub on every process startup (~2.8s) even when the tokenizer was already cached on disk. Now uses `Tokenizer.from_file()` to load directly from the local HF cache (~417ms), falling back to network only on first-ever use or corrupted cache. Saves ~2.4 seconds per reindex process, eliminating HF Hub rate-limit warnings during multi-repo branch change operations.

## v9.4.1

### Bug Fixes

- fix: Description refresh scheduler false positive error detection (Bug #382). Split error pattern validation into two tiers: infrastructure errors (always checked) and content-ambiguous patterns like "rate limit" and "quota exceeded" (only checked when output lacks YAML frontmatter). Valid repository descriptions mentioning rate limiting are no longer incorrectly rejected.
- fix: Clean stale dependency-map.staging/ directory after failed analysis and on server startup (Bug #383). Added staging directory cleanup in the finally block of run_full_analysis() when analysis fails, and startup cleanup in app.py to remove orphaned staging directories from prior crashes. Prevents stale staging content from polluting semantic search results via RefreshScheduler indexing.

## v9.4.0

### Features

- feat: Wiki Settings Generalization (Epic #322). Made the wiki module fully generic by converting 5 hard-coded Salesforce Knowledge Base assumptions into settings-driven behavior exposed through the Config Web UI. Added WikiConfig dataclass with 4 boolean toggles (enable_header_block_parsing, enable_article_number, enable_publication_status, enable_views_seeding) and a metadata_display_order comma-separated string for controlling metadata panel field ordering. All defaults match current behavior for backward compatibility. All toggles OFF produces a clean generic wiki with no KB-specific artifacts. Settings take effect without server restart. Includes comprehensive regression tests for every toggle ON/OFF state.

## v9.3.108

### Features

- feat: Refinement job invokable from Dependency Map tab with background job tracking (Bug #371). Added "Refinement Pass" button to the Dependency Map dashboard that triggers a manual refinement cycle. The refinement job registers with the background job tracker system (operation_type: dependency_map_refinement), providing status/progress visibility. Includes lock acquisition to prevent concurrent refinement/analysis races, conflict detection (409 when busy), and proper job completion/failure tracking.

## v9.3.107

### Bug Fixes

- fix: Subscription mode config display always showed "API Key" regardless of actual setting (Story #367). The `get_all_settings()` method was missing 4 subscription-related fields (`claude_auth_mode`, `llm_creds_provider_url`, `llm_creds_provider_api_key`, `llm_creds_provider_consumer_id`) from the `claude_cli` dict, causing the Web UI config page to always render the default "API Key" mode. Also added input validation for `claude_auth_mode` in `_update_claude_cli_setting()`.

## v9.3.106

### Features

- feat: Continuous dependency document refinement (Story #359). Added scheduled refinement job that fact-checks existing dependency domain documents against source code using Claude CLI. Refinement is editorial (corrects/verifies existing content) not authorial (does not rewrite from scratch). Features cursor-based round-robin domain cycling, truncation guard rejecting output less than 50% of original body, no-op detection for unchanged content, and three-way domain dispatch (create/refine/normal). Configurable via Web UI: refinement_enabled, refinement_interval_hours, refinement_domains_per_run. Adds last_refined timestamp to document frontmatter while preserving last_analyzed.

## v9.3.105

### Features

- feat: Configurable job history retention period via Web UI (Story #360). Changed default cleanup_max_age_hours from 24 to 720 (30 days) so dashboard 7-day and 30-day time filters show meaningful historical data. Added Job History Retention field to Background Task Workers config section with validation (1-8760 hours). Admin API DELETE /api/admin/jobs/cleanup now uses configured default when no explicit param provided.

## v9.3.104

### Bug Fixes

- fix: directory_explorer crashes with PermissionError on symlinks to restricted system files (Bug #368). Extended iterdir() and per-entry stat() error handling to catch both PermissionError and OSError, gracefully skipping inaccessible entries instead of crashing.
- fix: research_assistant_service logs empty error when Claude CLI fails (Bug #370). Added three-tier fallback chain for error messages: stderr, then stdout, then exit code. Error messages are never empty now.

## v9.3.103

### Bug Fixes

- fix: SQLite thread-local connection leak causing unbounded memory growth (Story #369). Added piggyback cleanup to DatabaseConnectionManager and SQLiteLogHandler that detects dead threads via threading.enumerate() and closes their orphaned connections. Cleanup is throttled to every 60 seconds to avoid overhead. SQLiteLogHandler.close() now closes ALL tracked connections, not just the calling thread's. Added check_same_thread=False to DatabaseConnectionManager for cross-thread cleanup safety.

## v9.3.102

### Bug Fixes

- fix: Lease lifecycle api_key path didn't write to ~/.claude.json (Bug #363). Claude CLI reads apiKey from ~/.claude.json, not ANTHROPIC_API_KEY env var. Added _write_claude_api_key() and _clear_claude_api_key() helpers to the lifecycle service. Both credential paths (api_key and oauth) now clean up ~/.claude.json on stop/crash-recovery, preventing stale credentials from surviving mode switches. File written with 0o600 permissions.

## v9.3.101

### Bug Fixes

- fix: API key auto-seeding was bidirectional, preventing operators from clearing keys (Bug found during Epic #363 staging validation). When an operator removed the Anthropic API key from config, the auto-seeder would read it back from the systemd environment variable and re-write it to config. Rewrote api_key_seeding.py to be strictly unidirectional: config is the single source of truth, keys flow config-to-env only, and blank config actively clears the environment variable. Never writes back to config.

## v9.3.100

### Bug Fixes

- fix: LLM lease lifecycle ignores api_key credential type from provider (Bug found during E2E testing of Epic #363). When the llm-creds-provider returns an api_key credential (instead of OAuth tokens), the lifecycle now sets ANTHROPIC_API_KEY in os.environ instead of writing empty tokens to .credentials.json. Also fixed crash recovery to branch on credential_type (persisted in LlmLeaseState), and reset _credential_type on failed start to prevent stale state.

## v9.3.99

### Enhancements

- feat: LLM Creds Provider integration for subscription-based Claude credentials (Epic #363). Added subscription auth mode where CIDX server checks out OAuth credentials from an external llm-creds-provider service instead of using a static API key. Includes: HTTP client with typed exceptions (Story #365), encrypted lease state persistence with AES-256-CBC (Story #365), Claude credentials file manager writing .credentials.json in claudeAiOauth format (Story #365), lifecycle service with crash recovery and token writeback on checkin (Story #366), mode guard making ApiKeySyncService a no-op in subscription mode (Story #366), server startup/shutdown hooks (Story #366), Config UI section with test-connection, save-config, and lease-status endpoints (Story #367), and 409 guard preventing API key save in subscription mode (Story #367). 137 new unit tests.

## v9.3.98

### Bug Fixes

- fix: add_golden_repo_index shows empty error messages when subprocess fails (Bug #361). When index creation failed and the error was in stdout (not stderr), the error message showed "Failed to create FTS index: " with nothing after the colon. Applied stderr-or-stdout-or-exit-code fallback chain to all 5 index types (init, semantic, fts, temporal, scip) plus the CalledProcessError handler, ensuring diagnostic information is always present in error messages.

## v9.3.97

### Bug Fixes

- fix: Dashboard index_memory_mb stuck at 0.0 despite loaded indexes (Bug #358). Health endpoint was reading from SystemMetricsCollector background-cached value which could stay stale at 0.0 during startup race conditions. Changed to call get_total_index_memory_mb() directly from HNSW/FTS cache stats (always live, always current). Also fixed OTEL machine_metrics.py using wrong import path (from src.code_indexer... instead of from code_indexer...) which created a separate singleton with no index memory provider registered.

## v9.3.96

### Enhancements

- feat: Real index memory tracking replacing broken mmap metric (Story #526). The dashboard "Mmap Files" metric was fundamentally incorrect -- hnswlib loads indexes via fread() into heap memory, not mmap, making HNSW indexes invisible to process.memory_maps(). Replaced with "Index Mem" that reports actual cache footprint: hnswlib.Index.index_file_size() for HNSW heap allocation, directory size scan for Tantivy FTS mmap files. Field renamed from mmap_total_mb to index_memory_mb across API model, health service, metrics collector, and dashboard. Added callback-based provider pattern to avoid circular imports between cache and metrics modules.

## v9.3.95

### Enhancements

- feat: Debounced cidx-meta refresh on batch repository registration (Story #345). When multiple repos are registered in rapid succession, DuplicateJobError from concurrent refresh attempts is now caught and deferred via a CidxMetaRefreshDebouncer. The debouncer coalesces signals into a single refresh after a 30-second cooldown, with automatic retry if the refresh slot is still occupied.
- feat: Self-Monitoring tab pagination (Story #344). Both the Scan History and Created Issues tables now have client-side pagination with selectable page sizes (10/20/50/100). Each table paginates independently with Prev/Next navigation and boundary handling.

## v9.3.94

### Enhancements

- feat: Dashboard memory metrics - CIDX RSS, Mmap Files, and Swap (Story #358). Added three new metrics to the System Resources card on the web UI dashboard below the existing Memory bar: CIDX RSS (process resident set size in MB), Mmap Files (total file-backed memory-mapped size in MB from HNSW indexes and SQLite DBs), and Swap (used/total in MB). Metrics are collected via psutil with graceful fallback to 0.0 on AccessDenied. Displayed as plain text MB values with 2-second HTMX auto-refresh.

## v9.3.93

### Bug Fixes

- fix: FTS search crashes with ValueError on queries containing colons, parentheses, or brackets (Bug #357). Queries like `com.cdk.recreation:SomeClass`, `std::vector`, `foo(bar)`, `test[0]` caused Tantivy parse errors that propagated as QUERY-MIGRATE error bursts (3 log entries per repository). Added Phase 2 to sanitize_fts_query() that escapes Tantivy syntax characters (colon to space, strip `()[]{}`) before boolean operator validation. Defense-in-depth wrapper around _build_search_query() catches any remaining parse errors and returns empty results with a warning instead of propagating ValueError.

## v9.3.92

### Enhancements

- feat: Signal-based server restart via auto-updater (Story #355, v9.3.92). Server no longer needs sudo/privilege escalation to restart under systemd with NoNewPrivileges=true. When admin triggers restart from Diagnostics tab, server writes JSON signal file (~/.cidx-server/restart.signal) with timestamp and reason fields. Auto-updater poll_once() detects the signal, deletes the file before calling restart_server() (at-most-once delivery), and executes the restart. Stale signals (>120s) are cleaned up without restart. Dev mode (os.execv) unchanged.

## v9.3.91

### Enhancements

- feat: Auto-updater configures vm.overcommit_memory=1 and provisions 4GB swap file to prevent ENOMEM fork failures (Story #356). Production servers with large mmap'd HNSW indexes (~57GB VmPeak) fail subprocess.run() calls (git, pip, systemctl) when kernel heuristic overcommit refuses fork(). Added two new idempotent _ensure_* methods to DeploymentExecutor: _ensure_memory_overcommit() writes /etc/sysctl.d/99-cidx-memory.conf and applies immediately, _ensure_swap_file() creates /swapfile with fstab persistence. Both are non-fatal (deployment continues on failure) and safe for repeated runs.

## v9.3.90

### Bug Fixes

- fix: scip_context hangs indefinitely for large symbols on large repos (Bug #351). Added 30-second timeout protection using thread-based watchdog pattern (daemon thread + join(timeout=)). Works correctly from ThreadPoolExecutor worker threads where MCP handlers run, unlike signal.alarm() which only fires in the main thread. All except blocks in _get_smart_context_impl() re-raise QueryTimeoutError to prevent silent swallowing. Also added 60-second client timeout to perf suite httpx calls.

## v9.3.89

### Bug Fixes

- fix: Dashboard "Total Traces" count inflated by CIDX vector index files. The _get_langfuse_folder_stats() method used folder.glob("**/*.json") which counted .code-indexer/ vector store JSON files (88K+) alongside actual trace files (4K), inflating the reported count ~22x. Fixed by excluding files with .code-indexer in their path components.

## v9.3.88

### Enhancements

- feat: Proper boolean query support for FTS (Story #354). MCP clients and CLI users can now use boolean operators (OR, AND, NOT) in FTS queries naturally. Added _contains_valid_boolean_ops() detection function that identifies valid boolean operators before _build_search_query() splits into per-term queries. Exact-match boolean queries route directly to Tantivy's parse_query() which natively supports OR/AND/NOT. Fuzzy + boolean degrades gracefully with warning (strips operators, fuzzy-matches remaining terms). Non-boolean multi-word queries preserve existing AND semantics unchanged. Bare NOT handled correctly (requires compound form "term NOT excluded").

## v9.3.87

### Bug Fixes

- fix: FTS bare boolean operator crash (Bug #353). Tantivy parse_query() raises "Syntax Error: OR" when queries contain bare boolean operators (OR, AND, NOT) without proper operands. Extended sanitize_fts_query() with Phase 2 that detects bare/invalid boolean operators and lowercases them so Tantivy treats them as literal search terms. Handles: bare operators alone ("OR"), trailing ("term OR"), leading ("OR term"), and adjacent ("term OR AND other"). Valid boolean queries ("term1 OR term2") are preserved unchanged. Moved _BOOL_OPS to module-level frozenset for performance.

## v9.3.86

### Bug Fixes

- fix: FTS unmatched quote crash (Bug #346). Tantivy parse_query() raises ValueError on queries with odd number of double-quotes. Added sanitize_fts_query() that strips all quotes when count is odd, applied at both top-level and per-term parse paths in _build_search_query().

- fix: API metrics tracking gap (Bug #350). Dashboard showed ZERO activity for SCIP, browse, list, and many other MCP tools because only search_code, regex_search, file_crud, ssh_key_manager, and git_operations tracked metrics. Centralized tracking in protocol.py dispatch layer (handle_tools_call) with _SELF_TRACKING_TOOLS frozenset to skip search_code/regex_search. Removed 25 scattered increment_other_api_call() calls from file_crud_service (3), git_operations_service (17), ssh_key_manager (5). Added tracking to 6 REST endpoints: /api/query/multi, /api/scip/multi/definition, /references, /dependencies, /dependents, /callchain.

## v9.3.85

### Enhancements

- feat: Add canary file-write test to Pass 1 dependency map synthesis prompt. Before Claude spends cycles analyzing 125+ repos, it now runs a quick write/delete test on the target directory as Step 0. If the canary fails (OS permission denied or Claude CLI permission restricted), the process bails immediately with a diagnostic RuntimeError instead of wasting 50+ turns attempting analysis and retrying different write methods. Saves significant time and API tokens when permission issues exist.

## v9.3.84

### Bug Fixes

- fix: format_error_log() crashes in 32 error handlers across routes.py and handlers.py. All calls used printf-style %s formatting with 3+ positional arguments, but format_error_log() only accepts 2 positional args (error_code, message) plus **kwargs. Converted all 32 calls to f-string interpolation and moved extra=/exc_info= kwargs to the outer logger call.

- fix: Pass 1 dependency map synthesis fails with "permission restrictions" on production (125 repos). Claude CLI could not write pass1_domains.json because no permission bypass flag was passed. Switched from --permission-mode bypassPermissions to --dangerously-skip-permissions (matching all other Claude CLI invocations in codebase) and ensured both Pass 1 primary and retry calls include the flag. Pass 2 intentionally excluded to prevent frontmatter corruption.

- fix: Pass 1 error message stdout preview truncated to 200 chars, hiding actual error details. Increased to 1000 chars so the diagnostic information (permission errors, file paths, Claude self-diagnosis) is visible in the UI.

## v9.3.83

### Bug Fixes

- fix: Scope bypassPermissions to Pass 1 only, preventing Claude from writing domain files directly in Pass 2 (Bug #349). Root cause: v9.3.82 applied --permission-mode bypassPermissions globally to all agentic Claude CLI invocations, giving Claude Write tool access in Pass 2. Claude wrote domain .md files directly to staging (without YAML frontmatter), Python saw empty stdout, skipped its frontmatter-adding write, and Claude's files persisted through stage-then-swap. Fix: Made permission_mode a per-call parameter on _invoke_claude_cli(); only Pass 1 passes bypassPermissions. Added defense-in-depth cleanup to delete any Claude-written file when analyzer skips a domain due to insufficient output.

## v9.3.82

### Bug Fixes

- fix: Add --permission-mode bypassPermissions to Claude CLI agentic invocations (Bug #349). Pass 1 dependency map synthesis failed on staging because Claude CLI tool use (Write, Bash) was blocked by permission prompts in non-interactive subprocess mode. Also softened "do NOT output to stdout" instructions to allow stdout JSON fallback when file writing is impossible.

## v9.3.81

### Features

- feat: File-based output for Pass 1 dependency map synthesis (Story #349). Replaces stdout JSON parsing with file-based approach where Claude CLI writes JSON to pass1_domains.json, validates with python3 -m json.tool, and self-corrects errors. Eliminates production failure on servers with 100+ golden repos where Claude outputs narrative instead of JSON due to primacy/recency dilution in very large prompts. Output format instructions moved to top of prompt. Three-tier resilience: file primary, stdout fallback, agentic retry with file-write reminder.

### Bug Fixes

- fix: Dependency map "Running" state lost on navigate-back. Two fixes: (A) job status partial guard prevents content health from overriding Running badge when analysis is active, (B) activity journal polling starts on page load with every-3s trigger instead of requiring a trigger event.

## v9.3.80

### Features

- feat: CIDX Server Performance Testing Suite (Epic #332). Standalone CLI tool in tools/perf-suite/ that measures server response times under escalating concurrent load (1-50 users), identifies degradation thresholds, and produces publishable Markdown reports. Covers 14 endpoints (semantic/hybrid/FTS search, SCIP callchain/impact/context, regex search, multi-repo queries, filesystem ops, wiki analytics) across 3 JSON scenario files. Uses httpx async with asyncio.Semaphore for precise concurrency control, single shared JWT with proactive refresh, ASCII degradation charts, and hardware profiling via SSH. 160 unit tests, 19 source modules.

## v9.3.79

### Bug Fixes

- fix: IndexRegenerator case-sensitivity bug destroyed cross-domain dependency edges during repair for mixed-case domain names (Bug #348). Root cause was a DRY violation -- IndexRegenerator reimplemented cross-domain parsing with naive text matching that compared original-case domain names against lowercased lines (always failed for "Core DMS Platform" etc). Also wrote wrong 3-column format instead of 5-column. Fix deletes duplicated logic entirely and delegates to DependencyMapAnalyzer._build_cross_domain_graph() which correctly parses structured Outgoing Dependencies tables. File reduced from 394 to 286 lines.

## v9.3.78

### Performance

- perf: Batch visibility update optimization reduces unchanged-file visibility phase from 5-8 minutes to seconds on large repos (Story #339). Four fixes: (A) batch ensure-visible replaces 2823 sequential scroll_points calls with single pre-fetched in-memory filter + batch payload write, (B) lightweight _batch_update_payload_only bypasses full upsert pipeline for payload-only changes, (C) _parse_filter hoisted outside per-file loop in scroll_points, (D) proper pagination in _fetch_all_content_points prevents silent truncation at 10000 points.

## v9.3.77

### Bug Fixes

- fix: CleanupManager._onerror callback TypeError when retrying os.open() after EMFILE -- catch TypeError alongside OSError since shutil's internal os.open(path, flags) retry only receives path, preventing crash during file descriptor exhaustion cleanup (Bug #343)

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
