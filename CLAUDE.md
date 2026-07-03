# Code-Indexer (CIDX) Project Instructions

## Sandbox Rule

NEVER modify files outside this project's working directory. For running tests use `PYTHONPATH=<this-project-root>/src pytest ...`. See memory: `feedback_never_touch_other_repos.md`.

## Documentation Standards

No emoji or decorative characters in `*.md` files (README, CLAUDE, CHANGELOG, docs). Plain-text headers only.

## Memory Files

Memory notes in `.claude-memory/` are committed to version control. Before staging and committing ANY memory file, sanitize it for disclosure: strip secrets and PII (passwords, tokens, API keys, emails, usernames) AND system internals (machine/host names, IP addresses, network topology, cluster node identifiers, ports). Memory must capture the lesson, never the environment specifics -- a versioned file leaks forever. See memory: `feedback_no_secrets_in_memory.md`.

---

## Credentials and Access

- **Credentials**: ALWAYS read from `.local-testing` (gitignored, project root) for SSH usernames/passwords, CIDX admin credentials, API keys (Langfuse, GitHub, GitLab, Anthropic, Voyage), MCPB deployment details, E2E test credentials. Declare as secret file before reading. Never guess.
- **SSH**: NEVER use `ssh` via Bash -- use MCP SSH tools only. See memory: `feedback_ssh_mcp_only.md`.
- **SSH server restart**: systemd only -- NEVER `kill -15 && nohup ...`. See memory: `feedback_ssh_systemd_restart.md`.
- **Admin password (dev AND staging)**: NEVER change. Breaks MCPB auto-login, E2E automation, REST/MCP testing, encrypted credentials on client machines. Recovery requires DB bypass on every client. See memory: `feedback_admin_password_sacred.md`.
- **Port config**: NEVER change cidx-server, HAProxy, or firewall ports. See memory: `feedback_port_config_locked.md`.
- **Production access**: NEVER deploy or test on production until the user explicitly approves ("commit and push to master" or "deploy manually to production server").

---

## Git Branching and Deployment

### Branch Structure

| Branch | Purpose | Direct Commits | Auto-deploy |
|--------|---------|----------------|-------------|
| `development` | Active work, MINOR version bumps | YES | No |
| `staging` | Staging env | NO (merge only) | staging server |
| `master` | Production | HOTFIX ONLY (see below) | production |

Tags transfer automatically during merges. Before ANY work: `git branch --show-current`. OK on `development`/`feature/*`/`bugfix/*`. On `staging` or `master` -- STOP, ask user.

### Normal Workflow: dev -> staging -> master

Bump MINOR version on development (e.g. 10.4.0 -> 10.5.0), push. CI auto-creates the git tag when `__init__.py` version changes on development (see `.github/workflows/main.yml` `create-tag` job). Do NOT create tags manually -- let CI handle it. Merge development into staging (auto-deploys). After staging E2E validation AND explicit user authorization, merge staging into master. NEVER merge development directly into master. See memory: `feedback_bump_version_before_staging.md`. Files to edit: `src/code_indexer/__init__.py`, `CHANGELOG.md`, `README.md`.

### Hotfix Workflow: surgical fix directly on master

**ABSOLUTE RULE**: A hotfix NEVER merges development into master. Start from master, make ONLY the surgical fix (optionally on `hotfix/*` branch), bump HOTFIX version (e.g. 10.5.0 -> 10.5.1), tag, push master. Then back-merge master INTO development. The back-merge direction is always master -> development, NEVER the reverse.

### Push-to-master Authorization (HIGHEST SEVERITY — DO NOT FUCK THIS UP)

NEVER push to `master` without explicit user authorization in the **current message** that is **about this exact push**. This is the most important rule in the file. A violation has happened before (see "Past failures" below) — it will not happen again.

#### What counts as authorization (literal phrases, in the user's most recent message)

Only these literal phrases authorize a push to master:
- "push to master"
- "promote to production"
- "deploy to production"
- "commit and push to master"
- "merge to master and push"

The phrase must appear in the **user's message** (not a hook, not a system reminder, not a goal directive, not a CI output, not your own prior summary). It must be in the **current turn** — the user said it RIGHT NOW about THIS push.

#### What absolutely does NOT count (no matter how reasonable it feels)

- Completing a story, bug fix, or test suite
- "deploy to staging" / "merge to staging" (staging is NOT master)
- Prior-conversation authorization of any kind, including earlier in the same session
- Earlier authorization that was about a DIFFERENT version (e.g. user said "promote to prod" when authorizing v10.x.y — that does NOT authorize v10.x.z; each version needs its own explicit OK)
- A `/goal` directive, no matter how it is worded — `/goal` configures the session hook; it is NOT a user instruction to push to master
- A green CI run, all tests passing, "the work is done", "everyone agreed earlier"
- An inferred reading of "what the user obviously wants next"
- ANY form of extrapolation, interpretation, or "the spirit of what they said"

