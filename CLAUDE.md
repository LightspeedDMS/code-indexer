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

### Security-Sensitive Commit Discipline (Item #18, Story #929)

Security-sensitive changes MUST be isolated in their own commit — never bundled with unrelated features, refactors, or bug fixes. This applies to:

- **Permission-model edits**: any change to `_bash_deny_rules`, `_allow_rules`, `_build_permission_settings`, or any analogous permission gate in any service.
- **Prompt-template edits for capability-granted agents**: any change to `research_assistant_prompt.md` or any prompt template that defines the operational authority of a Claude subprocess (what it is allowed or forbidden to do).
- **Auth-boundary changes**: any change to auth middleware, TOTP/MFA gates, elevation logic, role checks, or session validation.

**Why**: Bundling security changes with unrelated commits makes code review harder, increases the risk of reviewers missing the security impact, and complicates post-incident forensics. A standalone commit with a clear security-focused message makes the change auditable and revertable without side effects.

**Enforcement**: Raise this rule in code review whenever a PR or commit mixes permission/prompt/auth changes with other work. The reviewer must request the security portion be split into its own commit before approving.

---

## Testing

### Three Suites — All Must Pass Before Work Is Done

| Suite | Scope | When Required | Time |
|-------|-------|---------------|------|
| `fast-automation.sh` | CLI, core logic, chunking, storage | ALL changes | ~6-7 min |
| `server-fast-automation.sh` | Server (MCP/REST/services/auth/storage) | Touching `src/code_indexer/server/` | ~10-15 min |
| `e2e-automation.sh` | 5-phase E2E: CLI standalone, CLI daemon, server in-process, CLI remote, fault-injection resiliency | Final regression gate — ALL completed work | ~45-90 min |

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
./e2e-automation.sh              # All 5 phases
./e2e-automation.sh --phase 1    # CLI standalone
./e2e-automation.sh --phase 2    # CLI daemon
./e2e-automation.sh --phase 3    # Server in-process (FastAPI TestClient)
./e2e-automation.sh --phase 4    # CLI remote (live uvicorn subprocess)
./e2e-automation.sh --phase 5    # Fault-injection resiliency (live fault server, dual provider)
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

**Bug #900 prevention** (2026-04-26): 251 mypy errors had accumulated across 79 files on `development` because story DoDs were being interpreted as "no NEW lint errors introduced by this changeset" rather than "`lint.sh` exits 0". Partial cleanups don't compound — they hide. Going forward, every story DoD must require `./lint.sh` to exit 0 BEFORE merging the story branch back to `development`. If a story can't reach a clean lint, it must either fix the upstream debt or be blocked. CI gate must be `./lint.sh` (full ruff check + ruff format check + mypy across `src/` and `tests/`), not just `mypy src/`.

---

## Critical Architecture Invariants

### Query Is Everything

Query capability is the core product value. NEVER remove or break: query functionality, git-awareness, branch-processing optimization, relationship tracking, deduplication of indexing. If refactoring removes any of these, STOP. See memory: `project_query_is_everything.md`.

### X-Ray Module Lazy-Load Invariant (Epic #968 / Story #969)

`src/code_indexer/xray/` is the AST-aware code search engine wrapping tree-sitter. The lazy-load discipline mirrors the existing FTS/Tantivy pattern: `tree_sitter` and `tree_sitter_languages` are imported ONLY inside `AstSearchEngine.__init__()`. Importing `code_indexer.xray.ast_engine` at module level does NOT trigger the heavy tree-sitter import. CLI startup is unaffected — `cidx --help` measured at ~0.57s with X-Ray code present (budget 2.0s).

**CI gate**: `tests/unit/xray/test_lazy_load.py` runs a SUBPROCESS test asserting `tree_sitter` and `tree_sitter_languages` are absent from `sys.modules` after `from code_indexer.cli import cli`. The subprocess form is required because pytest's session may pre-load tree-sitter via other tests, polluting the in-process check. This test is BLOCKING — if it fails, X-Ray has regressed CLI startup.

**Architecture invariants**:
- All raw `tree_sitter.Node` objects are wrapped in `XRayNode` before exposure to evaluator code; raw nodes NEVER reach user-supplied evaluator scripts (`xray_node.py` uses `__slots__ = ("_node",)` and normal `self._node = node` assignment — DO NOT reintroduce `object.__setattr__` workaround, which breaks mypy tracking).
- `AstSearchEngine.supported_languages` and `extension_map` are INSTANCE-level dynamic registries (`self._supported_languages`, `self._extension_map`) — they conditionally include `terraform` / `.tf` when `tree_sitter_hcl` is importable at engine construction time.
- 10 mandatory languages: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css. Terraform is the optional 11th when HCL grammar is present.

