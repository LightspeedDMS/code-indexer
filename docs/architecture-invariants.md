# CIDX Architecture Invariants (Detailed Reference)

This file holds the detailed per-story/per-bug implementation invariants extracted from CLAUDE.md to keep that file tight. CLAUDE.md carries the short invariant + a pointer here. For each area, the deepest reference (docs/<topic>.md) is linked where one exists.

## Table of Contents

- [X-Ray](#x-ray)
- [Auth, TOTP, JWT](#auth-totp-jwt)
- [Golden Repo and Versioned Snapshots](#golden-repo-and-versioned-snapshots)
- [Query-Path and Embedding Caches](#query-path-and-embedding-caches)
- [Embedding Coalescer and Governor](#embedding-coalescer-and-governor)
- [Indexing and Migrations](#indexing-and-migrations)
- [Auto-Updater and Pace-Maker](#auto-updater-and-pace-maker)
- [Description-Refresh](#description-refresh)
- [Dep-Map and cidx-meta](#dep-map-and-cidx-meta)
- [Server Memory and Pooling](#server-memory-and-pooling)
- [Background Jobs](#background-jobs)
- [Global Repo Alias Fallback](#global-repo-alias-fallback)
- [Benchmarks](#benchmarks)
- [Fault Injection and Memory Retrieval](#fault-injection-and-memory-retrieval)

---

## X-Ray

Deepest references: `docs/xray-architecture.md`, `docs/xray-sandbox.md`.

### X-Ray Module Lazy-Load Invariant (Epic #968 / Story #969)

`src/code_indexer/xray/` wraps tree-sitter. Lazy-load discipline: `tree_sitter` and `tree_sitter_languages` imported ONLY inside `AstSearchEngine.__init__()`. CLI startup unaffected (~0.57s, budget 2.0s).

**CI gate**: `tests/unit/xray/test_lazy_load.py` -- SUBPROCESS test asserting tree-sitter absent from `sys.modules` after CLI import. BLOCKING.

**Key invariants**:
- Raw `tree_sitter.Node` NEVER exposed to evaluator code -- always wrapped in `XRayNode` (`__slots__ = ("_node",)`, normal assignment, NO `object.__setattr__`).
- `supported_languages`/`extension_map` are INSTANCE-level (conditional `terraform`/`.tf` when HCL grammar present in Python; mandatory in Rust).
- 17 mandatory languages in Rust xray-core: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css, hcl/terraform, yaml, sql, xml, groovy, c, cpp. Python xray supports 12 (hcl conditional via `_hcl_available()`; c, cpp added in Story #1077). Extensions mjs/cjs map to the javascript grammar; c uses `.c`/`.h`, cpp uses `.cc`/`.cpp`/`.cxx`/`.c++`/`.hpp`/`.hh`/`.hxx`/`.h++` (a `.h` C++ header parses under the C grammar and may emit ERROR nodes on C++-only syntax).
- **Dependency**: `tree-sitter>=0.21,<0.22` and `tree-sitter-languages==1.10.2` -- CORE deps since v10.2.1.

### X-Ray Sandbox Security Boundary (Epic #968 / Story #970)

Three defense layers: AST whitelist (Layer 1) + stripped builtins (Layer 2) + multiprocessing isolation (Layer 3). Dunder-access block via 39-name `DUNDER_ATTR_BLOCKLIST`. Timeout: `HARD_TIMEOUT_SECONDS=5.0` (SIGTERM), +1.0s SIGKILL grace. Pipe read BEFORE `is_alive()`. NO `signal.alarm` (FastAPI worker threads).

-> Full reference: `docs/xray-sandbox.md`

### X-Ray Search Engine and MCP Tool (Epic #968 / Story #972)

Two-phase pipeline: Phase 1 regex walk -> Phase 2 sandboxed evaluator over `XRayNode` ASTs.

**Key invariants**:
- Evaluator contract: 6 globals (`node`, `root`, `source`, `lang`, `file_path`, `match_positions`). Must return `{"matches": [...], "value": <any>}` -- bool REJECTED. Legacy `match_byte_offset`/`match_line_number`/`match_line_content` always `None`.
- Allowed nodes: Groups C (If/For/While/Break/Continue/Pass), E (BinOp/operator), G (FunctionDef/arguments/arg -- no Lambda), B comprehensions (ListComp/GeneratorExp/IfExp -- no SetComp/DictComp). Groups D (Try/ExceptHandler/Raise) and F (Import/ImportFrom) are BANNED. Still banned: `class`/`lambda`/`with`/`global`/`nonlocal`/`async`/`await`/`yield`/`try`/`import`. SAFE_BUILTIN_NAMES: 8 entries: `len, any, all, range, enumerate, sorted, min, max`. Structured `ValidationResult` fields: `error_code`, `offending_construct`, `offending_line`.
- Omni multi-repo: `repository_alias` accepts string, list-of-strings, or JSON array. Multi-repo returns `{job_ids, errors}`.
- Async job pattern: returns `job_id`, clients poll `GET /api/jobs/{job_id}`. Pre-flight `sandbox.validate()` before job submission. `await_seconds` in [0.0, 120.0] (warning logged at >30.0).
- v10.5.0 evaluator extensions: `match_positions[i]["ast_node"]` (XRayNode at byte offset), `{"skip": True}` early bail-out, `{"file_role": str}` in return dict surfaced in `file_metadata[]`. XRayNode helpers: `is_in_try_resources()`, `enclosing_method_body()`, `node_at_byte_offset()`.

-> Full reference: `docs/xray-architecture.md`

### X-Ray Spawn-Driver Architecture (Bug #994)

`XRaySearchEngine.run()` delegates Phase 2 evaluator execution to `PythonEvaluatorSandbox.run_batch()`, which spawns a clean driver process via `multiprocessing.get_context("spawn")`. The driver imports tree-sitter once, then forks per-file evaluators via `sandbox.run()` (inheriting the driver's clean ~50MB state, not the parent's potentially 2GB+ state).

**Key invariants**:
- Parent (main process): validates evaluator code, reads files, detects languages, builds file_specs -- NO tree-sitter in this path (just extension mapping).
- Driver (spawn'd): imports tree-sitter + AstSearchEngine, creates PythonEvaluatorSandbox, processes files via ThreadPoolExecutor, each file fork-evaluated from driver state.
- Results pipe back as `List[Tuple[matches, errors, meta]]`.
- `_evaluate_file()` kept as lower-level test API -- existing unit tests call it directly.
- `_run_inline_batch()` path still exists (activated by passing `ast_engine` to `run_batch`) -- reserved for in-process testing.

### Rust Xray Native Engine (Epic #1019)

Rust replacement for the Python xray evaluator pipeline. Located at `rust/xray-core/` (library), `rust/xray-cli/` (CLI binary), `rust/xray-benchmarks/` (benchmark suite).

**Key architecture**:
- `OwnedNode` (owned_node.rs): Heap-allocated AST node tree. Shares file source text via `Arc<str>` -- all nodes in a file hold a clone of one Arc, slicing via `(start_byte, end_byte)` through the `text()` method. Eliminates O(N) per-node String allocations.
- `scanner.rs`: rayon `par_iter` parallelism with `thread_local!` Parser reuse (Parser is !Send but reusable within a thread).
- `compiler.rs`: Compiles user evaluator Rust code to `.so` via `rustc --crate-type cdylib`. PREAMBLE defines the OwnedNode/EvalFinding types visible to evaluator code. `XRAY_ABI_VERSION` must match between PREAMBLE and dynlib.rs loader.
- `dynlib.rs`: Loads compiled `.so` via libloading. ABI version check before trusting function pointers.
- `evaluators.rs`: Built-in evaluators (catch_rethrow, deep_nesting, etc.).
- `validator.rs`: AST whitelist -- no unsafe, no std::fs/net/process, no raw pointers in evaluator code.

**Allocator constraint**: Custom allocators (jemalloc, mimalloc) are INCOMPATIBLE with the dynlib architecture. The host process and compiled evaluator `.so` use different allocators, causing segfaults when owned types cross the boundary. System malloc only.

**Benchmark evaluators** (`rust/xray-benchmarks/`):
- `bench.sh <target-dir> [evaluator]`: Runs COLD/WARM/WARM2 cycles per evaluator. Purges cache for cold run. Passes target directory to xray-cli (which walks via `collect_files()`).
- 4 evaluators: `catch_rethrow.rs`, `deep_nesting.rs`, `long_method.rs`, `method_census.rs`.
- Baseline (19K files): ~5.7-6.4s per evaluator. Optimized (Arc<str> + thread_local Parser): ~4.9-5.4s (~15% improvement).

### X-Ray Pattern Library (Story #1031)

Persistent storage of reusable Rust evaluator patterns in cidx-meta under `xray-patterns/`. Service: `XrayPatternService` in `server/services/xray_pattern_service.py`.

**Key invariants**:
- Storage layout: `cidx-meta/xray-patterns/{scope}/{name}.yaml`. `__any__/` for cross-repo, `{repo-alias}/` for repo-specific.
- Resolution order: repo-specific first, then `__any__/` fallback. NEVER reverse this.
- Path traversal protection: scope and name reject `/`, `\`, `..` characters before any filesystem access.
- Const injection: parameters declared in YAML become typed `const NAME: type = value;` lines prepended to evaluator code before Rust compilation. Supported types: usize, i64, f64, bool, str.
- `pattern_name` in xray_search/xray_explore is mutually exclusive with `evaluator_code`. Handler helper: `_resolve_evaluator_code()` in `handlers/xray.py`.
- `_seeds_ensured` module-level flag: seed patterns (catch-rethrow, deep-nesting) checked once per process lifetime.
- `store_xray_pattern` MCP tool: overwrite defaults to false. Evaluator code validated via `validate_rust_evaluator()` before storage.

---

## Auth, TOTP, JWT

Deepest reference: `docs/totp-elevation.md`.

### TOTP Step-Up Elevation (Epic #922 / Story #923)

**Essential invariants** -- NEVER refactor these:
- Three error codes exactly: `totp_setup_required` (403), `elevation_required` (403), `elevation_failed` (401).
- Kill switch returns HTTP **503 NOT 403**.
- Recovery codes (10, HMAC-SHA256-hashed) grant narrow `scope=totp_repair` only -- never full-scope.
- TOTP replay prevention via atomic CAS on `last_used_otp_counter`.

-> Full reference: `docs/totp-elevation.md`

### CLI Elevation Retry (Story #980)

`with_elevation_retry` wraps ALL `cidx admin users` and `cidx admin groups` commands. On 403 `elevation_required` -> prompt TOTP -> `POST /auth/elevate` -> single retry. On `totp_setup_required`/`elevation_failed`: `sys.exit(1)`, no retry loop. Always unwrap via `body.get("detail", {})` (FastAPI wraps HTTPException detail).

-> Full reference: `docs/totp-elevation.md`

### JWT Logout Token Revocation (Story #1163)

Both logout routes (`GET /logout` via `web_router` and `GET /user/logout` via `user_router` in `server/web/routes.py`) blacklist the JWT `jti` at logout time using `get_token_blacklist().add(jti)`. The blacklist is DB-backed (`TokenBlacklist` in `server/app.py`, wired at lifespan) so the revocation is cross-worker and cross-node — every uvicorn worker and every cluster node rejects the revoked jti on the next request.

**Key invariants** -- NEVER remove these:
- `_extract_jti_from_request(request)` (private helper in `routes.py`) tries `Authorization: Bearer` header first, then `cidx_session` cookie; returns `None` without raising on any decode error.
- The JTI-blacklist block is wrapped in `try/except` in both logout routes -- failure logs a WARNING but NEVER prevents the 303 redirect and session clear.
- `TokenBlacklist.prune_expired(ttl_seconds)` (added in Story #1163) deletes rows where `blacklisted_at < time.time() - ttl_seconds` from SQLite (`_sqlite_prune`) or PostgreSQL (`_pg_prune` using `DELETE ... RETURNING jti`); also evicts deleted JTIs from the local in-memory set.
- `DataRetentionScheduler._safe_prune_token_blacklist` wires pruning into both `_execute_cleanup_sqlite()` and `_execute_cleanup_pg()`. TTL = `config.jwt_expiration_minutes * 60` (read live from config_service each cycle; NOT hardcoded). Result key `token_blacklist_deleted` is included in both result dicts and in `total_deleted`.
- The `blacklisted_at` column is a NUMERIC UNIX timestamp (seconds, `time.time()`), NOT an ISO string -- the generic `_cleanup_table` helper (ISO string comparison) MUST NOT be used for `token_blacklist`.

### Maintenance Mode Localhost-Only (Epic #922 / Story #924)

Write endpoints (`POST .../maintenance/enter|exit`) restricted to loopback (`127.0.0.0/8`, `::1`, `::ffff:127.x.x.x`) via `require_localhost`. MCP enter/exit tools removed. Read endpoints unaffected. Reverse-proxy must NOT forward these externally.

---

## Golden Repo and Versioned Snapshots

### Activation Branch-Delta Reindex (Bug #1203)

Activation of a golden repo on a NON-DEFAULT branch (and `switch_branch` / `sync_with_golden_repository`) now runs a branch-aware delta semantic reindex as its final phase, via `ActivatedRepoIndexManager.run_branch_delta_index(repo_path)` (public wrapper over `_execute_semantic_indexing(repo_path, clear=False)` -> `cidx index` subprocess -> SmartIndexer git-topology delta). Before #1203 the CoW clone copied the golden's DEFAULT-branch index byte-for-byte and never reindexed, so non-default branches silently served default-branch embeddings for files that differ.

**Key invariants -- NEVER violate:**
- All three lifecycle sites route through the single helper `ActivatedRepoManager._run_branch_delta_index`. Skip guards: target `branch == golden_repo.default_branch` (CoW index already correct), `user_alias.endswith("-global")` (global repos share the golden's immutable index), or `self._index_manager is None`.
- `_index_manager` is wired POST-HOC in `startup/lifespan.py` (mirrors the Bug #1044 `_clone_backend` block): `arm._index_manager = ActivatedRepoIndexManager(activated_repo_manager=arm, background_job_manager=...)`. Passing `activated_repo_manager=arm` explicitly avoids the circular-construction default at `activated_repo_index_manager.py:84`. If this assignment is removed, the fix goes INERT (the production ARM falls back to None and silently skips reindex). Guard: `tests/unit/server/startup/test_lifespan_index_manager_wiring_bug1203.py`.
- After a SUCCESSFUL reindex, `_run_branch_delta_index` invalidates the server in-memory caches for the repo via PREFIX eviction: `get_global_cache().invalidate_prefix(index_base)` and `get_global_id_index_cache().invalidate_prefix(index_base)` where `index_base = {repo_path}/.code-indexer/index`. The HNSW/id-index caches are keyed by the per-COLLECTION path (`{repo}/.code-indexer/index/{collection}`, resolved), NOT the repo root -- a plain `invalidate(repo_path)` matches nothing and silently serves stale results. NO FTS cache invalidation: the server FTS query builds `TantivyIndexManager(fts_index_dir)` directly from disk and does not read `get_global_fts_cache()`, so a fresh per-query manager picks up the rewritten index automatically.
- Failure is correctness-first: a failed reindex raises `ActivatedRepoError` (activation also `shutil.rmtree`s the freshly-created orphan clone before re-raising). Cache invalidation is non-fatal (WARNING, never fails an already-successful reindex) but runs on the success path.

### Golden Repo Versioned Path (mutable-vs-immutable -- resolver-accurate)

- **Base clone** (`golden-repos/{alias}/`): mutable -- where git ops and indexing happen
- **Versioned snapshot** (`.versioned/{alias}/v_{timestamp}/`): IMMUTABLE after creation

**Resolver reality (Story #1082 audit -- corrects the prior "served to queries = immutable" claim).** `GoldenRepoManager.get_actual_repo_path(alias)` (`server/repositories/golden_repo_manager.py:2150`) is **Priority-1 / Priority-2**: if the **mutable base clone** `golden_repo.clone_path` exists on disk it is returned (line 2206-2216); only when it does NOT exist does it fall through to the latest `.versioned/{alias}/v_*` snapshot (line 2218+). So for GOLDEN/ACTIVATED repos the query path commonly receives the **mutable** path, NOT the immutable snapshot. GLOBAL repos differ: the alias JSON `target_path` is repointed to a `.versioned/{alias}/v_*` snapshot after the first refresh (`global_repos/refresh_scheduler.py:1171/1429/1623`), so `AliasManager.read_alias()` yields the immutable snapshot for global repos.

Consequence for any path-keyed cache: do NOT assume the query-path string is immutable. Use the explicit predicate `is_immutable_versioned_snapshot(path)` (`server/services/query_path_cache.py`) -- it returns True ONLY for a validated `.versioned/{alias}/v_*` shape -- and **default to a SHORT TTL** for anything it does not prove immutable.

Alias JSON `target_path` is authoritative for global repos. Use `GoldenRepoManager.get_actual_repo_path(alias)` for golden/activated. NEVER modify/checkout/index inside `.versioned/`. See memory: `feedback_versioned_path_trap.md`.

### Canonical Versioned-Snapshot Convention + Backend-Aware Cleanup (Bug #1084 Phase A)

Old versioned snapshots used to leak forever on the cow-daemon/ONTAP backends because cleanup was gated on the literal substring `".versioned" in current_target`, which only matches the LocalCloneBackend layout. Phase A replaces that with ONE canonical convention + ONE predicate + backend-aware deletion.

**Single canonical predicate -- `src/code_indexer/server/storage/shared/snapshot_paths.py`:**
- `is_versioned_snapshot(path, *, mount_point=None) -> bool` is the ONLY authority. Canonical rule: path contains a `/.versioned/` segment AND leaf matches `v_\d+` AND the immediate parent is the namespace dir (`.../.versioned/{ns}/v_<ts>`). Transition clause (recognition only, NEVER created): legacy cow-daemon `{mount}/{ns}/v_<ts>` and flat ONTAP `{mount}/v_<ts>` are recognized ONLY when `mount_point` is supplied. `{mount}/activated-repos/...` and the master base clone (`golden_repos_dir/{repo}`) MUST be False.
- Facade: `VersionedSnapshotManager.is_versioned_snapshot(path)` supplies the backend mount automatically. Callers hold the facade; never reimplement the substring test. Scheduler/manager helpers `_is_versioned_snapshot` delegate to the facade (module predicate fallback when no snapshot_manager is wired).

**Canonical cow-daemon layout + sanitization symmetry (`clone_backend.py`):** `CowDaemonBackend.create_clone` routes versioned snapshots through `create_clone_at_path(dest={mount}/.versioned/{sanitized_ns}/{sanitized_name})` (daemon registers identity `(ns, name)` from dest parent/leaf -- DB unaffected). `delete_clone` skips a leading `.versioned` segment when parsing `(ns, name)` (handles canonical AND legacy). `_sanitize_identifier` (dots->underscores) is applied uniformly across create/delete/list_clones/clone_exists. Local backend unchanged (already canonical); ONTAP layout unchanged (gated -- per-swap delete-by-basename still works).

**Discovery API (`VersionedSnapshotManager`):** `list_snapshots(alias) -> [(path, ts)]` (ascending) and `latest_snapshot(alias) -> Optional[str]`. cow-daemon: `list_clones(sanitized_ns)` mapped to mount paths (canonical + legacy share the daemon ns). local/CoW-fs: glob `golden_repos_dir/.versioned/{ns}`. ONTAP/FlexClone: returns `[]` (retention disabled -- `list_clones` ignores namespace). Reconstruction sites MUST use this API, never re-glob `golden_repos_dir/.versioned`.

**Three cleanup gates (Defect A) -- `refresh_scheduler.py` swap site, `golden_repo_manager.py` `_cb_swap_alias` + add-index:** all use `current_target and <facade>.is_versioned_snapshot(current_target) and current_target != master_path` (master_path = `golden_repos_dir/{repo}`). All None-guarded (add-index previously raised TypeError on None).

**Backend-correct deletion behind the refcount gate (Defect B) -- `cleanup_manager.py`:** `CleanupManager` is handed the snapshot manager via `set_snapshot_manager()` (wired in `lifecycle/global_repos_lifecycle.py`). Its `_delete_index` calls `snapshot_manager.delete_snapshot("", path)` for predicate-recognized snapshots (daemon DELETE / FlexClone free / local rmtree-inside-manager); rmtree remains the fallback for non-snapshot/local paths. The QueryTracker refcount-zero gate + backoff + circuit breaker are UNCHANGED -- deletion still only fires after refcount reaches zero. Direct swap-site deletion remains forbidden (would delete snapshots under in-flight NFS queries).

**Keep-last-N retention (defense in depth) -- `RefreshScheduler._enforce_retention`:** after each successful swap (all 3 sites) lists via the discovery API and schedules (through the same refcount-gated CleanupManager) all but the N newest, NEVER the current `target_path` or `previous_path` (the latter read via `AliasManager.get_previous_path`, which this finally wires -- Defect D). N = runtime knob `ServerConfig.snapshot_retention_keep_last` (default 3, Web-UI configurable; values < 1 fall back to 3). Enabled on local + cow-daemon; inert on ONTAP (discovery returns []).

**Defect C/E -- `refresh_scheduler.py`:** `_has_local_changes` and `_restore_master_from_versioned` use the discovery API (`_latest_versioned_timestamp` / `latest_snapshot`) instead of the `golden_repos_dir/.versioned` glob, so change-detection is correct and a lost master is restorable on cow-daemon.

**Phase B (NOT done here):** secondary `.versioned`-substring consumers (provider-index write guard in `repos.py`, `query_path_cache.is_immutable_versioned_snapshot`, SCIP discovery, dep-map cidx-meta read, `_legacy.py`) still need migration to the canonical predicate / discovery API. The ONTAP canonical-layout + alias-scoped naming work is gated on confirming ONTAP is deployed.

### ActivatedRepoManager clone_backend Wiring (Story #1034 / Bug #1044)

`ActivatedRepoManager._clone_with_copy_on_write` routes CoW clones through `self._clone_backend.create_clone_at_path(...)` and hard-raises if `_clone_backend is None` (guard at `activated_repo_manager.py:2643`). The constructor declares `clone_backend: Optional[CloneBackend] = None`, so construction succeeds without it -- the failure only surfaces on the first activation.

**Wiring is post-hoc in lifespan**, not at construction. In `startup/lifespan.py`, the `if snapshot_manager is not None:` block injects `snapshot_manager._clone_backend` into the ARM reachable from `golden_repo_manager.activated_repo_manager` -- matching the same belt-and-suspenders pattern used for `_snapshot_manager` on `GoldenRepoManager` and `RefreshScheduler`.

**Invariant**: any refactor of `startup/lifespan.py` or `startup/service_init.py` MUST preserve the `arm._clone_backend = snapshot_manager._clone_backend` assignment. Regression guard at `tests/unit/server/startup/test_lifespan_clone_backend_wiring_bug1044.py` (source-text + source-order checks) will fail if removed.

---

## Query-Path and Embedding Caches

Deepest reference: `docs/query-embedding-cache.md`.

### Query-Path Drift-Safe Caching (Story #1082)

Per-query server orchestration glue is cached off the GIL-bound hot path WITHOUT extra RAM or workers, with a precise staleness policy. Single primitive `TTLCache` in `server/services/query_path_cache.py` (thread-safe, per-key **single-flight** -- no thundering herd on cold miss/expiry -- bounded LRU, hit/miss/reload/invalidate/evict counters, optional NO-TTL mode that is STILL bounded).

**Staleness model (do not violate):**
- **ZERO staleness** for (a) static package model-spec YAML and (b) keys PROVEN immutable by `is_immutable_versioned_snapshot()`. A golden-repo refresh makes a NEW versioned path = new key = cache miss, never an in-place stale read. These use NO TTL but are still bounded (LRU + alias-repoint / old-version invalidation).
- **BOUNDED staleness <= the configured short TTL `T`** for mutable / not-provably-immutable repo paths (incl. the Priority-1 base clone), provider-config state, and DB metadata. TTL is a self-healing net even where event invalidation exists.
- **NEVER cached:** auth-bearing rows (api keys / key-hashes, user rows, MCP credentials, permissions, group membership, token validation) -- so revocation / access-gating changes take effect immediately, zero grace, on every node.

**What is implemented:**
- **Load-once model-spec:** `voyage_ai._get_voyage_model_specs()` / `cohere_embedding._get_cohere_model_specs()` parse the static YAML ONCE per process (was per `VoyageAIClient.__init__`, i.e. per query). HTTP client stays per-request for thread safety -- only the parsed model-spec state is shared.
- **RepoConfigCache** (`query_path_cache.py`): composes a NO-TTL bounded sub-cache (predicate-proven `.versioned/v_*` paths) + a SHORT-TTL bounded sub-cache (default, everything else). Wired in `startup/lifespan.py` as `app.state.repo_config_cache`; consumed by `search_service._load_repo_config()` (returns None registry -> direct load on CLI/in-process, NOT a fallback). Knobs on `CacheConfig`: `query_path_cache_enabled` (kill-switch), `repo_config_cache_ttl_seconds` (30), `repo_config_cache_max_entries` (2048) -- named, Web-UI-tunable, no hardcoded literals.
- **`provider_config_digest()`**: normalized digest over ALL behavior-affecting fields (provider, model, key-FINGERPRINT never raw secret, api_endpoint, connect_timeout, timeout, Cohere max_retries/retry_delay/exponential_backoff). Two repos same provider/model/key but different endpoint/timeouts/retries -> distinct digests -> never share state.
- **`codebase_dir mismatch` de-spam:** `config.py` logs the WARNING at most once per distinct config path (process-local memo); the per-load Bug #1033 NFS multi-mount reconciliation still runs on EVERY load -- no normalized `codebase_dir` persisted to shared state.

**Deliberately deferred (KISS / scope):** explicit refresh-event `invalidate()` wiring through the refresh scheduler (the `invalidate()` API exists + is unit-tested, but Scenario 6 is already satisfied by new-path=new-key + bounded old-version eviction + SHORT TTL); provider-state object cache (the model-spec parse -- the real per-query cost -- is already eliminated by the load-once memo; caching a per-request client is forbidden for thread safety); the confirm-first DB-metadata `list_repos` cache and health-monitor memoization (NOT confirmed non-auth-safe at the call sites in scope, so deferred). NEVER cache auth-bearing data to "improve" any of these.

### Query-Embedding Cache (Epic #1103)

Server-side cache of query embeddings on the query path, both providers (voyage-code-3 1024-dim, embed-v4.0 1536-dim). Wraps `coalesced_query_embedding` (`server/services/governed_call.py`) OUTSIDE-IN: the cache intercepts BEFORE the governor/coalescer; the pre-cache body became `_compute_live()` (the EXACT post-S0 body). CLI/daemon are untouched -- the cache is installed only by `startup/lifespan.py` (`set_query_embedding_cache`), so `get_query_embedding_cache()` is None on CLI/solo and the wrap returns the live path (same registry-None gate as the coalescer).

**Hard invariants** -- NEVER violate:
- NEVER lowercase the key. `build_key()` (`server/services/query_embedding_cache.py`) keeps case at every step (CamelCase identifier signal -- empirically top-1 flips ~34% under lowercase on a code index). Anchor-token normalization: first N tokens in order + sorted tail; default anchor depth 2; N>=token count == exact-match; N=0 == sort-all.
- Composite PK `(cache_key, provider, model, dimension)`. NO repo/collection column (embedding is repo-independent). PK is the cross-provider/model/dimension isolation.
- DB access pattern: sync SELECT on lookup (zero-copy read), sync UPSERT on miss (+ `prune_to_max`). Hit path does ZERO synchronous DB writes -- `last_used` is updated asynchronously/best-effort (Bug #1181 Perf Fix #2): `record_hit()` coalesces touches into an in-process dict keyed by `(cache_key, provider, model, dimension)` -> latest float timestamp; a background daemon thread (`qec-touch-flusher`) drains the buffer every ~5s via `touch_last_used_batch()` in ONE transaction. SQLite uses `executemany` in a single `execute_atomic` transaction; PG uses `SET LOCAL synchronous_commit = off` then `executemany` + commit (ephemeral LRU bookkeeping -- crash durability relaxed, row remains valid). Buffer capped at 2048 entries; early flush on cap hit. `QueryEmbeddingCacheBackend` Protocol includes `touch_last_used_batch(items)` (mypy-enforced). `QueryEmbeddingCache.start()` / `stop(timeout)` lifecycle: `start()` called after `set_query_embedding_cache()` in lifespan startup; `stop()` called before `clear_query_embedding_cache()` in shutdown. This is approximate LRU -- the ordering is best-effort, correctness (row validity) is never compromised. NO RAM embedding layer. The shared count cap `max_entries` (default 10000, >=100 floor in `_resolve_max_entries`, single LRU bucket both providers) is the SINGLE true cluster-wide cap.
- Table stores ONLY query-purpose embeddings -- NEVER document-purpose (different Cohere `input_type` semantics). Cache value is ONLY query->vector, NEVER auth-bearing.
- Key format (Story #1149): `s:<config-digest>:<normalized-query>`. The `s:` prefix is provably disjoint from legacy 64-hex SHA-256 keys (passive LRU reset -- old rows age out via prune_to_max, no active clear needed). `config_digest` is the coalescer-registry digest (provider + endpoint + model) so cache identity == coalescer identity. `build_key()` returns None when the normalized-query part exceeds 256 chars -- callers treat None as a MISS and skip lookup/write.
- Migration `028_query_embedding_cache.sql` is additive (`CREATE TABLE IF NOT EXISTS`). Both backends first-class: `QueryEmbeddingCacheSqliteBackend` (solo) + `QueryEmbeddingCachePostgresBackend` (cluster); float32-LE blob (BLOB/BYTEA). All cache ops are fail-open (WARNING + live path; never break a query).

**Semantics**: per-provider mode off/shadow/on (default shadow). off = always live, no lookup/write. shadow = ALWAYS live (returns live), lookup + record cosine on hit / upsert on miss -- measures without changing results ("would-serve rate"). on = HIT returns cached (skips provider) / MISS computes + upserts ("serving rate"). Mode/enabled/anchor read LIVE from `QueryEmbeddingCacheConfig` each call (8 Web-UI settings; no restart).

**S4 bypass**: per-request `no_embedding_cache_shortcut` (default False) on all REST/MCP search endpoints (`SemanticSearchRequest`) skips the cache READ but STILL writes; the off/not-enabled gates fire FIRST. **S5 metrics**: `query_embedding_cache_metrics.py` on `cidx.cache` meter (hit/miss tagged `{mode,provider}`, total_entries ObservableGauge from cheap memo NOT live COUNT, shadow_cosine histogram); built only when cache+telemetry present. **S6 audit**: `embedding_cache_audit.py` runs a 2nd HNSW at the FSV `search()` chokepoint on sampled hits (per-provider `audit_sample_rate`, default 0.0) -- shadow audits the already-computed live vec for free; on-mode sampled hits RE-EMBED one provider call (sampled fraction only; non-sampled on-hits skip the provider). **S0 Cohere fix (Bug #1104)**: query sites passed `embedding_purpose=None` -> Cohere embedded queries as `search_document`; fix sets `embedding_purpose="query"` at all query-embed sites + threads it through the coalescer.

-> Full reference: `docs/query-embedding-cache.md`

### FSV skip_staleness_check for Immutable Versioned Snapshots (Bug #1181 Perf Fix #3)

`FilesystemVectorStore._get_chunk_content_with_staleness` previously called `_compute_file_hash` (reads the entire file + SHA-1) for every git-repo result on every query. For an immutable `.versioned/{alias}/v_{ts}` snapshot the file cannot change, so this second whole-file read is pure overhead.

**Key invariants**:
- `FilesystemVectorStore.__init__` accepts `skip_staleness_check: bool = False`. Default False = CLI and mutable-path behavior byte-identical. No existing call sites change.
- When `skip_staleness_check=True`: Tier-1 branch (file exists) reads content once, then returns immediately as NOT stale WITHOUT calling `_compute_file_hash`. File-deleted branch is unaffected (fires before the skip guard).
- Non-git / payload-content results are unaffected (early return path, never calls `_compute_file_hash`).
- `FilesystemBackend.get_vector_store_client()` sets the flag: inside the existing `if self.hnsw_index_cache is not None:` server-mode guard, calls `is_immutable_versioned_snapshot(str(self.project_root))` (from `server/services/query_path_cache.py`). Import is server-mode-only so CLI never loads server modules.
- Mutable base clones, activated CoW repos, and CLI mode all leave the flag False and continue the full staleness check. The immutability predicate is the SINGLE source of truth -- never skip for any path not proven by `is_immutable_versioned_snapshot`.

---

## Embedding Coalescer and Governor

### Embedding Request Coalescer + 4-Lane Adaptive Governor (Story #1079, refines Bug #1078)

Server-side query-embed coalescing gated by a self-tuning per-lane concurrency governor. Replaces Bug #1078's 2 per-provider budgets. CLI/solo path is untouched.

**Governor — 4 independent lanes** (`server/services/provider_concurrency_governor.py`): `voyage:embed`, `voyage:rerank`, `cohere:embed`, `cohere:rerank` (was 2 per-provider). Each lane owns a `ResizableLimiter` + `AimdController` + ONE sinbin health key (`voyage:embed->voyage-ai`, `voyage:rerank->voyage-reranker`, `cohere:embed->cohere`, `cohere:rerank->cohere-reranker`). `execute(budget, fn, *, acquire_timeout)` API preserved (KeyError on unknown lane; singleton). Lane mapping: `governed_call._get_embedding_budget`->`{provider}:embed`; `reranking._RERANKER_BUDGET`->`{provider}:rerank`.
- **`ResizableLimiter`** (`server/services/resizable_limiter.py`) replaces `BoundedSemaphore`: lock+condition, runtime-resizable K, per-instance bounds (seeded from config, see below). Its `in_flight`/`high_water` are the SINGLE SOURCE OF TRUTH for per-lane telemetry — the governor reads them directly; do NOT reintroduce hand-incremented counters. `_wait_count` stays governor-maintained. Shrink never kills in-flight work; grow `notify_all`s parked acquirers. `acquire()` is monotonic-deadline-bounded (no hang -> `GovernorBusyError`).
- **`AimdController`** (`server/services/aimd_controller.py`) drives the limiter via `set_limit` under the limiter's OWN `Condition` (shared lock domain -> race-free, fully lane-independent: a 429 on one lane never changes another lane's K). +1 after `SUCCESS_THRESHOLD` successes up to K_MAX; halve on a canonical 429 (`provider_backoff.is_rate_limited`) down to K_MIN, decrement ONCE PER 429 ATTEMPT; `COOLDOWN_SECONDS` blocks immediate re-grow. Structured WARNING (`old_k`/`new_k`) on each real decrease.

**429 normalization (isolated commit, latent Bug #1078 fix)** — `provider_backoff.is_rate_limited(exc)` is the canonical classifier (true for `httpx.HTTPStatusError` 429 or `ProviderRateLimitedError`). Providers MUST re-raise a 429 INTACT on the `retry=False` query path (Voyage previously masked it as generic `RuntimeError` -> backoff/AIMD never saw it; `voyage_ai.py` now `if is_rate_limited(e): raise` before wrapping). `execute_with_backoff` retries iff `is_rate_limited`. NEVER re-mask a 429.

**`EmbeddingCoalescer`** (`server/services/embedding_coalescer.py`) — one per `:embed` lane; ONE lock; governor is the SOLE limiter (holds NO semaphore/in_flight; dispatches via `execute_with_backoff(lambda: governor.execute(lane, do_call, acquire_timeout))` so backoff sleeps OUTSIDE the slot). Exactly one dispatcher per batch ALWAYS completes every caller's Future (success OR exception — shared fate; no hang past ACQUIRE_TIMEOUT). **Dual-constraint sealing** guarantees one sealed batch == exactly ONE provider HTTP call (no sub-split): seals before a text would exceed EITHER the texts cap (`_get_texts_per_request()`; Voyage has none -> config ceiling) OR `int(provider._get_model_token_limit() * margin)` where the margin is derived from the provider spec (`safety_margin_percentage`, 0.9 fallback) — IDENTICAL to the provider's internal split predicate, using the provider's OWN token counter (`_count_tokens_accurately`/`_count_tokens`). Count-mismatch is a RAISED `ValueError` (survives `python -O`), not `assert`.

**Query embedding_purpose invariant (Bug #1104)** — ALL server query-path embedding calls MUST pass `embedding_purpose="query"`. This applies on BOTH the direct path (`governed_query_embedding`) and the coalesced path (`coalesced_query_embedding` -> `EmbeddingCoalescer.submit(text, embedding_purpose="query")`). Cohere maps `"query"` -> `input_type="search_query"` and anything else -> `"search_document"` (via `CohereEmbeddingProvider._map_embedding_purpose()`). Voyage is unaffected (no `input_type` in its API). Before #1104 fix, `search_service.py` and `temporal_search_service.py` passed `embedding_purpose=None` and the coalescer dropped the purpose entirely, causing all Cohere server queries to be embedded as `search_document`. NEVER pass `embedding_purpose=None` or omit the argument at a query-embed call site. Regression guard: `tests/unit/server/services/test_embedding_purpose_1104.py`.

**Server-gating + kill switch** — `coalesced_query_embedding` (`server/services/governed_call.py`) is the single entry point on all 4 query sites (`search_service.py`, `mcp/handlers/search.py`, `temporal/temporal_search_service.py`, `storage/filesystem_vector_store.py`); call sites are identical on CLI and server (NO per-site `if cli/server`). `CoalescerRegistry` (`server/services/coalescer_registry.py`) is built ONCE in `startup/lifespan.py` (before `yield`) and cleared after; `get_coalescer_registry()` returns None until then — CLI/solo/daemon NEVER build one, so they stay on the direct `governed_query_embedding` single call (no batching). Provider keys are seeded into env FROM runtime config by `seed_api_keys_on_startup` (lifespan, BEFORE registry build) — a lane whose key is absent is simply absent (explicit, logged; falls back to direct). Any refactor of `lifespan.py` MUST preserve the `set_coalescer_registry`/`clear_coalescer_registry` calls (guard: `tests/unit/server/startup/test_lifespan_coalescer_registry_wiring.py`).

**Runtime config (NOT bootstrap; no env vars; mirrors `memory_retrieval_enabled`)** — `coalesce_enabled` (default True; read LIVE each call -> kill switch + hot-reload), `coalesce_max_batch_size` (default 96 == Cohere texts cap; live ceiling, hot-reloads at seal time), `coalesce_k_min=8`/`coalesce_k_max=32` (construction-scoped AIMD/limiter K bounds seeded into the governor at build; NOT live-reload, clamp-validated with 8/32 fallback). Initial K seed (`query_provider_max_concurrency`) clamps to `[k_min, k_max]`. Observability: governor `current_k` per lane, AIMD-decrease WARNING. The per-instance coalescer `batches_dispatched`/`texts_coalesced` counters were deleted (Story #1295, Epic #1288 final) -- the durable, cluster-aggregated equivalent is `WindowedCacheMetrics.overall.{batches,texts_coalesced}`, sourced from `search_embed_event` (Story #1293/#1294) and re-exported via `EmbeddingCacheOtelMetrics` (DB-backed ObservableGauge, `server/services/embedding_cache_otel_metrics.py`).

**Per-worker governor scaling (Story #1165)** — `query_provider_max_concurrency` is the PER-NODE total provider-concurrency budget. At governor construction (auto-seed path only, i.e. `ProviderConcurrencyGovernor()` with no explicit `max_concurrency` argument), the per-node budget is divided by `config.workers` so combined embedding pressure across all uvicorn workers on the node stays within the configured limit. Per-worker seed = `max(k_min, per_node_budget // workers)`, then clamped to `[k_min, k_max]`. Key invariants: workers=1 is byte-identical to pre-#1165 behavior (no change); workers=0 or negative falls back to 1 (no division); explicit `max_concurrency` construction (used in tests) is NEVER divided. Cross-node budgeting remains the operator's responsibility — each node has its own `query_provider_max_concurrency`. This division introduces NO shared/cross-process state; it is pure per-process construction-time arithmetic.

-> Deterministic fault-injection gate: `tests/integration/server/test_coalescer_fault_injection_1079.py`.

---

## Indexing and Migrations

### Indexing Path Has No Job/Subprocess/Per-File Timeouts (Bug #1218)

The indexing / golden-repo-registration / SCIP-generation path carries NO wall-clock timeout on the whole job, the whole subprocess, or any per-file/per-batch unit. A large repo legitimately takes hours (runtime tracks normal outbound embedding-provider latency); bounding the job on a clock SIGKILLs healthy indexing, and per-file timeout-swallow handlers produce a silent partial index that reports success.

**The ONLY legitimate timeout on this path is the per-request outbound embedding-provider HTTP call** (connect/read on a single POST to Voyage/Cohere) plus its retry/backoff. Those stay (`voyage_ai.py`, `cohere_embedding.py`, `cohere_multimodal.py`, `provider_backoff.py`).

**Key invariants -- NEVER reintroduce:**
- `run_with_popen_progress` (`services/progress_subprocess_runner.py`) has NO `timeout` parameter, NO watchdog thread, NO `os.killpg(...SIGKILL)`, NO `returncode == -9` detection. Do not add a job/subprocess clock here or at any caller.
- No `future.result(timeout=...)` + swallow-and-skip on the per-file/per-batch path (`file_chunking_manager.py`, `high_throughput_processor.py`, `temporal/temporal_indexer.py`). A genuine post-retry embedding failure must PROPAGATE and fail the job LOUD -- never `except TimeoutError: skip this file`, never a silent partial index (Messi #2 Anti-Fallback, #13 Anti-Silent-Failure).
- The removed `ScipConfig` fields `indexing_timeout_seconds`, `scip_generation_timeout_seconds`, `registration_indexing_timeout_seconds` are GONE; both `ScipConfig(**...)` construction sites strip them from old persisted configs (backward compat). Do not re-add.
- KEEP (NOT a job clock, do not remove): governor acquire, coalescer join, rerankers, BackgroundJobManager SIGTERM/SIGKILL (cancel/shutdown only), and short local-git metadata subprocess bounds (progress-estimate only, not index correctness).
- **Fail-loud on total failure (anti-silent-failure):** `cidx index` exits NON-ZERO when `files_processed == 0 and failed_files > 0` (`cli.py` index completion block). This propagates through `run_with_popen_progress` (raises `IndexingSubprocessError` on returncode != 0) so a golden-repo registration whose indexing all-fails (e.g. bad provider key) FAILS the registration instead of reporting success with an empty index. The "All files failed to index" message is deliberately distinct from the benign "No files found to index" allowlist so it is not swallowed.
- **Registration failure cleans up its own clone:** `golden_repo_manager._cleanup_failed_clone(_clone_path_for_cleanup)` runs in all `background_worker` failure paths and removes ONLY the freshly-created clone before re-raising, so a retry never hits "destination path already exists". Without this, a failed registration leaves an orphan clone that permanently blocks retries.
- **Known residual follow-up (NOT fixed in #1218):** the daemon in-process FTS rebuild (`daemon/service.py`) can still report `status: success` on an all-failed in-process rebuild -- a separate path, tracked separately.

### Per-Commit Temporal Dual-Embedder Indexing (Epic #1289, Stories #1290/#1291/#1292)

Temporal (git-history) indexing was rewritten as a per-commit-aggregation model with pluggable, coexisting embedder adapters, replacing the deleted per-file-diff layout (separate vectors per changed file plus a message-only vector embedded on its own). Deepest reference: `docs/temporal-search.md`; projection/recall tooling: `scripts/analysis/temporal_vector_projection.py`, `scripts/analysis/temporal_recall_gate.py`.

**Per-commit aggregation**: `commit_aggregator.build_aggregated_document()` produces ONE document per commit -- the commit message once at the head, followed by each changed file's diff under a `--- <path> ---` header (binary files and pure renames skipped). `contextual_chunker.chunk_aggregated_document()` chunks that document by CHARACTERS with a PER-ADAPTER overlap (`TemporalEmbedder.overlap_percentage`: 0% for the contextual voyage-context-4 adapter, 15% for the standard embed-v4.0 adapter) -- the identical aggregated text yields DIFFERENT chunk boundaries per adapter. Point ids are unified as `{project}:commit:{hash}:{j}`.

**Pluggable embedder registry** (`services/temporal/embedders/`): `TemporalEmbedder` ABC (`base.py`) + `register_embedder()`/`create_embedder()` (`registry.py`). Two first-class adapters: `ContextualTemporalEmbedder` (voyage-context-4, 1024-dim, 0% overlap, POST `/v1/contextualizedembeddings`) and `StandardTemporalEmbedder` (embed-v4.0, 1536-dim, 15% overlap, `CohereEmbeddingProvider.get_embeddings_batch`). `TemporalConfig.embedders` (set) + `.active_embedder` control which adapters build shards and which one recall defaults to; `temporal_embedder` is an explicit per-query override (REST/MCP field) that NEVER silently falls back to `active_embedder` -- an override naming an embedder with no indexed collections returns a typed empty result.

**Quarterly sharding retained after aggregation** (Story #1292 AC6): per-commit aggregation already delivered the primary vector-count reduction; quarterly sharding (`code-indexer-temporal-{model_slug}-{YYYY}Q{N}`) is an orthogonal, complementary optimization. Shard count grows linearly with CALENDAR time (`4*N` shards per embedder for N years of history), independent of commit volume or aggregation strategy. `--time-range-all` fans out across the (small, bounded) full shard set; a narrow `--time-range` only opens the shards whose quarter overlaps the window. Quarterly boundaries also give a natural, low-blast-radius retention/archival unit (delete a shard directory) and keep each shard's HNSW rebuild cost bounded to one quarter's vectors.

**Incremental refresh is reconcile-based, NOT cursor-bounded**: `TemporalIndexer._blank_out_legacy_collections()` runs unconditionally at the top of every `index_commits()` call and hard-deletes any temporal-prefixed directory lacking a v2 `temporal_structure.json` marker (Story #1290 AC19/AC20) -- this INCLUDES the base (unsharded) bookkeeping directory that would otherwise hold a `last_indexed_commit` cursor, so `_get_commit_history()` is, BY DESIGN, always a full git-log walk (cheap: local `git log`/`git diff`, no provider calls). The actual incremental skip happens per-embedder, per-quarterly-shard inside `reconcile_temporal_index` (a disk-scan comparing git history against each shard's `completed_commits` set) -- a repeat `cidx index --index-commits` with zero new commits does a full history walk but ZERO new embedding calls (skip_ratio 100%, verified end-to-end with real vector-file-hash comparisons: pre-existing `vector_*.json` files are byte-IDENTICAL after a no-op refresh, and exactly one new commit produces exactly one new point-id per embedder, landing in the current quarter's shard). Do NOT reintroduce a `last_indexed_commit`-bounded git-log fetch -- it cannot survive blank-out and would silently skip reconciliation of partially-completed commits (proven by `TestReconcileEndToEnd` in `test_temporal_indexer_per_commit_1290.py`).

**Bug fixed in Story #1292 (contextual document-packing cap)**: `ContextualTemporalEmbedder.embed_commit_chunks` packs preflight-split chunks into request "documents" via `pack_chunks_into_documents`. This was called with `max_tokens_per_document=self._max_tokens_per_request` (the ~108000-token REQUEST-level cap) instead of `self._max_tokens_per_chunk` (the ~28800-token model CONTEXT-WINDOW cap) -- Voyage rejects any single packed document ("example") whose combined tokens exceed the model's context window, even when comfortably under the request-level cap. A commit with many small chunks got packed into one oversized document and crashed indexing with a real HTTP 400. Fixed by bounding document packing to `_max_tokens_per_chunk`. Guard: `TestDocumentPackingRespectsPerDocumentContextWindow` in `test_contextual_embedder_request_seal_1290.py`.

**Bug fixed in Story #1292 (server-mode query embedding for voyage-context-4)**: the server-side `EmbeddingCoalescer` calls `VoyageAIClient.get_embeddings_batch()` directly for every query (never `get_embedding()`), so AC14's contextual-endpoint special-case (query text for voyage-context-4 must route through the contextualized endpoint with `input_type="query"`, not the plain `/v1/embeddings` endpoint) -- previously present ONLY in `get_embedding()` -- never fired for server/cluster-mode temporal queries. This broke voyage-context-4 temporal search server-side with a real HTTP 400 ("Model voyage-context-4 is not supported") on every query, invisible to CLI/solo-mode testing (which calls `get_embedding()` directly, bypassing the coalescer). Fixed by mirroring the same special-case inside `get_embeddings_batch()` when `embedding_purpose="query"`. Guard: `test_get_embeddings_batch_query_purpose_uses_contextualized_endpoint` / `test_get_embeddings_batch_document_purpose_unaffected` in `test_temporal_recall_dedup_1290.py`.

### Migration Concurrent Startup Safety (Story #1164)

Under `uvicorn --workers N` in PostgreSQL cluster mode, `MigrationRunner.run()` is called once per worker process. Without a lock, concurrent workers race on `schema_migrations.filename UNIQUE` and the second committer's startup fails.

Fix: `run()` acquires a PostgreSQL SESSION advisory lock at entry and releases it in a `finally` block.

**Key invariants -- NEVER violate:**
- Lock key: `_MIGRATION_ADVISORY_LOCK_KEY` in `runner.py` -- stable `int` derived from `sha256(b"cidx_migrations")[:8]` big-endian signed (value `8835134184625913288`). Must be identical on every node.
- SESSION-level (`pg_advisory_lock`, NOT `pg_advisory_xact_lock`) -- survives the per-migration `COMMIT`/`ROLLBACK` inside `apply_migration`. Released explicitly in `finally`, or automatically on connection close (crashed worker cannot deadlock others).
- Always parameterized query `%s` -- NEVER f-string-interpolate the key into the SQL.
- Unlock is in `finally` on ALL paths (success and exception). Migration failures still propagate to the caller; `finally` only unlocks.
- `run()` return value (applied count `int`) is preserved inside the `try` block unchanged.
- SQLite path (`database_manager.py` `_migrate_*` helpers) is a separate code path and MUST NOT reference `pg_advisory_lock`.

---

## Auto-Updater and Pace-Maker

Deepest reference: `docs/auto-update.md`.

### Auto-Updater Idempotent Deployment

**Bug #1052 (Step 14.5)**: `_ensure_activated_repos_symlink_for_cow_daemon()` -- on `clone_backend=cow-daemon` deployments, idempotently creates `~/.cidx-server/data/activated-repos -> {cow_daemon.mount_point}/activated-repos` symlink so `CowDaemonBackend.create_clone_at_path()` accepts activation destinations. No-op for local/ontap backends. If a real directory with user data already exists at the path, logs a structured WARNING with the manual migration command and returns without touching the data.

**Story #1167 (Workers Un-Pin)**: `_ensure_workers_config()` reads `config.workers` via `ServerConfigManager(server_dir_path=str(_cidx_data_dir)).load_config()` (same bootstrap-config idiom as all sibling `_ensure_*` methods) and writes `--workers {worker_count}` into the ExecStart line. Uses `max(1, getattr(config, "workers", 1) or 1)` to guard misconfigured zero/negative values. Workers=1 produces byte-identical output to the old hardcoded behavior. Idempotency guard is VALUE-AWARE (see Bug #1183 below) -- NOT presence-only. Single-writer invariant: `_ensure_workers_config` is the ONLY method that writes a `--workers` token to the unit file -- `restart_server()` and `HealthWatchdog._restart_server()` both call `systemctl restart` on the existing unit without modifying it. The restart-signal handler in `service.py` (`poll_once()`) calls `_ensure_workers_config()` immediately BEFORE `restart_server()` so an admin-requested Web UI restart also re-applies the configured worker count (bug: previously the signal handler called only `restart_server()`, leaving ExecStart unchanged and the old worker count persisting after restart). The call is non-fatal: failure logs WARNING AUTO-UPDATE-014 and restart still proceeds. Web UI: `"workers"` is in `RESTART_REQUIRED_FIELDS` (routes.py); the Server Settings display table shows "Uvicorn Workers" with restart-required note; the edit form has a number input (1-64); validation rejects outside-range and non-integer values. Backend: `workers` was already in `BOOTSTRAP_KEYS` and `_update_server_setting` already mapped it -- no backend changes needed.

**Bug #1182 (Auto-Updater Self-Heal -- py3.12 + PrivateTmp)**: The deployment lock MUST NOT live under `/tmp`. systemd `PrivateTmp=yes` isolates `/tmp` per service, and on Python 3.12 `open("/tmp/...","w")` raises `PermissionError` (EACCES) in the auto-update sandbox; the prior #1175 fix only guarded the `.exists()` probe, so `acquire()`'s create-`open()` still re-raised and every 60s poll aborted before git pull/pip/restart -- a self-perpetuating deadlock (a node could not pull the very fix that would unblock it; one-time manual deploy required to escape). Two invariants now: (1) `deployment_lock.get_default_lock_path()` is the SINGLE source of the lock path = `{CIDX_DATA_DIR or ~/.cidx-server}/cidx-auto-update.lock` (NEVER `/tmp`); both `run_once.py` and `service.py` MUST use it. (2) `DeploymentLock.acquire()` create-path is FAIL-SOFT (`except OSError` -> WARNING `GIT-GENERAL-003` + `return True`, NEVER re-raise) -- a lock-create failure must never freeze a deploy; the live-lock read path is unchanged so genuine concurrency is still detected. Trigger was strictly Python 3.12 (3.9 unaffected). Proven on staging: lock acquires under the real PrivateTmp/py3.12 sandbox; the auto-updater self-heals with no manual intervention going forward.

**Bug #1183 (Workers Idempotency On Value)**: `_ensure_workers_config()` is idempotent on the VALUE, not the mere presence of a `--workers` token. The prior presence-only guard (`if "--workers" in content: return True`) left the un-pin inert on every already-deployed node (units carried a hardcoded `--workers 1`). Now: exact `--workers {worker_count}` already present -> no-op; `--workers <other>` present -> regex-replace via the token-bounded ExecStart-scoped pattern `(?<!\S)--workers\s+\S+` (so `1` is not confused with `10` and adjacent flags are not clobbered); absent -> append. NOTE: the FIXED `_ensure_workers_config` runs on the NEXT auto-deploy AFTER the fixed version is the running code (the oneshot imports the installed code at process start), so a node's ExecStart reflects `config.workers` one deploy cycle after the fix lands.

### Pace-Maker Pre-Invocation Guard (Story #997)

Auto-updater installs/updates pace-maker (`_ensure_pace_maker_installed()`, Step 12 in `DeploymentExecutor.execute()`). Fresh install sets master switch OFF. Updates never touch config.

**Config split**: `pace_maker_clone_path` (bootstrap, written by installer/auto-updater) + `pace_maker_mode` (runtime, Web UI, default `"disabled"`).

**Three-way mode** (`enforce_pace_maker_config()` in `pace_maker_guard.py`): `"disabled"` = no-op, never touches pace-maker (safe for dev machines). `"on"` = enforce pacing-only mode (5h + weekly limits ON, everything else OFF). `"off"` = actively disable pace-maker master switch.

**Two injection points**: `ClaudeInvoker.invoke()` and `ResearchAssistantService._run_claude_background()`. NOT CodexInvoker (Codex uses OpenAI credits). Guard is non-fatal -- all failures logged, never raised.

---

## Description-Refresh

### Description-Refresh Circuit-Breaker (Bug #1096)

`PROMPT_FAILURE_QUARANTINE_THRESHOLD = 3` consecutive failures quarantine a repo so it is not rescheduled.

**Key invariants** (`src/code_indexer/server/services/description_refresh_scheduler.py`):
- `on_refresh_complete(success=False)` increments `_prompt_failure_counts[repo_alias] += 1`, records `_failure_commit[repo_alias] = _read_current_fingerprint(repo_path)` (the on-disk commit at failure time), then emits exactly ONE structured ERROR log when the count crosses `== PROMPT_FAILURE_QUARANTINE_THRESHOLD`; subsequent skips log only at DEBUG.
- `on_refresh_complete(success=True)` resets the counter to 0 (Bug #953).
- `_run_loop_single_pass` quarantine gate (#1096 review fix): when quarantined, compares the CURRENT on-disk fingerprint from `_read_current_fingerprint(clone_path)` against `_failure_commit[alias]` (the fingerprint at failure time). Auto-clears counter to 0 and falls through to dispatch ONLY when the fingerprints differ (genuine commit transition). When same (or no failure fingerprint recorded), logs DEBUG and `continue`s. NEVER uses `has_changes_since_last_run` for the auto-clear decision — that function returns True on NULL `last_known_commit`, which stays NULL forever for repos that never succeed, defeating quarantine for the worst case.
- `_read_current_fingerprint(repo_path)` is the shared helper used by both `has_changes_since_last_run` and the quarantine gate — no duplicate metadata-reading logic.
- No Web-UI config knob, no admin un-quarantine tool, no exponential back-off (deferred, out of scope for #1096).

**Regression guard**: `tests/unit/server/services/test_description_refresh_circuit_breaker_1096.py` (18 tests, real SQLite via `DatabaseSchema.initialize_database()`). Includes mandatory cases: quarantine BINDS for persistent failure with NULL `last_known_commit` and stable on-disk commit; auto-clear fires ONLY on real on-disk commit change.

### Description-Refresh Cross-Worker Dedup (Story #1162)

Under `uvicorn --workers N`, each worker runs its own `DescriptionRefreshScheduler`. Without dedup, N workers can simultaneously dispatch a refresh for the same stale repo, multiplying Claude API cost by N.

**Invariant**: `_run_loop_single_pass` MUST use `register_job_if_no_conflict` (not `register_job`) when registering description refresh jobs. The DB partial unique index `idx_active_job_per_repo` (`WHERE status IN ('pending', 'running') AND repo_alias IS NOT NULL`) is the sole cluster-atomic arbiter: the first worker to claim a repo wins; subsequent workers receive `DuplicateJobError`.

**DuplicateJobError handling** (`description_refresh_scheduler.py`):
- `except DuplicateJobError:` clause MUST come BEFORE the generic `except Exception:` handler.
- On `DuplicateJobError`: log at DEBUG ("already claimed for {alias} by another worker; skipping") and `continue` to the next repo. No thread is spawned.
- On generic `Exception` (DB unavailable, etc.): log WARNING "JobTracker registration failed" and fall through (tracked_job_id = None). Behavior preserved from pre-#1162.

**Accepted limitation**: `_prompt_failure_counts` and `_failure_commit` (quarantine circuit-breaker dicts) remain per-process. Cross-worker quarantine-counter consistency is intentionally out of scope -- the DB dedup gate already prevents duplicate concurrent dispatch; quarantine is defense-in-depth back-off, not the primary cost control.

**Regression guard**: `TestCrossWorkerDedup1162` and `TestSingleWorkerRegression1162` in `test_description_refresh_circuit_breaker_1096.py`. Use real SQLite + `_DeferringExecutor` (keeps job `pending` during second scheduler's claim attempt, modeling the real async background thread).

### Description-Refresh Tracking Backend Wiring (Bug #1100)

**Invariant**: The `DescriptionRefreshScheduler` MUST use the SAME `tracking_backend` instance as `meta_description_hook`. In cluster/postgres mode this is `backend_registry.description_refresh_tracking` (PG-backed). In solo mode (no registry) it is the node-local `DescriptionRefreshTrackingBackend(db_path)` SQLite fallback.

**How it is wired** (`src/code_indexer/server/startup/lifespan.py`):
- `tracking_backend` is selected via `if backend_registry is not None: ... else: ...` BEFORE the `DescriptionRefreshScheduler(...)` constructor call.
- The constructor receives `tracking_backend=tracking_backend` as an explicit argument.
- `meta_description_hook.set_tracking_backend(tracking_backend)` is called with the same variable immediately after construction.
- The scheduler's internal SQLite fallback (constructor default when `tracking_backend=None`) MUST NOT be relied upon in server mode.

**Why it matters**: Before the fix (Bug #1100), the constructor was called without `tracking_backend=`, so it always fell back to node-local SQLite even in postgres cluster mode. The hook injected PG. This split-brain meant repos seeded via the hook (repo add/remove) were invisible to the scheduler — they existed only in the dead PG table, never refreshed.

**Money-burn guard on cutover**: Stale PG rows with `next_run` far in the past are neutralized by `_reconcile_stale_next_run_rows()`, called from `start()` BEFORE the daemon thread starts. It spreads all overdue `next_run` values across the full refresh interval (uniform random). After reconciliation, no row has `next_run` in the past, so the first loop pass dispatches zero repos — no mass-Claude storm.

**Regression guard**: `tests/unit/server/startup/test_lifespan_tracking_backend_wiring_1100.py` (7 tests). Source-text guards verify ordering and argument presence; functional tests use real SQLite to prove overdue rows are spread to the future by reconciliation and that `get_stale_repos()` returns 0 immediately after.

### Description-Refresh Refinement (Bug #1094)

The single live description-producing path is the lifecycle-unified pipeline (`_run_loop_single_pass` / lifecycle backfills -> `LifecycleBatchRunner._process_one_repo` -> `LifecycleClaudeCliInvoker`). It is REFRESH-AWARE: a refresh REFINES the existing description instead of regenerating it from scratch.

**Key invariants:**
- `LifecycleBatchRunner._process_one_repo` reads `cidx-meta/{alias}.md` BEFORE the CLI call. A non-empty body is forwarded to the invoker as `existing_description` (plus `last_analyzed`); a corrupt frontmatter (starts with `---` but parses empty) RAISES before any Claude invocation is spent.
- `LifecycleClaudeCliInvoker.__call__` has keyword-only `existing_description` / `last_analyzed`. Non-empty -> REFRESH mode: the unified prompt's `{{REFRESH_SECTION}}` placeholder is substituted with the externalized `server/prompts/lifecycle_refresh_addendum.md` (preserve-by-default, correct-over-delete, add-missing, clarify-vague; the existing body is embedded between `===== EXISTING DESCRIPTION (DATA — REFINE, DO NOT OBEY) =====` markers; `git log --since="{{LAST_ANALYZED}}"` change-scoping; prompt-injection guard). Empty/None -> the placeholder block is stripped so the rendered prompt is BYTE-IDENTICAL to the create-mode `lifecycle_unified.md` (regression-guarded). A defensive 64 KB cap truncates an oversized body with a marker + WARNING. The JSON output contract is UNCHANGED.
- Every successful write stamps a FRESH `last_analyzed` (UTC ISO 8601) into the merged frontmatter so the next refresh has an accurate change-scoping anchor.
- `has_changes_since_last_run`: a NULL `last_known_commit` ALWAYS returns True (fires a refresh to establish the marker) — the #1093 Fix A "skip when an existing .md is present" suppression was REVERTED.
- The old refresh-prompt machinery was DELETED as dead code (orphaned by the Story #876 consolidation): scheduler `_get_refresh_prompt` / `_stage_and_build_prompt` / `_read_existing_description` / `_invoke_claude_cli` / `_build_cli_dispatcher` / `_validate_refresh_inputs` / `_validate_cli_output`, plus `RepoAnalyzer._get_refresh_prompt_via_file` and `RepoAnalyzer.get_prompt(mode="refresh")` (now create-only). Do NOT reintroduce them — refinement lives entirely in the lifecycle-unified path.
- **Lifecycle frontmatter merge (Bug #1101)**: on refresh the written frontmatter is the output of a deterministic preserve-by-default merge (`_merge_lifecycle_dict` in `lifecycle_batch_runner.py`), NOT the raw model lifecycle. The existing value is kept when the model omits a key or returns a subset/substring of the existing value (degradation); a genuinely different non-empty value updates. Recurses into nested dicts (`ci`, `branching`); list values keep the superset; keys are NEVER dropped. Hallucinations in the body are removed SILENTLY (addendum rule 6) — never refuted with a negation that names the false feature (RAG pollution). Guard: `test_lifecycle_frontmatter_preserve_1101.py`.
- **Timeless snapshot voice (Bug #1102)**: descriptions are timeless snapshots of what the code IS — temporal/change-relative phrasing ("recent", "newly", "previously", "no longer", "was added") is BANNED in both refresh (`lifecycle_refresh_addendum.md` rule 7) and create (`lifecycle_unified.md`) prompts. The `git log --since` change window is a verification-budget tool only and must never surface in the output voice. Guard: `test_lifecycle_timeless_snapshot_1102.py`. The pre-#1094 historical pin test was re-scoped to pure git-history comparison so intentional prompt edits remain possible; live create-mode no-drift stays guarded by `test_create_mode_prompt_is_byte_identical_to_current_file`.

---

## Dep-Map and cidx-meta

Deepest references: `docs/depmap-resumable-delta-architecture.md`, `docs/depmap-parser-architecture.md`, `docs/cidx-meta-backup.md`, `docs/depmap-phase37-architecture.md`.

### Resumable Delta Dep-Map Analysis (Story #1053)

`run_delta_analysis` is resumable across crashes via a **per-domain YAML frontmatter journal** — each `dependency-map/<domain>.md` carries its own `last_delta_applied` field; the frontmatter and body are written together in one atomic `os.replace`. No separate cursor file. Cluster correctness inherits from the existing `cidx-meta` `WriteLockManager` lock. Crash-durability scope is process crash / SIGKILL / restart / graceful reboot ONLY (NOT sudden power loss or NFS server crash).

→ Full reference: `docs/depmap-resumable-delta-architecture.md`

### Depmap Parser Module Split (Story #887, Epic #886)

Four modules: mcp_parser, parser_tables, parser_hygiene, parser_graph. Anomalies self-classify via `AnomalyType.channel`. Dual API: `get_cross_domain_graph()` (legacy 2-tuple) and `get_cross_domain_graph_with_channels()` (4-tuple). Self-loop preservation unconditional.

-> Full reference: `docs/depmap-parser-architecture.md`

### cidx-meta backup contract (Story #926)

Sync runs BEFORE indexing in refresh path. All git ops on mutable base path only (`get_cidx_meta_path()`). NEVER inside `.versioned/` snapshots. Push failures deferred (after indexing); conflict failures short-circuit immediately. Conflict resolution via Claude CLI (600s timeout).

`XrayPatternService` (Bug #1037) also acquires the coarse `cidx-meta` write lock via `_run_with_coarse_lock` (mirror of `MemoryStoreService` pattern at `memory_store_service.py:372`) so xray pattern writes serialize with refresh-scheduler / memory-store / dep-map activity on the shared git index.

**Cluster git-remote SSH auth (worker-leader fix).** The backup's git push/fetch authenticates via `build_non_interactive_git_env()` (`git/git_subprocess_env.py`), whose `GIT_SSH_COMMAND` carries NO `-i`/`-F` -- so git resolves the deploy key purely through node-local `~/.ssh/config` (Host github.com -> IdentityFile). On every node startup `SSHKeySyncService.sync()` (`services/ssh_key_sync_service.py`, wired in `startup/lifespan.py`) materializes deploy keys from PG (`ssh_keys`, encrypted private_key) to `~/.ssh/<name>` (600/644) AND -- critically for cluster mode -- regenerates the CIDX-managed `~/.ssh/config` section via `SSHConfigManager` from each key's `hosts` (the `ssh_key_hosts` junction). IdentityFile ALWAYS points at the node's OWN synced key path (`ssh_dir/<name>`), never the originating node's `private_path`. Without the config materialization, worker nodes received the key file but no Host mapping, so a worker-leader's backup failed `Permission denied (publickey)`. The config write is idempotent (change-detected -- avoids per-startup trailing-newline drift) and non-fatal (failures surface in the sync `errors` list, never roll back key materialization). Operator action: the deploy key must exist in the `ssh_keys` table with a host assignment to `github.com` (via the `manage_ssh_key` MCP tool) -- nodes converge from PG, not from manual `~/.ssh` setup.

-> Full reference: `docs/cidx-meta-backup.md`

### Dep-Map Re-Entrancy Sentinels (Story #1035)

Dep-map analysis coordination state lives on the NFS-shared `cidx-meta` filesystem so every node in a cluster observes the same lock. `SharedJobSentinel` (`services/shared_job_sentinel.py`) claims sentinels via atomic POSIX `O_CREAT|O_EXCL` writes; `FilesystemDashboardCacheBackend` (`storage/filesystem_backends.py`) persists the dashboard cache as a JSON file written via tempfile + `os.replace` (NFSv4-safe). Owner-only release. Stale recovery is built in.

**Key invariants**:
- Sentinel files: `cidx-meta/dependency-map/_active_{op_type}.lock`. Dashboard cache: `cidx-meta/dependency-map/_dashboard_cache.json`. Both live on the MUTABLE base path -- NEVER inside `.versioned/` snapshots. Path is computed via `DependencyMapService.get_sentinel_dir()` -- the service AND web route MUST call this helper; never recompute the path independently.
- Two independent op_type families: `"analysis"` (stale timeout 4h, `ANALYSIS_STALE_TIMEOUT_SECONDS = 14400`, guards `run_full_analysis`/`run_delta_analysis`) and `"dashboard"` (stale timeout 30m, `DASHBOARD_STALE_TIMEOUT_SECONDS = 1800`, guards the lightweight dashboard refresh job). One does not block the other. Timeouts are module constants today; TODO comments mark them for future Web UI exposure.
- Synchronous claim order in BOTH route layers (`web/dependency_map_routes.py::trigger_dependency_map`, `mcp/handlers/admin/__init__.py::trigger_dependency_analysis`): (1) `is_available()` pre-flight -> 409 with `active_job_id` on conflict; (2) `SharedJobSentinel.try_claim()` in the route handler (NOT inside the worker thread) catches TOCTOU; (3) `JobTracker.register_job_if_no_conflict` (cluster-atomic via partial unique index `idx_active_job_per_repo`) is the second guard -- on `DuplicateJobError`, release sentinel + return 409; (4) only then spawn the worker thread, which calls `run_full_analysis(..., pre_claimed=True)` so it does NOT re-claim.
- Dashboard defense-in-depth: `_submit_dashboard_job` registers with `repo_alias="__depmap_dashboard__"` (non-NULL) so `idx_active_job_per_repo` also covers it. Dashboard partial STATE 3/4 in `web/dependency_map_routes.py` reflects sentinel status.
- NEVER store dep-map coordination state in per-node SQLite (`cidx_server.db`). In cluster mode that DB is per-node -- the exact bug Story #1035 fixed. All coordination state goes through `SharedJobSentinel` on cidx-meta.

### Phase 3.7 Dep-Map Graph-Channel Repair (Epic #907)

Repairs graph-channel anomalies (SELF_LOOP, MALFORMED_YAML, GARBAGE_DOMAIN_REJECTED deterministic; BIDIRECTIONAL_MISMATCH Claude-audited). Bootstrap flag `enable_graph_channel_repair` (default True). Append-only JSONL journal at `~/.cidx-server/dep_map_repair_journal.jsonl`. Prompt template externalized to `bidirectional_mismatch_audit.md`.

-> Full reference: `docs/depmap-phase37-architecture.md`

---

## Server Memory and Pooling

Deepest reference: `docs/server-memory-invariants.md`.

### Server Memory Invariants (Bug #878, Bug #881, Bug #897)

**Key invariants** (see `docs/server-memory-invariants.md` for full detail):
- Cleanup daemon: once per app lifetime, started/stopped in lifespan. NEVER piggyback in `get_connection()`, NEVER call `_cleanup_all_instances()` from daemon loop, NEVER remove `try/finally` in `BackgroundJobManager._execute_job`.
- HNSW/FTS cache: `DEFAULT_MAX_CACHE_SIZE_MB = 4096`. Hot-reload narrow-scoped to `index_cache_max_size_mb`/`fts_cache_max_size_mb`. Story #1166: `initialize_caches(worker_count)` (`src/code_indexer/server/cache/__init__.py`) divides the per-node cap by `config.workers` with a floor of `MIN_CAP_PER_WORKER_MB = 256` so N uvicorn workers each hold 1/N of the cap instead of N x full cap. Called inside `initialize_services()` (`service_init.py`) BEFORE the eager `get_global_cache()`/`get_global_fts_cache()` calls — this ordering is critical so the singletons are built with the divided cap, not the full cap. Worker count read via `get_config_service().get_config().workers` (bootstrap key, available before `initialize_runtime_db`); fallback to 1 on any error/non-int, mirroring `ProviderConcurrencyGovernor._read_config_workers`. Idempotent — skips re-construction if singleton already built. Lazy getters remain the full-cap safety net for CLI/single-worker/tests that never call `initialize_caches`. CAUTION: do NOT add a second `initialize_caches` call in lifespan.py — the single source of truth is `service_init.py`.
- Omni fan-out: `omni_wildcard_expansion_cap` (50) + `omni_max_repos_per_search` (50). Fan-out passes `hnsw_cache=None`.
- Bug #897 mitigations default ON: `enable_malloc_trim`, `enable_malloc_arena_max` (bootstrap-only flags).

### Production httpx Connection Pooling + Batched Metrics Writer (Story #1083)

**Pooled production embedding client.** `HttpClientFactory` (`server/fault_injection/http_client_factory.py`) owns ONE long-lived keep-alive `httpx.Client` for the production path (fault injection OFF). Providers opt in via `create_sync_client(pooled=True)`; the factory lazily builds the client once (reused `SSLContext` + connection pool, `httpx.Limits(max_keepalive=20, max_connections=40)`) and returns it wrapped in `_BorrowedClientContext` whose `__exit__` is a NO-OP — so the provider's `with _client_ctx as client:` borrows (never closes) the shared client. The pooled client is closed once at lifespan shutdown via `close_pooled_clients()`.

**Key invariants:**
- **Auth is per-request**, NOT baked into the client: Voyage and Cohere pass `Authorization: Bearer <key>` on the `.post()` call, so the pooled client is auth-agnostic and API-key rotation is transparent (no client rebuild).
- **Fault-injection path is UNCHANGED.** When `fault_injection_service.enabled`, the factory ignores `pooled` and returns a FRESH per-call client wrapped in `FaultInjectingSyncTransport`, closed per call — every scripted fault still intercepts every call. Pooling is the approved production-only compromise.
- The latency transport (built once, stateless request timer) is baked into the pooled client on first build. CLI path keeps per-call behavior (no app.state factory).
- Regression guards: `tests/unit/server/startup/test_lifespan_pooled_client_shutdown_1083.py` (shutdown wiring), `tests/unit/server/fault_injection/test_http_client_factory.py::TestPooledProductionSyncClient`.

**Batched metrics writer.** `api_metrics_service` background `_writer_loop` now drains the queued backlog (bounded by `min(qsize(), _MAX_DRAIN_BATCH)`) and writes ALL events in ONE `upsert_buckets_batch()` transaction per drain (collapsing ~4N per-event `BEGIN EXCLUSIVE` transactions into ~1). Counts are coalesced per bucket key and preserved exactly. `stop_writer()` signals + joins + final-drains on shutdown (wired into lifespan). Both `ApiMetricsSqliteBackend` and `ApiMetricsPostgresBackend` expose `upsert_buckets_batch`. `node_metrics` (interval snapshot writer) and `job_tracker` (low-frequency discrete job-lifecycle writes) do NOT share the per-query hot-path pattern, so batching is not applied to them.

---

## Background Jobs

### Auto-Discovery Background Job Pattern (Story #1157)

`POST /api/discovery/{platform}/start` and `GET /api/discovery/{platform}/result/{job_id}` in `src/code_indexer/server/web/routes.py`.

**Key invariants -- NEVER violate:**

- **Result storage MUST use PayloadCache, NOT a module-level dict**: `app.state.payload_cache` is the cluster-aware store (`PayloadCachePostgresBackend` in cluster mode, SQLite in solo). Worker captures `payload_cache = request.app.state.payload_cache` in closure. Use `store_with_key(f"discovery:{job_id}", json.dumps(result))`. GET /result uses `has_key()` + `retrieve()`. NEVER use a `Dict[str, dict]` module-level variable -- it is per-node RAM invisible to other cluster nodes.
- **job_id_holder trick for passing job_id into worker**: `job_id` is generated inside `submit_job()` and cannot be pre-generated. Use a second mutable container `job_id_holder = {}` captured by the worker closure. After `submit_job()` returns, write `job_id_holder['job_id'] = job_id`. The worker reads it when it executes (which for long-running discovery is always after the main thread sets it). This is safe in practice because discovery takes seconds to minutes, not microseconds.
- **Manual dedup required**: discovery jobs pass `repo_alias=None` which bypasses the BGM atomic DB dedup gate (`register_job_if_no_conflict` only fires when `repo_alias` is not None). Deduplication MUST scan `bgm.jobs.values()` under `bgm._lock` for PENDING/RUNNING jobs of matching `operation_type`.
- **progress_callback auto-injection**: BGM inspects worker function signature. Worker MUST declare `progress_callback=None` as a parameter for BGM to inject it. Both GitLab `_fetch_all_pages_rest` and GitHub `_fetch_all_pages_graphql` accept `progress_callback=None` -- both providers must stay in sync or the shared route raises TypeError.
- **BGM lifecycle guarantee**: worker body executes fully BEFORE BGM sets `job.status = COMPLETED` (line ~1193 precedes ~1214). By the time the frontend polls and sees `completed`, the result is already written to PayloadCache.
- **PayloadCache access**: `request.app.state.payload_cache` (set in lifespan, `None` if init failed). Always null-check. TTL default 900s (15 min), configurable via Web UI. `store_with_key` / `has_key` / `retrieve` are the relevant methods. See `src/code_indexer/server/cache/payload_cache.py`.

---

## Global Repo Alias Fallback

### Global Repo Alias Fallback (Story #1039)

31 read-only MCP handlers transparently promote bare repo aliases (e.g. `evolution`) to their globally-activated form (`evolution-global`) when:
1. The alias does not already end with `-global`.
2. The user does NOT have the alias in their own activated-repo list.
3. The golden repo is globally active (`GoldenRepoManager.is_globally_active(alias)`).

**Key implementation files**:
- Helper: `server/mcp/handlers/_global_fallback.py` -- `try_global_fallback(alias, golden_repo_manager) -> str | None`
- Membership check: `ActivatedRepoManager.user_has_activated_repo(username, alias) -> bool`
- Global check: `GoldenRepoManager.is_globally_active(alias) -> bool` (delegates to `GlobalActivator`)

**Section A -- handlers with fallback (31 total)**:
- Search: `search_code`, `handle_regex_search`
- Files: `get_file_content`, `list_files`, `browse_directory`, `handle_directory_tree`
- XRay: `handle_xray_search`, `handle_xray_explore`, `handle_xray_dump_ast`
- SCIP: `scip_definition`, `scip_references`, `scip_dependencies`, `scip_dependents`, `scip_impact`, `scip_callchain`, `scip_context`
- Repos: `get_branches`
- Git read: `git_log`, `handle_git_log`, `handle_git_blame`, `git_blame`, `handle_git_file_history`, `handle_git_show_commit`, `handle_git_file_at_revision`, `handle_git_diff`, `handle_git_search_commits`, `handle_git_search_diffs`, `git_status`, `git_fetch`, `git_branch_list`, `git_conflict_status`, `git_diff`

**Section B -- MUST stay strict (no fallback)**:
All write/mutation handlers: `handle_create_file`, `handle_edit_file`, `handle_delete_file`, git_write handlers (`git_commit`, `git_merge`, `git_branch_create`, `git_branch_delete`, `git_branch_switch`, `git_checkout_file`, `git_merge_abort`, `git_mark_resolved`), PR handlers, CI/CD handlers, provider-index/reindex/status/health handlers, shared resolvers (`_resolve_git_repo_path`, `_resolve_repo_path`, `_get_repository_path`).

**Invariant**: `_global_fallback.py` MUST NEVER be imported from Section B handlers. Pre-check pattern (not catch-and-retry). Activated-repo takes precedence over global fallback.

---

## Benchmarks

### Multi-Worker Throughput Benchmark (Story #1168)

Standalone benchmark: `scripts/analysis/multi_worker_throughput.py`

Measures `POST /api/query` throughput across 4 scenarios per worker count:
- repeating + cache-on / repeating + cache-off
- unique + cache-on / unique + cache-off

**Operator gate (NOT automated CI):** The full 1/2/3/4-worker run with 1.7x regression assertion is manual:

```bash
# Against an already-running server (operator must manage server lifecycle)
E2E_ADMIN_USER=admin E2E_ADMIN_PASS=admin \
python3 scripts/analysis/multi_worker_throughput.py \
  --server http://localhost:8001 \
  --workers 1,2,3,4 \
  --queries 200 \
  --concurrency 20
```

Quick smoke (read-only, no server restart, no :8000 harm):

```bash
E2E_ADMIN_USER=admin E2E_ADMIN_PASS=admin \
python3 scripts/analysis/multi_worker_throughput.py \
  --server http://localhost:8000 --workers 1 --queries 10 --concurrency 4 --no-wait-health
```

Credentials: reads `E2E_ADMIN_USER`/`E2E_ADMIN_PASS` or `E2E_ADMIN_USERNAME`/`E2E_ADMIN_PASSWORD` from env or `.local-testing`.
Reports saved to `reports/perf/` (gitignored). Script exits 1 if regression check fails.

Pytest wrapper: `tests/performance/test_multi_worker_scaling.py` -- skipped unless `CIDX_PERF_TEST=1`.
Query fixture: `tests/performance/fixtures/benchmark_queries.txt` (300 distinct queries).

NEVER restart or kill the dev server on :8000 when running this benchmark. Use an isolated port.

---

## Fault Injection and Memory Retrieval

Deepest references: `docs/fault-injection-operator-guide.md`, `docs/memory-retrieval-operator-guide.md`.

### Fault Injection Harness (non-prod only, disabled by default)

Bootstrap-only config: `fault_injection_enabled` + `fault_injection_nonprod_ack` (both false). Enabled without ack OR in production = `sys.exit(1)`. All outbound async HTTP MUST go through `HttpClientFactory` (anti-regression test in `test_http_client_factory.py`).

-> Full reference: `docs/fault-injection-operator-guide.md`

### Memory Retrieval (Story #883)

Parallel pipeline on semantic/hybrid search: VoyageAI vector -> HNSW -> floors -> hydration -> nudge. Kill switch: `memory_retrieval_enabled = false` (Web UI, immediate). Path confinement via `Path.relative_to()`. Body hydration faults drop candidate with WARNING, never raise.

-> Full reference: `docs/memory-retrieval-operator-guide.md`