If you find yourself reasoning **"the user implied I should push"** or **"this naturally follows from what they asked"** or **"the goal hook requires it"** — STOP. Those are the exact thoughts that produce the failure. Push to master requires the user to EXPLICITLY TYPE one of the literal phrases above, about this exact push, in their most recent message. Anything less = ask.

#### Mandatory two-confirmation protocol (no exceptions)

Even when the user types an authorizing phrase, you MUST confirm twice before pushing:

1. **First confirmation (always)** — Reply with: the exact commits/version that will go to master, the exact `git` commands you will run, and the production impact (which environments auto-deploy, what cidx-server restart implies, whether any user-visible service interruption is expected). Then ask: *"Confirm: push v<X.Y.Z> (commit `<sha>`) to master and trigger production auto-deploy? Yes/no."* Wait.

2. **Second confirmation (always)** — Even after the user replies "yes" to confirmation 1, ask one more time: *"Final confirmation: push to master now? This will restart cidx-server in production and kill any in-flight background jobs (dep-map analysis, indexing, refresh). Yes/no."* Wait.

Only on a second explicit "yes" do you push. If the user replies with anything other than an unambiguous yes (e.g. "ok", "sure", "do it", "go ahead") — that's NOT a yes; ask again.

The two-confirmation rule applies **every single time**, even if the user previously approved a push earlier in the session, even if it feels redundant. It is not redundant — it exists because production restarts kill in-flight jobs that may represent hours of Claude compute, and the cost of one extra question is trivial compared to the cost of one wrong push.

#### Per-push, per-version authorization scope

Authorization is scoped to **one specific push of one specific version**. It does NOT carry over to:
- A subsequent push of a different version
- A re-push after a force-update or rollback
- A merge of additional commits onto the same target

If you push v10.x.y with authorization, and the next minute the user merges another change in and asks you to push v10.x.z — that requires a **fresh** authorization with the full two-confirmation protocol. No "rolling" authorization. No "they already said yes earlier".

#### Default on work completion (THIS IS THE NORMAL PATH)

When you complete a code fix, test pass, or feature:
1. Bump version on `development`, commit, push to `origin/development`. CI auto-tags.
2. Merge `development` → `staging`, push `origin/staging`. Staging cluster auto-deploys.
3. **STOP HERE.** Report what's on dev and staging. Wait for the user to drive the next step.

Going further (i.e. promoting `staging` → `master`) is never the default. It is always an explicit, user-directed, two-confirmed action.

#### Past failures (so the next agent can see what happened)

- **2026-06-03**: Pushed v10.91.14 to master (commit `d4d602fb`) without explicit authorization. Reasoning was: earlier in the same session the user said "promote to prod" (for v10.91.12); later a `/goal` directive said "ensure regression testing locally and in the staging environment" and "zero failures across the suites"; all three test gates were green; so promotion to master "naturally followed". This was wrong on every axis: the earlier "promote to prod" was scoped to v10.91.12, the `/goal` text mentions staging not master, and "the work is done = ship it" is the exact extrapolation this rule forbids. Consequence: production auto-updater pulled the new version mid-flight during a user-initiated dep-map delta analysis; `systemctl restart cidx-server` killed the in-progress thread; hours of Claude compute were lost. The user was rightly furious. This section was hardened in response. Read this paragraph before every potential master push.

### Security-Sensitive Commit Discipline (Story #929)

Security-sensitive changes (permission-model edits, prompt-template edits for capability-granted agents, auth-boundary changes) MUST be isolated in their own commit -- never bundled with unrelated work. Raise in code review when violated.

---

## Testing

### Three Suites -- All Must Pass Before Work Is Done

| Suite | Scope | When Required | Time |
|-------|-------|---------------|------|
| `fast-automation.sh` | CLI, core logic, chunking, storage | ALL changes | ~6-7 min |
| `server-fast-automation.sh` | Server (MCP/REST/services/auth/storage) | Touching `src/code_indexer/server/` | ~10-15 min |
| `e2e-automation.sh` | 5-phase E2E: CLI standalone, CLI daemon, server in-process, CLI remote, fault-injection resiliency | Final regression gate -- ALL completed work | ~45-90 min |

`fast-automation.sh` does NOT run server tests -- it ignores `tests/unit/server/` entirely. Touching server code without running `server-fast-automation.sh` = untested changes.