**Dependency**: `tree-sitter>=0.21,<0.22` and `tree-sitter-languages==1.10.2` (pinned for grammar version stability) — both are CORE dependencies since v10.2.1 (previously [xray] optional extras, but the auto-updater installs the base package only, so X-Ray was broken on staging — promoted to core in v10.2.1).

**Files**: `src/code_indexer/xray/{ast_engine,xray_node,languages}.py`. Tests: `tests/unit/xray/` (>=95% coverage per module).

### X-Ray Sandbox Security Boundary (Epic #968 / Story #970)

`PythonEvaluatorSandbox` securely executes caller-supplied Python evaluator code against AST nodes. Three defense layers (AST whitelist, stripped builtins, multiprocessing isolation) plus dunder-access block at validation time close the confirmed `__class__.__init__.__globals__` exploit.

**Essential invariants**:
- Three defense layers: AST whitelist validation (Layer 1, rejects before subprocess spawn) + stripped exec() environment (Layer 2, removes `STRIPPED_BUILTINS` and limits to 17 `SAFE_BUILTIN_NAMES`) + `multiprocessing.Process` isolation (Layer 3).
- 24-name `DUNDER_ATTR_BLOCKLIST` covers `__class__`, `__globals__`, `__builtins__`, `__import__`, `__dict__`, `__subclasses__`, etc. Both `ast.Attribute` and string-`Constant` subscript paths are blocked at validation time.
- Timeout policy: `HARD_TIMEOUT_SECONDS=5.0` (SIGTERM), `SIGKILL_GRACE_SECONDS=1.0` (SIGKILL if still alive). Pipe data is read BEFORE `is_alive()` check (waitpid races).
- `signal.alarm` is NOT used — FastAPI request handlers run in worker threads; `signal.alarm()` only works in the main thread.

**Files**: `src/code_indexer/xray/sandbox.py`. Tests: `tests/unit/xray/test_sandbox*.py` (8 files, 112+ tests).

→ Full reference: `docs/xray-sandbox.md`

### X-Ray Search Engine and MCP Tool (Epic #968 / Story #972)

`XRaySearchEngine` is a two-phase orchestrator (regex driver Phase 1 → sandboxed Python evaluator Phase 2 over `XRayNode` ASTs). The MCP handler `handle_xray_search` is a thin shim that validates params, runs sandbox pre-flight validation, then submits an async background job.

**Essential invariants**:
- Two-phase pipeline: Phase 1 regex walk produces candidate `Path` list (honors include/exclude fnmatch patterns); Phase 2 parses + sandbox-evaluates each candidate. Failure modes (UnsupportedLanguage, EvaluatorTimeout, EvaluatorCrash, NonBoolReturn) accumulate in `evaluation_errors` and never fail the job.
- Async job pattern: handler returns `{"job_id": "<uuid>"}` immediately; clients poll `GET /api/jobs/{job_id}`. Pre-flight runs `sandbox.validate(evaluator_code)` to reject malformed evaluator code BEFORE submitting the job.
- Tree-sitter is a CORE dependency since v10.2.1 (no longer optional); `XRayExtrasNotInstalled` was deleted along with the `[xray]` extras.
- `max_files` cap surfaces `partial=True` / `max_files_reached=True` in the result.

**Files**: `src/code_indexer/xray/search_engine.py`, `src/code_indexer/server/mcp/handlers/xray.py`. Tests: `tests/unit/xray/test_search_engine.py` (20 tests, 100% coverage), `tests/unit/server/mcp/test_xray_search_handler.py` (15 tests).

→ Full reference: `docs/xray-architecture.md`

### TOTP Step-Up Elevation (Epic #922 / Story #923)

`ElevatedSessionManager` implements server-side step-up admin elevation with rolling 5-min idle timeout and 30-min absolute max age. Dual-backend (SQLite solo / PostgreSQL cluster), session_key keyed off JWT `jti` (Bearer) or `cidx_session` cookie (Web UI), atomic re-elevation via `INSERT ... ON CONFLICT DO UPDATE`.

