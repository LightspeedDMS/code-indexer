# Code-Indexer (CIDX) Project Instructions

## Sandbox Rule

NEVER modify files outside this project's working directory. For running tests use `PYTHONPATH=<this-project-root>/src pytest ...`. See memory: `feedback_never_touch_other_repos.md`.

## Documentation Standards

No emoji or decorative characters in `*.md` files (README, CLAUDE, CHANGELOG, docs). Plain-text headers only.

---

## Credentials and Access

- **Credentials**: ALWAYS read from `.local-testing` (gitignored, project root) for SSH usernames/passwords, CIDX admin credentials, API keys (Langfuse, GitHub, GitLab, Anthropic, Voyage), MCPB deployment details, E2E test credentials. Declare as secret file before reading. Never guess.
- **SSH**: NEVER use `ssh` via Bash — use MCP SSH tools only. See memory: `feedback_ssh_mcp_only.md`.
- **SSH server restart**: systemd only — NEVER `kill -15 && nohup ...`. See memory: `feedback_ssh_systemd_restart.md`.
- **Admin password (dev AND staging)**: NEVER change. Breaks MCPB auto-login, E2E automation, REST/MCP testing, encrypted credentials on client machines. Recovery requires DB bypass on every client. See memory: `feedback_admin_password_sacred.md`.
- **Port config**: NEVER change cidx-server, HAProxy, or firewall ports. See memory: `feedback_port_config_locked.md`.
- **Production access**: NEVER deploy or test on production until the user explicitly approves ("commit and push to master" or "deploy manually to production server").

---

## Git Branching and Deployment

### Branch Structure

| Branch | Purpose | Direct Commits | Auto-deploy |
|--------|---------|----------------|-------------|
| `development` | Active work, version bumps | YES | No |
| `staging` | Staging env | NO (merge only) | staging server |
| `master` | Production | NO (merge only) | production |

Tags transfer automatically during merges. Before ANY work: `git branch --show-current`. OK on `development`/`feature/*`/`bugfix/*`. On `staging` or `master` — STOP, ask user.

### Workflow: dev → staging → master

```bash
# 1. On development: code, test, bump version, tag
git checkout development
# edit src/code_indexer/__init__.py, CHANGELOG.md, README.md
git tag vX.Y.Z
git push origin development --tags

# 2. Staging (auto-deploys to staging server)
git checkout staging && git merge development && git push origin staging
# E2E test on staging before promoting

# 3. Master — ONLY after staging validation AND explicit user authorization
git checkout master && git merge staging && git push origin master
```

NEVER commit directly to `master` or `staging`. All changes flow through `development`. See memory: `feedback_bump_version_before_staging.md`.

### Push-to-master Authorization (HIGHEST SEVERITY)

NEVER push to `master` without explicit user authorization in the **current conversation**.

**Counts as authorization**: "push to master", "promote to production", "deploy to production", "commit and push to master", "merge to master and push".

**Does NOT count**: completing a story or bug fix, "deploy to staging", prior-conversation authorization, assumed authorization because "the work is done".

**Default on work completion**: push to `development` (with version bump and tag), merge and push to `staging`, **STOP** and wait. When in doubt, ASK.

---

## Testing

### Three Suites — All Must Pass Before Work Is Done

| Suite | Scope | When Required | Time |
|-------|-------|---------------|------|
| `fast-automation.sh` | CLI, core logic, chunking, storage | ALL changes | ~6-7 min |
| `server-fast-automation.sh` | Server (MCP/REST/services/auth/storage) | Touching `src/code_indexer/server/` | ~10-15 min |
| `e2e-automation.sh` | 4-phase E2E: CLI standalone, CLI daemon, server in-process, CLI remote | Final regression gate — ALL completed work | ~30-60 min |

`fast-automation.sh` does NOT run server tests — it ignores `tests/unit/server/` entirely. Touching server code without running `server-fast-automation.sh` = untested changes.