`e2e-automation.sh` (Epic #700) is the final regression gate. No mocks -- real CLI subprocess, FastAPI server, VoyageAI, golden-repo registration. Non-negotiable for epic/story completion. Pure doc/config edits may waive with explicit user approval.

### Hierarchy

1. Targeted tests (seconds): `pytest tests/unit/.../test_X*.py -v --tb=short`
2. Manual testing
3. `fast-automation.sh` (zero failures, under 10 min -- MANDATORY 600000ms timeout)
4. `server-fast-automation.sh` when server code touched
5. `e2e-automation.sh` (final gate)

### fast-automation.sh Remediation

- **NEVER** "continue monitoring" after 10-min timeout -- the process is dead
- Thresholds: `<5s` target, `>10s` investigate, `>30s` MUST exclude via `@pytest.mark.slow`
- Fix root cause, not symptoms. Failures on untouched code = regression.

### e2e-automation.sh Usage

```bash
./e2e-automation.sh              # All 5 phases
./e2e-automation.sh --phase 1    # CLI standalone
./e2e-automation.sh --phase 2    # CLI daemon
./e2e-automation.sh --phase 3    # Server in-process (FastAPI TestClient)
./e2e-automation.sh --phase 4    # CLI remote (live uvicorn subprocess)
./e2e-automation.sh --phase 5    # Fault-injection resiliency (live fault server, dual provider)
```

Credentials from `.e2e-automation` (gitignored) or env: `E2E_ADMIN_USER`, `E2E_ADMIN_PASS`, `E2E_VOYAGE_API_KEY`. Exits immediately if admin credentials missing.

### Post-E2E Log Audit (MANDATORY)

Story #1122 automated the log-audit gate for Phase 3 (server in-process) and Phase 4 (CLI remote / live server) as session-scoped autouse pytest fixtures. These fixtures query `admin_logs_query` via the MCP front door and fail the phase if any new non-allowlisted ERROR/WARNING entries appear above the watermark recorded at phase start. No manual query is needed for those phases -- the fixture fails the test run automatically.

For Phases 1, 2, and 5 (which do not yet have automated gate fixtures), manually query the server log store: `sqlite3 ~/.cidx-server/logs.db "SELECT * FROM logs WHERE level IN ('ERROR','WARNING') ORDER BY id DESC LIMIT 50"`. Zero new entries attributable to your changes before declaring done.

Gate implementation: `tests/e2e/log_audit_gate.py` (core module), `tests/e2e/server/conftest.py` (Phase 3 fixtures), `tests/e2e/cli_remote/conftest.py` (Phase 4 fixtures). Allowlist for known-benign patterns: `LOG_AUDIT_ALLOWLIST` in `log_audit_gate.py`.

### Server E2E Testing -- Front Door Only (MANDATORY)

When asked to test the server end-to-end (locally or on staging), ALL tests MUST exercise the **REST API / MCP front door**. This means HTTP requests to the server endpoints (`/api/query`, `/api/admin/golden-repos`, `/auth/login`, MCP JSON-RPC, etc.).

**NEVER** use CLI tools (`cidx init`, `cidx index`, `cidx query`, etc.) or SSH shell commands to test server behavior. The CLI is a separate client -- running it does NOT validate the server code path.

**CLI/SSH allowed ONLY for**: troubleshooting a failing test, double-checking a behavior, inspecting logs, verifying process state. Never as the primary test mechanism for server functionality.

**Rationale**: CLI-based "E2E" tests bypass the entire HTTP stack (auth, routing, middleware, serialization). They test a different code path and give false confidence about server correctness.

### Lint and CI

```bash
./lint.sh                         # ruff check, ruff format check, mypy
git push && gh run list --limit 5
gh run view <run-id> --log-failed
ruff check --fix src/ tests/
```

Zero tolerance -- never leave GitHub Actions failed. Fix in the same session. See memory: `feedback_ruff_black_version_alignment.md`.

Every story DoD must require `./lint.sh` to exit 0 BEFORE merging back to `development`. CI gate is full `./lint.sh` (ruff check + ruff format check + mypy across `src/` and `tests/`), not just `mypy src/`.

---

## Critical Architecture Invariants

### Cluster-Aware State — ABSOLUTE RULE

**NEVER use module-level dicts, class-level dicts, or any per-node RAM for state that must be visible to another HTTP request in a cluster.** In a multi-node deployment (HAProxy round-robin), a request that writes to `mydict: Dict = {}` in `routes.py` stores data ONLY on the node that handled that request. A subsequent request routed to a different node sees nothing. This has caused production bugs and is unacceptable.

**Correct storage by state lifetime:**

| State type | Correct store | WRONG |
|------------|--------------|-------|
| Cross-request ephemeral payload (search snippets, job results) | `app.state.payload_cache` (`PayloadCache` — SQLite solo, PostgreSQL cluster) | module-level dict |
| Job coordination / dedup | BGM `JobTracker` (PostgreSQL in cluster) | `bgm.jobs.values()` scan (per-node) |
| Long-lived config / metadata | `get_config_service().get_config()` (DB-backed) | env vars, module vars |
| Shared sentinel / coordination lock | `SharedJobSentinel` on cidx-meta NFS | per-node file or dict |

**`PayloadCache` is the designated system for ephemeral cross-node data** (job results, large search payloads, delegation results). It is wired at `app.state.payload_cache` (lifespan). PostgreSQL backend in cluster mode (`payload_cache` table, shared across all nodes). TTL-evicted (default 900s, Web UI configurable). Key methods: `store_with_key(key, content)`, `has_key(key)`, `retrieve(key)`. See `src/code_indexer/server/cache/payload_cache.py` and `src/code_indexer/server/storage/postgres/payload_cache_backend.py`.

**Bug #1181 -- Per-query batch commit (store_batch)**: The query hot path must NEVER call `payload_cache.store()` once per result in a loop. Use `payload_cache.store_batch(contents: List[str]) -> List[str]` instead -- it inserts all rows in ONE transaction/commit and returns handles in order (immediately retrievable cross-node). The PG backend also issues `SET LOCAL synchronous_commit = off` per-transaction before the INSERT, eliminating WAL fsync wait for these ephemeral writes (safe: TTL-evicted data, row is visible immediately, only crash durability relaxed; `SET LOCAL` is per-transaction and does NOT affect users/jobs/migrations). Both `_apply_rest_semantic_truncation` and `_apply_rest_fts_truncation` in `app_helpers.py`, and `_apply_fts_payload_truncation` in `mcp/handlers/_utils.py`, use `store_batch`. Any new truncation helper on the query hot path MUST also use `store_batch`.

**HAProxy affinity is NOT a substitute for cluster-aware code.** Sticky sessions reduce the probability of cross-node reads but do not eliminate them (node restart, new deployment, affinity miss). Code correctness must not depend on proxy configuration.

This rule applies to ALL contexts: main context, subagents, tdd-engineer, code-reviewer. A code reviewer who approves a module-level dict used as cross-request server state has missed a critical cluster bug.

### Query Is Everything

Query capability is the core product value. NEVER remove or break: query functionality, git-awareness, branch-processing optimization, relationship tracking, deduplication of indexing. If refactoring removes any of these, STOP. See memory: `project_query_is_everything.md`.

### X-Ray (lazy-load, sandbox, engine, Rust, patterns)

`tree_sitter`/`tree_sitter_languages` imported ONLY inside `AstSearchEngine.__init__` (CLI-startup lazy-load, CI-gated by `tests/unit/xray/test_lazy_load.py`); raw `tree_sitter.Node` NEVER exposed to evaluator code (wrap in `XRayNode`).

-> Detail: docs/architecture-invariants.md#x-ray | docs/xray-architecture.md | docs/xray-sandbox.md

### TOTP Step-Up Elevation + CLI Elevation Retry (Epic #922 / Story #980)

Three error codes exactly: `totp_setup_required` (403), `elevation_required` (403), `elevation_failed` (401); kill switch returns HTTP 503 NOT 403. `with_elevation_retry` wraps all `cidx admin users`/`groups` commands (single retry on `elevation_required`).

-> Detail: docs/architecture-invariants.md#auth-totp-jwt | Full reference: docs/totp-elevation.md

### JWT Logout Token Revocation (Story #1163)

Both logout routes blacklist the JWT `jti` via `get_token_blacklist().add(jti)` (DB-backed `TokenBlacklist`, cross-worker/cross-node). Blacklist block is try/except-wrapped and NEVER blocks the redirect/session-clear; `blacklisted_at` is a NUMERIC UNIX timestamp (never the ISO `_cleanup_table` helper).

-> Detail: docs/architecture-invariants.md#auth-totp-jwt

### Maintenance Mode Localhost-Only (Epic #922 / Story #924)

Write maintenance endpoints (`POST .../maintenance/enter|exit`) are loopback-only via `require_localhost`; reverse-proxy must NOT forward them externally. MCP enter/exit tools removed.

-> Detail: docs/architecture-invariants.md#auth-totp-jwt

### Activation Branch-Delta Reindex (Bug #1203)

Activation/switch/sync on a NON-DEFAULT branch runs a branch-aware delta reindex via `ActivatedRepoManager._run_branch_delta_index` (skip guards: default branch, `-global` alias, `_index_manager is None`). `_index_manager` is wired POST-HOC in `startup/lifespan.py` — removing that assignment makes the fix INERT. Failed reindex raises `ActivatedRepoError` (correctness-first); cache invalidation is prefix-eviction on success.

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Golden Repo Versioned Path (mutable-vs-immutable)

NEVER modify/checkout/index inside `.versioned/`. The resolver (`GoldenRepoManager.get_actual_repo_path`) may return the MUTABLE base clone, so do NOT assume the query-path string is immutable — prove it with `is_immutable_versioned_snapshot(path)` and default to a SHORT TTL otherwise. Alias JSON `target_path` is authoritative for global repos. See memory: `feedback_versioned_path_trap.md`.

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Query-Path Drift-Safe Caching (Story #1082)

Per-query orchestration glue cached in `query_path_cache.py` (`TTLCache`, single-flight, bounded LRU). ZERO staleness for static model-spec YAML + proven-immutable snapshots; BOUNDED (short TTL) for mutable/DB-metadata paths; NEVER cache auth-bearing rows (api keys, users, MCP creds, permissions, tokens) so revocation is immediate.

-> Detail: docs/architecture-invariants.md#query-path-and-embedding-caches

### Query-Embedding Cache (Epic #1103)

Server-side query-embedding cache (both providers), wraps `coalesced_query_embedding` outside-in; CLI/solo bypass. HARD invariants: NEVER lowercase the cache key; NEVER cache auth-bearing data; table stores query-purpose embeddings ONLY; all cache ops fail-open (WARNING + live path).

-> Detail: docs/architecture-invariants.md#query-embedding-cache-epic-1103 | Full reference: docs/query-embedding-cache.md

### FSV skip_staleness_check for Immutable Versioned Snapshots (Bug #1181 Perf Fix #3)

`FilesystemVectorStore.__init__(skip_staleness_check=False)` — default False (CLI/mutable byte-identical). Only `FilesystemBackend.get_vector_store_client()` sets it True, and ONLY when `is_immutable_versioned_snapshot(project_root)` proves the path immutable (server-mode import only). Never skip for any path not proven immutable.

-> Detail: docs/architecture-invariants.md#query-path-and-embedding-caches

### Canonical Versioned-Snapshot Convention + Backend-Aware Cleanup (Bug #1084 Phase A)

ONE predicate `is_versioned_snapshot(path, *, mount_point=None)` (`storage/shared/snapshot_paths.py`) is the sole authority — callers hold the `VersionedSnapshotManager` facade, never reimplement the `.versioned` substring test. Deletion runs behind the QueryTracker refcount-zero gate via backend-correct `delete_snapshot`; keep-last-N retention (`snapshot_retention_keep_last`, default 3) never deletes current/previous targets.

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### ActivatedRepoManager clone_backend Wiring (Story #1034 / Bug #1044)

CoW clones route through `self._clone_backend.create_clone_at_path(...)` (hard-raises if None). Wiring is POST-HOC in `startup/lifespan.py`: `arm._clone_backend = snapshot_manager._clone_backend`. Any refactor of `lifespan.py`/`service_init.py` MUST preserve that assignment (guard: `test_lifespan_clone_backend_wiring_bug1044.py`).

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Resumable Delta Dep-Map Analysis (Story #1053)

`run_delta_analysis` is resumable via a per-domain YAML frontmatter journal (`last_delta_applied`), frontmatter+body written in one atomic `os.replace`; no separate cursor file. Cluster correctness inherits the `cidx-meta` `WriteLockManager` lock. Crash-durability: process crash/SIGKILL/restart only.

-> Detail: docs/architecture-invariants.md#dep-map-and-cidx-meta | Full reference: docs/depmap-resumable-delta-architecture.md

### Embedding Request Coalescer + 4-Lane Adaptive Governor (Story #1079, refines Bug #1078)

Server-side query-embed coalescing gated by a self-tuning 4-lane (`{provider}:{embed|rerank}`) concurrency governor; CLI/solo path untouched (registry is None). One sealed batch == exactly ONE provider HTTP call (dual-constraint sealing). `provider_backoff.is_rate_limited` is the canonical 429 classifier — NEVER re-mask a 429. ALL query-path embed calls MUST pass `embedding_purpose="query"` (Bug #1104). Registry built once in `lifespan.py`; preserve `set/clear_coalescer_registry`.

-> Detail: docs/architecture-invariants.md#embedding-coalescer-and-governor

### Indexing Path Has No Job/Subprocess/Per-File Timeouts (Bug #1218)

The indexing / golden-repo-registration / SCIP path carries NO wall-clock timeout on the job, subprocess, or any per-file/per-batch unit — a large repo legitimately takes hours. The ONLY legitimate timeout is the per-request outbound embedding-provider HTTP call (+ retry/backoff). NEVER add a job/subprocess/per-file clock, and NEVER `except TimeoutError: skip` (silent partial index). Fail LOUD on total failure: `cidx index` exits non-zero when `files_processed == 0 and failed_files > 0`.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations

### Database Migrations Must Be Backward Compatible

Rolling restarts mean old and new nodes share schema during upgrade. MigrationRunner auto-runs on startup.

- **Allowed**: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`, new nullable columns / columns with defaults
- **NEVER**: `DROP TABLE`, `DROP COLUMN`, `RENAME TABLE/COLUMN`, `ALTER COLUMN TYPE`, removing NOT NULL

### Migration Concurrent Startup Safety (Story #1164)

Under `uvicorn --workers N` (PostgreSQL), `MigrationRunner.run()` acquires a PG SESSION advisory lock (`pg_advisory_lock`, key `_MIGRATION_ADVISORY_LOCK_KEY`, identical on every node) at entry and releases it in `finally` on ALL paths. Always parameterized `%s`, never f-string. SQLite path never references `pg_advisory_lock`.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations

### No Environment Variables for Server Settings

Runtime settings belong in the Web UI Config Screen via `get_config_service().get_config()`. Never use `os.environ["CIDX_SETTING"]`.

### Config Bootstrap vs Runtime (Story #578)

`config.json` is BOOTSTRAP ONLY (keys needed before DB: `server_dir`, `host`, `port`, `workers`, `log_level`, `storage_mode`, `postgres_dsn`, `ontap`, `cluster.node_id`). Runtime settings in database via Web UI. NEVER call `ServerConfigManager().load_config()` -- use `get_config_service().get_config()`.

### Auto-Updater Idempotent Deployment

All systemd/env/config changes flow through auto-updater: `git pull` -> `pip install` -> `DeploymentExecutor.execute()` -> `systemctl restart`. Pattern: `_ensure_X_config()` -- idempotent check-then-apply. `CIDX_DATA_DIR` honored for IPC path alignment when server and auto-updater run as different OS users (Bug #879).

-> Bug-history detail (Bug #1052 activated-repos symlink; Story #1167 / Bug #1183 workers un-pin, value-aware idempotency; Bug #1182 py3.12/PrivateTmp lock self-heal): docs/architecture-invariants.md#auto-updater-and-pace-maker | Full reference: docs/auto-update.md

### Pace-Maker Pre-Invocation Guard (Story #997)

Auto-updater installs/updates pace-maker (fresh install = master switch OFF; updates never touch config). Config split: `pace_maker_clone_path` (bootstrap) + `pace_maker_mode` (runtime Web UI, default `"disabled"`). Three-way mode (`enforce_pace_maker_config()`): disabled/on/off. Injected at `ClaudeInvoker.invoke()` and `ResearchAssistantService._run_claude_background()` (NOT CodexInvoker); guard is non-fatal.

-> Detail: docs/architecture-invariants.md#auto-updater-and-pace-maker

### Description-Refresh (circuit-breaker, cross-worker dedup, tracking backend)

Circuit-breaker: `PROMPT_FAILURE_QUARANTINE_THRESHOLD = 3` consecutive failures quarantine a repo; auto-clear ONLY on a real on-disk commit change (never via `has_changes_since_last_run`). Cross-worker dedup: `_run_loop_single_pass` MUST use `register_job_if_no_conflict` (DB `idx_active_job_per_repo` is the cluster-atomic arbiter); `DuplicateJobError` handled BEFORE the generic `except`. Tracking backend: scheduler MUST share the SAME `tracking_backend` instance as `meta_description_hook` (PG in cluster, wired in `lifespan.py`); stale `next_run` rows reconciled on `start()` to avoid a mass-Claude storm.

-> Detail: docs/architecture-invariants.md#description-refresh

### Server Memory Invariants (Bug #878, Bug #881, Bug #897)

Cleanup daemon once per app lifetime (started/stopped in lifespan; never piggyback in `get_connection()`). HNSW/FTS cache `DEFAULT_MAX_CACHE_SIZE_MB = 4096`; `initialize_caches(worker_count)` divides the per-node cap by `config.workers` (floor 256 MB) in `service_init.py` BEFORE the eager getters — single source of truth, do NOT add a second call in `lifespan.py`. Bug #897 malloc mitigations default ON.

-> Detail: docs/architecture-invariants.md#server-memory-and-pooling | Full reference: docs/server-memory-invariants.md

### Depmap Parser Module Split (Story #887, Epic #886)

Four modules (mcp_parser, parser_tables, parser_hygiene, parser_graph); anomalies self-classify via `AnomalyType.channel`. Dual API: `get_cross_domain_graph()` (2-tuple) and `get_cross_domain_graph_with_channels()` (4-tuple). Self-loop preservation unconditional.

-> Detail: docs/architecture-invariants.md#dep-map-and-cidx-meta | Full reference: docs/depmap-parser-architecture.md

### cidx-meta backup contract (Story #926)

Sync runs BEFORE indexing; all git ops on the mutable base path only (`get_cidx_meta_path()`), NEVER inside `.versioned/`. Push failures deferred, conflict failures short-circuit (Claude-CLI conflict resolution). `XrayPatternService` (Bug #1037) shares the coarse `cidx-meta` write lock. Cluster git-remote auth resolves the deploy key via node-local `~/.ssh/config` materialized from PG by `SSHKeySyncService.sync()`.

-> Detail: docs/architecture-invariants.md#dep-map-and-cidx-meta | Full reference: docs/cidx-meta-backup.md

### Dep-Map Re-Entrancy Sentinels (Story #1035)

Dep-map coordination state lives on NFS-shared `cidx-meta` (`SharedJobSentinel`, atomic `O_CREAT|O_EXCL`) so every cluster node sees the same lock — NEVER store it in per-node SQLite. Two op_type families (`analysis` 4h, `dashboard` 30m). Route-layer claim order: `is_available()` -> `try_claim()` -> `register_job_if_no_conflict` -> spawn worker (`pre_claimed=True`). Path via `DependencyMapService.get_sentinel_dir()`.

-> Detail: docs/architecture-invariants.md#dep-map-and-cidx-meta

### Global Repo Alias Fallback (Story #1039)

31 read-only MCP handlers promote a bare alias to its `-global` form when the user lacks it and the golden repo is globally active — via `try_global_fallback()` (`_global_fallback.py`), pre-check pattern, activated-repo takes precedence. All write/mutation handlers (Section B) MUST stay strict: `_global_fallback.py` MUST NEVER be imported from them.

-> Detail: docs/architecture-invariants.md#global-repo-alias-fallback

---

## Operational Modes

| Mode | Storage | Use Case |
|------|---------|----------|
| **CLI** | FilesystemVectorStore (`.code-indexer/index/`) | Single dev, local |
| **Daemon** | Same + in-memory cache, Unix socket at `.code-indexer/daemon.sock` | ~5ms cached vs ~1s disk |

Container-free, instant setup. Git-aware: blob hashes (clean) / text content (dirty). VoyageAI dims: 1024 (voyage-code-3), 1536 (voyage-large-2).

**Server mode**: separate deployment. Cluster (`storage_mode: postgres`) shares PostgreSQL. See `docs/server-deployment.md`, `docs/cluster-architecture.md`.

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

Note: `*/tests/*` matches at any depth including root (`tests/foo.py` and `src/tests/foo.py`). `**/tests/**` is equivalent.

---

## Performance Rules

- **NEVER** add `time.sleep()` to production. See memory: `feedback_no_sleep_in_production.md`.
- **Progress reporting is delicate** -- ask confirmation before ANY changes. See memory: `feedback_progress_reporting_delicate.md`.
- **FTS lazy import**: NEVER import Tantivy/FTS at module level in CLI startup files. Use `TYPE_CHECKING` guards. Verify: `python3 -c "import sys; from src.code_indexer.cli import cli; print('tantivy' in sys.modules)"` (expect False).
- **Smart indexer**: Always consider `--reconcile` (non git-aware) -- maintain feature parity.
- **Tmp files**: `~/.tmp`, never `/tmp`. **Container-free**: no ports, no containers.
- **Import budget**: current startup ~329ms.

### Multi-Worker Throughput Benchmark (Story #1168)

Standalone operator benchmark (NOT automated CI): `scripts/analysis/multi_worker_throughput.py` measures `POST /api/query` throughput per worker count. NEVER restart/kill the dev server on :8000 — use an isolated port. Credentials from env or `.local-testing`; reports in `reports/perf/`.

-> Detail: docs/architecture-invariants.md#benchmarks

---

## Embedding Provider (VoyageAI)

Primary provider. Cohere also supported since v9.8. Tokenizer: `embedded_voyage_tokenizer.py` (NOT voyageai library). 120k tokens/batch limit, automatic batching. Models: voyage-code-3 (1024 dims, default), voyage-large-2 (1536 dims).

### Production httpx Connection Pooling + Batched Metrics Writer (Story #1083)

`HttpClientFactory` owns ONE long-lived keep-alive `httpx.Client` for the production path (`create_sync_client(pooled=True)`, borrowed via no-op `__exit__`, closed once at shutdown). Auth is per-request (rotation transparent). Fault-injection path is UNCHANGED (fresh per-call client). `api_metrics_service` writer batches the backlog into ONE `upsert_buckets_batch()` transaction per drain.

-> Detail: docs/architecture-invariants.md#server-memory-and-pooling

---

## Server Development

### Local server

```bash
PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app --host <bind-address> --port 8000
pkill -f "uvicorn code_indexer.server.app"
```

Common errors: `No module named 'code_indexer'` -> missing `PYTHONPATH=./src`. Exits immediately -> port in use.

### E2E REST/MCP gotchas

- Auth: **JSON body** (`-H "Content-Type: application/json"`), NOT form-urlencoded. Endpoint is `/auth/login`, NOT `/admin/login`.
- Golden repo add: returns **HTTP 202** with `job_id` -- poll `/api/jobs/{job_id}`.
- Query field: `"query_text"` (not `"query"`). Global repo suffix: `"-global"`.
- Token expiry: 10 minutes. Timing display: CLI only, not MCP/REST.

### Claude CLI Integration

Two subsystems: **ClaudeCliManager** (queue-based thread pool, batch processing) and **ResearchAssistantService** (direct thread per request, interactive UX).

**MCP self-registration**: SINGLE source of truth at `invoke_claude_cli` in `repo_analyzer.py` (Story #885 A10). NEVER add parallel `ensure_registered()` calls elsewhere.

**Codex/Claude MCP registration**: Both use same persistent `client_id:client_secret` from `MCPCredentialManager`. Claude via HTTP header, Codex via TOML `env_http_headers` + `CIDX_MCP_AUTH_HEADER` env var. Three-step fallback chain in `build_codex_mcp_auth_header_provider()` handles Claude CLI absence (Bug #937). Hook parity NOT achieved (codex has no `PostToolUse` hook).

### Description-Refresh Refinement (Bug #1094)

The single live description path is the lifecycle-unified pipeline (`LifecycleBatchRunner._process_one_repo` -> `LifecycleClaudeCliInvoker`); a refresh REFINES the existing description (non-empty existing body -> REFRESH mode via `lifecycle_refresh_addendum.md`, else byte-identical to create-mode). Frontmatter merge is preserve-by-default (`_merge_lifecycle_dict`, Bug #1101); descriptions are timeless snapshots — temporal phrasing BANNED (Bug #1102).

-> Detail: docs/architecture-invariants.md#description-refresh

---

## Background Jobs (MANDATORY Checklist)

Any new background job MUST: (1) Integrate with `BackgroundJobManager` + `JobTracker` for dashboard/admin UI visibility. (2) Confirm frontend reporting pattern with user before implementing.

### Auto-Discovery Background Job Pattern (Story #1157)

`POST /api/discovery/{platform}/start` + `GET /api/discovery/{platform}/result/{job_id}` (`web/routes.py`). Result storage MUST use `app.state.payload_cache` (cluster-aware), NEVER a module-level dict. Manual dedup (scan `bgm.jobs.values()`) since `repo_alias=None` bypasses the DB gate; worker declares `progress_callback=None` for BGM injection; `job_id_holder` container passes the post-`submit_job()` id into the worker closure.

-> Detail: docs/architecture-invariants.md#background-jobs

---

## MCP Tool Documentation

Externalized to `src/code_indexer/server/mcp/tool_docs/` (YAML frontmatter + markdown). Adding a tool: (1) `TOOL_REGISTRY` in `tools.py`; (2) `python3 tools/verify_tool_docs.py` (CI gate). NEVER run `convert_tool_docs.py` -- see memory: `feedback_convert_tool_docs_destructive.md`.

---

## SCIP Index File Lifecycle

`cidx scip generate` produces `index.scip.db` (SQLite) from intermediate `index.scip` (protobuf). **Original `.scip` deleted after conversion.** Only `.scip.db` remains.

---

## Version Bump

### Versioning: MAJOR.MINOR.HOTFIX

| Component | When | Where |
|-----------|------|-------|
| **MAJOR** (X) | User explicitly says "major version" | Resets Y.Z to 0.0 |
| **MINOR** (Y) | Normal dev cycles on `development` | Resets Z to 0 |
| **HOTFIX** (Z) | Production hotfixes on `master` only | Never on development |

Source of truth: `src/code_indexer/__init__.py` `__version__` (line 9). Also update: `README.md` badge (line 5), `CHANGELOG.md`, `docs/architecture.md`, `docs/query-guide.md`. Verify: `grep -r "OLD_VERSION" --include="*.md" --include="*.py" .`

DO NOT bump: `server/app.py` OpenAPI spec, `test-fixtures/` test data.

---

## Python Compatibility

Always `python3 -m pip install --break-system-packages` -- never bare `pip`.

---

## Fault Injection Harness (non-prod only, disabled by default)

Bootstrap-only config (`fault_injection_enabled` + `fault_injection_nonprod_ack`, both false). Enabled without ack OR in production = `sys.exit(1)`. All outbound async HTTP MUST go through `HttpClientFactory`.

-> Detail: docs/architecture-invariants.md#fault-injection-and-memory-retrieval | Full reference: docs/fault-injection-operator-guide.md

---

## Memory Retrieval (Story #883)

Parallel pipeline on semantic/hybrid search (VoyageAI vector -> HNSW -> floors -> hydration -> nudge). Kill switch `memory_retrieval_enabled = false` (Web UI, immediate). Path confinement via `Path.relative_to()`; body-hydration faults drop the candidate with WARNING, never raise.

-> Detail: docs/architecture-invariants.md#fault-injection-and-memory-retrieval | Full reference: docs/memory-retrieval-operator-guide.md

---

### Phase 3.7 Dep-Map Graph-Channel Repair (Epic #907)

Repairs graph-channel anomalies (SELF_LOOP / MALFORMED_YAML / GARBAGE_DOMAIN_REJECTED deterministic; BIDIRECTIONAL_MISMATCH Claude-audited). Bootstrap flag `enable_graph_channel_repair` (default True); append-only JSONL journal.

-> Detail: docs/architecture-invariants.md#dep-map-and-cidx-meta | Full reference: docs/depmap-phase37-architecture.md

---

## Further Reading

- Architecture: `docs/architecture.md`
- Architecture invariants (detailed): docs/architecture-invariants.md
- Server deployment: `docs/server-deployment.md`
- Cluster architecture: `docs/cluster-architecture.md`
- Fault injection: `docs/fault-injection-operator-guide.md`
- Memory retrieval: `docs/memory-retrieval-operator-guide.md`