**Essential invariants**:
- Three error codes — NEVER refactor to two or four: `totp_setup_required` (403, with `setup_url`), `elevation_required` (403), `elevation_failed` (401).
- Kill switch returns HTTP **503 NOT 403** when `elevation_enforcement_enabled=false` — 403 misleadingly implies "forbidden"; 503 correctly signals "feature administratively off".
- Recovery codes (10, bcrypt-hashed in separate `totp_recovery_codes` table) grant a narrow `scope=totp_repair` window — usable ONLY for TOTP reset/regenerate/disable, never full-scope endpoints. Atomic CAS via `UPDATE ... WHERE used_at IS NULL` prevents TOCTOU.
- TOTP replay prevention via atomic CAS on `last_used_otp_timestamp`. Rate limiting via `login_rate_limiter` (429 on lockout). Revocation hooks invalidate windows on logout/password change/role change.

Files: `src/code_indexer/server/auth/elevated_session_manager.py`, `src/code_indexer/server/auth/elevation_routes.py`, `src/code_indexer/server/web/elevation_web_routes.py`, `src/code_indexer/server/auth/dependencies.py::require_elevation`.

→ Full reference: `docs/totp-elevation.md`

### CLI Elevation Retry (Story #980)

CLI admin commands in remote mode auto-elevate when the server returns 403 `elevation_required`. The retry helper `with_elevation_retry` wraps API calls: on `ElevationRequiredError` → prompt user for TOTP → call `POST /auth/elevate` → single retry. On `totp_setup_required` or `elevation_failed`: clear error + `sys.exit(1)` (no retry loop).

**Essential invariants**:
- `with_elevation_retry` wraps ALL `cidx admin users` commands (create, list, show, update, delete, change-password) AND all `cidx admin groups` commands.
- `AdminAPIClient` and `GroupAPIClient` both raise `ElevationRequiredError` for 403 responses with `{"detail": {"error": "elevation_required"}}` or `{"detail": {"error": "totp_setup_required"}}` — always unwrap via `body.get("detail", {})` (FastAPI wraps `HTTPException(detail={...})`).

Files: `src/code_indexer/api_clients/elevation.py`, `src/code_indexer/api_clients/admin_client.py`, `src/code_indexer/api_clients/group_client.py`, `src/code_indexer/cli.py` (admin users + groups sections).

→ Full reference: `docs/totp-elevation.md` (CLI Elevation Retry section)

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

The server runs a `DatabaseConnectionManager-cleanup-daemon` thread for the app lifetime sweeping stale SQLite connections across all registered singletons every 60s. HNSW/FTS singletons always carry a finite `max_cache_size_mb` (default 4096). Bug #881 omni fan-out caps and Bug #897 glibc arena fragmentation mitigations are both default ON.