`e2e-automation.sh` (Epic #700) is the final regression gate. Unit-green + E2E-red = broken system masquerading as working. No mocks — real CLI subprocess, FastAPI server, VoyageAI, golden-repo registration. Non-negotiable for epic/story completion. Pure doc/config edits may waive with explicit user approval.

### Hierarchy

1. Targeted tests (seconds): `pytest tests/unit/.../test_X*.py -v --tb=short`
2. Manual testing
3. `fast-automation.sh` (zero failures, under 10 min — MANDATORY 600000ms timeout)
4. `server-fast-automation.sh` when server code touched
5. `e2e-automation.sh` (final gate)

### fast-automation.sh Remediation

- **NEVER** "continue monitoring" after 10-min timeout — the process is dead
- Identify slow tests: `pytest tests/ --durations=20 --collect-only -q`
- Thresholds: `<5s` target, `>10s` investigate, `>30s` MUST exclude via `@pytest.mark.slow` and run `pytest tests/ -m "not slow"`
- Fix root cause, not symptoms. Failures on untouched code = regression. Flaky tests = fix or exclude.

### e2e-automation.sh Usage

```bash
./e2e-automation.sh              # All 4 phases
./e2e-automation.sh --phase 1    # CLI standalone
./e2e-automation.sh --phase 2    # CLI daemon
./e2e-automation.sh --phase 3    # Server in-process (FastAPI TestClient)
./e2e-automation.sh --phase 4    # CLI remote (live uvicorn subprocess)
```

Credentials from `.e2e-automation` (gitignored) or env: `E2E_ADMIN_USER`, `E2E_ADMIN_PASS`, `E2E_VOYAGE_API_KEY`. Exits immediately if admin credentials missing. Outcomes: SUCCESS = done; failures attributable to your change = root-cause → fix → re-run; new skips = treat as failure.

### Post-E2E Log Audit (MANDATORY)

After every E2E test, query the server log store for ERROR/WARNING entries introduced during the current development cycle. Storage backend depends on `config.json` `storage_mode`:

```bash
# Solo / standalone (SQLite)
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, level, source, message FROM logs \
   WHERE level IN ('ERROR','WARNING') ORDER BY timestamp DESC LIMIT 100;"

# Cluster (PostgreSQL) — same query, $POSTGRES_DSN from config.json postgres_dsn
psql "$POSTGRES_DSN" -c "SELECT timestamp, level, source, message FROM logs WHERE level IN ('ERROR','WARNING') ORDER BY timestamp DESC LIMIT 100;"
```

Filter pre-existing noise. For new ERRORs/WARNINGs attributable to your change: fix → redeploy → re-run E2E → re-audit. Only mark the cycle complete when zero new ERROR/WARNING entries are attributable to your changes. Declaring "done" without the log audit = incomplete validation.

### Lint and CI

```bash
./lint.sh                         # ruff, black, mypy
git push && gh run list --limit 5
gh run view <run-id> --log-failed
ruff check --fix src/ tests/
```

Zero tolerance — never leave GitHub Actions failed. Fix in the same session. See memory: `feedback_ruff_black_version_alignment.md`.

---

## Critical Architecture Invariants

### Query Is Everything

Query capability is the core product value. NEVER remove or break: query functionality, git-awareness, branch-processing optimization, relationship tracking, deduplication of indexing. If refactoring removes any of these, STOP. See memory: `project_query_is_everything.md`.

### TOTP Step-Up Elevation (Epic #922 / Story #923)

**ElevatedSessionManager** (`src/code_indexer/server/auth/elevated_session_manager.py`):
- Dual-backend (SQLite solo / PostgreSQL cluster), mirrors `MfaChallengeManager`
- Atomic touch: PostgreSQL `UPDATE...WHERE last_touched_at > cutoff RETURNING`; SQLite `BEGIN EXCLUSIVE`
- session_key = JWT `jti` (Bearer) OR `cidx_session` cookie (Web UI)
- Rolling 5-min idle timeout, 30-min absolute max age (both runtime-configurable via Web UI Config Screen)
- `INSERT ... ON CONFLICT (session_key) DO UPDATE` for atomic re-elevation (Codex M1)
- SQLite db lives at `~/.cidx-server/elevated_sessions.db` (NOT tempfile — survives restarts)

**Three error codes** (NEVER refactor to two or four):
- `totp_setup_required` (403, with `setup_url`) — admin has no TOTP secret enabled
- `elevation_required` (403) — no active elevation window for this session
- `elevation_failed` (401) — wrong code / replay / expired

**Kill switch returns HTTP 503 NOT 403** when `elevation_enforcement_enabled=false`. 403 misleadingly implies "forbidden"; 503 correctly signals "feature administratively off" (Codex M4/M12).

**Recovery code narrow elevation**: 10 codes generated at TOTP registration, stored as bcrypt hashes in `totp_recovery_codes` table (separate table, not column). Recovery code grants `scope=totp_repair` window — usable ONLY for TOTP reset/regenerate/disable. Full-scope endpoints reject. `verify_recovery_code` uses atomic CAS via single `UPDATE ... WHERE used_at IS NULL` (Codex M1) — no TOCTOU race.

**TOTP replay prevention**: `last_used_otp_timestamp` column on totp_secrets table. Atomic CAS rejects same-window replay (Codex C1). `verify_enabled_code()` rejects unactivated secrets (Codex C4).

**Rate limiting**: `POST /auth/elevate` chains through `login_rate_limiter` (per-IP+username key) — 429 when locked out, counter cleared on success (Codex H3).

**Revocation hooks**: `revoke_all_for_username()` called on logout / password change / role change to immediately invalidate active windows (Codex H2).

**Cluster deployment order**:
1. Apply `022_elevated_sessions.sql` + `023_totp_replay_prevention.sql` to all nodes (additive `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` — harmless on old code)
2. Deploy new code to all nodes (kill switch OFF by default — no behavior change)
3. Confirm version on every node via `/health`
4. Flip `elevation_enforcement_enabled=true` in Web UI Config Screen (hot-reload via 30s reload thread, no restart needed)

Files: `src/code_indexer/server/auth/elevated_session_manager.py`, `src/code_indexer/server/auth/elevation_routes.py`, `src/code_indexer/server/web/elevation_web_routes.py`, `src/code_indexer/server/auth/dependencies.py::require_elevation`.

### Maintenance Mode Localhost-Only (Epic #922 / Story #924)

Maintenance mode write endpoints (`POST /api/admin/maintenance/enter` and `POST /api/admin/maintenance/exit`) are restricted to loopback callers via the `require_localhost` FastAPI dependency in `src/code_indexer/server/auth/dependencies.py`. These endpoints are auto-updater driven (system processes, not humans) so TOTP step-up elevation does not apply — a system process cannot satisfy a TOTP prompt.

**Loopback whitelist**: `127.0.0.0/8`, `::1`, `::ffff:127.0.0.1` (dual-stack), `::ffff:127.x.x.x`.

**MCP enter/exit tools removed entirely** — the `enter_maintenance_mode` and `exit_maintenance_mode` MCP tool registrations and tool docs were deleted. `get_maintenance_status` (read endpoint) remains.

**Read endpoints unaffected**: `GET /api/admin/maintenance/status`, `GET /drain-status`, `GET /drain-timeout` continue to require admin auth only — not localhost.

**Reverse-proxy caveat**: `require_localhost` checks `request.client.host` (the immediate peer). If a proxy fronts these endpoints, the proxy must NOT forward them externally — operators must lock down the proxy's exposure.

### Golden Repo Versioned Path (IMMUTABLE)

- **Base clone** (`golden-repos/{alias}/`): mutable — where git ops and indexing happen
- **Versioned snapshot** (`.versioned/{alias}/v_{timestamp}/`): IMMUTABLE — served to queries

Workflow for any base repo change: git op on base → `cidx index` on base (no `--clear`) → new CoW snapshot → atomic swap of alias JSON `target_path` → clean up old versioned directory. Same pattern as RefreshScheduler.

Alias JSON `target_path` is authoritative for current path. SQLite `golden_repos_metadata.clone_path` goes stale after first refresh. Use `GoldenRepoManager.get_actual_repo_path(alias)`. NEVER modify/checkout/index inside `.versioned/`. See memory: `feedback_versioned_path_trap.md`.

### Database Migrations Must Be Backward Compatible

Rolling restarts mean old and new nodes run against the same schema during the upgrade window. MigrationRunner auto-runs on startup (Story #519).

- **Allowed**: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`, new tables, new nullable columns / columns with defaults
- **NEVER**: `DROP TABLE`, `DROP COLUMN`, `RENAME TABLE/COLUMN`, `ALTER COLUMN TYPE` (changing type), removing NOT NULL that old code depends on

Dead schema is harmless. Broken old code is not. (Bug #534 analysis.)

### No Environment Variables for Server Settings

Runtime settings belong in the Web UI Config Screen and persist via `get_config_service().get_config()`. Never use `os.environ["CIDX_SETTING"]` — invisible, not persisted, inconsistent.

### Config Bootstrap vs Runtime (Story #578)

`config.json` is BOOTSTRAP ONLY. Runtime settings live in the database (SQLite solo, PostgreSQL cluster). Server reads bootstrap from file, loads runtime from DB, merges on startup.

**Bootstrap keys** (stay in file, needed before DB is available): `server_dir`, `host`, `port`, `workers`, `log_level`, `storage_mode`, `postgres_dsn`, `ontap`, `cluster.node_id`.

**Runtime keys** (in DB via Web UI): all `*_config` sub-objects, `jwt_expiration_minutes`, `service_display_name`, OIDC, security, cache settings, etc.

First-boot auto-migration strips runtime keys from `config.json` and backs up to `~/.cidx-server/config-migration-backup/config.json.pre-centralization`. Existing clusters: `scripts/cluster-config-migrate.sh` (idempotent, per node).

NEVER call `ServerConfigManager().load_config()` directly in new code. Always use `get_config_service().get_config()`.

### Auto-Updater Idempotent Deployment

All systemd/env/config changes flow through the auto-updater: `git pull` → `pip install` → `DeploymentExecutor.execute()` → `systemctl restart`. Production requires zero manual intervention.

Pattern in `deployment_executor.py`: each config step is `_ensure_X_config()` — idempotent check-then-apply with `sudo systemctl daemon-reload`. Examples: `_ensure_workers_config()`, `_ensure_cidx_repo_root()`, `_ensure_data_dir_env_var()` (Bug #879).

**CIDX_DATA_DIR IPC alignment (Bug #879)**: When `cidx-server` and `cidx-auto-update` run as different OS users (e.g. `code-indexer` vs `root`), module-level IPC path constants (`RESTART_SIGNAL_PATH`, `PENDING_REDEPLOY_MARKER`, `AUTO_UPDATE_STATUS_FILE`) honor `CIDX_DATA_DIR` so both processes resolve to the same files. Patched idempotently at Step 6.5 of `execute()` (error code `DEPLOY-GENERAL-058`). Same-user deployments are a no-op.

### Server Memory Invariants (Bug #878, Bug #881, Bug #897)

**FD/connection hygiene (Bug #878)**: A single `DatabaseConnectionManager-cleanup-daemon` thread runs for the app lifetime, sweeping stale SQLite connections across all registered singletons every 60s. Started/stopped in lifespan (error codes `APP-GENERAL-034`/`035`). Idempotent. Identity-guarded clear.

NEVER: re-introduce the piggyback cleanup trigger in `get_connection()` (lost races to thread churn in production, RC-3); call `_cleanup_all_instances()` from the daemon loop (double-throttle); remove the `try/finally` that calls `_close_thread_connections_on_all_managers(job_id)` in `BackgroundJobManager._execute_job` (Fix A.3 closes at source); remove the close-on-clobber guard in `get_connection()` (Linux TID recycling silently leaks FDs otherwise).

**HNSW/FTS cache cap (Bug #878)**: Singletons always carry a finite `max_cache_size_mb`. When config has `None`, `DEFAULT_MAX_CACHE_SIZE_MB = 4096` is overlaid at `get_global_cache()` / `get_global_fts_cache()` init. Dataclass defaults stay `None` (sentinel distinguishes explicit operator value from unset). Hot-reload via `ConfigService._hot_reload_cache_size_cap()` is narrow-scoped to `index_cache_max_size_mb` and `fts_cache_max_size_mb` only — `TestHotReloadScopeIsolation` asserts the boundary.

**Bug #881 omni fan-out mitigations**: two caps enforced in sequence — (1) `omni_wildcard_expansion_cap` (default 50, Web UI): per-pattern wildcard expansion cap, enforced inside `_expand_wildcard_patterns`, error code `wildcard_cap_exceeded`; (2) `omni_max_repos_per_search` (default 50, Web UI, Bug #894): total alias fan-out cap after wildcard expansion + literal union, enforced by `_enforce_repo_count_cap` in `_omni_search_code` and `_omni_regex_search`, error code `repo_count_cap_exceeded`. Both return `Union[List[str], CapBreach]` and callers handle via `cap_breach_response` / `cap_breach_http_exception`. Fan-out searches pass `hnsw_cache=None` to bypass the global HNSW cache; `sys.getsizeof(id_mapping)` added to `index_size_bytes` so label→id dict is no longer invisible to the size cap.

**Bug #897 glibc arena fragmentation mitigations** (both default ON since v9.23.3, bootstrap-only flags in `config.json`): After a bulk lifecycle backfill that cycles 500+ HNSW indexes through the LRU cache, process RSS can pin ~23 GB because glibc's multi-arena brk segments hold small `label_lookup_` / `linkLists_` allocations. Two mitigations behind feature flags (operators can disable either by setting to false in `config.json`):

- `enable_malloc_trim: bool = True` -- calls `glibc malloc_trim(0)` at the end of each `_cleanup_expired_entries()` cycle (implemented in `_maybe_malloc_trim()` in `hnsw_index_cache.py`). Linux + glibc only; silently no-ops on musl/macOS. Default ON since v9.23.3.
- `enable_malloc_arena_max: bool = True` -- idempotently injects `Environment=MALLOC_ARENA_MAX=2` into the cidx-server systemd unit file on each auto-updater run (`_ensure_malloc_arena_max()` in `deployment_executor.py`, step 6.6, error code `DEPLOY-GENERAL-143`). Reverting the flag removes the line on the next auto-updater cycle. Default ON since v9.23.3.

**Codex CLI auto-install (Story #845)**: `_ensure_codex_cli_installed()` in `deployment_executor.py`, step 6.7, error code `DEPLOY-GENERAL-144`. Runs `npm install -g @openai/codex` on every auto-updater cycle (idempotent — both first install and updates). If npm is not on PATH, logs WARNING and returns True (optional-feature semantics — CIDX starts fine without Codex). After install, probes `codex --version` and logs result at INFO. Non-fatal: a failed install logs WARNING and returns False but does not abort the auto-updater. Tests: `tests/unit/server/auto_update/test_ensure_codex_cli_installed_845.py`.

**Codex auth modes (Story #846)**: Two distinct paths in `src/code_indexer/server/startup/codex_cli_startup.py`. `api_key` mode: delegates auth.json population to `codex login --with-api-key` (key read from stdin) via `_login_codex_with_api_key()` — codex owns the auth.json schema for this mode, preventing the OAuth-schema mismatch that caused WebSocket 401s. Also sets `OPENAI_API_KEY` env var as belt-and-suspenders fallback. `subscription` mode: uses the OAuth lease-loop path via `CodexCredentialsFileManager` + `CodexLeaseLoop` (unchanged). `none` mode: no-op. Tests: `tests/unit/server/startup/test_codex_cli_startup_846.py`, `tests/unit/server/startup/test_codex_login_with_api_key.py`.

Both flags are bootstrap-only (read from `config.json` before DB is available) and default True since v9.23.3 so fresh installs and existing installs that don't pin the flags automatically get the protection. Tests: `tests/unit/server/cache/test_malloc_trim_flag_bug_897.py`, `tests/unit/server/auto_update/test_malloc_arena_max_bug_897.py`.

Files: `src/code_indexer/server/storage/database_manager.py`, `src/code_indexer/server/startup/lifespan.py`, `src/code_indexer/server/repositories/background_jobs.py`, `src/code_indexer/server/cache/__init__.py`, `src/code_indexer/server/services/config_service.py`, `src/code_indexer/server/mcp/handlers/_utils.py`, `src/code_indexer/server/cache/hnsw_index_cache.py`. Tests: `tests/unit/server/mcp/test_wildcard_cap.py`, `test_cap_breach_helper.py`, `test_repo_count_cap.py`, `test_cache_bypass_on_fanout.py`, `tests/unit/server/cache/test_id_mapping_size_bytes.py`.

Operational check:
```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, message FROM logs WHERE message LIKE '%cleanup daemon%' ORDER BY timestamp DESC LIMIT 5;"
# Expect one 'started' per process + periodic 'Cleaned up N stale SQLite connections' under churn.
```

### Depmap Parser Module Split and Anomaly Channels (Story #887, Epic #886)

The depmap parser was split from a single 1042-line `dep_map_mcp_parser.py` into four cohesive modules under the MESSI rule 6 soft cap (500 lines). Each module has a single responsibility:

| Module | Responsibility | Lines |
|--------|----------------|-------|
| `dep_map_mcp_parser.py` | Orchestration + public API (2-tuple legacy + 4-tuple with-channels) | ~440 |
| `dep_map_parser_tables.py` | Markdown table extraction | ~354 |
| `dep_map_parser_hygiene.py` | Identifier normalization, `AnomalyEntry`/`AnomalyAggregate`/`AnomalyType` dataclasses, dedup + aggregation helpers | ~279 |
| `dep_map_parser_graph.py` | Graph edge aggregation, filter hooks (reserved for Story #889), channel split | ~365 |

**Public API dual-surface** (both are stable contracts):
- `get_cross_domain_graph(output_dir) -> Tuple[List[Dict], List[Dict[str, str]]]` — legacy 2-tuple, anomalies as `{file, error}` dicts (backward-compat).
- `get_cross_domain_graph_with_channels(output_dir) -> Tuple[List[Dict], List[Union[AnomalyEntry, AnomalyAggregate]], List[Union[AnomalyEntry, AnomalyAggregate]], List[Union[AnomalyEntry, AnomalyAggregate]]]` — rich 4-tuple `(edges, all, parser_anomalies, data_anomalies)` for callers that need channel separation.

**Anomaly channel structure** (response envelope for all 5 `depmap_*` tools):
- `parser_anomalies[]` — structural file defects: malformed YAML, truncated table, unreadable bytes, path-traversal rejected, missing required frontmatter keys, section-present-but-empty.
- `data_anomalies[]` — source-graph drift: bidirectional mismatch, dual-source inconsistency (JSON↔markdown), garbage-domain rejected, self-loop, edge with no derivable types, case normalization applied.
- `anomalies[]` — legacy concatenation of both, preserved for ONE release after Epic #886 completes (to be dropped in vN+1 per epic BREAKING CHANGES).

**AnomalyType self-classifying enum**: each variant carries a bound `channel: Literal["parser", "data"]` attribute. Routing is `AnomalyType.channel` lookup — no manual classification logic. Aggregates route identically (the aggregate's `.type.channel` determines the channel).

**Frozenset-keyed bidirectional dedup**: `_check_bidirectional_consistency` aggregates by `frozenset({normalize(source), normalize(target)})` so one anomaly emits per unordered edge pair. Prevents the pre-Story-#887 pattern of ~170 anomalies for ~150 edges. Both sides of the frozenset are normalized (strip_backticks + lowercase) to prevent case/backtick drift from producing false mismatches.

**Invariants (MESSI rule 15, stripped under `python -O`)**:
- `strip_backticks()` postcondition: `assert not s.startswith("\`") and not s.endswith("\`")` — all wrapper backticks stripped via `while` loops (not just one pair).
- Self-loop preservation unconditional: `finalize_graph_edges()` excludes self-loops from the empty-types drop filter (self-loops with empty types still emit the `GARBAGE_DOMAIN_REJECTED` anomaly AND are preserved as edges).
- Late-anomaly routing: `finalize_graph_edges()` anomalies flow through `aggregate_anomalies()` + channel split before response assembly — no silent drops (MESSI rule 13).

**Handler serialization**: `src/code_indexer/server/mcp/handlers/depmap.py::_anomaly_to_dict()` handles both `AnomalyEntry` and `AnomalyAggregate` — the same helper is reused at every response assembly site. Aggregates serialize as `{"file": "<aggregated>", "error": "N occurrences: <type>"}`.

Files: `src/code_indexer/server/services/dep_map_{mcp_parser,parser_tables,parser_hygiene,parser_graph}.py`, `src/code_indexer/server/mcp/handlers/depmap.py`. Tests: `tests/unit/server/services/test_dep_map_887_*.py` (70 tests across 8 ACs + 4 remediation blocker files).

---

## Operational Modes

Two local modes plus a separate server deployment:

| Mode | Storage | Use Case |
|------|---------|----------|
| **CLI** | FilesystemVectorStore (`.code-indexer/index/`) | Single dev, local |
| **Daemon** | Same + in-memory cache, Unix socket at `.code-indexer/daemon.sock` | ~5ms cached vs ~1s disk, watch mode |

Both are container-free, instant setup. Vectors stored as JSON in `.code-indexer/index/{collection}/`. Quantization: model dims (1024/1536) → 64-dim → 2-bit → filesystem path. Git-aware: blob hashes (clean) / text content (dirty). Thread-safe atomic writes. VoyageAI dims: 1024 (voyage-3), 1536 (voyage-3-large). <1s query, <20ms incremental HNSW updates.

**Server mode**: separate deployment. Cluster (`storage_mode: postgres`) shares PostgreSQL across nodes. See `docs/server-deployment.md`, `docs/cluster-architecture.md`, `docs/architecture.md`.

---

## CIDX Quick Reference

```bash
cidx init                              # Create .code-indexer/
cidx index                             # Index codebase
cidx query "authentication" --quiet    # Semantic search
cidx query "def.*" --fts --regex       # FTS/regex search

cidx config --daemon && cidx start     # Daemon mode
cidx watch / watch-stop / stop         # Daemon controls
```

**Flags** (always `--quiet`): `--limit N` (start 5-10), `--language python`, `--path-filter */tests/*`, `--min-score 0.8`, `--accuracy high`.

**Search decision**: Concepts/questions → CIDX. Exact strings (variable names, config literals) → grep/find.

---

## Performance Rules

- **NEVER** add `time.sleep()` to production. See memory: `feedback_no_sleep_in_production.md`.
- **Progress reporting is delicate** — ask confirmation before ANY changes. See memory: `feedback_progress_reporting_delicate.md`.
- **FTS lazy import**: NEVER import Tantivy/FTS at module level in CLI startup files. Use `TYPE_CHECKING` guards and import inside methods. Keeps `cidx --help` at ~1.3s vs 2-3s. Verify:
  ```bash
  python3 -c "import sys; from src.code_indexer.cli import cli; print('tantivy' in sys.modules)"  # expect False
  ```
- **Smart indexer**: Always consider `--reconcile` (non git-aware) — maintain feature parity.
- **Tmp files**: `~/.tmp`, never `/tmp`.
- **Container-free**: no ports, no containers.
- **Import budget**: current startup is ~329ms (voyageai eliminated from startup path, CLI lazy-loaded from 736ms).

---

## Embedding Provider (VoyageAI)

Only provider in v8.0+.

- **Tokenizer**: `embedded_voyage_tokenizer.py`, NOT the voyageai library. Critical for the 120,000 tokens/batch API limit. Lazy imports, caches per model (~0.03ms), 100% identical to `voyageai.Client.count_tokens()`. DO NOT remove/replace without extensive testing.
- **Batch**: 120k token limit enforced, automatic batching and transparent splitting.

| Model | Dims | Notes |
|-------|------|-------|
| voyage-3 (default) | 1024 | Best balance |
| voyage-3-large | 1536 | Highest quality |

---

## Server Development

### Local server

```bash
PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app --host <bind-address> --port 8000
curl -s http://localhost:8000/docs | head -5
pkill -f "uvicorn code_indexer.server.app"
```

Common errors: `No module named 'code_indexer'` → missing `PYTHONPATH=./src`. `No module named 'fastapi'` → use `python3 -m uvicorn`. Exits immediately → port 8000 already in use.

### E2E REST/MCP

```bash
# Auth — JSON body, NOT form-urlencoded; endpoint is /auth/login, NOT /admin/login
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r '.access_token')

# Add golden repo — returns HTTP 202 with job_id; poll /api/jobs/{job_id}
curl -s -X POST http://localhost:8000/api/admin/golden-repos \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"repo_url":"git@github.com:org/repo.git","alias":"my-repo","description":"..."}'

# Query — field is "query_text" (not "query"); global repos suffix is "-global"
curl -X POST http://localhost:8000/mcp -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_code","arguments":{"query_text":"...","repository_alias":"repo-global","limit":5}}}'
```

Token expiry: 10 minutes. Timing display: CLI only, not MCP/REST.

### Claude CLI Integration (Two Subsystems)

- **ClaudeCliManager** (`src/code_indexer/server/services/claude_cli_manager.py`) — queue-based thread pool with configurable worker count (default 2). Used by golden repo registration (generates description) and catch-up processing. Concurrency capped by "Max Concurrent Claude CLI" Web UI setting. Methods: `submit_work(job_id, repo_path, prompt)`, `get_job_status(job_id)`.
- **ResearchAssistantService** (`src/code_indexer/server/services/research_assistant_service.py`) — direct `threading.Thread(daemon=True)` per request, no queue, messages persisted to SQLite immediately. In-memory `_jobs` dict for active tracking with DB fallback. Used by admin Web UI "Research Assistant" tab.

Rationale: batch processing needs rate limiting for API cost; interactive UX expects immediate response, persistence through nav-away/back.

**MCP self-registration — SINGLE source of truth at `invoke_claude_cli`** (Story #885 A10). `MCPSelfRegistrationService.ensure_registered()` lives at the top of `src/code_indexer/global_repos/repo_analyzer.py::invoke_claude_cli` — every subprocess invocation of the Claude CLI that routes through this function automatically inherits `cidx-local` MCP availability. NEVER add parallel `ensure_registered()` calls in other adapters (the lifecycle path previously silently lost MCP access by bypassing ClaudeCliManager — A10 fixed this by centralizing at the subprocess boundary). Preconditions belong at the boundary they guard.

No fallbacks — research and propose solutions. JSON errors: use `_validate_and_debug_prompt()`, check non-ASCII characters, long lines, quotes.

**Codex/Claude divergence (v9.23.10)**:

- `cidx-local` MCP is registered automatically at server startup for BOTH CLI paths using the SAME persistent `client_id:client_secret` credentials issued by `MCPCredentialManager`: Claude via `MCPSelfRegistrationService` (HTTP + Basic auth header passed directly), and Codex via `_ensure_codex_mcp_http_registered` in `codex_cli_startup.py` (TOML config — `env_http_headers = { Authorization = "CIDX_MCP_AUTH_HEADER" }` — codex reads `CIDX_MCP_AUTH_HEADER` env var and injects it verbatim as the Authorization header). `CodexInvoker` retrieves the header value via `build_codex_mcp_auth_header_provider()` closure. No JWT, no TTL. See `src/code_indexer/server/startup/codex_mcp_registration.py` and `src/code_indexer/server/services/codex_mcp_auth_header_provider.py`.
- Hook parity is NOT achieved — codex 0.125 has no `PostToolUse` hook equivalent (verified via `codex --help` and `codex exec --help`; reference: github.com/openai/codex/issues/16732). Citation and audit enforcement at the hook layer remain Claude-only. This is accepted as permanent degradation. See CHANGELOG v9.23.9.

---

## Background Jobs (MANDATORY Checklist)

Any new background job or long-running operation MUST:

1. **Integrate with job tracking**: Register with `BackgroundJobManager` (`src/code_indexer/server/repositories/background_jobs.py`) and, when applicable, `JobTracker` (`src/code_indexer/server/services/job_tracker.py`, Story #311/Epic #261). Report progress updates, completion, and errors so the job appears in the dashboard and admin UI.
2. **Confirm frontend reporting with the user**: Before implementing, ask how progress/status should appear in the Web UI — progress bar vs status text, polling interval, tab/page, error display format. Do NOT assume a UI pattern.

Files: `BackgroundJobsSqliteBackend` (`src/code_indexer/server/storage/sqlite_backends.py`), dashboard UI (`src/code_indexer/server/web/templates/partials/dashboard_recent_jobs.html`). Skipping either step = incomplete implementation.

---

## MCP Tool Documentation

Tool docs externalized to `src/code_indexer/server/mcp/tool_docs/` by category: `admin/`, `cicd/`, `files/`, `git/`, `guides/`, `repos/`, `scip/`, `search/`, `ssh/`.

Format: YAML frontmatter (`name`, `category`, `required_permission`, `tl_dr`, optional `quick_reference: true`) + markdown body.

**Adding a tool**: (1) Add entry to `TOOL_REGISTRY` in `src/code_indexer/server/mcp/tools.py`; (2) `python3 tools/convert_tool_docs.py`; (3) `python3 tools/verify_tool_docs.py` (CI gate).

Runtime loader with caching: `tool_doc_loader.py`. Tests: `tests/unit/tools/test_convert_tool_docs.py`, `test_verify_tool_docs.py`, `tests/unit/server/mcp/test_tool_doc_*.py`.

---

## SCIP Index File Lifecycle

`cidx scip generate` produces `index.scip.db` (SQLite) from intermediate `index.scip` (protobuf). **The original `.scip` file is DELETED after conversion.** Only `.scip.db` remains. Never search for `.scip` files.

---

## Version Bump

Source of truth: `src/code_indexer/__init__.py` `__version__` (line 9). Also update `README.md` version badge (line 5), `CHANGELOG.md` (new entry at top), `docs/architecture.md` server response example, `docs/query-guide.md` version refs. Check for stale refs in `docs/mcpb/setup.md` and `docs/server-deployment.md`.

DO NOT bump on CIDX version change: `mcpb/__init__.py` (separate version 1.0.0), `server/app.py` OpenAPI spec, `test-fixtures/` test data.

Verify: `grep -r "OLD_VERSION" --include="*.md" --include="*.py" .`

---

## Python Compatibility

Always `python3 -m pip install --break-system-packages` — never bare `pip`.

---

## Fault Injection Harness (non-prod only, disabled by default)

Full guide: `docs/fault-injection-operator-guide.md`.

Bootstrap-only config (in `config.json`, never DB): `fault_injection_enabled` (default false), `fault_injection_nonprod_ack` (default false). 4 startup scenarios; enabled + acked + non-prod = harness live. Enabled without ack OR in production = CRITICAL log + `sys.exit(1)`.

**All outbound async HTTP to embedding/reranking providers MUST go through `HttpClientFactory`**. Direct `httpx.AsyncClient()` construction outside the factory is caught by the Scenario 18 anti-regression test in `test_http_client_factory.py` (`_EXCLUDED_PATHS` lists auth/infra exemptions).

Files: `src/code_indexer/server/fault_injection/`, tests at `tests/unit/server/fault_injection/`.

---

## Memory Retrieval (Story #883, semantic-triggered)

Full guide: `docs/memory-retrieval-operator-guide.md`.

When `search_code` runs with `search_mode` = `semantic` or `hybrid`, a parallel pipeline retrieves stored memories and injects them into the `relevant_memories` response field. Pipeline stages: VoyageAI query vector → HNSW candidates → Voyage floor → assembly → ordering → Cohere floor (if reranker) → body hydration → empty-state nudge.

**Kill switch**: `memory_retrieval_enabled = false` in Web UI Config Screen (effective immediately, no restart; no VoyageAI call made, field absent).

**Path confinement**: Memory IDs validated via `^[A-Za-z0-9_-]+$` regex and resolved with `Path.relative_to()` (not `str.startswith()`) to prevent traversal. Invalid IDs / unconfined paths skipped with WARNING log.

**Body hydration fault (AC15)**: On file read error for any candidate, log WARNING and drop that candidate; do NOT raise. Prevents a single corrupt memory file from blocking all results.

Files: `src/code_indexer/server/mcp/memory_retrieval_pipeline.py`, `src/code_indexer/server/mcp/handlers/search.py`, `src/code_indexer/server/mcp/prompts/memory_empty_nudge.md` (editable by operators), `tests/unit/server/mcp/test_search_memory_retrieval.py`.

---

## Further Reading

- Architecture: `docs/architecture.md`
- Server deployment: `docs/server-deployment.md`
- Cluster architecture: `docs/cluster-architecture.md`
- Fault injection: `docs/fault-injection-operator-guide.md`
- Memory retrieval: `docs/memory-retrieval-operator-guide.md`
