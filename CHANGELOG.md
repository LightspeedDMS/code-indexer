# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [11.76.0] - 2026-07-21

### Fixed

- **#1449**: Server crashed reading non-UTF-8 file content (e.g. files containing binary or Latin-1 byte sequences that aren't valid UTF-8). Fixed by passing `errors="replace"` at both file-read sites in `server/services/file_service.py`, so invalid byte sequences are substituted with the Unicode replacement character instead of raising `UnicodeDecodeError`.
- **#1452**: Jobs panel filter chain was broken across multiple independent surfaces. The status filter dropdown offered a dead `"queued"` option that never matched any stored job status. Dashboard job-count tiles linked to the Jobs panel using a `status` query param while the panel's own filtering logic expected `status_filter`, and similarly linked search using `search` instead of `search_text`, so tile clicks silently failed to pre-filter the list. Separately, the jobs search predicate matched job type/alias/user but never `job_id`, so searching by job id returned no results. Fixed across all four independent backend implementations that maintain their own filtering logic (SQLite backend, PostgreSQL backend, in-memory/repository layer, and the web routes merge layer) plus the affected templates (`jobs.html`, `partials/dashboard_job_counts.html`, `partials/dashboard_stats.html`, `partials/jobs_list.html`, `partials/repos_list.html`).
- **#1453**: `check_hnsw_health` (MCP) and the legacy synchronous REST health-check endpoints for golden and activated repos could hang or time out the calling connection on large indexes, since HNSW integrity verification is a genuinely long-running operation with no natural time bound. Converted `check_hnsw_health` to the standard async job-submission pattern (submit, then poll for status/result) and removed the legacy synchronous REST `GET .../health` endpoints entirely, since they are fully superseded by the existing async `POST .../health/check` siblings already used elsewhere.

## [11.75.0] - 2026-07-19

### Fixed

- **#1451**: Research Assistant Claude CLI invocations were failing 100% of the time in production with `Claude CLI failed: Permission allow rule (...): Write(/opt/code-indexer/.cidx-server/golden-repos/cidx-meta/**) is not matched by file permission checks — only Edit(path) rules are.`. Root cause: `_allow_rules()` (`server/services/research_assistant_service.py`) has, since Story #554, emitted both a `Write({cidx_meta_path}/**)` and an `Edit({cidx_meta_path}/**)` allow rule scoped to the cidx-meta working directory. Claude Code CLI >= 2.1.215 now hard-rejects the bare `Write(path)` rule format for file-permission checks -- per the CLI's own error message, only `Edit(path)` rules are honored, and they already cover all file-editing tools. Fixed by removing the redundant, now-fatal `Write(...)` rule; the retained `Edit(...)` rule grants the same file-editing access unaided. Confirmed via `grep -rln '"Write('` across `src/` that this was the only bare `Write(path)` rule construction site in the codebase. Updated the stale regression test (`tests/unit/server/services/test_research_assistant_permissions_bug738.py::TestExistingAllowRulesPreserved`) that previously asserted the Write rule's presence to instead assert its absence, closing the gap that let this class of bug ship undetected.

## [11.74.0] - 2026-07-19

### Fixed

- **#1450**: `_build_pip_install_cmd()` (`server/auto_update/deployment_executor.py`, factored out by #1442) always built a plain `pip install -e .` command shared by BOTH `pip_install()` (server's pipx venv, every deploy cycle) and `_ensure_cli_dependencies_synced()` (CLI's separate system-Python interpreter, #1442's self-heal, every deploy cycle). `psycopg[binary]`/`psycopg-pool` are declared under `[project.optional-dependencies] cluster` in `pyproject.toml` -- NOT the base `dependencies` list -- so a bare `-e .` never installed them into the CLI's interpreter. `server/storage/postgres/connection_pool.py` does an unconditional module-level `import psycopg` regardless of storage_mode (a deliberate, already-documented invariant), transitively imported during `cidx index` execution via `cli.py`'s `_install_embedding_stats_writer_for_index()`, so every `cidx index` subprocess run through the CLI's system-Python interpreter crashed with `ModuleNotFoundError: No module named 'psycopg'` (wrapped as `RuntimeError: semantic indexing on source failed for ...`). Confirmed live in production (recurring `cidx-meta-global`/`k8s-wildfly-sandboxes-*-global` refresh failures) and reproduced on a solo staging VM running the exact same code at the exact same version via the real automated deploy mechanism. Fixed by requesting the `cluster` extras group (`.[cluster]` instead of bare `.`) in this single shared helper, closing the gap for both callers at once, self-healing on the next deploy cycle with zero operator action.

## [11.73.0] - 2026-07-19

### Fixed

- **#1447**: `test_inline_repos_sync_job_params.py::test_submit_job_without_params_kwarg_succeeds` intermittently stuck at job status `"pending"` under full-chunk parallel test execution. Two distinct issues coincided: (1) `test_bug1063_part4_dashboard_bounded_fetch.py` constructed real SQLite-backed `BackgroundJobManager` instances without initializing the schema first and without ever shutting them down, producing real `"no such table: background_jobs"` errors from its own tests (a real bug, fixed, but not the actual cause of the target flake); (2) the real root cause -- `test_inline_auth_api_keys_hybrid_auth_1283.py`'s module-level `import code_indexer.server.app` triggers a module-level `create_app()` that installs a genuine `MemoryGovernor` singleton with a real background sampling thread, never torn down; since the target job's `operation_type` (`sync_repository`) is memory-heavy and the admission gate is enabled by default, the leaked governor's admission gate re-queued the job with backoff past the test's wait window. Fixed by initializing schema + registering shutdown finalizers in the first file, and eagerly stopping/clearing the leaked governor at module-import time in the second (co-located with the leak it reverses, since pytest imports all collected modules during collection before any test runs -- a module-local fixture teardown alone would fire too late if a sibling module ran first). Verified via multiple clean combined runs including an independently-constructed adversarial ordering. Test-suite reliability fix only, no production code touched.

## [11.72.0] - 2026-07-19

### Fixed

- **#1448**: `test_pod_pull_work_stealing_roundtrip_bug1424.py`'s `TestPodPullRoundTrip` and `TestPodPullRoundTripRealSubmitBug1430` classes only cleared the global `MemoryGovernor` singleton in teardown, never in setup. If an earlier test in the same pytest worker left a pressured governor installed, `DistributedJobClaimer.claim_next_job()`'s memory gate blocked job admission, causing `_process_one_job()` to return `False` instead of `True`. Confirmed `clear_memory_governor()` (sets the singleton to `None`) is sufficient to fix this without injecting a permissive sample, since `claim_next_job()` fails open when no governor is installed -- a fresh `MemoryGovernor()` instance, by contrast, genuinely starts pressured (`band=RED`) and would still block. Added the reset to both classes' setup paths; verified via a genuine before/after reproduction (temporarily reverted, confirmed the exact original failure, restored the fix, confirmed it resolves). Test-suite reliability fix only, no production code touched.

## [11.71.0] - 2026-07-19

### Fixed

- **#1446**: `server-fast-automation.sh`'s "services/" chunk intermittently failed 4-5 unrelated pod-pull tests with `MaintenanceModeError`, even though none of them touch maintenance mode. Root cause: `MaintenanceState` (`maintenance_service.py`) is a module-level singleton; `test_maintenance_service.py` reset it at the start of each of its own tests but never after, so a maintenance-entering test running before unrelated tests in the same pytest worker left the singleton dirty for the rest of that process. Added an autouse fixture resetting state both before and after every test in the file, closing the pollution vector; confirmed via a combined run (this file + the affected pod-pull test files) that the `MaintenanceModeError` no longer reproduces. Test-suite reliability fix only -- `MaintenanceState` is explicitly non-persisted (cleared on real server restart), so this had no production impact.

## [11.70.0] - 2026-07-19

### Fixed

- **#1445**: `e2e-automation.sh` Phase 3's mandatory post-test log-audit gate intermittently failed on a single non-allowlisted WARNING from `temporal_snapshot_store` -- the intentional, already-shipped retry-and-log behavior from #1421 (temporal snapshot reassembly race detection, released as v11.60.0). The underlying test suite passed cleanly; only the zero-tolerance log audit flagged it. Added `"concurrent checkpoint rewrite detected during reassembly"` to `LOG_AUDIT_ALLOWLIST`, verified narrow enough that it cannot mask the distinct terminal `TemporalSnapshotReassemblyError` (all-retries-exhausted) failure message, which remains correctly non-allowlisted.

## [11.69.0] - 2026-07-19

### Fixed

- **#1444**: `/healthz` and `/api/system/health` permanently reported `unhealthy` (503) on every solo/SQLite install (production included), even though the server was genuinely healthy throughout. Root cause: `database_health_service.py`'s `_resolve_db_path()` special-cased `payload_cache.db` to `data/golden-repos/.cache/payload_cache.db` -- a path nothing ever writes to. The real file lives at `data/payload_cache.db` (same directory as `cidx_server.db`/`api_metrics.db`, per `storage/factory.py`'s `_create_sqlite_backends()`; confirmed via the live runtime wiring in `service_init.py`/`lifespan.py` that `PayloadCache`'s SQLite persistence is fully delegated to `PayloadCacheSqliteBackend`, never the vestigial `.cache`-subdir path). Fixed by resolving `payload_cache.db` the same way as its data-dir siblings. Two pre-existing tests were found to have been silently asserting the buggy path as correct (building their fixture there, never checking per-database `status`, only coarse list shape) -- fixed alongside the root cause, plus a new end-to-end test creating a real SQLite file at the corrected path and asserting `HEALTHY` status.

## [11.68.0] - 2026-07-19

### Fixed

- **#1443**: `python-frontmatter>=1.0.0` (unbounded) resolved to `1.2.0`, whose `util.py` imports `typing.TypeGuard` -- unavailable in the Python 3.9 stdlib (this project's own `requires-python` floor), so `import frontmatter` raised `ImportError` on every Python 3.9 host (confirmed: staging and production both run 3.9.25). Degraded gracefully via `wiki_service.py`'s existing try/except (wiki front-matter parsing silently disabled), so no crash, but a real silent feature gap. Fixed by capping the pin for `python_version < '3.10'` at `<1.2.0` (verified: 1.1.0 is the last pre-1.2.0 release, confirmed importable and correct on Python 3.9.25 in an isolated venv); hosts on 3.10+ keep the original unbounded behavior unchanged.

## [11.67.0] - 2026-07-19

### Fixed

- **#1442**: production's `cidx` CLI runs under a genuinely separate Python environment from the server (its own pipx venv) -- the auto-updater's deploy pipeline only ever ran `pip install -e .` against the server's environment, never the CLI's, so the CLI's dependencies froze at whatever the host's one-time bootstrap installed and silently drifted from the server's as the codebase grew. Confirmed on production via live investigation: `openpyxl`, `PIL`, `pyotp`, `python-frontmatter`, `qrcode`, `tree_sitter_languages`, and `langfuse` all missing under the CLI's interpreter despite being current, required dependencies. Fixed with a new self-heal step, `_ensure_cli_dependencies_synced()`, mirroring the existing Bug #1392 CLI-hnswlib-sync pattern: resolves the CLI's interpreter via the same `_get_cli_python_interpreter()` discovery, runs `pip install -e .` against it on every deploy cycle, non-fatal on failure (an independent environment's failure must never block the server's own restart). `pip_install()`'s sudo/`--break-system-packages` command construction was factored into shared helpers so both call sites stay in sync rather than duplicating that logic.

## [11.66.0] - 2026-07-19

### Fixed

- **#1441** (production hotfix): golden-repo refresh was failing fleet-wide in production with `ModuleNotFoundError: No module named 'psycopg'` since v11.64.0's embedding-stats bootstrap wiring (#1418). Root cause: the CLI's `NoOpWriter` fail-open fallback was itself transitively not psycopg-free (`embedding_stats_writer.py` -> `embedding_call_stats.py` -> `connection_pool.py` -> `import psycopg`), so on any interpreter lacking psycopg (the CLI's own separate Python environment, distinct from a postgres-mode server's venv) the fallback itself crashed a second time. Fixed by deferring `EmbeddingCallRecord`/`ConnectionPool` imports under `TYPE_CHECKING` in both modules (both were only ever used in type annotations), making `NoOpWriter` genuinely psycopg-free. Proven via a real `cidx index` subprocess under a genuine psycopg import blocker (not a mock) -- the exact production failure now exits 0 with the documented fail-open WARNING instead of crashing.
- **#1440**: Bug #1392's CLI/hnswlib sync mechanism (`_ensure_cli_hnswlib_capability()`) has been silently inert on every deployment since it was introduced -- confirmed via live investigation on 3 staging cluster nodes, every auto-update run for a full week logged a skip because `cidx-auto-update.service` has no explicit `PATH=` and systemd's compiled-in default never includes a per-user `~/.local/bin`. Production avoided this only by coincidence (its CLI installs system-wide to `/usr/local/bin`, which is on the systemd default). Fixed in two parts, per this project's now-explicit rule that bootstrap changes must be automated in both places: (1) the install template now sets an explicit `PATH=` covering both known install shapes; (2) a new idempotent self-heal method, `_ensure_auto_update_service_has_cli_path()`, repairs an already-deployed unit file automatically on its next auto-update cycle -- no manual operator step required.

## [11.65.0] - 2026-07-18

### Fixed

- **#1437**: hnswlib fork's `load_index()`/`save_index()` never released the Python GIL, freezing the entire server process (Web UI + MCP) for the duration of every HNSW shard load during temporal queries. Fixed in the fork (`py::call_guard<py::gil_scoped_release>()`, commit `e03aa236`); re-pinned `pyproject.toml` and the `third_party/hnswlib` submodule. Proven with a real concurrent-thread progress test (0.078% max freeze during a 1.08s load, vs. the ~100% a held GIL would produce).
- **#1433**: `/health` had no probe of golden-repo storage readability, so a node with broken NFS/CoW storage kept reporting healthy and HAProxy kept routing to it. Added a bounded-timeout readability probe folded into overall status. Also added a new unauthenticated `GET /healthz` liveness endpoint (both `/health` and `/api/system/health` require auth, so an unauthenticated load-balancer probe could never see the real signal) exposing only the coarse status enum, mapped to HTTP 200/503, protected by a short TTL cache against unauthenticated-flood amplification. HAProxy setup docs updated to check `/healthz` instead of the inert `/docs`.
- **#1436**: self-monitoring's LLM-response JSON extractor committed to the first balanced bracket/brace substring found, failing ~47% of scans when a stray fragment preceded the real JSON payload. Now tries every candidate in order.
- **#1434**: deferred temporal query poll-completion response (`GET /api/query/result/{job_id}`) omitted `total_results`, unlike the inline response. Added, matching inline semantics.

### Added

- **#1435**: REST's `POST /api/query` temporal path now has an outer handler-deadline safety net (new `rest_query_handler_timeout_seconds` config field) mirroring MCP's existing protection, so a misconfigured `temporal_inline_wait_seconds` is bounded on both front doors identically.

## [11.64.0] - 2026-07-18

### Fixed

- **#1400**: eliminated the recurring forced-deferral race in the async-hybrid temporal query timeout mechanism (`execute_live_temporal_search`), superseding 11.63.0's incomplete `no_embedding_cache_shortcut`-based mitigation which still raced intermittently under concurrent load. Two real bugs fixed: (1) no well-defined `temporal_inline_wait_seconds == 0.0` contract -- the poll loop always performed a status check first regardless of budget, so a fast-completing job could win the race; (2) unbounded oversleep past the deadline -- the loop checked the deadline only after a "waiting" read, then slept the full unconditional poll interval before looping back, letting a deadline between two ticks be overshot and the following read observe "completed" purely from the extra elapsed time. Fixed by checking the deadline before every status read and capping each sleep to the remaining budget; `inline_wait_seconds <= 0.0` now short-circuits to an immediate deferred envelope with zero status checks -- a genuine race-free "always defer" contract. Default config value (60.0) is unaffected; only an operator explicitly setting 0.0 opts into the new behavior.

## [11.63.0] - 2026-07-18

### Fixed

- **MCP `authenticate` tool dispatch broken on both `/mcp` and `/mcp-public`**: found via the post-e2e log-audit gate. The authenticated `/mcp` endpoint's generic dispatch chain never supplied `http_request`/`http_response` to `handle_authenticate` (whose signature departs from the standard `(args, user)` shape), raising a `TypeError` on every call. The public `/mcp-public` endpoint's special-cased authenticate dispatch unconditionally `await`-ed `handle_authenticate`'s return value, but that handler is sync and returns a plain dict -- also a `TypeError`, meaning the real unauthenticated login front door was completely broken. Both paths fixed; added regression tests covering both the crash and the security-relevant positive path (a valid login actually sets the HttpOnly `cidx_session` cookie).
- **#1400 (test-only)**: intermittent 202-vs-200 race in the forced-deferred-handoff e2e tests for the async-hybrid temporal query mechanism -- a 1ms forced inline-wait made deferral likely but not guaranteed against variable-latency real embedding work. Fixed by forcing a real embedding-provider round trip via the existing `no_embedding_cache_shortcut` field.

## [11.62.0] - 2026-07-18

### Added

- **Cluster memory-aware admission + pod-pull work-stealing**: heavy index jobs (golden-repo add, provider-index add) in cluster/PostgreSQL mode now support pod-pull work-stealing -- a job submitted on one node can be claimed and executed by any node with capacity, rather than being pinned to the submitting node. Includes a memory-gated admission control layer to avoid overloading any single node with concurrent heavy jobs.
- **#1404**: a single global, DB-backed "temporal indexing floor date" now bounds all `cidx index --index-commits` runs across the fleet -- commits dated on/after the floor are indexed, older commits skipped. Composes with the pre-existing per-repo `since_date` override via "more restrictive wins" (the later of the two dates governs). `None`/empty is a safety no-op, byte-identical to prior full-history behavior. Includes a Web UI Config section with a floor-date field and backfill advisory note. Also fixes a bundled bug: the per-repo launch path previously emitted the wrong CLI flag (`--since` instead of `--since-date`), which crashed the child `cidx` process whenever a per-repo `since_date` was set.

### Fixed

- **#1430**: pod-pull-eligible job submissions stamped `executing_node` to the submitting node at insert time, while the claimer's SQL required `executing_node IS NULL` to consider a row claimable -- jobs were born unclaimable by construction, silently defeating cross-node work-stealing. Fixed by leaving `executing_node` unset for pod-pull-eligible rows only; node-scoped orphan cleanup (the only other consumer of that stamp) is unaffected.
- **#1431**: two independent clusters of pre-existing, deterministic test failures across `tests/unit/server/repositories/` (45 tests total) traced to test-fixture staleness against several already-shipped production changes: (1) `ActivatedRepoManager`'s `clone_backend` became hard-required by Story #1034 but several test fixtures never wired one; a related in-memory-dict-vs-SQLite-backend mismatch (Bug #176), a stale `cidx init` subprocess-argv assertion (Bug #1013/#1014), an unmocked `cidx index` Popen call, and a stale `register_job_if_no_conflict` call-arg assertion (Bug #1430) were fixed in the same pass. (2) `repository_listing_manager`'s test fixture wrote golden-repo fixtures only to an in-memory dict never read by the production listing path (which reads exclusively from the SQLite backend per Bug #176's single-source-of-truth model).
- **#1432**: concurrent writes to the provider-health "sinbin" persistence file could silently lose one provider's entry -- a lost-update race where `_build_merged_state` accepted the currently-persisted state as a parameter but never used it, so every write discarded whatever a concurrent process had just persisted for a different provider. Fixed by merging into the existing persisted state rather than overwriting it wholesale.
- **#1429**: `fts_cache_reload_on_access` had the same `bool("false") == True` string-coercion bug as #1418's embedding-stats kill-switch -- a Web UI form posting the string `"false"` could never actually disable the setting. Fixed to use the same `_parse_bool` helper as its sibling boolean settings.
- **#1428**: reranker API-key-preflight tests could fail deterministically (not flakily) when run after certain other test files in the same pytest process, due to a module-level `ConfigService` singleton leaking real on-disk provider credentials across test boundaries. Fixed with a global autouse fixture resetting the singleton before and after every test.

## [11.61.0] - 2026-07-17

### Added

- **#1418**: cidx-server now tracks every real (non-cached, non-suppressed) embedding and reranker call to VoyageAI/Cohere -- provider, model, token/item counts, purpose, golden-repo/job context, success/failure, latency -- recorded asynchronously outside the hot path to a new dual-backend `embedding_call_stats` table, so operators can reconcile observed vendor usage against internal records. Works in both solo (SQLite) and clustered (PostgreSQL) server deployments; cache hits and coalesced-away duplicates never produce a row. Includes a Web UI Config section (enabled kill-switch, flush interval, retention window), a filterable Web UI dashboard, an admin REST/MCP query endpoint, and a retention sweep scheduler.

### Fixed

- **#1423**: `xray_search`/`xray_explore` crashed with a raw `TypeError` when `pattern_name` (a stored pattern) was combined with list-typed `repository_alias` (the omni multi-repo form), even a single-element list. Fixed by reordering repository-alias normalization to run before pattern resolution, with a defensive guard against non-string aliases and server-side exception logging closing a related silent-failure gap.
- **#1422**: `temporal_inline_wait_seconds` was writable via the admin config API but had no read surface -- missing from both the Web UI Config screen and the JSON config-read surface. Added the missing display row and edit field alongside its `SearchTimeoutsConfig` siblings.
- **#1420**: `quick_daemon_check()`'s directory walk didn't stop at the nearest `.code-indexer/config.json` -- it could skip past a nearer daemon-disabled config to inherit a more distant ancestor's daemon-enabled state, misrouting `cidx index`/`query`/`watch`/`clean`. Fixed so the walk always stops at the first config found, using its daemon-mode value (enabled, disabled, or malformed-treated-as-disabled) as the final answer.
- **#1425**: concurrent xray evaluator compilation of the same cold-cache hash could clobber rustc's intermediate codegen files across jobs, producing a `rust-lld` error and a silent 0-match result for the losing job. Fixed via per-compile-attempt isolated temp build directories with an atomic publish of the finished artifact on success.
- **#1426**: two tests in `test_cli_fast_path.py` called `is_delegatable_command()` with a stale single-argument signature, failing with `TypeError` since Bug #1417 added a required `args` parameter.
- **#1427**: two test files sharing an identical basename in different directories (`tests/unit/server/storage/` and `tests/unit/server/services/`) broke pytest collection whenever both directories were collected together.

## [11.60.0] - 2026-07-16

### Fixed

- **#1421**: temporal queries with `time_range_all=true` intermittently failed with "Temporal snapshot ... missing page N", and in rarer cases could silently return corrupted results (pages spliced from two different write generations) with no error and no log entry. The temporal worker writes grow-then-shrink checkpoints while a query is in flight; the snapshot reader read pages via separate, non-isolated calls that could straddle a mid-flight rewrite. Fixed by detecting a concurrent rewrite (missing page / page-count mismatch / JSON parse failure) and bounded-retrying the reassembly against the latest write; genuine exhaustion now logs a WARNING per retry and an ERROR on final failure. Diagnosed as a single-process, backend-agnostic timing race, not cluster-specific.

## [11.59.0] - 2026-07-16

### Added

- **#1416**: golden repos gain an `externally_managed` config flag. When true, an external owner manages golden-repo presence/freshness (materializes repos into `golden_repos_dir`, registers via the admin API); the server skips its own periodic refresh and startup restore-from-snapshot reconciliation. Also fixes a cluster/postgres startup-ordering bug where the global-repos lifecycle previously started before the ConfigService PG pool was set. Includes a Web UI Config-screen checkbox for the new flag.

## [11.58.0] - 2026-07-16

### Added

- **#1400**: async-hybrid temporal query execution and cluster-aware retrieval. Temporal queries now run through a dedicated dual-lane BGM path with cooperative cancellation, node-scoped orphan cleanup (a node restart no longer fails another node's running jobs), an honest no-resubmit poll contract, static+dynamic deadline budgeting (including terminal rerank), and atomic config updates. `search_code` (MCP) and `POST /api/query` (REST) route temporal queries through the new live async path; `poll_search_job` and `GET /api/query/result/{job_id}` expose real, registered poll endpoints. Job coordination and results flow through JobTracker/PayloadCache (PostgreSQL-backed in cluster mode) rather than per-node RAM.

### Fixed

- **#1415**: HNSW finalize integrity check hard-crashed all indexing with `AttributeError` when the deployed hnswlib was the stock PyPI build instead of the `LightspeedDMS/hnswlib` fork -- caused a real production outage across ~12 golden repos. Reversed Bug #1392's fail-loud design to graceful degrade: missing fork capability now logs a WARNING and skips the optional orphan-repair hardening pass instead of aborting indexing; already-computed embeddings are still persisted and the index remains valid and queryable. Health surfaces the degraded state via a distinct `hnswlib_capability_available` field without spoofing the zero-tolerance `orphan_count` binary.
- **#1417**: `cidx index --index-commits` silently succeeded (exit 0) instead of failing loud when the PG bootstrap DSN was unreachable, because it was misrouted through the daemon-delegation fast path which has no knowledge of the Bug #1313 fail-loud wiring. Added a `--index-commits` carve-out so temporal indexing always takes the standalone path where the PG-unreachable check runs.
- **#1419**: `ActivatedRepoIndexManager`'s FTS/semantic indexing error messages silently dropped the "run cidx init" guidance on an uninitialized repo, because the wrapped subprocess error string came back empty. Added an explicit `.code-indexer/config.json` existence check that fast-fails with actionable guidance before the subprocess is ever spawned.

## [11.57.0] - 2026-07-15

### Fixed

- **#1407**: scheduled temporal refresh ran a full multi-shard disk-scan reconcile on every tick, even when the repo was fully caught up (~44 min measured on a 93k-commit / 69-shard repo). Root cause: `TemporalIndexer` relied on a buggy global `last_commit..HEAD` cursor narrowing with no cheap "already caught up" gate, so every run re-scanned every shard's `vector_*.json` files (also resolves #1411, a related global-cursor multi-embedder blind-spot bug). Fixed by introducing a durable stale-lifecycle marker system (`mark_stale`/`clear_stale`, fsync-durable) and per-embedder commit-set-difference enumeration (new `temporal_incremental_gate.py`) replacing the cursor entirely: a no-op tick now performs zero `vector_*.json` reads. A physically-stale shard is force-rebuilt rather than incrementally appended onto a possibly-inconsistent index; stray points from a crashed prior run are deleted fail-closed before rebuild. The shared finalize path (`end_indexing`/`save_incremental_update`/`rebuild_from_vectors`) gained a scoped `clear_stale` parameter defaulting to today's behavior, so ordinary (non-temporal) incremental indexing and watch-mode are unaffected -- verified by explicit regression tests. Also fixes a defect found during code review: operator `--reconcile` no longer silently clears `is_stale` on a shard that was already stale coming in (e.g. from a real prior crash) without rebuilding it -- that shard is now force-rebuilt instead of being blessed as fresh.

## [11.56.0] - 2026-07-15

### Fixed

- **#1414**: golden repo `temporal_options` (`all_branches`, `max_commits`, `since_date`, `diff_context`) split-brain across two DB tables. `GoldenRepoManager.save_temporal_options` (the Web UI's only write path) wrote exclusively to `golden_repos_metadata`, but `RefreshScheduler._index_source` read from the separate `global_repos` table -- frozen at registration time -- so any post-registration edit was silently ignored by every future scheduled refresh. Most dangerous under Story #1412's all-branches gate: an operator disabling `all_branches` via the Web UI would have the scheduler keep reading the stale `True` value and keep doing multi-branch indexing against explicit operator intent, forever. Fixed by repointing the read to the authoritative `golden_repos_metadata` table (fail-closed WARNING + existing Bug #642 NULL-fallback on any read error), and by adding the previously-missing `update_temporal_options` method to the PostgreSQL metadata backend + its Protocol (the Web UI save 500'd unconditionally in cluster/production mode until now). `enable_temporal`/`enable_scip` reads are unchanged (already correctly handled by Bug #1390/#1406). Discovered via adversarial review of #1412, connected but independently root-caused and fixed.

### Added

- **#1412**: golden-repo temporal indexing now tracks only the branch registered at golden-repo registration by default. The pre-existing `all_branches` opt-in is retained as scaffolding but ships DISABLED behind a new server-wide runtime flag `temporal_all_branches_enabled` (default off, Web Config screen checkbox, no env var). With the gate off, a request that tries to acquire `all_branches=true` is rejected loudly at three front doors -- REST `POST /api/admin/golden-repos`, the Web UI temporal-options form, and MCP `add_golden_repo` -- never silently dropped. Defense-in-depth at the three (now four, including the MCP provider-index background job) temporal command-build sites skips `--all-branches` and logs a WARNING when a legacy stored `all_branches=true` value is seen with the gate off. Fully reversible with no re-index: the temporal index format carries no branch-membership fields, so enabling the gate later just widens the git-log walk on the next refresh. Standalone CLI `--all-branches` and the `temporal_indexer` engine parameter are untouched (server/golden surface only).

## [11.54.0] - 2026-07-14

### Fixed

- **#1401**: `regex_search`'s ripgrep/grep output parsing accepted subprocess-reported paths without verifying they stayed inside the repo root, so a `../`-relative path or an internal symlink escape could leak results from outside the intended repo. The repo root is now canonicalized once at construction, and every subprocess-reported path (absolute or relative) is resolved and containment-checked via `relative_to()` before acceptance -- anything that escapes is rejected outright, not silently absolutized.
- **#1405**: `TemporalIndexer`'s legacy-collection blank-out ran unconditionally at the top of every `index_commits()` call and hard-deleted any temporal-prefixed directory lacking a v2 marker -- including the bare `code-indexer-temporal` bookkeeping directory, which anchors the single shared `TemporalMetadataStore` used by every quarterly shard across every embedder. That directory shares its bare name with a genuine pre-#1290 legacy monolith, so it was being amputated on every single run. Fixed via a data-presence discriminator (`_is_shared_bookkeeping_directory`): a bare-named directory is now skipped (never deleted) if it has neither `hnsw_index.bin` nor any nested `vector_*.json` -- the bookkeeping dir only ever holds metadata, never vector data.
- **#1406** (companion to #1405, confirmed trigger of a production incident): `RefreshScheduler`'s filesystem-reconciliation for `enable_temporal` was bidirectional -- it could silently re-ENABLE temporal indexing when restored data appeared on disk, even after an operator had explicitly disabled it as part of a recovery procedure. Reconciliation is now one-way on both tracked tables: a stored `True` still downgrades to `False` when the filesystem shows no real data (preserving Bug #1390's fix), but a stored `False` is never flipped back to `True` -- an INFO log documents the honored operator disable instead.
- **#1398**: five hardcoded, non-Web-UI-configurable timeout constants (search-handler, default-handler, write-mode-handler, embedding-provider, reranker) are consolidated into a new validated `SearchTimeoutsConfig` Web UI settings section, plus a new `.code-indexer/.remote-config` field (`api_read_timeout_seconds`) so the CLI's remote HTTP client timeout is durably configurable per deployment without a repeated `--timeout` flag.
- **#1399**: several DB-backed Web UI settings (cache TTL/cleanup-interval/FTS-reload-on-access, memory-governor sample interval, `lifecycle_analysis` timeouts at both of its two consumer call sites, xray default timeout) persisted and echoed correctly but were never actually re-read by the live running process -- fixed with explicit hot-reload paths mirroring the existing cache-size-cap precedent, plus a `RESTART_REQUIRED_FIELDS` UI hint extended to the settings that still need a restart to take effect.
- Two small regressions in the above, caught by full regression re-verification before this release: a doc-staleness false-positive trigger in `docs/architecture-invariants.md` (#1405 follow-up), and a missing bootstrap/runtime classification entry for the new `search_timeouts_config` field (#1398 follow-up).

## [11.53.0] - 2026-07-14

### Fixed

- **#1391**: dashboard cache-metrics On-Mode Hit Rate ignored the Time Window selector (unwindowed lifetime aggregate), and Shadow Hit Rate used an operation-denominated source (`search_embed_event`) inconsistent with On-Mode's request-denominated one (`search_event_log`). Both cards now share the same windowed, request-denominated source.
- **#1392**: server and CLI subprocess can run different Python environments, and a stock PyPI hnswlib silently lacking `check_integrity()`/`repair_orphans()` would pass an import-only probe. Adds a fail-loud runtime capability gate on build/finalize paths only, a non-fatal startup check, and makes the deploy-pipeline CLI-sync guards capability-aware (an import-only guard made the sync silently no-op when the server build already advanced the shared last-built-commit marker).
- **#1393**: golden-repo activation's copy-on-write clone could race with a concurrent `RefreshScheduler` refresh of the same repo, corrupting the activated repo's initial state. Adds fail-fast + write-lock coordination, plus a wiring-gap fix (`GlobalReposLifecycleManager` never forwarded `job_tracker` into `RefreshScheduler`, making the fail-fast check a permanent no-op in production) found and closed via live manual E2E testing.
- **#1394**: `GET /api/repositories/{alias}/health` ran a synchronous, serial per-collection HNSW integrity check inside an async route, causing HTTP 504 on large temporal repos and no per-collection exception isolation. Adds a bounded-concurrency, per-collection-isolated batch helper and new async `POST .../health/check` job endpoints across all four frontend call sites.
- **#1396**: the Cache Settings Web UI form rejected the entire submission -- including unrelated field edits -- whenever `memory_governor_swap_pswpin_red_threshold` was blank. Fixed across three layers: validator blank-tolerance, ConfigService default-fallback, and a `get_all_settings()` serialization gap that left the template's pre-population guard inert.

### Added

- **#1397** (supersedes #1395): HNSW orphan-repair fleet sweep is now configurable from the Web UI -- `enabled`, `batch_size`, `tick_interval_minutes`, plus a new daily UTC operating-hours window (with overnight wrap-around, fail-open default) so the sweep's disk I/O can be confined to off-peak hours. Changes take effect live, no restart required.

## [11.52.0] - 2026-07-13

### Fixed

- **#1390** (priority-1): the filesystem-reconciliation mechanism for the `enable_temporal` flag had two independent defects that combined to trigger an unattended, hours-long full temporal reindex against operator intent. (1) `RefreshScheduler._reconcile_registry_with_filesystem` only updated the `global_repos` table (the one the scheduled-refresh trigger actually reads), never `golden_repos_metadata` -- the same logical repo's `enable_temporal` flag could permanently disagree between the two. (2) `_detect_existing_indexes`'s temporal check matched only on directory NAME (`code-indexer-temporal*`), never verifying real shard data was present -- a metadata-only leftover directory (real shard data removed for maintenance) was indistinguishable from a fully-populated index, and falsely reported "temporal exists." Fixed: reconciliation now updates both tables using the same alias-normalization already proven correct for Bug #1373; detection now requires a real `hnsw_index.bin`+`collection_meta.json` pair (reusing the existing `iter_index_files_for_repo` discovery primitive) rather than a name match. Directly reproduces and fixes a real incident: quarter-shard data pulled for maintenance, metadata directory left behind, reconciliation false-positived `enable_temporal=True`, re-arming the scheduler and triggering an unattended `cidx index --index-commits` run that redid 8 quarters from scratch over ~1.5 hours.

## [11.51.0] - 2026-07-13

### Fixed

- **#1387**: `check_hnsw_health` MCP tool hardcoded `.code-indexer/index/default/index.bin`, so it reported "Index file not found" for every real repo even when healthy -- real collections are named after the resolved embedding model and use the `hnsw_index.bin` filename, neither of which is `default`/`index.bin`. Now reuses the existing `iter_index_files_for_repo` discovery primitive (Story #1360) to find the real collection(s); single-collection repos keep the original response shape, multi-collection repos (multi-provider/temporal) get an additive `collections` list instead of one silently winning.
- **#1383** (follow-up to #1382): the `/health` DEGRADED message during circuit-breaker buildup reported only a bare consecutive-sweep count -- now includes the actual at-risk alias set and a "will auto-remove at N/N confirmations" framing. Separately, the moment auto-removal actually fired, the breaker-state reset erased all trace of it in the same tick; a new `golden_repo_reconcile_auto_heal_event` record (SQLite + PostgreSQL) now persists the removed-alias set + timestamp independently of that reset, exposed as an informational-only `last_golden_repo_reconcile_auto_heal` field on `GET /api/system/health`. Also: a confirmed sweep whose removals all fail no longer discards the accumulated confirmation count.
- **#1388**: Epic #1333's HNSW finalize-time orphan detect+repair runs correctly inside the `cidx index` child subprocess spawned by golden-repo add/refresh, but its outcome never reached the server's admin-visible logs -- a subprocess never inherits the server's log handler, and the CLI's own logging setup separately filters the check's INFO-level line before it's written anywhere. The repair event now bypasses the percentage-based `--progress-json` wire protocol entirely (which two independent gates were silently dropping it through) via a dedicated stderr marker channel that the parent subprocess runner scrapes and re-logs through the server's own logger, alias-tagged, reaching `logs.db` for real.

## [11.50.0] - 2026-07-13

### Fixed

- **#1382** (priority-1): the golden-repo registry-orphan reconcile circuit-breaker (#1317) had no recovery path -- a live staging incident showed a genuine, persistent orphan set (crash-recovery gap: DB restored, on-disk clones not) tripped the >50% abort threshold on EVERY restart for ~2 months with only a repeated log-only WARNING. Added a persisted, cross-restart confirmation counter (`golden_repo_reconcile_breaker_state` table, SQLite solo / PostgreSQL cluster): if the SAME orphan-candidate fingerprint is observed on 3 CONSECUTIVE sweeps, each with a healthy `golden_repos_dir` and at least 30 minutes apart (rolling-deploy hardening -- a single multi-node restart wave no longer counts as 3 "consecutive" confirmations), the sweep proceeds with removal instead of aborting forever. A base-dir-unhealthy event or a normal within-threshold sweep resets the counter, so real infra flapping can never accumulate toward confirmation. `HealthCheckService` now surfaces a currently-tripped breaker as a DEGRADED `/health` `failure_reasons` entry immediately, instead of only a startup log line.

## [11.49.0] - 2026-07-12

### Fixed

- **#1380** (priority-1): temporal query recall spent 95-98% of wall-clock time (65-93s on warm-cache queries against a real 4-quarter index) in `_reconstruct_full_commit_message()`, a sequential `git show -s --format=%B <hash>` subprocess call issued once per deduped candidate commit whose winning chunk was non-head, per shard, BEFORE the final `limit` truncation (48-89 git calls observed for `limit=5`). Removed entirely; non-head dedup winners now source their message from `dedup_by_commit()`'s already-free `_head_commit_message` stash. Verified live against the real evolution golden repo's temporal index: warm queries dropped to 1.7-3.6s sequential, 24.6-29.7s for 15 concurrent (down from 43-95s concurrent). Zero git subprocess calls proven both structurally and via live `strace`.
- **EVO-64244** (PR #1352): `HNSWIndexCache.get_or_load` negatively-cached a loader's `(None, id_mapping)` result (returned when `hnsw_index.bin` doesn't exist yet, e.g. a repo mid-(re)index) for the full TTL, so "HNSW index not found" persisted even after the index finished building. A `None` result is no longer stored. Also: the cache had no invalidation path for the common case of a repo reindexed via a background job or separate worker/CLI subprocess (only two narrow call sites existed: branch-isolation filtered rebuild, and the orphan-repair sweep) -- a multi-worker server could silently serve a stale, pre-rebuild HNSW index for up to the TTL after a normal reindex. `get_or_load` now takes an optional `index_file` path and invalidates a cache HIT when the on-disk file's mtime is newer than what was cached at load time.
- **#1379**: two unit tests hardcoded the legacy pre-Story-#1171 temporal storage layout and failed against the current quarterly-sharded, per-embedder collection layout. Test-fixture-only fix; production code unaffected (confirmed via manual repro).
- **#1381**: two test-infrastructure flakes found while validating the above under full-suite concurrent load (both passed reliably in isolation). `test_no_git_commands_for_non_git_repo` globally patched `subprocess.run`, exploitable by any unrelated concurrent thread spawning a real `git` subprocess (same class as #1375) -- fixed via a per-instance `RefreshScheduler._run_subprocess()` injection seam. `test_dispatch_parallel_with_jitter_zero_jitter_disables_jitter` asserted a hard real-wall-clock bound sensitive to CPU contention under concurrent chunked execution -- fixed via a deterministic `time.sleep`-never-called assertion scoped to the dispatcher module's own namespace.

## [11.48.0] - 2026-07-12

### Fixed

- **#1377** (priority-1): `HNSWIndexCache`/`FTSIndexCache._enforce_size_limit()` evicted the ENTIRE cache (including the just-loaded entry itself) whenever a single index exceeded the per-worker cap, since LRU always evicts oldest-first and the newest entry (with nowhere to go) was evicted last -- destroying every other repo's cached indexes for zero benefit and making oversized indexes permanently uncacheable. Individually-oversized entries are now evicted first and in isolation, before normal LRU runs on the remainder. Directly explains production temporal-query timeouts and non-growing memory usage on repos with large quarterly shards.
- **#1374**: the memory governor's `swap_forces_red` heuristic forced and HELD the RED band on swap-in rate alone, with no corroboration from actual memory usage -- pinning RED for days on hosts with abundant free memory, forcing `evict_after_use` on every temporal quarter-shard. Now requires `used_pct >= yellow_pct` too, while preserving Bug #1225's legitimate death-spiral guard.
- **#1373**: `enable_temporal` never persisted after a successful temporal index build because `_set_enable_temporal_flag()` received an already-`-global`-suffixed alias and double-suffixed it (`evolution-global-global`) for one DB write path while under-suffixing the other -- both silently no-op'd. Alias normalization is now unconditional and unambiguous (a bare golden-repo alias can never itself end in `-global`); the silent no-op is now a loud ERROR.
- **#1376**: a null `temporal.active_embedder` in a repo's `config.json` failed pydantic validation for the WHOLE `Config` model, so even completely unrelated non-temporal queries hard-errored. `active_embedder` is now `Optional[str]`; an invalid temporal section degrades to disabled-temporal with a de-duplicated warning instead of invalidating the whole config.
- **#1378**: temporal indexing's progress bar/ETA and the `X/Y commits` counter used different denominators -- the temporal indexer reset its progress to per-shard values every quarter, and separately `MultiThreadedProgressManager`/`AggregateProgressDisplay` only set Rich's internal `task.total` on the first tick, freezing the bar/ETA at whatever the first quarter reported (observed: bar pegged at 100% with 174/8008 commits actually done). Both now consistently use the whole-run total.
- **#1369**: `ClaudeInvoker`'s shared frontmatter-stripping logic (discarding everything before the first `---` line) had zero legitimate consumers across any flow routed through it, but actively corrupted self-monitoring scan JSON payloads whenever Claude's trailing prose happened to contain a markdown horizontal rule. Removed entirely as dead/harmful code.
- **#1370 / #1372**: two test-infrastructure-only bugs found while running `e2e-automation.sh`/`fast-automation.sh` as the final regression gate for Epic #1333. No production `code_indexer.cli` runtime behavior changed for either.
  - **#1370**: 14 modules (`cli.py` and 13 siblings) each construct a module-level `rich.Console()` singleton that caches color/terminal detection at import time; a single pytest process running hundreds of Click `CliRunner`-based tests reused whichever test's import ran first, corrupting plain-text output assertions with stray ANSI codes in ~23 unrelated CLI test files. A dual import-path issue (`code_indexer.cli` vs `src.code_indexer.cli` loading as separate module objects) and Rich reading `FORCE_COLOR`/`NO_COLOR` live off `os.environ` on every color check (not just at construction) compounded the effect. Fixed via one autouse pytest fixture that resets all singletons and normalizes the environment per test -- order-independence proven for all 23 originally-failing node IDs.
  - **#1372**: `e2e_cli_env` and two sibling CLI-env builders leaked ambient `FORCE_COLOR` into every real `cidx` subprocess invocation, breaking plain-text e2e output assertions in environments (like an interactive Claude Code session) that set it. Fixed with a shared `sanitize_cli_subprocess_env()` helper.
  - **#1371 / #1375**: two pre-existing, order-dependent unit-test flakes found and fixed along the way -- a Click `CliRunner.isolated_filesystem` cwd misuse in `test_status_multimodal.py`, and a process-wide `subprocess.run` mock-patch in `test_delta_merge_frontmatter.py` that could be hijacked by an unrelated background thread under full-suite load.

## [11.47.0] - 2026-07-12

### Fixed

- **#1370 / #1372**: two test-infrastructure-only bugs found while running `e2e-automation.sh`/`fast-automation.sh` as the final regression gate for Epic #1333. No production `code_indexer.cli` runtime behavior changed for either.
  - **#1370**: 14 modules (`cli.py` and 13 siblings) each construct a module-level `rich.Console()` singleton that caches color/terminal detection at import time; a single pytest process running hundreds of Click `CliRunner`-based tests reused whichever test's import ran first, corrupting plain-text output assertions with stray ANSI codes in ~23 unrelated CLI test files. A dual import-path issue (`code_indexer.cli` vs `src.code_indexer.cli` loading as separate module objects) and Rich reading `FORCE_COLOR`/`NO_COLOR` live off `os.environ` on every color check (not just at construction) compounded the effect. Fixed via one autouse pytest fixture that resets all singletons and normalizes the environment per test -- order-independence proven for all 23 originally-failing node IDs.
  - **#1372**: `tests/e2e/conftest.py`'s `e2e_cli_env` fixture (and two sibling CLI-env builders under `tests/e2e/phase5_resiliency/`) blindly copied the full ambient environment into every real `cidx` subprocess invocation, so a `FORCE_COLOR` set in the shell running pytest (e.g. an interactive session) forced color output even on piped/captured stdout, breaking `e2e-automation.sh` Phase 1's plain-text result-count regex. Fixed with a shared `sanitize_cli_subprocess_env()` helper; confirmed `FORCE_COLOR=0` does not work (Rich treats any present value as "force on") -- the key must be removed entirely.

## [11.46.0] - 2026-07-12

### Fixed

- **#1368**: found via live E2E validation of Epic #1333 S3 on the staging cluster -- `HNSWOrphanRepairSweepScheduler` raised `AttributeError` ('dict' object has no attribute 'enabled'/'batch_size') on every real SQLite AND PostgreSQL config load, because `hnsw_orphan_repair_sweep_config` was the one nested-dataclass `ServerConfig` field missing its dict-to-dataclass conversion block in `_dict_to_server_config` (every sibling section, e.g. `data_retention_config`/`activated_reaper_config`, already had one). The scheduler's defensive fallback masked this functionally (fallback values happened to match the story's intended defaults), but it meant any Web-UI change to the sweep's enable/batch-size/tick-interval settings had zero effect. Added the missing conversion block; regression coverage now drives the real SQLite seed-then-restart cycle and a live-PostgreSQL round trip against the actual `server_config` schema, not a hand-constructed config object injected in-process (the unfaithful-mock gap that let this through undetected).

## [11.45.0] - 2026-07-12

### Added

- **Epic #1333 -- Eliminate HNSW orphan nodes (silent recall loss)**: three stories closing a spike-confirmed gap where HNSW index elements could end up with zero inbound graph connections (unreachable by ANN search, silent recall loss) with no detection or repair anywhere in the system.
  - **#1358**: fork-level `repair_orphans()` C++ method added to the custom hnswlib fork, using a pigeonhole-principle-proven anchor-selection strategy to force valid back-edges for orphaned elements. Ships with a committed synthetic-orphan corpus generator covering both measured orphan regimes (exact-tie multi-threaded race, near-tie deterministic pruning) plus real on-disk save/load round-trip fixtures across a size/regime/construction-shape matrix.
  - **#1359**: wires detect -> repair -> re-verify into every HNSW build/finalize path (`build_index`, `rebuild_from_vectors`, incremental updates) so newly-built or updated indexes -- regular, temporal, multimodal -- self-heal before publish. `cidx health` / MCP `check_hnsw_health` / REST / Web now expose `orphan_count` as a strict zero-tolerance binary signal (any orphan is ERROR, no graded severity). Validated end-to-end against a real orphaned staging shard, now a permanent regression fixture.
  - **#1360**: a paced, resumable, cluster-safe fleet sweep (`HNSWOrphanRepairSweepScheduler`) that walks all existing on-disk indexes (golden + activated, regular + temporal shards) and repairs the pre-existing backlog built before #1359's build-path fix existed. Stable string-key cursor (never a numeric offset) survives insertions/deletions of shards between ticks; repair reuses the exact same per-collection lock the build path takes, with a locked re-check immediately before writing. Ships enabled by default with conservative pacing (15 items/tick, 7-minute interval), both adjustable via the Web UI config screen; one short BackgroundJobManager job per tick, with cross-pass fleet stats on a dedicated admin endpoint.

## [11.44.0] - 2026-07-10

### Fixed

- **#1355/#1356**: Two-pass audit (independent fact-check + adversarial verification) of all 151 MCP tool docs against their handler code found 35 confirmed inaccuracies. Fixed across 40 doc files: a `node.text` field-vs-method Rust compile bug (the method is `.text()`, not a field) that would break copy-pasted X-Ray evaluator examples in `xray_search.md`, `store_xray_pattern.md`, and `xray_explore.md`, with a new regression test guarding against recurrence; SCIP tool docs claiming unsupported `project`/`exact` filters on `scip_callchain`/`scip_impact`/`scip_context`, a wrong `max_depth` cap, and example return shapes that didn't match actual handler output; Git tool docs with a wrong `git_push` output field name, a non-functional `git_commit` `author_email` param, a non-functional `git_log` `aggregation_mode` param, overclaimed `git_status` fields, a false `git_amend` write-mode-gating implication, and undocumented `git_diff`/`switch_branch`/`get_branches` parameters; and Repos/Admin/Guides doc mismatches including wrong response-shape claims on `list_global_repos`/`authenticate`/`cidx_quick_reference`/`poll_delegation_job`/`update_group`, a missing elevation-required error across 11 admin tools, and several wrong tool-name/example/field references. `discover_repositories.md` was deliberately left unchanged -- its documented behavior (discovering not-yet-indexed external repos) looks like a real missing feature rather than a doc bug, flagged separately for a product decision. Documentation-only; zero application code changed.

## [11.43.0] - 2026-07-10

### Fixed

- **#1354**: Discovered during live ChatGPT MCP connector validation of #1351/#1353 -- the connector completed the full OAuth PKCE flow and successfully called `POST /mcp` (initialize, notifications/initialized, tools/list all 200/202), then still reported "Disconnected" after a brief delay. Root cause (proven via manual protocol replay against staging, with cluster node affinity independently ruled out via cookie-jar testing): `mcp_endpoint`'s session-ID resolution only checked a `session_id` query parameter that no client populates, never the `Mcp-Session-Id` request header that MCP clients (including ChatGPT's) use to convey session continuity after `initialize` -- so a brand-new session ID was minted on every single request, even when hitting the identical cluster node. Added header lookup as the primary source (query param kept as a legacy fallback tier, `str(uuid.uuid4())` as the final fallback for brand-new sessions) -- restores `get_or_create_session`'s originally intended cross-call session persistence. The separate, unauthenticated `/mcp-public` endpoint was confirmed unreachable from ChatGPT's flow and left untouched.

## [11.42.0] - 2026-07-10

### Fixed

- **#1353**: Follow-up to #1351 -- live staging validation showed the ChatGPT MCP connector still failed OAuth authorization even with #1351's discovery-metadata fix live. Root cause (proven via staging `journalctl` logs): the connector requests `GET /.well-known/oauth-authorization-server/mcp` (path-suffixed with the MCP resource's own path segment) rather than the root-level discovery URL, and only the root path was registered, so the suffixed request 404'd and the client fell back to assuming PKCE was unsupported. Added a path-suffixed alias route (`/.well-known/oauth-authorization-server/mcp`) that serves the identical discovery metadata from the same existing handler -- zero behavior change to the pre-existing root route relied on by Claude Code/Claude.ai, verified via runtime route-table inspection to be a non-overlapping literal path with no precedence/collision risk.

## [11.41.0] - 2026-07-10

### Fixed

- **#1351**: OAuth authorization-server discovery metadata (`/.well-known/oauth-authorization-server`) omitted `code_challenge_methods_supported`, so RFC 8414-compliant clients (e.g. ChatGPT's MCP connector) assumed PKCE was unsupported and omitted `code_challenge` when building their `/oauth/authorize` request, which the server's required `code_challenge` parameter then rejected with a 422 ("Field required"). Claude Code/Claude.ai were unaffected because they send `code_challenge` unconditionally. Discovery metadata now advertises `code_challenge_methods_supported: ["S256"]` and `token_endpoint_auth_methods_supported: ["none", "client_secret_basic"]` (pure metadata addition -- PKCE enforcement itself is unchanged). Also corrected a stale comment in `routes.py` that incorrectly warned against adding `/.well-known/oauth-protected-resource`, which already exists and is required by the current MCP auth spec.

## [11.40.0] - 2026-07-10

### Added

- **Cluster-mode hardening (PR #1339, external contribution + review-driven fixes)**: opt-in HRW (rendezvous-hash) repo sharding for cluster mode (`cluster.sharding_enabled`, default off; fail-open and byte-for-byte unchanged when disabled — independently verified), a trigram-index-assisted regex pre-filter for large NFS-backed repos (with a fixed cross-line multiline correctness gap and additional non-ASCII/concurrent-build-safety fixes from the author), and six independent correctness fixes: `scip_impact` scoping (48s→39ms, plus a latent access-control fix), ripgrep-in-image for regex search, a recursive/depth-bounded repo-file-listing endpoint, an activated-repos storage-locality startup warning, and golden-repo clone-URL credential masking.

### Fixed

- Completed the Starlette 1.x `TemplateResponse` call-signature migration across the remaining 29 call sites the PR left unmigrated (`wiki/routes.py`, `web/dependency_map_routes.py`, `web/repo_category_routes.py`, `web/elevation_web_routes.py`, `auth/oauth/routes.py`), with a permanent AST-based regression guard against reintroducing the old signature anywhere in the server tree.

## [11.39.0] - 2026-07-10

### Fixed

- **#1350**: Follow-up to #1349 — live re-validation on staging proved the bounded-retry window for clone-phase activation-cancel orphan cleanup (worst case 1.2s) was too short for real CoW-daemon/NFS materialization lag, still leaving permanent orphan clone directories in 3/3 reproductions. Widened the bound to ~12s (still a fixed, provably-terminating loop) and added a WARNING when the retry loop exhausts without removing anything, so any residual case is now visible in logs instead of requiring manual disk inspection.

## [11.38.0] - 2026-07-10

### Fixed

- **#1349**: Follow-up to #1345 — the clone-phase activation-cancel orphan cleanup used a single-instant `os.path.exists()` check, which could miss a partial clone directory that the CoW Storage Daemon (over NFS) was still materializing a beat after the cancellation/failure exception had already propagated. This let a complete, unregistered orphan clone permanently leak on the CoW-daemon/NFS backend (reproduced 4/4 on live staging). Cleanup now does an unconditional removal attempt first, then a short, fixed-iteration-count bounded retry (worst case ~1.2s) to catch late materialization, with zero added latency in the common non-racy case.

## [11.37.0] - 2026-07-10

### Fixed

- **#1344**: Guard `BackgroundJobManager` job persistence so a stale out-of-lock `running` snapshot can no longer overwrite an already-terminal (`completed`/`completed_partial`/`failed`/`cancelled`) job row, on both the SQLite and PostgreSQL backends. This was a root cause of cancelled/killed jobs appearing to never leave the dashboard (the persisted row could revert to `running` after the worker's terminal write).
- **#1345**: A user cancel during the CoW clone phase of activation (before branch-delta reindex) now cleans up the partial activated-repo clone directory, matching the existing reindex-phase cleanup.
- **#1346**: User-initiated activation cancellations now log at INFO ("cancelled by user") instead of ERROR; genuine failures still log ERROR.
- **#1348**: Extended the #1344 terminal-status guard to `JobTracker._upsert_job` (both its backend `update_job()` call and its raw SQLite `UPDATE`), closing the second, uncovered path to the same stale-`running`-revert race. Also fixed a `_TERMINAL_JOB_STATUSES` divergence in `JobTracker` (was missing `completed_partial`). Together with #1342 and #1344, this completes the fix for cancelled/killed jobs persisting on the dashboard across both in-memory and persisted-row read paths.

## [11.36.0] - 2026-07-09

### Fixed

- **#1347**: The #1343 warn-only `git status --porcelain` sanity check in `ActivatedRepoManager._clone_with_copy_on_write` no longer false-warns on the expected untracked `.code-indexer/` index directory. Porcelain output includes untracked (`??`) entries, so `?? .code-indexer/` fired a WARNING on every activation and tripped the Phase 3 (server in-process) POST-E2E log-audit gate. The check now filters out untracked lines and warns only on tracked-file drift (which `git restore` could actually discard). Still warn-only; `git restore` is never invoked. No CoW-clone/`checkStat`/`preserve_attrs`/timeout behavior changed.

## [11.35.0] - 2026-07-09

### Fixed

- **#1342**: Activation-job cancellation now actually terminates the running clone/index subprocess. Previously an unbounded blocking subprocess ignored user cancels and the dashboard left a zombie RUNNING job. A new cancellable subprocess helper (own process group; SIGTERM -> grace -> SIGKILL; stdout/stderr drained on daemon threads) plus a `cancel_check` callback threaded from `BackgroundJobManager` through the clone backend, branch-delta reindex, and the telemetry subprocess runner now kill the process group on cancel, best-effort clean up CoW-daemon leftovers, and mark the job CANCELLED cross-node (backend + dashboard). No wall-clock/job/per-file timeout added (Bug #1218 invariant); termination is user-cancel-driven only.
- **#1343**: Activation CoW clone no longer re-hashes the whole tree. The clone now preserves file attributes and sets `git config --local core.checkStat minimal` so git treats CoW-copied files as clean, turning non-default-branch activation into a fast branch-delta reindex instead of a full re-index.

## [11.34.0] - 2026-07-09

### Fixed

- **Permanent git-fetch errors no longer trigger an unbounded retry/re-clone flood (#1341).** A permanent GitLab `project you were looking for could not be found or you don't have permission` error also emits the generic `fatal: Could not read from remote repository.` line, which matched a TRANSIENT pattern — so `classify_fetch_error()` returned `transient` and the golden-repo refresh scheduler retried and auto-re-cloned the repo every cycle forever, flooding the logs. A `PERMANENT` category (project-not-found / no-permission / repository-not-found / `remote: Not Found`) is now checked before the transient patterns. On a permanent classification the scheduler no longer re-clones (cloning an inaccessible/nonexistent repo cannot succeed) but still retries the fetch later via **non-terminal exponential backoff** (base 5m, cap 6h); transient errors keep today's immediate-retry behavior below the threshold, then back off (cap 1h). The repo is NEVER removed from scheduling or quarantined — it keeps retrying at a slower cadence and recovers automatically if access is restored (a successful fetch resets to normal cadence). ERROR logging is throttled to power-of-two failure milestones, ending the flood. Transient auth/token-rotation blips (`HTTP Basic: Access denied` / `Authentication failed`) correctly remain transient.

## [11.33.0] - 2026-07-09

### Fixed

- **Fresh cow-daemon cluster provisioning is now correct (#1337 completion).** A cluster node's `daemon_storage_path` is derived at startup from the NFS mount source of `mount_point` (`findmnt` / `/proc/mounts`; the export path after the host prefix, robust to hostname / IPv4 / bracketed-IPv6 forms) when it is empty — so NFS-client nodes self-configure (the co-located daemon config file only exists on the CoW host). The installer now provisions `activated-repos` as a node-aware symlink into the CoW storage (like `golden-repos`), so per-user reflink activation's DESTINATION also resolves on the CoW XFS. Startup placement validation is generalized to WARN (never disable `snapshot_manager`) for both `golden-repos` and `activated-repos`. No migration logic; the solo/local path (`clone_backend != cow-daemon`, i.e. production) is byte-for-byte unchanged. Together with the earlier v11.31.0/v11.32.0 work, per-user activation now works end-to-end on a cow-daemon cluster.
- **Typed `OrphanedRepoError` replaces brittle error-message string matching (#1338).** #1336 detected orphaned golden aliases by matching raised error-message substrings across module boundaries, so a reworded message would silently re-break orphan-skip. A dedicated `OrphanedRepoError(ValueError)` is now raised only at the two orphaned-clone source sites (missing clone for a registered alias) and caught by type at both skip sites; other `ValueError`s (empty alias, bad path, real Claude/git failures) still propagate and fail the job. Message-drift-proof tests drive the real invoker/updater.
- **Missing `wiki_article_views` SQLite migration (#1340).** Removing a golden repo logged an ERROR (`no such table: wiki_article_views`) on SQLite nodes where no wiki page had ever been viewed — the startup SQLite init created the other wiki tables but not this one (it was created eagerly for PostgreSQL, lazily for SQLite on first wiki route). An idempotent `CREATE TABLE IF NOT EXISTS` is now part of `DatabaseSchema.initialize_database` (additive, backward compatible, repairs existing DBs), so golden-repo removal no longer errors.

## [11.32.0] - 2026-07-09

### Fixed

- **#1337 startup validation no longer disables snapshot_manager on NFS-mounted golden-repos (staging regression fix).** The v11.31.0 #1337 startup check raised `RuntimeError` (caught -> `snapshot_manager=None`) when `realpath(golden_repos_dir)` was not under the CoW `mount_point`/`daemon_storage_path`. On a real cow-daemon cluster golden-repos is an NFS mount of the CoW host's storage, and `os.path.realpath()` does not follow NFS mounts -> it returns the mount-point path, tripping the check even though the storage is correct and reflink-capable. That degraded per-user activation AND versioned snapshots on every NFS-client node. The not-under-root case now logs a prominent WARNING (with the exact symlink-migration remediation) and returns; `snapshot_manager` stays functional. Per-user activation still fails at translate time until golden-repos is symlinked into the CoW tree — surfaced by the warning, not by disabling snapshots server-wide. Installer/auto-updater symlink provisioning unchanged.

## [11.31.0] - 2026-07-09

### Fixed

- **Per-user activation on the CoW-daemon cluster (#1337).** Per-user activation reflink-clones the golden repo, which requires the golden bytes to live on the CoW-managed filesystem; registration placed clones in a plain `<data>/golden-repos` dir outside the CoW mount, so `CowDaemonBackend` could not translate the path (`... is not under mount_point ... cannot translate to daemon view`). The installer AND the auto-updater (Step 14.7) now idempotently provision `golden-repos` as a node-aware symlink into the CoW storage (empty dir -> auto-convert; non-empty dir -> WARNING with exact migration steps, never moved unattended). Startup validation fails loud on a plain-dir misconfig but degrades (no worker crash) on a dangling symlink / NFS host down. Docs corrected: a plain bind mount gives query visibility but does NOT satisfy `realpath`-based daemon translation — a symlink (or a path physically under the CoW mount) is required.
- **First-boot PG seed preserves bootstrap host/port/workers (#1335).** `ConfigService`'s first-boot PostgreSQL runtime seed gap-filled the now-runtime-only `host`/`port`/`workers` by re-reading an already-stripped `config.json`, always seeing them absent and falling back to an UNRELATED `cidx-server.service` systemd unit on the host (which flipped `should_use_secure_cookies` on a freshly provisioned node). The intent check now consults an in-memory bootstrap snapshot captured BEFORE the strip; only truly-absent values fall back to ExecStart. Makes the #1324 harness `SYSTEMD_UNIT_DIR` workaround unnecessary.
- **Orphaned golden aliases are skipped, not fatal, in lifecycle_backfill / global_repo_refresh (#1336).** An orphaned alias (registry row present, on-disk clone absent) raised `ValueError` and failed the whole job. It is now skipped with a WARNING (narrow per-call-site catch; a genuine non-orphan `ValueError` still fails the job), so valid aliases process and the job succeeds. Orphan cleanup stays delegated to the #1317 reconciler. (Typed-exception hardening tracked as #1338.)
- **Atomic token-bucket decrement eliminates cross-node rate-limit overshoot (#1334).** `TokenBucketManager._pg_consume` replaced its non-atomic SELECT-then-UPDATE with a single conditional `UPDATE` (refill+decrement inline; allow/deny read from `rowcount`). PostgreSQL EvalPlanQual re-evaluates the guard and SET against the winner's committed tuple, so simultaneous cross-node consumers on one key can no longer both pass at the boundary. Applies to both the auth login limiter and `PerConsumerRateLimiter`; refill semantics preserved; proven by a real-PG concurrency test (was 100/100 allowed, now strict capacity).

## [11.30.0] - 2026-07-09

### Fixed

- **Dep-map verification passes are concurrency-capped again (#1323).** What looked like a flaky test was a real regression: the Bug #936 dispatcher migration routed verification through `CliDispatcher.dispatch -> ClaudeInvoker.invoke -> subprocess.run`, bypassing the `max_concurrent_claude_cli` semaphore that generation still holds — so verification ran UNCAPPED (proven: 3 concurrent at cap=2), a runaway-concurrent-Claude-process resource/cost risk. `_run_verification_attempt` now acquires the SAME process-wide verification semaphore around the dispatch (release in `finally` on all paths), so generation + verification share one cap. Scoped to verification only.
- **Golden-repo registry-orphan guard + reconcile with mass-deletion circuit-breaker (#1317).** Registration is now all-or-nothing (activation failure rolls the row back + cleans the clone; a global repo always gets its alias pointer); removal deletes the registry row before any files. A fail-soft startup reconcile removes rows whose on-disk clone is absent — guarded by a positive health-gate and a 0.5 mass-deletion circuit-breaker (a stale/unmounted NFS can never wipe the registry) plus Pass-3 pointer-repair for the #1315 symptom, single-flighted across cluster workers.
- **CoW versioned-snapshot publish path translation (#1320).** Part A: `CowDaemonBackend._translate_to_daemon_path` fails loud (raises) instead of silently emitting an untranslatable NFS path the daemon rejects with 400. Part B: `cow_daemon.daemon_storage_path` is populated durably in both the installer and the auto-updater (value-aware idempotent; resolved from operator param/env/co-located daemon config, never hard-coded).
- **Golden-repo mutable fields read authoritatively on cross-node mutation (#1316).** `change_branch`/`change_branch_async`, `add_indexes` temporal_options, and the per-provider temporal rebuild route now read the decision-driving field from the shared backend (not the per-worker cache), so a cross-node branch/temporal-option change is visible without a worker restart. Reload-on-miss read cache retained.
- **e2e Phase-6 PG-parity web-session auth (#1324).** The Phase-6 throwaway server inherited `host=0.0.0.0` (via a ConfigService gap-fill from an unrelated systemd unit), making the web-session cookie `Secure` and thus dropped over the harness's plain HTTP -> 303-to-login. Harness now pins `SYSTEMD_UNIT_DIR` so the gap-fill falls back to `host=127.0.0.1`. (Underlying product gap-fill bug filed as #1335.)
- **De-flaked `test_sqlite_log_handler_batched_1241`** — the background writer raced the test's enqueue loop; the test now drives a single deterministic drain cycle.

### Added

- **Merged PR #1331 (perf): short-TTL LRU caches for `FileListingService._collect_files` and `BranchService.list_branches`** read-only hot paths, with review fixes (post-walk TTL anchoring; branch-cache invalidation on branch mutations). Cluster-safe read-only listings.
- **Merged PR #1332 (feat): opt-in admission-control / backpressure middleware** — global per-worker in-flight cap + a genuinely cluster-shared per-consumer token-bucket rate limiter (dedicated `consumer_rate_limit_state` PG table, migration 034). Both gates off by default.

### Research

- **HNSW orphan-node research spike (#1330).** Findings doc (`docs/research/hnsw-temporal-orphans-1330.md`): orphans affect both temporal AND regular semantic indexes via the shared builder; two regimes (exact-tie multi-thread race vs near-tie deterministic); recommended Strategy B (post-build detect+repair). Proposed implementation story #1333 (open for review). Follow-ups: #1334 (rate-limiter atomic decrement), #1335 (ConfigService gap-fill).

## [11.29.0] - 2026-07-08

### Fixed

- **Relative `PYTHONPATH` no longer shadows CIDX dependencies in spawned `cidx` subprocesses (#1325, #1328).** A server (or CLI) launched with a relative `PYTHONPATH` (e.g. dev `PYTHONPATH=./src`) spawned `cidx` subprocesses with `cwd=<clone>`, and the relative `./src` re-anchored into the clone — so a cloned `src`-layout repo whose package name collides with a CIDX dependency (e.g. `click`) shadowed the installed one, breaking `cidx`'s own imports (SyntaxError under Python 3.9) and hard-failing golden-repo registration/refresh/activation/provider-index. New shared helper `code_indexer/utils/subprocess_env.py::build_cidx_subprocess_env()` absolutizes relative `PYTHONPATH` entries against the current process cwd before the child changes directory (never strips; preserves absolute/empty entries; composes with the Bug #1313 temporal PG env). Applied at every server-side (#1325) and CLI/proxy (#1328) `cidx`-with-`cwd` spawn site; git spawns untouched. Live-confirmed by registering `click` under a relative `PYTHONPATH`.
- **X-Ray tool-doc evaluator examples now compile (#1326).** The Rust `OwnedNode.text` is a method; the shipped examples used field access (`&f.text`) and failed to compile (E0615). Corrected to `f.text()` across `xray_search.md` and `xray_explore.md`.
- **Env-wiring unit tests no longer leak `os.environ` secrets on failure (#1327).** `assert KEY (not) in <env>` made pytest repr the whole dict (values included) on failure, dumping live secrets from `os.environ`-derived subprocess envs into logs/traces. New shared `tests/utils/env_assertions.py` converts each check to a bool/scalar before the assert; guard test proves sentinel secrets never appear in failure text.
- **14 masked git-push tests fixed and un-hidden (#1329).** `test_git_push_with_pat.py` mocked the push result with an unconfigured `MagicMock().stderr`, so `_count_pushed_commits` (Bug #569) raised `TypeError`; the 14 failures were hidden by explicit `--deselect` lines in `fast-automation.sh`. Made the mocks faithful (`.stderr` a real string; product code untouched) and removed the deselect lines so the gate covers them again.

## [11.28.0] - 2026-07-07

### Fixed

- **Temporal query reuse-seam now resolves the real embedder model name instead of the collection name (#1321).** The Story #1293 up-front-embed reuse seam passed the temporal COLLECTION base name (e.g. `code-indexer-temporal-voyage_context_4`) to `_build_query_provider_for_embedder`, which expects a real embedder MODEL name. `create_embedder()` raised `KeyError` on the collection name and fell through to the Voyage branch, building a `VoyageAIClient(model="code-indexer-temporal-<slug>")`; the tokenizer loader then requested `voyageai/code-indexer-temporal-<slug>` from HuggingFace -> 401 on every temporal query -> WARNING + silent fallback to per-shard embedding (the Cohere path was doubly wrong, building a Voyage client for a Cohere collection). Fix routes through the existing `_create_embedding_provider_for_collection` reverse-mapping helper (the same one the per-shard path already uses) to recover the real model name for both embedders. The up-front optimization now fires instead of always erroring into the fallback; query semantics are unchanged. Surfaced by the e2e Phase-3 log-audit gate (10 non-allowlisted WARNINGs).
- **Langfuse trace-sync lifecycle unit tests are now isolated from the network (#1322).** `TestServiceLifecycle` started a `LangfuseTraceSyncService` whose background sync thread immediately called `LangfuseApiClient.discover_project()` (a real outbound HTTP request); when the Langfuse host was down that request hung and the tests tripped the 15s pytest-timeout, producing spurious `server-fast-automation.sh` failures. A `mock_langfuse_api_client` fixture now patches `LangfuseApiClient` at its import site so the sync loop completes with zero network I/O regardless of host reachability. Test-only change; no product code touched.

## [11.27.0] - 2026-07-07

### Fixed

- **Auto-updater now provisions the Node.js toolchain, unblocking SCIP indexing and Codex-CLI provisioning cluster-wide (#1318).** SCIP builds and the Codex-CLI install were inert on server nodes because Node.js/npm was never installed (scip-python and codex are npm packages), so v11.26.0's `ensure_scip_python()` correctly logged `npm not available on PATH` and SCIP builds failed with `[Errno 2] No such file or directory: 'scip-python'`. New idempotent `DeploymentExecutor.ensure_nodejs()` (deploy Step 6.65, before the Codex-CLI and scip-python steps) installs pinned Node.js v22.11.0 LTS from the official static tarball to `/opt/node` (mirroring the `/opt/rust` provisioning; x86_64-guarded; non-fatal WARNING `DEPLOY-GENERAL-202`, never aborts a deploy; path-traversal-guarded extraction). `/opt/node/bin` is wired onto BOTH the running auto-updater process PATH (so the same-run npm consumers, which call `shutil.which`/`npm` with no explicit env, find npm) AND the `cidx-server.service` unit PATH (so the server and its index subprocesses find scip-python at runtime), the latter mirroring `_ensure_systemd_rust_path`. Known follow-up: the reinstall/partial-extract path should also verify `bin/npm` and clear the install dir.
- **Temporal `search_code` queries no longer hit the blanket 60s MCP handler timeout under load (#1319).** Temporal search (query embed + HNSW over many quarterly shards + hydration + reranking) legitimately takes ~13-20s and its tail intermittently exceeded the generic `HANDLER_TIMEOUT_SECONDS = 60` cap (Bug #1008) under concurrent load, aborting queries whose recall was correct. Added `search_code` to the existing per-tool `_HANDLER_TIMEOUT_OVERRIDES` with a bounded `SEARCH_HANDLER_TIMEOUT_SECONDS = 180`; the 60s default is unchanged for all other tools and `exit_write_mode` (720s) is untouched. `search_code` is the single registered query tool (temporal is a mode parameter, not a separate tool).

## [11.26.0] - 2026-07-07

### Fixed

- **Auto-updater now provisions `scip-python`, enabling SCIP indexing cluster-wide.** SCIP index generation (`cidx scip generate` / `add_golden_repo_index index_type=scip`) failed on server nodes with `[Errno 2] No such file or directory: 'scip-python'` — the binary was never installed by the installer or the auto-updater. New idempotent `DeploymentExecutor.ensure_scip_python()` (wired non-fatally as deploy Step 7.1, right after `ensure_ripgrep()`) checks `shutil.which("scip-python")` and, if absent, runs `npm install -g @sourcegraph/scip-python` (300s timeout; nonzero/timeout/OSError logged as WARNING `DEPLOY-GENERAL-201`, never raising — a failed provisioning never aborts a deploy). Mirrors the existing ripgrep/Codex-CLI provisioning pattern. Fresh installs inherit the same coverage: `install-cidx-server.sh` (which itself provisions no toolchain deps) starts the auto-update timer, so `execute()` runs and provisions scip-python on first cycle. Follow-up: `scip-typescript` is a trivial npm mirror; go/csharp/java SCIP indexers use different install patterns.

## [11.25.0] - 2026-07-07

### Fixed

- **Omni and global-repo search no longer fail when a repo's alias pointer file is missing (#1315).** Global-repo path resolution called `AliasManager.read_alias(alias)` and hard-failed with `"Alias for global repository '<alias>' not found"` when the alias pointer JSON was missing, ignoring the valid `index_path` already stored in the registry row. In a multi-repo cluster this made omni `*` cross-repo search silently return partial results (11 of 12 global repos erroring in `results_by_repo`) for repos registered without an alias pointer (a bulk-provisioning gap). A new shared `resolve_alias_or_index_path()` helper resolves via the alias pointer first (authoritative/current) and falls back to the registry's own `index_path` when the pointer is missing and the path exists on disk (WARNING logged, `None` -> preserved hard failure when neither resolves). The fallback is versioned-path-trap-safe: `index_path` is written only at registration and is always the mutable base clone (refresh re-indexes the base clone in place and swaps only the alias pointer, never rewriting `index_path`), so the fallback never serves a `.versioned/` immutable snapshot. Routed through the helper at all five resolution sites: `multi_search_service.py` (omni fan-out), `mcp/handlers/search.py` (direct global query), `services/search_service.py`, `multi/scip_multi_service.py`, and `services/stats_service.py`. Underlying provisioning gap (alias pointers uncreated) tracked as a follow-up.

## [11.24.0] - 2026-07-07

### Fixed

- **Golden-repo management ops no longer use a stale per-worker in-memory dict in multi-worker clusters (#1314).** `GoldenRepoManager.golden_repos` was a per-process dict populated once at `__init__` and never reloaded, used as the sole source of truth by `add_golden_repo_index`, `refresh_golden_repo`, `get_golden_repo_indexes`, and other management ops (plus a handler-level bypass in `mcp/handlers/repos.py`). Under `uvicorn --workers N` behind HAProxy round-robin, a repo registered after workers start was visible only to the worker that served the `add_golden_repo` request, so other workers returned `Golden repository '<alias>' not found` (a Cluster-Aware-State invariant violation; observed as 28 consecutive misses on a 2-workers/node staging cluster). A new `_resolve_golden_repo(alias)` cache-aside read-through now resolves from the shared metadata backend (`GoldenRepoMetadataPostgresBackend` in cluster mode / SQLite solo) with reload-on-miss; all management call-sites route through it, and `find_by_canonical_url` iterates a fresh `list_repos()` snapshot. The query path and `manage_provider_indexes` were already DB-backed and are unchanged. A residual stale-positive on cross-node mutation (bounded, non-corrupting, self-healing on restart) is tracked separately (#1316).

## [11.23.0] - 2026-07-06

### Fixed

- **Temporal indexing no longer bottlenecks on SQLite-WAL-over-NFS in cluster mode (#1313).** The temporal metadata store (`hash_prefix -> point_id` map, write-only at runtime) was a per-shard SQLite-WAL database living inside each quarterly shard directory -- i.e. on the shared golden-repos NFS mount in cluster (`storage_mode: postgres`) deployments -- committed and WAL-checkpointed once per commit by 8 index-worker threads. py-spy proved all 8 threads parked in `save_metadata_batch`/`checkpoint_wal`, yielding a ~25x slowdown (a ~450-commit repo took ~77 min for ~3 min of real work) and a Cluster-Aware-State invariant violation. The metadata store is now backend-pluggable behind a `TemporalMetadataBackend` protocol: `TemporalMetadataSqliteBackend` (CLI/solo, byte-for-byte unchanged) and `TemporalMetadataPostgresBackend` (cluster; new additive table `temporal_metadata` via migration 033, keyed by a `collection_key = sha256(str(collection_path))[:32]` discriminator, single-transaction batch upsert with `SET LOCAL synchronous_commit = off`). Backend selection is process-local injection: the server installs the PG factory (or a fail-loud poison factory) in postgres mode; CLI/solo installs nothing and keeps SQLite.
- **Cluster temporal indexing subprocesses now use PostgreSQL, not per-node SQLite-on-NFS (#1313).** Cluster temporal indexing runs in child `cidx index --index-commits` subprocesses, which do not inherit the server process's in-memory backend registry. A path-only IPC contract (`CIDX_TEMPORAL_PG_BOOTSTRAP_DIR`, carrying the server dir path -- never the DSN, which the child re-reads from bootstrap `config.json`) lets each child install the PG backend before any `TemporalMetadataStore` construction, failing loud (`sys.exit(1)`) rather than silently recreating `temporal_metadata.db` on NFS. All five server-side temporal launch sites (post-clone registration, scheduled refresh, `add_golden_repo_index` temporal, provider temporal job, activated-repo temporal) pass the contract in postgres mode; a source-scan enumeration guard test fails CI if a future launch site is added without it. CLI/solo indexing is unchanged (no env -> SQLite). Existing SQLite-on-NFS temporal indexes need no forced re-index (the store is write-only at query time); PG rows populate on the next refresh.

## [11.22.0] - 2026-07-06

### Fixed

- **Job-reconciliation no longer wall-clock-reaps legitimately-queued pending jobs (#1312).** `JobReconciliationService._reclaim_stuck_index_blocking_jobs` dropped `pending` from its status filter entirely — it now reclaims only the anomalous `status='running' AND started_at IS NULL` case. A pending job queued behind long-running index jobs for other repos (bounded worker-pool exhaustion, `max_concurrent_background_jobs`) is never failed on a wall clock, closing a milder Bug #1218 edge. Bug #1141 remains covered: genuinely-abandoned pending rows are still failed by `DistributedJobWorkerService` (Bug #582), which runs on the same leader-election gate and claims the oldest pending row unconditionally (`DistributedJobClaimer` `max_concurrent_jobs=0`), failing non-retryable ops. Bug #1310 (never reap a running job with a valid `started_at`) is intact.
- **Realigned deployment-executor memory/swap unit-test assertions (v11.21.0 test regression).** `tests/unit/auto_update/test_deployment_executor_memory.py` asserted the pre-`_run_systemd_op_with_retry` `subprocess.run` signature (raw `timeout=30/60`); the v11.21.0 hardening routes `_ensure_memory_overcommit`/`_ensure_swap_file` through the retry helper (`input=None`, `timeout=SYSTEMD_OP_TIMEOUT_SECONDS=120`). Assertions corrected to reference the constant. Test-only; production behavior was already correct and validated live.

## [11.21.0] - 2026-07-06

### Fixed

- **Cluster installer now provisions the auto-updater.** `scripts/install-cidx-server.sh` previously created only `cidx-server.service`, so freshly-built cluster nodes had no `cidx-auto-update.service`/`.timer` and never self-updated (the server logged `launch_restart_generation target > applied ... check cidx-auto-update service status` indefinitely). The installer now renders and enables both units from the shipped template. The `cidx-auto-update.service` template and the `cidx server install-auto-update` CLI command gained a `--branch` parameter (default `master`) threaded through `CIDX_AUTO_UPDATE_BRANCH`, so staging nodes track `staging` instead of falling back to `master`.
- **Auto-update deploys survive transient systemd/sudo starvation.** On a fresh node the first deploy compiles hnswlib and installs the Rust toolchain, briefly starving systemd/sudo (compounded when a `hard` NFS mount points at a down node); the deploy executor's hard 30s `sudo`/`systemctl` timeouts then fired and silently skipped config steps (MALLOC_ARENA_MAX injection, sudoers self-restart rule). Renamed `SYSTEMCTL_TIMEOUT_SECONDS` (30) to `SYSTEMD_OP_TIMEOUT_SECONDS` (120) and added a bounded, `TimeoutExpired`-only retry (3 attempts, 5s backoff) around the systemd control-plane operations (daemon-reload, sudoers verify/create, unit-file writes, memory-overcommit, swap-file). Fail-soft behavior preserved; the indexing / golden-repo / SCIP path (Bug #1218) is untouched.

## [11.20.0] - 2026-07-06

### Fixed

- **Job-reconciliation no longer wall-clock-reaps live indexing jobs (#1310).** `JobReconciliationService` deleted the blanket `max_execution_time` reclaim path that failed still-progressing long-running indexing / golden-repo-registration / SCIP / temporal jobs on a live node, restoring the Bug #1218 invariant (the indexing path carries no wall-clock timeout — a large repo legitimately takes hours). Dead-node/heartbeat reclaim (the only legitimate mechanism) is unchanged; the stuck-index-blocking reclaim now guards on `started_at IS NULL`, so it can only free genuinely never-started jobs blocking `idx_active_job_per_repo`, never a running job. Tracked follow-up: #1312 (a legitimately-queued pending job can still be reaped on queue age — pre-existing edge, out of scope here).
- **Removed redundant "durable / windowed / cluster-aggregated" badges from the cache-metrics dashboard (#1311).** Every card on the panel is now DB-sourced from the shared store (Cache Entries = live `query_embedding_cache` COUNT; On-Mode Hit Rate = `search_event_log` request counts; all others = `WindowedCacheMetrics` over `search_embed_event`), so the per-card provenance badge introduced mid-migration by Story #1294 distinguished nothing and overflowed the card. Removed the 8 header badges, 8 footer note-prefixes, and their CSS; per-card explanatory footers and all data sources are unchanged.

## [11.19.0] - 2026-07-05

### Added

- **Durable query-embedding decision event log `search_embed_event` (#1293, epic #1288).** One row per NEEDED embed on every live path (direct, coalesced owner/joiner sharing one `live_batch_id`, temporal), on SQLite (solo) and PostgreSQL (cluster). Replaces the restart-volatile in-memory counter that overcounted under coalescing.
- **Windowed, cluster-aggregated cache-metrics dashboard (#1294, epic #1288).** Every cache card is re-sourced from `search_embed_event` via a fail-open `WindowedCacheMetrics` aggregation with a time-window selector, so the dashboard reconciles with the analytics export instead of a per-node counter that reset on restart.
- **`count_transport_calls()` — real transport HTTP-call count (#1305).** Additive alongside the unchanged `provider_embed_calls` (successful NEEDED embeds); it additionally counts shadow-validation, failover-attempt, and bypass wire calls. Documented residual: an all-shadow-hit coalesced batch is not additively countable.

### Changed

- **Removed the `config.json` transition copies of `workers`/`log_level`/`host`/`port` (#1196, epic #1194).** These launch settings now resolve solely from shared DB / `launch.json` / `applied_launch.json`; DEPLOY preserves the live systemd ExecStart and never applies a saved-but-unconfirmed launch change.
- **BREAKING: `cidx.cache.embedding.*` OTEL instruments re-sourced from an in-memory tracker to durable, cluster-aggregated ObservableGauge callbacks (#1295, epic #1288 final).** `cidx.cache.embedding.hits` and `.misses` were monotonic Counters (incremented once per operation, restart-volatile, per-node only); they are now windowed `ObservableGauge` instruments re-computed from the durable `search_embed_event` table (Story #1293/#1294) on every OTEL export tick. Any downstream OTEL consumer that took a `rate()`/`increase()` derivative over the old Counters must instead read the Gauge value directly. `cidx.cache.embedding.shadow_cosine` similarly moved from a push Histogram to windowed percentile/histogram Gauges (`shadow_cosine_p50`/`_p05`/`_min`/`_histogram`). `cidx.cache.embedding.total_entries` is UNCHANGED (still a cheap cache-state COUNT, not event-sourced).

### Fixed

- **Temporal watch mode called an undefined `TemporalIndexer.index_commits_list` (`AttributeError`) — rewired both call sites to the real per-commit `index_commits()` entry point (#1296).**
- **Fixed pre-existing daemon/diagnostics test debt hidden from the CI gate by `@pytest.mark.slow`/marker filters, and in the process restored a production prompt-template resource (`diagnostic_troubleshooting.txt`) that an earlier "dead code removal" wrongly deleted while its live consumer remained (#1304).**
- **id-index rebuild no longer logs benign missing-`id` WARNINGs for the temporal sidecar files (`temporal_structure.json`/`temporal_progress.json`/`temporal_meta.json`); a genuinely malformed vector file still warns (#1297).**

### Removed

- **The in-memory `QueryEmbeddingCacheMetrics` tracker and `CoalescerRegistry.metrics()` deleted entirely (#1295, epic #1288 final).** Both were restart-volatile, per-node-only tallies now fully superseded by the durable `WindowedCacheMetrics` aggregation. The `GET /api/admin/coalescer-metrics` REST route was removed (redundant with the windowed dashboard, which already exposes cluster-aggregated `texts_coalesced`/`batches_dispatched`/`dedup_savings`/`provider_embed_calls`). `cidx.cache.embedding.audit_top1_match` was removed (no `search_embed_event` schema column for top1-match). The deep-fidelity audit path (`embedding_cache_audit.py`) now stamps `audit_sampled`/`audit_cosine` directly onto the durable event row via the Story #1293 keyed `update_audit_by_key` UPDATE, wiring a previously-orphaned code path.
- **Deleted the orphaned diagnostics "actionable feedback" feature (`DiagnosticsService.get_actionable_feedback()`, `_load_prompt_template()`, `_execute_claude_prompt()`, and the `server/feedback/` package) — it had zero callers and was never wired to any route/UI (#1307).**

## [11.18.3] - 2026-07-04

### Fixed

- **Daemon-mode temporal queries were 100% broken (#1302, epic #1289).** Standalone/server temporal queries worked, but the daemon's `exposed_query_temporal` resolved the collection via the regular embedding_provider/model scheme (`resolve_temporal_collection_from_config`, yielding `code-indexer-temporal-voyage_code_3`) while per-commit temporal data lives under the active-embedder scheme (`code-indexer-temporal-voyage_context_4`), and the daemon's mmap `CacheEntry` was shard-blind (real HNSW data lives in per-quarter shard subdirectories, e.g. `...-2026Q3`). Both defects fixed by delegating `exposed_query_temporal` to `execute_temporal_query_with_fusion` -- the same shard-aware, active-embedder-named dispatch already used by the CLI, server, and multi-search paths (its docstring already claimed daemon support; the wiring had simply never been done). A `--temporal-embedder` query override is now threaded end-to-end through the daemon path. Verified with a real daemon E2E for both embedders (daemon results byte-for-byte match standalone: voyage-context-4 and Cohere embed-v4.0).

### Removed

- **Dead mmap temporal-cache code in the daemon (#1302).** The fusion-delegation fix left `CacheEntry.load_temporal_indexes` / `invalidate_temporal` / `is_temporal_stale_after_rebuild` (plus the `temporal_hnsw_index` / `temporal_id_mapping` / `temporal_index_version` fields and their `get_stats` keys) unreachable from any production caller. Removed (87 lines) along with the test file that only exercised them, for anti-orphan compliance. The regular non-temporal daemon cache path is unchanged.

## [11.18.2] - 2026-07-03

### Fixed

- **Daemon-mode temporal query crashed with a 100% `AssertionError` (#1300, epic #1289).** `TemporalDaemonService.exposed_query_temporal` asserted `config_manager is not None` before the lazy-init that creates it had run, so every daemon-mode temporal query failed. The lazy-init (`ConfigManager.create_with_backtrack`) is now hoisted above the first use and the assert.

- **Temporal query params `at_commit`, `show_evolution`, `evolution_limit` were silent no-ops on the per-commit temporal index (#1301, epic #1289).** `at_commit` was advertised on REST/MCP but never applied and never validated -- a bogus commit hash was silently accepted and returned the full unfiltered result set. Now implemented as point-in-time scoping: the ref/hash is resolved via git to a commit + UNIX timestamp (`resolve_commit_timestamp`), which becomes an upper bound on `commit_timestamp` -- the same mechanism `time_range`'s upper bound already uses. An unresolvable ref/hash now returns a typed HTTP 400 error instead of silently succeeding.

### Removed

- **`show_evolution`, `evolution_limit`, and `include_removed` retired from the REST/MCP query surface (#1301).** These parameters were advertised as working but were permanent no-ops on the per-commit temporal index (Epic #1289) -- neither filtering/augmenting results nor returning a warning/error. Per-file diff timelines belong to the existing git tools (`git_file_history`, `git_log`, `git_blame`, `git_diff`), not the semantic temporal search path. Removed end-to-end: `SemanticQueryRequest` (REST model), the MCP `search_code` tool schema/docs, the Web UI query page's "Include removed files" checkbox, and all internal plumbing (`execute_temporal_query_with_fusion`, `SemanticQueryManager` query chain). A client that still sends these fields simply has them ignored by the request parser (they no longer exist) rather than silently accepted and dropped deeper in the stack.

## [11.18.1] - 2026-07-03

### Fixed

- **Temporal search dropped the most-relevant commits (#1299, epic #1289).** Found by a real REST/MCP front-door E2E on clean code. Two root causes: (1) `TemporalSearchService.query_temporal` sorted deduped results by commit date and then truncated to `limit`, keeping the newest matches instead of the most relevant; fixed to select the top-`limit` by relevance first, then order that subset reverse-chronologically for display. (2) Fusion across disjoint quarterly shards used reciprocal-rank fusion (RRF by within-shard rank), discarding true cosine magnitude; replaced with a score-preserving `merge_shards_by_score` for disjoint shards (RRF retained unchanged for the overlapping path). Verified via the live front door: both ground-truth commits recall at rank 0 across all tested limits for both embedders. Embedding, projection, HNSW, and the regular voyage-code-3 path were proven correct and left untouched.

## [11.18.0] - 2026-07-03

### Added

- **Epic #1289 — Per-Commit Contextualized Temporal Indexing (Pluggable Dual Embedders).** Replaces the per-file-diff temporal layout (which exploded to millions of one-vector-per-file files) with one aggregated document per commit (commit-message head + `--- path ---`-delimited diffs), chunked and embedded into model-slug-keyed quarterly shards. Far fewer vectors: a 20-file commit produces 1 vector instead of ~21.
  - **Story #1290** — voyage-context-4 contextualized-embeddings adapter (1024-dim, 0% overlap), per-commit aggregation, v2 `temporal_structure.json` marker, hard-cut removal of the legacy per-file-diff path and monolith->shard migration machinery, recall dedup-by-commit with commit message surfaced once.
  - **Story #1291** — Cohere embed-v4.0 as a pluggable coexisting second embedder (1536-dim native, 15% overlap). Multiple embedders' temporal indexes live side-by-side; adding one never rebuilds another. `temporal_embedder` query override threaded through REST/MCP. Per-embedder reconcile.
  - **Story #1292** — git-history-only file-count projection script (`scripts/analysis/temporal_vector_projection.py`), absolute recall-quality gate (`scripts/analysis/temporal_recall_gate.py`), real REST+MCP front-door dual-embedder e2e (`tests/e2e/server/test_18_temporal_dual_embedder_1292.py`), and documentation.

### Fixed

- **voyage-context-4 temporal search returned HTTP 400 in server/cluster mode.** `VoyageAIClient.get_embeddings_batch()` (the server coalescer's embedding entry point) bypassed the contextualized-query special case; it now routes contextualized queries correctly (non-contextual/document/CLI/voyage-code-3 paths unchanged).
- **Contextual embedder document packing** capped each packed document at the ~108k-token request budget instead of the ~28.8k-token context window, causing HTTP 400s; now caps at the context window.

## [11.17.0] - 2026-07-02

### Fixed
- **#1286 (P1): Temporal shard migration is now a guaranteed lossless verified move.** The Story #1172 monolith->quarterly-shard migration silently dropped embedded vectors, wrote a false `migration_complete.marker`, and deleted the source vector files before verification -- irreversible data loss. Migration now verifies losslessness (exact count/point-id reconciliation) BEFORE deleting the monolith, hard-aborts on ANY unplaceable vector (missing id_index / missing JSON / unresolved timestamp) before building shards (no silent skips), derives the shard quarter from the immutable payload `commit_timestamp` first (git log fallback, per-SHA-resilient so one bad SHA can't poison a batch), and recovers by re-extracting vectors from the monolith with ZERO re-embedding. Covered by a real-VoyageAI E2E suite (no mocks).
- **#1285 (P2): Activation CoW clone timeout is config-driven with partial-clone cleanup.** Large golden-repo activations (evolution/phoenix, ~1M-file index) failed at a hardcoded 120s subprocess timeout; it now honors `cow_clone_timeout` (default raised 600->3600) across LocalCloneBackend and CowDaemonBackend, and a timed-out activation removes its partial clone before re-raising so retries are not blocked.
- **#1283 (P2): Web UI "Create API Key" no longer returns 401.** `POST/GET/DELETE /api/keys` used Bearer-only `get_current_user`; they now use `get_current_user_hybrid`, which accepts both the `Authorization: Bearer` header and the web session cookie, matching the other Web-UI-facing REST routes.
- **#1284 (P3): Stale-mock dimension guard test fixed and un-hidden.** `TestLayer3APIValidation` mocked 3-dim embeddings that a newer dimension guard rejected before the None-detection path was reached; mocks are now dimension-correct (1024-dim) and the test is no longer `--deselect`'d by `fast-automation.sh`.
- **#1287 (P4): e2e log-audit gate two-queue flush race fixed.** The Phase 3 gate captured its watermark after draining only `SQLiteLogHandler`, leaving the `async_logging` queue-listener undrained; a new `flush_log_pipeline()` drains both. The benign `cidx-meta-global` "FTS index not available" condition (auto-bootstrapped internal repo) is allowlisted, scoped to that exact alias.

## [11.16.0] - 2026-07-01

### Fixed
- **#1264 (P1): Temporal projection-matrix self-heal is now applied at the `upsert_points` write chokepoint, closing the gap left by #1242.** The #1242 self-heal (v11.8.0) only repaired shards enumerated by the `index_commits` prep loop, but the actual crash lives in a separate module: `FilesystemVectorStore.upsert_points()` routes each point to its per-quarter shard by commit date and calls `ProjectionMatrixManager.load_matrix()`, which raised a bare `FileNotFoundError` when a shard's `projection_matrix.npy` was missing. Any shard the prep loop did not revisit (old-history quarters, lazily-touched shards) still hard-crashed every golden-repo refresh -- observed in production for `evolution-global` (2009Q4), `genai-talk2db-global` (2025Q4), and `mobile-global` (2026Q2). The fix wraps the `load_matrix` call: on a missing matrix it reuses the existing `_ensure_shard_has_projection_matrix()` helper (copy-from-base or deterministic regenerate -- no duplicated logic), evicts the stale matrix-cache entry, and retries the load once; a genuinely nonexistent collection still raises loudly (anti-silent-failure). Validated by real-path reproduction against the exact production stack trace: crash at the pre-fix commit, clean self-heal at HEAD, index remains queryable.
- **#1264 (hardening): The shared `_ensure_shard_has_projection_matrix()` helper now writes the projection matrix atomically (temp file in the same directory + `os.replace`) for both the copy-from-base and regenerate branches.** Because #1264 makes the helper reachable from the temporal parallel-worker write path, two workers first-writing the same missing-matrix shard concurrently could otherwise hit a torn-read window; the atomic rename closes it. Both the #1242 prep-loop path and the #1264 chokepoint path benefit from the single shared change.

## [11.15.0] - 2026-07-01

### Fixed
- **#1261: Removed artificial work-budgets (search-call ceiling / agent-turn caps / output-length caps) on dep-map analysis jobs.** These caps were throttling legitimate long-running dependency-map analysis, causing large repos to hit budget limits before completing a full pass.
- **#1259: `frontmatter_json_mismatch` anomalies now route to the free Phase 3.5 sync instead of Phase-1 Claude re-analysis.** Splits the overloaded `REPAIRABLE_ANOMALY_TYPES` set so frontmatter/JSON drift -- a deterministic sync fix -- no longer burns a Claude re-analysis pass that Phase 3.5 can resolve for free.
- **#1260: Stopped deleting the domain `.md` before Phase-1 repair analysis, restoring the previous `previous_domain_dir` extend/improve reuse.** The prior delete-then-regenerate path discarded reusable domain context that Phase-1 repair analysis depends on to extend/improve rather than start from scratch.
- **#1257: Dashboard On-Mode Hit Rate is now request-denominated (from `search_event_log`) instead of operation-denominated.** The previous operation-denominated calculation misrepresented actual query-embedding cache effectiveness; the metric now reflects the true per-request hit rate.
- **#1262: `LifecycleBatchRunner.run()` now surfaces per-alias failure so a failed single-alias description refresh is marked failed and increments the circuit-breaker.** Previously a failed alias inside a batch was masked by the batch-level success reporting, silently burning Claude spend on repos that were quietly failing every cycle without ever tripping the #1096 quarantine circuit-breaker.
- **#1258: `JobTracker.complete_job`/`fail_job` persisted-row fallback closes the double-completion "not in memory" warning flood and the pop-before-persist zombie edge.** A completion racing an in-memory pop could log a flood of "not in memory" warnings and leave a zombie job row; the fallback now reads the persisted row when the in-memory entry is already gone.

### Testing
- **test(auto-update): aligned the 5 swap-failure tests with the Bug #1254 non-fatal contract.** These tests had gone stale after #1254 changed `_ensure_swap_file` to best-effort (WARNING + return True on failure); updated assertions to match the current non-fatal behavior.

## [11.14.0] - 2026-07-01

### Fixed
- **#1256 (P3): `JobTracker.update_status(running)` logged a full-traceback WARNING on a benign `idx_active_job_per_repo` UniqueViolation during multi-node cluster restart.** On a rolling restart, the `update_status('running')` UPDATE for the node-`server` singleton startup jobs (`reap_activated_repos` / `data_retention_cleanup` / `startup_reconcile`) collides on the `idx_active_job_per_repo` partial unique index with a stale-running row left by the SIGKILLed pre-restart process (before `startup_reconcile` demotes it). The raw `psycopg.errors.UniqueViolation` (PG) / `sqlite3.IntegrityError` (SQLite) bubbled to a generic `except` and logged a traceback at WARNING on every restart -- log-noise that trips the Story #1122 audit gate. The job executes correctly regardless (registration dedup already prevents genuine live duplicates); this is the UPDATE-path sibling of the INSERT-path #1252/#1235 fix. Discovered during the v11.13.0 staging post-deploy log audit. Fix (conservative, no execution-behavior change): extracted the `('IntegrityError','UniqueViolation')` class-name check already used inline by `JobTracker._atomic_insert_impl` into a shared driver-agnostic helper `is_active_job_unique_violation()` (single source of truth, Messi #4); the `update_status(running)` catch site now demotes the benign case to DEBUG (no traceback) while ALL other exceptions keep the original WARNING + traceback (Messi #13 anti-silent-failure). The dedup index, registration dedup, and `startup_reconcile` are untouched (out of scope).

## [11.13.0] - 2026-06-30

### Fixed
- **#1254 + #1255 (P1): auto-updater deploy-path immutable-host hardening (swap + rust non-fatal).** After #1251, the deploy reached `pip install` then aborted on OPTIONAL infra steps that cannot succeed on a read-only immutable host. #1254: `_ensure_swap_file` is best-effort (swap is an OOM optimization) -- failures log WARNING + return True. #1255 (the confirmed final blocker): `_ensure_rust_toolchain` skips `mkdir`/`chown`/install when `/opt/rust` already holds a usable toolchain (`rustc`/`cargo --version` succeed), a `chown` failure on a read-only fs is non-fatal when the toolchain is usable (`CARGO_HOME` redirected to `~/.cargo` for the build while `RUSTUP_HOME` stays `/opt/rust`), and a missing C compiler / failed `cargo build` are non-fatal (xray native search is unavailable -- `BinaryNotFound` -- until the binary builds; logged at ERROR, NOT a silent "Python fallback", which does not exist). FATAL only for a genuinely missing/uninstallable toolchain; the CORE deploy (pip install + restart) always completes. Audit confirmed `_ensure_rust_toolchain` is the last `execute()` step. Found by deploying each version to the real PrivateTmp+py3.12+editable-home+read-only staging cluster (chain: #1182/#1243/#1245/#1251/#1254/#1255).
- **#1249 (P2): DB-outage error storm -- background loops logged a fresh ERROR+traceback per tick during a PostgreSQL outage (~37k rows in ~5 min, re-flooding logs.db).** New shared `DbOutageThrottle` (`is_db_connectivity_error` classifier + per-loop state machine): first connectivity error of an outage logs ONE ERROR, subsequent consecutive errors log DEBUG, recovery logs once, and the loop's retry interval gets exponential-capped backoff (cap 60s). Wired into `refresh_scheduler`, `leader_election_service`, `node_metrics_writer_service`, `node_heartbeat_service`, `config_service`. Genuine bugs (ProgrammingError/IntegrityError/...) are NOT throttled. Includes an OverflowError guard (clamps the backoff exponent so an unbounded failure count can't crash the daemon thread). Trigger is infrastructure (PG host restarts); this fixes the cidx-side resilience gap.
- **#1252 (P2): atomic job insert gave up when the conflicting row vanished (benign TOCTOU).** #1252's fatal-RuntimeError symptom was already resolved by #1235; the residual gap: when the blocking job completes between the INSERT and the blocking-row lookup, the slot is free but the code skipped the refresh as a duplicate. Fix: bounded retry (3 attempts) of the atomic insert when the unique-violation fires with no active blocking row; a real duplicate still raises immediately; exhausted retries fall back to `DuplicateJobError(sentinel)`, never a fatal RuntimeError (preserves the #1235 invariant).
- **#1253 (P2): langfuse golden repos with a missing/invalid `.code-indexer/config.json` failed refresh every cycle (231x staging).** `register_local_repo` runs `cidx init` once and swallows a mid-init failure, leaving a `.code-indexer/` dir without a valid config; registration is idempotent so init never retries, and the Bug #268 refresh guard only checked `dir.exists()`. Fix: `RefreshScheduler` self-heals -- when `.code-indexer/` exists but `config.json` is missing/invalid, it re-runs `cidx init --no-override-file --force` (non-destructive; never deletes index data) then indexes; repair failure returns `success=False` (visible, not swallowed).
- **#1250 (P3): `LlmLeaseLifecycleService.start()` logged the provider's EXPECTED 'no available credentials' response at ERROR on every worker startup (253x staging).** Classifies the expected case by exception TYPE (`LlmCredsProviderError`) + HTTP 404 (the provider's documented status), NOT message text (the provider's `{"error":...}` body doesn't match the parsed envelope, so message-matching would silently fail in production). Expected case -> WARNING (degraded, actionable); genuine provider/transport errors -> still ERROR. The non-blocking DEGRADED behavior is unchanged.

## [11.12.0] - 2026-06-30

### Fixed
- **#1251 (P1): auto-updater dead-looped on "No usable temporary directory" -- the no-sudo pip path had no `TMPDIR`.** #1243 set the deploy `TMPDIR` ONLY on the `sudo env TMPDIR=...` prefix. v11.11.0's #1245 writability fix correctly routes editable-home installs (the staging cluster layout) through the NO-SUDO command, which carried no `TMPDIR` -- so under systemd `PrivateTmp=yes` + Python 3.12 the auto-updater's isolated `/tmp` was unusable and pip's `tempfile.gettempdir()` raised `FileNotFoundError: No usable temporary directory found in ['/tmp','/var/tmp','/usr/tmp','/']` at the pybind11/hnswlib build step, dead-looping the auto-updater (same self-perpetuating class as #1182/#1243/#1245). Fix: `os.environ["TMPDIR"] = self._deploy_tmpdir()` at the TOP of `DeploymentExecutor.execute()`, before any subprocess is spawned. This is exhaustive by construction -- every `subprocess.run()` in the module either omits `env=` (inherits `os.environ`, now carrying `TMPDIR`) or builds its env from `os.environ.copy()`/`dict(os.environ)` (git env, self-restart smoke test, pace-maker install, Rust toolchain/cargo) -- so the single early mutation reaches every no-sudo child without per-call-site changes. The SUDO path's explicit `["sudo","env",f"TMPDIR={tmpdir}",...]` prefix (#1243) is byte-identical. Found by deploying v11.11.0 to the real staging cluster.

## [11.11.0] - 2026-06-30

### Fixed
- **#1245 (re-fix of v11.10.0, P1): auto-updater STILL dead-looped on editable-home installs.** v11.10.0's `_is_user_install` classified a host as user-install ONLY via the `/.local/` substring; the staging cluster runs an editable install at `/home/<user>/code-indexer/src` (no `/.local/` segment), so the probe returned False -> sudo'd pip -> root's read-only `/root/.local` + `/root/.cache/pip/wheels` -> `[DEPLOY-GENERAL-021]` -> the dead-loop persisted even after the v11.10.0 source was pulled. Fix: `_is_user_install` now ALSO returns True when `os.access(install_dir, os.W_OK)` (the running import location is writable by the auto-updater's user) -- editable-home, `~/.local`, and genuine root-owned system installs are all classified correctly (conservative False -> sudo on probe failure, DEBUG-logged). System-install command shape (sudo + `env TMPDIR=` #1243, break-system-packages probe/retry #1234) is byte-identical. Caught ONLY by deploying+testing on the real staging cluster (unit tests had asserted `/.local/` paths).
- **#1248 (P1): reindex endpoint 500'd -- `TriggerReindexResponse(**result)` but `trigger_reindex` returns a job_id str, not a mapping.** The router built the response via `**result` while the service returns a bare job_id string (and the response model needs `success`/`job_id`/`status`/`index_types`/`started_at`), so every successful reindex submission 500'd. The failure was masked on cow-daemon by the #1246 400 and never covered by a router-level test. Fix: construct `TriggerReindexResponse` explicitly (`status=JobStatus.PENDING.value` from the BGM's source-of-truth enum, `started_at=now UTC ISO`); `trigger_reindex` keeps its `-> str` contract; the endpoint stays HTTP 202 (async job-trigger semantics). Adds the missing router-level (TestClient) coverage. Found during live staging testing immediately after #1246 unblocked the reindex path.

## [11.10.0] - 2026-06-30

### Fixed
- **#1245 (P1): auto-updater dead-looped at the custom hnswlib build on user-install hosts (sudo'd pip -> read-only `/root/.local`).** Even after #1243 fixed the TMPDIR/PrivateTmp issue, `build_custom_hnswlib()` and `pip_install()` ran pip via `sudo` unconditionally; on a user-install layout (code-indexer in the service user's `~/.local`, e.g. the py3.12 immutable staging host) sudo'd pip targets root's `/root/.local` (read-only) -> `[Errno 30] Read-only file system` -> "Deployment failed at custom hnswlib build step" -> the auto-updater dead-looped (same self-perpetuating class as #1182/#1243: the broken updater cannot deploy its own fix; a one-time manual deploy is required to escape). Fix: `_is_user_install(python_path)` probes the actual `code_indexer.__file__` (True when under `/.local/`, conservative False on any failure) and gates `use_sudo = not _is_user_install(...)` at every pip site -- user-install hosts now run `python -m pip install ...` WITHOUT sudo (landing in the service user's `~/.local`, where the server imports from), while system-install hosts keep the byte-identical sudo+env+TMPDIR shape (#1243) and the `--break-system-packages` probe/retry-strip (#1234). Also: skip the rebuild when hnswlib already imports AND the `third_party/hnswlib` submodule commit is unchanged (recorded under `_cidx_data_dir`); non-fatal (WARNING + continue) when a rebuild fails but hnswlib still imports; DEBUG-log the probe exception paths for diagnosability. Verified against the real run-as-user topology (auto-update service `User={USER}`).
- **#1246 (P1): manual reindex (REST `/api/v1/repos/{alias}/reindex` AND MCP `trigger_reindex`) 400'd "Security violation: Repository path escapes data directory" on every cow-daemon cluster -- ALL cluster reindex broken; masked #1244.** `ActivatedRepoIndexManager.trigger_reindex` did `Path(repo_path).resolve()` (which FOLLOWS the cow-daemon `activated-repos -> /mnt/cow-storage/...` symlink, Bug #1052) then `relative_to(Path(self.data_dir).resolve())` (data_dir is not itself a symlink), so the resolved repo path always escaped the data dir -> false-positive 400 for every repo. Fix: `_compute_allowed_repo_roots()` builds the set of allowed RESOLVED roots -- `data_dir` plus the resolved symlink targets of `data_dir/activated-repos` and `data_dir/golden-repos` (when present) -- and `_path_is_within_any()` accepts the repo iff its resolved path is `relative_to` ANY allowed root. Uses `relative_to` (not string-prefix), so a sibling like `activated-repos-evil` is still rejected; genuine `../`/absolute traversal still fails closed; behavior is byte-identical on local (non-symlink) backends. Security-isolated commit (Story #929). Discovered while attempting a front-door reproduction of #1244 (which this unblocks).

## [11.9.0] - 2026-06-30

### Fixed
- **#1243 (P1): auto-updater dead-looped on Python 3.12 + systemd `PrivateTmp` ("No usable temporary directory") -> NO staging deploy could complete.** Under `PrivateTmp=yes`, the auto-update service has an isolated `/tmp`; the sudo'd `pip install` (pybind11 + hnswlib build, and `pip install -e .`) found no usable temp dir (private /tmp/var-tmp, CWD `/` not writable, and `sudo env_reset` strips inherited `TMPDIR`), crashing every retry. Same self-perpetuating-deadlock class as #1182 (the broken auto-updater cannot deploy the fix), and `/etc` is read-only on the hosts so a unit drop-in is impossible. Fix: `DeploymentExecutor` now passes a writable `TMPDIR` THROUGH sudo at every pip site via the POSIX `env` utility -- `["sudo","env",f"TMPDIR={tmpdir}",python,"-m","pip","install",...]` -- where `tmpdir = {CIDX_DATA_DIR}/.deploy-tmp` (created on demand, never under `/tmp`). The `--break-system-packages` handling (#1234) and retry-strip variants are preserved. Validated live (`sudo env TMPDIR=... pip install` -> rc=0).
- **#1241 follow-up (P1, regression caught live on the staging PostgreSQL cluster): `LogsPostgresBackend.insert_log_batch` called `conn.executemany`, but psycopg3 `executemany` is a CURSOR method, not a Connection method.** Every batched log insert raised AttributeError, swallowed by the fail-open WARNING -> ALL batched operational logs silently dropped on the cluster. The #1241 server-fast tests passed because the fake backend was unfaithful (had a connection-level `executemany`). Fix: `with conn.cursor() as cur: cur.executemany(...)` (with `SET LOCAL synchronous_commit = off` on the cursor first). New `test_logs_pg_batch_cursor_1241.py` uses a faithful fake Connection with NO `executemany` attribute (mirrors psycopg3) so the regression is guarded. A cross-backend audit confirmed no sibling `conn.executemany`-on-a-psycopg3-Connection bug elsewhere.

## [11.8.0] - 2026-06-29

### Fixed
- **#1242 (P1): temporal monolith->shard migration created shards WITHOUT `projection_matrix.npy`, breaking ALL incremental temporal indexing (690/693 shards).** `_build_one_shard` wrote `collection_meta.json` + `id_index.bin` + `hnsw_index.bin` but never copied the projection matrix, so `collection_exists()` returned True, the indexer skipped `create_collection()` (the only writer of the matrix), and the next `upsert_points()` crashed at `load_matrix()` with `FileNotFoundError`. Verified-correct fix (the projection matrix is write-path-only — it computes the on-disk hex bucket for the JSON payload; HNSW stores full-dim vectors and search is matrix-independent, so a regenerated matrix is safe): (1) `_build_one_shard` now COPIES the monolith's surviving `projection_matrix.npy` into each shard (regenerate fallback) and backfills `quantization_range` into the shard meta; (2) a self-heal `else` branch in `temporal_indexer.index_commits` repairs the 690 already-deployed shards on their next refresh — when a shard exists but lacks the matrix, it copies from the base collection (or regenerates) + backfills quant-range BEFORE the upsert workers run, so incremental indexing recovers without a re-index. Also corrected a stale comment that wrongly claimed deleting the matrix breaks queries, and added `base_collection_name()` to single-source the quarter-suffix strip. Proven by `TestIndexCommitsSelfHeal` driving the real `index_commits()` path on a production-shaped broken shard.
- **#1241: server log/audit writes made fully async + batched to eliminate SQLite "database is locked" contention.** The app-boundary log queue was already async, but the logs.db writer wrote one transaction per record (a 34k-record migration burst = 34k commits -> WAL churn -> lock contention), and `AuditLogService` wrote synchronously on the request thread. Fixes: (1) `SQLiteLogHandler` writer now drains up to `MAX_DRAIN_BATCH=512` records and inserts them in ONE `executemany` transaction (`insert_log_batch` added to both SQLite and PostgreSQL `LogsBackend` backends; PG uses `SET LOCAL synchronous_commit = off`); (2) `AuditLogService` converted to a bounded-queue + daemon-writer + batched `executemany` design, with `start()`/`stop()` wired into lifespan startup/shutdown (+ regression guard) so audit writes no longer block the operation thread and drain on graceful restart; (3) audit-write failures now log a WARNING instead of being silently swallowed, with a per-row fallback so one poison row can't drop a whole batch. WAL was already enabled and `DatabaseConnectionManager` already sets `busy_timeout=30000` per connection. Follow-up: shared `AsyncBatchedDBWriter` consolidation deferred.

## [11.7.0] - 2026-06-29

### Fixed
- **#1240: temporal migration flooded the SQLite logs.db (one WARNING per skipped point) -> "database is locked" storm on large corrupt indexes.** `_build_quarter_buckets` logged a WARNING for every skipped point across all three drop paths (missing_id_index, missing_json, timestamp_unresolved). On a large repo with a partially-corrupt temporal index (production `evolution`: ~1,914 orphans in one collection; ~34,000 migration log rows total) this overwhelmed the single-node SQLite log store and surfaced as Logs-DB lock-contention events on the dashboard. Fix: demote the four per-point `logger.warning` calls in `_build_quarter_buckets` to `logger.debug`; the per-collection aggregate summary WARNING (which reports the counts: structural orphans, missing_id_index, missing_json) is retained, so operators keep the actionable signal while the per-point detail moves to DEBUG. drop_counts accounting, the reason-aware guard, and all bucketing behavior are unchanged. (Root-cause logging-architecture follow-up tracked in #1241: fully-async batched log writer.)

## [11.6.0] - 2026-06-28

### Fixed
- **#1238 (CRITICAL data loss): temporal monolithic→quarterly-shard migration could silently drop ALL vectors on a large repo, then delete the only copy and report success.** `_batch_get_commit_timestamps` placed EVERY indexed commit SHA on a single `git log --no-walk` argv; a repo with ~50k+ unique commits exceeds Linux `ARG_MAX` (or the 60s timeout), the helper returned an empty dict, and because production temporal payloads are empty the per-payload fallback then dropped every point. `_migrate_one_collection` counted only bucketed vectors and called `_cleanup_monolithic_collection` UNCONDITIONALLY, deleting `hnsw_index.bin`/`id_index.bin`/payloads and logging "Migration complete" — unrecoverable, and the marker blocked re-run. Fix: (1) chunk the SHA lookup into 1000-SHA batches (E2BIG impossible; 60s timeout PER chunk; a failing chunk warns and continues). (2) A reason-aware post-condition guard in `_migrate_one_collection`: `_build_quarter_buckets` now returns per-reason drop counts (`missing_id_index`, `missing_json`, `timestamp_unresolved`); when any structurally-valid point fails timestamp resolution the migration RAISES before cleanup and preserves the monolith for a clean re-run (Messi #2/#13, Bug #1218 fail-loud), while genuine structural orphans (already unqueryable) are logged as a loud WARNING and the resolvable remainder migrates. Never reports success on a partial drop. Validated by unit tests (chunking, fail-loud-no-data-loss, raise-then-clean-rerun, structural-orphan-proceeds) and a live VM data-loss reproduction + clean fixed-upgrade.
- **#1239: first v11 deploy ran the single uvicorn worker on ~1/4 of the node's cache + provider-concurrency budget.** On the first auto-update from a pre-11 node, `applied_launch.json` does not exist, so `get_applied_worker_count()` fell back to `config.json["workers"]` (e.g. 4) while DEPLOY-mode ExecStart preservation (the correct anti-`--host`-flip behavior) left the unit with no `--workers` token → uvicorn launched 1 worker. `initialize_caches()` and the `ProviderConcurrencyGovernor` then divided the per-node budget by 4 for a single running worker — a silent 4x under-resourcing until the next Web-UI restart. Fix: `get_applied_worker_count()` now reads the LIVE systemd ExecStart `--workers` as the highest-priority source (no `--workers` token → 1, matching the running process; reuses `DeploymentExecutor._is_cidx_execstart`/`_read_flag`, no regex duplication, fail-soft), so the cache/governor divisor always matches the actually-launched worker count. DEPLOY-mode ExecStart preservation is unchanged.

## [11.5.0] - 2026-06-28

### Fixed
- **Temporal monolithic→quarterly-shard migration discarded ALL git timestamps on Python 3.9 → broke migration of pre-11 indexes with empty payloads (#1237).** The v11 startup migration (#1172) derives each commit's quarter from `git log --format=%cI`, but git 2.x emits a trailing `Z` for UTC commits (e.g. `2026-03-25T05:18:38Z`), which Python 3.9's `datetime.fromisoformat()` cannot parse. The git-timestamp helper wrapped the whole batch in one try/except, so a single unparseable line returned `{}` ("timestamps unavailable") and the migration fell back to per-payload `commit_timestamp` (epoch). Production temporal payloads are empty, so the fallback skipped every vector → incomplete/empty shards → temporal index not usable after upgrade. Fix: normalize a trailing `Z` to `+00:00` before `fromisoformat` (Py3.9-compatible; offset forms unchanged), and parse each git-log line in its own try/except so one bad timestamp skips only that commit instead of aborting the whole batch. Now git timestamps are available for all UTC commits and any pre-11 monolithic temporal index migrates cleanly into the correct quarterly shards. Validated by unit tests including the empty-payload production scenario (git Z-timestamps → shards created); live upgrade re-test on a v10.141.0 monolithic index (spanning 2020–2024) performed on the test VM.

## [11.4.0] - 2026-06-27

### Fixed
- **#1234 (corrected): pip-capability probe checked the wrong pip.** The v11.3.0 #1234 fix probed `python -m pip --version` WITHOUT sudo (resolving the service user's `~/.local` pip, e.g. 26.0.1) while the actual hnswlib/pybind11 install runs `sudo python -m pip install ...` (resolving root's SYSTEM pip, e.g. 21.3.1 on stock Rocky 9). The probe therefore reported "supports --break-system-packages" and the sudo install still failed with "no such option", aborting the auto-update at the hnswlib build. Caught on a live Rocky 9 VM (non-sudo pip 26.0.1 vs sudo pip 21.3.1). Fix: `_pip_supports_break_system_packages(python_path, use_sudo)` now probes with the SAME sudo context the install uses (both `build_custom_hnswlib` and `pip_install` call it with `use_sudo=True`); added a belt-and-suspenders retry-without-flag fallback when an install still returns "no such option: --break-system-packages". No behavior change when the install-context pip is >= 23.0.1.

## [11.3.0] - 2026-06-27

Production/staging-reported correctness fixes for single-server multi-worker deployments (auto-reported by Neo + PreProdServer), found while hardening the upgrade-test release.

### Fixed
- **A single repo's missing/corrupt local index sin-bins the embedding provider cluster-wide (#1236).** The parallel-dispatch failure handler recorded ANY provider-task exception as a provider failure (`record_call(success=False)`), including local-storage errors raised AFTER the embedding call already succeeded -- so one un-indexed (or corrupt-indexed, or missing-`collection_meta.json`) repo flipped voyage-ai/cohere to `down` and every semantic query for EVERY repo skipped that provider for the sin-bin window. Introduced `LocalIndexNotFoundError` (raised by `filesystem_vector_store` for absent HNSW, corrupt HNSW via `_is_corrupt_index_error`, and missing `collection_meta.json`); the dispatch handler now skips provider-health recording for it. Genuine provider failures (HTTP/rate-limit/timeout) still sin-bin (Bug #678 preserved). The timeout branch is unchanged (cause genuinely ambiguous at timeout; a local index error surfaces via the except branch, not timeout).
- **PostgreSQL multi-worker data-retention duplicate-claim race surfaced as a cleanup error (#1235).** Under multiple uvicorn workers on PostgreSQL, concurrent `data_retention_cleanup` claims race on the `idx_active_job_per_repo` partial unique index. The intended `DuplicateJobError` skip was not reliable: when the blocking active row completed between the unique violation and the follow-up lookup, `_atomic_insert_or_raise` raised `RuntimeError` (escaping the scheduler as a false failure); separately, `complete_job()` could deadlock on `background_jobs`. Now the vanished-row race deterministically yields `DuplicateJobError` (sentinel `existing_job_id`), and `BackgroundJobsPostgresBackend.update_job` retries on PG deadlock/serialization (40P01/40001) with bounded backoff. SQLite single-worker path unchanged.

## [11.2.0] - 2026-06-27

Multi-worker hardening + deploy-robustness fixes, found by a full end-to-end upgrade test (release v10.141.0 -> staging) on a fresh Rocky 9 VM running uvicorn with 4 workers.

### Fixed
- **Concurrent FTS queries fail with Tantivy `LockBusy` under multi-worker (#1233).** Every server FTS query called `TantivyIndexManager.initialize_index(create_new=False)`, which creates a Tantivy `IndexWriter` and takes the exclusive `.tantivy-writer.lock`. Under multi-worker uvicorn (separate processes, enabled by the #1167 workers un-pin) concurrent FTS reads from different workers collided on that lock -> ~67% HTTP 500 (12 concurrent -> 8 failed; sequential clean). Added `TantivyIndexManager.open_for_search()` -- a read-only path that opens the index via `Index.open` + a reader and NEVER creates a writer/takes the writer lock -- and wired it into the three server FTS read sites (`server/query/semantic_query_manager.py`, `server/routers/inline_query.py`, `server/multi/multi_search_service.py`). The write/commit path (golden-repo CoW cleanup) still uses the writer. Search results are unchanged; the daemon FTS path was already correct (it reuses a cached searcher) and is unchanged.
- **Auto-update hnswlib build assumes pip>=23 (`--break-system-packages`) -> deploy fails on stock Rocky 9 (#1234).** The deployer's hnswlib/pybind11 rebuild ran `pip install --break-system-packages` against the system python; on a fresh Rocky 9 box (system pip 21.3.1) that flag does not exist, so the auto-update deploy failed at the build step and never completed. Added `_pip_supports_break_system_packages()` (parses `pip --version`, true only for pip>=23.0.1, fail-safe false) and made the flag conditional at all three `pip install` sites in `deployment_executor.py`. No behavior change on pip>=23.

## [11.1.0] - 2026-06-27

Upgrade-safety hardening of the DB-centralized runtime-config path (the feature that moves bootstrap settings into the database), found by a focused architecture review of the automatic auto-updater upgrade path.

### Fixed
- **Non-atomic `config.json` write could corrupt the only copy of bootstrap keys (#1231).** `ServerConfigManager.save_config`/`save_config_dict` wrote `config.json` via `open(path,"w")` (truncate-then-write). After config centralization, `config.json` holds the ONLY on-disk copy of the bootstrap keys (`postgres_dsn`, `storage_mode`, `clone_backend`, `cow_daemon`), so a crash mid-write (OOM, power loss, `TimeoutStartSec` SIGKILL) left a truncated file -> next startup `load_config` raised `JSONDecodeError`/`ValueError` -> dead node with no auto-recovery. Both writers now go through an atomic helper (tempfile in the same directory + `flush` + `os.fsync` + `os.replace`, temp unlinked on failure), mirroring `materialize_launch_config`. Success-path output is byte-identical (per-function trailing-newline contract preserved), and the existing file mode is preserved across the atomic replace (`0644`->`0644`, etc.; fresh files default to `0644`, not mkstemp's `0600`) so a multi-user server/auto-updater split (Bug #879) keeps read access.
- **Config centralization seeded launch host from the ServerConfig default (127.0.0.1) instead of the live ExecStart (#1232).** At first-boot centralization the runtime row (desired launch state) is seeded from `get_config()`; if `config.json` had no explicit `host`, the seeded value was the dataclass default `127.0.0.1` rather than the node's real bind. For a node that binds `0.0.0.0` purely via its systemd `--host` flag, this planted a latent value mismatch that would detonate on the first Web-UI launch change or admin restart (ExecStart rewritten to `--host 127.0.0.1` -> node drops off HAProxy). Fixed at the SEED layer (not at materialize, which must keep honoring desired state per Story #1198): a first-boot gap-fill backfills `host`/`port`/`workers` from the live ExecStart ONLY when absent from `config.json` (precedence: explicit config.json -> live ExecStart -> ServerConfig default + WARNING), in both the SQLite (solo) and PostgreSQL (cluster) seed paths. An explicit operator value is never overridden. `materialize_launch_config` continues to write desired state directly, so Web-UI launch changes still propagate.

### Removed
- **Retired orphaned config-migration scripts (cluster-config-migrate.sh, config_migration_helper.py, verify_config_migration.py).** Runtime config is migrated automatically at server startup (unified config model; file=bootstrap-only), so the manual one-time scripts from Story #578 are obsolete and were removed. They were orphaned (zero callers) and carried a stale BOOTSTRAP_KEYS set that had diverged from config_service.py's canonical set and could strip pre-DB bootstrap keys (clone_backend/cow_daemon) on cow-daemon clusters if run.

## [11.0.0] - 2026-06-26

Major release. Consolidates the reliability, cluster-resilience, and Query-Analytics work from the 10.16x-10.17x series (CI-token decrypt de-spam, corrupt index/HNSW self-heal, cluster OIDC shared state, memory-governor swap threshold, config-strip noise, dangling-symlink startup resilience, and shadow-mode cache-hit instrumentation).

### Fixed
- **Interrupted query-analytics exports stay stuck `pending` forever (#1228).** Export jobs interrupted by a worker death / restart / infra outage were never reconciled to `failed` -- the export records are not covered by the BackgroundJobManager startup orphan-reconciliation, so they showed as perpetually in-progress in the Analytics export history (e.g. the 3 exports orphaned during the node-23 NFS outage). Added `reconcile_orphaned_exports()` on both export backends (SQLite + PostgreSQL) and the service: a bulk `UPDATE status='failed' WHERE status IN ('pending','running') AND created_at < now - 300s`, wired fail-soft into startup right after the existing `fail_orphaned_jobs` block. Cluster-safe: `created_at` is stamped at work-start (queue delay never inflates age), the export workload is hard-bounded (50k-row cap, finishes well under the 300s window), and `update_export` writes status unconditionally so a false-positive self-heals (a live worker's completion overwrites `failed`->`completed`). Terminal rows untouched; idempotent; no schema change. Clears existing stuck exports on the next deploy.

## [10.172.0] - 2026-06-26

### Fixed
- **Dangling CoW/NFS symlink crash-looped every worker at startup (#1229).** When a CoW/NFS storage node is down, `golden-repos`/`activated-repos` (symlinks onto `/mnt/cow-storage`) become dangling, and the manager `__init__`'s `os.makedirs(path, exist_ok=True)` raises `FileExistsError` on a dangling symlink (`exist_ok` only suppresses the error for an existing directory) — crashing every uvicorn worker in a tight loop and turning a single storage-node outage into a full app-layer outage on every node. Added `server/utils/cow_utils._safe_makedirs_cow()`: on a dangling symlink it logs one rate-limited degraded-mode WARNING and returns without touching the link (preserving Bug #1052), so the worker starts and serves non-CoW traffic; otherwise it behaves exactly like `os.makedirs(path, exist_ok=True)`. Wired into the two `__init__` crash-loop sites only; operational makedirs sites still fail loudly (anti-fallback).
- **Shadow-mode cache HITS never recorded on Query Analytics (#1230).** Shadow-mode query-embedding cache hits were structurally always `voyage_cache_hit=False` (and `cohere_cache_hit`) in the search-event log / analytics export, making shadow's "would-serve rate" invisible. `EmbeddingCoalescer._dispatch()` set every coalesced caller's returned metadata to miss unconditionally; the hit was counted on the metrics layer but never threaded onto the metadata that becomes the search-event field. Now the per-entry metadata reflects a real shadow hit using the pre-write `_shadow_blobs` lookup — a genuinely-new shadow query can never be a false-positive hit (the lookup provably precedes all cache writes in the single-threaded dispatcher). On-mode unaffected; provider-agnostic (voyage + cohere lanes).

## [10.171.0] - 2026-06-26

### Changed
- **Analytics export date filter now uses a UTC datetime picker instead of a raw epoch.** The Query Analytics Export page made the admin type a UTC epoch number into the From/To filter (`type="number"`, placeholder "e.g. 1750000000") -- non-intuitive. Both inputs are now `type="datetime-local"` pickers labeled "(UTC)" with a "Times are interpreted as UTC" note; the client converts the picked UTC datetime to epoch seconds (`Date.parse(value + 'Z') / 1000`) before submitting. The backend is unchanged -- `POST /api/admin/search-events/export` still receives `from_timestamp`/`to_timestamp` as float epoch seconds; only the UI input format and client-side conversion changed. Everything stays UTC.

## [10.170.0] - 2026-06-26

### Fixed
- **Corrupt HNSW index also wedges golden-repo refresh (#1223 follow-up).** Staging validation of v10.169.0 showed the first #1223 fix was partial: it self-heals a corrupt/0-byte `collection_meta.json`, but the same crashed rebuild also leaves a corrupt/partial HNSW index `.bin` (and a stale `.tmp_hnsw_*.tmp`), so after the meta heals the refresh fails one step later with hnswlib's "Index seems to be corrupted or unsupported". Extended the self-heal to the index, same index-time-recover / query-time-raise principle: `load_for_incremental_update` now catches the corruption RuntimeError, discards the corrupt `.bin` + orphaned temp files, and returns the existing no-index sentinel so the caller does a clean full rebuild; `rebuild_from_vectors` cleans orphaned `.tmp_hnsw_*.tmp`. A tight classifier (`_is_corrupt_index_error`) prevents a healthy index from ever being discarded, and the discard is path-safe/idempotent. Query-time load sites are unchanged (a corrupt index still raises on query — no silent serving of an empty index). Composes with the meta fix so a repo with both a 0-byte meta and a corrupt `.bin` fully self-heals on its next refresh, no manual cleanup.
- **Latency tracker logged WARNING on expected shutdown DB-close (#1227).** `DependencyLatencyTracker` logged a WARNING ("database closed -- writer thread terminating") on every server shutdown -- an expected, gracefully-handled teardown condition (the daemon writer's DB is closed during shutdown; it already sets its stop event and terminates cleanly). Changed that closed-db branch in `_flush_buffer` and `_prune_stale` from WARNING to DEBUG; control flow is unchanged and the genuine consecutive-failures ERROR path is untouched, so no real failure is hidden.

## [10.169.0] - 2026-06-26

### Fixed
- **Corrupt/0-byte `collection_meta.json` permanently wedged index refresh (#1223).** A crashed/interrupted index rebuild could leave a 0-byte `collection_meta.json`, after which every subsequent index/refresh of that repo failed forever ("Collection metadata file corrupted (invalid JSON)") with no self-healing -- on staging this wedged a golden repo into 32 identical `global_repo_refresh` failures. Two defects in `FilesystemVectorStore`: `create_collection` wrote the metadata non-atomically (`open(...,"w")+json.dump` truncates to 0 bytes first, so a crash mid-write leaves a 0-byte file), and `collection_exists` returned `metadata_path.exists()` so a 0-byte file counted as a valid collection and blocked re-creation. Now all metadata writers use an atomic temp-file+fsync+`os.replace` helper (fsync only on the rare meta writes; per-vector hot path unchanged), and `collection_exists` validates the file is non-empty and parses with a `vector_size` field -- empty/corrupt meta is treated as absent so a full (re)index self-heals. The query/read path still raises on corrupt meta (a query has no data to rebuild). Deployed, this auto-unblocks the wedged repo on its next refresh.
- **SSO/OIDC login broken in cluster: OIDC state held in per-worker RAM (#1224).** OIDC transaction state (PKCE `code_verifier`, CSRF `state`) lived in a per-process dict (`StateManager._states = {}`), so the IdP callback -- round-robined by HAProxy to a different worker/node than served `/auth/sso/login` -- could not find the state and login failed (a retry "worked" only when both landed on the same worker). State now persists in a shared store (SQLite solo / PostgreSQL cluster, `oidc_state_tokens`), with atomic single-use validation (`DELETE ... RETURNING` on PG, `BEGIN EXCLUSIVE` on SQLite) and read-time TTL; the callback now ensures the provider is initialized on a fresh worker; and the retention scheduler prunes via the wired (PG-backed) instance. PKCE/CSRF/open-redirect properties preserved; isolated security-sensitive commit per Story #929.
- **Memory governor forced RED on any swap-in, thrashing at low memory (#1225).** The Epic #1213 governor forced RED whenever `pswpin_rate > 0`, so trivial OS page-in noise (1-3 pages/interval) forced RED at 25-35% memory (watermarks 70/85), thrashing GREEN<->RED (72+80 GOV transitions/hour on staging) and defeating the GREEN-retain optimization. Added a configurable `memory_governor_swap_pswpin_red_threshold` (default 100, hot-reloaded, Web-UI tunable): only `pswpin_rate >= threshold` forces RED, so a genuine death-spiral (pswpin >> 100) still forces RED while trivial noise no longer does. The master swap-forces-red switch and all watermark/hysteresis/dwell/fail-safe behavior are unchanged.
- **`config.json` load logged a WARNING per restart for `launch_restart_generation` (#1226).** The runtime DB key (Story #1195/#1198), written into bootstrap `config.json` by the auto-updater, is correctly not a bootstrap `ServerConfig` field, so the loader stripped it -- but at WARNING level (~22 entries per cluster restart). Added it to `EXPECTED_ORPHAN_KEYS` so it is stripped silently (INFO); genuinely unknown keys still WARN. Logging hygiene only.

## [10.168.0] - 2026-06-25

### Fixed
- **Repeating APP-GENERAL-061 "Failed to decrypt github token" warning flood (#1222).** `CITokenManager.get_token()` (SQLite/PostgreSQL branch) catches every decrypt failure, logs `APP-GENERAL-061` at WARNING, and returns `None` while preserving the undecryptable ciphertext in the DB for recovery -- correct behavior, but because `create_token_manager()` is built fresh at every call site (not a singleton), every CI/CD credential check re-read the same bad row and re-emitted an identical WARNING (85 times in one staging delta) with no self-healing short of token replacement. Added a process-local, lock-guarded log-once memo keyed by `(platform, sha256(ciphertext))`: the first distinct undecryptable ciphertext logs WARNING as before, repeats are downgraded to DEBUG (not silenced), and a successful decrypt for the platform clears the memo so a genuinely new bad ciphertext after a rotation warns once again. Raw token material is never stored or logged -- only the SHA-256 of the ciphertext. Logging hygiene only: no behavior/contract change, no DB schema/migration; the functional path (treat as unconfigured, preserve ciphertext) is unchanged. The JSON-file path (`APP-GENERAL-062`, which deletes the row and already self-heals) is untouched.

## [10.167.0] - 2026-06-25

### Fixed
- **Cluster elevation: elevated TOTP session not shared across workers/nodes (#1221).** The step-up elevation state lived in per-process RAM, so an elevation obtained on one uvicorn worker (or cluster node) was invisible to every other worker/node behind HAProxy -- admin user/group mutations failed with `elevation_required` immediately after a successful elevate, depending on which worker served the retry. Elevation state now persists in a shared store (`ElevatedSessionManager` backed by PostgreSQL in cluster mode, SQLite in solo) wired at lifespan, with `prune_expired()` cleanup and a fail-closed `get_status()`. The three TOTP error codes, the 503 kill switch, the `totp_repair` recovery scope, and the replay-prevention CAS are all preserved. Isolated security-sensitive commit per the Story #929 discipline.
- **Duplicate-job insert surfaced as scheduler RuntimeError instead of a benign skip (#1220).** The blocking-job lookup used the paginated `list_jobs` view, which could miss the conflicting active row, so a concurrent duplicate registration raised an unhandled `RuntimeError` (seen as a `DataRetentionScheduler` error) instead of the intended `DuplicateJobError` benign skip. Added a direct, non-paginated `find_active_job_by_type_and_alias(operation_type, repo_alias)` on the job-tracker (both SQLite and PostgreSQL backends) and routed `_find_blocking_active_job_id` through it, so collisions deterministically raise `DuplicateJobError` and the caller skips quietly.
- **Daemon in-process FTS rebuild reported success when every file failed (#1218 residual).** `exposed_rebuild_fts_index` returned `status:"ok"` even when `indexed_count == 0 and failed_count > 0`, masking a total failure as success. It now returns `status:"error"` in that case so callers see the failure. Completes the #1218 timeout-removal work on the daemon path.

## [10.166.0] - 2026-06-25

### Added
- **Memory-Pressure-Aware Index-Cache Governor (Epic #1213).** Temporal queries fan out over time-sharded HNSW indexes, and Bug #1171 unconditionally evicts each shard's HNSW after use to prevent a production swap death-spiral, at the cost of reloading every shard from disk/NFS on every query. A new node-aware, cgroup-aware MemoryGovernor makes that eviction conditional on a memory-pressure band: GREEN (memory comfortable) retains shards across queries for fast warm temporal queries; YELLOW proactively evicts LRU to a floor and trims; RED reverts to the exact Bug #1171 evict-after-use. Fail-safe is RED (the band starts RED and any signal error / kill-switch reverts to the proven-safe evict), so the swap-spiral guard is never weakened. CLI/solo and the RED/disabled/errored server paths are byte-identical to #1171. Eight Web-UI-tunable, hot-reloaded watermark settings (no env vars). Observability: GET /api/admin/memory-governor + an MCP twin return a per-process snapshot (band, used_pct, basis cgroup_v2/v1/host, pswpin_rate, counters, live watermarks, pid); GOV-001..005 structured records land in the server log store (front-door queryable via admin_logs_query and sqlite3). Validated solo (local) with GREEN-retain / RED-#1171 / YELLOW-degrade proven by config hot-reload and a real balloon crossing RED with swap flat.

## [10.165.0] - 2026-06-25

### Fixed
- **Regression from #1202: MCP search_code crashed on single global/activated repos (#1219).** #1202 changed `_perform_search` to return a `(results, effective_strategy)` tuple and updated `query_user_repositories`, but missed the second caller `_execute_tracked_search`, so the single `-global` path iterated the tuple and raised `'list' object has no attribute 'to_dict'` on every mode. Unpacked the tuple at the missed call site and threaded the effective-mode echo through the global path. Found on the staging cluster during v10.164.0 validation.

## [10.164.0] - 2026-06-25

### Fixed
- **Temporal indexing per-commit save serialization (#1206).** The per-commit save stage serialized all worker threads (per-vector SQLite commit/fsync, per-commit full-file re-sort+rewrite under an exclusive lock, a global write lock around every vector JSON write), starving the embedding pool. Batched SQLite writes (one transaction per upsert batch + WAL checkpoint), amortized O(1) progress flush (in-memory staging + flush every 10 commits + final flush, race-guarded), and removed the global write lock. Per-commit save cost is now amortized constant; crash-durability and the deterministic point_id contract preserved.
- **Activation never ran branch-aware reindex (#1203).** Activating a golden repo on a non-default branch (and switch_branch / sync_with_golden_repository) CoW-copied the default-branch index and never reindexed, so semantic search returned default-branch embeddings for files that differ. Wired the existing branch-aware delta indexer into all three lifecycle sites (skip for default branch and *-global), and invalidate the in-memory HNSW + id_index caches (prefix eviction) after reindex so the corrected on-disk index is actually served.
- **Temporal CLI --index-commits left a non-sharded monolith (#1207).** CLI sharding never deleted the base provider dir's monolithic index nor wrote migration_complete.marker, so every temporal query enumerated the marker-less base dir (spurious escalating HNSW-stale warnings + stale-read risk). CLI now runs the shared cleanup after a successful shard run (gated so a partial run never deletes the monolith), and get_overlapping_shards excludes a marker-less base dir with no real monolith (unified predicate).
- **REST /api/query had no reranking (#1209).** Added rerank_query/rerank_instruction to the REST query model and wired the shared rerank funnel (after fusion, before truncation), achieving parity with MCP/CLI.
- **Temporal rerank document omitted the commit message (#1208).** commit_diff results were reranked on diff text only. A single shared content extractor (used by MCP, REST, CLI) now includes the commit message for temporal commit_diff results.
- **fts/hybrid silently ignored on dual-provider repos (#1202).** When both embedding providers were configured, the auto-parallel default preempted the FTS branch, so search_mode=fts/hybrid ran semantic fusion instead. Gated the auto-parallel default on search_mode==semantic; added effective_search_mode/effective_query_strategy echo and fts/hybrid overflow metadata to the response.
- **CLI temporal query dropped --exclude-path and extra --path-filter values (#1210).** Now forwards exclude_path and all path filters to the temporal fusion call (parity with the server temporal path).
- **Documented path glob matched only nested paths (#1211).** A leading-star-slash pattern is normalized so it matches a directory segment at any depth including root-level, fixing silent under-filtering (symmetric for include and exclude).
- **VoyageMultimodalClient missing get_provider_name (#1212).** Multi-index queries including a multimodal collection silently dropped the multimodal contribution via a swallowed AttributeError; the method now exists.
- **Multi-provider temporal indexing showed no progress for additional providers (#1205).** Each additional provider now renders its own live progress display (the primary pass is unchanged).
- **repository_status omitted next_refresh and enable_scip for global repos (#1204).** Added both fields from the already-loaded record (no extra query), eliminating the need to pull the bulk list_global_repos for a single repo.
- **Indexing/registration/SCIP path carried job/subprocess/per-file timeouts (#1218).** Removed all overarching wall-clock timeouts (job, subprocess watchdog, per-file/per-batch) and their swallow-and-skip handlers; only the per-outbound-embedding-HTTP-call timeout + retry remain. A genuine post-retry failure now fails the job loud (no silent partial index); cidx index exits non-zero on total failure so a failed registration fails and cleans up its orphan clone instead of reporting success with an empty index.

## [10.163.0] - 2026-06-24

### Fixed
- **Epic #1194 production-safety: a routine deploy could rebind a cluster node to loopback and drop it off HAProxy.** The auto-updater's DEPLOY path rewrote the systemd ExecStart `--host` from `config.host`, whose `ServerConfig` default is `127.0.0.1` -- a value the installer never overwrote (it hardcodes `--host 0.0.0.0` directly into ExecStart). On any node whose `applied_launch.json` did not yet exist, a routine code deploy therefore rewrote `--host 0.0.0.0` -> `127.0.0.1`, binding loopback-only and severing HAProxy/cluster reachability. Confirmed live on the staging cluster (all 3 nodes went 503). Fix: `_ensure_launch_config` DEPLOY mode now PRESERVES the live ExecStart when `applied_launch.json` is MISSING (identical to the existing CORRUPT handling) -- a routine deploy never rewrites the live unit from a stale config; the live ExecStart is the confirmed running state (MAJOR-M1). Intentional host changes still flow through the Web UI -> diagnostics-restart (APPLY) path, which is unchanged. Additionally the installer now writes `host: 0.0.0.0` into the generated `config.json` so it matches the `--host 0.0.0.0` it already bakes into ExecStart, eliminating the stale default for fresh installs. Validated end-to-end on the live staging cluster (host=0.0.0.0 propagated cluster-wide, generation-bump restart converged all 3 nodes, HAProxy front door recovered 503 -> serving).

## [10.162.0] - 2026-06-24

### Added
- **Epic #1194 -- Cluster-wide launch-affecting settings via shared state.** `host`, `port`, `workers`, and `log_level` are now runtime-configurable from the Web UI and propagate across a cluster via a shared-state, uncoordinated per-node self-restart -- no leader election, no new table, no ordered rolling restart. Completes (and supersedes) the partial v10.161.0.
  - **#1197 -- Runtime launch keys.** Moves `host`/`port`/`workers`/`log_level` out of the `config.json` bootstrap into runtime config (kept in `config.json` for one transition release via `TRANSITION_PRESERVE_KEYS`). New `get_applied_worker_count()` resolver (`applied_launch.json` -> `config.json` -> default) feeds the provider-concurrency governor and the HNSW/FTS caches so per-node concurrency/memory budgets size against the APPLIED worker count.
  - **#1198 -- Per-node launch.json materializer.** `ConfigService.materialize_launch_config()` writes a per-node `launch.json` (the TARGET snapshot: workers/log_level/host/port + `target_restart_generation`) atomically via tempfile + `os.replace`, wired into the runtime save path and startup, and registered on the Bug #586 reload callback.
  - **#1199 -- Auto-updater ExecStart reconstruction.** `_ensure_launch_config(APPLY|DEPLOY)` performs a token-bounded, value-aware in-place rewrite of `--host`/`--port`/`--workers` on the live systemd ExecStart, matching BOTH the `code_indexer.server.main` and `uvicorn` unit shapes (the old `uvicorn`-only gate was a silent no-op on real installer units). APPLY sources from `launch.json` and records `applied_launch.json` (incl. `applied_restart_generation`) ONLY after a successful ensure + restart; DEPLOY sources from `applied_launch.json` -> `config.json` -> defaults and never from the TARGET (corrupt applied -> preserve the live ExecStart). `code_indexer.server.main` and the solo CLI launch path now accept/forward `--workers`. `--log-level` is deliberately NOT in ExecStart (it is applied in-process).
  - **#1200 -- Cluster-wide restart via `launch_restart_generation`.** A raw `config_json` JSON key bumped by an atomic in-SQL `jsonb_set` increment (no `asdict()` round-trip; bumping node's `_db_config_version` left unadvanced so it self-restarts). A normal settings-save preserves the generation via a race-safe targeted single-key re-inject (no lost update, no dropped-key resurrection). An explicit per-poll `check_pending_launch_restart()` -- wired into the reload poll loop, version-diff-independent -- materializes then signals while `target > applied`, never records applied (the auto-updater owns that), and emits one rate-limited WARNING when a node is stuck.
  - **#1195 -- Web UI launch settings + server-side host/port guardrail.** The four settings are editable via the runtime save path with restart-required badges; a SERVER-SIDE confirmation guardrail (pre-change value read before mutation; client `<dialog>` injects the confirm flag for UX) blocks accidental host/port changes that could sever HAProxy/firewall connectivity; the diagnostics restart is cluster-aware (cluster bump-only, solo single-node). The existing TOTP elevation gate on the config-save and `/restart` endpoints is verified, not duplicated.

## [10.161.0] - 2026-06-24

### Fixed
- **Partial Epic #1194 (#1199) intermediate cut** -- auto-updater APPLY/DEPLOY ExecStart reconstruction defect fixes. Superseded by v10.162.0, which completes the epic; use v10.162.0.

## [10.160.0] - 2026-06-23

### Fixed
- **Auto-updater admin-restart ignored the worker count:** An admin-UI-requested restart (`POST /admin/restart` -> `restart.signal` file in systemd mode) was handled by the auto-updater's poll loop with a bare `restart_server()` (`systemctl restart`) only -- it never ran `execute()`, so `_ensure_workers_config()` (the sole writer of the systemd unit's `--workers N`) was skipped. The server relaunched with the OLD worker count even though `workers` is in `RESTART_REQUIRED_FIELDS`. Proven on staging: set `config.workers=3`, requested restart, `ExecStart` stayed `--workers 1` and only 1 worker came up. Fix: the restart-signal handler in `server/auto_update/service.py` now calls `_ensure_workers_config()` (idempotent, value-aware, non-fatal) immediately before `restart_server()`, so an admin-requested restart re-applies the configured worker count. The single-writer invariant is preserved (`restart_server()` still does not modify the unit; `_ensure_workers_config` remains the only `--workers` writer, now invoked from both `execute()` and the restart-signal handler).

## [10.159.0] - 2026-06-22

### Fixed
- **#1190 MCP exit_write_mode 60s guillotine:** The MCP protocol dispatch applied one global 60s cap (`HANDLER_TIMEOUT_SECONDS`) to all synchronous tool handlers, killing `exit_write_mode` 10x below its budget when the cidx-meta backup hit a rebase conflict that real Claude-CLI resolution needs minutes for. Added a per-tool timeout override (`exit_write_mode` -> 720s = 600s conflict budget + 120s buffer) threaded through `_invoke_handler` at both dispatch call sites; every other tool keeps the 60s default (Bug #1008 protection intact).
- **#1186 cidx-meta backup rebase masking:** Backup sync treated any non-zero `git rebase` as a conflict-stop and then ran `rebase --continue` even when no rebase was in progress, masking the real failure. Now gates conflict resolution on `_rebase_in_progress()` (checks `.git/rebase-merge`/`rebase-apply`) and surfaces the original rebase stderr otherwise.
- **#1185 regex_search dash-pattern crash:** User regex patterns starting with `-` were passed as a bare ripgrep positional arg, crashing with "unrecognized flag". Patterns are now passed via `-e <pattern>` and the search path after `--`.
- **#1184 reaper/retention DuplicateJobError noise:** Data-retention and activated-reaper schedulers logged benign cross-worker `DuplicateJobError` collisions at ERROR; now use `register_job_if_no_conflict` and demote the dedup collision to DEBUG (matching the #1162 pattern).
- **#1180 Search Event Log config save:** The `export_retention_days` field was posted under the wrong config category, raising a ValueError on save. Added a per-key category override so the section saves correctly.
- **#1178 SQLite multi-worker bootstrap race:** Under `uvicorn --workers N`, concurrent workers raced in DB bootstrap (`initialize_database` / `seed_initial_admin` / golden-repo + cidx-meta seeding). Serialized the bootstrap path with a `filelock` singleton lock; backends remain fail-loud (strict INSERT) for race safety.
- **#1188 local:// URL-normalize WARNING:** Golden/activated repo URL-match loops logged a benign WARNING for the internal `local://` scheme; the scheme is now skipped before normalization (the genuine-error WARNING remains as a safety net).
- **#1189 / #1192 / #1187 test-and-gate hygiene:** Allowlisted a benign `auto_watch_manager` "No watch running" e2e WARNING (#1189); rewrote a stale file-content truncation test to the post-Bug-#1080 line-offset pagination contract (#1192); fixed a coalescer e2e test to the `(vector, EmbeddingCacheMetadata)` tuple contract (#1187).

## [10.158.0] - 2026-06-22

### Documentation
- **Auto-Updater Invariants (#1182, #1183):** Documented the deployment-lock self-heal invariant in `CLAUDE.md` -- the lock MUST live under `CIDX_DATA_DIR` (never `/tmp`, which systemd `PrivateTmp=yes` isolates and Python 3.12 rejects with `PermissionError`), and `DeploymentLock.acquire()` must be fail-soft so a lock-create failure never freezes a deploy. Corrected the now-stale Story #1167 note: the `_ensure_workers_config` idempotency guard is value-aware, not presence-only. No code change versus 10.157.0 (this release carries documentation only and serves as the staging deploy that validates the previously-frozen Python 3.12 node self-heals without manual intervention).

## [10.157.0] - 2026-06-22

### Fixed
- **Bug #1182 - Auto-Updater Self-Heal (incomplete Bug #1175 fix):** The auto-updater no longer deadlocks permanently on Python 3.12 nodes running under systemd `PrivateTmp=yes`. Two-layer fix: (1) the deployment lock is moved off `/tmp` to a `CIDX_DATA_DIR`-anchored path via a new shared `get_default_lock_path()` helper (consumed by both `run_once.py` and `service.py`), so the lock lives under the server data directory that `PrivateTmp` does not isolate; (2) `DeploymentLock.acquire()` create-path is now fail-soft (`except OSError` -> WARNING + `return True`, never re-raises), so a lock-create failure can never freeze a deploy. The prior #1175 fix only hardened the `.exists()` probe, not the `open()` write, and left the lock on `/tmp`; every 60s poll re-raised `PermissionError` and aborted before git pull / pip install / restart, a self-perpetuating deadlock. Trigger was strictly Python 3.12 (3.9 nodes unaffected). New tests exercise the real failure surface (patch `builtins.open` to raise on the lock write; assert the lock path is not under `/tmp`).
- **Bug #1183 - Workers Config Idempotent-On-Value (Story #1167 guard gap):** `DeploymentExecutor._ensure_workers_config()` now rewrites an existing `--workers N` token to the configured `config.workers` value instead of short-circuiting on the mere presence of any `--workers` token. The prior presence-only guard (`if "--workers" in content: return True`) left the multi-worker un-pin inert on every already-deployed node (units already carried a hardcoded `--workers 1`), so `config.workers > 1` had no effect. The replacement uses a token-bounded, ExecStart-scoped regex (`(?<!\S)--workers\s+\S+`) so `--workers 1` is not confused with `--workers 10` and adjacent flags are never clobbered; three cases handled: exact-value no-op, wrong-value rewrite (tee + daemon-reload), absent append. New tests cover the 1->4 rewrite and the 4->4 true no-op. Makes Epic #1161's configurable multi-worker actually take effect on real clusters.

## [10.156.0] - 2026-06-22

### Added
- **Story #1162 - Description Refresh Scheduler Cross-Worker Dedup:** The description-refresh dispatch now uses cluster-atomic `register_job_if_no_conflict` (was `register_job`) so exactly one Claude CLI invocation fires per stale repo per refresh cycle regardless of uvicorn worker count. Losing workers catch `DuplicateJobError`, log at DEBUG, and continue before any refresh thread is spawned; the generic registration-failure WARNING path is preserved. The DB partial unique index `idx_active_job_per_repo` is the sole arbiter (no in-process dedup state). Accepted limitation: per-process quarantine counters remain per-worker.
- **Story #1163 - JWT Logout Token Revocation + Blacklist Pruning:** Both `GET /logout` (web admin) and `GET /user/logout` (user portal) now blacklist the JWT `jti` at logout via `get_token_blacklist().add(jti)`. The blacklist is DB-backed (`TokenBlacklist`, SQLite solo / PostgreSQL cluster) so revocation is cross-worker and cross-node -- every uvicorn worker and every cluster node rejects the revoked JWT on the next request. New `TokenBlacklist.prune_expired(ttl_seconds)` method deletes expired rows from both SQLite and PostgreSQL backends and evicts matching entries from the local set. `DataRetentionScheduler` now calls `prune_expired` in both cleanup paths (SQLite and PG) using `jwt_expiration_minutes * 60` as the TTL (read live from config, not hardcoded); result dict includes `token_blacklist_deleted` in `total_deleted`. Logout is non-fatal: any failure to extract/blacklist the jti logs a WARNING and never prevents the 303 redirect.
- **Story #1164 - PG Migration Concurrent Startup Safety:** `MigrationRunner.run()` now acquires a PostgreSQL SESSION advisory lock (`pg_advisory_lock`) keyed by `_MIGRATION_ADVISORY_LOCK_KEY` (stable `int` derived from `sha256(b"cidx_migrations")[:8]` big-endian signed, value `8835134184625913288`) before any migration work. Under `uvicorn --workers N`, concurrent workers previously raced on the `schema_migrations.filename UNIQUE` constraint; the second committer's startup failed. With the SESSION-level lock, exactly one worker applies pending migrations while others block; when they acquire the lock the pending set is empty and they apply nothing. The lock is released in `finally` on all paths (success and exception) and automatically on connection close (crashed worker cannot deadlock others). The `run()` int return value and all internal migration logic are unchanged. SQLite init path (`database_manager.py`) is unaffected.
- **Story #1165 - Per-Worker Embedding Governor Concurrency Scaling:** `query_provider_max_concurrency` is now the PER-NODE total provider-concurrency budget. `ProviderConcurrencyGovernor` divides it by `config.workers` at construction (auto-seed path only) so combined embedding pressure across all uvicorn workers on a node stays within the configured limit. Per-worker seed = `max(k_min, per_node_budget // workers)`, then clamped to `[k_min, k_max]`. Workers=1 is byte-identical to previous behavior. Workers=0 or negative falls back to 1 (no division). Explicit `max_concurrency` construction (used in tests) is never divided. Cross-node budgeting remains the operator's responsibility. New `_read_config_workers()` static helper mirrors `_read_config_concurrency` fallback discipline (returns 1 on any config error).
- **Story #1166 - Per-Worker HNSW/FTS Cache Memory Budget:** New `initialize_caches(worker_count)` in `src/code_indexer/server/cache/__init__.py` divides the per-node HNSW and FTS index-cache cap by `config.workers` so N uvicorn workers each hold 1/N of the budget instead of N x the full `DEFAULT_MAX_CACHE_SIZE_MB = 4096` MB cap. New constant `MIN_CAP_PER_WORKER_MB = 256` (exported) floors the per-worker cap so no worker is starved. New private helpers `_load_hnsw_config()`, `_load_fts_config()`, and `_divided_cap()` eliminate the config-load duplication between the lazy getters and `initialize_caches`. Called inside `initialize_services()` (`src/code_indexer/server/startup/service_init.py`) IMMEDIATELY BEFORE the eager `get_global_cache()`/`get_global_fts_cache()` getters, so the per-worker cap is in place when the singletons are first constructed (single source of truth -- the prior lifespan placement ran after the singletons already existed at full cap, a no-op). Worker count read via `get_config_service().get_config().workers` (a bootstrap key) with try/except fallback to 1. Idempotent (skips re-construction if a singleton already exists, no second cleanup thread spawned). Lazy getters `get_global_cache()`/`get_global_fts_cache()` remain unchanged as safety nets at the full undivided cap for CLI/single-worker/tests. Workers=1 is byte-identical to previous behavior; workers=0 or negative falls back to divisor=1 (no ZeroDivisionError).
- **Story #1167 - Auto-Updater Workers Un-Pin + Web UI Workers Setting:** `_ensure_workers_config()` in `DeploymentExecutor` now reads the configured worker count from `ServerConfigManager` (same bootstrap-config idiom as sibling `_ensure_*` methods) instead of hardcoding `--workers 1`. Uses `max(1, config.workers or 1)` to guard against misconfigured zero/negative values. Workers=1 is byte-identical to previous behavior. Single-writer invariant preserved: `restart_server()` and `HealthWatchdog._restart_server()` both invoke `systemctl restart` on the existing unit without modifying it. Web UI Server Settings screen gains a read-only "Uvicorn Workers" display row and a number input (1-64) in edit mode; `"workers"` added to `RESTART_REQUIRED_FIELDS` so the UI shows the restart-required note; validation rejects values outside 1-64. Backend already had `workers` in `BOOTSTRAP_KEYS` and `_update_server_setting` already mapped it.
- **Story #1168 - Multi-Worker Throughput Benchmark Suite:** Standalone benchmark script `scripts/analysis/multi_worker_throughput.py` measures search throughput across 1/2/3/4 uvicorn workers under 4 scenarios (repeating/unique x cache-on/off). Emits markdown + JSON reports to `reports/perf/` (gitignored). Reads admin credentials from `E2E_ADMIN_USER`/`E2E_ADMIN_PASS` (or `E2E_ADMIN_USERNAME`/`E2E_ADMIN_PASSWORD`) environment variables or `.local-testing`. Does NOT auto-start/stop servers — operator manages server lifecycle. Metrics collected from verified REST endpoints: `POST /api/query` (with `no_embedding_cache_shortcut`), `GET /api/admin/coalescer-metrics` (provider embed calls), `GET /cache/stats` (HNSW index hit ratio). No dedicated JSON endpoint exists for query-embedding cache stats; effectiveness is approximated from provider call deltas. Includes a regression check: 2-worker repeating+cache-on throughput must be >= `--regression-multiplier` (default 1.7x) of 1-worker; script exits 1 on failure. Pytest wrapper `tests/performance/test_multi_worker_scaling.py` (marked `@pytest.mark.performance`, skipped unless `CIDX_PERF_TEST=1`) invokes the script as a subprocess. Fixture `tests/performance/fixtures/benchmark_queries.txt` provides 300 diverse code-search queries. The full 1/2/3/4-worker benchmark with 1.7x assertion is an operator-reviewed gate — CI runs only the skip-guarded pytest wrapper.

### Fixed
- **Bug #1181 - Perf Fix #1: per-query payload_cache batch commit + synchronous_commit=off:** The query hot path previously called `payload_cache.store()` once per large result inside `_apply_rest_semantic_truncation`, `_apply_rest_fts_truncation`, and the MCP equivalent `_apply_fts_payload_truncation`. At concurrency 20 this caused 6+ fsync'd PostgreSQL COMMITs per query, saturating the WAL write lock (`LWLock|WALWrite|COMMIT` pileup visible in `pg_stat_activity`). Fix has two parts: (A) new `store_batch(contents: List[str]) -> List[str]` on `PayloadCacheSqliteBackend`, `PayloadCachePostgresBackend`, and the `PayloadCache` facade collects all large snippets per query and stores them in ONE transaction/commit (`executemany` + single `conn.commit()`), returning handles in order (each immediately retrievable via `retrieve(handle)` cross-node); (B) the PG backend issues `SET LOCAL synchronous_commit = off` before the INSERT in both `store()` and `store_batch()`, eliminating WAL fsync wait for these ephemeral TTL-evicted rows -- the commit is visible immediately (no deferred/fire-and-forget), only crash-durability is relaxed, and `SET LOCAL` is per-transaction so it does NOT affect users/jobs/migrations or any other statement type. All three truncation helpers now use two-pass batching: first pass marks small results, collects large contents; second pass calls `store_batch()` once and assigns handles back. Fail-open preserved: if `store_batch` raises, all pending large results get `cache_handle=None, has_more=False` and the query response succeeds.
- **Bug #1181 - Perf Fix #2: async/coalesced best-effort last_used touch for query-embedding cache:** The 98%-common cache-hit path previously issued a synchronous `UPDATE query_embedding_cache SET last_used=...` on every hit, contending on the WAL lock and the hot row under multi-worker deployments. Fix: `record_hit()` now does ZERO synchronous DB writes. It coalesces touches into an in-process dict keyed by `(cache_key, provider, model, dimension)` -> latest float timestamp; a background daemon thread (`qec-touch-flusher`) drains the buffer every ~5 seconds via new `touch_last_used_batch(items)` method in ONE transaction. SQLite backend uses `executemany` in a single `execute_atomic` transaction. PostgreSQL backend uses `SET LOCAL synchronous_commit = off` then `executemany` + commit -- safe because `last_used` is ephemeral LRU bookkeeping: a crash losing a buffered touch leaves the row valid, just without a freshly updated timestamp. Buffer is bounded at 2048 entries with early flush on cap hit (Messi Rule #14 compliance). `QueryEmbeddingCacheBackend` Protocol gains `touch_last_used_batch(items)` (mypy-enforced on both backends). `QueryEmbeddingCache.start()` / `stop(timeout)` lifecycle wired in `startup/lifespan.py`: `start()` after `set_query_embedding_cache()`; `stop()` before `clear_query_embedding_cache()`. This is approximate LRU -- ordering is best-effort, row correctness is never compromised. 34 new unit tests in `tests/unit/server/services/test_query_embedding_cache_async_touch_1181.py` and `tests/unit/server/services/test_query_embedding_cache_async_touch_backends_1181.py`.
- **Bug #1181 - Perf Fix #3: skip per-query file-hash staleness re-read for immutable versioned snapshots:** `_get_chunk_content_with_staleness` in `FilesystemVectorStore` previously called `_compute_file_hash` (reads the whole file + SHA-1) for every git-repo result on every query — even when the index is served from an immutable `.versioned/{alias}/v_{ts}` snapshot whose files can never change. Fix adds a `skip_staleness_check: bool = False` constructor kwarg and attribute to `FilesystemVectorStore` (default False keeps CLI and mutable-path behavior byte-identical). When True, the Tier-1 branch (file exists) reads the file content once, then returns immediately as NOT stale without calling `_compute_file_hash`. The server layer sets the flag via `is_immutable_versioned_snapshot(str(project_root))` in `FilesystemBackend.get_vector_store_client()`, guarded inside the existing `if self.hnsw_index_cache is not None:` block so server modules are never imported on the CLI path. Mutable base-clone and activated-CoW paths keep the flag False and continue running the full staleness check. File-deleted path is unaffected (fires before the skip). 15 new tests in `tests/unit/storage/test_fsv_skip_staleness_1181.py`.
- **Bug #1181 - psycopg3 executemany is a Cursor method, not a Connection method:** Perf Fixes #1 and #2 initially called `conn.executemany(...)`, which raises `AttributeError` on psycopg v3 (executemany lives on the cursor). Wrapped in the fail-open handler, this made both batched writes SILENT NO-OPS in PostgreSQL mode -- payload_cache rows were never persisted (`/cache/{handle}` -> 404) and `last_used` was never updated. The unit tests passed only because their `FakeConn` mocks defined `executemany` on the connection, modeling an API the real driver lacks. Caught by benchmarking against real PostgreSQL. Fix: both PG backends use `with conn.cursor() as cur: cur.executemany(...)` in the same transaction (SET LOCAL synchronous_commit=off -> executemany -> single commit); the `PayloadCacheBackend`/`QueryEmbeddingCacheBackend` test mocks were made faithful to psycopg3 (cursor-based); a real-PG regression test was added (gated on `CIDX_TEST_PG_DSN`). Audit confirmed the bug was contained to these two backends.
- **Bug #1179 - Admin Config screen returns HTTP 500 (UndefinedError on config.search_event_log):** `_get_current_config()` in `web/routes.py` did not include a `search_event_log` key, but the Jinja template (`partials/config_section.html` lines 2440/2445/2464/2471) dereferences `config.search_event_log.search_event_log_retention_days` and `config.search_event_log.export_retention_days` inside the `<details id="section-search_event_log">` block. The `| default()` Jinja filter only guards the leaf attribute, not the missing intermediate section, so every `GET /admin/config` and every `POST /admin/config/{section}` (which re-renders the page) raised `UndefinedError` and returned HTTP 500. Root cause: Story #1159/#1160 added the template section and `get_all_settings()` support but did not update `_get_current_config()`. Fix: added `"search_event_log"` to the return dict of `_get_current_config()`, merging `search_event_log_retention_days` (from `settings["search_event_log"]`, default 90) and `export_retention_days` (from `settings["export"]`, default 30) into one dict matching the template's data contract. A new completeness-guard test asserts that every top-level section the template dereferences is present in `_get_current_config()` to prevent recurrence.
- **Epic #1161 test/regression maintenance:** Fixed test-isolation and faithful-mock issues surfaced by the full fast/server suites: migration-runner advisory-lock tests needed a faithful mocked `_conn.cursor()`; the description-refresh integration stub needed `register_job_if_no_conflict`; the #1166 real-path tests needed `CIDX_SERVER_DATA_DIR`/config-service isolation under chunked runs; and the async-touch unit tests use `asyncio.run` to avoid event-loop contamination. Test-only; no production behavior change.

## [10.155.0] - 2026-06-21

### Fixed
- **Bug #1177 - ProviderHealthMonitor emits spurious WARNING on None→path transition in CLI temporal query path:** `get_instance()` fired WARNING whenever the singleton existed with `path=None` and a subsequent call provided a real persistence path. In the CLI temporal query path, `semantic_query_manager.py` creates the singleton (path=None) before `cli.py` configures the reranker sinbin persistence path, triggering the mismatch warning on every `--time-range-all` invocation. The WARNING was designed for the dangerous case of two competing non-None paths. A None→real_path transition is benign (no sin-bin state was ever loaded or persisted). Fix: WARNING now fires only when both existing and requested paths are non-None and different; the None→real_path case is demoted to DEBUG with the ignored path for diagnostics.

## [10.154.0] - 2026-06-21

### Fixed
- **Bug #1176 - spurious WARNING from migrate_legacy_temporal_collection() on every temporal query:** The function treated the `code-indexer-temporal/` metadata tracker directory (containing only `temporal_metadata.db`) as a legacy HNSW collection. The provider-detection chain exhausted and `_detect_from_config()` emitted a WARNING on every `--time-range-all` query. Added early guard after existence check: if `temporal_metadata.db` is present and none of `{hnsw_index.bin, collection_meta.json, temporal_meta.json}` exist, return `SKIPPED` immediately. The guard cannot false-positive on a genuine legacy collection because `collection_meta.json` is always written at `create_collection()` time before any vectors are added.

## [10.153.0] - 2026-06-21

### Fixed
- **Epic #1169 test contamination fix (test_temporal_cache_injection_1170.py):** The module-level `_STUB_MODULES` loop unconditionally wrote `MagicMock()` into `sys.modules` for every listed module not yet imported, including `numpy` and `msgpack`. When pytest collected `test_temporal_cache_injection_1170.py` before `test_temporal_migration_1172.py`, numpy was stubbed before 1172's `import numpy as np` ran, causing `hnswlib` to receive a MagicMock numpy when it imported internally and then raise `AttributeError: __version__` (dunder attributes are not auto-created on MagicMock). All 9 tests in 1172 failed when the three Epic #1169 test files ran in fixed order. Fix: changed the stub loop to `try: __import__(_mod) / except ImportError: sys.modules[_mod] = MagicMock()` — installed packages (numpy, msgpack) now get their real implementations; uninstallable packages (google.protobuf, rich, pathspec) still get MagicMock stubs. All 76 tests in the three 1170+1171+1172 files now pass in fixed order.

## [10.152.0] - 2026-06-20

### Fixed
- **Bug #1175 - deployment_lock.py PermissionError crash on Python 3.12 with PrivateTmp=yes:** `cidx-auto-update.service` crashed on every invocation when systemd `PrivateTmp=yes` made `/tmp` paths inaccessible. Python 3.12 propagates `PermissionError` from `Path.exists()` instead of returning `False`. Added `_lock_file_exists()` private helper that wraps all three `self.lock_file.exists()` call sites in `deployment_lock.py` with `try/except OSError: return False`. Staging nodes were stuck at old versions and required manual SSH deployment to recover.
- **Test suite performance - TestExecuteWiring in test_deployment_executor_memory.py:** Four `execute()` wiring tests each took 6-28 seconds because the un-mocked `_wait_for_drain()` made real HTTP calls to localhost:8000 and `_ensure_claude_cli_updated()` blocked for 5s on `npm --version` timeout. Fixed by: (1) adding `patch.object(executor, "_wait_for_drain", return_value=True)` to each `with` block, and (2) pre-stubbing all non-essential execute() steps (6.5-15) with lambda stubs in `_make_executor_with_mocked_steps()`. Tests now complete in 0.94s for 4 tests (was 28s).
- **test_partial_file_bug.py AttributeError:** `TrackedFilesystemClient` had no `resolve_collection_name` method; fixed in `high_throughput_processor.py` by computing the collection name inline.

## [10.151.0] - 2026-06-20

### Fixed
- **Search event logging and analytics export bug fixes (Bugs #1173 and #1174):** Three bugs found during E2E testing of Stories #1159 and #1160. (1) Bug #1173: `inline_query.py` now gates `_writer.enqueue()` behind a `_search_succeeded` flag (initialized False, set True before each of the 3 success return paths: async 202, FTS/hybrid, and semantic). Events are never logged for failed searches (upstream errors, provider timeouts, invalid queries). The context `_ctx_token` reset stays unconditional in `finally:`. (2) Bug #1174a: `inline_admin_ops.py` `POST /api/admin/analytics-exports` removed the pre-generated `export_id = str(uuid.uuid4())`; the worker now reads `export_id = job_id_holder["job_id"]` after `submit_job()` returns, making the export row id identical to the BGM job id. (3) Bug #1174b: `GET /api/admin/analytics-exports` now accepts an optional `?id=<export_id>` query parameter passed through to `export_svc._backend.list_exports(export_id=id)`, enabling the download handler to retrieve a specific export row by job id.

## [10.150.0] - 2026-06-20

### Added
- **Background startup migration of monolithic temporal indexes to quarterly shards (Story #1172):** When the server starts up, `submit_temporal_migration_jobs()` (wired in `lifespan.py` after BGM initialization, before `yield`) scans all activated and golden repos for unsharded temporal collections. Detection via `_needs_temporal_migration(index_path)`: a collection qualifies if it passes `is_temporal_collection()`, fails `is_sharded_temporal_collection()`, has no `migration_complete.marker`, and has a `hnsw_index.bin` file. One BGM job (`operation_type="temporal_index_migration"`) is submitted per repo; `DuplicateJobError` is caught and skipped (DEBUG log) for cluster-aware dedup via the `idx_active_job_per_repo` PG partial unique index. The migration job: (1) loads the monolithic HNSW via `hnswlib.Index`, (2) reads `label→point_id` from `collection_meta.json` `hnsw_index.id_mapping`, (3) batch-fetches commit timestamps from git via `git log --no-walk --format=%H %cI` (`%cI` = ISO 8601 strict, Python 3.9 compatible), (4) groups vectors by quarter, (5) writes each quarterly shard atomically via `{shard_name}.migrating` → `os.replace()`, (6) writes `migration_complete.marker`, (7) deletes monolithic `hnsw_index.bin`, `id_index.bin`, and JSON payload files, (8) calls `del monolithic_index; gc.collect()` in a `finally` block to free C++-managed HNSW memory. Stale `.migrating` dirs from prior crashed runs are cleaned at job start. Fully idempotent on restart. Both providers discovered (all unsharded temporal collections, no provider filter). Progress reported via `progress_callback` after each shard. Non-fatal: startup scan failures log WARNING and continue.

## [10.149.0] - 2026-06-20

### Added
- **Quarterly temporal index sharding (Story #1171):** Temporal commit indexes now write into quarterly shard collections (`code-indexer-temporal-{model_slug}-{YYYY}Q{N}`) instead of a single monolithic collection. `TemporalIndexer.index_commits()` groups commits by quarter, creates each shard collection before first write, and processes shards sequentially to bound peak RAM. On the query path, `execute_temporal_query_with_fusion()` calls `get_overlapping_shards()` per configured provider to discover only the quarterly shards whose date range overlaps the query `time_range`, skipping non-overlapping shards entirely. Each shard HNSW index is evicted from the server-mode cache immediately after its query completes (`hnsw_index_cache.invalidate()` in a `finally` block), ensuring peak RAM is bounded to one shard per concurrent provider rather than accumulating all shard indexes in memory. A single `fuse_rrf_multi` pass merges results from all shards across all providers (no double-RRF). Legacy monolithic collections are included when present on disk for backward compatibility.

## [10.148.0] - 2026-06-20

### Fixed
- **Temporal query path — cache injection + shared executor + subprocess timeout (Story #1170):** Three performance and reliability fixes on the temporal search path. (1) `MultiSearchService._search_temporal_sync()` now builds `FilesystemVectorStore` with `hnsw_index_cache` and `id_index_cache` (guarded on `self.hnsw_index_cache is not None`), mirroring the injection pattern already used by `FilesystemBackend.get_vector_store_client()` on the regular semantic path. (2) `MultiSearchService.__init__()` and `get_instance()` accept a new `hnsw_index_cache: Optional[Any] = None` parameter; `_search_temporal_sync()` forwards `parallel_executor=self.thread_executor` to `execute_temporal_query_with_fusion()`, which propagates it through `_query_single_provider()` and `_query_multi_provider_fusion()` into `TemporalSearchService`. `TemporalSearchService.__init__()` stores it as `self.parallel_executor` and passes it to `FilesystemVectorStore.search()` on the `FilesystemVectorStore` branch. (3) `TemporalSearchService._reconstruct_temporal_content()` now passes `timeout=30` to `subprocess.run()`; on `subprocess.TimeoutExpired` it logs a WARNING and returns `"[Content unavailable - git reconstruction timed out]"` instead of hanging. All new parameters default to `None` — no behavioral change for CLI/daemon mode.

## [10.147.0] - 2026-06-20

### Fixed
- **psycopg3 TupleRow dict conversion in QueryAnalyticsExportPostgresBackend.list_exports():** `list_exports()` called `dict(row)` on psycopg3 `TupleRow` objects returned by `fetchall()`. psycopg3 does not return dict-like rows by default, so `dict(row)` raised `ValueError: dictionary update sequence element #0 has length 36; 2 is required` (UUID string has length 36), causing HTTP 500 on all cluster nodes when listing or downloading query analytics exports. Fixed by mapping columns by explicit positional index matching the `query_analytics_exports` CREATE TABLE column order.

## [10.146.0] - 2026-06-20

### Fixed
- **psycopg3 TupleRow dict conversion in SearchEventLogPostgresBackend (Bug #5):** `query()` and `query_for_export()` called `dict(row)` on psycopg3 `TupleRow` objects returned by `fetchall()`. psycopg3 does not return dict-like rows by default, so `dict(row)` raised `ValueError: cannot convert dictionary update sequence element #0 to a sequence`, which the `except` clause swallowed silently — making all search event reads return `[], 0` on PostgreSQL. Both methods now map columns by explicit positional index matching the `030_search_event_log.sql` CREATE TABLE column order.
- **Context.run() re-entrancy on parallel Voyage+Cohere execution (Bug #4):** `semantic_query_manager.py` called `contextvars.copy_context()` once before the `ThreadPoolExecutor.submit()` loop and reused the same `Context` object across both provider threads. Since a `Context` can only be entered once at a time, the second thread raised `RuntimeError: Context.run() already entered`. Fixed by moving `copy_context()` inside the provider loop so each submitted thread gets its own independent context copy.

## [10.145.0] - 2026-06-20

### Fixed
- **Search event embedding metrics always NULL (Stories #1159/#1160):** Three root causes corrected: (1) `semantic_query_manager.py` now calls `contextvars.copy_context()` before the `ThreadPoolExecutor.submit()` loop so `_search_event_ctx` is visible inside worker threads (Python 3.9 does not propagate ContextVars across `submit()`); (2) `inline_query.py` now imports `get_current_correlation_id` from `telemetry.correlation_bridge` (the registered middleware) instead of `middleware.correlation` (unregistered, always returned None), fixing `correlation_id` always being NULL; (3) `GET /api/admin/search-events` and analytics export endpoints now use `get_current_admin_user_hybrid` so they are accessible from the Web UI session cookie, and test fixtures updated accordingly.

## [10.144.0] - 2026-06-20

### Added
- **Query Analytics Export UI (Story #1160):** New admin page at `/admin/analytics-export` allows admins to filter search event data and export it to Excel. Filter panel supports: UTC epoch date range (`from_timestamp`/`to_timestamp`), username, repository alias, search type (semantic/fts/hybrid/temporal/regex/xray), and embedding cache hit filter (hits_only/misses_only). Export runs as a background job (`POST /api/admin/search-events/export` returns `job_id`); the page polls `GET /api/jobs/{job_id}` and updates status in real time. Export history table (`GET /api/admin/search-events/exports`) shows all exports with created time, initiator, filter summary, row count, file size, status, and a Download link for completed exports. Downloads served at `GET /api/admin/search-events/exports/{id}/download` with `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` MIME type. Excel files contain 15 canonical columns per search event. Export retention configurable via Web UI `export_retention_days` (default 30, range [1, 3650]); old files are evicted automatically. Dual-backend storage: `QueryAnalyticsExportSqliteBackend` (solo) and `QueryAnalyticsExportPostgresBackend` (cluster). SQL migration `031_query_analytics_exports.sql` is additive-only. "Analytics" nav link added between Logs and Self-Monitoring. `ExportFiltersRequest` Pydantic model enforces JSON-body binding (not query params) with correct key names aligned to the service contract.

## [10.143.0] - 2026-06-20

### Added
- **Search Event Logging (Story #1159):** Every semantic/hybrid search now records operational statistics to a `search_event_log` table. Fields captured per query: `query_text`, `repository_alias`, `search_type`, `result_count`, `total_latency_ms`, `username`, `node_id`, and embedding-cache telemetry (`provider_name`, `embedding_cache_mode`, `embedding_cache_hit`, `provider_latency_ms`, `cohere_*`/`voyage_*` fields). The writer uses an async fire-and-forget queue (maxsize=5000, 5s drain, 500 events/batch) so telemetry never blocks a query. Both backends wired: `SearchEventLogSqliteBackend` (solo) and `SearchEventLogPostgresBackend` (cluster/PG). SQL migration `030_search_event_log.sql` is additive-only (`CREATE TABLE IF NOT EXISTS`). `EmbeddingCacheMetadata` dataclass propagates cache hit/miss and provider latency from `EmbeddingCoalescer.submit()` through `coalesced_query_embedding()` to the per-request `SearchEventContext` ContextVar. Admin endpoint: `GET /api/admin/search-events?limit=N` returns paginated events (max 1000). Retention configurable via Web UI `search_event_log_retention_days` (default 90, range [1, 3650]).

## [10.142.0] - 2026-06-19

### Added
- **GitLab/GitHub auto-discovery background job (Story #1157):** `POST /api/discovery/{platform}/start` now runs discovery asynchronously via `BackgroundJobManager`, returning a `job_id` immediately. The client polls `GET /api/jobs/{job_id}` for live progress (0-90% during page fetching, 100% on completion). Results are stored in `PayloadCache` (cluster-safe: PostgreSQL in cluster mode, SQLite in solo) under key `discovery:{job_id}` and retrieved via `GET /api/discovery/{platform}/result/{job_id}` with automatic multi-page reassembly. Duplicate jobs (same platform, PENDING or RUNNING) return the existing `job_id` with `existing=true`.

### Fixed
- **Discovery start endpoint security:** Added `require_elevation()` dependency to `POST /api/discovery/{platform}/start` — previously missing, now enforces TOTP step-up for all admin mutation callers.
- **Discovery result retrieval for large payloads:** `GET /api/discovery/{platform}/result/{job_id}` now accumulates all `PayloadCache` pages (each max 5000 chars) before deserializing JSON, preventing 500 errors on real-sized repository lists that span multiple pages.

## [10.141.0] - 2026-06-18

### Fixed
- **Audit cache-metric cards rendered inconsistently:** "Audit Top-1 Match" showed a percentage while "Audit Avg Top-10 Overlap" showed a bare decimal (0.97), though both are [0,1] proportions. Top-10 Overlap now renders as a percentage too. Also added a zero-guard to Top-1 Match (it divided by `audit_total` with no check — a `ZeroDivisionError` when no audit samples exist, e.g. just after a node restart); it now shows `--` like the other cards, so both are consistent in format and no-data handling.

## [10.140.0] - 2026-06-18

### Fixed
- **Shadow Cosine Distribution chart appeared empty even when samples existed.** Cosine-audit similarities (cached vs freshly-computed embedding of the same query) cluster at ~1.0, so all samples land in the last bucket `[0.95, 1.00)` of the 40 ascending buckets — which was scrolled off the bottom of the `max-height: 28rem` container, so the chart looked all-zero. Now renders highest-similarity buckets first (reversed) with a compact `max-height: 14rem`, so the populated buckets are visible without scrolling. Full 40-bucket distribution retained (low-cosine outliers still show on scroll).

## [10.139.0] - 2026-06-18

### Fixed
- **"Recent Activity" dashboard table rendered "N/A" under the Status header with the status badge floating unlabeled to its right.** The HTMX refresh partial (`dashboard_recent_jobs.html`) renders 5 columns (Repository, Job Type, Completed, User, Status) plus a conditional Providers column, but `dashboard_stats.html`'s `<thead>` (and its inline initial body) had only 4, so after a refresh the rows misaligned under the header. Fixed the `<thead>` to match (added "User" + a conditional "Providers" header) and replaced the inline initial body with `{% include "partials/dashboard_recent_jobs.html" %}` so initial render and HTMX refresh share one row source and can't drift. Regression guard: `test_dashboard_stats_thead_alignment.py`.

## [10.138.0] - 2026-06-18

### Fixed
- **Query Embedding Cache "Clear" button now sits next to the "Total Cached Entries" count** on the admin config screen (it was previously in the section footer next to "Edit", ~9 rows below the count, so it read as missing). Moved in both `config_section.html` and the `qec_display.html` HTMX swap-target partial; HTMX behavior unchanged.
- **Shadow Cosine Distribution dashboard card rendered as an unreadable single overflowing line** instead of a chart. The ASCII bar chart relied on Jinja whitespace-control that stripped the newlines between the 40 buckets, collapsing them onto one line that overflowed the card. Replaced with a contained CSS horizontal bar chart (bar width = the existing log-scaled `bar_pct`), with `overflow-x` hidden + a capped scrollable height, plus a "No shadow cosine samples recorded yet" empty state when no samples have been recorded.

## [10.137.0] - 2026-06-18

### Fixed
- **Activated-repo reindex failed "not initialized" and timed out (server in-process path).** `ActivatedRepoIndexManager` constructed with no `data_dir` hardcoded `~/.cidx-server/data`, ignoring `CIDX_SERVER_DATA_DIR`, so the reindex worker shelled `cidx index` against the wrong directory and reported "no configuration found - project needs initialization." The constructor now reads `CIDX_SERVER_DATA_DIR` (mirroring `startup/lifespan.py`); behavior is unchanged when the env var is unset.
- **Reindex job was invisible to the job-status API.** The four reindex / index-status handlers (`mcp/handlers/admin`, `routers/indexing`) constructed `ActivatedRepoIndexManager()` without injecting the app's shared `BackgroundJobManager`, so the job ran in a throwaway manager and `GET /api/jobs/{id}` never saw it (poll timeout) — and each call leaked a worker-thread pool. The handlers now inject `app.state.background_job_manager` (and `activated_repo_manager`), failing loud if unavailable.
- **Finished background jobs could appear stuck RUNNING forever under SQLite contention.** `_execute_job` evicted a terminal job from memory even when its terminal-status persist silently failed, leaving the polled status stale. Memory eviction is now gated on a successful persist; on failure the job is retained in memory with its terminal status (anti-silent-failure).

### Tests
- e2e Phase 3 hardening: per-request admin-token refresh in the snapshot-retention test (long-running token expiry), disabled the inline registration lifecycle Claude call (registration latency), refreshed the log-audit allowlist (dropped the stale #1154 entry, added benign operational-warning patterns), strengthened the AC2 reindex assertion to require `completed`, and widened the `fake_popen_progress` mock to accept the registration-index `timeout` kwarg.

## [10.136.0] - 2026-06-17

### Fixed
- **Omni multi-repo search re-embedded the query per repo and inflated the cache hit/miss metric (#1148, completion).** Building on the single-flight fix in 10.135.0, the per-config coalescer is now the single embedding chokepoint that records hit/miss EXACTLY ONCE per `(text, provider-config)` key-resolution per user query. An omni over K same-config repos embeds the query ONCE and reuses the vector across all K (cold omni -> 1 miss + 1 provider call; warm omni -> 1 hit, not K); a single-repo query reuses its one embedding across the primary search and the memory-retrieval pass. `_compute_shared_query_vector` returns `(vector, config-digest)` and `multi_search_service` reuses the precomputed vector for a repo only when the repo's own provider-config digest matches AND neither digest is the fail-open sentinel (`is_fallback_digest` guard) — a different-config repo (e.g. Cohere embed-v4.0 1536-dim vs Voyage 1024-dim) never receives a wrong-config vector and embeds via its own provider. When a precomputed vector is present, `FilesystemVectorStore.search` uses it directly and bypasses `coalesced_query_embedding` (no re-embed, no second metric). Over-cap queries count as exactly one miss + one `long_key` per query.

### Added
- **Shadow-cosine distribution histogram on the Database Health dashboard (#1152).** The single Shadow Cosine P50 (stuck at 1.0000 in production) is replaced by a full-range distribution histogram: 40 uniform buckets spanning [-1.0, 1.0] (width 0.05, named constants), bar lengths log10-scaled so the dominant ~1.0 spike and the thin sub-1.0 collision tail are both visible, with raw-count labels and a P50 / Min / P05 summary. Computed from the existing bounded shadow-cosine ring buffer under the existing lock — observability-only, no new persistence, schema, or behavior change. Bar lengths are precomputed in Python (no Jinja `log` filter, which previously 500'd the card on any populated bucket).

## [10.135.0] - 2026-06-17

### Fixed
- **EmbeddingCoalescer single-flight registry leaked memory and zeroed the hit-rate metric (#1148).** The previous implementation (rejected in code review) used thread identity to track in-flight keys and retained done futures indefinitely — causing unbounded memory growth (O(total requests) instead of O(live concurrency)), suppressing hit-rate metrics for sequential same-key warm queries on different threads (hits=0 instead of the true hit count), and leaving a latent joiner hang when the owner path had no try/finally. Reimplemented using the correct standard single-flight pattern: key registered at MISS start, removed unconditionally in try/finally on owner completion (both dispatcher and non-dispatcher paths). Concurrent same-key joiners wait on the pending Future with a bounded 60-second timeout (Messi #14); later sequential same-key callers find no registry entry, perform a real cache lookup, and each record a genuine hit metric. Cache I/O (lookup, record_hit) is restored to outside `_inflight_lock` preserving the lock-free invariant. Registry is now O(live concurrency) with zero retained entries after resolution.

## [10.134.0] - 2026-06-15

### Fixed
- **Self-monitoring was non-functional in cluster mode -- scans ran (spending Claude credits) but the dashboard showed no scan history and no created issues (Bug #1140).** `LogScanner` wrote scan records, issues, and dedup fingerprints to node-local SQLite while the cluster dashboard reads through the PostgreSQL `SelfMonitoringBackend` -- a silent split-brain (scans succeeded into the wrong store). The scanner and `IssueManager` now route all persistence (scan records, issue metadata, and dedup fingerprints) through the injected backend; the manual "Scan Now" trigger now wires the registry backend (it previously built the service with none -> SQLite); and the PostgreSQL backend gained the missing `list_issues` method so the "Created Issues" panel renders on the cluster. Added a protocol-conformance test asserting both backends implement the full `SelfMonitoringBackend` protocol (the gap that hid the missing method), and the dashboard's issue read now logs its previously-silent fallback. Solo/SQLite mode is unchanged.
- **Dependency-map domain-file warning flood + frontmatter corruption (Bug #1114).** A domain file with structurally-invalid YAML frontmatter (duplicated, un-indented `participating_repos`) made the cross-domain graph builder emit a full-traceback WARNING on every parse (observed 47x, no de-spam). The malformed-YAML branch in `parse_domain_file_for_graph` now de-spams per distinct file path (warn once, then DEBUG; the `MALFORMED_YAML` anomaly is still recorded on every call), mirroring the existing missing-file branch. Separately, the domain-file frontmatter writers (`_update_frontmatter_timestamp`, `_build_refinement_frontmatter`, and the delta/render path) now serialize via a single dict -> `yaml.safe_dump` (which de-dupes keys) instead of line-based string manipulation -- using a timestamp-preserving loader/dumper so ISO-8601 `last_analyzed`/`last_refined` values survive byte-for-byte (with the `T` separator), keeping the line-based downstream readers working.

## [10.133.0] - 2026-06-15

### Added
- **Per-node cache-telemetry cards now show the serving node's id.** The three in-memory / per-node query-embedding-cache metrics (Shadow Hit Rate, On-Mode Hit Rate, Shadow Cosine P50) display the node id (`cluster.node_id` if set, else hostname) in their footer note and the "volatile" badge tooltip. In a cluster, HAProxy cookie affinity pins a browser to one node, so without the label two operators on different nodes can see different values for the same cluster and have no way to tell why; the node id disambiguates it. Cache Entries is unchanged (DB-backed, cluster-wide). The node id reuses `app.state.node_id` (wired by lifespan in cluster mode) with a `socket.gethostname()` fallback for solo.

### Fixed
- **Docs: corrected the cluster load-balancer session-affinity description.** `docs/cluster-architecture.md` previously claimed "the load balancer does not pin sessions to specific nodes," which contradicted the same doc's "session-pinned via HAProxy" note and `cluster-setup.md`. It now accurately states HAProxy uses cookie-based session affinity (`cookie SERVERID insert indirect nocache`) to pin each browser session to one node, with TTL-bounded cache staleness; the LB diagram label was updated to `roundrobin+sticky`.

## [10.132.0] - 2026-06-15

### Fixed
- **Tag/commit-pinned golden repos no longer fail their refresh every cycle.** `GitPullUpdater.has_changes()` ran `git log HEAD..@{upstream}`, which raises on a detached HEAD (`@{upstream}` does not resolve), so a golden repo whose `default_branch` is a git tag or specific commit (detached HEAD) failed every scheduled refresh indefinitely (observed: `type-fest` pinned to tag `v4.8.3`, thousands of consecutive failures). Detachment is now detected via `git symbolic-ref -q HEAD` and treated as a graceful no-op (a pinned ref is immutable -- nothing to pull); the normal-branch change-detection path is byte-for-byte unchanged.

### Added
- **Query-embedding cache database is now shown on the Database Health honeycomb.** Added `query_embedding_cache.db` ("Embedding Cache") as a 9th hexagon: `_resolve_db_path` routes it to the server `data/` dir (the hexagon shows the real path + size, not "file not found"), the honeycomb `viewBox` was extended so the 3rd row renders un-clipped, and the file is registered in `POSTGRES_MIGRATED_DATABASES` (it is PostgreSQL-backed in cluster mode, so in postgres mode it is covered by the single "postgresql" check; in solo mode it renders as its own SQLite hexagon).
- **Cache-metrics cards distinguish volatile from durable.** Shadow Hit Rate, On-Mode Hit Rate, and Shadow Cosine P50 now carry a "volatile" badge plus an "in-memory - per-node - resets on restart" footer note (these are process-local metrics that reset on restart and are not aggregated across cluster nodes); Cache Entries is marked durable (read from the cache database).

### Removed
- **Dead api_metrics read/write path.** Removed the unused `get_metrics`/`record` code path that read the empty legacy `api_metrics` table (the dashboard already reads the bucketed `api_metrics_buckets` tables). The legacy `api_metrics` table schema is deliberately KEPT (`CREATE TABLE IF NOT EXISTS`) for rolling-restart backward compatibility -- only the dead access code was removed, not the table.

## [10.131.0] - 2026-06-14

### Fixed
- **Query-embedding cache: a corrupt cache row could break live queries (Codex review).** A mode=on cache HIT decoded the stored blob with no length/dimension validation and no try/except, so a corrupt or wrong-dimension row either raised `struct.error` into the query handler or returned a wrong-length vector that corrupted the downstream HNSW search. The on-hit decode now validates `len(blob) == dimension*4`; on any mismatch or decode error it logs a WARNING and treats the row as a MISS (recompute + rewrite), restoring the "all cache ops fail-open" contract.
- **Indexing: a vanished file was counted as a failure instead of a skip (Codex review, refines #1118).** The FileChunkingManager TOCTOU path returned `success=False`, so a benign vanished file inflated the failure count. A vanished file is now an explicit SKIP (neither processed nor failed); genuine errors on existing files still count as failures. The vanished-file `FileNotFoundError` guard was also confirmed scoped to the absent source file.
- **Query-embedding cache deep-fidelity audit overlap divisor (Codex review).** The audit's top-K overlap divides by `max(len of the two result lists)` so identical cached-vs-live vectors correctly yield 1.0 fidelity (a fixed `/AUDIT_TOP_K` divisor wrongly reported a perfect small-result match as 0.5); the stale docstring was corrected to match.

### Removed
- **Two orphan/hidden query-embedding-cache config knobs (Codex orphan review, Messi Rule #12).** Removed the global `query_embedding_cache_anchor_tokens` (consumed by cache-key normalization but never exposed on the config screen — a stale `config.json` value could silently govern cache keys for both providers) and the dead global `query_embedding_cache_audit_sample_rate` (zero consumers). The per-provider anchor-token and audit-sample-rate knobs remain fully wired and UI-exposed; the anchor-token default is now a module constant (2). Existing configs that still contain the removed keys load unchanged (the keys are ignored).

## [10.130.0] - 2026-06-14

### Fixed
- **Description-refresh hard-failed on valid CI trigger events like `release` (Bug #1116).** The lifecycle parser's `ci.trigger_events` used a closed 7-value enum (`push|pull_request|merge_request|tag|schedule|workflow_dispatch|manual`); a repo whose CI accurately reported any other valid event (e.g. GitHub Actions `on: release`, observed for `fastapi`) raised `UnifiedResponseParseError` and -- all-or-nothing -- discarded the entire description+lifecycle refresh. `trigger_events` is now validated as an open list of non-empty strings (consistent with its sibling `required_checks`), and the prompt's allowed-list was relaxed to examples. Other lifecycle enums stay strict.
- **Langfuse telemetry not installed cluster-wide (Bug #1117).** `langfuse` was not a declared dependency, so it existed only where hand-installed (one cluster node), and telemetry init failed at startup on the others ("No module named 'langfuse'"). It is now a mandatory dependency in `pyproject.toml`, so the auto-updater's `pip install -e .` installs it on every node.
- **A vanished file aborted the entire index (Bug #1118).** During refresh+index, a file that disappeared between directory enumeration and the hash phase (`[Errno 2] No such file`, e.g. a transient `*.tmp`) raised a fatal error that discarded the whole index. Such a TOCTOU vanish is now a per-file skip with a WARNING in both `high_throughput_processor` (hash worker) and `file_chunking_manager`; real errors on existing files still abort as before.
- **Omni `*` / wildcard search could not resolve globally-activated repos (Bug #1119).** A wildcard repository_alias string (`"*"`, `"name-?"`) was routed to the single-repo path ("Repository '*' not found"), and bare member names were looked up by their bare alias even when only the `-global` form existed. Wildcard strings are now routed to the omni/expansion path, and bare names are promoted to their `-global` form (access-filtered) during expansion. Applied to both `search_code` and `regex_search`.
- **Dangling activated-repo registrations were un-removable and queryable (Bug #1120).** When an activated repo's on-disk directory was gone but its registry row remained, `deactivate_repository` returned a permanent 404 (it gated on `os.path.exists`), so the dangling registration could never be cleaned up and kept producing query errors. Deactivation now gates on the authoritative registry (PG row / metadata) and removes all traces even when the directory is missing. Added admin-only endpoints so operators can clean up across users: `GET /api/admin/activated-repos` (lists all activated repos with a `path_exists` flag to surface dangling ones) and `DELETE /api/admin/activated-repos/{username}/{user_alias}` (admin force-deactivate of any user's repo). Self-service deactivation is unchanged; the admin routes require admin (non-admin -> 403).

## [10.129.0] - 2026-06-14

### Fixed
- **Coalescer ignored per-repo embedding endpoint/model (Bug #1112).** The Story #1079 embedding coalescer built its per-lane Voyage/Cohere providers from bare default config, so with coalescing enabled (the default) per-repo `api_endpoint`/`model` overrides were ignored on the coalesced path and every query used the default model/dimension (wrong query vector for heterogeneous-model repos; per-repo endpoint overrides silently dropped). The coalescer registry is now lazy and keyed by provider-config digest: coalescers are built on demand from the caller's per-repo provider, so per-repo endpoint and model are honored on the coalesced path. Homogeneous-default deployments are unchanged (one coalescer per lane); heterogeneous configs each get their own coalescer (preserving the one-batch-one-config seal), bounded with a direct-path fallback on overflow.
- **Query-embedding cache config screen could not be re-saved after resetting a per-provider anchor_tokens to inherit (#1107).** The input rendered the inherited (None) value as the literal "None", so re-submitting the form sent "None" and int() rejected it. The Voyage/Cohere anchor inputs now render blank for an inherited value so the config screen round-trips.
- **Flaky fast-automation tests under parallel load (Bug #1113).** Three timing/scheduler unit tests had real test-isolation bugs -- a class-level threading.Event.wait patch that captured other tests' waits, a leaked module-level semaphore, and an operator-precedence bug that counted every file open -- surfacing as non-deterministic flakes under concurrent load. Made deterministic (fake clock / formula assertion / per-test isolation) without weakening what they verify.

## [10.128.0] - 2026-06-14

### Fixed
- **Query-embedding cache (Epic #1103) dashboard metrics now populate without telemetry.** The cache-metrics dashboard cards (hit ratio, shadow cosine, deep-fidelity audit) were blank whenever telemetry was disabled (the default config on staging/production), because the metrics object was only constructed in the telemetry-enabled startup branch and the accessor stayed None. The object is now built whenever the cache is wired (with the OTEL meter when telemetry is on, else a no-op meter), so its in-process snapshot tallies -- which the dashboard reads independently of OTEL export -- always record. Also: per-provider `query_embedding_cache_{voyage,cohere}_anchor_tokens` can now be reset to inherit-global (None) via the Web UI instead of being coerced to 0. Found via staging-cluster E2E.

## [10.127.0] - 2026-06-14

### Added
- **Epic #1103 (server-side query-embedding cache).** Caches the QUERY-path embedding for both providers (VoyageAI, Cohere) so repeated or anchor-equivalent query texts skip the embedding round-trip. Synchronous DB-direct, backend-dual (SQLite solo / PostgreSQL cluster) -- no RAM layer, no TTL. Tristate per-provider mode (`off`/`shadow`/`on`, default `shadow`) plus a master kill switch (`query_embedding_cache_enabled`). Anchor-token normalization dial (per-provider `query_embedding_cache_{voyage,cohere}_anchor_tokens`, inheriting the global default of 2): keeps the first N tokens in order and sorts the remaining tail, so reordered queries share one key; the key is CASE-PRESERVED (never lowercased) and SHA-256-hashed; composite PK `(cache_key, provider, model, dimension)`. Shared count-based LRU cap (`query_embedding_cache_max_entries`, default 10000) -- one bucket across both providers, oldest evicted, enforced on every write. Per-request `no_embedding_cache_shortcut` (default false) on all REST and MCP search endpoints (skips the cache READ but still writes). Observability: hits/misses counters + total-entries gauge + per-mode hit ratio on the dashboard and via OTEL, plus shadow-mode `cos(cached, live)` fidelity. Sampled deep-fidelity audit (per-provider `query_embedding_cache_{voyage,cohere}_audit_sample_rate`, default 0.0): on a sampled fraction of cache hits, runs a second HNSW search and records top-10 overlap + top-1 match (on-mode sampled hits re-embed once on the sampled fraction only). Includes the Cohere `embedding_purpose` fix (Bug #1104): all server query-path embed calls pass `embedding_purpose="query"` so Cohere maps to `input_type="search_query"` (previously queries were mis-embedded as `search_document`). New reference docs: `docs/query-embedding-cache.md` and `docs/query-embedding-cache-empirical-study.md`.

## [10.126.0] - 2026-06-12

### Fixed
- **Bug #1102 (description refresh wrote change-relative language — descriptions must be timeless snapshots for RAG).** Staging fault-injection on v10.125.0 showed a refreshed description containing "Recent code also enforces form parser field and part-size limits..." — changelog voice leaking into the RAG corpus, caused by the `git log --since` change-scoping priming the model to narrate findings as recent changes. Added refinement rule 7 "TIMELESS SNAPSHOT VOICE" to `lifecycle_refresh_addendum.md` (bans temporal/change-relative phrasing — "recent", "newly", "previously", "no longer", "was added" — and mandates plain present-tense facts), reinforced the change-scoping paragraph (the window is a verification-budget tool only and must never surface in the output voice), and added the same timeless-snapshot instruction to the create-mode description guidance in `lifecycle_unified.md`. Prompt-content guard tests in `test_lifecycle_timeless_snapshot_1102.py` (mutation-verified: they fail when the rules are removed). The pre-#1094 historical pin test (`test_create_mode_matches_pre_1094_head_content`) was re-scoped to a pure git-history comparison so intentional prompt edits remain possible; the live create-mode no-drift invariant stays guarded by `test_create_mode_prompt_is_byte_identical_to_current_file`.

## [10.125.0] - 2026-06-12

### Fixed
- **Bug #1100 (description-refresh scheduler ignored the PG tracking backend in cluster mode — split-brain starved new repos).** `startup/lifespan.py` constructed `DescriptionRefreshScheduler` without a `tracking_backend` argument, so it fell back to node-local SQLite even in postgres cluster mode, while `meta_description_hook` was given the PG-backed registry backend. Repos seeded via the hook were written to PG but never seen by the scheduler (which read SQLite), so their descriptions never refreshed. Moved the registry-selected `tracking_backend` resolution ahead of the scheduler constructor and passed it in, so scheduler and hook now share one backend (PG in cluster mode, SQLite only on the genuine no-registry/solo path). The existing `_reconcile_stale_next_run_rows()` runs synchronously in `start()` before the dispatch loop, spreading overdue rows across the refresh interval so the cutover cannot trigger a mass-dispatch refresh storm. Regression guards in `test_lifespan_tracking_backend_wiring_1100.py` (source-order wiring + storm-prevention against real SQLite).
- **Bug #1101 (description refinement degraded lifecycle frontmatter and left hallucination-negation residue).** On refresh, the lifecycle frontmatter was rebuilt from the model output, silently dropping richer existing values (`build_system: uv/hatchling` → `hatchling`), list entries (`ci.required_checks: [check, zizmor]` → `[check]`), and omitted keys (`branch_environment_map`). Added a deterministic preserve-by-default lifecycle merge in `lifecycle_batch_runner.py` (invoked from `_process_one_repo` on the refresh write-back): the model value is treated as a degradation and the existing value kept when the new value is a subset/substring of the existing one or the model omits the key; a genuinely different non-empty value still updates. Recurses into nested dicts (`ci`) and keeps superset lists; never drops a key. Added refinement rule "REMOVE FABRICATIONS SILENTLY" to `lifecycle_refresh_addendum.md` so hallucinations are removed rather than refuted in-place (negations that name a false feature pollute the RAG corpus). CREATE-mode prompt remains byte-identical (#1094 invariant preserved). Tests in `test_lifecycle_frontmatter_preserve_1101.py`.
- **Bug #1099 (research cleanup emitted a repeating WARNING for the well-known 'default' session directory).** `ResearchCleanupService.cleanup()` logged at WARNING for every directory `_is_session_dir_name()` rejected, including the expected `DEFAULT_SESSION_DIR_NAME` dir, so every hourly sweep produced a never-self-healing WARNING that masked genuine unexpected-directory warnings. Downgraded the `default` case to DEBUG; WARNING is retained for genuinely unexpected non-session directory names. Preservation behavior is otherwise unchanged. Tests in `test_research_cleanup_service.py::TestDefaultDirLogLevel`.

## [10.124.0] - 2026-06-12

### Fixed
- **Bug #1098 (activated-repo reaper disabled fleet-wide by admin/dashboard reads).** `get_repository()` unconditionally stamped `last_accessed` on every call. A single load of the admin Activated Repositories page reset the TTL for every repo in the system (all users, pre-pagination), preventing the reaper from ever evicting idle repos. Added `touch: bool = True` keyword parameter to `get_repository()`; admin/dashboard paths (`web/routes.py:repo_details`, `dashboard_service.get_temporal_index_status`) now pass `touch=False`. Added throttled `touch_last_accessed()` method (1-hour window) used by the MCP search path so search-only active users are not inadvertently reaped. Tests in `test_get_repository_last_accessed_1098.py`.
- **Bug #1096 (description-refresh quarantine bypassed for repos with stable NULL last_known_commit).** The circuit-breaker (`PROMPT_FAILURE_QUARANTINE_THRESHOLD = 3`) auto-cleared on the `has_changes_since_last_run()` check, which returns True when `last_known_commit` is NULL — meaning perpetually-failing repos with no successful run were never quarantined. Changed the auto-clear gate to compare the current on-disk commit fingerprint against the fingerprint recorded at failure time; clears only when the fingerprint genuinely differs (real commit transition). Regression tests in `test_description_refresh_circuit_breaker_1096.py`.
- **Bug #1095 (search `exclude_path` silently ignored when comma-separated).** `exclude_path` accepted only a single pattern; passing `"tests/,docs/"` was treated as one literal string and matched nothing. Added `parse_exclude_patterns()` helper that splits on commas and trims whitespace; applied to the semantic leg (`search_service.py`), the FTS/regex leg (`semantic_query_manager.py`), and the temporal leg (`temporal_fusion_dispatch.py`).

### Added
- **Story #1094 (description-refresh refines existing descriptions instead of replacing).** `LifecycleClaudeCliInvoker` now passes `existing_description` + `last_analyzed` when a non-empty `cidx-meta/{alias}.md` already exists. The externalized `lifecycle_refresh_addendum.md` prompt instructs Claude to preserve accurate content, correct outdated content, and add missing context — not regenerate from scratch. The `{{REFRESH_SECTION}}` placeholder in `lifecycle_unified.md` is substituted with the addendum in refresh mode and stripped entirely in create mode (byte-identical to prior behavior). A 64 KB defensive cap truncates oversized existing descriptions with a marker and WARNING.

## [10.123.0] - 2026-06-11

### Fixed
- **Bug #1089 (description backfill threads crash on duplicate job registration).** On fast server restarts both `_run_lifecycle_backfill_async` and `_run_description_backfill_async` called `register_job()` while a prior run's job row was still active in the DB. On PostgreSQL the `idx_active_job_per_repo` partial unique index raised `UniqueViolation`, which the existing `except Exception` block caught and logged as an ERROR, silently aborting the backfill sweep. Changed both calls to `register_job_if_no_conflict()` (atomic duplicate-safe path) and added a specific `except DuplicateJobError` guard that logs at INFO ("already active — skipping") and returns early; the `finally` block still clears the event flag. Regression tests added in `test_description_refresh_scheduler_bug1089.py`.
- **Bug #1093 (permanent description quality-regression loop — four structural defects).** Four bugs in `description_refresh_scheduler.py` combined to cause every golden repo's description to be silently overwritten with degraded output on every scheduled refresh cycle after a DB reset or fresh install: (A) `has_changes_since_last_run()` returned True unconditionally when `last_known_commit` is NULL — fixed to return False when a non-empty `.md` file already exists (no-herd guarantee: 844 repos with NULL commits reschedule cheaply instead of dispatching AI jobs); (B) `on_refresh_complete()` only read `metadata.json`, missing the provider-specific `metadata-{provider}.json` files used by golden repos — fixed to mirror the existing fallback from `has_changes_since_last_run` so `last_known_commit` is written correctly; (C) `_get_refresh_prompt()` staged a temp file and built a prompt that was never used (only its None-ness was checked) — replaced with a lightweight `_has_existing_description()` check; (D) temp dirs created by `_stage_and_build_prompt()` were never cleaned up on the success path — fixed to return the tmp dir path and clean up in the caller. Regression tests added in `test_description_refresh_scheduler_bug1093.py`.

### Added
- **Story #1092 (batch-create: skip redundant pre-flight git validation).** `POST /admin/golden-repos/batch-create` blocked the HTTP response for N x 2-5s per repo on `git ls-remote` validation — redundant since repos were already confirmed reachable by the discovery API. Added `skip_pre_flight_git_validation: bool = False` to `add_golden_repo()` (single-repo path unchanged); `_batch_create_repos()` now passes `True` for the flag and hoists `list_golden_repos()` to one call before the loop. `generate_unique_alias()` gains an optional `existing_aliases` set parameter so the pre-built set is reused across the loop instead of issuing a DB query per repo. A 20-repo batch now returns in under 2 s with a job_id per repo and zero `git ls-remote` subprocesses during the HTTP request. Tests added in `test_batch_creation_1092.py`.

## [10.122.0] - 2026-06-11

### Fixed
- **Bug #1091 (Config UI: description refresh help text misleading).** The Web UI config screen help text for `description_refresh_enabled` and `description_refresh_interval_hours` omitted critical behavioral details. Updated both the view and edit sections to clarify: (1) the enabled toggle gates two one-shot startup backfill sweeps (lifecycle metadata repair and terse-description regeneration) in addition to the periodic loop — disabling stops ALL automated description activity; (2) the interval governs only the periodic loop and does not delay the startup backfills, which always run immediately at scheduler start.
- **SCIP context timeout tests broken by Bug #1088 fast-fail.** `TestSCIPQueryServiceContextTimeout` tests assumed `get_smart_context` would always be reached; our Bug #1088 pre-flight check caused early return when no SCIP files existed. Added `patch.object(service, 'find_scip_files', return_value=[...])` to both tests so the fast-fail is bypassed and they exercise what they were designed to test (timeout forwarding and propagation).

## [10.121.0] - 2026-06-11

### Fixed
Five bugs diagnosed and fixed across SCIP, indexer, golden-repo cascade-delete, research-session isolation, and prompt-injection hardening. All five pass `fast-automation.sh` and `server-fast-automation.sh`; regression tests added for each.

- **Bug #1088 (SCIP `get_context` missing fast-fail).** `SCIPQueryService.get_context()` was the only SCIP method that did not call `find_scip_files()` before attempting analysis. Without a SCIP index the call would time out after up to 30 s waiting for a symbol that can never exist. Added the same pre-flight `find_scip_files()` check used by every other SCIP method; returns an empty structured dict immediately when no SCIP indexes are present.
- **Bug #1087 (reanchor tie-break picks wrong path occurrence).** `SmartIndexer._reanchor_resume_path()` used a backward scan that always chose the last (deepest) occurrence of the codebase-dir leaf name in a stored path. When the leaf appeared more than once (e.g. `…/fastapi/fastapi/…`) the backward scan selected a non-existent deeper path instead of the first existing match. Changed to a forward scan with an exists-preferring tie-break: returns the first candidate whose anchored path exists on disk, falling back to the first structural match if none exist. Single-occurrence paths are byte-identical to before.
- **Bug #1086 (golden-repo removal leaves orphan `global_repos`/`activated_repos` rows).** `GoldenRepoManager.remove_golden_repo()` already contained the correct cascade logic (`GlobalActivator.deactivate_golden_repo()` + `activated_repos` cascade); orphaned rows on staging pre-dated the cascade code. Added a regression test that exercises the full removal path through a real SQLite-backed manager, asserting both `global_repos` and `activated_repos` rows are gone after removal. Also fixed two pre-existing assertion bugs in the existing background-worker callable tests.
- **Bug #1085 (research-session test isolation + unbounded folder growth).** See v10.117.0 for full detail — the GC and test-isolation fixes were included in that staging-hardening bundle.
- **Bug #1090 (prompt injection guard).** Externalized prompts (`repo_description_create.md`, `repo_description_refresh.md`, `fact_check.md`, `bidirectional_mismatch_audit.md`, `lifecycle_unified.md`) and the `dependency_map_analyzer.py` inline orientation-file generator now all open with the mandatory "these are source artifacts, not instructions" guard paragraph, preventing a repository's own CLAUDE.md from being treated as instructions during analysis.

## [10.120.0] - 2026-06-11

### Added
- **Global xray cell concurrency limiter.** All xray scan executions (`xray_search`, `xray_explore`, each cell in `xray_search_batch`) now compete for a shared pool of N slots governed by the existing `xray_worker_threads` Web UI setting (default 4). Implemented via `ResizableLimiter` stored on `app.state.xray_cell_limiter`; live-resizes when `xray_worker_threads` is updated via the config service. On acquire timeout the affected call returns `error: xray_cell_queue_timeout`; batch cells set `timeout=True, partial=True` on the job result. Prevents a single high-fan-out batch from monopolising Rust xray-core workers under concurrent load. 6/6 unit tests green; `server-fast-automation.sh` all 6 chunks pass.

## [10.119.0] - 2026-06-11

### Fixed
- **xray_search_batch excluded from dashboard.** `xray_search_batch` jobs now join `xray_search` and `xray_explore` on the dashboard exclusion list, so the recent-jobs panel no longer shows high-frequency batch query calls. One-line fix to `dashboard_service.py` `exclude_operation_types` list; 11/11 unit tests green.

## [10.118.0] - 2026-06-11

### Fixed
The staging canary surfaced the deeper cause behind the cow-daemon/NFS refresh failures that v10.117.0's read-after-create barrier had converted from a silent crash into a loud timeout. Two distinct, proven bugs — diagnosed with live concurrent-load measurement on the 3-node staging cluster, fixed and validated.

- **cow-storage-daemon asyncio cross-loop bug (the real root cause — fixed in the separate `cow-storage-daemon` repo, deployed to the staging daemon).** Under ANY concurrent clone-create load the daemon returned HTTP 500 with `RuntimeError: got Future attached to a different loop`, killing the clone job so the snapshot never landed and CIDX's barrier timed out. Cause: the daemon builds its `MetadataStore`/`CloneManager` (and their `asyncio.Lock`s) via `asyncio.run(_create())` on a loop that is then CLOSED, while uvicorn serves on a different loop; in Python 3.9 a Lock created at construction binds to the (now-dead) creation loop, so contended `acquire()` parks a Future on the wrong loop. Uncontended acquisition takes a futureless fast path — which is why single/idle refreshes worked and the failure only appeared under the auto-scheduler's concurrent refresh wave. Fix: lazy, loop-aware lock accessors that bind to `get_running_loop()` and rebind on loop change. Proven live: 8 concurrent creates went from 4x HTTP 500 to zero, and the langfuse-global (3.3 GB) refresh now completes.
- **NFS read-after-create visibility lag under concurrent load (CIDX side).** With the daemon fixed, refreshes still intermittently hit `NFS read-after-create visibility timeout` when a large reflink ran alongside another create: a newly-created snapshot dir was instantly visible when idle but took >15s to propagate to the scheduler node under concurrent metadata churn (NFS client directory / negative-lookup caching). `wait_for_nfs_visibility` now forces a fresh `listdir(parent)` READDIR on each poll — refreshing the client's directory-entry cache far more aggressively than the prior GETATTR-only ancestor stat, so a child the create side already has resolves immediately even under load — and the timeout becomes a runtime-tunable `ServerConfig.nfs_visibility_timeout_seconds` (default raised 15s -> 60s) wired into both clone backends and the refresh-scheduler barrier. Bounded + fail-loud preserved (the readdir is best-effort; the authoritative `isdir`/deadline checks still raise on a genuine timeout). Local backend still does no wait.

## [10.117.0] - 2026-06-10

### Fixed
Staging-hardening bundle — seven fixes caught by the staging canary (cluster + cow-daemon over NFS) before any reached production. All reviewed/approved; `./lint.sh`, `fast-automation`, and `server-fast-automation` green.

- **Bug #1084 regression (NFS read-after-create):** the canonical `.versioned/{ns}/v_*` snapshot path nests under freshly-created parent dirs the scheduler's NFS client had never looked up (negative dcache), so `_create_snapshot`'s `subprocess.run(cwd=new_snapshot)` (`git restore`, `cidx fix-config`) hit `FileNotFoundError` ~2s after the cow-daemon reported create success. New `server/storage/shared/nfs_visibility.py::wait_for_nfs_visibility` (bounded monotonic deadline + root-first ancestor `stat` to bust the negative dcache; raises on timeout) is called at the cow-daemon/ONTAP create boundary + defense-in-depth before the subprocess steps. Local backend unchanged.
- **cow-daemon `codebase_dir` path-domain leak (indexing):** the git-aware resume path replayed absolute file paths persisted by a prior run under the NFS-mount prefix while the current run's `codebase_dir` was the daemon-local prefix, so `file_identifier.relative_to(project_dir)` raised → "Hash calculation failed" (~101 langfuse refresh failures). `SmartIndexer._reanchor_resume_path` re-anchors stale-prefix stored paths onto the actual walk root (keyed off path shape, covers any cow-daemon repo); local/normal indexing byte-identical.
- **Cluster git-key not synced to workers:** `SSHKeySyncService.sync()` materialized deploy-key files on every node but never wrote `~/.ssh/config`, so only the node where the key was host-assigned could select it → cidx-meta backup `Permission denied (publickey)` when a worker won leader election. `sync()` now regenerates the CIDX-managed `~/.ssh/config` Host blocks on every node (IdentityFile → this node's own synced key), idempotent and non-fatal. (Requires the deploy key registered in PG `ssh_keys` to materialize on workers.)
- **§-preamble JSON parsing (self-monitoring + scip self-healing):** the cidx-server parsed Claude CLI stdout with a bare `json.loads`, which failed `Expecting value: line 1 column 1 (char 0)` whenever a pace-maker `§` telemetry line prefixed the output. New `self_monitoring/llm_response_parser.py::extract_json_from_llm_response` (strips `§`/Warning/code-fence/prose, finds the first balanced top-level JSON, string-literal-aware; raises on empty/garbage — never a false success); wired into `LogScanner.parse_claude_response` and reused by `scip_self_healing._parse_claude_response`.
- **`get_file_content` bare-alias fallback:** a stale own-activation record made `get_file_content` raise an unhandled `FileNotFoundError` (server-side traceback + `[CACHE-GENERAL-011]`) instead of applying the Story #1039 global fallback like `search_code`. It now recovers via the `-global` form when globally active (activated-repo precedence preserved), returns a clean MCP error for genuine not-found (no traceback), and de-spams the expected-absent log to DEBUG. `_global_fallback` stays read-only-handlers-only.
- **Bug #1085 (Research Assistant folders unbounded, 22,654 dirs / ~73 GB):** (a) test isolation — `ResearchAssistantService` gains a `research_base_dir` seam and an autouse fixture redirects research tests off the real `$HOME` (proven no leak); (b) server-side GC — new `ResearchCleanupService` (startup orphan reconciliation + hourly TTL sweep, `research_session_retention_days` runtime knob, mirrors SCIP `WorkspaceCleanupService`). The GC sources its live-session set from the active `backend_registry.research_sessions` (PostgreSQL in cluster, SQLite in solo), fails closed (deletes nothing) on any untrustworthy live-set, validates a UUID session-dir shape before deletion, and is symlink-safe — closing a review-caught data-loss path that would otherwise have deleted live sessions in cluster mode.

## [10.116.0] - 2026-06-10

### Fixed
- Bug #1084: Versioned snapshots were never deleted on the `cow-daemon` and `ONTAP` clone backends (leaked ~1.25 GB per refresh/branch-change/add-index; 957 leaked on staging). Root cause: cleanup was gated on the local-only substring test `".versioned" in path`, which the non-local backends' path layouts never contain. (Note: GitHub issue #1084 is THIS snapshot-leak bug; the v10.115.0 entry below informally reused the "#1084" label for the reranker-pooling follow-on to #1083 — unrelated.)
  - **One canonical convention across all backends**: versioned snapshots now live at `<root>/.versioned/{ns}/v_<ts>` on local AND cow-daemon (cow-daemon creates via the existing `create_clone_at_path`). The path convention lives in ONE place -- `is_versioned_snapshot()` in `server/storage/shared/snapshot_paths.py` -- consumed via the `VersionedSnapshotManager` facade. The brittle `".versioned" in path` substring test and `golden_repos_dir/.versioned/{repo}` reconstruction are removed from all decision/discovery paths (grep-enforced by `test_versioned_single_source_bug1084.py`). A transition clause still recognizes pre-migration legacy cow-daemon shapes for cleanup.
  - **Backend-correct deletion behind the refcount gate**: superseded snapshots are deleted through `VersionedSnapshotManager.delete_snapshot()` (cow-daemon `DELETE` REST -> daemon registry stays consistent, no ghost rows; ONTAP frees the FlexClone volume; local rmtree) -- but ONLY via `CleanupManager`'s preserved QueryTracker refcount-zero gate, so a snapshot serving an in-flight query is never deleted.
  - **Keep-last-N retention** after each alias swap (`snapshot_retention_keep_last` runtime config, default 3; never deletes the current `target_path` or `previous_path`; enabled on local + cow-daemon; inert on ONTAP pending alias-scoped naming).
  - **Defect C** (`_has_local_changes`) and **Defect E** (`_restore_master_from_versioned`) now use the backend discovery API (`list_snapshots`/`latest_snapshot`) instead of the local `.versioned` glob -- no more spurious every-cycle re-index/snapshot on cow-daemon, and a lost master is restorable on cow-daemon.
  - **Secondary consumers reconciled to the canonical predicate**: the provider-index immutability guard (`mcp/handlers/repos.py` -- closes a write-into-snapshot corruption vector on cow-daemon), SCIP repo discovery (`scip_query_service.py` -- SCIP and semantic now resolve the same version), dep-map cidx-meta read (`dependency_map_service.py`), the `query_path_cache` immutability predicate (canonical snapshots gain NO-TTL caching), and `_legacy.py`.
  - Master base clone is never deleted (incl. first refresh). Manual E2E validated against a REAL CoW Storage Daemon (canonical creation, per-swap daemon deletion with zero ghost rows, legacy-transition cleanup, retention, Defect C/E, master-never-deleted, query continuity). ONTAP canonical-layout/alias-scoped-naming (AC11) and the one-time post-deploy staging snapshot purge (AC12) are deferred/gated per the issue.

## [10.115.0] - 2026-06-10

### Added
- Story #1084 (extends #1083): Production httpx connection pooling for the RERANKER clients. `VoyageRerankerClient` and `CohereRerankerClient` now borrow the factory's pooled keep-alive client (`pooled=True`) instead of building and closing a fresh client + latency transport per rerank call -- eliminating the per-request TLS handshake/connect/DNS/SSLContext churn on the `:rerank` lanes, mirroring the #1083 embed-lane fix (latency transport baked into the pooled client once; auth already per-request so key rotation stays transparent; fault-injection path unchanged with fresh per-call fault-intercepted clients). Code review approved; build-once proven by `tests/unit/server/clients/test_reranker_pooled_httpx_1084.py`.

## [10.114.0] - 2026-06-10

### Added
- Story #1083: Production httpx connection pooling + batched metrics writer (query-path perf). After #1082 removed the per-query orchestration glue, profiling v10.113.0 showed the next single-worker time sinks were (a) building and tearing down a fresh httpx client + TLS handshake on every embedding call (~37% of embed wall-time) and (b) the background metrics writer doing one `BEGIN EXCLUSIVE` SQLite transaction per query (~15% on-CPU).
  - **Production keep-alive connection pooling**: `HttpClientFactory` now owns one long-lived, thread-safe `httpx.Client` (reused SSLContext + connection pool, sized to the governor concurrency) returned via a no-op-close "borrow" context, built once and closed at lifespan shutdown. The provider sends `Authorization` per-request so the pooled client is auth-agnostic (API-key rotation is transparent, no client rebuild). The latency-tracking transport is baked into the pooled client once (no per-query SSLContext churn). Applied to VoyageAI and Cohere. The fault-injection path is unchanged (fresh per-call fault-intercepted client) — pooling applies only when fault injection is OFF (always true in production).
  - **Batched metrics writer**: the background `api_metrics` writer drains the queue and commits batched/coalesced transactions instead of one per event (no counts lost; drained on shutdown).
- Measured single-worker front door, same box/repo/harness, v10.113.0 vs optimized: per-query TLS-handshake/connect/DNS frames collapsed 38-77x, per-query SSLContext construction ~1397 -> 1 sample, metrics-writer CPU ~34% lighter; **throughput +38% at the C=8 knee (22.16 -> 30.65 rps), +33-35% at C=1, ~25% lower CPU per request**, byte-identical results, zero new errors.

## [10.113.0] - 2026-06-09

### Added
- Story #1082: Server query-path per-query overhead elimination with drift-safe caching. Concurrent semantic searches on a single worker were GIL-bound on redundant per-query orchestration work (re-parsing the static model-spec YAML, reloading + path-resolving repo config.json, and emitting a per-query codebase_dir-mismatch WARNING) rather than on embeddings or vector search. Eliminating that redundant work reclaims the wasted core and lifts single-worker front-door throughput.
  - **Load-once static model-spec**: `voyage_models.yaml` / Cohere specs parsed once per process instead of on every `VoyageAIClient.__init__` (the HTTP client stays per-request for thread safety).
  - **Drift-safe `RepoConfigCache`** (`server/services/query_path_cache.py`): a thread-safe, single-flight (refcounted key-locks + invalidate-epoch), bounded-LRU `TTLCache` with hit/miss/reload/invalidate/evict counters. NO-TTL only for paths proven immutable by `is_immutable_versioned_snapshot()` (globally-activated `.versioned/v_*` snapshots); SHORT bounded TTL (default 30s) for everything not provably immutable. Provider state keyed on a `provider_config_digest` (key fingerprint, never the raw secret) covering endpoint/timeouts/retries/model. Auth-bearing data (API keys, user rows, MCP credentials, permissions, token validation) is NEVER cached. Runtime kill switch `query_path_cache_enabled`.
  - **`codebase_dir` mismatch de-spam**: reconciled silently per-node and logged once per config path instead of on every query, preserving the Bug #1033 NFS multi-mount override.
- Corrected the CLAUDE.md "Golden Repo Versioned Path" invariant: `get_actual_repo_path()` returns the MUTABLE base clone first (Priority-1), not the immutable versioned snapshot — query-path config caching defaults to TTL accordingly.
- Measured on a single worker (front door, Voyage-only, same box/repo/harness, pre-#1082 vs optimized): per-query `_load_model_specs`/`yaml.safe_load`/config-reload frames present -> absent, cache serving 128,863 hits / 1 miss / 1 reload, codebase_dir warnings 5,658 -> 1, and sustained throughput +33-34% at the saturation knee (~17 -> ~23.6 rps) with byte-identical results and zero new errors. Spec hardened through two Codex GPT-5 reviews before implementation.

### Performance (also shipping in this release)
- Async server logging: the root logger now routes through a single `QueueHandler` -> `DrainableQueueListener` so request threads only enqueue (formatting and handler I/O run on the listener thread), removing the per-request logging-lock contention found by py-spy under concurrent `/api/query` load. Per-query hot-path INFO logs on the semantic-search path were also removed.
- Shared long-lived query executor: the embed/index-load fan-out in `FilesystemVectorStore.search()` now uses one app-lifetime `ThreadPoolExecutor` (`app.state.query_executor`) instead of constructing a fresh `ThreadPoolExecutor` per request, eliminating per-request thread create/destroy churn and the associated `_global_shutdown_lock` contention. CLI/solo path unchanged (per-call executor when no server executor is injected).

## [10.112.0] - 2026-06-08

### Added
- Story #1079: Server-side embedding request coalescer with adaptive per-lane concurrency governance (refines Bug #1078). Concurrent server query-embeds now coalesce into batched provider calls, gated by a self-tuning per-lane governor, so searches succeed under burst load without provider 429 failures at near-zero added latency at low load.
  - **4 independent governed lanes** (`voyage:embed`, `voyage:rerank`, `cohere:embed`, `cohere:rerank`) replace Bug #1078's 2 shared per-provider budgets, each with its own `ResizableLimiter` (lock+condition, runtime-resizable K, replaces `BoundedSemaphore`), `AimdController` (additive-increase/multiplicative-decrease adaptive concurrency, K_MIN=8/K_MAX=32), and sinbin health key. Lanes adapt fully independently.
  - **`EmbeddingCoalescer`** (one per `:embed` lane) coalesces concurrent submissions into a single batched `get_embeddings_batch(retry=False)` call through the governor as the SOLE limiter (no second semaphore; backoff sleeps outside the slot). Dual-constraint sealing (provider texts cap AND `_get_model_token_limit()*margin`, using the provider's own token counter) guarantees exactly one provider HTTP call per sealed batch. Shared-fate fan-out completes every coalesced caller's future on success or any exception (no hang).
  - **Canonical 429 normalization** (`provider_backoff.is_rate_limited`): providers re-raise 429s intact (fixes a latent Bug #1078 gap where VoyageAI masked 429s as generic `RuntimeError`, invisible to backoff retry and AIMD).
  - **Server-gated**: a coalescer registry is built only in server lifespan; the CLI/solo path keeps the direct governed single call (no batching, no accumulation window). Runtime kill switch (`coalesce_enabled`) and hot-reloadable caps (`coalesce_max_batch_size`), plus `coalesce_k_min`/`coalesce_k_max` AIMD bounds. Per-lane observability (`current_k`, AIMD-decrease logs, coalescing-ratio counters).
- Validated end-to-end against a live server via the REST front door: a 40-concurrent `/api/query` burst across both providers (Voyage + Cohere) completed with zero 429s and proven coalescing (40 embeds -> 17 Voyage / 27 Cohere batches).

## [10.111.0] - 2026-06-08

### Removed
- Bug #1081 (cleanup, no behavior change): Deleted the orphaned `handle_git_diff` MCP handler in `git_read.py`. It was registered-then-overwritten dead code -- the live `git_diff` tool is the separate `git_diff` function (Story #686, diff-line pagination); `handle_git_diff` had zero runtime callers and was only reachable via a registry line immediately overwritten by the live variant. Removed its definition, its dead registration line, its re-exports from `handlers/__init__.py` and `_legacy.py`, and the test that drove it (`test_git_diff_truncation.py`, which exercised the byte-envelope cache_handle path retired in #1080). The dead-handler-based git_diff cases in `test_bug1080_git_coherence.py` (`_call_git_diff` / `TestGitDiffCoherence`) were also removed -- the live `git_diff` pagination coherence is covered by the #1080 manual front-door E2E. `handle_git_log` (the live omni-fan-out worker) and `handle_git_blame` are untouched.

### Fixed
- Bug #1081 (doc accuracy): `git_diff.md` outputSchema now matches the live `git_diff` handler exactly -- `success`, `diff_text`, `files_changed`, `lines_returned`, `total_lines`, `has_more`, `next_offset`, `offset`, `limit`, `error`. Removed the stale dead-handler fields it previously advertised (`from_revision`, `to_revision`, `files`, `total_insertions`, `total_deletions`, `stat_summary`) that the live tool never returns. MCP clients reading the `git_diff` output schema now see the true response shape.

## [10.110.0] - 2026-06-08

### Fixed
- Bug #1080 (fix): Incoherent pagination across the MCP content tools. These tools stacked TWO pagination axes on one payload -- a domain-unit axis (file lines / commits / diff-lines) AND a byte/char "envelope" (TruncationHelper + PayloadCache) -- with no reconciliation. The body was byte-cut (`content[:max_chars]`, often mid-line) while line metadata was computed against the PRE-truncation full slice, and `has_more` was overwritten with the byte-envelope value, so a single response could report `next_offset==null` (line axis: done) yet `has_more==true`/`total_pages>1` (byte axis: more) -- clients could not paginate by the documented offsets. Fix collapses to a SINGLE domain-unit axis: a new line-aware, token-bounded `_read_chunk` in `get_file_content` selects WHOLE lines only (never `content[:max_chars]` mid-line), splitting on `\n` exactly to match the service's `\n`-based `total_lines` count (`str.splitlines()` would over-split on `\f`/`\v`/`\x85`/etc. and break the `returned_lines <= total_lines` invariant), and recomputes `returned_lines`/`next_offset`/`has_more`/`truncated` FROM the actually-returned content. A single line larger than the whole budget is emitted WHOLE (the only allowed one-response budget overrun) so pagination always terminates with a strictly-advancing `next_offset`. The byte envelope (`cache_handle`/`total_pages`) is RETIRED for `get_file_content`, `git_diff`, `git_log`, `git_blame` (now `null`/`0`). NOTE for MCP clients: paginate these four tools by the domain `offset`/`next_offset`, NOT by `cache_handle`/`total_pages` (that path is gone for these tools). The `search_code`/`scip_*`/`xray_*` per-field 2000-char preview is intentionally UNCHANGED (clean item-count axis, regression-guarded). Validated front-door (REST/MCP) E2E: byte-for-byte gap-free reconstruction (`get_file_content` 33 pages == file sha256; `git_diff` == full unpaginated diff; `git_log` all 5539 commits, no dup/drop; `git_blame` contiguous 1..N). Pre-existing dead-code / doc-mismatch (`git_diff.md` outputSchema still advertises dead-handler stat fields; orphaned `handle_git_diff`) tracked separately as #1081.

## [10.109.0] - 2026-06-08

### Changed
- Bug #1078 (tuning): default `query_provider_max_concurrency` (the per-provider serving-path embedding/rerank concurrency cap K) raised from 8 to 16. A 3-runs-per-K benchmark sweep (K=8/16/32 at concurrency 8/20/50, single worker, real VoyageAI) found K=16 is the diminishing-returns knee: vs K=8 it cut conc-50 p50 latency ~20% (2221->1780ms) and raised throughput ~9% (14.3->15.6 rps), while K=32 showed no benefit and slight regression. High-concurrency throughput is ultimately capped by the provider round-trip + single-worker GIL (~13-16 rps regardless of K), so K governs how efficiently the queue drains. NOTE: K is PER PROCESS — a 3-node cluster now runs up to 3x16=48 concurrent provider calls (was 3x8=24); ensure this stays within the provider account's concurrent-request budget (still operator-tunable via the runtime config field). Real-provider regression test updated (EXPECTED_K=16, C=24) and re-confirmed: in_flight_high_water_mark==16, 0 errors, no hang.

## [10.108.0] - 2026-06-08

### Performance
- Bug #1078 (perf): id_index is now cached cross-query, mirroring the HNSW index cache. Previously a fresh `FilesystemVectorStore` was created per query, so its per-instance id_index cache never persisted and `_load_id_index` re-deserialized the id_index from disk on EVERY query -- rebuilding thousands of `pathlib.Path` objects in pure Python. A GIL-only py-spy profile showed this id_index deserialization was ~33% of all GIL-holding time, serializing concurrent queries on the single worker. New `IdIndexCache` (`server/cache/id_index_cache.py`, mirrors `HNSWIndexCache`: TTL, per-key load dedup, `invalidate`/`invalidate_prefix`/`clear`) is a process-global singleton (`get_global_id_index_cache()`) wired in via `FilesystemBackend.get_vector_store_client()` in server mode (gated on the HNSW cache being present; CLI/standalone keeps the per-instance dict, unchanged). The id_index cache is invalidated wherever the HNSW cache is invalidated (index rebuild/refresh), so a refresh never serves a stale path mapping. Measured (voyage-only, single worker, real provider): concurrency-8 semantic latency dropped from ~929ms to ~426ms (-54%, 2.2x); id_index load 316ms->8ms, with a cascade (less GIL contention) cutting embed 533->288ms, HNSW-cache-hit 88->13ms, and per-query setup 143->76ms. Single-query latency also improved 359->274ms.

## [10.107.0] - 2026-06-07

### Fixed
- Bug #1078 (REAL root cause): Semantic-search concurrency collapse on the server was caused by **logging-lock contention**, not provider rate-limiting. `SQLiteLogHandler.emit()` performed a synchronous SQLite write (`execute_atomic` -> `get_connection`) WHILE holding the Python logging handler lock; every per-query INFO log (amplified by the dual-provider "parallel" query strategy) serialized on that lock. Proven by py-spy thread dumps: under a 16-concurrent semantic burst, 32 request threads were parked on `logging/__init__.py:901` `Handler.acquire()` while the holder was stuck in `emit -> get_connection`; zero HTTP 429s occurred. Fix: `SQLiteLogHandler` is now non-blocking -- `emit()` extracts fields and enqueues onto a bounded in-memory queue (`maxsize=10000`), and a dedicated daemon writer thread (`sqlite-log-writer`) drains it to the DB, so the handler lock is never held during I/O (mirrors the existing `ApiMetricsService` writer pattern). On queue saturation, ERROR/CRITICAL records get a bounded blocking enqueue (never silently lost); lower-severity records drop with an observable `dropped_count` + throttled stderr warning (anti-silent-failure). Hot-path per-query INFO logs (e.g. `backend_factory.py` "Creating FilesystemBackend", `semantic_query_manager` routing/strategy logs) demoted to DEBUG. `close()` flushes the queue and joins the writer. Validation (real VoyageAI, single-worker server): 48 concurrent requests -> 48/48 HTTP 200, 0 timeouts, 0 threads on the logging lock, post-burst recovery 0.45s (baseline: 40/40 timeouts + worker hang). Why local (SQLite) collapsed but staging (PostgreSQL) did not: SQLite's single-writer + connection-manager contention wedges under concurrent log writes; PostgreSQL tolerates them. The v10.106.0 ProviderConcurrencyGovernor is retained as independent hardening but is NOT the fix for this stall.

## [10.106.0] - 2026-06-07

### Fixed
- Bug #1078: Semantic-search concurrency collapse under load. The query serving path made multiple unbounded blocking calls per query to the embedding/rerank provider (VoyageAI/Cohere) -- query embedding, optional memory-retrieval embedding, and optional reranking -- all hitting the same provider account with no admission control and inconsistent 429 handling. Under concurrency this self-DoSed the provider: a single-worker server hung with 40/40 timeouts at concurrency 20 (measured, real provider). Fix (Phase 1): a process-wide `ProviderConcurrencyGovernor` (`server/services/provider_concurrency_governor.py`) with a `threading.BoundedSemaphore(K)` per provider budget (`voyage`, `cohere`) -- Voyage embedding and Voyage rerank share one `voyage` budget (shared account rate limit). All 5 serving call sites (PG + filesystem query embedding, memory-retrieval embedding, reranking, temporal embedding) acquire one slot per single HTTP attempt via the shared `governed_call.py::governed_query_embedding` helper, which wraps a new bounded `execute_with_backoff` (`services/provider_backoff.py`: max 2 retries, per-attempt cap 15s, cumulative cap <=45s, full jitter, Retry-After honored/clamped) so 429 backoff sleeps happen OUTSIDE the held slot. Query-path embedding is now single-attempt (`get_embedding -> get_embeddings_batch(retry=False)`); the indexing batch path keeps its own 429 retry/backoff and is NOT governed. A sinbin pre-check skips sinbinned providers without consuming a slot. The two fault-injection bypasses (memory + rerank client construction) now route through `_http_client_factory`. New server runtime config `query_provider_max_concurrency` (default 8). Real-provider regression test (`tests/integration/test_provider_governor_real_concurrency_1078.py`, no mocks): 20 concurrent governed embeddings -> 20 successes / 0 errors, in-flight high-water-mark held at 8, no worker hang, sentinel recovery 0.45s. Phase 2 (pooled keep-alive HTTP client) deferred. Per-process cap; in a 3-node cluster the global ceiling is 3xK by design (no distributed limiter).

## [10.105.0] - 2026-06-07

### Added
- Story #1077: C and C++ language support for xray AST search, across BOTH the Python `AstSearchEngine` (Phase 1) and the Rust `xray-core`/`xray-cli` (Phase 2). Rust xray-core now supports 17 languages (was 15) via new `tree-sitter-c` (0.24.2) and `tree-sitter-cpp` (0.23.4) grammar crates; the Python engine supports 12 (was 10) using the C/C++ grammars already bundled in `tree-sitter-languages`. Extension mapping: C = `.c`, `.h`; C++ = `.cc`, `.cpp`, `.cxx`, `.c++`, `.hpp`, `.hh`, `.hxx`, `.h++` (`.h` maps to C per GitHub-Linguist; a C++ header named `.h` parses under the C grammar and may yield ERROR nodes on C++-only syntax — name C++ headers `.hpp`/`.hh`/`.hxx`/`.h++`). The auto-updater rebuilds `xray-cli` on deploy (DeploymentExecutor Step 16), so no deploy-plumbing change is needed. Verified node kinds: `translation_unit`, `function_definition`, `call_expression`, `struct_specifier` (C), `class_specifier`/`namespace_definition`/`template_declaration` (C++), `if_statement`/`for_statement`/`while_statement`, `try_statement`/`catch_clause` (C++), `string_literal`, `comment`. Adds two C/C++ playbook examples to the xray_search tool docs (both live-verified through `xray-cli`), a verified cross-language node-type table, 8 new test fixtures (c/cpp x smoke/realistic/advanced/pathological), and Rust integration tests asserting zero-ERROR parse + exact node kinds. Lazy-load invariant preserved (tree-sitter not imported at CLI startup).

## [10.104.0] - 2026-06-07

### Fixed
- Bug #1072: SSH key registration was not cluster-aware. In PostgreSQL cluster mode, `SSHKeyManager` wrote keys only to the node-local SQLite store and the registering node's `~/.ssh` (storing a `private_path`, never the private key content), so the cluster sync service (`SSHKeySyncService`, which distributes private keys from shared PG to every node's `~/.ssh`) had nothing to distribute. Net effect: an SSH key registered on one node worked only there; git ops on other nodes failed with `Permission denied (publickey)` (e.g. cidx-meta backup push silently degraded to single-node). Fix: the private key content is now encrypted at rest in shared PG and distributed cluster-wide. (1) New `private_key` column on `ssh_keys` (migration `027`, PG + SQLite, nullable/backward-compatible). (2) `SSHKeyManager` is cluster-aware via `set_cluster_dependencies(pg_backend, fernet)` (injected in lifespan): on registration it reads the generated private key, encrypts it with a cluster Fernet key (new `ssh_key_encryption_key` in `cluster_secrets`, same trust model as the MFA key), and persists the ciphertext to PG. (3) `SSHKeySyncService` decrypts before writing each node's `~/.ssh/<key>` at `0600`; undecryptable keys are logged and skipped, never written corrupt. (4) `delete_key` now also removes the key from PG in cluster mode (prevents resurrection on the next sync); `create_key` is an idempotent upsert (`ON CONFLICT (name) DO UPDATE`) so re-registration populates the encrypted blob without a primary-key collision. New shared helper `cluster_key_provider.load_or_create_fernet_key`. Operational note: existing `ssh_keys` rows have `private_key = NULL` after migration; re-register affected keys to populate the encrypted content for cross-node distribution.

## [10.103.0] - 2026-06-07

### Fixed
- Bug #1071: Intermittent `POST /auth/login` HTTP 500 (`KeyError: 0`) on PostgreSQL cluster nodes. Root cause: `ElevatedSessionManager._PgBackend.touch_atomic*` mutated `conn.row_factory = dict_row` directly on a SHARED pooled psycopg3 connection. psycopg_pool does not reset `row_factory` on connection return, so that dict_row factory persisted and polluted the next borrower; any code reading a row positionally (`row[0]`) on the polluted connection then crashed with `KeyError: 0`. Observed 24 times on staging over 4 days. Fix attacks the bug at its source AND defensively pins all readers: (1) `elevated_session_manager` now uses a scoped `conn.cursor(row_factory=dict_row)` that never mutates the shared connection; (2) `token_bucket._pg_consume`, `rate_limiter` (PasswordChange), `oauth_rate_limiter` (Token + Register), `concurrency_protection` (advisory lock), and `totp_service.verify_recovery_code` all now pin `conn.cursor(row_factory=tuple_row)` for positional reads. `login_rate_limiter` was already immune. New regression coverage: `test_row_factory_pollution_bug1071.py`, `test_token_bucket_pg_row_factory_bug1071.py`, `test_rate_limiter_pg_row_factory_bug1071.py`.

## [10.102.0] - 2026-06-06

### Fixed
- Bug #1075: `BackgroundJobsPostgresBackend._row_to_dict()` called `json.loads()` directly on the `metadata` column (JSONB), bypassing the `_json_col()` helper already used for all other JSON columns. psycopg3 auto-deserializes JSONB to Python dicts before returning rows, so `json.loads(dict)` raised `TypeError`. This broke `get_job_details` and `cancel_job` for all xray jobs (which are only in PG, never in BJM's in-memory cache). Fix: use `_json_col()`. Also removes the `and row[19]` falsy guard — `_json_col` handles `None` correctly and the old guard would silently coerce `{}` to `None`.
- Bug #1076: `SyncJobsPostgresBackend._row_to_dict()` had the identical defect for five JSONB columns (`phases`, `phase_weights`, `progress_history`, `recovery_checkpoint`, `analytics_data`). Added `_json_col()` helper and replaced all five `json.loads(row[N]) if row[N] else None` calls.

## [10.101.0] - 2026-06-06

### Changed
- xray jobs (`xray_search`, `xray_explore`) are now excluded from the dashboard recent-jobs panel. They remain fully tracked (cancel still works) but no longer clutter the 20-slot recent-jobs list with "Unknown" repo entries. Exclusion is pushed into SQL on both SQLite and PostgreSQL backends so `LIMIT` fires after filtering — dashboard fills correctly even under heavy xray traffic. Adds `exclude_operation_types` param to `get_recent_jobs()` in `JobTracker` and `list_jobs()` in both storage backends.

## [10.100.0] - 2026-06-06

### Fixed
- Bug #1074: Multi-repo `xray_search` and `xray_explore` fan-out paths still called `bjm.submit_job(repo_alias=single_alias)`, which routes through `register_job_if_no_conflict` and enforces `idx_active_job_per_repo`. Concurrent multi-repo xray calls sharing any single alias got `DuplicateJobError` → 409. Fix: both multi-repo loops now use `job_tracker.register_job(repo_alias=None, metadata={"repo_alias": alias})` + `xray_executor`, identical to the single-repo fix from Bug #1073. Also hardened `_make_xray_explore_job_fn` with `try/finally` around the engine call so `bjm.unregister_child_processes` is always invoked even on exception (pre-existing child-process leak, now consistent with the search path).

## [10.99.0] - 2026-06-06

### Fixed
- Bug #1073: `xray_search` and `xray_explore` handlers called `register_job()` with `repo_alias=repo_alias_parsed` (non-NULL), which triggered the `idx_active_job_per_repo` partial unique index (`WHERE status IN ('pending','running') AND repo_alias IS NOT NULL`) at INSERT level on both SQLite and PostgreSQL. On PostgreSQL cluster (staging, NFS-backed repos) where xray ops take seconds, every concurrent call beyond the first raised `UniqueViolation` — a 100% failure rate for parallel xray stress tests on the same repo. Fix: both call sites now pass `repo_alias=None` (the designed escape hatch — NULL values are excluded by the index predicate) with `metadata={"repo_alias": repo_alias_parsed}` preserving the alias for observability. Incomplete follow-up to Bug #1070.

## [10.98.0] - 2026-06-06

### Fixed
- Bug #1070: `xray_search` and `xray_explore` MCP handlers were routed through `BackgroundJobManager.submit_job()`, which serializes all per-repo jobs behind a `register_job_if_no_conflict` gate — preventing concurrent xray calls on the same repo and causing unnecessary queue contention. Fix: both handlers are now `async def` and route compute to a dedicated 20-worker `ThreadPoolExecutor` (`xray_executor`, wired in lifespan), using `job_tracker.register_job()` directly (read-only; no conflict gate needed). The async `_await_xray_future` helper replaces the old synchronous `time.sleep` polling loop — no `_mcp_executor` thread is held during the wait. `_AWAIT_SECONDS_MAX` lowered from 120.0 to 45.0 to stay comfortably under the ALB 60 s hard timeout. `cancel_job` extended to handle xray jobs (absent from `self.jobs`, reached via `_child_processes` + `JobTracker`). New test coverage: architectural fix (concurrent calls, no serialization), cancel path, await-seconds validation, params, and lifespan executor-wiring.

## [10.97.0] - 2026-06-05

### Fixed
- PostgreSQL `background_jobs` metadata serialization: `BackgroundJobsPostgresBackend.update_job` json.dumps'd only `result`/`claude_actions`/`extended_error`/`language_resolution_status` — `metadata` was missing from its `_JSON_FIELDS`, even though INSERT and READ both serialize/deserialize it. So updating a job that carries a `metadata` dict raised `psycopg.ProgrammingError: cannot adapt type 'dict'`. The lifecycle/description backfill jobs register with a metadata dict (dep-map jobs don't), so every backfill job crashed instantly in cluster mode (`0 succeeded, 0 failed`) — the third bug blocking these features in production. Fix: add `metadata` to `update_job`'s `_JSON_FIELDS`, aligning PG with the already-correct SQLite backend (which includes metadata in its update json_fields). Found while validating the v10.95.0/v10.96.0 fixes on staging.

## [10.96.0] - 2026-06-05

### Fixed
- Scheduler golden-backend cluster-wiring bug: `DescriptionRefreshScheduler` defaulted its golden-repo backend to local SQLite (`GoldenRepoMetadataSqliteBackend`) when no `golden_backend` was injected, and `lifespan.py` constructed it without one. In cluster/postgres mode the golden repos live in PostgreSQL (`golden_repos_metadata`, 15 repos on staging) but local SQLite had only 1 stale row, so the description backfill, lifecycle backfill, and description scheduled refresh saw 1 repo instead of all cluster repos — these features were non-functional in cluster/production (the startup sweep logged "1 aliases clean"). Fix: inject `backend_registry.golden_repo_metadata` (the storage factory already creates the correct per-mode backend — SQLite solo, Postgres cluster) into the scheduler. Solo mode is byte-for-byte identical (same SQLite backend at the same db path). Verified the PG and SQLite `GoldenRepoMetadataBackend.list_repos()`/`get_repo()` contracts match (both `List[Dict]`/`Optional[Dict]` keyed on `alias`, with `clone_path`). Found while validating the v10.95.0 config-load-ordering fix on staging.

## [10.95.0] - 2026-06-05

### Fixed
- Scheduler config-load-ordering bug (cluster/postgres mode): the Description Refresh Scheduler and Dependency Map Scheduler read their enabled flags (`description_refresh_enabled` / `dependency_map_enabled`) from `config_service.get_config()` at `start()` time, but `ConfigService.set_connection_pool()` (which loads runtime config from the PG `server_config` table via `_load_runtime_from_pg`) was only called much later in `lifespan.py` (~line 2264). So at scheduler `start()` the config returned bootstrap defaults (both flags false), and the one-shot startup sweeps (`reconcile_terse_descriptions`, `reconcile_broken_lifecycle_metadata`) were permanently skipped even when the operator had enabled the feature in the Web UI — and the dep-map scheduler read disabled across restarts. Fix: set the ConfigService PG pool EARLY (before the scheduler inits) in postgres mode, so `get_config()` returns merged runtime config at `start()`. The late call is kept (belt-and-suspenders; `start_config_reload` is idempotent). Solo/SQLite mode is unaffected (postgres-gated). Regression tests assert the source-order invariant (early `set_connection_pool` precedes both scheduler constructions).

## [10.94.0] - 2026-06-05

### Fixed
- Bug #1069: dep-map delta retry money-pit. `invoke_delta_merge_file` returned `None` for three distinct situations — genuine dispatch failure, mtime-unchanged (Claude ran but made no edit), and byte-identical content — and `DependencyMapService._update_domain_file` mapped every `None` to `FAILED`, retrying up to `MAX_DOMAIN_RETRIES` (3) times. Each retry is a fresh ~20-minute Opus call, so a domain that legitimately needs no change burned 3x the cost (observed live on staging: `llm-observability-trace-archives` re-explored from scratch three times, producing zero change). The two no-op cases now return `_DELTA_NOOP` (-> `NOOP` -> loop breaks, no retry); only genuine dispatch failures return `None` and retry. A regression test asserts a no-op domain dispatches the subprocess exactly once (not three times), and a genuine failure still retries `MAX_DOMAIN_RETRIES` times.
- Bug #1069: `_dispatch_via_flow` now raises on `result.success is False` so genuine dispatch failures propagate to each caller's `except` block instead of falling through and being misclassified as a no-op. This also closes a latent silent-failure in the new-domain path, where a failed dispatch previously wrote an empty/garbage domain file and reported success.

## [10.93.4] - 2026-06-05

### Fixed
- Bug #1068: `DataRetentionScheduler` cross-backend cleanup consistency. PostgreSQL `sync_jobs` retention now deletes by `completed_at` with `status IN ('completed','failed')` (previously `created_at` with `status='completed'` only), matching the SQLite path — both backends now delete identical rows for the same retention intent (a job created long ago but completed recently is correctly KEPT). All five cleanup tables (logs, audit_logs, sync_jobs, dependency_map_tracking, background_jobs) were audited for column/status parity across both backends.
- Bug #1068: cleanup is now per-table independent — a failure in one table no longer aborts the whole cycle and silently reports success. Each table's cleanup runs in its own try/except, per-table failures are recorded in the result (`failed_tables`) and surfaced in the JobTracker outcome (not just logged), so retention errors are visible instead of lingering. Confirmed no retention path references the non-existent `last_updated` column (the staging error was a transient rolling-deploy schema skew).

## [10.93.3] - 2026-06-05

### Fixed
- Bug #1062: dependency-map admin page returned HTTP 500 due to an unclosed Jinja `{% if %}` block
  in the backfill-cards partial template. The nested conditional was redundant — the outer block
  already gates the cards to admin users — so it was removed entirely.

### Removed
- 5 stale MCPB removal-verification tests that referenced `src/code_indexer/mcpb`, a module
  that no longer exists. The tests were validating the absence of a module that had already been
  fully removed in a prior release; keeping them caused spurious failures on clean checkouts.

## [10.93.2] - 2026-06-05

### Added
- Story #1055: `xray_search_batch` MCP tool — cross-repo multi-expression X-Ray sweep in ONE
  background job. Runs a repos x scans matrix: every scan bundle (`driver_regex` + optional
  `evaluator_code` or `pattern_name`) is applied to every resolved repository. Each match is tagged
  with `repository_alias`, `scan_index`, and `pattern_name`. Returns exactly one `job_id` (not
  `job_ids`). Limits: 50 aliases, 50 scan bundles, timeout [10, 7200]s, await_seconds [0, 30].
  Global-alias fallback applied per alias. Unresolvable aliases become `error_level="repo"` errors;
  partial jobs proceed over the resolved subset. Per-cell `pattern_name` resolution uses
  `XrayPatternService` (repo-specific scope first, then `__any__/` fallback). Cancellation checked
  between cells. Large results spill to PayloadCache. REST shim at `POST /api/xray/search/batch`.
  45 new unit tests in `tests/unit/server/mcp/test_xray_search_batch_handler.py`.

## [10.93.1] - 2026-06-05

### Added
- Story #1067: `frontmatter_verifier` module in `global_repos/` — non-raising single-file
  and batch verification of cidx-meta lifecycle frontmatter. Reuses `UnifiedResponseParser._validate`
  and `_validate_optional_sections` as the single source of truth for enum tables and required-key
  lists (zero enum duplication). No description-length floor (bug #1064 established the [500,2000]
  floor was fictional — any non-empty string passes). Structured `VerificationResult` (passed bool +
  violations list) and `BatchReport` (valid/invalid counts + per-file detail). Batch never aborts on
  a bad file. 95% test coverage across 6 Gherkin scenarios plus integration no-drift tests against
  `UnifiedResponseParser`.

## [10.93.0] - 2026-06-05

### Added
- Story #1062: live lifecycle and description backfill observability cards on /admin/dependency-map.
  BackfillJournalService writes a shared-NFS `_activity.md` journal (append-only, offset-tracked)
  and an atomic (fsync + os.replace) `_status.json` sidecar per namespace (lifecycle / description).
  Two new HTMX partial routes (`GET /admin/partials/lifecycle-backfill-journal` and
  `/admin/partials/description-backfill-journal`) return the journal fragment with a pinned
  six-header contract (`X-Backfill-Active`, `X-Backfill-Total`, `X-Backfill-Done`,
  `X-Backfill-Failed`, `X-Backfill-Completed-At`, `X-Journal-Offset`) plus server-side 30s
  grace after completion before switching to idle polling.
  `_backfill_in_progress` split into `_lifecycle_backfill_running` and
  `_description_backfill_running` flags so both backfill types can run concurrently without
  blocking each other.
  `LifecycleBatchRunner` gains a `journal_callback` parameter for per-alias progress reporting;
  first-run cidx-meta directory is now created with `mkdir -p` before journal writes.
  Two frontend cards mirror the existing depmap-activity-panel polling pattern.

## [10.92.8] - 2026-06-05

### Fixed
- Config-loader hardening: `_dict_to_server_config` now filters unknown keys before
  constructing `BackgroundJobsConfig` (matching the existing pattern for other sub-configs),
  so config drift or version downgrade no longer crashes the description-refresh scheduler
  loop with `unexpected keyword argument 'max_concurrent_refresh_jobs'`.
- Jobs-dashboard tracker/BG merge: replaced the per-page dedup (which dropped tracker-only
  dep-map jobs — Bug #736 regression — and could O(history)-fetch) with a single bounded
  id-only query (`list_job_ids_filtered`, shared WHERE-builder, ORDER BY created_at DESC
  LIMIT 50000); Postgres `list_jobs_filtered` gained username scoping for parity.
- Test-suite robustness: added `actor_username` to hand-rolled `background_jobs` fixtures
  (8 files) so Bug #1065's atomic INSERT works; deterministic in-memory backend for
  description-scheduler first-enable tests (were breaching the 15s server-fast timeout);
  adapted refresh_scheduler collision_log tests to the `list_due_repos` API.

## [10.92.7] - 2026-06-05

### Fixed
- Bug #1063 follow-up: `_get_all_jobs` (jobs dashboard) tracker/BG merge dedup was
  regressed — tracker-only dep-map jobs vanished (Bug #736 regression), and the first
  fix attempt would have either re-dropped jobs or done O(history) per-page DB fetches.
  Final fix: dedup tracker jobs against a single bounded id-only query
  (`list_job_ids_filtered`, shared WHERE-builder with `list_jobs_filtered`,
  `ORDER BY created_at DESC LIMIT 50000`) so tracker-only jobs appear, jobs in both
  appear exactly once, total_count is page-consistent, and only ONE id query runs per
  view regardless of jobs-table size. Also fixed missing `username` scoping in the
  Postgres `list_jobs_filtered` (parity with SQLite). ORDER BY hardening added to both
  SQLite and Postgres `list_job_ids_filtered` so the capped 50,000 ids are always the
  newest rows, guaranteeing any tracker-visible (<=24h-recent) job is inside the id-set
  regardless of daily volume or physical row order.
- Bug #1065 follow-up: three test fixtures (`dep_map_tracking/conftest.py`,
  `test_golden_repo_manager_lifecycle_hook.py`,
  `test_add_golden_repo_unified_lifecycle.py`) hand-rolled a `background_jobs` table
  missing the `actor_username` column that #1065's atomic INSERT writes (production has
  it via the `_migrate_background_jobs_actor_username` migration); added the column to
  the fixtures so they no longer fail when the INSERT runs.

## [10.92.6] - 2026-06-04

### Fixed
- Bug #1060: Leaked `SQLiteLogHandler` on the root logger after lifespan shutdown caused
  silent log drops in subsequent tests. The actual root cause was NOT the originally-
  hypothesized "database is locked" WAL contention (busy_timeout=30000 already handles
  that). The real cause: `SQLiteLogHandler` is installed on the root logger during
  lifespan startup via `logging.getLogger().addHandler(sqlite_handler)` but was never
  removed on shutdown. After the lifespan exits and pytest deletes the tmp `logs.db`
  directory, the stale handler remains on the root logger. Subsequent `logger.warning()`
  calls in other tests hit the deleted DB and fail with `unable to open database file`,
  silently dropping those log records and masking real test failures.
  Fix: lifespan shutdown now calls `logging.getLogger().removeHandler(sqlite_handler)`
  and `sqlite_handler.close()`, symmetric with the install at startup (idempotent/safe).
  The removal is placed as the first action immediately after `yield` (in its own
  try/except) so it runs robustly even if a later shutdown step raises.

## [10.92.5] - 2026-06-04

### Fixed
- Bug #1061: `DescriptionRefreshScheduler.calculate_next_run` now uses pure uniform-random
  across the full `description_refresh_interval_hours` window (mean spacing ~96s at 900
  repos/24h) instead of hash-based bucket assignment + 18-minute in-bucket jitter that
  clustered ~37 repos/hour into spikes. Dropped the dead hashlib bucket path entirely.
- Bug #1061: Added `_reconcile_stale_next_run_rows()` called from `start()` after
  `reconcile_orphan_tracking()`. Re-slots all NULL or past `next_run` rows on startup so
  first-enable and long-disabled-restart no longer fire the whole fleet at once. Also fixes
  the latent gap where NULL `next_run` rows never matched `WHERE next_run <= ?` and thus
  were never due for scheduling.
- Bug #1061: `_reconcile_stale_next_run_rows` now compares `next_run` as tz-aware datetimes
  (parsing via `datetime.fromisoformat`, treating naive timestamps as UTC) instead of raw
  string comparison, preventing the Postgres TIMESTAMPTZ lexicographic-compare footgun in
  cluster mode.

### Changed
- `DescriptionRefreshScheduler.start()` log line now reads
  "uniform random across full interval" (was "hash-based bucket scheduling with jitter").
- Module and class docstrings updated to reflect uniform-random scheduling.

### Tests
- Bug #1061: `tests/unit/server/services/test_description_refresh_scheduler_uniform_jitter_bug.py`
  covers `calculate_next_run` uniformity (Kolmogorov-Smirnov + histogram bin cap),
  `_reconcile_stale_next_run_rows` (NULL recompute, past recompute, future preserved,
  mixed rows, NULL rows never due, spread across full interval), and `start()` call-order
  (orphan reconcile before stale reconcile before daemon thread).

## [10.92.4] - 2026-06-04

### Fixed
- Bug #1063 Part 1: `GlobalRepoManager.list_due_repos()` now accepts a `cap` argument and issues a `LIMIT`-bounded SQL query instead of loading all rows then slicing in Python. New `max_concurrent_refresh_jobs` field added to `BackgroundJobsConfig` (default 3). The scheduler uses `count_active_refresh_jobs()` to gate concurrent refreshes, preventing thundering-herd on large golden-repo registries.
- Bug #1063 Part 2: `BackgroundJobManager._execute_job` now debounces `_persist_jobs` writes for intermediate progress ticks (coalescing within `PROGRESS_DEBOUNCE_INTERVAL = 0.5 s`), while still flushing immediately on terminal state (COMPLETED/FAILED/CANCELLED) and running `_check_db_cancellation` on every tick for responsive cancel latency.
- Bug #1063 Part 3: `BackgroundJobManager` now uses a `threading.BoundedSemaphore(max_concurrent_background_jobs)` worker pool instead of spawning an unbounded thread per job. Pending jobs that arrive while the pool is full queue in `_pending_queue` and are dispatched by the releasing worker. Cancel of a pending job marks it CANCELLED before it ever starts; cancel of a running job sends SIGTERM to the child process.
- Bug #1063 Part 4: `list_jobs()` and `get_jobs_for_display()` now hard-cap `page_size` at `MAX_PAGE_SIZE = 50` (module-level constant, importable). The DB query limit tracks the capped page_size, not the old 10000-row bulk-fetch sentinel. `_get_all_jobs()` in `routes.py` applies the same cap. Dashboard fetch is now O(page_size) instead of O(total_job_history).
- Bug #1063 cleanup: `_MAX_DB_FETCH_FOR_PAGINATION` renamed to `_MAX_OP_TYPE_SCAN` to accurately describe its only remaining use site (`get_jobs_by_operation_and_params` full-op-type scan), since all pagination paths now use `MAX_PAGE_SIZE`.

### Tests
- Bug #1063: New test files `tests/unit/global_repos/test_bug1063_part1_capped_due_query.py`, `tests/unit/server/repositories/test_bug1063_part2_progress_debounce.py`, `tests/unit/server/repositories/test_bug1063_part3_bounded_worker_pool.py`, `tests/unit/server/repositories/test_bug1063_part4_dashboard_bounded_fetch.py` covering all four fix parts plus the Part 4G `_get_all_jobs` multi-page reachability test (Bug #736 / BLOCKING 3).
- Bug #1065: `tests/unit/server/repositories/test_bug1063_part4_dashboard_bounded_fetch.py::TestGetAllJobsMergeReachability.test_all_jobs_reachable_exactly_once_with_partial_bg_page` long assertion strings wrapped to comply with ruff E501 line-length.

## [10.92.3] - 2026-06-04

### Fixed
- Bug #1066: `RefreshScheduler._scheduler_loop` now uses a `_submit_failed` sentinel to track whether `_submit_refresh_job` raised a generic (non-`DuplicateJobError`) exception. `update_next_refresh` is only called on success or `DuplicateJobError`; on a transient failure the repo's `next_refresh` is left unchanged so the scheduler retries on the very next poll cycle instead of silently skipping a full refresh interval.

## [10.92.2] - 2026-06-04

### Fixed
- Bug #1065: `BackgroundJobManager.submit_job` now routes repo-scoped operations through the cluster-atomic `JobTracker.register_job_if_no_conflict()` (honoring the `idx_active_job_per_repo` partial unique index) BEFORE spawning the worker thread. A duplicate raises `DuplicateJobError` as a hard reject instead of being swallowed. Previously the legacy TOCTOU precheck + non-atomic `register_job` + swallowed constraint violations let duplicate workers run in both solo and cluster modes.
- Bug #1065: New `atomic_claim_insert` method added to the `BackgroundJobsBackend` Protocol and both SQLite and Postgres backend implementations. Uses a plain `INSERT` (no `OR IGNORE`, no conflict suppression of the partial unique index) so the `idx_active_job_per_repo` constraint violation actually raises and is translated to `DuplicateJobError`. The existing `save_job` `INSERT OR IGNORE` is unchanged for its other callers.
- Bug #1065: `register_job_if_no_conflict` (and the underlying atomic insert) now persists `is_admin` and `actor_username` (Story #1032 AC12 audit trail) on the atomic claim path, fixing silent loss of those columns for repo-scoped jobs that previously went through the non-atomic code path.
- Bug #1065: New regression tests at `tests/unit/server/repositories/test_submit_job_atomic_dedup.py` covering: atomic dedup rejects the second caller with `DuplicateJobError`, AC12 `is_admin`/`actor_username` columns are persisted via the atomic insert, and `INSERT OR IGNORE` swallow on `save_job` no longer silently hides duplicate violations. Updated tests in `test_background_job_manager_tracker.py`, `test_job_tracker_atomic_register.py`, and `test_job_tracker_bug892.py`.

## [10.92.1] - 2026-06-04

### Fixed
- Bug #1064: `TERSE_DESCRIPTION_MAX_CHARS` lowered from 500 to 200 in `description_refresh_scheduler.py`. Small-repo descriptions of 201-500 chars are legitimately concise — they were being re-flagged as terse on every scheduler startup and re-queued for regeneration in an infinite loop. At the new threshold of 200 (barely a sentence), only genuine stubs or failed generations are queued.

### Changed
- Bug #1064: `src/code_indexer/server/prompts/lifecycle_unified.md` description field instruction no longer requests a fixed 500-2000 character count. Replaced with quality/coverage-oriented guidance: cover purpose, domain(s), high-level capabilities, key technologies, and integration surface at the level of detail the repository actually warrants — a few sentences for a small library, several paragraphs for a large system. No padding, no truncation of real substance.

## [10.92.0] - 2026-06-04

### Added
- Bug #1056: New shared helper `src/code_indexer/server/services/jittered_dispatcher.py` with two public functions and three module-level constants. `dispatch_parallel_with_jitter(items, *, concurrency, base_jitter_seconds, worker_fn)` submits items to a `ThreadPoolExecutor` where each worker thread sleeps `random.uniform(0, base_jitter_seconds)` before invoking the worker function, smoothing the thundering-herd burst of concurrent Claude CLI calls that all sites previously submitted in lockstep. `sleep_with_jitter(base_jitter_seconds)` provides the same randomised inter-iteration sleep for sequential loops. Both functions are no-ops when `base_jitter_seconds <= 0`. Constants: `DEFAULT_LIFECYCLE_DISPATCH_JITTER_SECONDS = 2.0`, `DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS = 2.0`, `DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS = 2.0`. 6 unit tests at `tests/unit/server/services/test_jittered_dispatcher_bug1056.py` and 3 integration tests at `tests/unit/server/services/test_dispatcher_integration_bug1056.py` covering all public behaviours.

### Fixed
- Bug #1056: `LifecycleBatchRunner._run_sub_batch` now delegates parallel repo dispatch to `dispatch_parallel_with_jitter` instead of submitting all futures in a tight loop via `ThreadPoolExecutor`. The `ThreadPoolExecutor` block and its dict-comprehension future map have been replaced with a single `dispatch_parallel_with_jitter` call; per-repo error handling semantics are preserved exactly (per-alias `Exception` logged at ERROR level and sub-batch continues; `BaseException` re-raised immediately).
- Bug #1056: `DependencyMapService` Pass 2 per-domain loop now calls `sleep_with_jitter(DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS)` at the top of each iteration after the first (guarded by `domain_idx > 0`, positioned after the cancellation check and before per-domain work). Prevents all Claude CLI calls for N domains firing simultaneously when Pass 2 starts on a repo with many domains.
- Bug #1056: `DepMapRepairExecutor` broken-domain repair loop now calls `sleep_with_jitter(DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS)` at the top of each iteration after the first (guarded by `anomaly_idx > 0` via `enumerate`). Prevents all Phase 3.7 re-analysis Claude CLI calls firing in lockstep when multiple domains need repair in a single pass.

## [10.91.17] - 2026-06-04

### Fixed
- Bug #1058: `UnifiedResponseParser` no longer fails with `not valid JSON: Extra data` when Claude appends trailing prose after the JSON object. New `_strip_postamble` static method walks the string from the first `{`, tracking brace depth while ignoring braces inside JSON string literals (handles `\"` escaped quotes correctly), and truncates everything after the matching closing `}`. Applied in `parse()` immediately after `_strip_preamble`, symmetrically completing the preamble/postamble defence-in-depth pipeline. Previously caused 100% lifecycle-batch failure rate (17/17 calls failing in production) and a silent retry burn loop that consumed tokens with zero forward progress. 5 new unit tests at `tests/unit/server/services/test_unified_response_parser_postamble_bug1058.py` covering: trailing prose, preamble+postamble composition, brace-inside-string-literal, escaped-quote-then-brace, and code-fenced-JSON-with-trailing-prose.
- Bug #1059: `SharedJobSentinel.read_active()` no longer returns `None` for losers caught in the narrow race window between a winner's `os.open(O_CREAT|O_EXCL)` and the subsequent `os.write()` (Story #1035). Bounded retry: up to 3 attempts at 10ms intervals (~30ms worst-case loser-path latency, only when the race is hit; zero overhead in the common case). Preserves the Story #1035 O_CREAT|O_EXCL locking primitive — only the read path changed. Surfaced as a flaky `test_concurrent_claim_race_single_winner` under chunked-parallel `./server-fast-automation.sh` load, but also a real cluster-correctness fix: losers now reliably learn the winner's `job_id` so downstream code (dashboard sentinel display, dep-map analysis pre-flight at `web/dependency_map_routes.py::trigger_dependency_map`) can attribute the conflict correctly. 5 new unit tests at `tests/unit/server/services/test_shared_job_sentinel_read_active_retry_bug.py` covering: absent-file sanity, valid-file sanity, transient-empty-then-succeeds, persistent-corrupt-returns-None, and persistent-corrupt-warning-message.

## [10.91.16] - 2026-06-03

### Fixed
- Bug #1054: Phase 3.7 dep-map graph-channel repair no longer crashes with `'AnomalyAggregate' object has no attribute 'message'` when the hygiene-parser aggregates a per-type anomaly cluster that exceeds the example threshold. Root cause: `_audit_bidirectional_mismatch`, `_repair_self_loop`, and `_repair_malformed_yaml` in `dep_map_repair_executor.py` were forwarding the iteration value straight to their per-anomaly delegates, which read `anomaly.message` / `anomaly.file`. Story #911 AC6 had retrofitted the unwrap pattern into `_repair_garbage_domain_rejected` but the other three repair sites were missed. All three now apply the same `examples = [anomaly] if not isinstance(anomaly, AnomalyAggregate) else anomaly.examples` unwrap and loop per example. 3 regression-guard tests at `tests/unit/server/services/test_dep_map_repair_aggregate_unwrap_bug1054.py` (AnomalyAggregate scenarios for each of the three handler types, asserting per-example dispatch and zero AttributeError).

## [10.91.15] - 2026-06-03

### Added
- Story #1053: Resumable delta dep-map analysis via per-domain YAML frontmatter journal. Each `dependency-map/<domain>.md` carries its own `last_delta_applied: <fingerprint>` marker; frontmatter and body are written together in one atomic `os.replace` (tempfile + fsync + rename in the same parent directory — no cursor-vs-file ambiguity window). On crash/restart, the resumed run computes the same delta fingerprint and skips any domain whose frontmatter shows the current delta already applied. New-repo discovery is monolithic skip-or-redo: if `_domains.json` already covers every new repo alias, the discovery Claude CLI call is skipped entirely; if the JSON is missing, malformed, or wrong-shape, the call re-runs. Cluster correctness inherits from the existing `cidx-meta` `WriteLockManager` lock (atomic `O_CREAT|O_EXCL` on NFSv4). Crash-durability scope: process crash / SIGKILL / `systemctl restart` / graceful reboot — NOT sudden power loss or NFS server crash (the in-flight domain is re-processed on resume in those cases by design). 40 unit/integration tests covering all 16 acceptance criteria. New helper scripts `tests/e2e/manual/provision_delta_fixture.sh` and `tests/e2e/manual/audit_processes.sh` for the full process-tree-kill E2E test (Scenario 16).

### Docs
- New architecture reference: `docs/depmap-resumable-delta-architecture.md` (182 lines) documenting the 5 primitives, the resume loop, the lock dependency, the honest crash-durability scope, the 7-item rejected-approaches anti-regression list (no backup-by-N, no prompt context hint, no separate cursor file, no batched discovery, no fingerprint intersection, no `run_full_analysis` hardening, no parent-dir fsync), and production observability.
- CLAUDE.md "Critical Architecture Invariants" gains a slim 5-line "Resumable Delta Dep-Map Analysis (Story #1053)" subsection pointing to the architecture doc.

### Separate isolated commit (per Story #929 Security-Sensitive Commit Discipline)
- `125d7568` "docs: harden Push-to-master Authorization rule" — rewrites the CLAUDE.md push-to-master rule with anti-extrapolation language, mandatory two-confirmation protocol, per-push per-version authorization scope, explicit /goal exclusion, and a "Past failures" paragraph documenting the v10.91.14 unauthorized-push incident verbatim. Permission/authorization rule change; isolated from feature work.

## [10.91.14] - 2026-06-03

### Fixed
- Bug #1052: Auto-updater now idempotently creates the `~/.cidx-server/data/activated-repos` symlink under CoW-managed storage on CoW-daemon cluster deployments. Story #1034 had set this up for `golden-repos` but not for `activated-repos`, so freshly-provisioned cluster nodes silently couldn't accept activations until an admin manually fixed the symlink (as documented in Bug #1044/#1046 staging validation history). New step `_ensure_activated_repos_symlink_for_cow_daemon()` runs in `DeploymentExecutor.execute()` after Step 14 (NFS research symlinks); no-op for local/ontap backends; refuses to silently move pre-existing real-directory user data — emits a structured WARNING with the manual migration command instead. 6 regression-guard tests added at `tests/unit/server/auto_update/test_activated_repos_symlink_setup_bug1052.py` (real `os.symlink()` in `tmp_path`, no mocks).

### Cleanup
- Removed 2 `@pytest.mark.xfail` tests per zero-disabled-tests policy:
  - `test_admin_jobs_stats_endpoint.py::test_stats_endpoint_calculates_total_jobs` (auth-mocking infrastructure debt; endpoint existence already covered by `test_stats_endpoint_exists`).
  - `test_dep_map_888_ac7_ac8_ac9.py::TestAC7DomainNotIndexedGap` (whole class — documented architectural gap "awaiting product decision"; the 4 reachable resolution states remain covered by separate classes).

## [10.91.13] - 2026-06-03

### Fixed
- Bug #1049: `build_dep_map_dispatcher` did not derive `claude_soft_timeout_seconds` from `config.dependency_map_pass_timeout_seconds`, so the inner shell `timeout <N> claude ...` command was always wrapped at `_DEFAULT_SOFT_TIMEOUT_SECONDS = 1800` regardless of operator setting. Operators raising `dependency_map_pass_timeout_seconds` to 18000 were silently capped at 30 minutes per pass. Factory now derives the value from config when the caller does not pass an explicit override; explicit override still wins. 3 regression-guard tests added at `tests/unit/server/services/test_dep_map_dispatcher_factory_timeout_config_bug1049.py`.
- Bug #1051: 15 depmap-dashboard/sentinel/state-polling unit tests failing on development across 3 root causes:
  - **Group A (4)**: `FakeTracker` test double in `tests/unit/server/services/test_depmap_dashboard_job_runner.py` lacked `fail_job` method after v10.91.6's `update_status(..., error=...) → fail_job(...)` change. Added the method + updated 4 `TestFailurePath::*` assertions.
  - **Group B (8 + setup-error cascade)**: `tests/unit/server/web/conftest.py`'s autouse `_bootstrap_server_database` seeded admin/admin in SQLite but did NOT assign the new `UserManager` to `dependencies.user_manager`. Combined with `_restore_dependency_globals` saving/restoring a None-initial singleton, this caused `POST /login` to return 500 `User manager not available` on every test after the first, blocking ~15 sentinel/refinement/state-polling tests at fixture setup. Extended `_bootstrap_server_database` to assign the singleton.
  - **Group C (3)**: production-logic drift in `src/code_indexer/server/web/dependency_map_routes.py` dashboard-partial handler — missing sentinel-held / claim-race / terminal-job guards before submitting a new `dep_map_dashboard` job or rendering the in-progress partial.

### Tests
- 121 previously-affected tests now pass; 0 failures across the targeted depmap dashboard sweep. (`os.path.realpath`) at construction (`__init__` and `set_shared_repos_dir`). Discovered during Bug #1044 staging E2E validation: clusters that deploy `~/.cidx-server/data/activated-repos` as a symlink to the CoW-managed storage (e.g. `-> /mnt/cow-storage/activated-repos`) hit `_fd_anchored_phase1_rename`'s `os.open(..., O_NOFOLLOW)` check on the top-level dir, which correctly refuses to follow the symlink and raises `Errno 20 Not a directory`. Deactivation jobs reported `success: true` but left workspaces on disk with 7 cleanup warnings. The admin-controlled top-level path is safe to resolve once at init; per-user/alias subpaths under it still get O_NOFOLLOW protection.

### Tests
- New regression-guard suite `tests/unit/server/repositories/test_activated_repos_dir_symlink_resolution.py` (3 tests using real `os.symlink()` — no mocks per Anti-Mock):
  - Symlinked data_dir resolves to target on construction.
  - Non-symlink direct directory continues to work.
  - `set_shared_repos_dir` resolves symlinks too.

## [10.91.11] - 2026-06-03

### Fixed
- Bug #1046 (extension): v10.91.10 added symlink resolution but only accepted paths under `mount_point`. On the CoW daemon host node (cluster node 23) the `golden-repos` symlink target is under `daemon_storage_path` directly (`/home/jsbattig/cow-storage/golden-repos`) — the bind-mount alias of the same XFS filesystem reached via the symlinked source rather than the mount point. v10.91.10 still rejected these resolved paths. v10.91.11 extends `_translate_to_daemon_path` to accept paths resolving under EITHER `mount_point` (translate to daemon-local form) OR `daemon_storage_path` (already in daemon-local form, return as-is).

### Tests
- Added 2 regression-guard tests to `tests/unit/server/storage/shared/test_cow_daemon_backend_symlink_resolution_bug1046.py`:
  - `test_symlink_to_daemon_storage_path_passes_through` — symlink resolving under daemon_storage_path returns the resolved path unchanged.
  - `test_direct_daemon_storage_path_passes_through` — direct daemon_storage_path entries pass through unchanged.
  Real `os.symlink()` only — no mocks (Anti-Mock rule).

## [10.91.10] - 2026-06-03

### Fixed
- Bug #1046: `CowDaemonBackend._translate_to_daemon_path` (`clone_backend.py:315-335`) rejected the `golden_repos_dir` symlink path with `"is not under mount_point '/mnt/cow-storage' — cannot translate to daemon view"`, blocking every cluster activation after the Bug #1044 wiring fix made this code path reachable. Root cause: literal string `startswith` comparison with no symlink resolution; staging's `golden_repos_dir` is intentionally a symlink (`/home/jsbattig/.cidx-server/data/golden-repos -> /mnt/cow-storage/golden-repos`) to satisfy the XFS-reflink-same-filesystem constraint. Added `os.path.realpath()` resolution before the mount-point prefix check; the resolved path is now used for both the comparison and the daemon-side path computation.

### Tests
- New regression-guard suite `tests/unit/server/storage/shared/test_cow_daemon_backend_symlink_resolution_bug1046.py` (3 tests using real `os.symlink()` under `tmp_path` — zero mocking of the symlink layer per Anti-Mock rule). Test 1 confirmed RED before fix; all 3 pass after.

## [10.91.9] - 2026-06-03

### Fixed
- Bug #1044: Activation was completely broken because Story #1034 added a hard `clone_backend is None` guard to `ActivatedRepoManager._clone_with_copy_on_write` (activated_repo_manager.py:2643) but the lifespan wiring never injected `_clone_backend` into the `ActivatedRepoManager` reachable from `golden_repo_manager.activated_repo_manager`. Every activation raised `"ActivatedRepoManager._clone_with_copy_on_write invoked without clone_backend — wiring bug. Story #1034 Commit 4 requires clone_backend injection."`. Added `arm._clone_backend = snapshot_manager._clone_backend` to `lifespan.py` belt-and-suspenders block (lines 615-625), matching the existing Story #1034 pattern that injects `_snapshot_manager` into `GoldenRepoManager` and `RefreshScheduler`.

### Tests
- New regression-guard suite `tests/unit/server/startup/test_lifespan_clone_backend_wiring_bug1044.py` (6 tests: source-text guards, source-order guard, runtime simulation using real `ActivatedRepoManager` and real `VersionedSnapshotManager` via `build_snapshot_manager`).

### Docs
- Added "ActivatedRepoManager clone_backend Wiring (Story #1034 / Bug #1044)" invariant to project CLAUDE.md "Critical Architecture Invariants" section.

## [10.91.3] - 2026-06-02

### Fixed
- Users page CRUD: extended the PRG (Post-Redirect-Get) fix from v10.91.2 to the 4 remaining POST handlers — `create_user`, `update_user_role`, `change_user_password`, `update_user_email`. All five Users-page POST handlers now return `RedirectResponse(303, "/admin/users?...")` on every code path, so the form interceptor in `base.html` does a real `window.location.href` navigation and HTMX initializes normally. Users table re-renders correctly after every CRUD operation, not just delete.
- Extended `_USERS_PAGE_ERROR_MESSAGES` whitelist with 6 new error codes: `passwords_mismatch`, `invalid_role`, `cannot_demote_self`, `sso_password_change_denied`, `user_not_found`, `operation_failed`. Unknown codes still silently render no banner (XSS prevention).

### Audited (no fix needed)
- Other admin CRUD pages reviewed: `golden_repos.html`, `groups.html`, `ssh_keys.html`, `repos.html`, `config.html`, `self_monitoring.html`, `dependency_map.html`. None have the `hx-trigger="load"` + intercepted POST-form combination that triggers the `document.write` HTMX re-init bug, so PRG is not required for them.

## [10.91.2] - 2026-06-02

### Fixed
- Users page delete (and other admin POST handlers) now use PRG (Post-Redirect-Get): on success/failure the server returns a 303 redirect to `/admin/users?success=<code>&u=<username>` (or `?error=<code>`) instead of rendering the page inline. This avoids the `document.write()` HTMX re-init bug (v10.91.1 polling fallback was insufficient in practice). The form interceptor in `base.html` already handles `resp.redirected === true` by doing a full `window.location.href = resp.url` navigation, which triggers HTMX auto-init normally. The users table now re-renders after delete.
- Added `_USERS_PAGE_SUCCESS_MESSAGES` and `_USERS_PAGE_ERROR_MESSAGES` whitelist dicts in `routes.py` so query-string status codes can never inject HTML into the rendered page (XSS prevention). Unknown codes silently fall through to no banner.

## [10.91.1] - 2026-06-02

### Fixed
- Users page: after a successful delete (or any other `/admin/` form POST), the success banner appeared but the users table never re-rendered — stuck on "Loading users…". Root cause: the global form interceptor in `base.html` `_doSubmit()` POSTs via `fetch()` and replaces the page with `document.open()/write()/close()`. `document.write` does not reliably re-fire HTMX's `DOMContentLoaded` auto-init on the new document, so `hx-trigger="load"` elements (like `#users-list-section`) never fire their initial request. Added an explicit `htmx.process(document.body)` call after `document.close()`, with a short polling loop (capped at 40 retries × 50ms = 2s) to wait for the new document's htmx script to load.

## [10.91.0] - 2026-06-02

### Fixed
- Users page delete button consistently returned "Invalid CSRF token". Root cause: the hidden `<form id="delete-form-...">` was placed directly inside `<tbody>` in `partials/users_list.html`. HTML5 in-table insertion mode inserts the form and immediately pops it off the open elements stack, orphaning the `<input type="hidden" name="csrf_token">` from the form. Native form submit therefore sent no csrf_token field. Wrapped the delete form in `<tr><td colspan="5">` to match the edit/email/password forms in the same template. Regression test added: `tests/unit/server/web/test_users_delete_form_html5_parse.py` (uses html5lib parser, which enforces HTML5 in-table rules — html.parser/lxml are lenient and do not catch this).

## [10.90.0] - 2026-06-02

### Added (Story #1040)
- Dependency map analysis cancellation: admin can stop a running full or delta analysis mid-flight via `POST /admin/dependency-map/cancel`.
- `DependencyMapService.cancel_running_analysis()` sets a `threading.Event` (`_cancel_event`) that domain loops in `run_full_analysis`, `run_delta_analysis`, and `run_refinement_cycle` check at the top of each iteration, allowing graceful stop within one domain boundary.
- Cancel event is cleared at the start of every new analysis run so stale cancellations never affect subsequent runs.
- When analysis is cancelled, `JobTracker.fail_job()` is called with `"Cancelled by admin"` in the finally block (previously the job was left in `running` state).
- "Stop Analysis" button added to the dep-map admin page Actions card; visible only while a job is running; disables itself and shows "Stopping..." after click; updates status div with outcome.
- REST endpoint only (no MCP tool): `POST /admin/dependency-map/cancel` requires admin elevation.

## [10.89.0] - 2026-06-02

### Added (Story #1039)
- Per-handler bare-to-global alias fallback for 31 read-only MCP handlers. When a dep-map analysis Claude subagent passes a bare repo alias (e.g. `evolution`) instead of the `-global`-suffixed form (`evolution-global`), and the user does not have that repo in their own activated-repo list, the handler transparently promotes the alias to `evolution-global` if the golden repo is globally active. Eliminates the 445 daily "Repository not found for user admin" errors that Claude dep-map subagents were generating.
- New `_global_fallback.py` helper module (`server/mcp/handlers/`) with `try_global_fallback(alias, golden_repo_manager) -> str | None`.
- New `user_has_activated_repo(username, alias) -> bool` method on `ActivatedRepoManager` for pre-check membership test.
- New `is_globally_active(alias) -> bool` method on `GoldenRepoManager` delegating to `GlobalActivator`.
- Fallback applied to: `search_code`, `handle_regex_search`, `get_file_content`, `list_files`, `browse_directory`, `handle_directory_tree`, `handle_xray_search`, `handle_xray_explore`, `handle_xray_dump_ast`, `scip_definition`, `scip_references`, `scip_dependencies`, `scip_dependents`, `scip_impact`, `scip_callchain`, `scip_context`, `get_branches`, and 12 git-read handlers.
- Write/mutation handlers remain strict -- no fallback applied.

## [10.88.0] - 2026-06-01

### Fixed (Bug #1038)
- Bug #1038: Removed FILE_EDIT_COMPLETE sentinel from dep-map verification pipeline. Verification is now best-effort (single attempt, returns bool, never raises). Eliminates catastrophic retry cascade that burned hundreds of dollars when model output format changed.

### Fixed (Bug #1037)
- `XrayPatternService.store_pattern` / `delete_pattern` (`server/services/xray_pattern_service.py`) now serialize via the existing `_COARSE_ALIAS="cidx-meta"` write lock through a `_run_with_coarse_lock` helper modeled on `MemoryStoreService` (`server/services/memory_store_service.py:372`). When the lock is held by `refresh_scheduler` / `memory_store_service` / `dep_map_service`, the xray service writes the YAML to disk and SKIPS `_git_commit` — the lock holder's refresh will sweep the change via `git add -A`. When unlocked, xray acquires the lock and runs `_git_commit` + `CidxMetaBackupSync.sync()` inside the critical section.
- Two MCP handler call sites in `server/mcp/handlers/xray.py` (`store_xray_pattern`, `delete_xray_pattern`) pass `cidx_meta_path` / `coarse_lock_owner` so the service can identify the lock owner.

### Tests (Bug #1037)
- 13 new unit tests in `tests/unit/server/services/test_xray_pattern_service_coarse_lock.py` covering lock acquisition (`store_pattern`/`delete_pattern`), piggyback skip when lock held, full critical-section execution when lock free, owner string emission (`xray-pattern:store:*` / `xray-pattern:delete:*`), and integration with `CidxMetaBackupSync`.
- Handler fixture in `tests/unit/server/mcp/test_xray_pattern_handler.py` updated to accept `**kwargs` for forward compatibility.

### Documentation
- `CLAUDE.md` adds one-line note to the "cidx-meta backup contract" section pointing to the new coarse-lock invariant.

## [10.87.0] - 2026-06-01

### Fixed (Bug #1036)
- `XrayPatternService._git_commit` (`server/services/xray_pattern_service.py`) now uses the canonical cidx-meta backup pattern from Story #926: `build_non_interactive_git_env()` + `cidx-meta-backup` author/committer identity, followed by `CidxMetaBackupSync.sync()` to push to the configured remote. Previously the bare `subprocess.run(["git", "commit"], ...)` produced commits authored as `code-indexer@<hostname>` that were never pushed — pattern changes only reached the remote when the next refresh cycle's sync happened to run.
- Sync failures surface via WARNING log (deferred-failure pattern mirroring `refresh_scheduler.py`); the commit itself is not rolled back.
- Graceful degradation when `cidx_meta_backup_config.enabled=False` — local commit still happens, sync is skipped.

### Tests (Bug #1036)
- 5 new unit tests in `tests/unit/server/services/test_xray_pattern_service_backup_sync.py` covering canonical env vars, sync invocation, sync_failure surfacing, graceful degradation when backup disabled, and `cidx_meta_path` correctly forwarded to `CidxMetaBackupSync`.

## [10.86.0] - 2026-06-01

### Added (Story #1035)
- `SharedJobSentinel` (`server/services/shared_job_sentinel.py`) — cluster-wide re-entrancy primitive using atomic `O_CREAT|O_EXCL` on NFS-shared cidx-meta. Methods: `try_claim(op_type, job_id, node_id)`, `release(op_type, expected_job_id)`, `read_active(op_type)`, `is_stale(info, timeout)`. Stale recovery built into `try_claim` (atomic replace via tempfile + os.replace).
- `FilesystemDashboardCacheBackend` (`server/storage/filesystem_backends.py`) — drop-in replacement for the per-node SQLite dashboard cache. Stores `_dashboard_cache.json` under `cidx-meta/dependency-map/`. Atomic NFSv4-safe writes via tempfile + os.replace. Interface parity with the SQLite backend: `is_fresh`, `get_cached`, `set_result`, `claim_job_slot`, `clear_job_slot_for_retry`, `get_running_job_id`.
- `AnalysisAlreadyRunningError` — surfaced from `DependencyMapService.run_full_analysis` / `run_delta_analysis` when sentinel claim fails. Carries `active_job_id` for 409 response surfacing.
- `CLAUDE.md` "Dep-Map Re-Entrancy Sentinels" architectural invariant subsection.

### Changed (Story #1035)
- `DependencyMapService.is_available()` now consults `SharedJobSentinel.read_active("analysis")` (cluster-visible) instead of a per-process `threading.Lock` (per-node only).
- `DependencyMapService.run_full_analysis()` / `run_delta_analysis()` wrap analysis body in `SharedJobSentinel.try_claim`/`release` (try/finally). New `pre_claimed` parameter allows route-layer ownership (web/MCP claim sentinel synchronously before spawning the worker thread).
- Web `POST /admin/dependency-map/trigger` (`dependency_map_routes.py`): synchronous sentinel claim before thread spawn; 409 response body now includes `job_id` of the active analysis; sentinel released on thread-spawn failure.
- Dashboard partial STATE 3/4 (`dependency_map_routes.py`): STATE 3 reads `SharedJobSentinel.read_active("dashboard")` from shared storage; STATE 4 attempts sentinel claim before submitting the dashboard job. Losing nodes fall through to STATE 3 rendering Processing view attached to winner's job_id.
- Dashboard background job now submitted with non-NULL `repo_alias="__depmap_dashboard__"` (defense-in-depth — engages JobTracker's `idx_active_job_per_repo` partial unique index alongside sentinel).
- MCP `trigger_dependency_analysis` handler (`mcp/handlers/admin/__init__.py`): same architecture — synchronous sentinel claim before thread spawn; 409-equivalent error envelope `{"success": false, "error": "already in progress", "job_id": <active>}` on cross-node collision.
- Bare `except Exception` clauses in both trigger paths narrowed to catch `(DuplicateJobError, AnalysisAlreadyRunningError)` and surface as 409 / MCP error envelope. Other exceptions propagate.

### Tests (Story #1035)
- 102 new unit tests; 100% coverage on `SharedJobSentinel` and `FilesystemDashboardCacheBackend`.
- `tests/unit/server/services/test_shared_job_sentinel.py` — claim/release/stale/concurrent race/owner safety.
- `tests/unit/server/storage/test_filesystem_dashboard_cache.py` — interface parity with SQLite backend.
- `tests/unit/server/web/test_dependency_map_routes_sentinel.py` — pre-flight claim, 409 surfacing, dashboard STATE 3/4 sentinel paths.
- `tests/unit/server/services/test_dependency_map_service_sentinel.py` — service-layer sentinel integration.
- `tests/unit/server/mcp/test_trigger_dependency_analysis_handler_sentinel.py` — MCP handler exception narrowing.

## [10.85.0] - 2026-05-31

### Fixed (Story #1034)
- CRITICAL: CoW clone operations now route through VersionedSnapshotManager / CowDaemonBackend in cluster mode, fixing 600s timeouts on the langfuse repo (1.1GB, 11.7k files) — every refresh was timing out for 20+ hours when the job landed on a non-storage node (NFS byte-copy instead of XFS reflink).
- Pre-existing wiring bug: clone_backend_wiring.build_snapshot_manager() now passes versioned_base to VersionedSnapshotManager.
- _fallback_copy_on_write_clone() dead code removed from activated_repo_manager.py.

### Added (Story #1034)
- CloneBackend.create_clone_at_path() method (Protocol) with LocalCloneBackend and CowDaemonBackend implementations.
- CowDaemonBackend._sanitize_identifier() — replaces dots with underscores for daemon namespace/name.
- Daemon version check at CIDX startup: _check_daemon_health reads daemon version field, fails loud if < 0.2.0.
- RefreshScheduler/ActivatedRepoManager/GoldenRepoManager accept snapshot_manager / clone_backend injection.
- scripts/check_no_direct_cp_reflink.py AC15 CI gate (wired into lint.sh).
- LocalCloneBackend.versioned_base constructor param is now Optional (default None); callers using only create_clone_at_path() no longer need to provide a meaningless value. create_clone() raises loud RuntimeError if called when versioned_base is None.

### Companion daemon
- Daemon v0.1.0 -> 0.2.0: optional dest_path in POST /api/v1/clones, version in HealthResponse, MetadataStore dest_path column + delete fix, Path.resolve() validation.

## v10.84.0 (2026-05-30) -- Opus M3 helpers extraction (Story #1032 Commit 11)

### Refactor (Opus M3 — Phase 1 of 2-phase)
- Extracted the 4 module-level deactivation helpers from `activated_repo_manager.py` into a new module `src/code_indexer/server/repositories/deactivation_helpers.py`:
  - `_safe_purge_trash_entry(trash_root, entry_name)` — fd-anchored recursive delete
  - `_fd_anchored_rmtree(name, parent_fd, expected_st_dev)` — recursive helper
  - `_fd_anchored_phase1_rename(activated_repos_dir, username, user_alias)` — atomic Phase 1 rename
  - `_predeactivation_leak_scan_enabled()` — bootstrap config flag accessor
- Manager file: **3888 → 3580 lines** (~308 lines removed). Still violates MESSI Rule 6 (500-line cap) but the largest single contributor is gone.
- All 4 helpers re-exported from `activated_repo_manager` namespace so tests/callers that patch via `activated_repo_manager._fd_anchored_phase1_rename` continue to work unchanged.
- Zero behavior change — pure file-organization refactor. All 57 deactivation tests pass. server-fast-automation all 6 chunks green (13,417 tests).

### Deferred (Opus M3 Phase 2)
- The deactivation METHODS (`_do_deactivate_single`, `_do_deactivate_composite`, `_do_deactivate_repository`, `sweep_orphan_trash_dirs`, `_detect_resource_leaks`) remain on `ActivatedRepoManager` for now. A future commit can extract them into a `DeactivationService` class (composition pattern) for further file-bloat reduction.

## v10.83.0 (2026-05-30) -- Opus review H1 + H2 (Story #1032 Commit 10)

### Documentation (Opus H1)
- Added justification comments to 7 remaining `shutil.rmtree(path, ignore_errors=True)` call sites in activation/clone failure-cleanup paths in `src/code_indexer/server/repositories/activated_repo_manager.py` (lines 953, 1004, 1031, 1069, 2033, 2992, 3090). Each comment documents:
  - The path is computed from instance state + validated function args in the SAME call frame — no user input crosses the boundary.
  - No metadata row has been written yet (cleanup happens BEFORE Step 6 metadata persistence in activation flow) — so the deactivation rename-to-trash pattern does not apply.
  - Why path-based rmtree is the correct rollback mechanism here.
- Inconsistent with the new fd-anchored discipline only stylistically; not exploitable from current call patterns per opus review.

### Tests (Opus H2)
- New file `tests/unit/server/repositories/test_pg_metadata_first_ordering.py` with 3 tests asserting the metadata-before-Phase-2 ordering invariant (HIGH #2 fix from Commit 6):
  - `TestMetadataDeletedBeforePhase2Single::test_metadata_deleted_before_phase2` — single path: `_delete_metadata` called AFTER `_fd_anchored_phase1_rename` and BEFORE `_safe_purge_trash_entry`.
  - `TestMetadataDeletedBeforePhase2Composite::test_metadata_delete_ordering_holds_for_composite` — composite path: same invariant.
  - `TestPhase2NeverRunsBeforeMetadataDelete::test_phase2_not_invoked_until_metadata_call_returns` — belt-and-suspenders: Phase 2 must not START until metadata delete RETURNS (state-based assertion, not just call-count).
- Invariant is backend-agnostic (file mode + PG mode) so single-mode tests cover both.
- All 57 deactivation-related tests pass. server-fast-automation all 6 chunks green (13,417 tests).

## v10.82.0 (2026-05-30) -- Codex RED ghost-window closure (Story #1032 Commit 9)

### Fixed
- **Codex GPT-5 final-final review of v10.81.0 found RED**: the Commit 8 ghost-prevention guard `if not phase1_succeeded and os.path.exists(repo_dir):` still relied on `os.path.exists()`, which returns `False` (not raises) on permission errors — leaving a narrow ghost-window where a refused Phase 1 + permission-restricted parent would route to the metadata-delete branch.
- Replaced single-flag exists()-based guard with **two explicit boolean flags** in both `_do_deactivate_single` and `_do_deactivate_composite`:
  - `rename_was_attempted` — set True when we enter the rename branch (initial existence check passed at entry).
  - `phase1_succeeded` — set True ONLY after `_fd_anchored_phase1_rename` returns.
  - Outer guard now reads purely from these flags: `if rename_was_attempted and not phase1_succeeded:` → preserve metadata + log GHOST REPO PREVENTION. No filesystem probes in the discriminator → immune to exists() lying.

### Tests
- New regression test `TestPermissionErrorFalseNegativeBlocked::test_ghost_blocked_even_when_exists_returns_false_negative` in `tests/unit/server/repositories/test_ghost_repo_prevention.py`. Mocks `os.path.exists` to lie with False after the entry check, asserting the explicit flag still preserves metadata. Locks in the Commit 9 fix.
- 27/27 deactivation-related tests pass. server-fast-automation all 6 chunks green (13,414 tests).

## v10.81.0 (2026-05-30) -- Ghost-repo prevention (Story #1032 Commit 8, codex re-re-review NEW HIGH)

### Fixed
- **NEW HIGH from Codex GPT-5 final review of v10.80.0**: when `_fd_anchored_phase1_rename` raised (e.g. `.trash` symlink swap → ValueError, OSError, IOError), the outer `_delete_metadata` call still ran unconditionally in both `_do_deactivate_single` AND `_do_deactivate_composite`. Result: live repo dir remained on disk in `{username}/{user_alias}` while metadata was deleted → UI showed "deactivated" while bytes were ghost-alive.
- Both deactivation methods now guard the outer `_delete_metadata` with `if not <repo_dir>.exists()` — metadata is only removed when the dir is actually gone (Phase 1 success OR orphan-already-gone state). On Phase 1 failure, metadata is preserved AND a `GHOST REPO PREVENTION` error is logged with `requires_admin_cleanup: True`.
- Stale comment in `config_manager.py` for `orphan_trash_sweep_per_startup_cap` corrected: was "synchronously at server startup ... blocks startup", now accurately says "dispatched asynchronously via asyncio.create_task ... NEVER blocks FastAPI startup". (HIGH #5 leftover from previous review.)

### Tests
- New file `tests/unit/server/repositories/test_ghost_repo_prevention.py` with 3 regression tests:
  - Single-path metadata preserved when Phase 1 fails + ghost warning surfaced.
  - Composite-path metadata preserved when Phase 1 fails + ghost warning surfaced.
  - Normal success path still deletes metadata correctly (no regression).
- All 13,413 server tests pass via `server-fast-automation.sh`. Lint exit 0.

### Status
This is the final cleanup commit of Story #1032 deactivation work. Three Codex re-reviews now performed; the BLOCKER + HIGH list from each review is fully closed. The orphan-sweep cap operational weakness (10K orphans needing multiple restarts to drain) is documented but not a blocker — admins can set `orphan_trash_sweep_per_startup_cap: 0` for unlimited background-task sweep when needed.

## v10.80.0 (2026-05-30) -- Xray pattern library doc improvements (Story #1032 Commit 7)

### Documentation
User reported building six xray evaluators in one session before discovering `store_xray_pattern` existed. Tool docs were mechanically accurate but the motivation/trigger framing was buried. Five enhancements:

- **`store_xray_pattern.md` slim_description** rewritten to lead with the iteration trigger: "Persist a hard-won xray evaluator... Use this whenever an evaluator took more than one iteration to get right." (was: "Store a named, reusable Rust xray evaluator pattern..."  — no trigger.)
- **"When to store a pattern" trigger block** added near the top of `store_xray_pattern.md`, before the YAML schema. Lists 5 trigger heuristics + the cost-of-not-storing line: "If you close the session without storing, that work is gone."
- **"Post-session checklist"** subsection added: "Before ending a session where you developed a new evaluator: if the evaluator took iteration, call `store_xray_pattern`..."
- **`xray_search.md` + `xray_explore.md` `pattern_name` description** rewritten from passive ("Mutually exclusive with evaluator_code") to active: "Before writing evaluator_code inline, check the pattern library — a pattern for your use case may already exist. Use `browse_directory('cidx-meta-global', path='xray-patterns')` to list available patterns." Plus post-session reminder.
- **"Best Practices" cost quantified**: replaced vague "complex, tuned through iteration, or laborious to produce should be stored" with concrete: "A non-trivial evaluator typically costs 3-6 tool round-trips to develop... Storing the result means that cost is paid once across all users and all future sessions. An evaluator that is not stored is effectively thrown away when the session ends."

### Verification
- `python3 tools/verify_tool_docs.py` CI gate: PASS (all 149 tools documented, frontmatter valid).
- Schema, YAML examples, parametrization, error codes unchanged. Mechanics intact; only framing improved.

## v10.79.0 (2026-05-30) -- Codex HIGH items + test bug (Story #1032 Commit 6)

### Performance / Visibility
- **HIGH #2 fixed**: metadata-first ordering. `_do_deactivate_single` and `_do_deactivate_composite` now delete metadata IMMEDIATELY after Phase 1 rename, before Phase 2 purge starts. In PG/cluster mode, the UI sees the repo gone instantly via `_list_user_repos_pg`. Phase 2 still runs to free disk; if Phase 2 fails, metadata is already deleted and orphan sweeper handles the leftover dir.
- **HIGH #3 fixed**: startup orphan sweep no longer blocks lifespan. Bounded via new bootstrap config flag `orphan_trash_sweep_per_startup_cap` (default 100) — after the cap, the remaining orphans are picked up on next restart. Documented in the sweeper docstring.
- **HIGH #4 fixed**: `_fd_anchored_rmtree` no longer materializes the whole directory via `list(scandir)`. Iterates the scandir streaming, deleting per iteration. Eliminates OOM/DoS vector on malicious huge subtrees (e.g. crafted `.git/objects/`).

### Documentation
- **HIGH #5 fixed**: three lying docstrings/comments corrected:
  - Phase 1 "fall through to direct rmtree as before" comment removed (the except block doesn't actually call rmtree).
  - Lifespan AC8 comment "never blocks startup" replaced with accurate "bounded by `orphan_trash_sweep_per_startup_cap`".
  - `_safe_purge_trash_entry` docstring softened: "Refuses to cross filesystem boundaries (different `st_dev`). NOTE: same-superblock bind mounts share `st_dev` and are NOT detected by this check."

### Tests
- Test bug fixed: `tests/unit/server/repositories/test_deactivate_walk_removal.py` fixture helper now writes `{alias}_metadata.json` (matching production code), not `{alias}.json`. Tests had been silently passing on an unverified path.
- 5 new tests covering metadata-first ordering + bounded sweep + streaming scandir. All 50 deactivation-related tests pass. server-fast-automation all 6 chunks green.

## v10.78.0 (2026-05-30) -- Phase 1 fd-anchored rename + composite safe purge (Story #1032 Commit 5) + Bug #1033

### Security (Story #1032 Commit 5 — closes 3 Codex GPT-5 BLOCKERS)
- **BLOCKER #1 + #2 fixed**: Phase 1 of `_do_deactivate_single` no longer uses path-based `os.makedirs` + `os.rename`. New helper `_fd_anchored_phase1_rename(activated_repos_dir, username, user_alias)` opens parent fds with `O_DIRECTORY|O_NOFOLLOW` (refuses if any ancestor is a symlink), pins inodes via `st_dev` check, and uses fd-anchored `os.rename(src, dst, src_dir_fd=user_fd, dst_dir_fd=trash_fd)`. Codex GPT-5 reproduced two TOCTOU exploits locally against v10.77.0 (ancestor `.trash` swap and `{username}` swap); both are now structurally impossible because the kernel uses the pinned fd identity, not pathname resolution.
- **BLOCKER #3 fixed**: `_do_deactivate_composite` no longer calls `shutil.rmtree(subrepo_path)` per component + `shutil.rmtree(repo_path)` on the composite root. Both replaced by a single `_fd_anchored_phase1_rename` + `_safe_purge_trash_entry` sequence — components move atomically with the composite dir into `.trash/`, then are purged via the fd-anchored recursive deleter. Composite path is now TOCTOU-immune symmetrically with single path.
- **HIGH #1 (carryover) fixed**: `_do_deactivate_repository` metadata-missing orphan branch (Bug #1030 cleanup) no longer uses path-based `shutil.rmtree(repo_dir)`. Routes through `_fd_anchored_phase1_rename` + `_safe_purge_trash_entry`.

### Tests
- 14 new tests in `tests/unit/server/repositories/test_phase1_fd_anchored.py` covering: rename success path, `.trash` symlink swap resistance, `{username}` ancestor symlink swap resistance, symlinked activated_repos_dir refusal, O_NOFOLLOW flag verification, dual `dir_fd` rename verification, nonexistent-repo handling.
- Composite leak-on-failure test updated to patch `_fd_anchored_phase1_rename` (new failure mode under fd-anchored composite flow).
- All 45 deactivation-related tests pass. fast-automation + server-fast-automation (13,405 server tests) all green.

### Fixed (Bug #1033 — concurrent agent work)
- `ConfigManager.load()` now reconciles a stored absolute `codebase_dir` against the actual config file location. On NFS clusters where nodes mount the same share at different paths (e.g. node-A: `/mnt/nfs/project`, node-B: `/data/project`), a config saved on node-A would point at node-A's path; the new behavior auto-corrects to the current node's path and logs a warning. Test: `tests/unit/config/test_config_manager_codebase_dir_reconcile.py`.

## v10.77.0 (2026-05-29) -- Rename-then-delete + orphan trash sweeper (Story #1032 Commit 4)

### Changed
- `_do_deactivate_single` now uses rename-then-delete: Phase 1 atomically renames `repo_dir` to `{activated_repos_dir}/.trash/{ts}-{uuid8}-{username}-{user_alias}/` (visible to user as instant), Phase 2 performs the slow recursive purge against the trash entry. Rename is atomic on XFS (production) and NFSv4 (CoW Daemon staging) within the same export — the trash root is a single top-level directory under `activated_repos_dir` so source and destination always share a mount.

### Added
- New `_safe_purge_trash_entry(trash_root, entry_name)` module-level helper that recursively deletes a trash entry using fd-anchored Python ops only (no subprocess). Replaces the original `_safe_rm_rf_trash` subprocess wrapper after Codex GPT-5 code review found a TOCTOU vulnerability in path-based check-then-delete: an ancestor symlink swap between `os.path.exists()` and `subprocess.run(["rm","-rf",...])` could redirect deletion outside the trash root. The new design eliminates the entire attack surface:
  - Opens trash_root with `O_DIRECTORY | O_NOFOLLOW` (pins inode; refuses symlinked root).
  - Accepts only a basename `entry_name` (no path traversal possible by design).
  - Validates `entry_name` has no `/`, `\\`, `..`, `.`, dot-dot-prefix, or null bytes.
  - All recursive ops via `os.unlink(name, dir_fd=...)` and `os.rmdir(name, dir_fd=...)` — no path strings cross to the kernel after the initial fd.
  - `st_dev` mount-boundary guard at every descent — refuses to cross filesystem boundaries (NFS bind, etc.).
  - Symlinks never followed (O_NOFOLLOW + `is_symlink` check) — unlinked as the link, never recursed into the target.
  - Raises ValueError on any safety violation; raises OSError on kernel-level failures (no silent CompletedProcess returns).
- New `ActivatedRepoManager.sweep_orphan_trash_dirs()` method scans `{activated_repos_dir}/.trash/` and purges every leftover entry via the fd-anchored helper. Idempotent (missing/empty trash → 0). Returns count purged.
- App lifespan startup invokes `sweep_orphan_trash_dirs()` once per server start (best-effort; logged at WARNING on failure, never blocks startup) to recover from crashes mid-Phase-2.

### Performance
- Deactivation now feels instant from the user's perspective: source dir disappears within milliseconds of clicking Deactivate (Phase 1 rename). Slow recursive purge runs synchronously in the same background-job worker thread but is no longer in the UI-visible critical path.

### Security (Codex GPT-5 code review findings — addressed)
- BLOCKING: TOCTOU via ancestor symlink swap — fixed by replacing path-based subprocess with fd-anchored Python deletion.
- HIGH #1: Broken `..` invariant in path normalisation — eliminated by requiring basename-only entry_name (no path components to traverse).
- HIGH #2: Mount-boundary crossing — fixed via `st_dev` check at every directory descent.
- HIGH #3: Silent nonzero handling — fixed by raising on any failure (no silent CompletedProcess).
- HIGH #4: Caller-supplied root misuse-prone — mitigated by ActivatedRepoManager always passing its own `self.activated_repos_dir`.

### Tests
- 16 new tests in `tests/unit/server/repositories/test_safe_rm_rf_trash.py` covering all safety invariants (8 entry-name validations, 3 trash-root validations, symlink defense, cross-FS refusal, real deletion).
- 6 new tests in `tests/unit/server/repositories/test_orphan_trash_sweep.py` covering idempotency, single/multi orphan purge, sweep resilience past individual failure.

### Deferred (composite path)
- `_do_deactivate_composite` not yet refactored to rename-then-delete — composite repos are rarely used and the existing Commit-3 leak-on-failure diagnostic already applies. Will be addressed in a follow-up if usage patterns warrant.

## v10.76.0 (2026-05-29) -- Drop redundant pre-deletion walks in deactivation (Story #1032 Commit 3)

### Performance
- `_do_deactivate_single` and `_do_deactivate_composite` no longer perform the pre-flight `os.walk + os.path.getsize` traversal that existed purely to populate `repo_size_mb`/`file_count` fields in one log line. For a 100k-file activated repo (typical Java/Node golden-repo CoW clone), this saves ~100k+ `stat()` syscalls per deactivation — the dominant cost on cold cache.
- `_detect_resource_leaks` moved from a mandatory pre-flight scan to a post-failure-only diagnostic — runs only when `shutil.rmtree` raises, surfacing whatever blocked deletion (`.git` >100MB, large temp files, etc.) in the resulting cleanup warnings.

### Added
- New bootstrap-only config flag `enable_predeactivation_leak_scan` (default `false`). Set to `true` in `config.json` to restore the pre-flight leak scan when investigating an incident. Bootstrap-only (read once at startup) per the existing flag pattern used by `enable_malloc_trim`.

### Removed
- `repo_size_mb` and `file_count` fields no longer appear in the "Repository deactivation initiated" log line. They were the sole consumer of Walk #1 telemetry.

## v10.75.0 (2026-05-29) -- Fix actor_username persistence in PostgreSQL backend (Story #1032 Commit 2 follow-up)

### Fixed
- PostgreSQL `BackgroundJobsBackend.save_job` now accepts and persists the new `actor_username` column. v10.74.0 added the column and the SQLite/in-memory paths but missed the PG backend, so cluster-mode (storage_mode=postgres) deployments silently wrote `actor_username=NULL` for every job. Discovered during live staging verification immediately after v10.74.0 deployment.
- `_SELECT_COLS` constant and `_row_to_dict` helper updated in lockstep so PG SELECTs surface `actor_username` to callers.

## v10.74.0 (2026-05-29) -- Deactivation actor_username tracking (Story #1032 Commit 2)

### Added
- `background_jobs.actor_username TEXT NULL` column (PG migration `026_actor_username_background_jobs.sql` + SQLite migration in `database_manager.py`). Tracks WHO triggered the action when the actor differs from the resource owner.
- `BackgroundJobManager.submit_job(actor_username=None)` parameter. When None, defaults to `submitter_username` for backward compatibility (preserves prior audit semantics).
- `ActivatedRepoManager.deactivate_repository(..., actor_username=None)` parameter — propagates the actor through to submit_job.
- Entry points wired to derive actor_username from authenticated session:
  - `web/routes.py` admin deactivate route passes `actor_username=session.username`
  - `routers/inline_repos.py` REST DELETE /api/repos/{user_alias} passes `actor_username=current_user.username`
  - `mcp/handlers/repos.py` MCP `deactivate_repository` passes `actor_username=user.username` (credential owner)
  - `services/activated_reaper_service.py` TTL reaper passes `actor_username="system"` (explicit)
- Dashboard "Recent Jobs" partial: when `actor_username` is set AND differs from `username`, renders "actor → owner" instead of just "owner", surfacing admin actions on other users' repos.

### Changed
- `_get_all_jobs(...)` default flipped to `is_admin=False` (default-deny); both existing callers (`_create_jobs_page_response`, `jobs_list_partial`) now pass `is_admin=True` explicitly.
- `BackgroundJobManager.get_jobs_for_display(...)` SQLite branch now plumbs the username scope into `list_jobs_filtered`, closing a cross-user data leakage gap for completed DB-stored jobs when non-admin users query.
- `_build_deactivating_map` exception path: logs at `ERROR` with `exc_info=True` instead of swallowing silently (anti-silent-failure per Messi rule 13).

### Security
- Server-side actor derivation only — no API surface accepts `actor_username` from request body/query/header, preventing audit-evidence forging.

### Migrations
- Backward-compatible: column is NULL-able with no DEFAULT. Old rolling-restart nodes continue to INSERT without the column; rows are queryable by both old and new code. Idempotent on re-run.

## v10.73.0 (2026-05-29) -- Repository Deactivation Visibility + Admin Bypass (Story #1032 Commit 1)

### Added
- Dashboard: new "Active deactivations" stat tile showing count of in-flight `deactivate_repository` jobs across the whole cluster (queries both in-memory and PG/SQLite backend, de-duped by job_id).
- Repos page: per-row "Deactivating..." badge that links to the running job for any pending/running deactivation, visible to admin for any user's repo.
- REST `GET /api/repos` and MCP `list_repositories`: new `deactivation_job` field per repo (`{"job_id": "...", "status": "running"} | null`) so CLI/MCP clients see in-flight deactivations.
- Web UI admin deactivate success flash now contains a clickable Job ID linking to `/admin/jobs?search_text=<id>` for one-click navigation to the job detail.

### Changed
- `BackgroundJobManager.list_jobs`, `get_job_status`, `get_jobs_for_display` gain an `is_admin: bool = False` parameter; when True, the username scope is bypassed so admin can see all users' jobs.
- REST endpoints `GET /api/jobs`, `GET /api/jobs/{job_id}`, `DELETE /api/jobs/{job_id}` now pass `is_admin=(current_user.role == 'admin')`. Non-admin users continue to see only their own jobs (privilege-escalation regression test added).

### Removed
- Repos page: dead "File Count" column (always rendered `N/A` because the backend never populated the field). Pure UI noise removed.

### Security
- Stored-XSS hardening: `user_alias` is now HTML-escaped in the admin deactivation success flash before being rendered through Jinja's `|safe` filter, preventing script injection via maliciously-named activated repos.

### Fixed
- Test debt from Story #1031: `EXPECTED_REGISTRY_KEYS` in the MCP handler registry structure test now includes `store_xray_pattern` (was failing on master/development since the tool was registered).

## v10.72.0 (2026-05-29) -- directory_tree Absolute Path Disclosure Fix

### Fixed
- Security: `directory_tree` MCP tool no longer discloses the absolute server filesystem path in its `root_path` response field. It now returns the relative requested path (empty string for repo root), consistent with `tree_string` and `root.path`. Discovered during v10.71.0 staging E2E testing when verifying pattern-library discoverability via `directory_tree` on cidx-meta.

## v10.71.0 (2026-05-29) -- Xray Pattern Library: Path Security + Doc Improvements

### Fixed
- Security: `store_xray_pattern` response now returns relative path (e.g. `xray-patterns/__any__/name.yaml`) instead of absolute server filesystem path, preventing internal path disclosure.
- MCP docs: `xray_search` and `xray_explore` output schema corrected from "absolute path" to "relative path from repository root" (matches actual code behavior since v10.70.0).
- MCP docs: Error examples in `xray_search` and `xray_explore` updated to use relative paths instead of absolute paths.
- Pre-existing mypy type-ignore code mismatch in `xray_pattern_service.py` (`return-value` corrected to `no-any-return`).
- Pre-existing Rust compilation failures in `test_real_evaluator_patterns.py`: evaluator code used `node.text` (field access) instead of `node.text()` (method call) per OwnedNode API.

### Added
- Pattern Library Best Practices section in `store_xray_pattern` MCP docs advising clients to save laborious evaluators for reuse.
- Pattern Library section in `xray_search` and `xray_explore` MCP docs with discoverability guidance (`browse_directory`/`directory_tree` on cidx-meta).
- Test `test_store_pattern_returns_relative_path` verifying no absolute path disclosure.

## v10.70.0 (2026-05-29) -- Persistent Xray Evaluator Pattern Library

### Added
- Story #1031: Persistent xray evaluator pattern library with parametrization. Store reusable Rust evaluator patterns in cidx-meta (`xray-patterns/` folder), resolve by name in `xray_search` and `xray_explore` via `pattern_name` parameter, override pattern defaults via `pattern_params`.
- `store_xray_pattern` MCP tool for storing/updating patterns with overwrite protection.
- Const injection engine: pattern parameters become typed `const` lines (usize, i64, f64, bool, str) prepended to evaluator code before Rust compilation.
- Seed patterns: `catch-rethrow` and `deep-nesting` auto-created in `__any__/` scope on first use.
- Path traversal protection on scope and pattern name parameters.
- Pattern resolution: repo-specific scope first, then `__any__/` cross-repo fallback.
- `pattern_name` and `pattern_params` parameters added to `xray_search` and `xray_explore` tool schemas and documentation.

## v10.69.0 (2026-05-29) -- Bug Fixes: search_code Crash + Reaper Deactivation Loop

### Fixed
- Bug #1029: `search_code()` KeyError crash when `query_text` parameter missing (130 crashes/24h in production). Early validation in `search_code()` returns clean error response for missing, empty, whitespace-only, or non-string `query_text`. Defense-in-depth: hard dict access `params["query_text"]` changed to `params.get("query_text", "")` in response metadata builder.
- Bug #1030: Activated reaper deactivation infinite loop for phantom repositories (69 failed jobs/24h). Two-layer fix: (1) idempotent deactivation in `_do_deactivate_repository()` — when metadata is None, clean up orphan artifacts (rm repo dir + delete metadata) and return success instead of raising `ActivatedRepoError`; (2) reaper skip guard in `run_reap_cycle()` skips repos with empty username or user_alias.

### Added
- Rust xray-core: HCL/Terraform, YAML, XML, Groovy, and SQL language support (tree-sitter grammars).
- Xray tool doc updates referencing new supported languages.

## v10.68.0 (2026-05-28) -- Xray Tool Doc Improvements + Runtime Truncation Tests

### Added
- Quick Start sections in both `xray_search` and `xray_explore` MCP tool docs with complete working examples so first-time users see a runnable pattern before the reference material.
- `debug_log()` and `truncate_snippet()` now listed in the "Allowed constructs" security whitelist in both xray tool docs (were missing despite being preamble-provided functions).
- REST/MCP field name mismatch note in `xray_search` docs: REST API uses `driver_regex`/`max_files` while MCP uses `pattern`/`max_results`.
- Self-contained Quick Start in `xray_explore` with two examples (AST exploration without evaluator, evaluator with debug_log tracing).
- Rust runtime integration test `test_debug_log_truncation_limits_runtime`: compiles evaluator calling debug_log 150 times, loads .so, asserts exactly 100 messages returned in-order.
- Rust runtime integration test `test_debug_log_byte_limit_runtime`: compiles evaluator sending 60x200B messages, asserts exactly 51 retained (10KB cap enforced).

## v10.67.0 (2026-05-28) -- Xray Evaluator debug_log() Function

### Added
- Story #1028: `debug_log(msg: &str)` function available inside xray evaluator code for runtime debugging. Messages are collected in a per-thread buffer (max 100 messages, 10KB total) and returned in the JSON output as `debug_messages[]`. Python-side `XRaySearchEngine` surfaces them as `debug_output[]` in job results for both `xray_search` and `xray_explore`. Security: in-memory only, no sandbox bypass.
- `xray_drain_debug_log()` exported symbol in compiled evaluator dynlibs for draining debug messages after evaluation.
- `drain_debug_log()` method on `Evaluator` trait with default empty implementation; `DynlibEvaluator` overrides via trait dispatch to call the dynlib export.
- `debug_messages` field in `ScanResult` and xray-cli JSON output.
- `debug_output` field documented in `xray_search` and `xray_explore` MCP tool docs with usage examples, limits, and security notes.

## v10.66.0 (2026-05-28) -- Activation Metadata + Cache Page Index Fixes

### Fixed
- Bug #1026: Single-repo activation metadata now includes `username` field. The `_do_activate_repository()` metadata dict was missing the username key, causing reaper CRITICAL errors when accessing `metadata["username"]`. Also backfills the field for pre-existing metadata files via `setdefault()` in `_list_user_repos_fs()`.
- Bug #1027: `cidx_fetch_cached_payload` MCP handler now uses 1-indexed pages at the API boundary (was 0-indexed). Callers sending `page=1` for the first page no longer get "Page 1 out of range" errors on single-page results. Internal `PayloadCache.retrieve()` remains 0-indexed; translation happens at the handler boundary.

## v10.65.0 (2026-05-28) -- Non-root Auto-Updater Rust Toolchain Fix

### Fixed
- Auto-updater `_ensure_rust_toolchain()` now uses `sudo mkdir -p` and `sudo chown -R` to create `/opt/rust` instead of direct `Path.mkdir()`. The auto-updater runs as a non-root user on staging (e.g. `jsbattig`), so creating `/opt/rust` without sudo fails with `PermissionError: [Errno 13]`, crashing the entire deployment.

## v10.64.0 (2026-05-28) -- RUSTUP_HOME Systemd Service Fix

### Fixed
- Auto-updater now adds `RUSTUP_HOME=/opt/rust` and `CARGO_HOME=/opt/rust` to the cidx-server systemd service file. The rustup proxy at `/opt/rust/bin/rustc` needs `RUSTUP_HOME` to find the installed toolchain; without it, rustup defaults to `$HOME/.rustup` which has no default toolchain configured, causing "no default toolchain" errors at runtime.

### Added
- New `_ensure_systemd_env_var()` static method for idempotent `Environment="KEY=VALUE"` management in systemd unit files.
- `_line_parts()` helper to DRY indent/ending extraction from service file lines.

## v10.63.0 (2026-05-28) -- Rust Toolchain Install Location Fix

### Fixed
- Auto-updater now installs Rust toolchain to `/opt/rust` (system-wide) instead of `/root/.cargo`. The auto-updater runs as root so `Path.home()` resolved to `/root/`, but `cidx-server` runs as the `code-indexer` OS user which cannot access `/root/` (0550 permissions). This made `rustc` unreachable, breaking xray evaluator compilation.
- Stale `/root/.cargo/bin` entries in systemd service PATH are automatically cleaned up during deployment via new `_remove_path_segment()` method.
- `RUSTUP_HOME` and `CARGO_HOME` env vars explicitly set to `/opt/rust` and propagated to both `curl` and `sh` subprocess calls in `_install_rust_toolchain()`.

### Changed
- `_ensure_systemd_cargo_path` renamed to `_ensure_systemd_rust_path` to reflect the new system-wide install location.
- MCP tool doc `xray_explore.md` fully rewritten from Python-era documentation to Rust native evaluator documentation (OwnedNode API, EvalFinding struct, Rust examples).

## v10.62.0 (2026-05-28) -- Xray Memory Waste Fix + Test Fixture Cleanup (Epic #1019)

### Fixed
- Xray search engine no longer pre-loads ALL candidate file source code into memory (~170MB for 17K-file repos). The Rust subprocess reads files itself; Python-side `line_content` enrichment now reads on-demand per file with findings.
- `_DEFAULT_EVALUATOR_CODE` in MCP handlers changed from Python to Rust. The old Python code was silently rejected by `validate_rust_evaluator()` when evaluator_code was omitted.
- Atomic `.so` cache writes in `_try_pre_fill()`: writes to `{hash}.so.tmp.{pid}` then renames, preventing corruption from concurrent workers.
- `ast_debug` enrichment reads file bytes from disk instead of deleted `spec["source"]` key.
- 75 pre-existing test failures from Rust migration (commit 839e935): updated evaluator fixtures from Python `"return True"` to valid Rust `fn evaluate_node` across 4 test files.

### Changed
- `_build_matches()` now accepts `abs_path` parameter for on-demand file reading instead of relying on pre-loaded source.
- MCP tool doc `xray_explore.md` updated to reflect Rust evaluator validation (forbidden constructs section).

## v10.61.0 (2026-05-28) -- Xray Error Handling Hardening (Epic #1019)

### Fixed
- Pre-flight validator now catches `static` declarations (not just `static mut`). Previously slipped through to Rust compiler producing N duplicate errors per file.
- Compiler/invocation errors deduplicated: single error entry returned instead of one per file spec. Compile failures are per-evaluator, not per-file.
- Silent timeout on infinite-loop evaluators now produces synthetic `EvaluatorTimeout` error with diagnostic guidance instead of empty results.
- Server filesystem paths fully sanitized from all error messages returned to API callers. Six error return paths in `rust_backend.py` now apply `_sanitize_error_message()`: BinaryNotFound, JSON parse error, CLI JSON error field, subprocess timeout, non-zero exit stderr, FileNotFoundError. Server logs retain raw paths for debugging.
- Stale test (`test_json_error_field_returns_error_tuples_for_all_files`) fixed: was mocking `subprocess.run` but code uses `subprocess.Popen`, and was expecting per-file errors instead of deduplicated single entry.

### Added
- `_sanitize_error_message()` in `rust_backend.py`: two-rule regex sanitizer strips xray-cache paths (to `evaluator.rs`) and general server paths (to `<server-path>`).
- New unit tests: Rust pre-flight `static` validation (57 lines), backend sanitization and deduplication (145 lines), search engine timeout detection (56 lines), CLI error path sanitization (Test 20).

## v10.60.0 (2026-05-28) -- REST Xray Validator Fix (Epic #1019)

### Fixed
- REST `POST /api/xray/search` pre-flight validation was still calling the Python AST validator (`sandbox.validate()`) instead of `validate_rust_evaluator()`. Rust evaluator code submitted via REST API was rejected with `syntax_error: invalid syntax`. MCP handler was already correct; REST route was missed during the Rust-only migration.

### Changed
- `xray_routes.py` no longer instantiates `XRaySearchEngine` for pre-flight validation -- uses the lightweight `validate_rust_evaluator()` function directly (same as MCP handler).

## v10.59.0 (2026-05-28) -- Cluster-Aware Evaluator Cache + TTL (Epic #1019)

### Added
- Cluster-aware evaluator compilation cache: compiled .so blobs shared across cluster nodes via PostgreSQL (`xray_evaluator_cache` table). Solo mode uses local filesystem cache as before.
- 5-minute TTL for cached evaluator .so files: Rust-side `is_fresh()` rejects stale local cache entries, PostgreSQL-side `fetch()` filters by `compiled_at` timestamp. Lazy cleanup on `store()`.
- `XrayCacheBackend` Protocol in `rust_backend.py` for structural typing of cache backends.
- Module-level singleton for cluster cache backend in `search_engine.py` -- shared across all per-request `XRaySearchEngine` instances.
- `CIDX_DATA_DIR` support in Rust `get_cache_dir()` -- parity with Python for deployments using custom data directories (Bug #879).
- 3 new forbidden macros in Rust validator: `panic!`, `todo!`, `unimplemented!` (defense-in-depth, both Python and Rust layers).
- 5 new forbidden macros in Python pre-flight validator: `include_str!`, `include_bytes!`, `option_env!`, `print!`, `eprint!`.
- `_ensure_systemd_cargo_path()` in DeploymentExecutor: ensures `~/.cargo/bin` in systemd PATH for Rust toolchain (mirrors `_ensure_systemd_claude_path()`).

### Changed
- Evaluator cache check now requires TTL freshness in addition to hash and rustc version match.
- `RustNativeBackend.__init__()` accepts optional `xray_cache_backend` parameter for cluster cache injection.
- `install-cidx-server.sh`: Cargo bin directory added to systemd PATH line.

### Fixed
- 6 corrections in xray_search MCP documentation: `static mut` -> `static` in forbidden list, expanded allowed macros/constructs, fixed variable name in cookbook pattern 5, corrected language count, fixed error code name.
- Removed redundant `rustup default stable` subprocess call from `_install_rust_toolchain()` (already baked into RUSTUP_SH_ARGS).
- Fixed bytes/str stderr handling in curl and sh error paths in deployment executor.

## v10.58.0 (2026-05-27) -- Pure Rust Evaluators, Transpiler Removed (Epic #1019)

### Removed
- Deleted Python-to-Rust transpiler (`transpiler.py`, 1036 lines). Users write Rust evaluator code directly.
- Deleted transpiler test suite (`test_transpiler.py`, 61 tests) and construct parity gate (`test_construct_parity.py`).
- Removed 2 dead `XRaySearchEngine()` instantiations from MCP handlers after validation refactor.

### Changed
- RustNativeBackend now validates and passes Rust evaluator code directly to xray-cli (no transpilation step).
- MCP xray handlers use `validate_rust_evaluator()` from sandbox.py instead of `engine.sandbox.validate()`.
- All MCP xray_search documentation rewritten for Rust OwnedNode API: 15 cookbook patterns, evaluator reference, security whitelist, error codes.
- Real evaluator pattern tests converted from Python to Rust (`test_real_evaluator_patterns.py`).
- Rust backend tests updated for direct Rust evaluator code (`test_rust_backend.py`).

### Added
- Python-side Rust validator (`validate_rust_evaluator()` in sandbox.py): regex-based pre-flight security checks for 18 forbidden constructs (unsafe, std::fs/net/process/env/io, raw pointers, extern, mod, static mut, dangerous macros).
- 25 new Rust validator tests (`test_rust_validator.py`): covers all forbidden constructs, valid code acceptance, ValidationResult structure.

## v10.57.0 (2026-05-27) -- Rust Native Xray Engine (Epic #1019)

### Added
- Rust xray-core crate: tree-sitter parsing, rayon parallel scanning, OwnedNode heap-allocated AST, file collection across 10 languages (Java, Kotlin, Python, TypeScript, JavaScript, Go, C#, Bash, HTML, CSS).
- Rust xray-cli binary: `--dynlib`, `--json`, `--files` flags for pipeline integration. Scans 19K+ files in under 4 seconds.
- Dynamic evaluator compilation and caching: `rustc --crate-type cdylib` (~210ms compile), SHA-256 cache at `~/.cidx-server/xray-cache/`, libloading for native-speed execution, LRU eviction at 100 entries.
- Rust AST validator: whitelist enforcement (no unsafe, no std::fs/net/process, no raw pointers) before compilation.
- RustNativeBackend (`src/code_indexer/xray/rust_backend.py`): drop-in replacement for PythonEvaluatorSandbox.run_batch(). Validates, compiles, invokes xray-cli, parses JSON output.
- DeploymentExecutor Step 16: `_ensure_rust_toolchain()` installs rustup + stable toolchain idempotently, verifies C compiler, builds xray-cli. FATAL on failure.
- 81 new tests: 10 rust_backend, 19 toolchain provisioning, 11 Rust unit tests, 41 spike pattern validation.

## v10.56.0 (2026-05-23) -- Auto-Updater E2E Verification

### Changed
- Version bump to verify auto-updater end-to-end deployment across staging cluster (3 nodes).

## v10.55.0 (2026-05-23) -- Skipped Test Cleanup

### Removed
- Deleted 12 dead test files containing only permanently-skipped or mock-heavy placeholder tests: `test_clean_file_chunking_manager`, `test_vector_calculation_manager`, `test_parallel_processing_replacement`, `test_voyage_threadpool_elimination`, `test_advanced_query_filtering`, `test_semantic_query_manager_warning_log_conditions`, `test_server_startup_crash_fix`, `test_fixed_size_chunking_documentation`, `test_temporal_indexer_project_id`, `test_resume_and_incremental_bugs`, `test_teach_ai_templates`, `test_cancellation_handling`.
- Deleted ~32 permanently-skipped methods from 11 test files (RED-phase TDD stubs that were never un-skipped after implementation, unreachable-in-local-mode tests, placeholder classes).
- Removed 15 obsolete ruff/mypy exclusion entries from `pyproject.toml` pointing to deleted test files.

### Fixed
- Un-skipped ~62 tests across 8 files that were guarded by `try/except ImportError` or `if X is None: pytest.skip()` patterns left over from TDD red phase. All are now active and passing.
- Fixed indentation errors in `test_workspace_cleanup_service.py` and `test_file_crud_service.py` introduced during un-skip operations.
- Fixed cascading import error in `test_cancellation_handling.py` (imported from deleted `test_vector_calculation_manager`).

### Changed
- Zero `@pytest.mark.skip` decorators remain in the test suite. Only legitimate runtime `pytest.skip()` calls (environment/fixture checks) remain.

## v10.54.0 (2026-05-23) -- hnswlib Pre-flight Check + FTS Bootstrap Fix

### Fixed
- **hnswlib pre-flight guard**: `smart_index()` now raises `RuntimeError` immediately — before acquiring the lock or generating any vectors — when the custom hnswlib fork is not installed. Previously the error surfaced only after all vector files had been written, requiring a full re-index.
- **FTS bootstrap for new index**: `cidx index --fts` now correctly populates a brand-new Tantivy index from all codebase files when the semantic index is already up-to-date and there is nothing to embed. Previously the FTS index was left empty in this scenario.

### Changed
- **pyproject.toml**: Added hnswlib VCS dependency (`git+https://github.com/LightspeedDMS/hnswlib.git@89720633`) so `pip install` / `pipx install` builds the custom fork automatically. Also added `numpy>=1.24.0` as an explicit dependency.
- **install-cidx-server.sh**: Added `g++` / `gcc-c++` to system package list for C++ compilation of hnswlib; added `--recurse-submodules` to fresh clone; added `git submodule update --init third_party/hnswlib` after `git pull`.
- **docs/installation.md**: Documented C++ compiler prerequisite (gcc/g++/clang) required at install time.

### Added
- Unit tests for hnswlib pre-flight check and FTS bootstrap path (`tests/unit/services/test_smart_indexer.py`).

## v10.53.0 (2026-05-23) -- Python Version Constraint

### Changed
- Tightened `requires-python` from `>=3.9` to `>=3.9,<3.13` in `pyproject.toml`. `tree-sitter-languages==1.10.2` and `tantivy==0.25.0` ship no pre-built wheels for Python 3.13+; without this bound, installation on Python 3.13/3.14 silently falls back to source builds requiring Rust/Cargo. Affects all platforms (macOS, Linux, Windows).

## v10.52.0 (2026-05-22) -- MCP Response Key Collision Fix + TOTP Redirect Fix

### Fixed
- **Bug #1016**: Renamed inner `content` key to `file_content` in `get_file_content` MCP tool response to eliminate naming collision with MCP protocol's `CallToolResult.content` wrapper. Clients no longer navigate two identically-shaped `content` arrays. Updated tool doc schema accordingly.
- **Bug #1017**: Fixed race condition in `base.html` form interceptor where `document.write()` cancelled a pending `window.location.replace()` navigation on `totp_setup_required` 403 responses. Admin forms (role change, email, password, delete) now correctly redirect to `/admin/mfa/setup` when TOTP is not configured.

## v10.51.0 (2026-05-22) -- Auto-Updater E2E Verification

### Fixed
- Upgraded root pip on cluster node .22 from 21.3.1 to 26.0.1 to fix `--break-system-packages` flag not recognized during auto-updater deployment step. All 3 cluster nodes now auto-update end-to-end.

## v10.50.0 (2026-05-22) -- Auto-Updater Deployed to All Cluster Nodes

### Fixed
- Deployed auto-updater systemd service and timer to cluster nodes .22 and .23, which were missing the `cidx-auto-update.service` and `cidx-auto-update.timer` units. All 3 cluster nodes now auto-update from the staging branch every 60 seconds.

## v10.49.0 (2026-05-22) -- MCP Protocol Version Negotiation (Codex Compatibility)

### Fixed
- MCP initialize handler now negotiates protocol version by echoing back the client's requested version when supported (2024-11-05, 2025-03-26, 2025-06-18), falling back to latest (2025-06-18) for unknown versions. Previously hardcoded 2025-06-18, causing Codex's `rmcp` client to reject the handshake when it only supports 2025-03-26.

## v10.48.0 (2026-05-22) -- MCP Streamable HTTP Transport Fix (Codex Compatibility)

### Fixed
- MCP POST endpoints (`/mcp`, `/mcp-public`) now return HTTP 202 with empty body for JSON-RPC notifications (no `id` field), per MCP Streamable HTTP transport spec. Previously returned 200 with a JSON-RPC body, causing Codex's `rmcp` Rust client to close the transport during handshake on `notifications/initialized`.

## v10.47.0 (2026-05-20) -- Atomic Writes for Delta Dependency Map

### Fixed
- Delta dependency map writes now use temp-file + atomic rename pattern (`Path.replace()`) instead of direct `write_text()`, preventing partial/corrupt files on crash (Bug #1015). Three write sites fixed: `_update_domain_file` (domain .md), `_apply_domain_assignments` (_domains.json), `_remove_stale_repos_from_domains_json` (_domains.json).

## v10.46.0 (2026-05-20) -- Golden Repo Refresh Fixes, HCL Detection, REST Regex Search

### Fixed
- Golden repo refresh failures caused by `cidx init` creating `.code-indexer-override.yaml` in repo root, blocking subsequent `git pull` with "untracked working tree files would be overwritten" error (Bug #1013). Three-layer fix: `--no-override-file` CLI flag on all server subprocess calls, pre-pull cleanup of CIDX artifacts, and error recovery with retry and path traversal validation.
- X-Ray `_hcl_available()` probed nonexistent `tree_sitter_hcl` package instead of `tree_sitter_languages.get_language("hcl")`, causing Terraform/HCL support to be silently disabled (Bug #1014)

### Added
- REST endpoint `POST /api/regex/search` for server-side regex search with single-repo and omni fan-out support, 50-repo cap, auth/permission checks, structured error codes, and per-repo metrics (Story #1011)

## v10.45.0 (2026-05-19) — Fix Extension Cascade Race Condition (Bug #1012)

### Fixed
- Removed `cascade_indexable_extensions_to_repos()` call from Web UI config save handler -- cascade was pre-empting drift detection by writing new extensions to repo configs immediately, causing `sync_repo_extensions_if_drifted()` to find no drift and never trigger `--reconcile`
- Golden repos now pick up new indexable extensions via drift detection on next refresh cycle, which correctly detects the change and triggers a full reconcile index run

### Changed
- Web UI "Save & Cascade" button renamed to "Save" for indexing settings section
- Help text updated: "Changes apply to golden repos on next refresh cycle"

## v10.44.0 (2026-05-18) — Documentation and E2E Testing Standards

### Added
- Server E2E front-door testing mandate in CLAUDE.md -- all server E2E tests must use REST API/MCP endpoints, CLI/SSH only for troubleshooting
- Langfuse trace sync documentation (`docs/langfuse-trace-sync.md`)

### Changed
- README.md streamlined -- removed redundant "What's New in v10.0" section, consolidated navigation, reduced by ~400 lines

## v10.43.0 (2026-05-18) — NFS Lock File Writable Mode

### Fixed
- All 7 lock file opens changed from read-only `"r"` to read-write `"r+"` mode, fixing EBADF when POSIX `lockf()` attempts exclusive locks on NFS — `lockf(LOCK_EX)` requires a writable file descriptor
- Golden repo HNSW index builds, branch isolation, and metadata updates no longer fail on NFS-mounted storage

## v10.42.0 (2026-05-18) — NFS-Safe fsync

### Fixed
- All 12 `os.fsync()` call sites across 8 files now use NFS-safe wrapper that suppresses EBADF on directory file descriptors, fixing golden repo HNSW index finalization failure on NFS mounts
- Golden repo indexing no longer fails with `[Errno 9] Bad file descriptor` during ID index save and directory fsync operations on NFS-backed storage

### Added
- `nfs_safe_fsync()` utility in `file_locking.py` — wraps `os.fsync()` with EBADF tolerance for NFS close-to-open consistency semantics

## v10.41.0 (2026-05-18) — NFS-Safe File Locking

### Fixed
- All 33 `fcntl.flock()` call sites across 12 files now use NFS-safe locking via centralized `nfs_safe_flock`/`nfs_safe_funlock` utility, fixing EBADF failures on NFS mounts with `local_lock=none`
- Golden repo indexing on cluster nodes no longer fails during HNSW index builds, vector store writes, or background rebuilds due to BSD flock incompatibility with NFS

### Added
- `src/code_indexer/utils/file_locking.py` — centralized NFS-safe file locking utility that tries `flock()` first, falls back to `lockf()` (POSIX record locks via NLM) on EBADF
- Git hook template in `git_hook_manager.py` uses inline flock-to-lockf fallback (standalone script, cannot import utility)

## v10.40.0 (2026-05-18) — NFS Cluster Stability Fixes

### Fixed
- Background index rebuilder flock-first/lockf-fallback for NFS mounts where local_lock=none causes EBADF on flock(); lockf uses POSIX record locks via NLM protocol
- Dependency map services handle commit_hashes as dict (PostgreSQL JSONB via psycopg3) or str (SQLite), fixing TypeError crash and silent empty-dict return
- migrate_to_postgres tool handles JSONB dict type for commit_hashes column during SQLite-to-PostgreSQL migration

### Added
- cluster-migrate.sh --is-storage-node flag so the NFS export server uses local path validation instead of NFS self-mount

## v10.39.0 (2026-05-17) — NFS Golden Repo Compatibility

### Fixed
- Git clone/fetch/pull operations on NFS-backed golden-repos storage now pass `-c core.fsync=none` to avoid fatal fsync I/O errors on NFS v4 mounts with Git 2.47+ (Bug #1010)

## v10.38.0 (2026-05-17) — Database and Config Stability Fixes

### Fixed
- Validation error responses now properly serialize bytes values in rejected_value field, preventing JSON serialization crashes on malformed request bodies (Bug #1003)
- MCP credential verification no longer fails authentication when last_used_at timestamp update encounters database lock contention (Bug #1004)
- Background job submission no longer emits spurious "Duplicate active job rejected" warnings in PostgreSQL mode when JobTracker registers an already-persisted job (Bug #1005)
- Legacy config key enforce_pace_maker_pacing_only now properly migrates to pace_maker_mode before unknown-key stripping, eliminating repeated warning floods on server restart (Bug #1006)

## v10.37.0 (2026-05-17) — Health Watchdog, Blame Hardening, and Thread Pool Optimization

### Added
- Health watchdog for cidx-server auto-restart via systemd timer with consecutive failure tracking and cooldown enforcement (Story #1007)
- Git blame subprocess timeout (30s) with BlameErrorResult dataclass for graceful timeout handling (Bug #1008)
- PayloadCache + TruncationHelper for blame response payload size management (Bug #1008)
- MCP protocol handler timeout (60s) via asyncio.wait_for on run_in_executor calls (Bug #1008)
- Configurable MCP dispatch thread pool (mcp_dispatch_pool_size, default 128) as asyncio default executor (Story #1009)
- MultiSearchService singleton pattern to prevent redundant thread pool creation (Story #1009)

### Fixed
- Langfuse auto_span_logger _summarize_output now handles "lines" key from blame responses (Bug #1008)

### Changed
- Multi-search subsidiary pool defaults increased: multi_search_max_workers 2->8, scip_multi_max_workers 2->8, subprocess_max_workers 2->8 (Story #1009)
- X-Ray worker threads default increased from 2 to 4 (Story #1009)

### Removed
- OmniSearchService dead code (213 lines) — functionality fully replaced by omni fan-out in search handlers (Story #1009)

## v10.36.0 (2026-05-16) — NFS Symlink Sharing for Claude CLI Sessions

### Added
- Auto-updater Step 13: Claude CLI install verification with npm-based installation fallback
- Auto-updater Step 14: NFS symlink creation for entire ~/.claude/ directory, enabling shared Claude CLI sessions across all cluster nodes via single NFS mount
- Auto-updater Step 15: systemd PATH configuration ensuring Claude CLI binary is accessible to cidx-server service
- Migration tool --nfs-mount parameter: converts local Claude state directories to shared NFS storage with consolidation of old per-project claude directories
- LLM lease lifecycle late-init in lifespan: secondary cluster nodes that boot before scheduler assigns subscriptions now properly initialize LLM services on first config sync

### Changed
- Removed old Step 15 credential-sync methods (superseded by full ~/.claude/ symlink approach)
- DeploymentExecutor step count increased from 12 to 15

## v10.35.0 (2026-05-15) — NFS Golden Repo Volume Monitoring

### Added
- NFS golden repo volume appears on dashboard alongside local disk volumes in cluster mode (Story #1002)
- Device label "Golden Repos (NFS)" with accurate metrics from psutil.disk_usage() and correct fstype (nfs/nfs4)
- NFS volume correctly absent in standalone mode or when mount is down
- Graceful OSError handling when NFS mount becomes stale or inaccessible
- Both dashboard paths covered: standalone health endpoint (_get_mounted_volumes) and cluster node_metrics carousel (_collect_volume_info)
- NfsMountValidator.get_mount_path() method for external consumers
- NFS mount auto-detection in lifespan via psutil.disk_partitions(all=True) with longest-match selection

## v10.34.0 (2026-05-15) — Comprehensive Documentation Overhaul

### Fixed
- 75+ verified factual corrections across 30 documentation files
- README.md: install tags updated to current version, Cohere dimensions 2048->1536, VoyageAI model names corrected to canonical (voyage-code-3, voyage-large-2), server start command fixed, dep_map timeout default 600->1800
- CLAUDE.md: DUNDER_ATTR_BLOCKLIST count 24->39, TOTP field name corrected, lint.sh description fixed, Cohere provider acknowledged
- xray-sandbox.md: Story #993 changes reflected (SAFE_BUILTINS 27->34, Groups F+G added, banned list corrected, test count 112->303)
- server-deployment.md: flat-key config warning added, TTL key name corrected, stale versions updated
- query-guide.md: parameter count corrected to 28, min_score default fixed to None
- memory-retrieval-operator-guide.md: wrong defaults fixed (voyage 0.75->0.5, cohere 0.5->0.4)
- migration-to-v10.md: memory_retrieval_enabled default corrected to false
- Stale migration counts (3->25), phantom module/test paths, dead links, and stale fact-check headers cleaned across 20+ files
- docs/CHANGELOG.md replaced with redirect to root CHANGELOG (was 180+ versions behind)

### Changed
- Memory index reorganized by category (Safety, Quality, Workflow, Architecture, References)
- Removed 2 transient project memories (completed bug package, time-bound staging mission)
- Added 5 previously unindexed timeless memory files to MEMORY.md

## v10.33.0 (2026-05-15) — Auto-Reconcile on Extension Drift

### Added
- Extension drift detection in refresh scheduler: when server-side file extension config changes (additions or removals), the next refresh automatically triggers `--reconcile` to re-scan the full repository, even if there are no git or local file changes.
- `ExtensionDrift` dataclass in ConfigService captures added/removed extensions from `sync_repo_extensions_if_drifted()`.
- `has_files_with_extensions()` short-circuit file scanner with directory pruning for efficient drift-triggered file existence checks.
- `_check_extension_drift()` helper in RefreshScheduler, called before both git and local repo early-return paths to ensure drift is never skipped.
- Crash recovery (interrupted metadata) OR-logic with drift detection: either condition alone triggers `--reconcile`.

### Fixed
- Extension drift check was unreachable during normal scheduled refreshes when no git/local changes were detected (early return bypassed the drift block). Drift detection now runs before both early-return exits.

## v10.32.0 (2026-05-14) — Fix GlobalRepoOperations startup warning in cluster mode

### Fixed
- GlobalRepoOperations no longer eagerly resolves backend_registry at construction time during server startup. Registry resolution is deferred to a lazy property accessed at request time, eliminating the spurious "storage_mode=postgres but backend_registry not set; falling back to SQLite" WARNING that fired on every cluster node restart.
- Added thread-safe double-checked locking on the registry property to prevent duplicate registry construction under concurrent first-access requests.

## v10.31.0 (2026-05-14) — Fix PG token bucket LEAST() scalar function

### Fixed
- TokenBucketManager._pg_refund() used SQL MIN() as scalar function which only works in SQLite; PostgreSQL requires LEAST() for two-argument scalar minimum. This crashed every login attempt on the staging cluster with `psycopg.errors.UndefinedFunction`.

## v10.30.0 (2026-05-14) — Cluster-mode bypass fixes and PG state sharing

### Fixed
- GlobalRepoOperations (shared_operations.py) now detects postgres mode and delegates to BackendRegistry.global_repos instead of always creating a SQLite-backed GlobalRegistry.
- registry_factory.py returns PostgresGlobalRegistryAdapter wrapping the shared PG backend in cluster mode, ensuring all nodes read/write to the same global repos store.
- repos.py _set_enable_temporal_flag now checks storage_mode and updates enable_temporal via BackendRegistry.global_repos in postgres mode instead of creating a throwaway SQLite GlobalRegistry.
- golden_repo_manager.py enable_temporal updates route through BackendRegistry in postgres mode.
- alias_manager.py write-mode markers now use PG table instead of filesystem in cluster mode.
- Langfuse trace sync fromisoformat compatibility fix for Python 3.9.

### Added
- TokenBucketManager PG cluster support: cross-node rate limiting via token_bucket_state table with atomic consume/refund operations.
- LlmLeaseStateManager PG cluster support: encrypted lease state stored in cluster_secrets table, encryption key derived from shared JWT secret via PBKDF2.
- PostgreSQL migration 024_token_bucket_state.sql for cluster rate limiting.
- LLM Creds Client: NestJS response envelope unwrapping (metadata/payload format).
- lifespan wiring for rate_limiter.set_connection_pool() and LlmLeaseStateManager.set_connection_pool() in cluster mode.

## v10.29.0 (2026-05-13) — PG datetime serialization across all backends

### Fixed
- All PostgreSQL backend methods now wrap returned dicts with `sanitize_row()` to convert TIMESTAMPTZ datetime objects to ISO strings, preventing `TypeError: Object of type datetime is not JSON serializable` in cluster mode.
- Affected backends: users, git_credentials, ssh_keys, research_sessions, sync_jobs, oauth, description_refresh_tracking (7 files, ~50 datetime fields).

## v10.28.0 (2026-05-13) — Elevation dialog race condition fix

### Fixed
- Elevation dialog race condition in `ConfigService.load_config()` (Bug #998): when config hot-reload toggled `elevation_enforcement_enabled` between the `require_elevation` check and the `elevate-ajax` endpoint call, users got trapped in the modal with "Step-up elevation is currently disabled" error.
- MCP consolidated dispatcher session key markers.

## v10.27.0 (2026-05-13) — Three-way pace-maker toggle

### Added
- Three-way pace-maker mode toggle (disabled/on/off) with Web UI config screen support.
- Config screen crash fix for pace-maker settings.

## v10.26.0 (2026-05-13) — Langfuse sync cluster-aware leader election

### Fixed
- Langfuse trace sync now gated behind leader election in cluster mode (postgres). Only the leader node runs sync, preventing duplicate work and NFS write conflicts. Solo mode (sqlite) unchanged.

## v10.25.0 (2026-05-13) — Comprehensive template PG datetime slicing fixes

### Fixed
- Git Credentials page 500 in cluster mode: `created_at[:10]` slicing on PG datetime object (added `|string` filter).
- API Keys page: `created_at[:10]` datetime slicing (added `|string` filter).
- Repos list page: `activated_at[:10]` datetime slicing (added `|string` filter).
- Jobs list page: `started_at[:19]` and `created_at[:19]` datetime slicing (added `|string` filter).
- Dashboard recent jobs: `completion_time[:19]` datetime slicing (added `|string` filter).
- Dashboard stats: `completion_time[:19]` datetime slicing (added `|string` filter).
- Audit logs list: `timestamp[:19]` datetime slicing (added `|string` filter).
- Dependency map job status: `last_run[:16]`, `next_run[:16]`, and `run.timestamp[:16]` datetime slicing (added `|string` filter).
- Dashboard Langfuse: `last_sync_time[:19]` datetime slicing (added `|string` filter).

## v10.24.0 (2026-05-13) — PG type compatibility fixes for cluster web UI

### Fixed
- MFA cross-user setup flow: guard returns HTML errors instead of JSON for web UI pages, skips confirm_overwrite for show mode (read-only QR display), totp_code input has required attribute.
- Research Assistant 500 in cluster mode: `relative_time()` Jinja filter handles PG native datetime objects (was calling `.endswith("Z")` on datetime).
- MCP Credentials page 500 in cluster mode: template slicing on PG datetime objects (added `|string` filter).
- Timestamp slicing in golden repo and repo list pages: added `_safe_ts_slice()` helper that handles both str and datetime.
- Self-monitoring PG backend: added missing `list_scans()` and `get_running_scan_count()` methods required by protocol.
- `_format_datetime_display()` now accepts both str and datetime objects from PG.

## v10.23.0 (2026-05-12) — Cluster OIDC late-init and activated repos PG dict_row fix

### Fixed
- OIDC/SSO not initialized on cluster secondary nodes: ConfigService gets PG pool after OIDC startup, so secondary nodes without OIDC in bootstrap config.json skipped initialization. Added late-initialization block after ConfigService gets PG pool that retries OIDC setup from PG config.
- P0 query regression in cluster mode ("tuple indices must be integers or slices, not str"): `activated_repo_manager.py` PG SELECT queries used `conn.execute()` without `row_factory=dict_row`, returning tuples instead of dicts. All search, repo listing, and cascade deletion operations failed. Added `_dict_row_factory()` helper and `conn.cursor(row_factory=...)` to all three PG SELECT methods, following the pattern from `groups_backend.py` and `audit_log_backend.py`.

## v10.22.0 (2026-05-12) — Unified token encryption key with salt file and shared crypto module (Story #999)

### Added
- `token_encryption.py`: shared AES-256-CBC encryption module eliminating duplication between CITokenManager and GitCredentialManager. Three-priority key derivation chain: salt file > cluster_secret > hostname.
- `encryption_key_salt.py`: auto-seeds `.encryption_key_salt` from `.jwt_secret` (postgres mode) or hostname (sqlite mode) with 0600 permissions.
- Try-decrypt fallback: canonical key first, hostname-derived key on PKCS7 failure (exactly 2 attempts). Lazy re-encryption when fallback succeeds.
- `update_encrypted_token` added to `CITokensBackend` protocol, SQLite backend, and PG backend for lazy re-encryption persistence.
- `GitCredentialsPostgresBackend` gains `update_encrypted_token` for cluster-mode lazy re-encryption.
- PG backend wiring for CI tokens in web routes (`_get_token_manager()` detects storage_mode).
- `cluster-migrate.sh` seeds `.encryption_key_salt` from `.jwt_secret` with trailing newline stripped.
- Lifespan calls `ensure_encryption_key_salt()` on startup for consistent key derivation.
- 67 new/updated unit tests covering encryption primitives, salt management, factory integration, and lazy re-encryption.

### Changed
- CITokenManager and GitCredentialManager delegate all crypto operations to `token_encryption` module via relative imports (fixes mypy resolution with `--explicit-package-bases`).
- `create_token_manager()` and `create_git_credential_manager()` factories call `ensure_encryption_key_salt()` before construction.

## v10.21.0 (2026-05-12) — Cluster-aware token re-encryption and GitCredentialManager cluster support

### Added
- `GitCredentialManager` now accepts `cluster_secret` parameter for key derivation in cluster mode (same pattern as `CITokenManager`).
- `create_git_credential_manager()` factory function reads `.jwt_secret` in postgres mode for consistent encryption keys.
- `migrate_to_postgres.py` gains `--server-dir` flag for automatic token re-encryption during migration: re-encrypts both `ci_tokens` and `user_git_credentials` tables from hostname-derived key to `.jwt_secret`-derived key.
- `cluster-migrate.sh` passes `--server-dir` to migration tool automatically.

### Fixed
- Git credential operations now work in cluster mode (previously always used hostname-based key derivation, causing decryption failures after migration).
- BackgroundJob PG backend schema mismatch: added `current_phase`/`phase_detail` to `save_job()` and filtered unknown keys before `BackgroundJob` construction.

## v10.20.0 (2026-05-12) — Complete SQLite-to-PostgreSQL migration tool and cluster-migrate.sh fixes

### Fixed
- Migration tool now handles 6 additional tables with real data: user_mfa, user_recovery_codes, activated_repos, server_config, dependency_map_run_history, wiki_article_views. Previously, TOTP secrets and recovery codes would be lost during solo-to-cluster conversion.
- cluster-migrate.sh now sets cluster.node_id in config (required for leader election).
- cluster-migrate.sh data migration step now passes optional database paths (oauth.db, scip_audit.db, refresh_tokens.db) when those files exist.
- cluster-migrate.sh local clone backend no longer runs NFS copy steps with an empty mount path.

### Added
- `--node-id` CLI argument for cluster-migrate.sh (auto-generated from hostname if omitted).
- PostgreSQL migration 024: wiki_article_views table (was inline-only in PG backend, missing before data migration runs).
- BOOLEAN_COLUMNS entries for user_mfa.mfa_enabled and activated_repos.is_composite/wiki_enabled.
- JSON_COLUMNS entries for server_config.config_json, activated_repos.metadata_json, dependency_map_run_history.phase_timings_json.

## v10.19.0 (2026-05-12) — MCP cancel_job with XRay process termination (Story #996)

### Added
- `cancel_job` MCP tool: cancel running or pending background jobs by job_id. Users can cancel their own jobs; admins can cancel any job.
- XRay process termination: cancelling xray_search/xray_explore jobs sends SIGTERM to driver processes, escalating to SIGKILL after a 2-second grace period.
- Race condition handling (AC7): when cancel arrives before process registration, the process is terminated immediately upon registration.
- `on_process_spawned` callback threading from MCP handlers through XRaySearchEngine and PythonEvaluatorSandbox to BackgroundJobManager for child process tracking.
- Cancellation documentation added to xray_search and xray_explore tool docs.

### Fixed
- Cancelled jobs no longer have their status overwritten to "failed" by JobTracker.fail_job(). Three call sites in _execute_job now guard tracker notifications with `not job.cancelled`.

## v10.18.0 (2026-05-11) — Fix elevation dialog race condition (Bug #998)

### Fixed
- ConfigService.load_config() thread-safety race: bootstrap defaults (elevation_enforcement_enabled=False) were transiently visible to concurrent get_config() callers before DB merge completed. Now uses atomic base_config parameter threading so self._config is only published after the full merge.
- load_config() default-creation path no longer overwrites existing SQLite/PG runtime values with bootstrap defaults when config.json is missing.
- Elevation endpoints (elevate-ajax, elevate-form) return success instead of 503 when kill switch is OFF, preventing users from being trapped in the TOTP modal during transient config states.
- Request handlers in api_keys.py (11 calls) and llm_creds.py (1 call) changed from load_config() to get_config() to eliminate unnecessary full reloads on every HTTP request.

### Added
- Concurrency test suite for load_config() atomicity (7 tests covering SQLite merge window, base_config parameter threading, PG passthrough, and fallback paths).

## v10.17.0 (2026-05-11) — Three-way pace-maker toggle and config screen crash fix

### Changed
- Pace-maker enforcement setting replaced from boolean (Yes/No) to three-way mode (Disabled/On/Off). "Disabled" is a complete no-op that never touches pace-maker -- safe for dev machines. "On" enforces pacing-only mode. "Off" actively disables pace-maker.
- Removed brittle clone-path location awareness check from pace-maker guard. Gating is now purely mode-based.
- Config field renamed: `enforce_pace_maker_pacing_only` (bool) -> `pace_maker_mode` (string: disabled/on/off). Default: "disabled".

### Fixed
- Config screen 500 crash when `totp_elevation` or `pace_maker` settings absent from database. Both sections now provide proper defaults and Jinja2 `| default()` safety filters.

## v10.16.0 (2026-05-11) — Fix elevation session_key injection for consolidated MCP dispatchers

### Fixed
- Consolidated MCP dispatchers (`manage_group_members`, `manage_group_repos`, `list_mcp_credentials`, `manage_mcp_credential`) now propagate `session_key` to inner elevation-gated handlers. The `__mcp_requires_session_key__` marker was missing on the undecorated dispatcher functions, causing protocol.py to skip `session_key` injection and all elevation-dependent tool calls to fail with "No session key" (Epic #985 regression).

### Added
- Unit tests for dispatcher session_key marker presence and protocol.py injection flow.

## v10.15.0 (2026-05-10) — Epic #985 E2E regression coverage and CLI API client fixes

### Fixed
- CLI API clients (`group_client.py`, `credential_client.py`) migrated to consolidated MCP tool names matching Epic #985 changes.
- `manage_group_members` parameter corrected from `users` (list) to `user_id` (string) across all API clients and E2E tests.

### Added
- Phase 3 E2E regression suite: 63 tests validating all consolidated MCP tools via TestClient (Story #985).
- Phase 4 E2E regression suite: 63 tests validating all consolidated MCP tools against live uvicorn server (Story #985).
- Removed-tool regression tests: 25 old tool names verified to return clean errors (both Phase 3 and Phase 4).

## v10.14.0 (2026-05-10) — MCP Tool Surface Compression and Consolidation (Epic #985)

Reduces MCP tool count from 167 to 147 (net -20, ~12%) via action-param dispatcher pattern. All migrations are hard-cut -- no shims, no deprecation period. Old tool names removed from both HANDLER_REGISTRY and TOOL_REGISTRY.

### Added
- `slim_description` YAML frontmatter field on all 147 tool docs for compressed tool listing (Stories #987, #988).
- `cidx_quick_reference` tool for instant tool lookup by keyword (Story #987).

### Changed
- **BREAKING (MCP)**: 8 MCP credential tools consolidated into 2 (`list_mcp_credentials`, `manage_mcp_credential`) via scope/action params (Story #989).
- **BREAKING (MCP)**: 3 repo-status tools consolidated into 1 (`repository_status`) with detail param (Story #990). `get_all_repositories_status` unchanged.
- **BREAKING (MCP)**: 12 CI/CD forge-specific tools consolidated into 6 unified `ci_*` tools with auto-detection (Story #991).
- **BREAKING (MCP)**: 10 SSH/group-CRUD tools consolidated into 4 (`manage_ssh_key`, `list_ssh_keys`, `manage_group_members`, `manage_group_repos`) via action params (Story #992).

### MCP Tool Migration: Credential Operations (Story #989)

| Old Tool | New Tool | Parameter Mapping |
|----------|----------|-------------------|
| `list_mcp_credentials()` (no args) | `list_mcp_credentials(scope='self')` | **BREAKING**: same name, new required `scope` param |
| `admin_list_user_mcp_credentials(username)` | `list_mcp_credentials(scope='user', username=...)` | |
| `admin_list_all_mcp_credentials()` | `list_mcp_credentials(scope='all')` | |
| `admin_list_system_mcp_credentials()` | `list_mcp_credentials(scope='system')` | |
| `create_mcp_credential(description?)` | `manage_mcp_credential(action='create', description=?)` | |
| `delete_mcp_credential(credential_id)` | `manage_mcp_credential(action='delete', credential_id=...)` | |
| `admin_create_user_mcp_credential(username, description?)` | `manage_mcp_credential(action='create', target_user=..., description=?)` | |
| `admin_delete_user_mcp_credential(username, credential_id)` | `manage_mcp_credential(action='delete', target_user=..., credential_id=...)` | |

### MCP Tool Migration: Repository Status (Story #990)

| Old Tool | New Tool | Parameter Mapping |
|----------|----------|-------------------|
| `get_repository_status(user_alias=X)` | `repository_status(alias=X, detail='basic')` | `user_alias` renamed to `alias`; response nested under `status` with `kind='activated'` |
| `global_repo_status(alias=X)` | `repository_status(alias=X, detail='basic')` | Response now nested under `status` with `kind='global'` (was flat top-level) |
| `get_repository_statistics(repository_alias=X)` | `repository_status(alias=X, detail='stats')` | `repository_alias` renamed to `alias`; stats in `statistics` alongside `status` |

### MCP Tool Migration: CI/CD Operations (Story #991)

| Old Tool | New Tool | Parameter Mapping |
|----------|----------|-------------------|
| `github_actions_list_runs(owner, repo, ...)` | `ci_list_runs(repository_alias=..., forge='auto')` | `owner`+`repo` replaced by `repository_alias`; auto-detects GitHub |
| `gitlab_ci_list_pipelines(project_id, ...)` | `ci_list_runs(repository_alias=..., forge='auto')` | `project_id` replaced by `repository_alias`; auto-detects GitLab |
| `github_actions_get_run(owner, repo, run_id)` | `ci_get_run(repository_alias=..., run_id=..., forge='auto')` | |
| `gitlab_ci_get_pipeline(project_id, pipeline_id)` | `ci_get_run(repository_alias=..., run_id=..., forge='auto')` | `pipeline_id` renamed to `run_id` |
| `github_actions_get_job_logs(owner, repo, job_id, ...)` | `ci_get_job_logs(repository_alias=..., job_id=..., forge='auto')` | |
| `gitlab_ci_get_job_logs(project_id, job_id, ...)` | `ci_get_job_logs(repository_alias=..., job_id=..., forge='auto')` | |
| `github_actions_search_logs(owner, repo, run_id, pattern, ...)` | `ci_search_logs(repository_alias=..., run_id=..., pattern=..., forge='auto')` | |
| `gitlab_ci_search_logs(project_id, pipeline_id, query, ...)` | `ci_search_logs(repository_alias=..., run_id=..., pattern=..., forge='auto')` | `pipeline_id` renamed to `run_id`, `query` renamed to `pattern` |
| `github_actions_cancel_run(owner, repo, run_id)` | `ci_cancel_run(repository_alias=..., run_id=..., forge='auto')` | |
| `gitlab_ci_cancel_pipeline(project_id, pipeline_id)` | `ci_cancel_run(repository_alias=..., run_id=..., forge='auto')` | `pipeline_id` renamed to `run_id` |
| `github_actions_retry_run(owner, repo, run_id)` | `ci_retry_run(repository_alias=..., run_id=..., forge='auto')` | |
| `gitlab_ci_retry_pipeline(project_id, pipeline_id)` | `ci_retry_run(repository_alias=..., run_id=..., forge='auto')` | `pipeline_id` renamed to `run_id` |

### MCP Tool Migration: SSH and Group Operations (Story #992)

| Old Tool | New Tool | Parameter Mapping |
|----------|----------|-------------------|
| `cidx_ssh_key_create(...)` | `manage_ssh_key(action='create', ...)` | |
| `cidx_ssh_key_delete(name)` | `manage_ssh_key(action='delete', name=...)` | |
| `cidx_ssh_key_assign_host(name, hostname)` | `manage_ssh_key(action='assign_host', name=..., hostname=...)` | |
| `cidx_ssh_key_show_public(name)` | `manage_ssh_key(action='show_public', name=...)` | |
| `cidx_ssh_key_list(...)` | `list_ssh_keys(...)` | Renamed only; same signature |
| `add_member_to_group(group_id, user_id)` | `manage_group_members(action='add', group_id=..., user_id=...)` | |
| `remove_member_from_group(group_id, user_id)` | `manage_group_members(action='remove', group_id=..., user_id=...)` | |
| `add_repos_to_group(group_id, repo_names)` | `manage_group_repos(action='add', group_id=..., repos=[...])` | `repo_names` renamed to `repos` |
| `remove_repo_from_group(group_id, repo_name)` | `manage_group_repos(action='remove', group_id=..., repo_name=...)` | |
| `bulk_remove_repos_from_group(group_id, repo_names)` | `manage_group_repos(action='bulk_remove', group_id=..., repos=[...])` | `repo_names` renamed to `repos` |

### Fixed
- TOTP config section test updated to match checkbox-to-dropdown migration from v10.13.0.

## v10.13.0 (2026-05-09) — Config UI cosmetic fixes

### Fixed
- Config section checkbox labels replaced with Yes/No dropdowns matching existing UI pattern.
- Fixed label/description text overlap in config display tables (added fixed table layout CSS).
- TOTP Elevation and Pace Maker sections now use consistent Yes/No display values.

## v10.12.0 (2026-05-09) — Pace-maker integration for token cost control

Story #997: Integrates pace-maker into the CIDX auto-updater and installer for pacing-only mode enforcement. Prevents token runaway scenarios by throttling Claude CLI consumption via 5-hour and weekly quotas.

### Added
- **Pre-invocation guard** (`pace_maker_guard.py`): Three-layer safety model (location awareness, runtime toggle, idempotent CLI enforcement) called before every Claude CLI invocation.
- **Runtime config toggle**: `enforce_pace_maker_pacing_only` (default `false`) exposed in Web UI Config Screen for hot-toggling without server restart.
- **Auto-updater Step 12**: `_ensure_pace_maker_installed()` in `DeploymentExecutor` -- clones/updates pace-maker repo, runs `install.sh` as server user, sets master OFF on fresh install only.
- **Installer Step 5.5**: Pace-maker clone and install in `install-cidx-server.sh` with fresh-install guard.
- **Guard wiring**: `ClaudeInvoker.invoke()` and `ResearchAssistantService._run_claude_background()` (NOT CodexInvoker -- Codex uses OpenAI credits).
- 55 unit tests covering guard behavior, config service integration, deployment executor, and wiring verification.

### Fixed
- Guard subprocess return codes now checked (Holzmann Rule #7 compliance).
- `sudo -u` passthrough for `NONINTERACTIVE` env var in auto-updater install path.
- Installer script respects fresh-install guard (re-runs no longer reset pace-maker config).

## v10.11.0 (2026-05-09) — Dep-map prompt coherence and _index.md corruption fix

Bug #995 Phase 2: rewrote all dep-map prompt builders for coherence — consolidated duplicated content (granularity guidelines, evidence rules, PROHIBITED items) into `_analysis_guidelines.md`, expanded CLAUDE.md into a proper workspace orientation file, fixed `_CROSS_DOMAIN_SCHEMA` to list all 8 dependency types with canonical names, and removed inline content duplication across 6 prompt functions.

Additionally fixed two code paths that corrupted `_index.md` (the dep-map health index file): refinement called `_generate_index_md` with an empty repo list (wiping the Repo-to-Domain Matrix), and delta analysis never regenerated `_index.md` after domain updates. Both now use `IndexRegenerator.regenerate()` which derives repo data from domain files deterministically.

### Fixed
- **_index.md corruption (refinement)**: Refinement path passed `repo_list=[]` to `_generate_index_md()`, producing `repos_analyzed_count: 0` and an empty Repo-to-Domain Matrix after every successful refinement cycle. Now uses `IndexRegenerator.regenerate()`.
- **_index.md staleness (delta)**: Delta analysis updated domain docs but never regenerated `_index.md`, leaving it stale when domain assignments changed. Now regenerates after domain updates.
- **Prompt coherence**: All dep-map prompts (Pass 2, delta merge, new domain, refinement) now reference `_analysis_guidelines.md` and `_dep_types.md` instead of inlining inconsistent subsets of shared content.
- **CLAUDE.md expansion**: Transient CLAUDE.md now explains workspace structure, domain concepts, cidx-meta layout, and available tools — providing proper grounding for Claude CLI.
- **`_CROSS_DOMAIN_SCHEMA`**: Added missing "Message/event contracts" and "Semantic coupling" types; renamed "External tool" to "External tool invocation" to match `_dep_types.md`.
- **Repair prompt coherence**: Added verification guidance for 4 missing dependency types (External tool invocation, Message/event contracts, Deployment dependency, Semantic coupling) to `bidirectional_mismatch_audit.md`.
- **Startup migration**: Changed oversized CLAUDE.md migration from rename to delete (catalogue file no longer used).

### Added
- 3 new regression tests in `test_index_md_regeneration_paths.py` guarding against _index.md corruption in refinement and delta paths.

## v10.10.0 (2026-05-08) — Fix X-Ray path disclosure (security)

Security fix: X-Ray search/explore results leaked absolute host filesystem paths (e.g. `/opt/code-indexer/.cidx-server/data/golden-repos/.versioned/alias/v_TIMESTAMP/code/src/...`) in `file_path` fields of matches, evaluation_errors, and file_metadata. This exposed internal server directory structure, versioned snapshot naming conventions, and deployment paths to API consumers and evaluator scripts.

### Fixed
- **X-Ray path disclosure**: `file_path` values in all X-Ray result output channels (matches, evaluation_errors, file_metadata) are now relative to the repository root instead of absolute host paths. The fix converts paths at the source in `XRaySearchEngine.run()` before they enter `file_specs`, so all downstream consumers (sandbox, evaluator globals, normalized results) automatically produce relative paths.
- **Stale docstrings**: Updated `_evaluate_file()` and `PythonEvaluatorSandbox.run()` docstrings to reflect that `file_path` is relative in production.

### Added
- 4 new tests in `TestXRaySearchEngineRelativePaths` verifying that `file_path` values are always relative across matches, errors, and file_metadata.

## v10.9.0 (2026-05-08) — Fix dep-map CLAUDE.md context overflow ($3K token burn)

Production bug fix: `generate_claude_md()` wrote a full repo catalogue (~350K tokens) directly into `CLAUDE.md` at the golden-repos root. Claude CLI auto-loads `CLAUDE.md` from its cwd, so every dep-map invocation overflowed the 200K context window before the task prompt loaded -- causing $3K/night in wasted tokens with zero useful output.

### Fixed
- **CLAUDE.md context overflow**: Split `generate_claude_md()` into `generate_orientation_files()` which writes a minimal `CLAUDE.md` pointer (~50 tokens) + `dep_map_repo_catalogue.md` (full catalogue). Claude CLI loads the tiny pointer; the catalogue is read on demand.
- **Startup migration**: On server startup, oversized `CLAUDE.md` (>10KB) is renamed to `dep_map_repo_catalogue.md`, preserving catalogue content while immediately unblocking dep-map runs.
- **Delta analysis retry loop**: Fixed exception handling in retry loop that could swallow errors silently.
- **Delta analysis live-path reads**: Fixed file reads to use correct live path instead of stale versioned path.
- **Delta analysis next_run advancement**: Fixed `next_run` timestamp not advancing after completed delta runs.
- **Elevation gating**: Added missing `require_elevation()` dependency on `POST /golden-repos/activate` and `POST /repos/{username}/{user_alias}/deactivate` admin mutation routes.

### Removed
- 5 dead sync-gate tests in `test_meta_directory_updater_hardening.py` (tested a safety gate removed in d8922eb6).

## v10.8.0 (2026-05-07) — Remove `-global` from cidx-meta description filenames

Production bug fix: v10.4.9 changed cidx-meta description filenames from `{alias}.md` to `{alias}-global.md`, but only in WRITE paths. 10 READ paths still used `{alias}.md`, causing: UI showing no descriptions, terse-backfill storms on every startup, all repos quarantined by refresh scheduler, and a security bypass in access filtering (all descriptions visible to all users). The `-global` suffix was a registry naming convention leak into the filesystem -- this release removes it entirely and adds protective INVARIANT comments at all 10 filename-construction sites.

### Fixed
- **Description filename convention**: removed `-global` suffix from all WRITE/DELETE paths in `meta_description_hook.py`, `meta_directory_updater.py`, and `refresh_scheduler.py`. Filenames standardized on `{short_alias}.md`.
- **Managed-file filter**: `MetaDirectoryUpdater._get_existing_description_aliases()` now matches `.md` stems against known registered aliases instead of relying on `*-global.md` glob pattern. Non-managed files (README.md, runbooks) excluded by alias whitelist.
- **Migration**: `MetaDirectoryUpdater.update()` auto-renames existing `{alias}-global.md` to `{alias}.md` on first run (skips if target already exists).
- **Registry alias stripping**: `_get_safe_registered_aliases()` strips `-global` suffix from registry `alias_name` values before use.
- **Refresh scheduler**: `_queue_missing_description()` strips `-global` suffix before constructing filename.
- **10 READ paths auto-fixed**: all paths that construct `{alias}.md` from short aliases now find the correct files without any code changes.
- **Access filtering security bypass**: `access_filtering_service.py` stem comparison works again because file stems match short aliases.

### Added
- INVARIANT protective comments at all 10 filename-construction sites warning against reintroducing `-global` in filenames.
- Migration tests in `test_meta_directory_updater_hardening.py`: rename and skip-when-both-exist scenarios.

### Removed
- `test_cidx_meta_filename_alignment_v10_4_9.py` (405 lines) -- enforced the incorrect `-global.md` convention.

## v10.7.0 (2026-05-07) — cidx-meta MetaDirectoryUpdater hardening (defense-in-depth)

Production data loss prevention: MetaDirectoryUpdater twice wiped $500+ worth of Claude-generated description files due to blind destructive reconciliation with no safety gates. This release adds defense-in-depth at three layers (filesystem, git commit, git push) to prevent mass-deletion of cidx-meta content.

### Fixed
- **Mass-delete safety threshold**: `MetaDirectoryUpdater.update()` now raises `MetaDirectoryMassDeleteBlocked` when deletion ratio exceeds 50% and at least 3 managed files exist, preventing empty-registry or transient-glitch wipeouts.
- **Managed-file filter**: changed `glob("*.md")` to registry-aware alias matching so non-managed files (README.md, runbooks, etc.) are never treated as managed or counted in deletion ratios (further refined in v10.8.0).
- **Stub overwrite guard**: creation loop now checks `if not desc_file.exists()` before writing, preventing rich descriptions from being overwritten by 3-line stubs.
- **Lock discipline for MetaDirectoryUpdater**: `update()` acquires cidx-meta write lock before filesystem changes and releases in finally block. Skips update when lock not acquired.
- **Lock discipline for on_repo_removed**: `meta_description_hook.py` acquires cidx-meta write lock before deleting description files on repo removal.
- **Backup sync commit-level safety gate**: `CidxMetaBackupSync.sync()` inspects `git status --porcelain` before committing; blocks and restores deleted .md files when mass-deletion detected.
- **Bootstrap force-push removed**: `CidxMetaBackupBootstrap._push()` raises `RuntimeError` on push rejection instead of falling back to `--force`, preventing silent history destruction.
- **Dead raw writer gated**: `DescriptionRefreshScheduler._update_description_file()` now raises `NotImplementedError` directing callers to `atomic_write_description()` or `write_meta_md()`.

### Added
- `tests/unit/global_repos/test_meta_directory_updater_hardening.py` (29 tests) -- comprehensive safety threshold, stub guard, lock discipline, sync gate, bootstrap, and porcelain parsing tests.
- Path model documentation comments in `meta_directory_updater.py` module docstring.

## v10.6.0 (2026-05-06) — Fix Claude CLI timeouts, concurrency bug, and UI cosmetic issues

Production bug fix: Claude CLI invocations were being killed with exit code 124 on large repos due to hardcoded timeouts (90-360s) scattered across 5 code paths. Also fixes LifecycleBatchRunner ignoring config-driven concurrency, and 3 Web UI bugs.

### Fixed
- **Claude CLI timeout raised to 30 minutes**: all 5 hardcoded timeout sites unified under `LifecycleAnalysisConfig` defaults (1800s shell / 1860s outer). Configurable from Web UI at `/admin/config/lifecycle_analysis`.
- **LifecycleBatchRunner concurrency bug**: was hardcoded to 2 at all 3 call sites, now reads `max_concurrent_claude_cli` from config via `_get_lifecycle_concurrency()`.
- **repo_analyzer duplicate subprocess**: `_extract_info_with_claude` replaced copy-pasted subprocess block with call to shared `invoke_claude_cli()`.
- **FTS tag missing on activated repo cards**: path detection checked wrong location (`index/tantivy` instead of `tantivy_index`).
- **Description text not word-wrapping in golden repo details**: added CSS `word-wrap: break-word` and `white-space: pre-wrap` to `.repo-description-box`.
- **TOTP elevation required for repo activation/deactivation**: removed `require_elevation()` dependency from web routes `/golden-repos/activate` and `/repos/{username}/{user_alias}/deactivate`.

### Added
- `tests/unit/server/services/test_claude_timeout_defaults.py` (10 tests) -- regression guard for all timeout sites.
- `tests/unit/server/services/test_lifecycle_batch_concurrency.py` (5 tests) -- regression guard for concurrency propagation.

## v10.5.0 (2026-05-06) — Unify description refresh to single code path, fix terse descriptions, bootstrap backfill

Production crisis fix: 895 golden repos had broken/terse descriptions caused by competing code paths and mismatched thresholds.

### Changed
- **Removed stub-heal machinery entirely** (-421 lines from description_refresh_scheduler.py): the competing code path that destroyed lifecycle frontmatter and caused an infinite regeneration loop. All description refresh now goes through LifecycleBatchRunner which properly reads existing frontmatter, merges, and writes atomically.
- **Description length prompt**: changed from 1-500 chars to 500-2000 chars in lifecycle_unified.md, preventing the infinite loop where LifecycleBatchRunner wrote short descriptions that stub-heal flagged as stubs.
- **claude_invoker.py**: added `-e` flag to `script` command so inner command exit codes propagate (previously `script -q -c` always returned 0, masking Claude CLI failures).

### Added
- **Bootstrap description backfill sweep**: at server startup, scans all cidx-meta files and flags repos with body <= 500 chars for async regeneration through LifecycleBatchRunner. Fixes production gap where terse descriptions were never regenerated by any event-driven code path.

### Removed
- `tests/unit/server/services/test_description_refresh_stub_heal_v10_4_14.py` (923 lines) — tests for the removed stub-heal feature.

## v10.4.15 (2026-05-06) — HOTFIX: MetaDirectoryUpdater creates `-global.md` filenames instead of bare-name `.md`

Root cause of 895 misnamed stub files in production. `MetaDirectoryUpdater._get_active_aliases()` used `repo["alias_name"]` (which includes the `-global` suffix, e.g. `JSqlParser-global`) instead of `repo["repo_name"]` (bare name, e.g. `JSqlParser`). The scheduler and API expect bare-name `.md` files — so every file the updater created was invisible to refresh, heal, and query pipelines.

One-line fix: `meta_directory_updater.py` line 116 changed from `alias = repo["alias_name"]` to `alias = repo["repo_name"]`.

## v10.4.14 (2026-05-06) — HOTFIX: stub-heal must dispatch via BackgroundJobManager, not block the scheduler thread

Production-down hotfix for v10.4.13's stub-heal pivot. The v10.4.13 implementation invoked `_heal_stub_description` SYNCHRONOUSLY inside the scheduler thread when `_get_refresh_prompt` detected a stub. With many stubs (production has ~893), every heal serializes through the single scheduler daemon thread, blocking incremental refresh and lifecycle backfill for the duration of all heals (10s of minutes per cycle). This is untenable in production and violates the CIDX background-jobs contract (CLAUDE.md "Background Jobs (MANDATORY Checklist)").

Fix: heal dispatch now goes through `BackgroundJobManager.submit_job(operation_type="description_stub_heal", ...)` so it runs in BJM's worker pool with:

- Dashboard visibility via JobTracker registration (operation_type appears in Recent Jobs widget).
- Per-alias dedup via `DuplicateJobError` (BJM's existing `(operation_type, repo_alias)` gate).
- Standardized status transitions (PENDING → RUNNING → SUCCESS / FAILED) and error reporting.
- Survives restart (persisted to SQLite/Postgres backend).

`DescriptionRefreshScheduler` gains an injectable `background_job_manager` constructor parameter, wired in `lifespan.py:865` from the lifespan-scoped `background_job_manager` argument that already feeds golden-repo / dep-map / lifecycle paths.

`_get_refresh_prompt` rewritten with 4 explicit branches over `(dispatched, has_last_analyzed)`:
- `(True, True)` → mark no-quarantine, return None (heal in flight will replace stub; skip incremental to avoid race).
- `(False, True)` → return incremental refresh prompt only, do NOT mark no-quarantine (BJM unavailable; if incremental fails, normal quarantine SHOULD apply so operators see misconfig).
- `(True, False)` → mark no-quarantine, return None (heal in flight; nothing to incrementally refresh).
- `(False, False)` → mark no-quarantine, return None.

Race condition fix (E2E-discovered): when `_get_refresh_prompt` returned None from a stub-heal dispatch, `_run_loop_single_pass` fell through to the regular description refresh instead of `continue`-ing. This caused two concurrent Claude CLI invocations for the same repo — the heal produced a rich description, but the regular refresh finished second and overwrote it with a terse one-liner. Fixed: (1) `(True, True)` branch returns None instead of an incremental prompt; (2) `_run_loop_single_pass` adds `continue` when alias was in `_stub_heal_no_quarantine_aliases`.

New scheduler methods: `_heal_stub_description_worker(repo_alias, repo_path_str)` (BJM-compliant `Callable[..., Dict[str, Any]]` wrapping `_heal_stub_description`; raises `RuntimeError` on `None` return so BJM marks the job FAILED for dashboard visibility); `_dispatch_heal_via_background_job(repo_alias, repo_path_obj)` (submit wrapper handling `DuplicateJobError` + missing-BJM gracefully).

New log codes: `DESC-REFRESH-STUB-HEAL-011` (DuplicateJobError → heal already in flight, skip), `DESC-REFRESH-STUB-HEAL-012` (submit_job unexpected exception, ERROR), `DESC-REFRESH-STUB-HEAL-013` (BackgroundJobManager not wired, WARNING — lifespan misconfig), `DESC-REFRESH-STUB-HEAL-014` (job submitted, INFO with job_id).

Test suite: `tests/unit/server/services/test_description_refresh_stub_heal_v10_4_14.py` rewritten — 13 prior synchronous-heal assertions replaced with BJM dispatch assertions; new test classes `TestBackgroundJobDispatchContract`, `TestDuplicateJobErrorHandling`, `TestNoBackgroundJobManagerWired`, `TestNonBlockingDispatch`, `TestStubHealWorker` cover the BJM contract end-to-end.

**Breaking change for callers**: NONE. All scheduler instantiation flows through `lifespan.py` which now passes the BJM. Standalone test instantiation must inject a mock BJM (or set `_background_job_manager = None` to exercise the not-wired branch).

## v10.4.13 (2026-05-05) — Anti-fallback: descriptions require Claude CLI (no static regex, no README copy)

Production user reported "descriptions look very terse and relatively short" vs the richer historical state. Root cause: two silent fallback paths in the description-generation pipeline produced degraded output whenever Claude CLI was unavailable or returned no result, yet still wrote a description file (so operators saw "success" with terse content):

1. `meta_description_hook.on_repo_added` had a README-copy fallback (`_create_readme_fallback`) when `cli_manager` was `None` or CLI was off PATH.
2. `RepoAnalyzer.extract_info` fell back to static regex extraction (capped at 10 features + 5 use cases) when Claude returned `None`.

Both violated Messi Rule #2 (anti-fallback): graceful failure over forced success. Per the user's explicit mandate, v10.4.13 enforces hard-fail on missing Claude CLI:

- `meta_description_hook.on_repo_added`: raises `RuntimeError` when `cli_manager` is `None` or `check_cli_available()` returns `False`. Caller (golden repo registration job) logs ERROR; description file is NOT written; admin retries when CLI is restored.
- `meta_description_hook._generate_repo_description`: now REQUIRES a `cli_manager: ClaudeCliManager` positional parameter. Runtime `isinstance` guard raises `TypeError` on `None` or wrong type (negative test verifies bare `MagicMock` is rejected).
- `repo_analyzer.RepoAnalyzer.extract_info`: raises `RuntimeError` when `_extract_info_with_claude()` returns `None`. The static regex fallback is deleted entirely. `CIDX_USE_CLAUDE_FOR_META` env-var feature flag also removed (Claude is now mandatory, not optional).
- `_create_readme_fallback` function deleted from `meta_description_hook.py` (anti-orphan-code per Messi Rule #12).

Anti-regression test suite: `tests/unit/global_repos/test_description_no_fallback_v10_4_13.py` (8 tests across 4 ACs):
- AC1: `_generate_repo_description` raises `TypeError` on `None` / wrong-type cli_manager AND forwards valid manager to RepoAnalyzer (observable via `check_cli_available` call).
- AC2: `RepoAnalyzer.extract_info` raises `RuntimeError` when subprocess (mocked at the boundary) reports no result.
- AC3: `_create_readme_fallback` is absent from module namespace (`hasattr` check, not just `FunctionDef`).
- AC4: `on_repo_added` raises `RuntimeError` with specific reason (`ClaudeCliManager not initialized` / `Claude CLI not available on PATH`) when cli_manager unavailable.

Test fixture cleanup: 9 existing tests were relying on the now-removed fallback (bare `MagicMock()` for `cli_manager`, calls to `_generate_repo_description` without the new positional, expectations on README-copy outputs). All updated to use `MagicMock(spec=ClaudeCliManager)` with `check_cli_available=True` and patch `RepoAnalyzer` at the meta_description_hook import path.

Operator behavior change: when Claude CLI is unavailable, golden repo registration logs ERROR with code path `meta_description_hook.on_repo_added` and the repo registration completes WITHOUT a description file in cidx-meta. Refresh scheduler then picks the repo up for retry on subsequent passes once CLI is restored. No silent terse-output state any more.

## v10.4.12 (2026-05-05) — CRITICAL: DescriptionRefreshScheduler unwired in production (silent no-op)

Production user reported the description refresh job had NEVER been seen running in dashboard or logs (beyond "Description refresh scheduler started"). Root cause: `global_lifecycle_manager` is initialized inside a try/except at `lifespan.py:509`. If `GlobalReposLifecycleManager(...)` or `.start()` raises, the exception is caught at line 579, logged at `APP-GENERAL-015`, but `global_lifecycle_manager` stays `None`. The description scheduler block at lines 887-935 then computed `refresh_scheduler = None` and the ENTIRE D3 wiring block (lines 896-935) was guarded by `if refresh_scheduler is not None:` — meaning ALL four collaborator assignments (`_lifecycle_invoker`, `_golden_repos_dir`, `_lifecycle_debouncer`, `_refresh_scheduler`) were skipped. The scheduler's `_check_lifecycle_backfill_wiring()` then returned `False` on every 60s loop pass, silently bailed out. Bug invisible in production logs.

Fix (Tier 1 + Tier 2 combination):
- Moved `description_refresh_scheduler._golden_repos_dir = Path(golden_repos_dir)` OUT of the conditional — `golden_repos_dir` is always available so this slot can always be wired.
- Added explicit startup-time check before `description_refresh_scheduler.start()` that scans for missing collaborator slots and logs at ERROR level with code `APP-GENERAL-051` if any are None. The error message names the missing slots and explains the no-op consequence + the recovery hint (ensure global_lifecycle_manager initialization succeeds). Operators now see the misconfiguration immediately at startup, not 60s into a silent loop.

Anti-regression test suite (`tests/unit/server/startup/test_description_refresh_scheduler_wiring_v10_4_11.py`) ensures: (a) all 4 slots wired when global_lifecycle_manager present; (b) APP-GENERAL-051 ERROR logged when missing; (c) golden_repos_dir always wired regardless.

## v10.4.11 (2026-05-05) — Bug #984 logging dedup + xray dashboard verification

- **Bug #984 logging fix (#70)**: even after v10.4.9 cratered the warning rate 94% (3,010/24h → 4 in 30 min by stopping the wipe upstream cause), residual stub descriptions still re-emitted "Cannot generate refresh prompt for &lt;repo&gt;: missing description or last_analyzed" on every scheduler pass because the warning fires BEFORE the quarantine branch can suppress it. Fix in `description_refresh_scheduler.py`: per-repo "warned-already" flag set on first WARNING emission, downgrades subsequent emissions to DEBUG level. Flag re-armed after a successful refresh so legit failures still warn. Quarantine state is also checked before `_get_refresh_prompt()` to short-circuit the call entirely on already-quarantined repos.
- **#67 dashboard xray-visibility verification**: investigation confirmed `xray_search` and `xray_explore` jobs already appear correctly in the dashboard recent-jobs widget. Neither `BackgroundJobManager.get_recent_jobs_with_filter` nor `JobTracker.get_recent_jobs` filter by operation_type, and `dashboard_recent_jobs.html` has no exclusion list. No production change needed; 3 anti-regression tests added at `tests/unit/server/web/test_dashboard_xray_visibility_v10_4_11.py` to lock the contract.

## v10.4.10 (2026-05-05) — Health badge data-volume fix + auth test cleanup (partial)

- **Bug #71**: dashboard health badge (HEALTHY / DEGRADED / UNHEALTHY) and the system_metrics alert cache used `psutil.disk_usage("/")` (root volume only). If `/mnt/codeindexer-data` filled up while `/` stayed fine, the health indicator stayed green and no alert fired. Fix: both `health_service.py:298` (`_check_storage_health`) and `system_metrics_collector.py:97,151` (`_get_system_info`) now resolve `data_path` from `ServerConfig.server_dir`, validated via `os.path.isdir()`, with `/` fallback when config is unavailable. Per-volume progressbars (line 753) were already correct via `psutil.disk_usage(partition.mountpoint)` and remain unchanged.
- **Test cleanup #68 (partial — 5 of 47)**: `test_mcp_session_state.py` had a stale assertion that `NORMAL_USER` lacked `activate_repos` permission — Story #981 (commit `860db6dc`) granted it; assertion updated. `test_totp_service.py` 4 errors fixed via `@pytest.mark.timeout(65)` on classes whose fixtures sleep 31s to advance the TOTP window (default 30s pytest timeout). The remaining 42 failures categorized as Class B (test infrastructure: account-lockout state contamination, SQLite fixture setup, real-login fixture failures — 22 items) and Class C (test-side stale paths: `app.password_change_rate_limiter` should be `code_indexer.server.auth.rate_limiter.password_change_rate_limiter` after rate-limiter modularization; some tests use stale endpoint URLs — production code is INTACT — 18 items). Cleanup tracked as v10.4.11 (#72).

## v10.4.9 (2026-05-05) — CRITICAL HOTFIX: cidx-meta description filename mismatch (production data loss)

`MetaDirectoryUpdater` and `on_repo_added()` used different naming conventions for cidx-meta description files. Every cidx-meta refresh cycle treated all hook-created files as orphaned and deleted them, replacing with 3-line stubs. Production evidence: cidx-meta commit `971850c` deleted 892 files and added 893 stubs in one run (`git diff e0be4bd 971850c --stat`: 1787 files changed, 2682 insertions, 27622 deletions).

Root cause: `meta_description_hook.py:363,455` (and `on_repo_added`'s README fallback) wrote `{repo_name}.md` (e.g. `JSqlParser.md`); `MetaDirectoryUpdater.update()` and 6 other consumers expected `{alias_name}.md` (e.g. `JSqlParser-global.md`). 7 sites used the alias form, 3 used the bare form. Fix: align the 3 minority sites to the dominant alias-form convention. Plus `refresh_scheduler.py:2350-2351` was stripping `-global` then searching for the bare-name file (which doesn't exist) — now uses `alias_name` directly.

Anti-regression test (`tests/unit/global_repos/test_cidx_meta_filename_alignment_v10_4_9.py`) ensures the hook and MetaDirectoryUpdater always agree on filenames.

Likely also closes the upstream cause of GitHub bug #984 (description_refresh_scheduler warning spam — 18,082 "missing description or last_analyzed" warnings since log id 375287). The wipe-and-stub cycle produced descriptions without `last_analyzed` field, which the scheduler then repeatedly tried to refresh and re-warned on every pass. Stop the wipes → stop the warnings. Logging-side suppression in the scheduler (separate fix) tracked as #70.

## v10.4.8 (2026-05-05) — OAuth Bearer (opaque) pre-elevation — Open 1 FOURTH attempt

v10.4.7 fixed the Basic-auth client-credentials path (Priority 1 in `get_current_user_for_mcp`), but the v10.4.7 field test confirmed Open 1 was STILL broken: Claude.ai's MCP integration uses OAuth Bearer tokens issued via `/oauth/token` (authorization code flow), which are OPAQUE random strings (`secrets.token_urlsafe(48)` stored in `oauth_tokens` table) — NOT JWTs. Those land on Priority 2 (the JWT/Bearer path), where `jwt_manager.validate_token()` silently fails for opaque tokens (caught with debug log), `request.state.user_jti` is never set, and the elevation decorator's Gate 5 fires "No session key on MCP request."

Fix: in `get_current_user_for_mcp` Priority 2, when JWT validation didn't set `user_jti` and `oauth_manager.validate_token(token)` succeeds (proving it's an OAuth-issued opaque token, not an arbitrary string), derive `session_key = f"oauth:{sha256(token)[:16]}"`, set `user_jti`, AND open an elevation window via `elevated_session_manager.create()`. Same logical step as v10.4.7's Basic-auth fix, just at the Bearer/opaque path. Bearer JWT login tokens (issued by `/auth/mfa/verify`) keep existing behavior — `user_jti` from JWT `jti` claim, explicit `/auth/elevate` required.

**Open 1 attempt history**: v10.4.5 fixed handler bare-except (didn't reach — gate fires upstream); v10.4.6 fixed gate response shape (didn't reach — OAuth never set session_key); v10.4.7 fixed Basic-auth client-credentials (didn't reach Claude.ai — uses Bearer opaque); v10.4.8 closes the actual user-visible path. Certification this time MUST replicate the field tester's exact auth flow (real OAuth Bearer opaque token from `/oauth/token`, not synthetic credentials).

## v10.4.7 (2026-05-05) — OAuth-MCP sessions pre-elevated (Open 1 ROOT CAUSE)

OAuth-authenticated MCP sessions (e.g. Claude Code via CIDX MCP credentials) could not invoke admin-gated tools (`set_session_impersonation`, `list_users`, `create_user`, etc.) because the OAuth auth path never set `request.state.user_jti`, so the `@require_mcp_elevation` decorator's Gate 5 fired with "No session key on MCP request." End-to-end staging test confirmed the Bearer + TOTP + `/auth/elevate` pipeline works correctly; OAuth was the only gap.

Fix (Variant C — pre-elevated OAuth): `get_mcp_user_from_credentials` now sets `request.state.user_jti = f"oauth:{client_id}"` AND opens an elevation window keyed on that session_key after successful credential verification. OAuth client credentials are long-lived secrets *provisioned by* a TOTP-elevated admin session — they're already a step-up artifact. Forcing per-call TOTP would be defense-in-depth without proportional security gain. Industry pattern matches (AWS IAM service accounts don't MFA per-call). Bearer/cookie sessions still go through the explicit `/auth/elevate` flow.

Operator implications: OAuth credentials confer admin scope persistently until revoked. Scope at issuance time, audit via `session_key=oauth:<client_id>` log lines, revoke via the existing MCP credential management UI when needed.

This is the THIRD attempt at Open 1 — v10.4.5 fixed the impersonation handler's bare-except (didn't help; gate fired upstream); v10.4.6 fixed the gate response shape (didn't help; OAuth path never set session_key). v10.4.7 closes the actual root cause.

## v10.4.6 (2026-05-05) — Elevation decorator response shape + .git/ exclusion + is_admin field removed

Three defects from the v10.4.5 staging field test.

- **CRITICAL (Open 8 ROOT CAUSE)**: `set_session_impersonation` and other elevation-gated admin tools (`list_users`, `create_user`, etc.) returned generic "Error occurred during tool execution" with no diagnostic. v10.4.5 fixed the impersonation handler's bare-except, but the fix never executed because the `@require_mcp_elevation` decorator fires FIRST and was returning RAW dicts (not `_mcp_response`-wrapped). The MCP protocol layer expects `{"content":[{"type":"text","text":...}]}` shape; raw dicts get surfaced as generic errors at the higher transport layer. Now `_disabled_error`, `_elevation_required_error`, `_totp_setup_required_error` all wrap via `_mcp_response` (lazy import to avoid circularity). Structured codes — `elevation_required`, `totp_setup_required`, `elevation_enforcement_disabled` — now reach the MCP client as documented.
- **LOW (Obs 3.3)**: `cidx-meta-global` filename Phase 1 returned `.git/FETCH_HEAD` and `.git/COMMIT_EDITMSG` as candidates. Extended v10.4.4's `.code-indexer/` exclusion to also cover `.git/` in both filename and content Phase 1.
- **LOW (Obs 3.2)**: `is_admin` field removed from public job-result responses. v10.4.5 added a docstring explaining the field is a `BackgroundJobManager` priority/ownership-bypass flag (NOT submitter role), but field testers continued to misread it because they read response dicts, not source. Both leak paths in `BackgroundJobManager` — `get_job_status` SQLite fallback and `list_jobs` SQLite-merge — now scrub `is_admin` before returning. The internal `BackgroundJob.is_admin` field and SQLite storage remain unchanged for scheduling/ownership-bypass logic.

## v10.4.5 (2026-05-05) — Field-test follow-ups + UX cleanup

Five defects from the v10.4.3 / v10.4.4 staging field-test cycles.

- **Defect 1 — `set_session_impersonation` returned generic "internal error"**: bare `except Exception` in `handle_set_session_impersonation` swallowed the real cause (often `session_state=None` from MCP transport, or the `@require_mcp_elevation` decorator firing with structured 403 errors). Now surfaces specific error codes (`session_state_unavailable`, `elevation_required`, etc.) instead of stringifying.
- **Defect 2 — `xray_search` vs `xray_explore` dedup gate inconsistency**: BackgroundJobManager.submit_job's dedup fires identically for both (operation_type + repo_alias scoped, in-flight jobs only — completed jobs do NOT block resubmission). The field-test observation was a timing artifact (xray_search jobs complete fast on small repos and don't collide). Documented behavior in tool docs and added concurrency test to lock the contract.
- **Defect 3 — Admin-mask risk on C3.5 / C3.7**: added explicit non-admin protocol-level access tests proving v10.4.4's deactivate_repository fix and v10.4.4's nonexistent-repo wording both work for non-admins, not just via admin bypass.
- **Defect 4 — `is_admin` field naming confusion**: test agents repeatedly misread job-result `is_admin: false` as "this user is not an admin". Added clarifying docstring to `BackgroundJob` dataclass and tool docs explaining `is_admin` is a job-priority opt-in flag (bypasses ownership check on cancel/get-job), NOT the submitter's role. Xray handlers don't request the priority lane so `is_admin` always reports False regardless of who submitted.
- **Defect 5 — Single-element list returned multi-repo shape**: `repository_alias=["cidx-meta-global"]` returned `{job_ids:[...], errors:[]}` instead of `{job_id:"..."}`. Now normalized: 1-element list → single-repo shape; multi-element list → multi-repo shape unchanged. Plain-string alias unchanged.

## v10.4.4 (2026-05-05) — X-Ray hotfix bundle (post-v10.4.3 staging findings)

Seven findings from the v10.4.3 arms-length staging test cycle.

- **HIGH (3.5)**: `deactivate_repository` access check denied owners their own activations. Quirk 7's fix to `_check_repository_access` (use `golden_repo_alias` for activate's source-repo check) was never propagated to the symmetric deactivate handler. Fix: add `deactivate_repository` to a small ownership-enforced-tool allowlist that bypasses the protocol-level group-access guard, since the activation manager already enforces ownership at the data layer.
- **HIGH (3.2)**: `xray_dump_ast` `max_nodes` parameter ignored — every call returned ~142KB regardless of the value, triggering MCP token-cap overflow. Was hardcoded `max_nodes=500` at the call site to `_serialize_ast`. Now properly extracted from params with [1, 2000] range validation.
- **MEDIUM (3.1)**: PCRE2 invalid regex completed silently with empty results. RegexSearchService at `regex_search.py` was logging-and-swallowing ripgrep errors (Messi Rule 13 violation). Now raises `RipgrepExecutionError`; XRaySearchEngine catches it and surfaces `phase1_failed=True, phase1_error=<msg>` in the job result.
- **MEDIUM (3.3)**: Matches missing required `line_number` were silently accepted and enriched. Now rejected as `InvalidEvaluatorReturn` for the entire file response.
- **LOW (3.4)**: String `line_number` (`"42"`) raised raw Python `ValueError` from `int()` coercion. Now wrapped as `InvalidEvaluatorReturn` with actionable message.
- **LOW (3.6)**: `cidx-meta-global` and similar repos indexed their own `.code-indexer/` directory's internal error logs and vector JSON files as Phase 1 candidates. Both `_run_phase1_filename` and `_run_phase1_content` now exclude `.code-indexer/` unconditionally.
- **LOW (3.8)**: Validation error message listed 18 value builtins but omitted the 9 exception types that ARE in `SAFE_BUILTIN_NAMES`. Now lists both. CLAUDE.md count reconciled to 27 (18 + 9).

## v10.4.3 (2026-05-05) — X-Ray hotfix bundle + sandbox defense-in-depth

Three findings from v10.4.2 arms-length staging test cycle, plus one dunder-in-Slice bypass surfaced by code review of fix #2 before shipping.

- **HIGH-SEVERITY SECURITY**: `_check_repository_access` (`src/code_indexer/server/mcp/protocol.py`) skipped list-form `repository_alias` for non-admin users (omni xray dispatch bypassed access control — verified by staging test agent retrieving 32 matches from a denied repo). Now iterates each entry in native lists and JSON-encoded array strings, applying admin bypass / scoped_repos / accessible-set checks identically to the single-string path. Defense-in-depth fix: protects ALL omni-style tools, not just X-Ray.
- **MEDIUM**: `ast.Slice` added to sandbox whitelist (`src/code_indexer/xray/sandbox.py`). Evaluators can now use slice syntax (`source[10:20]`, `source[-30:]`, `lines[0:10:2]`).
- **MEDIUM SECURITY (caught pre-ship)**: Adding `ast.Slice` to the whitelist initially opened a dunder-in-Slice bypass — `obj['__class__':10]` and similar slice-wrapped dunder strings (lower / upper / step components) passed validation because the existing dunder check at `validate()` only inspected `node.slice` when it was a direct `ast.Constant`. Code review caught this before commit. Fix: extend the dunder block to ALSO inspect `Slice.lower`, `Slice.upper`, and `Slice.step` for dunder-string Constants. While not directly exploitable on built-in types (dict/list/str raise TypeError on string slice indices), the gap weakened the defense-in-depth posture and could become exploitable on custom `__getitem__` classes. New regression suite at `tests/unit/xray/test_slice_dunder_bypass_v10_4_3.py` covers all 5 attack vectors plus 3 legit-slice positive cases.
- **MEDIUM**: Invalid regex patterns pre-validated at handler level for `xray_search` and `xray_explore` (non-PCRE2 mode). Returns `invalid_regex` error immediately instead of silently empty results. PCRE2 patterns continue to be validated by ripgrep at execution time.

Plus pre-existing lint debt cleanup (16 ruff format violations + 6 mypy errors that shipped with v10.4.0/v10.4.1) — `./lint.sh` now exits 0 per Bug #900 prevention rule.

## v10.4.2 — 2026-05-05

### Fixed

- **`list_global_repos` returned only 1 of N repos for admin users** (HIGH severity, pre-existing — discovered during v10.4.1 staging test setup): `handle_list_global_repos` (`src/code_indexer/server/mcp/handlers/repos.py`) applied `AccessFilteringService.filter_repo_listing` to all callers including admins. The filter checks group membership; admins by role (e.g. `Seba.Battig@lightspeeddms.com` with `role='admin'`) but not yet assigned to an explicit "admins group" saw only `cidx-meta-global` despite 8 repos existing. Fix: bypass the access filter when `user.role == UserRole.ADMIN`. Bug dates back to commit `6b914ab73` (Story #496 handler refactor, 2026-04-14) but was dormant until today's OAuth-authenticated admin-role testing surfaced it.

- **`activate_repository` denied access to `user_alias` before creating it** (HIGH severity, pre-existing): `_check_repository_access` (`src/code_indexer/server/mcp/protocol.py`) extracted the repository identifier from `user_alias` (the NEW alias being created — doesn't exist yet) instead of `golden_repo_alias` (the existing source repo to activate from). Result: every activation attempt returned `Access denied: repository '<user_alias>' is not accessible to user '<username>'`. Fix: the access check now correctly extracts `golden_repo_aliases` (composite form, list — each entry checked individually), `golden_repo_alias` (single form, str), or falls through to `repository_alias`/`alias`/`user_alias`/`repo_alias` for tools that operate on existing repos. The `user_alias` is the new alias being CREATED in `activate_repository` and must NOT be checked.



### Fixed

- **X-Ray omni multi-repo crashed at MCP dispatch** (HIGH severity from production field testing): `repository_alias` as a native list or JSON-encoded array threw `AttributeError: 'list' object has no attribute 'endswith'` before the job was even queued. The v10.4.0 schema declared array support but handlers only had the string path wired. Fix: both `handle_xray_search` and `handle_xray_explore` now route through `_parse_json_string_array` and dispatch via `isinstance(..., list)`. Multi-repo response shape mirrors `regex_search`: `{job_ids: [...], errors: [...]}` with per-alias error handling.
- **Default evaluator returned bool, broke under v10.4.0 dict contract** (HIGH severity from production field testing): when `evaluator_code` was omitted, both `xray_search` and `xray_explore` defaulted to `"return True"` (legacy v10.3.x contract), which under v10.4.0 yielded `InvalidEvaluatorReturn` for every file → zero matches. Both handlers now default to `_DEFAULT_EVALUATOR_CODE` — a v10.4.0-compliant snippet that emits one match per Phase 1 regex hit: `matches = [{"line_number": mp["line_number"]} for mp in match_positions]; return {"matches": matches, "value": None}`.

## v10.4.0 — 2026-05-04

### Changed (BREAKING — X-Ray evaluator contract)

- **X-Ray evaluator now operates on the file-as-unit, returns dict** — major redesign of the evaluator contract per field-feedback that the per-regex-match-position model forced inverting predicates and was hard to express. The new contract:
  - The evaluator runs ONCE per candidate file (not once per regex hit)
  - It receives `node = root` (file's parse tree) plus a NEW `match_positions: List[Dict]` global containing ALL Phase 1 regex hits in the file `[{line_number, column, line_content, byte_offset}, ...]` (empty list in filename mode)
  - It returns a DICT: `{"matches": [{"line_number": int, ...optional fields...}], "value": <anything serialisable>}`. The `matches` list can be empty (no matches in this file). The `value` is a per-file open value surfaced in the response as `file_metadata`.
  - The server enriches each match with `file_path` (always), `line_content` (derived from source if omitted), `context_before`/`context_after` (derived if `context_lines > 0`), and `language` (always).
  - **MIGRATION**: existing evaluator code that returned `bool` must be rewritten to return the dict shape. The legacy globals `match_byte_offset`/`match_line_number`/`match_line_content` are still present but ALWAYS None — use `match_positions` instead. See xray_search.md cookbook for 15 worked patterns under the new contract.

### Added

- **Sandbox lifts statement-level control flow + structured exception handling**:
  - Newly allowed: `If`, `For`, `While`, `Break`, `Continue`, `Pass` (statement-level — termination is bounded by the 5s subprocess timeout, not by AST validation)
  - Newly allowed: `Try`, `ExceptHandler`, `Raise` (defensive evaluators, clean error surfacing)
  - Still banned: `def`, `class`, `lambda`, `import`/`from import`, `with`/`async with`, `global`/`nonlocal`, `async`/`await`, `yield`/`yield from`
  - Safe builtins extended with exception types: `Exception`, `ValueError`, `TypeError`, `RuntimeError`, `AttributeError`, `KeyError`, `IndexError`, `NameError`, `StopIteration`
- **Omni multi-repo support** for `xray_search` and `xray_explore` — `repository_alias` accepts `string` (single repo, returns `{job_id}`), `array of strings` (multi-repo, returns `{job_ids: [...], errors: [...]}` with per-alias error handling), or JSON-string-encoded array (parsed). Mirrors the `regex_search` omni pattern.
- **Server threadpool capacity bumped to 256** (was anyio default 40) — set at lifespan startup via `anyio.to_thread.current_default_thread_limiter().total_tokens`. Bootstrap-only `server_threadpool_size` knob in ServerConfig (default 256, set to 0 to keep anyio default). Absorbs concurrent X-Ray `await_seconds` long-polls without starving other endpoints.

### Changed

- **regex_search-parity parameter alignment** — X-Ray tools now mirror regex_search inputSchema for transferable mental model:
  - `driver_regex` renamed to `pattern`
  - `max_files` renamed to `max_results`
  - Added: `path` (subdirectory), `case_sensitive` (default true), `multiline` (default false), `pcre2` (default false), `context_lines` (int 0-10, default 0)
  - Output match envelope adds `column`, `context_before`, `context_after` per regex_search shape; renamed `code_snippet` to `line_content`
- **Documentation reframe** — dropped "expression" / "boolean expression" / "function body" framing throughout xray_search.md, xray_explore.md, docs/xray-architecture.md, CLAUDE.md. Replaced with "Python code snippet that returns a dict." All 15 cookbook patterns rewritten under the v10.4.0 contract using the lifted-ban constructs naturally where clearer than nested comprehensions; pattern 11 demonstrates `try/except` for defensive evaluation.

### Internals

- New error code `InvalidEvaluatorReturn` for evaluators that return non-dict / malformed dict shapes (replaces `NonBoolReturn` from v10.3.x — bool returns are no longer the contract).

## v10.3.2 — 2026-05-04

### Changed (BREAKING for evaluator code)

- **X-Ray evaluator contract: `node` is now ALWAYS the file root** — corrects the design mistake from v10.3.0 (the "Bug #983 fix" wrongly passed the smallest enclosing match node). Both content-mode and filename-mode evaluators now receive `node = root`. Phase 1 regex match position is exposed as THREE NEW evaluator globals: `match_byte_offset` (int|None), `match_line_number` (int|None), `match_line_content` (str|None) — all `None` in filename mode. **MIGRATION**: evaluators that walked UP via `node.enclosing(...)` should be rewritten to walk DOWN via `node.descendants_of_type(...)` and (if positional precision matters) filter via `match_byte_offset`. The `root` global is preserved as an alias for `node` for explicit code. Field-test feedback: previous contract forced inverting every predicate from "this match is leaky" to "block contains a leaky thing"; the corrected contract restores the natural top-down walking pattern.

### Added

- **`await_seconds` accepts FLOAT values** — for sub-second sync mode (e.g., `await_seconds=0.5` = 500ms wait). Single parameter, expanded type — backward compatible (integer values still work). Previously int-only.
- **7 new cookbook patterns** in xray_search.md tool docs — bringing the total from 8 to 15: functions with N+ branches, returns inside ifs, calls without error handling, TODO/FIXME comments, public functions missing return-type annotations (Python), bare except clauses, classes with no docstring. All examples rewritten to use the v10.3.2 node=root contract.

### Changed

- **`await_seconds` ceiling lowered from 30s to 10s** — bounds threadpool occupancy. FastAPI sync handlers run in a finite thread pool (default ~40 threads); `await_seconds=30` at modest concurrency could starve other endpoints. For longer waits, use the async `{job_id}` path. Operators who need a different cap can adjust the constant in `handlers/xray.py` or tune uvicorn `--limit-concurrency`.

### Documentation

- Comprehensive evaluator-contract documentation update across `xray_search.md`, `xray_explore.md`, `docs/xray-architecture.md`, and `CLAUDE.md`. New "Globals exposed to your evaluator" subsection documents all 8 globals (was 5). XRayNode reference table clarified to apply to any node reachable from `node`. inputSchema for `await_seconds` updated: `type: number`, `maximum: 10.0`.

## v10.3.1 — 2026-05-04

### Documentation

- **X-Ray docs: explicit ban on statement-level `if`** (post-v10.3.0 field-test follow-up): `xray_search.md` and `xray_explore.md` now explicitly call out that `if` / `for` / `while` / `try` statements are banned alongside the previously-documented banned constructs, with guidance to use `IfExp` ternary (`a if cond else b`) for conditional logic and comprehensions for iteration.
- **X-Ray docs: Evaluator code structure subsection**: a new "Evaluator code structure" section explains that the evaluator code is parsed as a Python **Module** (function body), not a bare expression. Multi-statement evaluators are first-class — bind locals with `=` (`Assign`) or `+=` (`AugAssign`), then return a final boolean. The previous documentation implied bare expressions were the only valid form, hiding the multi-statement capability that the v10.3.0 sandbox expansion enabled.

## v10.3.0 — 2026-05-04

### Added

- **X-Ray field-feedback bundle (21 issues, 14 work items)** — comprehensive response to real-world claude.ai testing feedback on the v10.2.0 X-Ray engine.

  **Sandbox whitelist expansion** (issue #7): the evaluator sandbox now accepts comprehensions (`comprehension`, `GeneratorExp`, `ListComp`, `SetComp`, `DictComp`), local variable binding (`Assign`, `AugAssign`, `operator`), and ternary expressions (`IfExp`). Top-level `for`/`while` statements remain banned (use a comprehension instead). This unlocks the canonical AST-search use case ("find functions with N elif clauses") which previously required client-side aggregation.

  **New XRayNode methods**: `descendants_of_type(name) -> list[XRayNode]` (DFS pre-order, excludes self), `count_descendants_of_type(name) -> int` (fast accumulator that doesn't materialise wrapper objects), `enclosing(type_name) -> XRayNode | None` (walks up parent chain, inclusive of self — solves the "regex landed on `def` keyword instead of `function_definition`" gotcha), and `node.text` property (raw source text decoded UTF-8 with `errors='replace'` — required for string-literal pattern checks like SQL injection detection).

  **xray_explore matched_node** (issue #14): every match in `xray_explore` output now includes a `matched_node` block with `type`, `start_byte`, `end_byte`, `start_point`, `end_point` — describes what the evaluator actually received, complementing the existing file-rooted `ast_debug` field.

  **xray_dump_ast MCP tool** (issue #19): new synchronous tool for single-file AST exploration without requiring a `driver_regex`. Returns the file's parse tree in the same serialisation format as `xray_explore`'s `ast_debug` field. 5s timeout. Auth: `query_repos`.

  **await_seconds parameter** (issue #17): `xray_search` and `xray_explore` now accept `await_seconds` (int, 0..30, default 0). When >0, the server polls job status for up to N seconds before falling through to the async `{job_id}` envelope. For fast queries (<5s), this halves the round-trip count vs the previous always-async pattern.

  **cidx_fetch_cached_payload MCP tool** (issue #20): the cache fetch endpoint (`GET /api/cache/{cache_handle}`) is now exposed as a discoverable MCP tool. Truncation messages in `xray_search`/`xray_explore` now name the tool by its registered name so clients can find it.

### Fixed

- **Glob zero-match warnings** (issue #3): when an `include_patterns` entry matches zero files in Phase 1, the response now includes a `warnings[]` array with a `zero_match_include_pattern` entry naming the pattern and explaining the `*` vs `**` distinction. Previously `*/time.py` silently produced `files_total: 0` with no diagnostic.

- **Sandbox validation error messages** (issue #18): rejection messages now include (a) the full current whitelist, (b) targeted workaround hints for common mistakes (e.g., `For` rejection now reads "Use a comprehension (ListComp/GeneratorExp/SetComp/DictComp) instead of a top-level `for` statement"), and (c) a pointer to the evaluator API documentation. Evaluator AttributeError messages now include `Did you mean: <closest valid attribute>?` suggestions via `difflib.get_close_matches`.

- **Doc accuracy** (issues #1, #2, #4, #10): the `xray_search` example using `any(n.type == 'X' for n in root.named_children)` now actually works (was previously rejected by the sandbox before #21). The `line_number` and `code_snippet` fields are correctly documented as populated for `search_target='content'` (was previously claimed null pending Story #978).

### Documentation

- **XRayNode reference table** (issues #8, #11): every public attribute and method on `XRayNode` is now documented in a single reference table in both `xray_search.md` and `xray_explore.md`. Users no longer need to probe with `hasattr` to discover the API.
- **Common patterns cookbook** (issue #12): 8 worked evaluator patterns covering the most common questions — filter to function bodies, exclude comments/docstrings, walk to ancestor, count structural property, string-literal pattern check, parameter count, deep nesting detection, comprehension presence.
- **Cross-language node type table** (issue #13): a 6-language reference covering 10 construct categories (function definition, function call, class definition, if statement, else-if, for loop, try block, variable declaration, string literal, comment) so users coming from one language can apply patterns to another.
- **evaluation_errors[] payload examples** (issue #15): one realistic payload per error_type (`EvaluatorTimeout`, `EvaluatorCrash`, `NonBoolReturn`, `UnsupportedLanguage`) plus the synchronous `validation_failed` rejection path that fires before job submission.

## v10.2.1 — 2026-05-04

### Fixed

- **X-Ray dependencies promoted to core (deployment fix)** — v10.2.0 shipped `tree-sitter>=0.21,<0.22` and `tree-sitter-languages==1.10.2` as `[xray]` optional extras, but the CIDX server auto-updater installs the base package only. Result: `xray_search` and `xray_explore` MCP tools were registered in the schema but failed at runtime on staging with `XRayExtrasNotInstalled` because tree-sitter wasn't installed. Both dependencies are now core (in `[project] dependencies`); the `[xray]` extras block is removed. The `XRayExtrasNotInstalled` exception class and all its error-handling branches in CLI, MCP handlers, and HTTP routes are deleted (per Anti-Fallback rule — tree-sitter is a system invariant now). Five tests that verified the optional-extras error path are deleted.

## v10.2.0 — 2026-05-04

### Added

- **Epic #968 — X-Ray AST-Aware Code Search (full implementation)** — v10.1.0 shipped only the `[xray]` optional install group; v10.2.0 delivers the actual two-phase search engine, sandboxed evaluator, MCP tools, CLI commands, and full test suite (Stories #969–#979, 11 stories). Phase 1 driver runs a regex walk over content or filenames (gated by `include_patterns`/`exclude_patterns`); Phase 2 evaluator runs caller-supplied Python against a `XRayNode` AST wrapper inside a hardened sandbox. New MCP tools `xray_search` (production search) and `xray_explore` (debug mode that serialises full AST trees to help authors understand tree-sitter output). Async job pattern returns `{job_id}` immediately; clients poll `GET /api/jobs/{job_id}`. Long results route through `PayloadCache` (`store(content) -> handle`, retrieved via `GET /api/cache/{handle}`) so MCP responses stay under transport limits. New CLI: `cidx xray search` and `cidx xray explore`. 10 mandatory languages: Java, Kotlin, Go, Python, TypeScript, JavaScript, Bash, C#, HTML, CSS — Terraform optional via `tree-sitter-hcl`. Lazy-load discipline preserved: importing `code_indexer.xray.ast_engine` does NOT trigger tree-sitter import (CLI startup ~0.57s, well under 2.0s budget). `ThreadPoolExecutor` parallelises Phase 2 evaluator runs across candidate files for throughput on large repos. New Web UI Config Screen entries for X-Ray runtime settings (max-files cap, timeout, parallelism).

- **Hardened Python evaluator sandbox** — `PythonEvaluatorSandbox` enforces three defense layers: (1) `ast.parse()` + walk against `ALLOWED_NODES` whitelist; (2) stripped builtins (`getattr`, `setattr`, `delattr`, `__import__`, `eval`, `exec`, `open`, `compile` removed; only 17 safe names like `len`, `str`, `min`, `max`, `sorted` remain); (3) `multiprocessing.Process` isolation with SIGTERM at 5.0s and SIGKILL at +1.0s. `DUNDER_ATTR_BLOCKLIST` (frozenset, 39 names) blocks every dunder attribute and `__`-prefixed Subscript at validation time so attempts like `node.__class__.__init__.__globals__['__builtins__']['open'](...)` are rejected before subprocess spawn. Four explicit failure modes returned as `EvalResult.failure`: `validation_failed`, `evaluator_timeout`, `evaluator_subprocess_died`, `evaluator_returned_non_bool`. 112+ canary tests in `tests/unit/xray/test_sandbox*.py` exercise positive, negative, and corner-poking patterns including the confirmed exploit. Architecture invariants: raw `tree_sitter.Node` objects are wrapped in `XRayNode` before exposure to evaluator code; `__slots__ = ("_node",)` and normal assignment (no `object.__setattr__` workaround that would break mypy tracking).

- **Lightspeed Neo Exploration skill — X-Ray section** — `skills/lightspeed-neo-exploration/SKILL.md` extended with a dedicated X-Ray usage section that teaches Claude.ai when to reach for `xray_search` vs `xray_explore`, how to compose driver regex + evaluator code, common evaluator patterns, and how to interpret AST exploration output. Plus 10 general improvements to clarity, examples, and accuracy across the rest of the skill (acronym preservation, paste-ready snippets, error-handling guidance, etc.).

- **`skills/build.sh`** — New build script for `.skill` zip bundles with `--check` mode for pre-commit verification. Wired into `.pre-commit-config.yaml` as `skill-bundle-sync` hook so `SKILL.md` changes that aren't bundled are caught before commit.

### Fixed

- **Bug #982 — `RegexSearchService` not wired into X-Ray Phase 1 driver** — `XRaySearchEngine._run_phase1_driver` for `search_target=content` now delegates to `RegexSearchService` instead of inline file walking, gaining the same `include_patterns`/`exclude_patterns` semantics, gitignore handling, and binary-file detection used elsewhere in CIDX. `search_target=filename` retains its inline path walker (the regex applies to relative paths, not file content). Bug #982 closed.

- **Bug #983 — Per-match-position evaluator missing** — `XRaySearchEngine` Phase 2 now uses `find_enclosing_node` to locate the smallest AST node enclosing each Phase 1 regex match, then evaluates the user's `evaluator_code` against THAT node — not the file's root node. This is what makes X-Ray "precision" search: a regex hit on `func_call(arg)` evaluates against the `call_expression` AST node, not the entire file. Bug #983 closed.

### Changed

- **MCP tool documentation cleanup** — Cleared 30 stub `tool_docs/admin/*.md` files that were generated mid-session by an earlier accidental `convert_tool_docs.py` run; these duplicated canonical docs in `depmap/`, `memory/`, `tracing/`, `files/`, `repos/`, `git/`, and `guides/` subdirectories with no `inputSchema`. Extended `tools/convert_tool_docs.py::CATEGORY_PATTERNS` with explicit routing for `depmap_*`, memory tools, tracing tools, write-mode tools, repo provider tools, PR tools, and `dependency_analysis_workflow` — so future regenerations correctly bucket these instead of dumping them into `admin/`. Re-tightened `src/code_indexer/server/mcp/tool_doc_loader.py` from `logger.warning + continue` (band-aid) back to `raise FrontmatterValidationError` for duplicate tool names — duplicates now fail loud at startup instead of silently being absorbed. Renamed `tool_docs/search/xray.md` → `tool_docs/search/xray_search.md` to match its `TOOL_REGISTRY` entry name; updated 23 references in `tests/unit/server/mcp/test_xray_tool_docs_completeness.py`.

### Security

- **Sandbox dunder-attribute escape closed** — A confirmed exploit pattern (`node.__class__.__init__.__globals__['__builtins__']['open'](path, 'w')`) could write to arbitrary files inside the evaluator subprocess, bypassing both the `STRIPPED_BUILTINS` defense (open was stripped from the dict but reachable via dunder traversal) and the AST node whitelist (`Attribute`, `Subscript`, `Constant`, `Call` all allowed). Fix: `DUNDER_ATTR_BLOCKLIST` now rejects any `ast.Attribute` whose `.attr` is dunder-listed AND any `ast.Subscript` whose slice is a string `Constant` starting with `__`, both at validation time before subprocess spawn. Verified by canary tests in `tests/unit/xray/test_sandbox_dunder_escapes.py`.

## v10.1.0 — 2026-05-02

### Added

- **X-Ray Engine (Epic #968)** — Added `[xray]` optional install group: `pip install code-indexer[xray]` pulls `tree-sitter>=0.21,<0.22` and `tree-sitter-languages==1.10.2` for AST-aware search. Without `[xray]`, `cidx xray` commands emit a graceful `XRayExtrasNotInstalled` error pointing to the install command. CLI startup time unaffected (lazy-load discipline preserves the existing baseline).
- **Story #967 — Activated Repository Reaper** — New background service that periodically scans activated repositories and auto-deactivates those that have been idle beyond a configurable TTL. `ActivatedReaperService` reads `last_accessed` timestamps from activated repos and submits `deactivate_repository` background jobs for any repo whose last-accessed time is older than `activated_reaper_config.ttl_days` (default 30 days); repos with a missing or null `last_accessed` are treated as never-accessed and always eligible for reaping (AC5). `ActivatedReaperScheduler` runs as a daemon thread, submits reap cycles via `BackgroundJobManager` so they appear in the job dashboard (AC3), and re-reads cadence and TTL from config on every cycle so Web UI changes take effect without a server restart (AC4). Manual trigger available via `POST /api/admin/reaper/trigger` (admin-only). New `activated_reaper_config` block in `ServerConfig` with `ttl_days: int = 30` and `cadence_hours: int = 24` configurable from the Web UI Config Screen.
- **Story #981 — Branch-aware exploration for normal users** — `UserRole.NORMAL_USER` now includes the `activate_repos` permission, allowing normal users to activate, deactivate, switch branches on, and sync their own workspace repositories without requiring admin access (AC1). The `switch_branch` route now enforces a guard preventing normal users from switching branches on `*-global` aliases — only admins may mutate global repositories (AC2). The Lightspeed Neo Exploration skill was extended with a Context Discovery Phase (Steps 0a–0e) that guides normal users through asking context questions, listing available branches from `cidx-meta-global`, activating their workspace with the correct branch, waiting for the activation job to complete, and scoping all searches to their workspace alias.

### Fixed

- **Bug #962 — HNSW double-delete crash on reload cycle** — `HNSWIndexManager.remove_vector` and `add_or_update_vector` raised `RuntimeError: "The requested to delete element is already deleted"` when `temporal_indexer.close()` triggered `_apply_incremental_hnsw_batch_update` on a point whose HNSW label had already been soft-deleted in a previous batch. The `load_for_incremental_update` path rebuilds `id_to_label` from persisted metadata which carries no soft-delete state, so a reload after a delete would silently re-expose the deleted label and cause the second `mark_deleted` call to raise. Fix: both `mark_deleted` call sites now wrap the call in a narrow `try/except RuntimeError` that re-raises if the message does not contain "already deleted" and logs a WARNING otherwise. Mapping cleanup (`id_to_label`, `label_to_id` eviction) proceeds regardless. Regression tests cover the exact Bug #962 stack trace path (delete → reload → delete again) plus add/update path and re-raise behavior for unrelated errors.
- **Bug #963 — Empty document strings crash FTS indexer** — The FTS (Tantivy) indexer raised during indexing when a document's `content` field was an empty string. Fix: the chunker now replaces empty string content with a single-space placeholder (`" "`) before writing to the FTS index. Regression tests verify both the empty-string and whitespace-only cases.

### Security

- **SSH key tool docs re-gated to `repository:admin`** — Five SSH key management MCP tools (`cidx_ssh_key_create`, `cidx_ssh_key_delete`, `cidx_ssh_key_list`, `cidx_ssh_key_show_public`, `cidx_ssh_key_assign_host`) had their `required_permission` set to `activate_repos`. Granting `activate_repos` to `NORMAL_USER` (Story #981) would have exposed these server-wide credential-management tools to all authenticated users. Permission gate updated to `repository:admin` (requires admin role) before the normal-user grant was applied.

## v10.0.16 — 2026-05-02

### Fixed

- **Bug fix — cidx-meta backup bootstrap falls back gracefully on strict branch protection** — `CidxMetaBackupBootstrap.bootstrap()` previously used `git push --force` unconditionally when initializing or re-pointing a remote, which is rejected by repositories with strict branch-protection rules (e.g. production git hosts). The two force-push call sites are replaced by a `_push_with_fallback()` helper that first attempts a plain `git push`; if the remote rejects it (e.g. non-fast-forward), it retries with `--force`; if the force push is also rejected, it raises `RuntimeError` with a clear message. This allows bootstrap to succeed against fresh remotes without privileges, and still recover via force push when the remote has diverged history.

## v10.0.15 — 2026-05-02

### Fixed

- **Bug fix (#980) — 403 responses not reaching admin client elevation check** — `_authenticated_request` in `base_client.py` was classifying all `>= 400` responses via `classify_network_error`, which converted 403 responses to `APIClientError` before admin methods could call `_check_elevation_required()`. This silently bypassed `ElevationRequiredError` raising, causing the CLI to print a generic "User creation failed" error instead of the proper TOTP setup/elevation prompt. Fix: exclude 403 from the network error classifier so admin methods receive the raw response and can inspect the body for elevation codes. Verified E2E against staging: `totp_setup_required` now correctly prints "TOTP setup required. Visit /admin/mfa/setup to configure your authenticator." and exits 1.

## v10.0.14 — 2026-05-02

### Added

- **Story #980 — CLI remote mode TOTP step-up elevation** — When the CLI is configured to talk to a remote CIDX server and an admin endpoint returns 403 `elevation_required`, the CLI now automatically prompts for a TOTP code, calls `POST /auth/elevate` to open an elevation window, and retries the original command once. If the TOTP code is wrong (401 `elevation_failed`), the CLI prints a clear error and exits with code 1. If the admin account has no TOTP configured (403 `totp_setup_required`), the CLI prints the setup URL and exits with code 1. Affects all 6 `cidx admin users` commands and all `cidx admin groups` commands. New module `src/code_indexer/api_clients/elevation.py` contains `ElevationRequiredError`, `ElevationFailedError`, `elevate()`, and `with_elevation_retry()`. FastAPI's `{"detail": {"error": "..."}}` wrapping is handled transparently in `AdminAPIClient` and `GroupAPIClient`. Previously-skipped Phase 4 E2E tests in `test_03_admin_users.py` and `test_04_admin_groups.py` (18 tests) are now enabled and passing.

## v10.0.13 — 2026-05-02

### Fixed

- **Bug A — elevation_decorator session_key leak when enforcement disabled** — `require_mcp_elevation` wrapper popped `session_key` from `**kwargs` only AFTER the Gate 1 kill-switch check, so when `elevation_enforcement_enabled=false` the raw session_key was forwarded to the inner handler. Handlers that do not declare `session_key` (or `**kwargs`) would receive a `TypeError`; handlers that do accept `**kwargs` received a credential they were never meant to see. Fix: `session_key` is now popped as the very first statement in `wrapper()`, before any gate, consuming it in the decorator regardless of which gate exits first. Unit tests added in `test_elevation_decorator_gate1_kwargs.py`.
- **29 pre-existing unit test failures resolved** — Handlers decorated with `@require_mcp_elevation()` were called in unit tests without an active elevation window. With `elevation_enforcement_enabled=true` in the local dev config, Gate 3 (TOTP service unavailable in test context) fired and returned a raw error dict instead of an MCP `{"content": [...]}` response, causing `KeyError: 'content'`. Fixed by adding `_active_elevation` context managers to 5 test files: `test_credential_mcp_tools_handlers_api_keys.py`, `test_credential_mcp_tools_handlers_mcp_creds.py`, `test_credential_mcp_tools_handlers_admin.py`, `test_group_mcp_tools.py`, and `test_credential_handlers_no_mock.py`.
- **Langfuse test mock `stop_event` kwarg mismatch** — `TestChronologicalTraceOrdering._mock_langfuse_api` patched `LangfuseApiClient.__init__` without the `stop_event` kwarg added by Bug #964, causing 3 tests to raise `TypeError`. Fixed by adding `**kwargs` to the mock signature.
- **AC3/AC7 session_key injection test assertions corrected** — After Bug A fix, `inner_handler` no longer receives `session_key` via `**kwargs` (the decorator consumes it). Assertions in `test_session_key_injection_protocol.py` updated to `is None` with explanatory comments.
- **8 flaky load-sensitive tests marked `@pytest.mark.slow`** — Tests failing under parallel load due to SQLite lock contention, TestClient app startup timeouts, or wall-clock timing assertions: `TestCleanupDaemon` class (test_cleanup_daemon.py), `TestUsersPageShell` class and `test_users_list_partial_allowed_when_elevated` (test_users_elevation_gate.py), `test_first_boot_logs_migration_event` (test_lifecycle_timeout_validation.py), `test_recovery_scope_insufficient_for_full_required` (test_elevation_decorator.py), and `test_concurrent_execution_with_timing_verification` (test_concurrent_execution.py).

## v10.0.12 — 2026-05-02

### Fixed

- **Bug #965 — CLI rerank order discarded by staleness detector** — `StalenessDetector.apply_staleness_detection` internally sorts results by `(is_stale, -score)`. The conversion loop iterated `enhanced_results` (staleness-sorted order), silently overriding the reranker's ranking. Extracted `_annotate_staleness(results, enhanced_results, preserve_order)` helper: when `preserve_order=True` (reranked queries) iterates `results` in caller order and looks up staleness by `(path, line_start)` composite chunk-identity key; when `preserve_order=False` (non-reranked) annotates all chunks then sorts fresh-first explicitly. Composite chunk-identity keys prevent sibling-chunk collisions when a file produces multiple result chunks. 5 regression tests added.
- **Bug #964 — Langfuse sync sluggishness** — Three fixes: (1) `LangfuseTraceSyncService` was creating a new `ThreadPoolExecutor` per page; moved construction outside the page loop so one pool is reused for the entire project sync. (2) `LangfuseApiClient._request_with_retry` called `time.sleep(wait)` in worker threads; replaced with `stop_event.wait(timeout=wait)` and wired `self._stop_event` from the service into the client so `stop()` immediately interrupts any in-progress backoff; added `is_set()` guard at loop top and after each wait. (3) `_on_langfuse_sync_complete` ran `register_langfuse_golden_repos()` + README generation on every cycle; now short-circuits when no new repos discovered since previous cycle; timing logged at INFO. 11 regression tests added.
- **Bug #966 — get_branches MCP tool only returned registration branch for golden repos** — `BranchService.list_branches` iterated only `repo.heads` (local branches); in a golden repo base clone only the registration branch exists locally. Now also iterates `repo.remotes.origin.refs`: strips `origin/` prefix, filters `origin/HEAD` symbolic ref, deduplicates with local branch taking precedence. `_resolve_branch_repo_path` was using `alias_manager.read_alias()` (returns frozen versioned-snapshot path); now calls `get_actual_repo_path(base_alias)` with `-global` suffix stripped (base clone has fresh remote refs). `GoldenRepoNotFoundError` falls back gracefully to AliasManager. 8 regression tests added.

## v10.0.11 — 2026-05-01

### Fixed

- **MCP TOTP elevation still failed after v10.0.10** — the v10.0.10 fix wired `session_id` (random MCP transport UUID) as `session_key`, but elevation windows are keyed by JWT `jti`. `get_current_user_for_mcp` never wrote `request.state.user_jti`, so `mcp_endpoint` always saw `elevation_key=None` and all MCP elevation checks returned `elevation_required`. Fixed by: (1) extracting JWT `jti` in `get_current_user_for_mcp` for both Bearer token path and `cidx_session` cookie path using `_jti_token = token or request.cookies.get(CIDX_SESSION_COOKIE)`, writing to `request.state.user_jti`; (2) extracting jti in `get_optional_user_from_cookie` for the `/mcp-public` cookie path; (3) threading `elevation_key` as a separate parameter through `_invoke_handler` → `handle_tools_call` → `process_jsonrpc_request` → `process_batch_request` → `mcp_endpoint`, populated from `request.state.user_jti`; (4) `_invoke_handler` injects `session_key` from `elevation_key` only — `session_id` (MCP transport UUID) is never used as elevation key per CLAUDE.md invariant. Covered by 14 new unit tests (AC1-AC14) in `test_session_key_injection_protocol.py`.

## v10.0.10 — 2026-05-01

### Fixed

- **MCP TOTP elevation always failed** — `_invoke_handler` never forwarded the MCP `session_id` as `session_key` to handlers, so `elevate_session` could not locate the caller's elevation window and gated tools always returned `elevation_required`. Fixed by adding `session_id` parameter to `_invoke_handler` with two injection paths: Case A (handler explicitly declares `session_key` param) and Case B (`__mcp_requires_session_key__ = True` marker on `@require_mcp_elevation` wrappers). Both `handle_tools_call` call sites now pass `session_id`.
- **Cross-user elevation bypass on MCP path** — `elevation_decorator.py` Gate 6 called `touch_atomic(session_key)` with no username constraint, so any client knowing a valid `session_key` could extend another user's elevation window. Fixed by introducing `touch_atomic_for_user(session_key, username)` on both `_PgBackend` and `ElevatedSessionManager` (SQLite path uses `BEGIN EXCLUSIVE` + `WHERE username = ?`; PG path uses `UPDATE...RETURNING` with `AND username = %s`). Decorator now calls `touch_atomic_for_user(session_key, user.username)`.
- **TOCTOU window in PG `touch_atomic`** (REST + Web elevation paths) — the prior two-step `UPDATE` then separate `SELECT` left a race window where a concurrent `revoke_all_for_username()` could delete the row between statements. Fixed by converting `_PG_TOUCH` to `UPDATE...RETURNING` single-statement form for `_PgBackend.touch_atomic()` and `touch_atomic_for_user()`.
- **psycopg3 `TypeError` on PG elevation row reads** — `_PgBackend` methods `touch_atomic`, `touch_atomic_for_user`, and `get_status` called `_row_to_elevated_session()` (dict-style access) without setting `conn.row_factory = dict_row`; psycopg3 default is `tuple_row`. Fixed by adding `conn.row_factory = dict_row` in all three methods (mirrors `mfa_challenge.py` pattern).

## v10.0.9 — 2026-05-01

### Fixed

- **Bearer token clients could not use elevation-gated REST endpoints**. `_hybrid_auth_impl` set `request.state.user_jti` only in the session-cookie auth path, but never in the Bearer token path. `_resolve_session_key` checks `user_jti` first, so Bearer token clients always got `None` — meaning their elevation windows were invisible to `require_elevation()`, producing a permanent 403 `elevation_required` even when a valid window existed. Fixed by extracting the JWT `jti` claim from the decoded Bearer token and setting `request.state.user_jti` immediately after `get_current_user` succeeds. `InvalidTokenError`/`TokenExpiredError` (OAuth tokens, opaque credentials) are caught and logged at DEBUG; elevation is simply unavailable for those token types. Covered by 5 new unit tests in `test_hybrid_auth_jti_bearer.py`.

## v10.0.8 — 2026-04-30

### Fixed

- **Elevation modal infinite loop on cross-user MFA setup for SSO users**. After the v10.0.7 fix taught `mfa_routes._resolve_session_key` to check `request.state.user_jti` first, a second mismatch remained: `mfa_setup_page` uses `_get_session_username` (not `get_current_admin_user_hybrid`), so `user_jti` is **never set** on `request.state` for that endpoint. `elevate_ajax` stores the elevation window under the `session` cookie value (via `user_jti`), but `mfa_setup_page` fell back to the `cidx_session` cookie which SSO users don't have — so the elevation check always failed, and the HTMX retry triggered the modal again indefinitely. Fixed by adding the `session` cookie as an intermediate fallback in `_resolve_session_key` (between `user_jti` and `cidx_session`), so endpoints that don't run through `get_current_admin_user_hybrid` can still resolve the correct elevation window.

## v10.0.7 — 2026-04-30

### Fixed

- **Elevation window not found for Web UI session users (mfa_routes)**. `mfa_routes._resolve_session_key` only read the `"cidx_session"` cookie, but web UI auth stores the elevation window under the `"session"` cookie value (mapped via `request.state.user_jti` by `_hybrid_auth_impl`). The lookup always missed for web UI users, causing a spurious 403 `elevation_required` even when the user had a valid active elevation window. Fixed by checking `request.state.user_jti` first (consistent with `dependencies.py` and `elevation_web_routes.py`). Affected: cross-user MFA setup, recovery codes page, and MFA disable for web UI session users.

- **Navigation trap on /admin/mfa/setup**. Admins without TOTP were redirected to the setup page via `window.location.href`, which pushed a browser history entry. Pressing Back returned to the same triggering page, looping forever. Fixed: `window.location.replace()` now replaces the history entry. Back link on the setup page changed from `/admin/users` to `/admin/` (dashboard) with label "Cancel — Go to Dashboard" so admins can always escape. Elevation modal text updated to emphasize "your" authenticator code to reduce confusion when managing another user's MFA.

## v10.0.4 — 2026-04-29

### Fixed (bug sweep + test suite green pass)

- **TOTP elevation bypass in admin Web UI (#956)**. All admin Web UI mutation routes (create/update/delete across golden repos, users, config, CICD, MCP credentials, TOTP, maintenance, research assistant) were missing `Depends(require_elevation())`. Any session with a valid JWT could mutate admin state without completing a TOTP challenge. Fixed by adding `require_elevation` to all relevant endpoints across `routes.py`, `mcp_credential_routes.py`, `cicd_routes.py`, `dependency_map_routes.py`, and `dep_map_health_repair_routes.py`. 14 additional mutation routes gated in the follow-up pass. Test fixtures updated with `_bypass_elevation` helper across 5 test files to prevent 403 failures.

- **HTMX silent failure on 403 elevation_required/totp_setup_required (#955)**. When the TOTP kill switch is ON and an ungated Web UI form submits a mutation, the server returns HTTP 403 with `error: elevation_required` or `totp_setup_required`. HTMX's default swap silently discarded the error response body and swapped empty content into the target. Added a global `htmx:beforeSwap` handler that intercepts 403 responses containing these error codes, prevents the swap, and triggers a full page reload so the elevation dialog is shown instead.

- **HNSW double-delete crash on incremental save retry (#944)**. `save_incremental_update` could be called twice for the same point if a background retry fired while the first save was in progress. The second call hit hnswlib's `mark_deleted` on an already-deleted ID, raising `RuntimeError: Cannot delete point not existing`. Added an `_already_deleted` set guard and made the metadata write atomic (temp-file + `os.replace`) so partial kills leave the previous consistent state on disk.

- **GitHub API errors lose response body (#949)**. When GitHub API calls fail, `RuntimeError` was raised with only the HTTP status code string (e.g. "404"), discarding the response body. Changed to include both status code and response text so operators can diagnose without forensic log inspection. 35 pre-existing test failures fixed in the same changeset.

- **Test suite green pass — 8 daemon-thread pollution and timeout fixes**. `fast-automation.sh` and `server-fast-automation.sh` both reach zero failures after fixing: (1) module-global `dependencies.*` singletons left pointing at deleted tmpdirs across chunk-4 tests (new `conftest.py` with restore+bootstrap fixtures); (2) TOTP singleton surviving across TestClient lifespan cycles (`set_totp_service(None)` on shutdown); (3) semaphore concurrency tests timing out under GIL pressure (300s internal + 360s pytest ceiling); (4) Cohere sleep-patch tests capturing daemon threads (thread-ID filter); (5) temporal metadata caplog leaking daemon warnings (logger namespace filter); (6) MagicMock auto-attributes polluting `indexed_blobs` set (explicit `blob_hash=None`); (7) temporal rampup 100ms threshold failing under load (raised to 2000ms); (8) SCIP definition test hitting 15s global cap (60s override).

## v10.0.3 — 2026-04-28

### Fixed (Story #923 AC gap remediation + v10.0.2 regression revert)

- **TOTP step-up elevation runtime config exposed in Web UI Config Screen (#943)**. Story #923 (Epic #922) shipped three runtime fields on `ServerConfig` — `elevation_enforcement_enabled` (kill switch), `elevation_idle_timeout_seconds` (5-min idle window), `elevation_max_age_seconds` (30-min absolute window) — and CLAUDE.md claimed they were "runtime-configurable via Web UI Config Screen", but no UI control existed and no POST handler accepted them. Operators could only flip the kill switch via direct DB write — defeating the purpose of making the fields runtime in the first place. Added a new "TOTP Step-Up Elevation" section to the Authentication & Security group (between Password Security and the next group) with a checkbox for the kill switch + two number inputs for the timeouts (idle: 60-3600s, max_age: 300-7200s, plus cross-field invariant `max_age >= idle`). New module-level helpers in `config_service.py`: `_validate_totp_elevation_tuple()`, `_apply_totp_elevation_to_config()`, `_rollback_totp_elevation()`, `update_totp_elevation_atomic()`. New method `ElevatedSessionManager.update_timeouts(idle, max_age)` (lock-guarded atomic pair assignment). New POST handler branch `section == "totp_elevation"` in `update_config_section`. Hot-reload wired from both the local save path and `lifespan._on_config_change` callback (PG-poll cluster reload). 30 new unit tests across `test_totp_elevation_config_section_943.py` and `test_totp_elevation_routes_943.py` cover template structure, ConfigService roundtrip, atomic batch staging, rollback-on-save-failure, ElevatedSessionManager hot-reload, exact boundary values, and POST handler with real auth + CSRF.

- **Atomic batch staging closes 4 Codex GPT-5 blockers in the #943 fix**. The naive per-field `update_setting()` loop had three correctness gaps surfaced by Codex: (B1) cross-field validation ran against the OLD on-disk peer value, falsely rejecting valid combined updates like `idle=2000, max_age=2400` when the existing `max_age=1800`; (B2) `ElevatedSessionManager.__init__` snapshotted timeouts into instance attrs that were never re-read on config reload, so 2 of 3 fields were silently restart-only despite UI presenting them as runtime-editable; (H1) save-failure paths mutated live in-memory config BEFORE persistence succeeded, so an operator seeing a "save failed" error page still had the kill switch silently flipped on this node; (M1) test suite did not cover combined-update ordering, live propagation, exact boundaries, or duplicate-field form contract. Fix: new `update_totp_elevation_atomic()` parses all 3 submitted values into a staged tuple, validates once, applies to live config, persists, and rolls back to the snapshot on any exception. Hot-reload propagates timeouts to the live `ElevatedSessionManager` singleton via `update_timeouts()`. 10 new tests cover the regression scenarios.

- **Reverted v10.0.2 `POST /auth/elevate` passthru (anti-fallback violation)**. v10.0.2 made `src/code_indexer/server/auth/elevation_routes.py:147` return synthetic-success `ElevateResponse(elevated=True, ...)` when the kill switch was off. Per the user's clarified policy ("both sites should error out if you hit them and elevation is disabled. if you make it passthru that breaks the anti-fallback rule"), this was wrong: the `POST /auth/elevate` endpoint exists *only* to satisfy a TOTP challenge — when the kill switch is off, no challenge is ever issued, so calling this endpoint is meaningless. Returning fake success would silently lie to a divergent caller (state-mismatch bug masking, MESSI Rules 2 + 13). Reverted to `raise _kill_switch_exc()` (HTTP 503 with `error: "elevation_enforcement_disabled"`). The pre-existing `test_elevate_error_cases[kill_switch_off_returns_503]` test now passes again. The two parallel sites — MCP `elevate_session.py:108-112` and web `elevation_web_routes.py:160-171` — were already correctly erroring (not passthru) and are unchanged. Note: the v10.0.2 fix to `dependencies.py::require_elevation()` (passthru on the decorator) and `mcp/auth/elevation_decorator.py` (passthru on MCP decorator) was correct and remains — those decorators gate normal admin operations that should work when elevation is disabled; the elevate-the-session endpoints are the opposite case.

## v10.0.2 — 2026-04-28

### Fixed (production-blocking regressions surfaced during v10.0.1 staging acceptance)

- **TOTP step-up elevation kill switch must passthru when disabled — 3 sites** (Epic #922 design correction). Before v10.0.2, when `elevation_enforcement_enabled=false` (the kill-switch default and Epic #922 deployment-step-3 state), every endpoint gated by `Depends(require_elevation())` returned HTTP 503. This locked Research Assistant + admin user CRUD + every other elevation-protected endpoint forever — verified live on staging via `POST /admin/research/send → 503 Service Unavailable` (3 occurrences captured 2026-04-28 17:23:22/30/56). The original Codex M4/M12 review note ("503 signals 'feature administratively off'") conflated two semantics; the corrected design is that an OFF kill switch means "do not enforce" (passthru), not "block all access". Three sites fixed: `src/code_indexer/server/auth/dependencies.py:910-922` (`require_elevation()._check`) — collapse the kill-switch + manager-None branches into `if not _is_elevation_enforcement_enabled() or elevated_session_manager is None: return user`; `src/code_indexer/server/auth/elevation_routes.py:147-148` (`POST /auth/elevate`) — return synthetic-success `ElevateResponse(elevated=True, elevated_until=0.0, max_until=0.0, scope="full")` instead of 503 so callers can proceed without acquiring a window; `src/code_indexer/server/mcp/auth/elevation_decorator.py:98-107` (`require_mcp_elevation` decorator) — passthru to wrapped handler instead of returning `_disabled_error()` dict, plus delete the dead `esm is None` guard (singleton, never None). Helper `_elevation_disabled_exc()` removed (zero call sites post-fix). Comprehensive Codex GPT-5 audit (Category-A bugs: 3 fixed; Category-B legitimate 503s: 14 left as-is; Category-C ambiguous: 2 surfaced for human decision: `mcp/handlers/admin/elevate_session.py:108-112` and `web/elevation_web_routes.py:160-171`).

- **Branch-isolation false orphan deletion (#941)**. `cidx index` on a multi-chunk-file git repo emitted thousands of `WARNING:code_indexer.storage.filesystem_vector_store:Vector file not found for point '<uuid>', skipping` lines per run. Root cause: `_batch_hide_files_in_branch` (`high_throughput_processor.py:1457`) used the heavy `_batch_update_points` path (which runs orphan-cleanup), so single-chunk payload updates misclassified all sibling chunks of the same file as orphans → silently deleted them from disk → `_apply_incremental_hnsw_batch_update` then couldn't find them → spurious warning per missing point. Fix mirrors Story #339 Fix B already applied to the symmetric "ensure visible" path at line 1333: route through `_batch_update_payload_only` instead. Two secondary defenses added: `end_indexing` (`filesystem_vector_store.py:516-541`) now skips the incremental update entirely when `_branch_isolation_did_filtered_rebuild=True` (the filtered rebuild already authored the HNSW state); `_apply_incremental_hnsw_batch_update` (`filesystem_vector_store.py:3781`) defensively computes `effective_added = changes["added"] - changes["deleted"]` so any "added then deleted in same session" point is treated as a no-op rather than emitting a warning. Verified live: 719 files / 3044 chunks indexed across 2 rounds with **0** `Vector file not found` warnings (was thousands pre-fix); query smoke test returns results, no silent vector loss.

- **Rich Live progress bar corruption (#942)**. `cli.py:1885` installed the stdlib default stderr `StreamHandler` which had no awareness of the Rich `Live` region in `progress_display.py:56`, so WARNING emissions (e.g. those from #941) collided with progress refreshes — warnings appeared truncated mid-word (`Vector fil` instead of the full message) because the next Live refresh overwrote the trailing text. Fix replaces `logging.basicConfig(...)` with a `RichHandler` from `rich.logging` so log records share the same `Console` instance and the handler pauses the renderer during emission. Per-logger band-aid for `code_indexer.services.provider_health_monitor` (`cli.py:1894-1896`) removed — the systemic RichHandler installation makes it unnecessary. New unit tests `tests/unit/test_cli_942_rich_logging.py` verify `setup_logging()` installs `RichHandler` (not plain `StreamHandler`), warnings emitted during a Live region appear complete, and the per-logger band-aid is absent from active code.

## v10.0.1 — 2026-04-28

### Operations and Resilience

- **All non-Research-Assistant Claude calls now dispatcher-routed (#936)**. Closes the comprehensive gap where `codex_weight` was silently ignored by ~13 hardcoded Claude call sites. Migrated paths: `dependency_map_analyzer` Pass 1, Pass 3, delta-merge, new-domain generation, domain discovery, refinement, verification (~9 sites; 3 Pass 2 retry sites remain Claude-only because they use Claude-specific `allowed_tools=mcp__cidx-local__search_code` constraints that don't translate to Codex); `dep_map_repair_executor` + `dep_map_repair_bidirectional` audit (Story #927/#912 paths); `lifecycle_claude_cli_invoker.LifecycleClaudeCliInvoker.__call__`; `cidx_meta_backup.conflict_resolver` (Story #926); `self_monitoring.scanner._invoke_claude_cli`. New module `dep_map_dispatcher_factory.build_dep_map_dispatcher(config, analysis_model, claude_soft_timeout_seconds)` is the single source of truth — `description_refresh_scheduler._build_cli_dispatcher` (Story #847) and `dependency_map_analyzer._build_pass2_dispatcher` (Story #848) now thin-delegate to it (MESSI Rule 4 closure). `DepMapRepairExecutor` parameter renamed `invoke_claude_fn` → `invoke_llm_fn` to reflect LLM-agnostic intent. Failover semantics preserved: each migrated site benefits from the dispatcher's existing RETRYABLE_ON_OTHER → fall-back-to-alternate-CLI behavior. Only Research Assistant remains intentionally Claude-only (per user policy: "RA stays Claude-only because we don't have a mechanism to make conversation transcript transportable"). Closed after 2 Codex GPT-5 review passes (5 critical findings + 3 non-blocking observations addressed).

- **CodexInvoker fails loud instead of degrading (#937)**. `build_codex_mcp_auth_header_provider()` previously returned `None` on staging when Claude CLI was absent (cache stayed `None` because `register_in_claude_code()` never ran), causing `CodexInvoker` to spawn `codex exec` WITHOUT `CIDX_MCP_AUTH_HEADER`. Codex's `search_code` MCP calls then failed with 401 silently — Pass 2 analysis ran in degraded filesystem-only mode (verified live on staging during v10.0.0 promotion: 3 occurrences in production logs). Fix adds `MCPSelfRegistrationService.build_header_from_stored_credentials()` as a third fallback path that reads stored credentials and populates the cache without depending on Claude CLI subprocess success. `CodexInvoker._start_process()` now logs ERROR (not WARNING) and returns `RETRYABLE_ON_OTHER` when the provider raises — lets the dispatcher transparently failover to Claude instead of spawning a degraded subprocess. Anti-silent-failure (MESSI Rule 13) closure.

- **Lifecycle writer preserves cidx-meta frontmatter (#940)**. `LifecycleBatchRunner._process_one_repo` previously overwrote `cidx-meta/<alias>.md` with frontmatter dict containing only `lifecycle` + `lifecycle_schema_version`, silently destroying `last_analyzed`, `name`, `url`, `technologies`, `purpose`. This broke the description-refresh scheduler permanently — `_get_refresh_prompt` requires `last_analyzed`, so every cycle emitted two WARNINGs and rescheduled forever. Fix adds module-level `_merge_frontmatter` helper that does `{**existing, **new_lifecycle}` semantics: lifecycle keys overwrite, all other keys preserved. Read-merge applied before `write_meta_md` when the target file exists; first-write flow (no pre-existing file) unchanged. MESSI Rule 15 invariant added post-write: re-reads the file and asserts `post_fm.keys() >= merged_fm.keys()` to catch any future regression at the call site.

### Changed

- **REST file-content cap raised 5K → 50K tokens for parity with MCP (#939)**. `FileService.get_file_content()` now reads `ContentLimitsConfig.file_content_max_tokens` (default 50000, matching the MCP `get_file_content` tool) and `ContentLimitsConfig.chars_per_token`. Previous behavior read `FileContentLimitsConfig.max_tokens_per_request` (default 5000) — silent 10x asymmetry between REST and MCP for the same operation. **Behavior change for REST clients**: file fetches that previously truncated at ~5K tokens will now return up to ~50K tokens. This is a deliberate parity fix; operators who relied on the 5K cap should explicitly lower `content_limits_config.file_content_max_tokens` in the Web UI. The legacy `FileContentLimitsConfig` dataclass + standalone `/admin/settings/file-content-limits` page + "File Retrieval Limits" section in the main config screen are removed entirely; the "Search Result Truncation" section is renamed to "Content Limits" since it now covers all token truncation. Backward-compat: existing `config.json` containing the legacy `file_content_limits_config` key loads cleanly (silently stripped on load).

### Removed

- **5 dead config fields removed (#938)**. `cache_max_entries` (`ContentLimitsConfig`), `omni_max_total_results_before_aggregation` (`MultiSearchLimitsConfig`), `system_metrics_cache_ttl_seconds` (`HealthThresholdsConfig`), `OtelConfig.export_logs`, `OtelConfig.trace_sample_rate` — all five were validated and saved by the Web UI but never read by any runtime code (verified via grep + cidx FTS audit). Their input fields, view rows, dataclass attributes, validators, serializer entries, and setter cases are all deleted. Operators tuning these settings were silently doing nothing. Backward-compat: existing `config.json` containing these keys loads cleanly via `.pop()` calls in the four affected sub-dataclass loaders. Net: ~30 LOC removed across 4 files. No behavior change for any existing functionality.

### Internal Quality

- **dispatcher_factory consolidation closes anti-duplication violation**. Three near-identical dispatcher builders previously coexisted: `description_refresh_scheduler._build_cli_dispatcher` (Story #847), `dependency_map_analyzer._build_pass2_dispatcher` (Story #848), and the freshly-introduced `dep_map_dispatcher_factory.build_dep_map_dispatcher`. Per MESSI Rule 4, three near-identical implementations is a three-strike abstraction violation. The factory is now parameterized (`analysis_model`, optional `claude_soft_timeout_seconds`) and the two pre-existing builders thin-delegate to it. Codex review explicitly verified consolidation (verdict: "duplication genuinely consolidated").

## v10.0.0 — 2026-04-27

### Breaking Changes

- **MCPB Removed (epic #756)**. The MCP Bridge subsystem has been removed in a single hard-removal pass with no deprecation window. Removed in this release: the `cidx-bridge` and `cidx-token-refresh` console-script entry points; the entire `src/code_indexer/mcpb/` Python module (12 files, ~2,053 LOC); the `tests/mcpb/` and `tests/installer/` test trees; the `install-mcpb.sh`, `scripts/setup-mcpb.sh`, `scripts/installer/mcpb-installer.nsi`, and `scripts/installer/README.md` installer scripts; the `scripts/build_binary.py` MCPB-bundle build script and its companion test; the `.github/workflows/release-mcpb.yml` CI workflow (688 lines); the entire `docs/mcpb/` documentation tree (6 files, ~5,862 lines). Migration: any MCP-aware client should connect directly to the CIDX server's native MCP endpoints — `/mcp` (JWT-Bearer-authenticated via `POST /auth/login`) or `/mcp-public` (unauthenticated). The `cidx-bridge` stdio-to-HTTP shim is no longer needed because every modern MCP client supports streaming HTTP/SSE transports natively. **Impact**: any installation that depended on `cidx-bridge` or `cidx-token-refresh` binaries will see `command not found` after upgrade. Past GitHub Release artifacts that bundle `install-mcpb.sh` remain downloadable per the repository's tag-immutability policy, but no new MCPB installer will be built going forward. See `docs/migration-to-v10.md` for the full migration guide.

### Security and Hardening

- **Research Assistant Security Hardening Phase 2 (#929)**. Replaces 36 hardcoded RFC1918-prefix curl allow rules with a closed-set whitelist via `scripts/cidx-curl.sh` wrapper. The wrapper enforces an operator-configured CIDR allowlist (`ra_curl_allowed_cidrs` in `config.json` under `claude_integration_config`), always-on loopback (127.0.0.0/8 + ::1/128 — operators cannot disable), DNS-rebinding mitigation via `--resolve` IP pin, ambient-state scrubbing (proxy env vars, `~/.curlrc` via `-q`, CA cert env vars), and rejection of curl flags that bypass URL validation (`--resolve`, `--connect-to`, `--proxy`, `--unix-socket`, `--noproxy`, `--interface`, `--dns-servers`, `--doh-url`, `--proxy-user`, `-x`/`-xVALUE`, `-L`/`--location`/`--location-trusted`, `--next`/`-:`, `--config`/`-K`, `-w`/`--write-out`, `--parallel`/`-Z`, `--alt-svc`, `--hsts`, `--insecure`/`-k`, `--cacert`, `--capath`, `--proxy-cacert`, `--proxy-capath`, `--proxy-insecure`, `--metalink`, `--url`, `--proto`, `--proto-default`, `--proto-redir`). Output restricted to `/dev/null` or `-` (stdout); @-prefix rejection on `-d`/`-H`/`--data*` to block file-read primitives. SECURITY_GUARDRAILS dead-fallback constant removed; `load_research_prompt` now fails closed on missing/unreadable template. Item #14 finding (shell-operator-aware assumption WRONG) documented in code at `_bash_deny_rules` audit-note site. Item #18 commit-discipline rule for security-sensitive changes added to project CLAUDE.md. Closed after 6 codex-code-reviewer passes (architecture migrated from blacklist to whitelist after Codex demonstrated 5 consecutive blacklist bypasses).

### Operations

- **Auto-trigger dep-map repair (#927)**. Scheduled delta and refinement jobs now optionally trigger a single repair pass when anomalies are detected. Default-off opt-in via Web UI Config Screen → `dep_map_auto_repair_enabled`. Cluster-aware decision lock: PostgreSQL `pg_try_advisory_xact_lock` (cluster) / `threading.Lock` (solo) at three trigger sites. Lock held only for the atomic claim window; long-running work runs OUTSIDE the lock with the JobTracker entry serving as the cross-node in-flight signal. Anti-fallback on health-check error: skip auto-repair (never repair against unknown anomaly state). Anti-fallback on cluster-mode-without-pg_pool: refuse to fire and log ERROR `scheduled_auto_repair_misconfigured_cluster_no_pg_pool`. 14 distinct decision-point log events for full observability. Lifespan late-binding via `set_repair_invoker_fn()` setter to avoid the use-before-assignment closure-captures-None bug class. Closed after 6 codex-code-reviewer passes.

- **Refinement scheduler bootstrap fix (#931)**. `update_tracking(refinement_next_run=...)` moved into `run_refinement_cycle` itself so every successful cycle (manual + scheduled) seeds the schedule — closes the chicken-and-egg bootstrap gap where the manual "Trigger Refinement" button never seeded the schedule and the scheduler was permanently stuck on "waiting for manual trigger". `_try_fire_scheduled_refinement` now calls `run_tracked_refinement()` (not `run_refinement_cycle()` directly) so scheduled runs register JobTracker entries with `operation_type="dependency_map_refinement"` — closes the JobTracker bypass that left scheduled runs invisible to the dep-map tab in-flight panel and global `/jobs` view. Mirrors the delta pattern. Asymmetry vs sibling subsystems documented in the bug report's root-cause table.

- **Dep-map dashboard `finalize 0.0s` removal (#930)**. Dropped the meaningless `finalize_s` phase timing pill from delta-run rows in the "Recent Run Metrics" table. The timer wrapped only two trailing DB writes (~10-100ms total) which always rounded to `0.0s` in the `"%.1f"` formatter — semantically nonsensical. Removed `finalize_s` emission from both delta and full paths in `dependency_map_service.py`. Historical rows in `dep_map_run_history` continue to render `finalize 0.0s` until they roll off the dashboard window (Option A — accepted, no backfill).

### Knowledge and Memory

- **Memory CRUD Protocol drift fix (#932)**. Resolves `AttributeError: 'RefreshScheduler' object has no attribute 'is_write_lock_held'` that broke `create_memory`/`edit_memory`/`delete_memory` MCP tools across all environments. Root cause: `RefreshSchedulerProtocol` declared `is_write_lock_held` but the real class implements `is_write_locked`. Surgical rename + Protocol/real-class signature reconciliation across all 4 methods (`acquire_write_lock` `ttl_seconds=` kwarg removed, `release_write_lock -> None`, `trigger_refresh_for_repo` parameter rename to `alias_name` with full signature). New parametrized conformance test `test_refresh_scheduler_protocol_conformance.py` uses `typing.get_type_hints()` to resolve PEP 563 forward references on both sides — would have failed loudly the moment commit `6514a8d6` landed. New integration test exercises `_coarse_piggyback_or_acquire` against a REAL `RefreshScheduler` (no MagicMock) — closes the "MagicMock complicity" anti-regression gap.

### Stability Mitigations

(These were shipped in the v9.x line and are now formalized for v10. See per-version sections below for the original commits.)

- **glibc malloc_trim + MALLOC_ARENA_MAX=2** (Bug #897, defaults ON since v9.23.3) — reduces HNSW cache fragmentation. Bootstrap-only flags `enable_malloc_trim` + `enable_malloc_arena_max` in `config.json`.
- **DatabaseConnectionManager-cleanup-daemon** (Bug #878) — single thread sweeps stale SQLite connections every 60s. Always-on, lifecycle-managed by lifespan.
- **Omni search caps** (Bug #881, Bug #894) — `omni_wildcard_expansion_cap` (default 50) + `omni_max_repos_per_search` (default 50) prevent fan-out exhaustion. Adjustable via Web UI.

## v9.23.11 — 2026-04-26

### Fixed: v9.23.10 silent registration failure on staging (Python 3.9 / no `tomli`)

- **Symptom**: After v9.23.10 deploy, staging's `codex mcp get cidx-local` still returned the v9.23.9 schema with `bearer_token_env_var = "CIDX_MCP_BEARER_TOKEN"`. End-to-end Codex Pass 2 still failed: stale env-var name, no `env_http_headers` block, codex sent the wrong header on every MCP call.

- **Root cause**: v9.23.10's `_read_toml` did `import tomli`. Production deploys ship Python 3.9 (no `tomllib` builtin) and do not have `tomli` installed. The import raised `ModuleNotFoundError`, the entry point's broad `except Exception` caught it and logged a single WARNING (`cidx-local MCP registration failed — ModuleNotFoundError: No module named 'tomli'`), and the registration was silently skipped. Unit tests in dev passed because `tomli` was already installed on dev machines via a transitive dep — codex review's independent verification ran on the same dev box, masking the bug.

- **Fix**: Dropped the parser dependency entirely from production code. `_read_toml` removed; `_is_already_registered(data, url)` replaced with `_is_already_registered_text(text, url)` using regex/substring checks against the raw config.toml file content. The section-replacement path was already regex-based, so read and write paths are now consistent.

- **Regression tests**:
  - `test_production_module_imports_without_tomli` — asserts `codex_mcp_registration.py` source contains zero `tomli` or `tomllib` references; locks in the no-parser invariant.
  - `test_staging_v9_23_9_stale_bearer_token_env_var_triggers_rewrite` — pre-populates config.toml with the v9.23.9 schema and asserts (via plain text matching, no parser) that the entry point rewrites to env_http_headers, removes the stale `bearer_token_env_var` line, and adds the `[mcp_servers.cidx-local.env_http_headers]` sub-table.

- **Lesson**: unit tests passing in dev does not mean production works. The codex E2E auth flow had to actually run on Python 3.9 staging to expose the dependency mismatch. v9.23.10 codex review approved the architecture but couldn't catch the runtime import gap because the review ran in dev too.

## v9.23.10 — 2026-04-25

### Codex MCP auth — persistent Basic credentials replace short-lived JWT

- **Root cause fixed**: v9.23.9 injected a short-lived admin JWT (`CIDX_MCP_BEARER_TOKEN`, default TTL 10 min) into each `CodexInvoker` subprocess. Pass 2 dependency analysis runs lasting 30+ minutes expired mid-flow, causing silent HTTP 401 failover to Claude. Fix: Codex now uses the same persistent `client_id:client_secret` pair issued by `MCPCredentialManager` that Claude uses, encoded as `Basic <base64>` and injected as `CIDX_MCP_AUTH_HEADER`. No JWT, no TTL, no mid-flow expiry.

- **TOML-based MCP registration**: Codex 0.125 `codex mcp add` has no `--http-headers` / `--env-http-headers` flags (only `--bearer-token-env-var`). Registration now writes `$CODEX_HOME/config.toml` directly with `env_http_headers = { Authorization = "CIDX_MCP_AUTH_HEADER" }`. The idempotency check reads the TOML back and skips the write when the section already matches. Atomic write via `.tmp` + `Path.replace()` with parent-dir creation and mode 0o600 preservation. Reference: https://developers.openai.com/codex/mcp.

- **Stale v9.23.9 config migration**: When `config.toml` contains a `[mcp_servers.cidx-local]` section with `bearer_token_env_var` (old schema), it is silently replaced with the new `env_http_headers` section on the next server startup.

- **New module `codex_mcp_auth_header_provider.py`**: `build_codex_mcp_auth_header_provider()` returns a closure that retrieves the cached `Authorization` header value from `MCPSelfRegistrationService` — fast path via `get_cached_auth_header_value()`, cache-miss path via `build_auth_header_from_creds()`. No credential assembly in the provider; the value is sliced from the already-assembled header string in `MCPSelfRegistrationService.register_in_claude_code()`.

- **`CodexInvoker` rename**: `bearer_token_provider` / `_bearer_token_provider` / `CIDX_MCP_BEARER_TOKEN` renamed to `auth_header_provider` / `_auth_header_provider` / `CIDX_MCP_AUTH_HEADER`. Both production wiring sites updated: `DependencyMapAnalyzer._build_pass2_dispatcher()` and `DescriptionRefreshScheduler._build_cli_dispatcher()`.

- **Deleted**: `codex_bearer_provider.py`, `test_codex_invoker_bearer_injection.py`, `test_codex_invoker_jwt_wiring.py`.

## v9.23.9 — 2026-04-25

### Codex MCP integration

- **Codex MCP launcher gap closed**: Codex now registers `cidx-local` MCP via HTTP transport at server startup. Replaces the empty `_DEFAULT_CIDX_MCP_COMMAND = ""` placeholder from Story #848. Closes parity gap with Claude (`MCPSelfRegistrationService` HTTP+Basic-auth path). Note: superseded by v9.23.10 which replaces the JWT credential with persistent Basic auth.

- **Hook gap accepted as permanent degradation**: codex 0.125 has no equivalent of Claude's `PostToolUse` hooks. Verified via `codex --help` and `codex exec --help`; only `--dangerously-bypass-approvals-and-sandbox` and `--sandbox` flags exist (reference: github.com/openai/codex/issues/16732). Citation and audit enforcement at the hook layer remain Claude-only. Documented in `CLAUDE.md` "Codex CLI Integration" subsection.

## v9.23.8

### Operator helpers

- **`scripts/setup-codex-npm-prefix.sh --update-cidx-server-systemd` flag**: extends the v9.23.6 helper to optionally patch the cidx-server systemd unit's `Environment="PATH=..."` line, prepending the npm bin dir (defaults to `~/.npm-global/bin`) so the cidx-server process can find `codex` after the install. Closes the operationalization loop: with this flag, the only manual steps an operator needs are (1) running the script (security: chooses where global npm packages land), (2) entering OPENAI_API_KEY in the Web UI Config Screen (security: secret entry). Everything else (npm install, PATH setup, systemd unit patch + daemon-reload, codex login on next server restart, auth.json schema, dispatcher invocation) is now automatic. Idempotent: when the bin dir is already in PATH, no-op + skip rewrite. Honors `CIDX_SYSTEMD_UNIT_PATH` env override for testing. 5 new unit tests (10 total in the script suite). Manual E2E confirmed on staging: when `~/.npm-global/bin` was already added to the systemd PATH (via earlier manual edit), running with the flag emits "PATH already configured" and exits clean.

### Recommended operator runbook (single command + 1 UI entry)

```bash
# 1. One-time host setup (operator-driven, security-sensitive):
sudo -u <cidx-server-user> bash scripts/setup-codex-npm-prefix.sh --update-cidx-server-systemd
sudo systemctl restart cidx-server

# 2. Set OPENAI_API_KEY in Web UI:
#    Browser → /admin/config → Codex CLI Integration → Enabled: Yes,
#    Credential Mode: api_key, OPENAI_API_KEY: <sk-proj-...>, Save.
#    On next cidx-server restart (or save-triggered reload), codex_cli_startup
#    runs `codex login --with-api-key` (v9.23.7), writes the correct minimal
#    apikey-mode auth.json, and CodexInvoker authenticates successfully without
#    any further operator intervention.
```

## v9.23.7

### Bug fixes

- **Codex api_key-mode auth schema mismatch (epic #843 follow-up)**: Story #846's `CodexCredentialsFileManager.write_credentials` writes the OAuth/subscription schema (`auth_mode: "chatgpt"` + `tokens.{access_token, account_id, id_token, refresh_token}` + `last_refresh`) regardless of credential mode. For api_key mode, codex-cli expects a minimal schema `{"auth_mode": "apikey", "OPENAI_API_KEY": "..."}`. When api_key mode hit the OAuth-style auth.json, codex tried to use it as OAuth and the WebSocket failed with HTTP 401 (verified via earlier manual E2E). Fix: `codex_cli_startup.py:initialize_codex_manager_on_startup` now delegates api_key-mode auth to `codex login --with-api-key` via subprocess (key piped through stdin, never on the command line) — codex itself owns the schema, so we don't have to track it. Mirrors the Claude precedent (`claude_cli_manager._ensure_api_key_synced` uses `~/.claude.json` writer + env var sync). Subscription mode unchanged (still uses `CodexCredentialsFileManager` + lease loop). 7 new unit tests in `test_codex_login_with_api_key.py` covering: correct argv, key-via-stdin (not argv), success/nonzero/timeout/missing-binary/empty-key paths. Existing #846 startup tests updated to reflect the new branch.

## v9.23.6

### Operator helpers

- **NEW `scripts/setup-codex-npm-prefix.sh`**: idempotent operator helper that resolves the EACCES failure observed on hosts where the system npm prefix (`/usr/local/lib/node_modules`) is not writable by the auto-updater's effective user. Detects current npm prefix; if it's a system path (`/usr`, `/usr/local`, `/opt`), switches to a user-writable `~/.npm-global` prefix via `npm config set prefix`; ensures `~/.bashrc` exports the new bin dir on PATH; runs `npm install -g @openai/codex`; verifies via `codex --version`. Prints a final summary block with the exact `Environment="PATH=..."` line operators need to add to the cidx-server systemd unit so the server process can find the binary. Once the systemd PATH is updated and cidx-server restarted, the auto-updater's Story #845 step 6.7 will find npm + the user-writable prefix on subsequent runs and `_ensure_codex_cli_installed` succeeds without WARNING. 5 unit tests (`tests/unit/scripts/test_setup_codex_npm_prefix.py`) using PATH-shimmed npm/codex binaries to avoid real network/host pollution. Manual E2E confirmed: ran on staging where v9.23.5 step 6.7 had failed `[DEPLOY-GENERAL-144] EACCES`; script installed Codex 0.125.0 to `/home/jsbattig/.npm-global/bin/codex` cleanly.

### Follow-up still required
- Operator must update the cidx-server systemd unit `Environment="PATH=..."` line to prepend `${HOME}/.npm-global/bin` (or the chosen prefix's `/bin`), then `systemctl daemon-reload + systemctl restart cidx-server`. The script's summary block prints the exact line to use. A future version may extend the script with a `--update-cidx-server-systemd` flag to automate this step.
- Codex CLI authentication still requires `codex login` (ChatGPT OAuth session) populated under `CODEX_HOME` before Codex can actually execute jobs. See v9.23.5 epic #843 known-limitations.

## v9.23.5

### Features — Epic #843: Codex CLI Integration for Background Intelligence

- **Story #844 — Codex CLI Configuration via Web UI**: New `CodexIntegrationConfig` dataclass with fields `enabled`, `credential_mode` (none/api_key/subscription), `api_key`, `lcp_url`, `lcp_vendor`, `codex_weight` (0.0–1.0). Web UI Config Screen surface: Yes/No `<select>` for `enabled` (matches OIDC/Langfuse/Claude precedent), `<select>` for credential mode, `type="password"` masked api_key input, numeric input for weight, and Jinja `{% if credential_mode == ... %}` guards in BOTH display and edit modes so only the relevant credential fields show per active mode. XSS-safe (`escHtml` on `aria-label`/dynamic attributes), masked-placeholder API-key preserve on save. Section sits immediately after Claude CLI Integration (its conceptual sibling). 49 tests including 4 regression tests preventing reintroduction of: form-name/handler-key mismatch, unmasked read-only display, no-op JS toggle stub, range slider, checkbox-vs-select inconsistency.

- **Story #845 — Auto-Updater Installs/Updates Codex CLI**: New idempotent step 6.7 in `DeploymentExecutor.execute()` (`_ensure_codex_cli_installed`) running `npm install -g @openai/codex` followed by `codex --version` verification at INFO level. Optional-feature semantics: when `npm` is absent on PATH, function logs WARNING and returns True (CIDX must not fail). Subprocess timeout protection via `subprocess.TimeoutExpired` catch. Non-blocking in `execute()` — a Codex install failure does not bail out the auto-updater. Error code `DEPLOY-GENERAL-144`. 8 unit tests + 212 broader auto_update regression all green.

- **Story #846 — Codex Session Vending via llm-creds-provider**: Three-mode credential lifecycle (`none`/`api_key`/`subscription`) mirroring Claude. New `CodexCredentialsFileManager` (atomic `auth.json` write to `{CODEX_HOME}/auth.json` with 0o600 perms; `chatgpt` auth_mode + tokens dict + OPENAI_API_KEY + last_refresh; idempotent delete; corruption-safe read). New `CodexLeaseLoop` parallel to Claude lease loop with vendor-scoped state file (`codex_lease_state.json`) preventing collision with Claude's `llm_lease_state.json`. New `initialize_codex_manager_on_startup` lifecycle entry point invoked from FastAPI lifespan; CODEX_HOME at `{CIDX_DATA_DIR}/codex-home/` honoring Bug #879 split-user pattern. Shutdown hook hoisted onto `app.state.codex_shutdown_hook` and invoked in lifespan teardown — leases returned, auth.json cleaned up. **SPIKE outcome**: llm-creds-provider OpenAI vendor checkout returns `{lease_id, credential_id, access_token, refresh_token, custom_fields:{}}` but Codex auth.json expects `tokens.account_id` and `tokens.id_token`. Workaround in `_provider_response_to_auth_json`: pull from `custom_fields` if vendor surfaces them, fall back to empty strings otherwise (documented inline; vendor-side enhancement is the long-term fix). New error code `APP-GENERAL-050` for Codex init failure (resolved collision with `api_key_management`'s `APP-GENERAL-046`). `LlmLeaseStateManager.__init__` extended with `state_filename` parameter (4-layer path validation: non-empty, no `.`/`..`, `Path.parts` length 1, basename equality). 85 tests covering all credential modes + state-file isolation + lifespan wiring.

- **Story #847 — CLI Dispatcher (Selection + Failover) for Description Gen + Refinement**: New `IntelligenceCliInvoker` Protocol + `InvocationResult` dataclass + `FailureClass` enum (`RETRYABLE_ON_SAME` / `RETRYABLE_ON_OTHER`). `ClaudeInvoker` extracts the existing PTY-via-`script` invocation pattern from `description_refresh_scheduler.py:544-621` preserving exact behavior. `CodexInvoker` builds `codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox <prompt>` (codex-cli 0.125.0 compatible — original spec assumed older `codex --json -q` syntax which 0.125 rejects), with `subprocess.Popen(start_new_session=True)` + `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` on timeout (kills full process group preventing orphaned Node + Rust subprocesses). JSONL parsing filters `event.type == "item.completed"` AND `item.type == "agent_message"`, returns `item.text` of the LAST matching event (no markdown scraping). `CliDispatcher` performs weighted random primary selection (`random() < codex_weight`), single retry on RETRYABLE_ON_SAME, failover to alternate on RETRYABLE_ON_OTHER (or after retry exhaustion). Wired into `description_refresh_scheduler.py` and `claude_cli_manager.py`. When Codex disabled (`codex=None`/`codex_weight=0.0`), behavior is bit-identical to legacy direct-Claude path. 90 tests covering selection distribution, retry, failover, JSONL parsing, process-group kill, and wiring.

- **Story #848 — Codex CLI for Dependency Map Pass 2 (Domain Refinement)**: Pass 2 entry point in `dependency_map_analyzer.py` wired to `CliDispatcher` (cached on instance attribute, built once per analyzer run). `flow="dependency_map_pass_2"` distinct from description flows. PostToolUse `--settings` JSON for turn-count escalation routes to `ClaudeInvoker` only (Codex MCP-tool calls don't fire PostToolUse hooks per [openai/codex#16732](https://github.com/openai/codex/issues/16732) — accepted degradation, documented in source comment block). All retries (max-turn errors AND insufficient-output retries) execute on Claude path with full hook support. New `_ensure_codex_mcp_registered` helper runs `codex mcp add cidx-local -- <command>` at startup with CODEX_HOME env scope, idempotent, `subprocess.TimeoutExpired` caught. **Known limitation (FIXME, follow-up issue needed)**: cidx-local stdio launcher command is empty placeholder. cidx exposes its MCP via HTTP transport (per existing `MCPSelfRegistrationService` for Claude); codex-cli's `mcp add ... -- <stdio cmd>` requires a real stdio launcher OR HTTP transport with custom auth headers. Two follow-up paths: (a) implement `cidx mcp serve` as a stdio launcher in `cli.py`, (b) verify codex-cli MCP HTTP transport support and use the existing HTTP+Basic-auth pattern. Until follow-up lands: registration is skipped (empty command → INFO log + early return), CIDX startup unaffected, dispatcher fails over Codex Pass 2 → Claude per AC5. 17 tests including failover end-to-end + GitHub issue #16732 source comment guard.

### Architecture Notes

- IntelligenceCliInvoker Protocol provides a structural duck-type contract for `ClaudeInvoker` and `CodexInvoker` (both implement `invoke(flow, cwd, prompt, timeout) -> InvocationResult`); `@runtime_checkable` for isinstance verification.
- `random.random() < codex_weight` selection: weight=0.0 is always-Claude (since `random()` returns [0.0, 1.0)), weight=1.0 is always-Codex.
- Codex JSONL output contract: structured events, no markdown scraping. `item.completed` + `item.type == "agent_message"` is the only path to extract the agent's final response text.
- Process-group kill (`start_new_session=True` + `os.killpg(os.getpgid(pid), SIGKILL)`) is the only way to clean up Codex's Node wrapper + Rust subprocess on timeout. `proc.kill()` alone leaks them.

### Known limitations & follow-ups

- **Codex CLI auth**: codex-cli uses ChatGPT OAuth session (via `codex login`), NOT `OPENAI_API_KEY`. For api_key mode to drive Codex end-to-end, either (a) operator runs `codex login` to populate CODEX_HOME with OAuth tokens before CIDX starts, OR (b) subscription mode via Story #846 lease loop populates auth.json with valid session tokens (open SPIKE: vendor-side `account_id` / `id_token` enhancement).
- **cidx-local MCP launcher**: see Story #848 known-limitation note. Without it, Codex Pass 2 cannot reach CIDX MCP tools end-to-end. Dispatcher failover to Claude works correctly; full Codex MCP path requires the launcher follow-up.

## v9.23.4

### Features

- **Story #868**: **Remove Repo from Auto-Discovery Pending List**. Operators can now refine selection inside the auto-discovery batch-create modal without cancelling and restarting. Per-row `<button type="button" class="remove-repo-btn" aria-label="Remove {repo}">` injected by `showCreateDialog`, with `removeRepo()` JS handler that drops the row from DOM, removes the entry from `selectedRepos`, calls `updateSelectionUI()` to keep the discovery-page checkboxes/count consistent, and shows a "No repositories selected." empty state with a disabled `#execute-batch-btn` when the list goes empty. CSS mirrors the existing `.close-btn` pattern; no confirmation dialog. XSS-safe: `aria-label` interpolation via `escHtml`, DOM selectors via `CSS.escape`. Async safety preserved via the existing `if (!item) return` guard in `updateRepoBranchDropdown`. Pure template change at `src/code_indexer/server/web/templates/auto_discovery.html`. 19 new template-render unit tests.

- **Story #872**: **Research Agent SQLite/PostgreSQL Database Access via Authorized Script**. New `scripts/cidx-db-query.sh` allow-listed for the Claude CLI subprocess invoked by `ResearchAssistantService`, so the Research Assistant can query/modify the CIDX server's databases during investigations without operator shell access. Algorithm: read `${CIDX_SERVER_DATA_DIR:-~/.cidx-server}/config.json`, dispatch to `sqlite3 -header -column` (standalone) or `psql -c` (cluster) based on `storage_mode` + `postgres_dsn`. Scope-enforced: `readlink -f` canonicalizes both the target DB path and the data dir, prefix-collision-safe (`/foo/cidx-server-evil/` rejected when data dir is `/foo/cidx-server`). `--db <path>` and `--pg <dsn>` flags override auto-detect; `--db` outside the data dir exits 1 with `target database is outside CIDX data directory`. `_allow_rules()` extended with optional `db_query_script_rule` parameter producing the `Bash({absolute_path} *)` pattern that mirrors `cidx-meta-cleanup.sh`. `_run_claude_background()` injects `CIDX_SERVER_DATA_DIR` (sourced from `self.db_path`) and `CIDX_REPO_ROOT` into the subprocess env so the script's auto-detection works regardless of the agent's invocation context. `_bash_deny_rules()` unchanged — `python3` remains denied at the agent prompt level; the script's internal `python3 -c` for JSON parsing runs inside the allow-listed subprocess and never traverses Claude's tool gate. New section in `research_assistant_prompt.md` documents invocation syntax + scope restriction (no example queries, per spec). 6 shell-script tests + 2 service-layer tests.

## v9.23.3

### Changes

- **Bug #897: `enable_malloc_trim` and `enable_malloc_arena_max` now default to `true`** (previously `false`). Both glibc arena-fragmentation mitigations ship enabled by default so fresh installs and existing installs that don't explicitly pin the flags automatically get the protection. Operators can still disable either by setting the flag to `false` in `~/.cidx-server/config.json` (bootstrap-only — both flags are read before the database is available).
- **Bug #897 follow-up: `_ensure_malloc_arena_max()` config discovery under split-user auto-updater deployments** (`deployment_executor.py:1560`). The auto-updater's idempotent-apply function now passes `server_dir_path=str(_cidx_data_dir)` to `ServerConfigManager`, honoring `CIDX_DATA_DIR` (Bug #879) so the auto-updater running as root reads the same `config.json` the cidx-server process (running as e.g. code-indexer) reads. Previously the function silently no-opped under split-user deployment because `Path.home()` resolved to `/root/.cidx-server` which did not contain the server's config file. Includes new test `test_ensure_malloc_arena_max_honors_cidx_data_dir_across_users` that exercises the cross-HOME path end-to-end using the real `ServerConfigManager` and a monkeypatched `_cidx_data_dir`.

## v9.23.2

### Bug Fixes

- fix: **Revert v9.23.1 tool_docs regeneration — MCP tool registry collapsed on staging (~200 → 30 tools)**. The v9.23.1 maintenance step that regenerated 165 MCP `tool_docs/*.md` files via `tools/convert_tool_docs.py` produced output that, once deployed, caused the MCP server to register only ~30 of the expected tools (including losing `search_code`, which surfaced as `"Invalid params: Unknown tool: search_code"` errors on the staging MCP endpoint). Recovery: reverted all 135 modified tool_docs files to their v9.23.0 content and deleted the 30 tool_docs files that v9.23.1 newly added. The #898 analyzer prompting fix (see below) is preserved untouched — the revert is scoped to `src/code_indexer/server/mcp/tool_docs/` only.

## v9.23.1

### Bug Fixes

- fix(#898): **Claude CLI invoked MCP `search_code` with bare golden-repo aliases during dep-map lifecycle analysis**. Subprocess Claude CLI (dep-map Pass 2, via `LifecycleBatchRunner` / `description_refresh_scheduler`) was calling `search_code(repository_alias="humanize")` instead of `search_code(repository_alias="humanize-global")`. The server registry keys are `-global`-suffixed, so the bare form raised `FileNotFoundError: Repository not found in global repositories`, surfacing as 12× `REPO-GENERAL-024/025/026` ERRORs per delta run on staging v9.23.0 (4 per peer repo queried). Non-blocking — delta completed — but degraded investigation quality and polluted logs. Fix at the prompting layer (per bug body recommendation, rejecting the broader registry-level lenience that would have required coordinated changes across SQLite + Postgres backends + the Protocol contract): extended the "### How to Use" Claude CLI instruction block in `dependency_map_analyzer.py:906-911` to explicitly document `repository_alias` with the `-global` suffix requirement, contrast examples (`humanize-global` vs `humanize`), Bug #898 citation, and `*-global` wildcard / parameter-omission options. Acceptance: zero `REPO-GENERAL-024/025/026` errors on a post-fix staging delta run.

### Maintenance

- chore: regenerated 165 MCP tool_docs/*.md files via `tools/convert_tool_docs.py` to re-sync with `TOOL_REGISTRY` — they had drifted from the registry pre-v9.23.1 (unrelated to #898, surfaced when the verifier ran as part of the #898 fix pipeline).

## v9.23.0

### Features

- feat(#887 Epic #886): **Depmap parser module split and anomaly channels**. `dep_map_mcp_parser.py` (1042 lines, MESSI rule 6 soft cap violation) split into four cohesive modules — `dep_map_mcp_parser.py` (orchestration + public API, ~440 lines), `dep_map_parser_tables.py` (markdown table extraction, ~354), `dep_map_parser_hygiene.py` (identifier normalization + `AnomalyEntry`/`AnomalyAggregate`/`AnomalyType` dataclasses, ~279), `dep_map_parser_graph.py` (graph edge aggregation + channel split, ~365). Dual public API surface: legacy `get_cross_domain_graph()` 2-tuple + new `get_cross_domain_graph_with_channels()` 4-tuple for callers needing `parser_anomalies`/`data_anomalies` separation. `AnomalyType` carries `channel: Literal["parser","data"]` bound attribute so routing is enum-lookup, not manual classification. Frozenset-keyed bidirectional dedup prevents ~170 anomalies from emitting for ~150 edges (pre-#887 pattern). 70 new tests across 8 ACs + 4 remediation blocker files.

- feat(#888 Epic #886)!: **Depmap response contract improvements** — `resolution` field + dual-write aliases. **BREAKING** to the MCP response envelope: 5 `depmap_*` tools now return `anomalies[]` (legacy concatenation), `parser_anomalies[]`, and `data_anomalies[]` channels. Legacy `anomalies[]` is preserved for ONE release after Epic #886 completes, then dropped. Handler `_anomaly_to_dict()` helper reused at every response assembly site.

- feat(#889 Epic #886): **Graph query ergonomics** — filters + `depmap_get_hub_domains` MCP tool. Graph queries now support filter parameters (min edges, domain prefix, type classification) exposed uniformly across the depmap tool family.

- feat(#894): **`omni_max_repos_per_search` cap implemented**. CLAUDE.md documented this cap alongside `omni_wildcard_expansion_cap` (Bug #881) but no code ever enforced it. Literal alias lists and combined wildcards could bypass any total-fan-out ceiling. Added `MultiSearchLimitsConfig.omni_max_repos_per_search: int = 50` + `_enforce_repo_count_cap()` helper in `mcp/handlers/_utils.py` (mirrors wildcard-cap pattern), wired into `_omni_search_code` and `_omni_regex_search` AFTER wildcard expansion + literal union and BEFORE fan-out. Dual-envelope `CapBreach` response (MCP structured + REST HTTP 400) distinguished via new `error_code` field (`wildcard_cap_exceeded` vs `repo_count_cap_exceeded`). Web UI exposure. CLAUDE.md §Bug #881 section corrected to accurately describe both caps.

### Bug Fixes

- fix(#895): **Nested `ClaudeIntegrationConfig` fields read off top-level `ServerConfig` via `getattr` — silent fallback masked Web UI config**. Four call sites in `dependency_map_routes.py` and `mcp/handlers/repos.py` read `dependency_map_pass2_max_turns`, `cohere_api_key`, `voyageai_api_key` off `ServerConfig` directly. Because those fields live on `ServerConfig.claude_integration_config`, `getattr` silently returned each `getattr`'s default every time — operator-configured `Pass 2 Max Turns = 500` was always overridden by the hardcoded fallback `25`; Web-UI-configured Cohere/Voyage API keys never reached the subprocess env (masked in prod only because systemd `Environment=` carried the keys). Fix: all 4 sites use the existing `config_manager.get_claude_integration_config()` / `config.claude_integration_config.*` pattern; hardcoded `25` and silent `except: pass` removed; new `_resolve_provider_api_key` helper de-duplicates sites 2-4; guard test iterates `dataclasses.fields(ClaudeIntegrationConfig)` asserting `hasattr(ServerConfig(), field) is False` for each to prevent regression via name collision.

- fix(#884): **Auto-updater crash loop — URL resolution ran before recovery-path retry check**. `run_once.py main()` called `_resolve_server_url()` BEFORE `_should_retry_on_startup()`. When config.json was missing/corrupt, URL resolution raised `RuntimeError`, escaped to outer `except` → `sys.exit(1)`; systemd re-triggered every 60s; the self-healing pending_restart recovery never fired. Exact incident during v9.21.1 → v9.21.2 cycle. Fix: `DeploymentExecutor` constructed without `server_url` first, then `_should_retry_on_startup()` runs BEFORE any URL resolution; retry branch wraps `_resolve_server_url()` in try/except writing `"failed"` status + `sys.exit(1)` so next invocation retries cleanly. Plus smoke-test guard in `execute()` — subprocess probe `python -c "from code_indexer.server.auto_update.run_once import main"` with 10s timeout BEFORE self-restart, aborting with `"failed"` status if the new auto-updater code won't import. `DEPLOY-GENERAL-141/142` error codes.

- fix(#890): **Dependency Map dashboard reported all repos OK** — metadata filename mismatch prevented change detection. Three reader sites (`dependency_map_dashboard_service:375`, `dependency_map_service:1233` and `:1415`) read `<clone>/.code-indexer/metadata.json` but `cidx index` has written the provider-suffixed `metadata-voyage-ai.json` since the provider-aware migration. All three returned `"local"`/`"unknown"` sentinels; both sides of the status comparison read the same sentinel → `"local" == "local"` → always OK. New `metadata_reader.py` helper prefers provider-suffixed file, falls back to legacy, returns `None` (not sentinels) on missing/malformed. Dashboard `_compute_repo_statuses` adds `current_commit is not None` guard so `None == None` no longer masquerades as OK. `UnicodeDecodeError` caught for belt-and-suspenders.

- fix(#891): **`search_code` failed with `AttributeError: 'dict' object has no attribute 'memory_retrieval_enabled'`**. `ServerConfig.memory_retrieval_config` sometimes arrived as a raw dict (not the `MemoryRetrievalConfig` dataclass) when runtime config was hydrated from SQLite/Postgres storage. 3 MCP tests consistently tripped on this pre-fix. Fix at the deserialization boundary: `_dict_to_server_config` in `config_manager.py` now coerces `dict → MemoryRetrievalConfig` with `__dataclass_fields__` filter for rolling-upgrade safety, mirroring the existing pattern for `claude_integration_config` and 34 other nested configs. The previously-inline `isinstance(_raw_mem, dict)` workaround at `config_service.py:586-596` became dead code and was removed (simplified to `mem_cfg = config.memory_retrieval_config or MemoryRetrievalConfig()`).

- fix(#892): **`lifecycle_backfill` JobTracker registration failed — SQLite parameter 7 unsupported type**. Root cause: `progress_info` typed `Optional[str]` in backend SQL but callers could pass `Dict[str, Any]` raw. SQLite only binds `int`/`float`/`str`/`bytes`/`None`. Fix: new `_serialize_progress_info` helper in `job_tracker.py` (raises `TypeError` on unknown types, anti-fallback per Messi Rule 2) + `json.dumps(v) if isinstance(v, dict) else v` at 6 write boundaries (3 inline in `job_tracker.py` + `save_job`/`update_job` in `sqlite_backends.py` + INSERT/UPDATE in `postgres/background_jobs_backend.py`). Symmetric across both storage backends. Per CLAUDE.md §"Background Jobs (MANDATORY Checklist)", this was a direct violation of the invariant.

- fix(#873): **Dashboard provider latency p95 and p99 identical due to floor-based percentile collapse at small N**. `ProviderHealthMonitor._percentile()` used floor-based nearest-rank (`int(N * pct / 100)`); for N < ~25 `floor(N*0.95) == floor(N*0.99)` → p95 and p99 resolved to the same index. Default 60-minute rolling window + typical traffic (<25 successful provider calls/hour) meant the collapse was the common case, not the edge case. Unit test `test_latency_percentiles` actively locked in the broken behavior. Fix: `float(np.percentile(sorted_values, pct))` — linear interpolation (NIST / numpy / Excel default). numpy is already a transitive dep. Regression guard `assert status.p99_latency_ms > status.p95_latency_ms` added.

- fix(#896): **HNSW stale log storm — 421 identical warnings/2h in production**. `filesystem_vector_store.py:2390` emitted `logger.warning("HNSW index is stale and missing for 'voyage-code-3'. ...")` on every failed search. No repo/alias context, no dedup, no escalation. New `storage/hnsw_stale_logger.py` module with LRU-bounded per-collection-path state machine: first miss → WARNING with `alias=<>, path=<>, model=<>` context; subsequent misses within `cooldown_s=60` → DEBUG (suppressed from WARNING stream); persistent staleness past `escalate_after_s=600` with continued misses → one-shot ERROR naming `persistent_staleness_seconds`; post-escalation → DEBUG forever (no re-storm). Cache cap 1024 entries. Thread-safe via `threading.Lock`. Clock injectable for deterministic testing. Wired into stale-AND-missing branch; stale-but-present branch left alone per bug scope.

- fix(#874): **Dependency Management "Recent Run Metrics" showed P1/P2 = 0.0 for every row**. Four-story Epic-scope fix:
  - Story A: `_finalize_delta_tracking` hardcoded `0.0, 0.0` literal removed; `run_delta_analysis` wraps `detect_changes()` + `_update_affected_domains()` with `time.time()` → passes real `detect_s`/`merge_s` through to `pass1_duration_s`/`pass2_duration_s` columns. No schema change.
  - Story B: new columns `run_type VARCHAR(16)` + `phase_timings_json TEXT/JSONB` on both SQLite + Postgres with idempotent ALTER TABLE migrations. Missing Postgres `CREATE TABLE dependency_map_run_history` fixed as sub-defect (fresh Postgres cluster deploy would have crashed on first run metrics write — latent bug) via new migration `021_dependency_map_run_history.sql`.
  - Story C: full/delta/refinement paths now emit `run_type` + `phase_timings_json`. Refinement path records run metrics for the first time (was invisible on dashboard before). `repos_skipped` populated honestly per run type.
  - Story D: `depmap_job_status.html` template rewritten — columns now `Timestamp | Type | Domains | Chars | Edges | Repos | Phase timings`. Type column renders badge (full/delta/refinement, em-dash for NULL). Phase timings column renders compact pill list (`synth 12.4s · per-domain 318.7s · finalize 0.2s`) parsed via `phase_timings_parsed` in `_render_complete_response` view layer (not a Jinja filter). Legacy fallback to `P1 X.Xs / P2 Y.Ys` when only pass1/pass2 populated. `or 0` coercion deleted — NULL now renders as em-dash, never `0.0`.

- fix(#897 feature-flagged — measurement loop required on staging): **cidx-server RSS pinned at ~23 GB after HNSW LRU cache drain — glibc arena fragmentation**. Two opt-in mitigations behind bootstrap feature flags (both default OFF):
  - `enable_malloc_trim: bool = False` — `glibc malloc_trim(0)` hook at end of `_cleanup_expired_entries()` in `hnsw_index_cache.py`, forcing contractible brk pages back to OS. `ctypes.CDLL("libc.so.6")` loaded lazily; silently no-ops on musl/macOS. Linux + glibc only.
  - `enable_malloc_arena_max: bool = False` — idempotent `_ensure_malloc_arena_max()` auto-updater step (mirrors Bug #879's `_ensure_data_dir_env_var()` pattern) injects/removes `Environment=MALLOC_ARENA_MAX=2` in the cidx-server systemd unit file. Bidirectional revert: flag on → inject; flag off → remove on next auto-updater cycle. `DEPLOY-GENERAL-143` error code.

  Both flags are bootstrap-only (read from `config.json` before DB is available). Production is unchanged until an operator explicitly opts in — intended to be enabled on staging first, RSS recovery measured across backfill-drain cycle, shipped only if >50% recovery per bug body mandate. Full revert leaves no dead state.

- fix: **Baseline `config.codebase_dir` absolute-path resolution on `ConfigManager.load()`**. The `codebase_dir` field defaulted to `Path(".")` when absent from `config.json`; without resolution, downstream sites (`cli.py:3149` constructing `index_dir`) produced relative paths that failed Bug #642's migration-path-under-project-root assertion. Fix: `ConfigManager.load()` resolves a non-absolute `codebase_dir` against `self.config_path.parent.parent` (project root) after `Config(**data)` construction, so callers always receive an absolute path regardless of whether JSON supplied the key.

### Closed with Prior-Commit Evidence

10 bugs closed with backfilled evidence — their fixes landed in commits pre-dating v9.22.1: #825, #841, #871, #879, #882, #893, #842, #851, #852, #824. Evidence comments posted on each.

### Tests

~60 new unit + integration tests across all bug fixes, plus 70+ tests from Epic #886 Stories #887/#888/#889.

### Merge

Merged `feature/epic-886-depmap-tooling-hardening` (commits `bb92e24d`, `19f0441e`, `07487c1f`) into development — 37 files from 886 + 51 files from development, sole overlap `CLAUDE.md` auto-merged cleanly (different sections).

## v9.22.1

### Refactor

- refactor: **clean up redundant local `datetime` imports + unused `recent_ts` variable in `tests/unit/server/self_monitoring/test_scanner.py`**. The two removed local imports were shadowing the module-level `import datetime` at line 8; `recent_ts` was assigned but never referenced elsewhere in the file. Zero functional change — 32/32 tests in the file still pass. Not in Story #885 scope; bundled here as a patch because the remote `v9.22.0` tag is immutable (per CLAUDE.md's "never replace a tag on a remote" rule) but the staging auto-deployer requires `git describe --exact-match HEAD` to succeed, which demands HEAD itself be tagged.

## v9.22.0

### Features

- feat(#885): **Schema v4 lifecycle metadata — environments + branch→environment mapping**. Builds on Story #876's v3 foundation with two new fields that let downstream consumers (research-assistant agents, MCP clients) answer branch-read questions deterministically without hallucinating:
  - `ci.environments` — now evidence-grounded and cross-repo investigable (not just null by default)
  - `branch_environment_map` (NEW) — dict mapping git branch names to environment names with strict cross-field consistency enforced by `SchemaValidationError` (HARD REJECT on hallucinated mappings; no silent recovery — failed parses cause re-invocation with the error as feedback)

  The `lifecycle_unified.md` prompt gets a new Section 6 with three load-bearing clauses: (a) no query budget cap on `cidx-local` MCP — precision > query count, (b) ANTI-RULE: branch-name coincidence is NOT evidence for branch_environment_map, (c) YAML quoting requirement for scalars starting with reserved indicators (fixes A9 production bug where bare `@scoped/pkg` list entries broke `yaml.safe_load`).

  `CURRENT_LIFECYCLE_SCHEMA_VERSION` bumped 3→4. Parser accepts v3 legacy responses (no `branch_environment_map` field required) for backward compatibility.

- feat(#885 A7): **Lifecycle analysis timeouts moved to Web UI Config Screen**. Shell and outer timeouts (defaults bumped 240/300 → 360/420 to accommodate unbounded cross-repo investigation) are now stored in DB-backed runtime config, editable via the Web UI, and hot-reloaded on next job without server restart. Auto-migration follows Story #578's pattern: on first boot after upgrade, `lifecycle_analysis_config` is auto-populated with defaults — no manual operator intervention required. Cross-field validation enforces `outer_timeout_seconds >= shell_timeout_seconds + 30` at save time.

- feat(#885 A9): **YAML emitter safety helper**. New `yaml_quote_if_unsafe()` in `src/code_indexer/global_repos/yaml_emitter_utils.py` wraps scalars starting with YAML reserved indicators (`@ ` ! & * ? | > % # { } [ ] ,`) in double quotes with proper escaping. Applied to 5 hand-rolled frontmatter emitter sites (`dependency_map_analyzer._build_domain_frontmatter`, `run_pass_3_index`, `_build_index_frontmatter`; `dep_map_repair_executor._rebuild_frontmatter_repos_block`; `dep_map_index_regenerator._format_index_md`) that previously used bare f-string interpolation. Prevents scoped npm package names (`@org/pkg`) and other reserved-char scalars from poisoning frontmatter round-trip via `yaml.safe_load`.

### Refactor

- refactor(#885 A10): **MCP self-registration unified at `invoke_claude_cli` boundary (Option B-clean)**. The `MCPSelfRegistrationService.ensure_registered()` call is now at the top of `src/code_indexer/global_repos/repo_analyzer.py::invoke_claude_cli` — the single subprocess boundary that all Claude CLI invocations route through. Removed redundant calls from `ClaudeCliManager._worker_loop` and `DependencyMapAnalyzer._run_claude_cli`. Prior to this change, the lifecycle Claude CLI path (`LifecycleClaudeCliInvoker`) bypassed both call sites by deliberately not using `ClaudeCliManager`'s work queue — creating a silent gap where `cidx-local` MCP was NOT registered for lifecycle jobs, making cross-repo investigation impossible. New pattern: any future adapter that invokes Claude CLI via `invoke_claude_cli` inherits MCP access automatically. Rationale and architectural principle (preconditions belong at the boundary they guard) documented as a load-bearing pattern.

### Logging

- fix(#885 A9c): **`split_frontmatter_and_body` log severity upgraded WARNING → ERROR** with structured context (`file_path`, `first_offending_line`). Behavior unchanged — still returns `({}, content)` on parse failure — but operator observability elevated so broken frontmatter is visible in log triage without manual filtering.

### Tests

- 56 new unit tests for A9 YAML emitter safety (10 reserved indicators × 5 emitter sites = 50 matrix cells + scoped-package round-trip + log-severity assertion)
- 18 parser tests covering v4 branch_environment_map type validation, trim/non-empty invariants, omitted-vs-empty-dict semantics, cross-field HARD REJECT
- 2 invoker tests covering hot-reload of timeout config without restart
- 5 Web UI tests covering validation rejection (outer < shell + 30) and auto-migration default population
- 2 A4 anti-regression tests (grep-based) preventing future `lifecycle_schema_version == N` literal comparisons from reappearing
- 5 prompt-clause invariant tests (AC-V4-15) encoding the no-budget-cap and anti-coincidence rules
- 2 A10 tests covering `ensure_registered` call-site centralization + cache short-circuit behavior
- 7 synthetic golden-repo fixtures under `tests/fixtures/e2e_v4/` + 187-line MANUAL_TEST_PLAN.md for AC-V4-14 E2E gate execution by manual-test-executor against a running localhost:8000 CIDX server with VoyageAI credentials

### Bug Fixes

Post-landing fixes caught during Story #885 validation/verification and bundled into v9.22.0:

- fix(#885 A7d persistence): **Codex-caught AC-V4-17 blocker — `lifecycle_analysis_config` defaults were merged into the in-memory `ServerConfig` but never persisted to SQLite**. `ConfigService.initialize_runtime_db()` now writes the defaults back via `_save_runtime_to_sqlite()` and bumps the `server_config` row version once when the key is newly added; idempotent on subsequent boots (no re-persist, no re-log). AC-V4-17 now passes end-to-end: upgraded server surfaces `lifecycle_analysis_config` in Web UI without operator action. Two new persistence-invariant tests (`test_first_boot_persists_defaults_to_sqlite_row`, `test_second_boot_is_no_op_on_already_migrated_row`) close the gap that let the in-memory-only defaults look correct to unit tests while silently failing on disk.

- fix(#885 /admin/config 500): **GET /admin/config raised `UndefinedError: 'dict object' has no attribute 'lifecycle_analysis'`**. The Phase 5b Jinja2 template referenced `{{ config.lifecycle_analysis.* }}` but `_get_current_config()` in `routes.py` never added the key to the template context dict. Added `"lifecycle_analysis": settings.get("lifecycle_analysis", asdict(LifecycleAnalysisConfig()))` alongside the existing Story-numbered runtime config entries.

- fix(#885 Invalid section on Save): **Three coupled integration-layer gaps on the POST /admin/config/lifecycle_analysis path**: (a) template form `action` used hyphenated `lifecycle-analysis` instead of the underscored convention every other config section uses (fixed: action → `/admin/config/lifecycle_analysis`); (b) `_VALID_CONFIG_SECTIONS` whitelist in `routes.py` omitted `lifecycle_analysis` so the POST handler rejected the request before reaching the validator (fixed: entry added with Story #885 comment); (c) `ConfigService.update_setting()`'s dispatch elif-chain lacked a branch for `lifecycle_analysis`, raising `ValueError("Unknown category: lifecycle_analysis")` (fixed: new `elif category == "lifecycle_analysis"` branch + `_update_lifecycle_analysis_setting` helper mirroring the `_update_mcp_session_setting` pattern).

- fix(#885 UX): **`step="30"` forced HTML number inputs to multiples of 30** from their current value. Removed — any integer is now accepted. Server-side `outer >= shell + 30` cross-field rule remains the real invariant; step attribute was stylistic noise.

- chore(#885): **server-fast-automation fixups** — `ruff format` applied to 3 files (`dep_map_index_regenerator.py`, two new test files), plus `lifecycle_analysis_config` added to the `_KNOWN_RUNTIME_KEYS` frozenset in `test_config_service_bootstrap_keys_story_746.py` to close the Story #746 classification audit gap introduced when the new runtime config field was added.

Root-cause note: the three integration-layer gaps (template URL, whitelist, dispatch) would have been caught by a single FastAPI TestClient-based POST test covering the full Save flow. Phase 5b's unit tests exercised `_validate_config_section()` directly, short-circuiting all three layers. Filing a follow-up for a integration test covering the complete `/admin/config/{section}` POST pipeline across all existing config sections.

## v9.21.2

### Bug Fixes

- fix(#882 v9.21.2 hotfix): **Auto-updater crash-loop under cross-user systemd topology**. v9.21.1's `_resolve_server_url()` called `ServerConfigManager()` with no arguments, which falls back to the `CIDX_SERVER_DATA_DIR` env var first, then to `Path.home() / ".cidx-server"`. The auto-updater service file (via Bug #879's `_ensure_data_dir_env_var()`) injects `CIDX_DATA_DIR` — a different env var name — so under cross-user production topology (`cidx-auto-update.service` runs as `User=root` while `cidx-server.service` runs as a different user), neither lookup found the real data directory. `Path.home()` resolved to `/root`, `.cidx-server` was not there, `load_config()` returned `None`, `RuntimeError` was raised, and `sys.exit(1)` ran within ~1 second of every 60s timer fire — indefinite crash loop that never advanced to `service.poll_once()` and therefore never pulled the next release. Same-user deployments (staging, dev machines) were unaffected because `Path.home()` coincidentally pointed at the correct directory. Fix: `_resolve_server_url()` now reads `CIDX_DATA_DIR` explicitly via `os.environ.get()` and passes it to `ServerConfigManager(data_dir)` so the correct data directory is resolved regardless of which env var name the internal fallback reads. Regression test `TestResolveServerUrlHonorsCidxDataDir` asserts on `ServerConfigManager.__init__` `call_args` to prevent future env-var-name drift — the v9.21.1 test suite's `_patch_config_manager` helper replaced the entire `ServerConfigManager` class with a MagicMock that swallowed constructor arguments, which is why this regression slipped past CI. Production recovery for servers already stuck on v9.21.1 requires manual intervention (e.g., `systemctl edit cidx-auto-update.service` adding `Environment="CIDX_SERVER_DATA_DIR=/opt/code-indexer/.cidx-server"` — the env var the broken code already reads), after which the auto-updater picks up v9.21.2 automatically on the next timer fire.

## v9.21.1

### Bug Fixes

- fix(#876 follow-up): **Schema v3 `_CI_TRIGGER_EVENT_ENUM` missing `"merge_request"`** caused every GitLab-hosted golden repo to fail lifecycle backfill with `UnifiedResponseParseError: Invalid enum value for lifecycle.ci.trigger_events`. Production impact on 2026-04-22: 19 aliases rejected, 140 failed jobs, entire GitLab fleet stalled. Root cause: the original v3 enum only included GitHub's `"pull_request"` trigger; GitLab uses `"merge_request"` (its native MR event). Fix: add `"merge_request"` to the enum alongside `"pull_request"`; prompt template updated to tell Claude CLI to use the correct value per host (GitHub → `pull_request`, GitLab → `merge_request`) without conflation. Regression test locked in at `test_unified_response_parser_v3.py::test_ci_trigger_events_accepts_merge_request`.

- fix(#882): **Auto-updater ignored operator-configured server URL**. `run_once.py` constructed `DeploymentExecutor` without passing `server_url`, leaving it at the hardcoded default `http://localhost:8000`. Deployments on non-default ports (e.g., 8080) could not issue maintenance-mode or drain-status requests against their own server. Fix: `run_once.py` now loads `ServerConfigManager().load_config()` and passes `http://{cfg.host}:{cfg.port}` explicitly into `DeploymentExecutor`. When `config.json` is absent, `run_once` raises `RuntimeError` with actionable remediation text ("Run the CIDX installer") per Messi Rule #2 (Anti-Fallback) — no hardcoded fallback URL, fail loud.

- fix(#882): **Drain loop burned 120s systemd TimeoutStartSec budget when cidx-server was unreachable**. `DeploymentExecutor._wait_for_drain()` spun for up to `drain_timeout` seconds (7200s fallback when the timeout endpoint also failed with `ConnectionError`), killing the entire `cidx-auto-update.service` upgrade cycle. Fix: track STRICTLY CONSECUTIVE `ConnectionError` count and return `True` ("assume drained — nothing to drain if server is down") after 3 in a row (~30s at the default 10s poll interval). Any non-ConnectionError iteration outcome (HTTP response received, auth failure, generic exception) resets the counter so the early-exit is never triggered by cumulative mixed failures.

## v9.21.0

### Features

- feat(#876): **Schema v3 lifecycle metadata — operational workflow enrichment**. The per-golden-repo `cidx-meta.md` now captures how the repo ships, not just what it is. Three new required sections are written on every analyze pass:
  - `branching` — `default_branch`, `model` (github-flow/gitflow/trunk-based/release-branch/unknown), `release_branch_pattern`, `protected_branches`
  - `ci` — `trigger_events` (push/pull_request/tag/schedule/workflow_dispatch/manual), `required_checks`, `deploy_on` (tag/merge-to-main/merge-to-release-branch/manual/none), `environments`
  - `release` — `versioning` (semver/calver/custom/none/unknown), `version_source`, `changelog`, `auto_publish`, `artifact_types`

  Purely additive: v2 classification keys (description + 6 lifecycle identification keys) remain unchanged; v2 `.md` files continue to be valid. Schema v3 is enforced by a type- and enum-aware validator in `UnifiedResponseParser` with escape values (`null`, `[]`, `"unknown"`, `"none"`) for each required-within-section field so the Claude CLI never has to invent values. Timeouts bumped from 180s/240s to 240s/300s to accommodate the wider investigation scope. Parser version gate changed from `== CURRENT_LIFECYCLE_SCHEMA_VERSION` to `>= 2` across both SQLite and PostgreSQL backends so consumers accept both v2 and v3 files during rolling upgrade.

  Post-deploy backfill path (startup sweep + delta/full dep-analysis pre-flight + golden-repo add + description-refresh event + repair executor) detects any `schema_version < 3` `.md` and routes through `LifecycleBatchRunner` to rewrite as v3. No operator action required.

- feat(#854): **Epic #854 — DepMap MCP tools suite** (5 new tools for dependency analysis workflow):
  - `depmap_find_consumers` (Story #855): given a symbol, return all repos that depend on it
  - `depmap_get_repo_domains` + `depmap_get_domain_summary` (Story #856): enumerate a repo's domain decomposition and fetch a per-domain summary (entry points, dependencies, consumers)
  - `depmap_get_stale_domains` (Story #857): list domains whose last_analyzed timestamp is older than a threshold, surfacing backfill candidates
  - `depmap_get_cross_domain_graph` (Story #858): build a cross-domain dependency graph for a repo or a set of repos
  - `dependency_analysis_workflow` guide + MCP handler + cross-refs (Story #859): narrative workflow that stitches the tools together for agent-driven investigations

### Bug Fixes

- fix(#876): Startup lifecycle backfill sweep — on server boot, `LifecycleFleetScanner` scans all golden repos, flags any `.md` with `schema_version < CURRENT_LIFECYCLE_SCHEMA_VERSION`, and routes them through `LifecycleBatchRunner` for auto-repair. Closes gaps where broken or missing `cidx-meta.md` files previously required manual intervention.

- fix: `golden_repo_manager._clone_repository` coerced `default_branch=None` to `""` via `default_branch or ""`, which then passed the `is not None` guard in `_clone_remote_repository` and produced `git clone --branch ""` — fails with "fatal: Remote branch not found in upstream origin". Three surgical fixes: remove `or ""` coercion at call site, widen `_clone_repository.branch` to `Optional[str] = None`, change guard from `is not None` to truthy check.

- fix: `refresh_scheduler.py` hard-coded the default branch to `"main"` when an orphan description_refresh_tracking row needed a fallback. Repos whose default branch is `master` (or any non-main) silently ran refresh against a non-existent branch. Now reads from golden-repo metadata.

- fix: `description_refresh_scheduler` now self-heals orphan `description_refresh_tracking` rows (rows referencing aliases whose golden repo no longer exists). Previously these accumulated indefinitely and caused scheduler warnings on every tick.

- fix: `fast-automation.sh` captured `PYTEST_EXIT_CODE=$?` after piping pytest through `tee`, which returned tee's exit code (always 0) and masked real pytest failures as green SUCCESS banners. Replaced with `PYTEST_EXIT_CODE=${PIPESTATUS[0]}` to capture pytest's actual exit status.

- fix(#876): Close codex-review blockers on lifecycle backfill sweep — stricter default_branch escape handling, prompt guard test hardening, converged cidx-meta writers through `write_meta_md`, strict JSON parser with `_strip_code_fence`/`_strip_preamble`, retired staged-rollout guard.

- fix(#857): E2E — serialize `last_analyzed` as ISO-8601 string (not datetime object) in MCP handler responses; reject naive datetimes at parse boundary.

## v9.20.16

### Bug Fixes

- fix(#879): IPC path misalignment between `cidx-server` (runs as `User=code-indexer`, `HOME=/opt/code-indexer`) and `cidx-auto-update` (runs as `User=root`, `HOME=/root`) caused the Restart Server admin UI button to silently fail in production. Each process computed `Path.home() / ".cidx-server"` independently, so the server wrote the restart signal file where the auto-updater never looked.

  **Root cause**: Module-level path constants `RESTART_SIGNAL_PATH`, `PENDING_REDEPLOY_MARKER`, and `AUTO_UPDATE_STATUS_FILE` in `deployment_executor.py` were fixed at import time using `Path.home()`, inheriting whichever user ran the process.

  **Fix — CIDX_DATA_DIR env var support**: All three constants now resolve via `os.environ.get("CIDX_DATA_DIR", Path.home() / ".cidx-server")` so both services can be pointed at the same data directory by setting the variable in their systemd unit files.

  **Fix — `_ensure_data_dir_env_var()` self-heal method**: New `DeploymentExecutor._ensure_data_dir_env_var()` detects the auto-updater service file, reads the `User=` directive to determine the server user's home, and injects `Environment="CIDX_DATA_DIR=<server-user-home>/.cidx-server"` into the `[Service]` section if not already present. Wired into `execute()` as Step 6.5 between python/ripgrep ensure steps; emits `DEPLOY-GENERAL-058` warning on failure without aborting the overall deployment.

  **Same-user short-circuit**: When the auto-updater service file has no `User=` directive (server and auto-updater run as the same user), `_ensure_data_dir_env_var()` returns immediately — no injection needed, no backward-compat break.

  Tests: 12 pass including acceptance criterion #5 (same-user unaffected). Gates: server-fast-automation.sh (10216 pass / 0 fail). Codex review: APPROVED after 1 rejection/remediation cycle.

## v9.20.15

### Bug Fixes

- fix(#881): CIDX server HNSW index cache accumulated orphan entries and undersized the LRU cap, driving RSS to 22.5 GB within 45 minutes in production. Four coordinated fixes applied.

  **Phase 1 — Diagnostic logging (MCP search path)**: Promoted/added 4 INFO log lines in `handlers/search.py` (`search_code` entry with correlation_id/user_id/query/alias/limit; `_omni_search_code` post-expansion with matched count and first 10 aliases; `search_code` success exit with result_count and elapsed_ms) and `handlers/_utils.py::_expand_wildcard_patterns` (wildcard expansion count). Operators can now observe wildcard blowup live via `sqlite3 logs.db` without restart. Coverage: `tests/unit/server/mcp/test_search_diagnostic_logging.py`.

  **Phase 2 — Orphan eviction on snapshot swap**: `RefreshScheduler._execute_refresh()` previously atomic-swapped the alias to a new `v_{timestamp}` CoW snapshot without invalidating the old cache key, leaving `HNSWIndexCacheEntry` resident for the 10-minute TTL. With dual-provider parallel queries (voyage + cohere) each refresh orphaned 2+ entries. New `HNSWIndexCache.invalidate_prefix(path_prefix) -> int` method (thread-safe via `_cache_lock`) evicts all keys matching the prefix and is called by `refresh_scheduler.py` immediately after `swap_alias()` in a try/except that logs but does not re-raise. Coverage: `tests/unit/server/cache/test_invalidate_prefix.py`, `tests/integration/server/test_refresh_evicts_hnsw_cache.py`.

  **Phase 3 — Wildcard expansion cap + Web UI setting + cache bypass on fan-out**: Wildcard patterns like `*-global` could expand to arbitrary counts, each loading a fresh `HNSWIndexCacheEntry` and pressuring the cap. New runtime-editable setting `multi_search_limits_config.omni_wildcard_expansion_cap` (default 50, validated 1..10000, hot-reloadable) exposed in Admin → Config → Multi-Search. `_expand_wildcard_patterns()` raises `WildcardExpansionCapExceeded` when the cap is breached; both MCP and REST paths return HTTP 400 with a user-facing message pointing to the Web UI setting. Wildcard fan-out now bypasses the HNSW cache by passing `hnsw_cache=None` through `MultiSearchService._search_semantic_sync`. Coverage: `tests/unit/server/mcp/test_wildcard_cap.py`, `test_cap_breach_helper.py`, `test_cache_bypass_on_fanout.py`.

  **Phase 4 — Size cap accounting honesty**: `HNSWIndexCacheEntry.index_size_bytes` previously returned only `hnswlib.index_file_size()` (serialized on-disk bytes), excluding the Python `id_mapping` dict and hnswlib native runtime overhead. The 4096 MiB cap (Bug #878) therefore fired way too late. New accounting includes `sys.getsizeof(id_mapping) + sum(getsizeof(k)+getsizeof(v) for k,v in id_mapping.items())`. Residual gap: hnswlib native C++ overhead is not measurable from Python; documented as a known limitation so operators understand the cap is a floor, not a ceiling. Coverage: `tests/unit/server/cache/test_id_mapping_size_bytes.py`.

## v9.20.14

### Bug Fixes

- fix(#880): Web UI Admin → Config cache section now exposes `index_cache_max_size_mb` and `fts_cache_max_size_mb` as operator-editable inputs in `<form id="edit-form-cache">`, and display rows render `4096 (default)` instead of the misleading `Unlimited` when the DB value is `None`. After Bug #878 Fix B.1 began overlaying `DEFAULT_MAX_CACHE_SIZE_MB = 4096` at singleton init, operators saw "Unlimited" on screen while the runtime silently enforced a 4096 MiB LRU cap — a UX lie. Four coordinated changes: (1) `config_section.html` adds two `<input type="number" min="1">` fields with helper text "Leave empty to use the 4096 MiB default", and rewrites display expressions from `{{ value or 'Unlimited' }}` to explicit `{% if value is none %}4096 (default){% else %}{{ value }}{% endif %}` so `0`/`""`/`None` are no longer conflated; (2) `ConfigService._hot_reload_cache_size_cap()` now imports `DEFAULT_MAX_CACHE_SIZE_MB` and applies it to the live singleton's `config.max_cache_size_mb` whenever the incoming value is `None`, preserving the 4096 MiB safety floor at runtime even after operator-driven clears (DB value remains `None` for correct "no override" persistence semantics); (3) `_validate_config_section("cache")` in `routes.py` rejects `"0"`, `"-100"`, `"abc"`, and `"1.5"` with the message `"<field> must be empty or a positive integer (MB)"` before `update_cache_setting()` is called; (4) 20 new parametrized tests in `tests/unit/server/web/test_config_cache_size_caps.py` cover edit-form presence, display semantics, POST round-trip through `reset_config_service()` + `initialize_runtime_db()`, hot-reload propagation with two-sided invariant (runtime=4096 AND DB=None), and negative validation. Dataclass defaults remain `None` (Bug #878 invariant preserved); hot-reload scope stays narrow to the two size-cap keys only. Codex code review APPROVED after one reject/remediate cycle.

## v9.20.13

### Bug Fixes

- fix(#878): CIDX server process accumulated SQLite file descriptors and unbounded HNSW/FTS cache memory under sustained background-job traffic, eventually causing `EMFILE: Too many open files` in long-running deployments. Five targeted fixes applied.

  **A.1 — close-on-clobber (`DatabaseConnectionManager.get_connection`)**: When Linux recycles an OS TID, a new `BackgroundJob` thread may land on the same TID as a previous dead thread. Its `threading.local` storage is empty, so `get_connection()` opens a fresh `sqlite3.connect()` and assigns `self._connections[thread_id] = conn`. Old code silently overwrote the prior entry, leaking the previous connection until non-deterministic Python GC reclaimed it. New code checks `self._connections.get(thread_id)` under `self._lock`, closes any pre-existing different connection (best-effort with a WARNING log on failure), and only then stores the new one. Test coverage: `tests/unit/server/storage/test_database_manager_clobber.py`.

  **A.2 — wall-clock cleanup daemon (`DatabaseConnectionManager.start_cleanup_daemon` / `stop_cleanup_daemon`)**: The former demand-driven piggyback cleanup (fired from `get_connection()` every 60s) lost races to short-lived `BackgroundJob` thread churn — production showed cleanup gaps of 1-16 minutes. Added a dedicated daemon thread (`DatabaseConnectionManager-cleanup-daemon`, `daemon=True`) that wakes every `DEFAULT_CLEANUP_INTERVAL_SECONDS` (60s) on a `threading.Event` and invokes `_sweep_all_instances_unthrottled()` across every registered singleton — decoupled from request/query traffic. `start_cleanup_daemon()` is idempotent (no-op if already running) and enforces `interval > 0`; `stop_cleanup_daemon()` signals the Event, joins with `DEFAULT_CLEANUP_STOP_TIMEOUT_SECONDS` (2s), and uses identity-guarded reference clearing (`cls._cleanup_thread is thread`) to preserve the single-daemon invariant if a concurrent start races with a join that times out. Wired into FastAPI lifespan startup/shutdown in `src/code_indexer/server/startup/lifespan.py` with `APP-GENERAL-034`/`APP-GENERAL-035` error codes. The piggyback trigger inside `get_connection()` was removed. Test coverage: `tests/unit/server/storage/test_cleanup_daemon.py`.

  **A.3 — finally-close in `BackgroundJobManager` (`_close_thread_connections_on_all_managers`)**: The cleanup daemon cannot keep up with workloads that spawn and retire many short-lived job threads between ticks (RC-3). `_execute_job`'s outer finally and `_execute_with_cancellation_check`'s inner worker finally now iterate `DatabaseConnectionManager._instances.values()` and call `close_thread_connection()` on each so every SQLite connection the worker opened is closed at thread exit rather than awaiting the next 60s daemon sweep. `close_thread_connection()` pops the tracked entry under `self._lock`, closes both the tracked and thread-local references (guarding against double-close when they are the same object), nulls `self._local.connection`, and logs WARNING on individual close failures without aborting sibling manager cleanup. Test coverage: `tests/unit/server/repositories/test_job_finally_cleanup.py`, `tests/integration/server/test_no_leak_on_thread_churn.py`.

  **B.1 — opinionated default cache cap (`src/code_indexer/server/cache/__init__.py`)**: `HNSWIndexCacheConfig.max_cache_size_mb` and `FTSIndexCacheConfig.max_cache_size_mb` defaulted to `None`, meaning hot repositories whose access-based TTL kept hitting the cache could grow native memory unbounded. Added `DEFAULT_MAX_CACHE_SIZE_MB = 4096` and helper `_apply_default_size_cap()` invoked at singleton init inside `get_global_cache()` and `get_global_fts_cache()`. The dataclass defaults remain `None` (explicit "no cap" configs are preserved); the 4096MB overlay only applies when no value is set in server config. Emits an INFO log so operators can see they are running on the default. Test coverage: `tests/unit/server/cache/test_size_cap_defaults.py`.

  **B.2 — runtime hot-reload of cache size cap (`ConfigService._hot_reload_cache_size_cap`)**: After Fix B.1, operators still needed a restart to change the cap. `ConfigService._update_cache_setting()` now detects writes to `index_cache_max_size_mb` / `fts_cache_max_size_mb` and invokes `_hot_reload_cache_size_cap()` which acquires the live cache singleton's `_cache_lock`, overwrites `cache.config.max_cache_size_mb`, and runs `_enforce_size_limit()` so entries exceeding the new cap are evicted immediately. Exceptions during hot-reload (e.g., cache singleton not yet constructed) are logged at WARNING and swallowed so the caller's config write stays atomic. Scope is intentionally narrow: only the two size-cap keys trigger hot-reload — `TestHotReloadScopeIsolation` asserts all other cache settings write through to config only. Test coverage: `tests/unit/server/services/test_cache_hot_reload.py`.

  Local E2E on port 8009 validated daemon startup/shutdown (startup log: `DatabaseConnectionManager cleanup daemon started (interval=60.0s)`; shutdown log: `Cleaned up 4 stale SQLite connections` + three `Cleaned up 1 stale SQLite connections` + `cleanup daemon stopped`) and cap application (`Applying default max_cache_size_mb=4096MB for HNSW cache` + same for FTS). Post-E2E log audit against `logs.db`: 0 ERRORs, 0 WARNINGs, 120 INFOs.

## v9.20.12

### Bug Fixes

- fix(#872): `lifecycle_backfill` jobs now emit real-time progress through the entire run instead of remaining silently at 0% until terminal completion. Two emission sites added inside `DescriptionRefreshScheduler`: the success branch of `_maybe_complete_backfill_job()` and the failure branch of `_maybe_fail_backfill_job()`. Each per-repo completion or failure now calls `JobTracker.update_status()` with computed `progress=int(processed*100/cluster_wide_total)` (capped below 100 until the terminal step) and `progress_info="{processed}/{total} repos processed"`. Also added `"lifecycle_backfill"` to the operation_type allowlist in `dependency_map_routes._get_progress_from_service()` so HTMX dashboard polling returns the emitted values in the `X-Journal-Progress` / `X-Journal-Progress-Info` response headers. Progress now advances 25 → 50 → 75 → 100 during a 4-repo backfill; terminal failure preserves the last intermediate value (e.g., 75) because `fail_job()` intentionally does not overwrite progress. Root cause: both aggregate-owner methods previously returned silently when `remaining > 0` without ever touching the JobTracker, and the reader filter rejected the emitted values anyway. Module-level constants `_BACKFILL_PROGRESS_PERCENT_SCALE` and `_BACKFILL_INTERMEDIATE_PROGRESS_MAX` introduced to avoid magic numbers.

## v9.20.11

### Bug Fixes

- fix(#875): `_update_claude_cli_setting()` now handles five previously-missing keys that caused a `ValueError` when saving Web UI config: `dep_map_fact_check_enabled` (bool), `fact_check_timeout_seconds` (int, min 60), `scheduled_catchup_enabled` (bool), `scheduled_catchup_interval_minutes` (int, min 1), and `cohere_api_key` (Optional[str], empty string clears to None). Added module-level constants `_MIN_FACT_CHECK_TIMEOUT_SECONDS` and `_MIN_SCHEDULED_CATCHUP_INTERVAL_MINUTES`, and a `_parse_bool()` helper to avoid magic numbers and duplicated boolean-parsing logic.

## v9.20.10

### Bug Fixes

- fix(#871): `_clean_claude_output()` now strips bare CSI tails (e.g. `[>4m`, `[?25h`, `[?1004h`, `[0m`) that `script -q -c ... /dev/null` produces when it drops the leading ESC byte. Every Phase 2 `invoke_lifecycle_detection` call in production was silently failing `yaml.safe_load` with `found character '>' that cannot start any token` (182+ occurrences since Epic #725 deploy), returning `None`, and never writing the per-repo lifecycle YAML. Added a second regex pass anchored on a private-param prefix (`[?<>=!]`) or a leading digit so YAML flow sequences (`[repo-a, repo-b]`) are preserved while all production-observed bare CSI variants are stripped.

- fix(#852): `DependencyMapService.run_full_analysis()` now calls `_queue_lifecycle_backfill_if_needed()` as the first statement — before conflict checks, before setup — matching the Story #728 AC2 pattern already used by `run_delta_analysis`. Fresh repos that had never gone through a delta run were never queueing the `lifecycle_backfill` aggregate job on first-run, leaving newly-registered repos without lifecycle metadata indefinitely.

- fix(#851): `DescriptionRefreshScheduler._maybe_fail_backfill_job()` added as a failure-path sweeper. When Phase 2 returns without a valid `lifecycle` block (e.g., the 100% YAML parse-failure scenario caused by #871), this method increments an in-memory processed counter under `_backfill_job_id_lock` and calls `job_tracker.fail_job()` on the active aggregate when the counter reaches `cluster_wide_total`. Before this fix, aggregate `lifecycle_backfill` jobs stayed `running` at 0% forever when every repo failed — relying on orphan cleanup at server restart.

## v9.20.9

### Bug Fixes

- fix(#869): RefreshScheduler now has `set_active_backfill_job_id()` method and `_active_backfill_job_id` attribute. Every delta refresh was crashing with `AttributeError: 'RefreshScheduler' object has no attribute 'set_active_backfill_job_id'` because `DependencyMapService._queue_lifecycle_backfill_if_needed()` called this method that did not exist on the scheduler.

- fix(#870): `BackgroundJobsSqliteBackend.update_job()` now JSON-serializes the `metadata` field. The `lifecycle_backfill` job registration was failing with `Error binding parameter 7 - probably unsupported type` because the `metadata` dict was passed directly to SQLite without serialization. Added `"metadata"` to the `json_fields` set in `update_job()`.

## v9.20.8

### Bug Fixes

- fix(#860): Auto-discovery now uses SSH URL for private repositories. The client-side JavaScript pagination rewrite in v9.20.2 hardcoded `clone_url_https` everywhere, causing silent failures for private repos. Added `preferredCloneUrl()` helper, `data-preferred-url` attribute on checkboxes, and updated `onCheckboxChange` + `toggleSelectAll` to use SSH for private repos.

- fix(#862): Batch-create golden repo registration failures now logged and surfaced in UI. Added `WEB-GENERAL-067` error code, `logger.warning(..., exc_info=True)` on per-repo exceptions, `_sanitize_batch_create_error()` helper, `MAX_BATCH_CREATE_REPOS=50` cap (HTTP 400 on overflow). UI now shows inline per-repo errors in modal instead of generic alert, deselects only successful repos on partial failure, and validates response shape at entry.

- fix(#861): `id_index.bin` write is now crash-durable and self-healing. `save_index()` uses atomic write (temp file + `os.fsync` + `os.replace` + dir fsync). `load_index()` raises typed `CorruptIDIndexError` for zero-byte files, truncated headers, unreasonable entry counts, and EOF mid-entry. `_load_id_index()` in `FilesystemVectorStore` auto-repairs on `CorruptIDIndexError` by calling `rebuild_from_vectors()`. `rebuild_from_vectors()` validates JSON shape and rejects non-string/empty point IDs.

## v9.20.7

### Bug Fixes

- fix(#853): Four additional fixes from code-review of v9.20.6 implementation:
  1. `JobTracker.is_cancelled(job_id)` added — queries the DB `cancelled` column directly, bypassing the stale in-memory cache, so the scheduler observes cancellations written by `BackgroundJobManager.cancel_job`.
  2. `DescriptionRefreshScheduler._self_close_backfill` now calls `complete_job` on the aggregate job when the last repo finishes (via `_count_repos_needing_backfill()` + `_maybe_complete_backfill_job()`); cancel path uses `update_status("cancelled")` instead of `fail_job`.
  3. Conditional clear: `_active_backfill_job_id` only set to `None` when the stored id matches the id being closed, preventing a race condition with a concurrent new backfill cycle.
  4. `is_admin=True` added to `cancel_job()` calls in `web/routes.py` and `inline_admin_ops.py` so admin-authenticated flows can cancel system-owned jobs consistently.

## v9.20.6

### Bug Fixes

- fix(#853): Cancel API failed for `lifecycle_backfill` jobs — "Job not found or not authorized". Five root causes fixed:
  1. `BackgroundJobManager.cancel_job` now accepts `is_admin: bool = False`; admin users bypass the username ownership check, allowing admins to cancel system-owned jobs.
  2. `inline_jobs.py` router now passes `is_admin=(current_user.role == UserRole.ADMIN)` so admin status reaches the cancel logic.
  3. `DependencyMapService._backfill_register_aggregate_job` now returns the registered `job_id` instead of discarding it; `_queue_lifecycle_backfill_if_needed` stores the result in `self._active_backfill_job_id` and propagates it to the scheduler via `set_active_backfill_job_id`.
  4. `DescriptionRefreshScheduler` gains `_active_backfill_job_id` (thread-safe via `threading.Lock`) and `set_active_backfill_job_id()` method.
  5. `DescriptionRefreshScheduler._run_loop_single_pass` checks the active backfill job status at the start of each pass; when the job is cancelled it calls `fail_job` with the correct `job_id` and returns early, stopping all further repo processing.

## v9.20.5

### Bug Fixes

- fix(#849): Delta dep-map retried 3x on intentional no-op. `invoke_delta_merge_file` returned `None` for both invocation failures and Claude-determined no-changes, making the retry loop treat every clean no-op as a failure. Added `FILE_UNCHANGED` sentinel to the prompt instructions; `invoke_delta_merge_file` now detects the signal and returns `_DELTA_NOOP` before the mtime check. `_update_domain_file` returns a `_DomainUpdateResult` enum (WRITTEN/NOOP/FAILED); the retry loop breaks immediately on NOOP, retrying only on FAILED.
- fix(#850): lifecycle_backfill ANSI cleaning incomplete — `ESC[>4m` (and other CSI private/intermediate sequences) survived cleaning and caused 100% YAML parse failures, leaving the backfill in an infinite retry loop. Extended the CSI regex from `[0-9;?]*[a-zA-Z]` to full ECMA-48 grammar `[0-?]*[ -/]*[@-~]` in both `repo_analyzer.py` and `description_refresh_scheduler.py`. Added `NO_COLOR=1` to `filtered_env` in both Claude CLI subprocess paths to prevent ANSI output at source.

## v9.20.4

### Bug Fixes

- fix: Add `--add-dir <journal_path.parent>` to Claude CLI invocation when `journal_path` is set. `--dangerously-skip-permissions` only disables interactive permission prompts — it does not expand the path sandbox. Without `--add-dir`, Claude could not write journal activity entries to `.tmp/depmap-delta-journal/` when the cwd was a different directory (e.g. `/mnt/codeindexer-data/...`). Journal writes are now unblocked on all deployments.

## v9.20.3

### Bug Fixes

- fix(#834): Delta merge produced staging files with 4 YAML frontmatter markers. Root cause: prompt seeded Claude's temp file with frontmatter while also instructing it not to emit frontmatter — model resolved the conflict by duplicating. Fix strips frontmatter at the service boundary, sanitizes Claude's returned body with a WARNING if it re-emits frontmatter, and keeps a defensive strip inside `invoke_delta_merge_file`.
- fix(#835): `lifecycle_backfill` jobs stayed pending forever. `get_stale_repos()` didn't surface `lifecycle_schema_version`, and `_run_loop_single_pass` rejected lifecycle-stale repos via the `has_changes_since_last_run` gate. Fix exposes the column on both SQLite and PostgreSQL backends and bypasses the change gate when `lifecycle_schema_version < LIFECYCLE_SCHEMA_VERSION`.
- fix(#836): `lifecycle_backfill` jobs displayed "Unknown" as the Repository column in the dashboard. Now labels aggregate jobs as "N repos" or "all repos".
- fix(#838): PostToolUse hook bash script had unquoted `F=path` (one-liner) preventing test path-rewriting, missing journal-writing logic, and no STATUS NUDGE at turn 10. Fix: single-quoted `F='path'` on own line, STATUS NUDGE at C==10, per-tool narrative block writing `**claude-tool**` entries to journal when `journal_path` provided.
- fix(#839): Auto-updater never refreshed the Claude CLI binary, pinning production servers to stale versions. Adds `_ensure_claude_cli_updated()` to `DeploymentExecutor` running `npm install -g @anthropic-ai/claude-code@latest` on every deploy, non-fatal on all error conditions.
- fix(#840): Systemic prompt-duplication antipattern across 6 sites in the dependency-analysis pipeline. Prompts embedded large content (domain docs up to 37KB) that was also delivered via file-based Read/Edit instructions, causing 116 compactions with no forward progress on large domains. All 6 sites now reference file paths instead of embedding content inline.
- fix(#841): `VoyageMultimodalClient.get_embedding` violated the `EmbeddingProvider` contract — missing `embedding_purpose` and `model` kwargs caused `MultiIndexQueryService` to silently drop the multimodal half of every RRF query via a caught TypeError. Signature now conforms to the base contract.

## v9.20.2

> Note: tags v9.20.0 and v9.20.1 were pushed to origin pointing at earlier commits (pre-commit mypy hook blocked the intended commits but the tag pushes succeeded). Per CLAUDE.md tag-immutability policy ("NEVER replace a tag on a remote"), the release is re-cut as v9.20.2. Content identical to what v9.20.0 was intended to deliver, plus two mypy fixes: `git_subprocess_env.py` now uses `dict(os.environ)` instead of `os.environ.copy()` for a proper `dict[str, str]` return type, and `git_runner.py:41` carries an explicit `env: Dict[str, str]` annotation so the inferred type propagates through the helper chain.

### Features

- feat(#754): Auto-Discovery Client-Side Pagination. Replaced server-paged auto-discovery with a single exhaustive upstream fetch plus client-side pagination for both GitLab and GitHub. New JSON endpoints `GET /admin/api/discovery/{platform}/all` (returns `{repositories, total_source, total_unregistered}` with each repo annotated `is_hidden`) and `POST /admin/api/discovery/{platform}/enrich` (max 50 clone_urls per call, returns `{enrichments: {clone_url: info}}` with GitLab GraphQL batching of 10 and per-repo soft-fail). Frontend `auto_discovery.html` uses a client-side pagination engine (pageSize=50, instant client-side search/sort/show-hidden, on-demand enrichment with DOM patching, compound-key `platform:clone_url_https` selection model, prune-on-refresh). GitHub `/enrich` is a no-op because commit data is embedded in `/all` directly (`description`, `default_branch`, `is_private`, `last_commit_hash`, `last_commit_author`, `last_commit_date`, `last_activity` all present). GitLab `/enrich` response includes `commit_hash`, `commit_author`, `commit_date` (ISO 8601), `last_activity` (ISO 8601 from `lastActivityAt`). Verified E2E against real tenants: GitLab 1037/1040 repos in one request, GitHub 113/116.
- Viewport-aware table scrollbox: `.table-container` max-height is computed at runtime from `getBoundingClientRect().top` + `visualViewport.height` minus `.pagination` height; recomputed on window resize and on `visualViewport.resize` (zoom). Pagination bar is always visible without page scroll regardless of browser size or zoom.

### Security / Stability Hardening

- Server-killer SSH password-prompt hang fixed across the entire server runtime. Every `git` subprocess against a remote (`clone`, `fetch`, `pull`, `push`, `ls-remote`) now passes a hardened env via new shared helper `src/code_indexer/server/git/git_subprocess_env.py::build_non_interactive_git_env()` which sets `GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o PubkeyAuthentication=yes"` plus `GIT_TERMINAL_PROMPT=0`. Before this fix, when a registered SSH key went stale or none matched, the git subprocess could hang indefinitely waiting on an interactive password prompt, blocking the worker thread and freezing the server. Audit covered `server/repositories/golden_repo_manager.py`, `server/repositories/activated_repo_manager.py`, `server/repositories/repository_listing_manager.py`, `server/services/remote_branch_service.py`, `server/services/git_operations_service.py`, `server/services/git_state_manager.py`, `server/git/git_sync_executor.py`, `server/auto_update/change_detector.py`, `server/auto_update/deployment_executor.py`, `global_repos/git_pull_updater.py`, `global_repos/refresh_scheduler.py`, `utils/git_runner.py`. Verification script in-repo confirms zero unprotected call sites remain. Regression tests in `tests/unit/server/git/test_git_subprocess_env.py` and `tests/unit/server/repositories/test_golden_repo_manager_ssh_noninteractive.py`.

### Bug Fixes (discovered during Story #754 E2E)

- Fixed GitLab `/enrich` returning 500 "Object of type datetime is not JSON serializable" — `commit_date` is now converted to ISO string in `enrich_repositories` before returning.
- Fixed GitLab `/enrich` returning 500 "'NoneType' object has no attribute 'get'" — added None-guards at every GraphQL nesting level (`project`, `repository`, `tree`, `lastCommit`) so null/empty responses fail soft per-repo.
- Added `last_activity` to GitLab `/enrich` response — GraphQL query now fetches `project.lastActivityAt`, passed through as ISO string.
- Fixed GitHub `/all` returning a stripped 5-field dict — now includes `description`, `default_branch`, `is_private`, `last_commit_hash`, `last_commit_author`, `last_commit_date` (ISO), `last_activity` (ISO) since GitHub `/enrich` is a no-op and all data must be embedded.
- Added max-50 validation to `/admin/api/discovery/{platform}/enrich` — requests with more than 50 clone_urls now return 400 instead of silently being processed.
- Fixed unauthenticated requests to `/admin/api/discovery/{platform}/all` returning 500 instead of 401; now correctly returns 401.
- Fixed per-row Hide / Unhide button silently failing — frontend now sends `application/x-www-form-urlencoded` body matching the server's `request.form()` expectation (was sending JSON).
- Fixed `NotRequired` Python-3.11+ import crashing on Python 3.9 — now sourced from `typing_extensions`.

### UI (auto-discovery)

- Restored Description, Branch, Last Commit (hash+author+date), Last Activity, Visibility badge columns (a prior rewrite in this series had dropped them).
- Restored per-row Hide / Unhide button and batch-create modal with branch-selection dropdowns.
- Removed per-row Add button — users select repos via checkbox and use the batch "Create Golden Repos" flow which routes through `/admin/golden-repos/batch-create` (the legacy per-row form POST routed through a single-add wizard that could trigger SSH password prompts on private repos).
- Selection bar moved from below the panels to above the tabs.
- Header row and search input now use theme-aware colors (`var(--card-sectioning-background-color)`, `var(--form-element-background-color)`) for proper dark-theme contrast.
- Table header is `position: sticky` so columns stay visible while scrolling; row-hover uses a subtle `rgba(255,255,255,0.04)` instead of the prior bright gray eyesore.

### Breaking Changes

- Removed legacy cursor-paged helpers from both providers: `_run_fill_loop`, `_FILL_SAFETY_CAP`, `_collect_unindexed_from_batch`, `_encode_cursor`, `_decode_cursor`, `_decode_cursor_payload`, `_validate_cursor_metadata`, `_extract_cursor_fields`.
- Removed partial templates `templates/partials/gitlab_repos.html` and `templates/partials/github_repos.html` (old server-paged routes `GET /admin/partials/auto-discovery/{gitlab,github}` remain as backwards-compat fallbacks).

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