**Essential invariants**:
- Cleanup daemon runs once per app lifetime, started/stopped in lifespan (error codes `APP-GENERAL-034`/`035`). NEVER re-introduce the piggyback cleanup trigger in `get_connection()`, NEVER call `_cleanup_all_instances()` from the daemon loop, NEVER remove the `try/finally` cleanup in `BackgroundJobManager._execute_job`, NEVER remove the close-on-clobber guard (Linux TID recycling leaks FDs otherwise).
- HNSW/FTS cache cap: `DEFAULT_MAX_CACHE_SIZE_MB = 4096` overlaid when config is `None`. Hot-reload via `ConfigService._hot_reload_cache_size_cap()` is narrow-scoped to `index_cache_max_size_mb` and `fts_cache_max_size_mb` only.
- Bug #881 omni fan-out: two caps in sequence — `omni_wildcard_expansion_cap` (per-pattern, default 50) and `omni_max_repos_per_search` (total fan-out, default 50). Fan-out searches pass `hnsw_cache=None` to bypass global HNSW cache.
- Bug #897 mitigations both default ON since v9.23.3 (bootstrap-only flags in `config.json`): `enable_malloc_trim` (calls `glibc malloc_trim(0)` after cleanup cycles), `enable_malloc_arena_max` (injects `MALLOC_ARENA_MAX=2` into systemd unit via auto-updater).
- Codex CLI auto-install (Story #845, step 6.7) and Codex auth modes (Story #846: `api_key` / `subscription` / `none`) both run idempotently on every auto-updater cycle.

Files: `src/code_indexer/server/storage/database_manager.py`, `src/code_indexer/server/startup/lifespan.py`, `src/code_indexer/server/repositories/background_jobs.py`, `src/code_indexer/server/cache/__init__.py`, `src/code_indexer/server/services/config_service.py`, `src/code_indexer/server/mcp/handlers/_utils.py`, `src/code_indexer/server/cache/hnsw_index_cache.py`.

→ Full reference: `docs/server-memory-invariants.md`

### Depmap Parser Module Split and Anomaly Channels (Story #887, Epic #886)

The depmap parser was split into four cohesive modules (mcp_parser orchestration, parser_tables extraction, parser_hygiene normalization/dataclasses, parser_graph aggregation) under the MESSI rule 6 cap. Anomalies route through a self-classifying `AnomalyType` enum into `parser_anomalies[]` (structural file defects) vs `data_anomalies[]` (source-graph drift) channels.

**Essential invariants**:
- Public API dual-surface (both stable contracts): `get_cross_domain_graph()` returns legacy 2-tuple; `get_cross_domain_graph_with_channels()` returns 4-tuple `(edges, all, parser_anomalies, data_anomalies)`.
- `AnomalyType` self-classifying: each variant carries bound `channel: Literal["parser", "data"]`. Routing is enum lookup — no manual classification logic. Aggregates route identically.
- Frozenset-keyed bidirectional dedup: `_check_bidirectional_consistency` aggregates by `frozenset({normalize(source), normalize(target)})` so one anomaly emits per unordered edge pair (both sides normalized via strip_backticks + lowercase).
- Self-loop preservation is unconditional: `finalize_graph_edges()` excludes self-loops from the empty-types drop filter (still emits GARBAGE_DOMAIN_REJECTED anomaly AND preserves the edge).
- `_anomaly_to_dict()` in `handlers/depmap.py` handles both `AnomalyEntry` and `AnomalyAggregate` — reused at every response assembly site.

Files: `src/code_indexer/server/services/dep_map_{mcp_parser,parser_tables,parser_hygiene,parser_graph}.py`, `src/code_indexer/server/mcp/handlers/depmap.py`. Tests: `tests/unit/server/services/test_dep_map_887_*.py` (70 tests across 8 ACs + 4 remediation blocker files).

→ Full reference: `docs/depmap-parser-architecture.md`

### cidx-meta backup contract (Story #926)

The server can maintain a continuous git backup of the cidx-meta directory to a remote repository. Sync runs BEFORE indexing in the refresh path; push/fetch failures defer (surface as `RuntimeError` after indexing) while conflict failures short-circuit immediately. URL changes are idempotent: bootstrap runs at Save time AND at the start of every backup-enabled refresh cycle.

**Essential invariants**:
- Mutable base path only: all git ops execute against `<server_data_dir>/data/golden-repos/cidx-meta/`. NEVER operate inside `.versioned/cidx-meta/v_{timestamp}/` snapshot directories. Use `get_cidx_meta_path(server_data_dir)` as the single source of truth.
- Index always runs after sync: deferred-failure pattern — push failure becomes `SyncResult.sync_failure` and is raised AFTER indexing completes (job marked FAILED). Conflict failure raises `RuntimeError` immediately (after `git rebase --abort`) and short-circuits indexing.
- Conflict resolution invokes Claude via `invoke_claude_cli()` (Story #885 A10 boundary). 600s timeout → SIGTERM → 30s grace → SIGKILL.
- Externalized conflict-resolution prompt at `src/code_indexer/server/mcp/prompts/cidx_meta_conflict_resolution.md` (operator-editable; must contain `{conflict_files}`, `{branch}`, `{repo_path}` placeholders).
- `detect_default_branch()` honors remotes with `main` as default; falls back to `master` on failure.

Files: `src/code_indexer/server/services/cidx_meta_backup/` (bootstrap, sync, conflict_resolver, branch_detect, paths), `src/code_indexer/global_repos/refresh_scheduler.py` (backup branch), `src/code_indexer/server/web/routes.py` (config save route).

→ Full reference: `docs/cidx-meta-backup.md`

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
- **Bug #937 (Codex spawns without CIDX_MCP_AUTH_HEADER on staging)**: Root cause: `_cached_auth_header` on `MCPSelfRegistrationService` is only populated by `register_in_claude_code()`, which requires Claude CLI. On staging servers where Claude CLI is absent, the cache stays None and `build_codex_mcp_auth_header_provider()` raised RuntimeError. Fix: three-step fallback chain in `build_codex_mcp_auth_header_provider()`: (1) `get_cached_auth_header_value()` — fast path; (2) `build_auth_header_from_creds()` via `get_or_create_credentials()` — ensure_registered path; (3) `build_header_from_stored_credentials()` — direct stored-creds path (new method on `MCPSelfRegistrationService`, calls `register_in_claude_code(creds)` to populate cache as side effect then returns `_cached_auth_header`). When all three fail, `logger.error()` fires before RuntimeError (MESSI Rule 13: Anti-Silent-Failure). `CodexInvoker` now fails fast when provider raises: logs ERROR (not WARNING), returns `FailureClass.RETRYABLE_ON_OTHER`, and does NOT spawn the subprocess. A10 invariant: `MCPSelfRegistrationService.set_instance()` must be called (by `service_init`) before any provider closure is invoked. After set_instance with stored credentials, the provider succeeds without Claude CLI. Files: `src/code_indexer/server/services/mcp_self_registration_service.py`, `src/code_indexer/server/services/codex_mcp_auth_header_provider.py`, `src/code_indexer/server/services/codex_invoker.py`. Tests: `tests/unit/server/services/test_codex_mcp_auth_header_provider_937.py`, `tests/unit/server/services/test_codex_invoker_937_auth_header.py`, `tests/unit/server/startup/test_codex_mcp_registration_937.py`.
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

Source of truth: `src/code_indexer/__init__.py` `__version__` (line 9). Also update `README.md` version badge (line 5), `CHANGELOG.md` (new entry at top), `docs/architecture.md` server response example, `docs/query-guide.md` version refs. Check for stale refs in `docs/server-deployment.md`.

DO NOT bump on CIDX version change: `server/app.py` OpenAPI spec, `test-fixtures/` test data.

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

### Phase 3.7 Dep-Map Graph-Channel Repair (Stories #908/#910/#911/#912, Epic #907)

Phase 3.7 is inserted in `_run_branch_a_dep_map` between Phase 3.5 (metadata backfill) and Phase 4 (index regeneration), at progress percent 78. It repairs graph-channel anomalies detected by the dep-map parser: SELF_LOOP, MALFORMED_YAML, GARBAGE_DOMAIN_REJECTED (deterministic) and BIDIRECTIONAL_MISMATCH (Claude-audited, only when `invoke_claude_fn` is provided).

**Essential invariants**:
- Bootstrap flag `enable_graph_channel_repair` in `config.json` (bootstrap-only, NOT DB), default `True` — pattern follows Bug #897 `enable_malloc_trim`. When `False`, `_run_phase37` returns immediately.
- Append-only JSONL journal at `~/.cidx-server/dep_map_repair_journal.jsonl` (CIDX_DATA_DIR honored per Bug #879). 12-field JSON object per line, atomic writes via module-scope `_write_lock`. `RepairJournal` class in `dep_map_repair_phase37.py`.
- Action enum master list grows per story: `self_loop_deleted`, `malformed_yaml_reemitted`, `auto_backfilled`, `claude_refuted_pending_operator_approval`, `inconclusive_manual_review`, `claude_cited_but_unverifiable`, `pleaser_effect_caught`, `repo_not_in_domain`, `verification_timeout`, `claude_output_unparseable`.
- Verdict enum: `CONFIRMED | REFUTED | INCONCLUSIVE | N_A` (deterministic repairs use `N_A`).
- File split (MESSI Rule 6): executor (orchestration), phase37 (journal types + SELF_LOOP), malformed_yaml, bidirectional + bidirectional_parser + bidirectional_verify.
- BIDIRECTIONAL_MISMATCH prompt template externalized to `src/code_indexer/server/mcp/prompts/bidirectional_mismatch_audit.md`. Timeouts overridable via `CIDX_BIDI_CLAUDE_SHELL_TIMEOUT` / `CIDX_BIDI_CLAUDE_OUTER_TIMEOUT` (defaults 270s/330s).

→ Full reference: `docs/depmap-phase37-architecture.md`

---

## Further Reading

- Architecture: `docs/architecture.md`
- Server deployment: `docs/server-deployment.md`
- Cluster architecture: `docs/cluster-architecture.md`
- Fault injection: `docs/fault-injection-operator-guide.md`
- Memory retrieval: `docs/memory-retrieval-operator-guide.md`
