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
| `fast-automation.sh` | CLI, core logic, chunking, storage | ALL changes | ~12-13 min (measured: 760s / 12,697 tests as of 2026-07-13; grows with suite size -- re-measure before trusting this number) |
| `server-fast-automation.sh` | Server (MCP/REST/services/auth/storage) | Touching `src/code_indexer/server/` | ~10-15 min |
| `e2e-automation.sh` | 5-phase E2E: CLI standalone, CLI daemon, server in-process, CLI remote, fault-injection resiliency | Final regression gate -- ALL completed work | ~45-90 min |

`fast-automation.sh` does NOT run server tests -- it ignores `tests/unit/server/` entirely. Touching server code without running `server-fast-automation.sh` = untested changes.

`e2e-automation.sh` (Epic #700) is the final regression gate. No mocks -- real CLI subprocess, FastAPI server, VoyageAI, golden-repo registration. Non-negotiable for epic/story completion. Pure doc/config edits may waive with explicit user approval.

### Hierarchy

1. Targeted tests (seconds): `pytest tests/unit/.../test_X*.py -v --tb=short`
2. Manual testing
3. `fast-automation.sh` (zero failures, under ~13 min -- MANDATORY 900000ms timeout; a timeout hit here is NOT automatically a hang -- check the actual duration against the current baseline above before assuming one)
4. `server-fast-automation.sh` when server code touched
5. `e2e-automation.sh` (final gate)

### fast-automation.sh Remediation

- **NEVER** "continue monitoring" after the 15-min (900000ms) timeout -- the process is dead
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

**`PayloadCache` is the designated system for ephemeral cross-node data** (job results, large search payloads). It is wired at `app.state.payload_cache` (lifespan). PostgreSQL backend in cluster mode (`payload_cache` table, shared across all nodes). TTL-evicted (default 900s, Web UI configurable). Key methods: `store_with_key(key, content)`, `has_key(key)`, `retrieve(key)`. See `src/code_indexer/server/cache/payload_cache.py` and `src/code_indexer/server/storage/postgres/payload_cache_backend.py`.

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

### Golden Repo Registry-Orphan Guard + Reconcile (Bug #1317)

A registered golden repo must never end up as a "registry-orphan": a `golden_repos` row (PostgreSQL in cluster mode, SQLite in solo mode) with no on-disk clone and/or no alias pointer file. Two mechanisms enforce this:

1. **Provisioning atomicity** (`golden_repo_manager.py`): in `add_golden_repo`'s background worker, global activation (alias pointer + global registry write) is now a HARD requirement, not best-effort — if `GlobalActivator.activate_golden_repo` fails, the just-inserted `golden_repos` row is rolled back (in-memory cache + SQLite/PG backend) and the clone directory is cleaned up before re-raising as `GitOperationError`. A successfully-provisioned global repo is therefore guaranteed to have its alias pointer, keeping the #1315 `index_path` fallback a rare safety net rather than steady state. Symmetrically, `remove_golden_repo`'s background worker now removes the registry row BEFORE deleting any on-disk files (previously files were deleted first; if the subsequent row removal then failed, the "rollback" only restored the PER-WORKER in-memory dict — useless cross-node — while the files were already gone, producing exactly this bug). If registry removal fails, nothing is touched; only after removal is confirmed does filesystem cleanup run, so a later filesystem-cleanup failure can only ever leave a harmless orphan CLONE (files, no row), never a registry-orphan.

**Bug #1523 — the same discipline was missing for the GLOBAL half**: the "never a registry-orphan" guarantee above was only ever true for the LOCAL `golden_repos` row. `GlobalActivator.deactivate_golden_repo()` (global registry entry + `-global` alias pointer), and the three sibling lifecycle detach hooks (cidx-meta description `.md`, group-access revocation, wiki view records), all sat INSIDE an `if cleanup_successful:` branch in `remove_golden_repo`'s worker — so a `_cleanup_repository_files()` failure skipped every one of them even though the row was already deleted earlier in the same worker. Confirmed live on clustered staging: row gone (retry reports "not found"), `-global` alias still present and still queryable with broken content (`fatal: not a git repository`), and the leftover clone directory blocking re-registration — permanently wedged with no front-door recovery. Fixed by treating row removal, global deactivation, and on-disk cleanup as three SEPARABLE steps: all four detach steps now run UNCONDITIONALLY and BEFORE filesystem cleanup (deactivate before the clone starts disappearing, never the reverse; `actual_path` is resolved earlier so removing the alias pointer cannot invalidate the cleanup target), each still individually non-fatal, and the "Resource leak detected" `GitOperationError` is raised afterwards exactly as before — the ORDER/GATING changed, never the failure signal. Any future post-row-removal teardown step MUST be added to that unconditional block, never behind a cleanup-success gate.

**Bug #1523 reconcile Pass 4 (reverse direction)**: `reconcile_golden_repo_registry` gained `_reconcile_global_registry_orphans` — the ordering fix stops NEW wedges but cannot heal installations wedged before it shipped, and Pass 1-3 iterate `list_golden_repos()` so they can never observe an alias that no longer has a row. Pass 4 lists global entries via the backend-agnostic `GlobalActivator.registry` (PostgreSQL cluster / SQLite solo, Bug #1308 deferred resolution) and deactivates any whose `golden_repos` row is gone, reusing `deactivate_golden_repo()` rather than reimplementing teardown. Sweeping is only sound because global entries have exactly ONE production writer (`GlobalActivator.activate_golden_repo`, golden repos only) and `RESERVED_GLOBAL_NAMES` is empty — there are no legitimate row-less global entries. Two guards, both against a shared-backend read that under-reports rows: (a) a ZERO-row `golden_repos` read suppresses the entire pass (indistinguishable from a total read failure); (b) every candidate is re-confirmed INDIVIDUALLY via `_resolve_golden_repo_authoritative()` (bypasses the per-worker cache), deliberately instead of a fleet-wide ratio threshold — a ratio guard would recreate the "can never heal" trap Bug #1382 had to dig this reconciler out of. Aliases that Pass 1 saw with a row are skipped, so Pass 4 never races Pass 2's in-flight removal jobs.
2. **Reconcile** (`server/services/golden_repo_reconciler.py`): `reconcile_golden_repo_registry(golden_repo_manager)` scans `list_golden_repos()` (the shared backend — correct in both SQLite and PostgreSQL modes) and, for every alias whose `get_actual_repo_path()` raises `GoldenRepoNotFoundError`, submits its removal via the existing `remove_golden_repo` cascade (row + alias pointer + global registry + activated-repo cascade — reused, not reimplemented). Wired fail-soft at server startup in `startup/lifespan.py` (mirrors the `reconcile_orphaned_exports` / `fail_orphaned_jobs` pattern) so any orphans are cleaned up automatically on the next restart. Three safety mechanisms are MANDATORY and must never be removed: (a) a positive health-gate (`_golden_repos_dir_is_healthy`, returns False on any `OSError`) skips the whole sweep if the golden_repos_dir is not a readable directory; (b) a mass-deletion **circuit-breaker** — the sweep classifies all repos absent/healthy FIRST, then deletes ONLY if `absent_count/total <= ORPHAN_FRACTION_ABORT_THRESHOLD` (0.5); if more than half resolve absent it ABORTS with a WARNING and deletes nothing (a stale/unmounted NFS makes `os.path.exists()` return False for ALL repos — without this guard a transient mount blip at startup would wipe the entire registry + every user's activations; see memory `project_nfs_host_down_hangs_systemd`); (c) **pointer-repair** — a healthy repo (clone resolves) that `is_globally_active()` (global-registry truth, not the pointer) but lacks its `-global` alias pointer is REPAIRED via `AliasManager.create_alias(...)`, never deleted (this is the #1315 symptom). In cluster mode the sweep is single-flighted across workers via `job_tracker.register_job_if_no_conflict`/`DuplicateJobError` (skipped when no job_tracker — solo/CLI).

**Circuit-breaker confirmation + escalation (Bug #1382)**: a live staging incident showed the (b) circuit-breaker above had no recovery path — a genuine, persistent orphan set (crash-recovery gap: DB restored, on-disk clones not) tripped the >50% threshold on EVERY restart for ~2 months with only a repeated log-only WARNING. Fix: a persisted, cross-restart confirmation counter (`golden_repo_reconcile_breaker_state` table — SQLite solo / PostgreSQL cluster, reusing the SAME `_sqlite_backend` GoldenRepoManager already injects, no new storage layer). Each high-ratio abort records a stable fingerprint (sorted alias list) of the orphan-candidate set; if the SAME fingerprint is observed on `CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD` (3) CONSECUTIVE sweeps, each with a healthy `golden_repos_dir`, the sweep proceeds with removal instead of aborting forever. A base-dir-unhealthy event or a normal within-threshold sweep resets the counter — real infra flapping or an unstable orphan set can never accumulate toward confirmation, preserving the original one-off-abort safety. Rolling-deploy hardening: a same-fingerprint sweep only increments the count if at least `MIN_BREAKER_OBSERVATION_GAP_SECONDS` (30 min) has elapsed since the previous observation — otherwise a single rolling restart across node-1/node-2/node-3 (each firing the sweep within minutes of the others) would reach "3 consecutive confirmations" from one restart wave instead of genuinely separate operational events; a sooner-arriving same-fingerprint observation is a no-op (count/state unchanged), and this gate never applies to a fingerprint change or a base-dir-unhealthy reset. Separately, `HealthCheckService._collect_golden_repo_reconcile_breaker_failures()` surfaces a currently-tripped breaker as a DEGRADED `/health` `failure_reasons` entry immediately (reusing the existing health-check surface, not a new alerting mechanism) so staleness is visible on the first abort, not after months of silent restarts.

**Follow-up (Issue #1383)**: the escalation signal above went silent exactly when auto-removal fired, and the buildup message didn't say which repos were at risk. Two fixes: (1) the buildup DEGRADED message now includes the actual at-risk alias set (parsed from the persisted `orphan_fingerprint`) plus a "will auto-remove at N/N confirmations" framing, instead of a bare count; (2) `_reset_breaker_state()` in `golden_repo_reconciler.py` now runs AFTER Pass 2 (the actual `remove_golden_repo()` calls), gated on `result.orphans_removed` being non-empty — a confirmed sweep whose Pass 2 removals ALL fail (e.g. a backend outage) preserves the confirmation count instead of silently discarding the 3-restart investment; the unrelated `else` branch's unconditional reset for a normal within-threshold sweep is unchanged. When Pass 2 does remove at least one orphan, a discoverable trace is also persisted (`golden_repo_reconcile_auto_heal_event` table — SQLite solo / PostgreSQL cluster, same dual-backend pattern as the breaker-state table) recording the removed aliases + timestamp; this record deliberately survives the breaker-state reset and is exposed as the `last_golden_repo_reconcile_auto_heal` field on the `/health` response (`HealthCheckResponse`, populated in `get_system_health()` via `HealthCheckService.get_golden_repo_reconcile_auto_heal_event()`) as an INFORMATIONAL-ONLY field (never folded into DEGRADED `status`/`failure_reasons` — it documents an already-resolved historical event, not a current problem) so an operator who wasn't watching `/health` in real time can still discover after the fact, through the actual `/health` response, that an automatic mass-removal occurred and which repos were affected. `occurred_at` is normalized to an ISO string in `get_golden_repo_reconcile_auto_heal_event()` regardless of backend (SQLite already returns a string; PostgreSQL's psycopg driver returns a native `datetime` for the `TIMESTAMPTZ` column) so the JSON response is consistent across both.

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Temporal Enable-Flag Cross-Table Reconciliation + Real-Data-Presence Detection (Bug #1390)

`RefreshScheduler`'s filesystem-reconciliation mechanism for `enable_temporal` had two independent defects that together triggered an unattended, multi-hour `cidx index --index-commits` rebuild against operator intent (a stale/emptied temporal directory falsely re-armed the scheduled-refresh trigger). Both are fixed:

1. **Cross-table update**: `_reconcile_registry_with_filesystem` used to write ONLY to `self.registry` (the `global_repos` table, `-global`-suffixed alias). This server ALSO tracks `enable_temporal` in `golden_repos_metadata` (bare-alias-keyed table, structurally separate backend) — reconciliation never touched it, so the two tables could drift and disagree indefinitely. Fixed by reconciling BOTH tables independently (checking each table's own current value against the filesystem truth, not assuming they already agree), using the same bare/`-global` alias normalization Bug #1373 established in `_set_enable_temporal_flag` (`server/mcp/handlers/repos.py`): `bare_alias` strips exactly one trailing `-global` suffix if present; `global_alias` is always re-derived from `bare_alias` (never blindly re-suffixed). `enable_scip` has no `golden_repos_metadata` column and stays registry-only.
2. **Real-data-presence detection**: `_detect_existing_indexes`'s temporal check used to be a pure directory-NAME match (`is_temporal_collection(d.name)`) against `.code-indexer/index/`, which cannot distinguish a genuinely populated temporal collection from a stale directory containing only the temporal metadata database (no quarter-shard vector data — e.g. after shard data was deliberately relocated for a maintenance operation). Fixed by requiring at least one temporal-named collection to also have a real `hnsw_index.bin` + `collection_meta.json` pair, reusing `iter_index_files_for_repo` (`server/services/hnsw_orphan_sweep/discovery.py`, Story #1360's existing "hnsw_index.bin + collection_meta.json pair defines a real HNSW collection" primitive) rather than writing new detection logic from scratch.

**Dependency wiring**: `RefreshScheduler` gained a new lazily-resolved `golden_repo_metadata` property (constructor param `golden_repo_metadata_backend`) mirroring the existing `registry` property's Bug #1308-hardened deferred-resolution pattern — an injected backend (tests) is used as-is; otherwise resolution defers to first access via `resolve_backend_registry_attr("golden_repo_metadata")` (a generalization of the `global_repos`-only `resolve_backend_registry_state`, both in `server/utils/registry_factory.py`) so `app.state.backend_registry` is guaranteed populated in postgres/cluster mode, falling back to a per-node `GoldenRepoMetadataSqliteBackend` in solo/CLI mode. Wired at construction time through `GlobalReposLifecycleManager` (new `golden_repo_metadata_backend` param, forwarded to `RefreshScheduler`) from `startup/lifespan.py`'s `make_lifespan()` closure (`backend_registry.golden_repo_metadata if backend_registry is not None else None`, the same accessor already used for `DescriptionRefreshScheduler`'s `golden_backend`).

**One-way reconciliation, auto-disable only (Bug #1406)**: item 1's cross-table fix above was still BIDIRECTIONAL — it also auto-ENABLED `enable_temporal` on both tables when filesystem detection reported real data present while the stored flag was `False`. This was the confirmed TRIGGER of a production incident: an operator's explicit "disable temporal + restore data" recovery procedure got silently converted back to enabled on the next scheduled refresh, launching an unattended multi-hour reindex against operator intent. Fixed by making `_reconcile_registry_with_filesystem` (split into `_reconcile_global_repos_temporal` / `_reconcile_golden_metadata_temporal` / `_reconcile_scip_flag` helpers) one-way for `enable_temporal` on BOTH tables: a stored `True` downgrades to `False` when the filesystem shows no real data (Bug #1390's fix, preserved); a stored `False` is NEVER flipped to `True` when the filesystem shows real data present — an INFO log documents the honored operator disable instead. The two tables are still reconciled fully independently (the #1390 drift lesson), so a residual split (e.g. registry `False`, golden_repos_metadata `True`, both matching or not matching the filesystem in different ways) is possible and is resolved only by explicit operator action or a later tick, never by auto-enabling. `enable_scip` reconciliation is explicitly OUT OF SCOPE for Bug #1406 and remains bidirectional, unchanged.

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Query & Search Timeouts Consolidation (Issue #1398)

`SearchTimeoutsConfig` (`server/utils/config_manager.py`, following the exact `SearchLimitsConfig` pattern) consolidates 5 previously hardcoded, non-Web-UI-configurable timeout constants into one validated config section: `search_code_handler_timeout_seconds` (180, replaces `protocol.py`'s `SEARCH_HANDLER_TIMEOUT_SECONDS`), `default_handler_timeout_seconds` (60, replaces `HANDLER_TIMEOUT_SECONDS`), `write_mode_handler_timeout_seconds` (720, replaces `WRITE_MODE_HANDLER_TIMEOUT_SECONDS`), `embedding_provider_timeout_seconds` (30, replaces the hardcoded `VoyageAIConfig()` default at the server-side query-embedding construction sites in `mcp/handlers/search.py`), `reranker_timeout_seconds` (15, replaces both hardcoded `timeout: float = 15.0` defaults in `reranker_clients.py`). Wired through `config_service.py` (`_search_timeouts_settings`/`_update_search_timeouts_setting`) and `web/routes.py` (`_VALID_CONFIG_SECTIONS`, `_get_current_config`, `_validate_config_section`) mirroring Story #1397's HNSW orphan-sweep two-layer pattern exactly.

**Sync/async MCP dispatch distinction (critical, easy to re-break)**: `protocol.py`'s `handle_tools_call` checks `is_async = asyncio.iscoroutinefunction(handler)`. BOTH branches now apply `_resolve_handler_timeout`'s value (the async branch's `asyncio.wait_for` was added by Story #1491 AC6) — but note the async wrapper cannot interrupt SYNCHRONOUS work inside a coroutine, so it bounds only genuinely-awaitable work. `search_code` is sync-dispatched (governed by `search_code_handler_timeout_seconds`).

**`regex_search` is SYNC-dispatched as of Story #1491 AC2** (superseding this section's original "async-dispatched" statement): the registry entry is the plain-`def` `handle_regex_search_sync`, so `protocol.py` offloads it via `run_in_executor` and its synchronous work (SQLite trigram prefilter, up to 8000 `Path.resolve()` calls, whole ripgrep output read + per-line `json.loads`) leaves the event loop. The async coroutine `handle_regex_search` still exists and is what `_omni_regex_search` awaits per repo.

**`_ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS` spans BOTH branches** (despite its name): membership is a property of the TOOL, not of the dispatch branch. Exempt tools (`regex_search`, `xray_search`, `xray_explore`, the `*_search_logs` family) get NO `asyncio.wait_for` on either branch, so `regex_search` remains governed solely by `search_limits_config.timeout_seconds` + its ripgrep subprocess timeout (Group A) — its pre-#1491 semantics, deliberately preserved. Rationale: `_omni_regex_search` loops repos SEQUENTIALLY, so an omni search over N repos has legitimate cumulative runtime no single fixed deadline can accommodate; and an outer `wait_for` around `run_in_executor` cannot cancel the worker thread anyway — it would only abandon it while returning a timeout error (work continues, unobserved, and the client is told it failed). **This is a deliberate, documented DEVIATION from Story #1491 AC2's literal wording** ("the regex search is subject to the dispatcher's timeout guard"): the starvation hazard AC2 exists to fix is resolved entirely by the sync-dispatch switch moving the synchronous work off the loop, independently of any deadline, so the pre-#1491 timeout semantics were preserved rather than changed. The same reasoning is recorded at the `_ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS` definition in `protocol.py`; change neither without the other. Any future tool whose sync/async declaration OR exempt-set membership changes silently changes which (if any) cap applies.

**CLI remote-timeout design decision**: `api_clients/base_client.py`'s hardcoded `httpx.Timeout(read=30.0, ...)` is CLIENT-side code that cannot read the server's `SearchTimeoutsConfig` (DB-backed, server-only). Resolved via a new optional `.code-indexer/.remote-config` JSON field `api_read_timeout_seconds`, read once in `remote/query_execution.py`'s `execute_remote_query` and threaded through `CIDXRemoteAPIClient.__init__(read_timeout_seconds=...)` into the `session` property's `httpx.Timeout(read=...)`. A persisted config field was chosen over a `cidx query --timeout` CLI flag: a shard-count-driven slow deployment needs a durable, standing fix rather than a flag to remember on every invocation. `None` (missing key) preserves the pre-#1398 hardcoded 30.0s default exactly.

**Dead-code removal**: `TEMPORAL_QUERY_TIMEOUT_SECONDS` (`services/temporal/temporal_fusion_dispatch.py`) was deleted entirely — confirmed zero real consumers since Story #1291 removed the multi-provider parallel fan-out branch that used it. Exposing it as a Web-UI setting would have added a misleading dead knob to the exact problem this issue reports.

**Template read-surface gap (Bug #1422)**: `temporal_inline_wait_seconds` is a 6th `SearchTimeoutsConfig` field (added by Story #1400) whose dict-level read surfaces (`_search_timeouts_settings` / `_get_current_config`) were already correctly wired, but `config_section.html`'s search-timeouts section never rendered it -- write-only, no Web UI display or edit control. Fixed by adding the missing display-table row and edit-form `<input type="number" step="0.001">` (float field, unlike its 5 integer siblings) alongside the existing 5 fields; no change to `config_service.py` or `web/routes.py` was needed.

-> Detail: docs/architecture-invariants.md#query-path-and-embedding-caches

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

### Per-Commit Temporal Dual-Embedder Indexing (Epic #1289)

Temporal indexing is per-commit-aggregated (message once + all changed-file diffs in ONE document per commit, unified `{project}:commit:{hash}:{j}` point ids) under pluggable, coexisting embedder adapters (`voyage-context-4` contextual 0% overlap, `embed-v4.0` standard 15% overlap; `TemporalConfig.embedders`/`.active_embedder`), quarterly-sharded per embedder. Incremental refresh is reconcile-based (full git-log walk is cheap and by-design regardless of blank-out -- there never was a cursor mechanism to preclude; the actual skip is a disk-scan per shard) — verified end-to-end with byte-identical pre-existing vector files and zero new embedding calls on a no-op refresh. Bug #1405 fixed blank-out (`temporal_blank_out.py`) to SKIP the shared bookkeeping directory (bare `code-indexer-temporal`, anchoring the single shared `TemporalMetadataStore` used by every shard) via a data-presence discriminator (`_is_shared_bookkeeping_directory`: bare name + no `hnsw_index.bin` + no nested `vector_*.json`), instead of hard-deleting -- and thus amputating the shared metadata store -- on every single run; genuine legacy monoliths (which always have real vector data) are still deleted unchanged.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations (see "Per-Commit Temporal Dual-Embedder Indexing")

-> Detail: docs/architecture-invariants.md#indexing-and-migrations

### Golden/Server Temporal All-Branches Gate (Story #1412)

Golden-repo temporal indexing tracks ONLY the branch registered at golden-repo registration by default. The existing `all_branches` opt-in (`temporal_options.all_branches` -> `--all-branches` appended to `cidx index --index-commits`) is retained as scaffolding but shipped DISABLED behind a new server-wide runtime flag `temporal_all_branches_enabled: bool = False` (`IndexingConfig`, Web Config screen checkbox, no env var). Gate off + a request ACQUIRES `all_branches=true` -> reject loudly at three front doors (REST `POST /api/admin/golden-repos`, Web `save_temporal_options`, MCP `add_golden_repo`), never silent-drop. Defense-in-depth at the three command builders (`golden_repo_manager.py`, `refresh_scheduler.py`, MCP `_build_temporal_index_cmd`) skips `--all-branches` + logs a WARNING when a stored legacy `all_branches=true` value is seen with the gate off. Reversible with NO re-index: point ids/payloads carry no branch membership, so enabling later just widens the git-log walk on the next refresh. Standalone CLI `--all-branches` and the `temporal_indexer` engine parameter are UNTOUCHED (server/golden surface only); `hidden_branches` (Bug #306) is a separate system, out of scope.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations (see "Golden/Server Temporal All-Branches Gate")

### HNSW Finalize-Time Orphan Detect+Repair + Zero-Tolerance Health (Epic #1333, Story #1359)

Every HNSW build/finalize path (`build_index`, `rebuild_from_vectors`, incremental `save_incremental_update`) runs `check_integrity()` -> `repair_orphans()` (Story #1358 fork primitive) -> re-verify BEFORE the index is persisted, via `HNSWIndexManager._detect_and_repair_orphans()`. A repair that fails to converge raises `HNSWIntegrityRepairError` loud — never a silent partial index. Health check (`cidx health`/MCP `check_hnsw_health`/REST/Web) exposes `orphan_count` as a STRICT BINARY signal: 0 is OK, any value >0 is ERROR — no WARNING tier, no configurable threshold (a graded-severity design was explicitly rejected during maintainer review).

**Missing-fork-capability degrades, never aborts (Bug #1415)**: `check_integrity()`/`repair_orphans()` exist ONLY in the custom `LightspeedDMS/hnswlib` fork, not stock PyPI hnswlib. Bug #1392 originally made every build/finalize entry point raise `HNSWCapabilityError` immediately when the fork is missing — but that still aborted the ENTIRE indexing operation (a confirmed fleet-wide production outage on 2026-07-14: ~12 golden repos failed refresh, an already-embedded batch discarded with the index never persisted, and one activated-repo branch-delta reindex blocked by Bug #1203's correctness-first `ActivatedRepoError`). `HNSWCapabilityError` and the raising `_ensure_hnswlib_capability()` are REMOVED. `_detect_and_repair_orphans()` now guards via a non-raising `_hnswlib_has_fork_capability()` hasattr predicate (never a try/except AttributeError around the calls — that would risk mis-classifying a genuine AttributeError from inside a present implementation) and, when the fork is absent, logs ONE WARNING and returns immediately — the orphan hardening pass is skipped, but the caller's build/save proceeds and persists the already-computed embeddings as a valid, correct index (proven via a fresh independent reload asserting the persisted vector count, not just "no exception"). The same guard-and-skip pattern was applied to the two other previously-unguarded call sites: `services/hnsw_health_service.py` (Level 4 integrity check: reports a new `hnswlib_capability_available: Optional[bool]` field, `valid=True`, `orphan_count=None` — never a false-positive corruption ERROR) and `server/services/hnsw_orphan_sweep/repair_executor.py` (`process_candidate()`: new `SweepOutcome.CAPABILITY_UNAVAILABLE`, reusing the existing server-side `check_hnswlib_capability()` probe from Bug #1392's `hnswlib_capability_check.py` rather than a third hasattr reimplementation). `hnswlib_capability_available` propagates through `CollectionHealthResult` (REST/Web) and `cidx health`'s human-readable output — a SEPARATE signal from `orphan_count`, never folded into that zero-tolerance binary. The Bug #1392 non-fatal SERVER STARTUP probe (`run_hnswlib_capability_startup_check()`, WARNING/ERROR-logs-only, never blocks startup) is unchanged and reused as-is.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations (see "HNSW Finalize-Time Orphan Detect+Repair + Zero-Tolerance Health")

### HNSW Fleet Orphan Repair Sweep (Epic #1333, Story #1360)

Paced, resumable background sweep (`src/code_indexer/server/services/hnsw_orphan_sweep/`) that repairs the PRE-EXISTING fleet backlog of orphaned indexes built before S2's build-path fix existed -- S2 only protects newly-built/rebuilt indexes. Discovery composes the SAME `list_golden_repos()`/`list_all_activated_repositories()` primitives other schedulers reuse (never a third enumeration mechanism), walking `.code-indexer/index/` for `hnsw_index.bin` + `collection_meta.json` pairs and skipping `.versioned/` snapshots via `is_versioned_snapshot`. Repair acquires the SAME `.index_rebuild.lock` `HNSWIndexManager` build/finalize uses, re-checks integrity under the lock immediately before writing, and invalidates the server-side `HNSWIndexCache` singleton on success so a running query-serving process sees the fix without a restart. The durable cursor (`hnsw_orphan_sweep_state` table, SQLite solo / PostgreSQL cluster) is a STRING stable sort key (`"golden:{alias}:{relpath}"` / `"activated:{user}/{alias}:{relpath}"`) — NEVER a numeric offset, since temporal shards and activated repos are created/deleted continuously between ticks. Cluster dedup is `register_job_if_no_conflict` ONLY — deliberately NOT `ShardOwnership.owns()` (that primitive is fail-open cache-locality, not a coverage guarantee; filtering by it here would create a real repair coverage gap). Dashboard pattern: one short job PER TICK (mirrors `ActivatedReaperScheduler`), never one job spanning the whole multi-tick pass; cross-pass stats live on `GET /api/admin/hnsw-orphan-sweep/stats`, independent of JobTracker. Ships ON by default (`batch_size=15`, `tick_interval_minutes=7`), both adjustable via `get_config_service()`.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations (see "HNSW Fleet Orphan Repair Sweep")

### Chunk Storage Layout: SHARDED_JSON vs CHUNKS_DB (Epic #1454, Story #1456/#1455)

Two coexisting on-disk chunk layouts, both fully supported simultaneously (Story #1460 will run a rollout bake window needing both readable): legacy **SHARDED_JSON** (one `vector_<hash>.json` file per chunk, 4-level hash-sharded) and the new **CHUNKS_DB** (one SQLite `chunks.db` file per collection, `ChunkStore` engine — `src/code_indexer/storage/sqlite_chunk_store.py`, Story #1455).

**Sole authority — `resolve_chunk_layout()`**: `src/code_indexer/storage/shared/chunk_layout.py`'s `resolve_chunk_layout(collection_dir) -> ChunkLayout` is the ONLY function authorized to decide a collection's layout. NEVER independently probe for `chunks.db`'s existence or hand-check the `chunks_db` field — every layout-decision call site must import and call this resolver. It reads a `chunks_db` key at the TOP LEVEL of `collection_meta.json` (sibling of `metadata`/`hnsw_index`, never nested inside them) and is **fail-closed**: any absent/malformed/invalid discriminator resolves to `SHARDED_JSON` — it never raises and never guesses. **Canonical schema (Story #1458 AC3 Finding 4, corrected from this story's own originally pulled-forward shape)**: the discriminator's value is a versioned object carrying EXACTLY an integer `version >= 1` -- `{"chunks_db": {"version": 1}}`. Validity is governed SOLELY by that `version` field (present, `int`, `>= 1`; a bare `bool` does NOT count as a valid integer even though `isinstance(True, int)` is `True` in Python) -- an `enabled` key is NOT part of the contract at all, present or absent, `True` or `False`; a superset object that also carries a harmless `enabled` key (written by earlier callers) still validates purely on `version`. `write_chunks_db_discriminator()` writes `{"version": CHUNK_LAYOUT_DISCRIMINATOR_VERSION}` only.

**Discriminator commit is a mandatory FINAL step**: `write_chunks_db_discriminator(collection_dir)` must be called ONLY AFTER `chunks.db` and all of its indexes (HNSW, path_index) are fully written and durable — it raises `FileNotFoundError` if `collection_meta.json` doesn't already exist (never creates one from scratch, so an out-of-order caller fails loudly). A collection whose data is fully valid but whose discriminator was never committed (or is later lost) MUST resolve as `SHARDED_JSON` — proven by `tests/unit/storage/test_chunk_storage_1456_ac1_ac6.py`.

**id_index.bin retirement for CHUNKS_DB collections**: for a CHUNKS_DB collection, NO code path may read or write `id_index.bin` (a point_id -> file_path map, unrelated to the HNSW integer-label bridge which lives in `collection_meta.json`'s `hnsw_index.id_mapping` and stays untouched). Every consumer resolves point_ids through `ChunkStore`'s primary key instead. Implemented across `filesystem_vector_store.py`: `search()` (query hot path), `get_point()`, `delete_points()`, `_batch_update_payload_only()`, `upsert_points()`'s orphan cleanup (via a dedicated `_upsert_points_chunks_db()` write path), `_apply_incremental_hnsw_batch_update()`, `end_indexing()` (commits the discriminator as its mandatory FINAL step), `_save_path_index()`'s co-write skip, `_rebuild_path_index_from_disk()`, `scroll_points()`'s rglob safety-valve, `get_file_index_timestamps()`, `sample_vectors()`, `validate_embedding_dimensions()`, `count_points()`/`get_indexed_file_count_fast()` fallbacks, `_calculate_and_save_unique_file_count()`, and `rebuild_hnsw_filtered()` (branch isolation); plus the daemon's `_load_semantic_indexes()` install gate (`daemon/service.py`) and `HNSWIndexManager.rebuild_from_vectors()`, which streams from `chunks.db` via `ChunkStore.stream_all()` instead of `rglob("vector_*.json")`, preserving Bug #306's `hidden_branches` branch-visibility filter semantics exactly. `get_collection_size()` needed zero changes (already a generic `rglob("*")` byte-count, layout-agnostic). `IDIndexManager.rebuild_from_vectors` is retired by construction for CHUNKS_DB (its only non-temporal caller, `_load_id_index`, is never reached).

**Production write-path opt-in**: `FilesystemVectorStore(use_chunks_db_for_new_collections=...)` — explicit constructor param wins; `None` (default) falls back to the `CIDX_CHUNKS_DB_NEW_COLLECTIONS` env var; unset env var means `False` everywhere, so all ~20 existing `FilesystemVectorStore(...)` call sites (CLI, daemon, server) stay byte-identical unless explicitly opted in. `create_collection()` records in-session build intent in `self._chunks_db_mode`; `_is_chunks_db_collection(collection_name, collection_path)` is the combined authority EVERY write/mutation call site must use (never the bare `resolve_chunk_layout()`) — it returns `True` if either this session's intent dict says so OR the durable discriminator is already committed from a prior session. Temporal collections are always excluded (Epic #1289 is a separate, untouched subsystem). Real E2E-proven via the actual `cidx init`/`cidx index`/`cidx query` CLI with real embeddings: `chunks.db` created, zero `vector_*.json` files, correct semantic ranking, and a second incremental `cidx index` run correctly extends the collection.

**Main-thread-only point-ID resolution (binding, do not deviate)**: `search()`'s HNSW-load worker (the `ThreadPoolExecutor` closure `load_index()`) MUST NOT perform any id-index/chunk-store path resolution for CHUNKS_DB collections — it only needs `hnsw_index.id_mapping`, which is worker-safe. The chunk-layout resolution AND the `ChunkStore` open for hydration happen on the MAIN/calling thread, strictly AFTER `.result()` returns — `sqlite3` connections are not safely shared across threads. Query hydration then dispatches: unfiltered (Case A) takes the top `limit` HNSW candidates FIRST, then hydrates only those (at most `limit` chunk-store reads, no existence pre-check); filtered/overfetch (Case B) evaluates the payload filter per HNSW candidate with `lazy_load`-conditional early exit, identical semantics to the legacy path. Both flag values of `lazy_load` are proven to return byte-identical result sets.

**Mid-build window is a real bug class (two confirmed instances)**: any write/finalize call site that checks the bare `resolve_chunk_layout()` instead of `_is_chunks_db_collection()` will silently misclassify a FRESH build as `SHARDED_JSON`, because the discriminator isn't committed until `end_indexing()`'s mandatory final step. This produced two real bugs, both caught only by driving the actual `cidx index` CLI (not unit tests alone): `_save_path_index()` silently wrote an empty `id_index.bin` on every collection's first build, and `rebuild_hnsw_filtered()` (branch isolation — the path a real indexing run actually takes) published `vector_count: 0` despite real data in `chunks.db`. Both fixed; any NEW write/finalize call site added to this layout must use `_is_chunks_db_collection()`, never the bare resolver.

**`collection_meta.json` writes must be atomic+durable**: this file holds the load-bearing `hnsw_index.id_mapping`. `write_chunks_db_discriminator()` (`chunk_layout.py`) writes it via the same temp-file + fsync + `os.replace` + directory-fsync pattern as `HNSWIndexManager._atomic_write_metadata_durable` (Bug #1407) — never a bare `write_text()`, which would risk destroying the id_mapping bridge on a mid-write crash.

### Temporal Never Writes Legacy JSON (Bug #1528)

Temporal (git-commit-history) indexing was the file-explosion workload Epic #1454 was commissioned to fix, yet it was the one workload hard-excluded from the consolidated `chunks.db` layout — measured live: 487,076 `vector_*.json` files for one real repo, 15,693 for a trivial one. Binding rules now:

1. **`create_collection()` layout decision is TRI-STATE for temporal** (`filesystem_vector_store.py`). The store retains the caller's raw `use_chunks_db_for_new_collections` in `self._new_collection_layout_explicit`. A temporal collection is built as CHUNKS_DB unless the caller EXPLICITLY passed `False` (`--new-collection-layout=sharded_json`) — no env var, no server context, no flag required. SEMANTIC collections are UNCHANGED (Story #1488's context-dependent default: SHARDED_JSON for CLI/daemon, explicit `chunks_db` from the server). The previous unconditional `and not is_temporal_collection(...)` carve-out discarded even an explicit caller request, including the server's own `--new-collection-layout=chunks_db` child arg.
2. **The CLI temporal branch MUST pass the layout through** (`cli.py`, the `if index_commits:` branch). It previously constructed `FilesystemVectorStore(base_path=index_dir, project_root=...)` with no layout argument at all — a second, independent gap. Guarded by an AST test (`tests/unit/cli/test_temporal_index_layout_wiring_1528.py`).
3. **Pre-existing legacy shards are migrated IN PLACE before any temporal write.** An existing collection's committed discriminator always wins, so the write-path fix alone cannot cover data indexed before it. `consolidate_legacy_temporal_shards(index_dir, console=...)` (`chunk_migration_cli.py`) runs at the top of the temporal branch, under the index-mutation lock it already holds, reusing the SAME `consolidate_collection_in_place` engine `--migrate-chunks-to-sqlite` drives — never a second migration mechanism. Any failure ABORTS indexing (`sys.exit(1)`): a failed shard is still authoritative in its legacy layout, so proceeding would add more legacy rows to it. Semantic collections are never auto-migrated here (their layout stays an explicit CLI opt-in).
4. **Fleet migration consolidates temporal IN PLACE, not to a "sister location"** (`fleet_migration/orchestrator.py`). `_consolidate_temporal_namespaces` routes every temporal shard through the SAME kind-agnostic `_consolidate_collections` helper as semantic collections (so it inherits the symlink/`.versioned` re-validation), and WARNs for any namespace that still has a legacy sister alias pointer (pointer-first resolution keeps reading that older published copy). `bootstrap_temporal_namespace_to_sister` is no longer called from production.
5. **The completion gate is a LAYOUT predicate, not physical absence.** `repo_temporal_dirs_fully_consolidated(index_path)` (`completion_gate.py`) replaces `repo_has_zero_residual_temporal_dirs`: in-place migration legitimately KEEPS the directory, so absence would be unreachable and the AC10 snapshot could never fire again. Every parseable temporal shard must pass `verify_collection_fully_migrated`. Skipped: the bare `code-indexer-temporal` bookkeeping name and unparseable names. A metadata-less directory is skipped when rowless (Story #1458 AC1a's "empty artifact", which nothing can consolidate) but FAILS the gate when it still holds `vector_*.json` rows (an un-migratable anomaly must never be silently reported as done).
6. **A metadata-less temporal directory is NEVER a migration target** (`enumerate_migration_targets`). `TemporalIndexer` creates one shared bookkeeping directory per embedder (`code-indexer-temporal-{slug}`, the quarter-less shape) with no `collection_meta.json`; the engine has no metadata file to record the authoritative `vector_count` in, so migrating it always failed. It is skipped (or reported through the existing anomaly channel if it holds data) exactly like the bare `code-indexer-temporal` name. This ALSO fixed `--migrate-chunks-to-sqlite`, which failed on it for every real temporal repo.
7. **A NATIVELY-built `chunks.db` collection is fully consolidated** (`collection_migration.py`, `_is_natively_built_chunks_db`). The content-integrity manifest is written ONLY by the migration engine, purely to make legacy-file DELETION safe; a collection built in the consolidated layout from the start (every server-provisioned collection since Story #1488, and now every temporal collection) has no legacy files and so never gets one. Before this, `verify_collection_fully_migrated` reported such a collection as unmigrated and `consolidate_collection_in_place` then raised `UnrecoverableConsolidationCorruptionError` over it — breaking fleet migration for server repos. All FOUR conditions are required: discriminator committed, `chunks.db` present, NO manifest, and ZERO `vector_*.json` anywhere, PLUS no top-level authoritative `vector_count` (only the migration engine writes that, so its presence proves a migration ran and an absent manifest means the manifest was LOST — still a loud failure). The early return also proves store integrity via `_check_integrity_fresh_connection`, so a corrupt native store is never accepted as consolidated.
8. **The CHUNKS_DB write path owes temporal the same side effects as the legacy path.** Two were missing and are now in `_upsert_points_chunks_db`: (a) the temporal METADATA batch (`save_metadata_batch` + `checkpoint_wal`, flush-AFTER-success ordering per Bug #1206) — a SEPARATE store (`temporal_metadata.db` solo / PostgreSQL cluster) that `reconcile_temporal_index`, the incremental gate and at-commit scoping all read; and (b) the temporal SKIP of `git ls-tree` blob-hash/uncommitted lookups (FIX 1 — Errno 7 argument-list overflow on large temporal indexes). Any future write-path side effect added to one branch MUST be added to both.
9. **The DAEMON refuses rather than migrating.** `daemon/service.py` has its OWN temporal branch (Bug #473's "check temporal FIRST"), so it never reaches the CLI branch's pre-index consolidation. It consults the SAME authority — the now-public `find_pending_legacy_temporal_shards(index_dir)` — and, if any shard is pending, returns `legacy_temporal_refusal_response(...)` instead of indexing. Migrating inside an RPC is deliberately NOT offered: consolidation needs the repo's exclusive index-mutation lock and can run for hours. The response MUST use the `cli_daemon_delegation` client contract — `status="error"` plus `message` — a real daemon run proved a `{"status": "failed", "error": ...}` dict renders as "Unexpected status: failed / Message:" with the reason SILENTLY DROPPED.

E2E-proven with the real CLI and real VoyageAI embeddings: a first-ever `cidx index --index-commits` produced ZERO `vector_*.json` files and a `chunks.db` per quarter shard; an incremental run added a new quarter with zero JSON; a repo built in the legacy layout had both shards migrated in place on its next incremental run (6 JSON files -> 0) and `cidx query --time-range-all` returned rows from both migrated and natively-built shards. Daemon mode was verified the same way against a real running daemon: an incremental temporal index over legacy shards refused with the actionable message and exit code 1 while adding ZERO files, `cidx index --migrate-chunks-to-sqlite` then consolidated both shards in place, and the next daemon-mode temporal index completed normally with still zero `vector_*.json` files.

### Server-Context Temporal Data Lives at a Fixed Path Outside the Repo Tree (Bug #1529, partial)

Bug #1529 replaces Story #1457's versioned-snapshot + alias-pointer + resolver design for temporal data placement. Read the issue for the locked spec; the binding rules that now hold:

1. **ONE module decides the location**: `services/temporal/temporal_server_paths.py`. `server_temporal_index_root(golden_repos_dir, repo_alias)` is deterministic and FIXED FOREVER from first creation -- no versioning, no alias pointers, no resolver indirection. That fixed-path property is also why the path-derived temporal metadata key (`sha256(str(collection_path))`) can never go stale here: there is no relocation for a re-keying migration to follow. `resolve_temporal_index_dir(codebase_dir)` is THE seam, and its contract is THREE-way, not two: NO server context -> the in-repo path, byte-identical to before (the standalone CLI, unchanged); server context (`CIDX_SERVER_REFRESH_CONTEXT`, all storage modes) + a structurally recognized golden repo clone -> the fixed root; server context + an UNRECOGNIZED layout -> **raises `ValueError`**. That last case must never fall back to the in-repo path: the read side fails closed on the identical failure, so falling back here would let writes land in-repo while reads consult the fixed root -- silently recreating the staleness/duplication defect this bug exists to close. Only `{golden_repos_dir}/<alias>` and `{golden_repos_dir}/.versioned/<alias>/v_*` are recognized, and those are the only shapes the server provisions (`_legacy.py`'s `golden-repos/repos/<alias>` probe is a defensive guess at a layout nothing in this repo creates). The canonical definition of `CIDX_SERVER_REFRESH_CONTEXT_ENV` lives in this dependency-free module and `temporal_child_wiring.py` must import it FROM here, never the reverse (Bug #1468 import-budget class).
2. **Physical layout**: `{golden_repos_dir}/.temporal/{bare_alias}/code-indexer-temporal-{embedder}-{quarter}/chunks.db`. The per-repo root is used as an ordinary `FilesystemVectorStore` index root, so collection naming, shard discovery, HNSW, projection matrices, progress metadata and reconcile all work unchanged. Dot-prefixed, a sibling of the `.versioned/` convention, so nothing listing `golden_repos_dir` mistakes a temporal root for a repo clone. (This nests one level deeper than the issue's illustrative example -- a deliberate, documented deviation; see the module docstring.)
3. **Write side**: `cli.py`'s `--index-commits` branch resolves `index_dir` through the seam, so the WHOLE temporal branch (clear/migrate/consolidate/index/reconcile) operates on one location. Standalone CLI unchanged. AST-guarded by `tests/unit/services/temporal/test_temporal_write_side_sister_path_1529.py` -- if that call regresses to an inline in-repo path the mechanism goes inert and activations silently carry temporal data again.
4. **Read side**: the location must be applied at BACKEND-CONSTRUCTION time, because that is the only point at which the store that actually performs the search can be rooted there. `FilesystemBackend`/`BackendFactory` take an additive `index_dir` override (None everywhere else = byte-identical); `reconstruct_temporal_backend` takes `temporal_index_dir`, overriding BOTH the store root and the returned `index_path` so discovery and reads cannot diverge. BOTH read seams are wired: `SemanticQueryManager._execute_temporal_query` (via `_resolve_temporal_index_dir(golden_repo_alias)`) and `server/services/temporal_worker.py`'s `run_temporal_worker` (via `_resolve_golden_temporal_context`, which resolves golden lineage BEFORE reconstructing). The worker is the LIVE MCP temporal front door (Story #1400 replaced `_execute_temporal_query` for that path) -- fixing only the REST seam leaves the primary read path half-wired, which is exactly the failure mode #1529 was filed for. Both read seams fail CLOSED, deliberately: once a golden alias IS known, the fixed root is the ONLY correct location for that repo's temporal data, so a failure deriving it RAISES rather than quietly falling back to `repo_path` (the activation's own CoW clone -- the frozen duplicate this bug exists to eliminate). Returning None is reserved for the ONE legitimate case: no golden lineage is known at all (an explicit-repo_path query shape), which then uses the legacy in-repo derivation unchanged. Failing a temporal query loudly beats answering it from knowingly-wrong data.
5. **`TemporalShardResolver` is retired from both read seams.** Never re-attach it to a temporal query's `vector_store`. A shard's path no longer moves, so there is nothing to resolve and no second location that could disagree.
6. **Activated repos never own temporal data.** The read path derives the location from the GOLDEN alias, never from `repo_path` (an activation's CoW clone). Story #1457 AC12's write-side exclusion stays as-is.

7. **Concurrency: NO new mechanism -- the existing primitives already provide it.** A fixed path means refreshes write IN PLACE, so a reader must never observe a torn shard. That is already guaranteed by the two primitives this codebase uses everywhere: `ChunkStore` writes through SQLite transactions (`INSERT OR REPLACE`, committed as a unit -- the mutable connection uses `journal_mode=DELETE`, not WAL, but transaction atomicity is the same either way), and `HNSWIndexManager` persists the graph via its existing temp-file + atomic-rename pattern. Temporal indexing therefore writes directly at the fixed path through the ordinary `begin_indexing` -> `upsert_points` -> `end_indexing` sequence -- **do not add a reflink-scratch/swap layer on top**. One was built (`services/temporal/temporal_atomic_refresh.py`) and deliberately deleted in commit `614a3b37`: it would have been unwired orphan code (Messi #12), and wiring it would have required building each shard under a scratch PATH, which reintroduces exactly the path-derived-key staleness the fixed-path design exists to eliminate (`TemporalMetadataStore.collection_key = sha256(str(collection_path))`, `temporal_metadata_store.py:118`). That module NO LONGER EXISTS -- if you find it, you are looking at a stale `__pycache__`/`.mypy_cache` artifact from before the deletion (this cost one full review round), not live code; `git log --diff-filter=AD -- <path>` settles it. Note also the scope the locked spec actually requires: "a reader must never observe a partially-written `chunks.db`" -- torn-read prevention WITHIN each artifact, NOT one transaction spanning chunks.db + HNSW + metadata. A reader seeing new rows before the graph includes them gets an incomplete-but-correct result, which is normal additive-index behavior and is asserted as acceptable by the concurrency test below. Changing that to a stable logical identity is real, separate work -- not a wiring detail. Proven by `tests/unit/services/temporal/test_temporal_inplace_refresh_concurrency_1529.py`: across 12 consecutive real incremental refreshes, a chunk-store reader and an HNSW `search()` reader both hammer the live directory and never raise, never see a phantom row, and never lose an already-committed row. A mid-rebuild search legitimately returning FEWER results is accepted additive-update behavior (incomplete, never incorrect), not a defect.

8. **Read-side cache freshness is now the ONLY cache-bust signal (Bug #1538).** The retired versioned/pointer design changed a shard's PATH on every publish, so the per-worker, path-keyed `HNSWIndexCache` was invalidated for free. A fixed path removes that side effect: nothing about the key changes when the file's CONTENT does, and each uvicorn worker (on each node) holds its own entry. Bug #1538 measured the consequence on the 3-node cluster -- ~58% of post-refresh temporal queries served the pre-refresh graph, still ~58% twelve minutes later, never self-healing. Two independent defects, both fixed:
   - **`HNSWIndexCache` stamped the entry AFTER the load** (`hnsw_index_cache.py`). A refresh that atomically replaced `hnsw_index.bin` between the loader's read and that stat produced an entry holding the OLD graph stamped with the NEW file's identity -- every later HIT compared equal, so the entry was POISONED PERMANENTLY. The fingerprint (`_stat_index_fingerprint`; see the next bullet for its exact fields) is now captured BEFORE `loader()` runs: conservative in the safe direction (a change during the load costs at most one extra reload, never a stale answer) and still a plain HIT with no spurious reload when the file is untouched. Comparison lives in `_apply_freshness_verdict` -- INEQUALITY of file identity, deliberately NOT "mtime strictly newer", since a same-tick rewrite, an NFS clock skew or a restored shard all change content without advancing the clock. Always `st_mtime_ns`, never the lossy float `st_mtime`, matching `CollectionMetaCache`/`ChunkStoreThreadCache`.
   - **The temporal dispatch's post-shard eviction targeted a key that never existed.** `_query_shards_raw`'s `finally` block passed a bare `str((base_path / shard_name).resolve())`, but `search()` stores under `_activation_scoped_cache_key(...)`, which embeds Story #1458 AC11's chunk-layout token -- so every temporal eviction had been a silent no-op since AC11 landed, and the MemoryGovernor's evict decision did nothing. Eviction now goes through `evict_shard_hnsw_entry(vector_store, shard_name)` (`temporal_fusion_dispatch.py`), which composes the key via the new `FilesystemVectorStore.hnsw_cache_key_for_collection(collection_path)`. **Any code that invalidates a shared HNSW/id_index entry MUST use that method** -- hand-building the key is how this bug happened twice (AC11 fixed it for `rebuild_hnsw_filtered`, missed this call site).
   - **The fingerprint must be an IDENTITY, not a timestamp+size.** `(st_mtime_ns, st_size)` alone leaves this bug reachable through a narrower window: a refresh may rebuild a shard to the same item count (same file size) with different content, and the timestamps can compare equal too (coarse server-side mtime granularity, a same-tick rewrite). The fingerprint is therefore `(st_mtime_ns, st_size, st_ino, st_dev)`. The inode is what makes detection EXACT rather than probabilistic: every `hnsw_index.bin` publish in this codebase is an atomic rename over the live path (`BackgroundIndexRebuilder.atomic_swap` plus `HNSWIndexManager`'s two `os.replace` sites), which always installs a different inode; `st_dev` qualifies it because inode numbers are only unique within a filesystem. All four fields come from the SAME single `os.stat()`, so this costs nothing extra. A content digest was rejected -- hashing a multi-megabyte graph on every query-path cache HIT is far too expensive for a question the inode answers exactly. If a future write path ever mutates `hnsw_index.bin` IN PLACE (same inode) this reasoning weakens back to time+size, so keep publishing via atomic rename.
   - **The freshness stat MUST NOT run under `_cache_lock`, and MUST be rate-limited.** This mount is `hard` NFSv3, so an outage makes `os.stat()` block in uninterruptible kernel retry rather than fail. Running that under the shared lock would let ONE hung stat stall every other cache operation in the worker -- an availability regression the freshness mechanism itself would have introduced. Three properties, all required: (a) `_verify_entry_freshness` performs the stat with NO lock held, then re-acquires it only to apply the verdict, re-confirming via `self._cache.get(repo_path) is not entry` that the entry was not replaced mid-stat; (b) `_freshness_checking` is a per-key in-flight guard (the same idea as the existing `_loading` sentinel, applied to the CHECK) so at most ONE thread per key is ever inside a blocking stat while others serve what they have; (c) `_FRESHNESS_RECHECK_MIN_INTERVAL_SECONDS` (2.0s) bounds how often the check itself runs per key. That interval is a rate limit on the CHECK, NOT a staleness tolerance and NOT a TTL -- and a freshly loaded entry (`freshness_checked_at is None`) is ALWAYS checked on its first HIT, so a refresh landing during the load is still caught immediately.
   - **An unverifiable freshness check SERVES the last verified graph, bounded and observable (Messi #13).** When the stat cannot be performed, `_apply_freshness_verdict` keeps the entry, emits ONE WARNING per degradation (`freshness_unverified_reported`, re-armed as soon as a check succeeds), and retries no sooner than the interval above. Dropping the entry instead was implemented and then REJECTED under review: with a broken stat and a working loader it reloaded on EVERY hit for as long as the stat stayed broken (reproduced: 5 hits produced 6 loads, never settling), and it cannot produce fresher data anyway -- a file that cannot be stat'd cannot be read by the loader either, so the thrash costs a full index load per query and buys nothing. "At most one extra load" is only true when the stat SUCCEEDS; do not restate it unconditionally. A distinct case is deliberately kept separate: an entry whose PRE-load stat failed (`index_file_fingerprint is None`) while the stat now succeeds IS dropped, because one reload re-stamps it permanently, whereas serving it would retain an entry that can never be verified for as long as it lives. `test_missing_index_file_does_not_crash_on_hit` (`test_hnsw_negative_cache_evo64244.py`) therefore keeps its ORIGINAL `calls == 1` expectation; only its rationale changed.
   - **Guarantee, stated precisely (and its UNVERIFIED residual)**: self-healing on the NEXT read, per worker and per node, with NO cross-process signalling -- the on-disk file is the shared source of truth, so no pub/sub, generation counter or cache epoch was added (and none exists in this codebase for this purpose). A TTL-only mitigation was rejected: it bounds the wrong answer instead of eliminating it. On a coherent filesystem this is ZERO staleness, proven by the tests. On the NFS-shared `golden-repos` mount the honest statement is narrower, and deliberately does NOT claim a number: correctness depends on when the CLIENT revalidates the file's attributes, and **that window has never been measured on the deployed cluster**. What IS verified from this repo: that mount is NFSv3 `vers=3,nolock,hard` (`docs/cluster-setup.md`, `scripts/install-cidx-server.sh`), NO `actimeo`/`acregmin`/`acregmax` is pinned on it anywhere, so the timeouts are whatever the client kernel defaults are -- an assumption, not a verified deployment fact. Attribute-cache staleness is a REAL observed symptom on this storage, not a theoretical one (`docs/cow-storage-setup.md` lists "Clones not visible / NFS attribute cache / run `ls`, or mount with `actimeo=1`"). Separately, `hard` means an NFS server outage makes `os.stat` BLOCK on uninterruptible retry rather than fail -- a hang, a different failure mode from stale data, and also why the unverifiable-drop path above is unlikely to fire spuriously there. Bottom line: the indefinite, never-self-healing staleness #1538 reported is eliminated by construction, but the NFS residual is bounded by an unmeasured client-revalidation window. **Closing #1538 requires re-running its own 15-query cross-node probe on the real 3-node cluster after a refresh** -- that was NOT done (no cluster access from the fix's worktree) and is the one remaining gate.

**Additional bug fixed en route**: `temporal_shard_has_committed_rows()` (`temporal_row_existence.py`) was `vector_*.json`-ONLY, so once Bug #1528 made temporal write `chunks.db` it reported "no data" for every populated temporal shard. `golden_repo_manager`'s `_temporal_vectors_exist_for_repo()` uses it to decide whether an explicit temporal add-indexes/reindex must pass `--clear`, so the false negative forced a FULL re-embed of an entire git history (real embedding spend) on every such run. Now layout-dispatched via `resolve_chunk_layout` + the read-only, never-creating `chunk_store_has_real_data`.

**NOT YET DONE (do not assume otherwise)**: no live local/staging verification has been run, and the four registered temporal-enabled golden repos on the local server (`cur-temporal`, `f-temporal-e2e`, `click`, `mock-test`) have NOT been relocated. That relocation is a plain `mv` of each repo's in-repo `code-indexer-temporal-*` directories into `{golden_repos_dir}/.temporal/{alias}/`, and it MUST happen only AFTER this code is deployed -- the running dev server tracks `development`, knows nothing about `.temporal/`, and would immediately stop finding temporal data for all four if the move landed first. Correct order: merge -> deploy -> relocate -> verify each repo's `--time-range-all` query through the REST front door. `evolution` is not registered here and must never be re-indexed (memory: `feedback_never_reindex_evolution.md`). **Rollout note**: an old node that predates this change reads the in-repo location, so a mixed fleet can disagree about where temporal data lives. The primitive to resurrect if that ever needs coordinating is the removed sister-relocation enable flag plus its `temporal_reader_capability` fleet-version gate (deliberately NOT naming the retired env var here -- `tests/unit/scripts/test_temporal_docs_stale_refs_1292.py` fails the build on any `CIDX_TEMPORAL_*` env-var name appearing in docs, since such a name in prose implies a knob that still exists; recover the exact identifier from this bug's own commit history instead).

### Story #1457 Sister-Location Temporal Placement -- RETIRED (superseded by Bug #1528 + Bug #1529)

Story #1457 ("Temporal Consolidation to a Sister Location + Activated-Repo Query Fix") built a versioned-snapshot + alias-pointer + resolver mechanism to place temporal data outside the golden repo's clone. BOTH halves of it are now retired: the WRITE half by Bug #1528 (temporal writes the consolidated `chunks.db` layout in place), the READ half by Bug #1529 (a fixed, deterministic path per `(alias, embedder, quarter)` -- see "Server-Context Temporal Data Lives at a Fixed Path Outside the Repo Tree" above, which is the ONLY description of how temporal placement works today).

Do NOT resurrect any of it. The retired surface, named here purely so a future agent recognizes it as dead rather than missing: `TemporalShardResolver` (+ its `pin()` / `TemporalShardPinExhaustedError` resolution-scope machinery), `maybe_relocate_shard_to_sister_location`, `temporal_refresh_dispatch` / `temporal_consolidated_build` / `temporal_shard_publisher`, `build_dedicated_temporal_read_store`, `bootstrap_temporal_namespace_to_sister`, and the DB-backed sister-relocation enable flag. Neither temporal read seam constructs a resolver, and no production write path calls the relocation trigger. Re-wiring the trigger specifically is UNSAFE, not merely redundant: it sources rows only from `vector_*.json`, so against a `chunks.db` shard it publishes an EMPTY version and swaps the pointer onto it -- silent data loss on read (`temporal_indexer.py` documents this at the call site it was removed from).

Alias pointers written while that mechanism was briefly enabled may still exist on some installations; they are inert. Reading pre-#1529 relocated data is not supported by any current code path -- the fixed-path root is the only location READ.

One deliberate exception to "nothing touches those pointers", so it is not mistaken for a live read path: `discover_and_enforce_temporal_retention` (`global_repos/snapshot_retention.py`, called from `refresh_scheduler.py` on every refresh) still globs `{alias}-temporal-*.json` alias files and prunes superseded versioned directories behind them. It is a pure DELETION sweep -- it never reads, serves, or resolves temporal data, and on any installation that never ran the retired mechanism the glob simply matches nothing. It was left in place rather than removed because on an installation that DOES have those leftovers it is the only thing still reclaiming that dead disk space; removing it (function + live refresh-path call site + two test modules) is a change to this project's most consequential scheduler and belongs in its own change, not bundled into a bug fix.

Three pieces of SHARED infrastructure hardening from that story are NOT retired and remain live, since they are unrelated to temporal placement:

- **Collision-safe version-id generation**: `VersionedSnapshotManager._create_cow_snapshot` and `GoldenRepoManager._cb_cow_snapshot`'s standalone-CLI fallback both check that a computed `v_{timestamp}` destination does not already exist and retry with an incremented timestamp on collision, bounded by `_MAX_VERSION_ID_COLLISION_RETRIES = 100` in each module. Prevents a same-second `create_snapshot` from nesting `cp --reflink=auto -a` inside an existing directory (a structurally corrupt snapshot).
- **`create_alias` + `swap_alias` parent-directory fsync**: both `AliasManager` publication primitives (`global_repos/alias_manager.py`) fsync the parent aliases directory immediately after `os.replace`. An atomic rename is not a durable one.
- **`CleanupManager` minimum-retention-age floor**: deletion requires BOTH the refcount-zero gate AND `time.monotonic() - scheduled_at >= min_retention_age_seconds` (constructor param, default `MIN_RETENTION_AGE_SECONDS = 900.0`). Tests needing immediate deletion must pass `min_retention_age_seconds=0.0`. Closes the cross-process residual a process-local `QueryTracker` cannot see.

Also still live, and load-bearing for Bug #1529: the **`CIDX_SERVER_REFRESH_CONTEXT` child-env marker**. `build_temporal_child_env` (`server/storage/postgres/temporal_child_wiring.py`) ALWAYS returns a dict and sets it in every storage mode -- a solo/SQLite server is genuinely server context, and an earlier postgres-only shape silently dropped the signal there. All five server-side temporal-child spawn sites route through that one builder, enforced by an AST guard test (`test_temporal_child_env_spawn_site_guard_1457.py`). Its canonical definition lives in `temporal_server_paths.py`; `temporal_child_wiring.py` imports it from there, never the reverse (import-budget, Bug #1468).

**AC5 fix — `_temporal_vectors_exist` layout-agnostic detection**: `golden_repo_manager.py`'s `_temporal_vectors_exist_for_repo(index_dir)` replaces the confirmed-buggy inline `_coll_dir.glob("vector_*.json")` (non-recursive, can never match the real 4-level hash-sharded layout, so it was always `False` and always forced `--clear`/a full re-embed on every explicit temporal add-indexes/reindex). The fix scans every `code-indexer-temporal*` directory via `temporal_shard_has_committed_rows()`. Bug #1529 then superseded the story's own "pointer-first against the resolver catalog" follow-up (that catalog no longer exists) and completed the check differently: `temporal_reindex_needs_clear` composes this in-repo name-glob scan with `get_temporal_repo_status`, which covers BOTH physical roots. Both halves run with `on_error="raise"`, because for THIS decision "no data" authorizes destruction.

**AC12 — activated-repo reindex excludes/rejects temporal**: `ActivatedRepoIndexManager.trigger_reindex` (`server/services/activated_repo_index_manager.py`, used EXCLUSIVELY for activated/non-golden repos) now raises `ValueError` immediately when `"temporal"` appears in the requested `index_types` — never a silent no-op — since temporal data is owned exclusively by the golden repo (today: its fixed `.temporal/{alias}/` root, Bug #1529) and an activated repo has no business re-deriving or duplicating it (this was previously live-permitted and actually dispatched to `_execute_temporal_indexing`). `run_branch_delta_index`'s existing semantic-only call graph (only `_execute_semantic_indexing`, never `_execute_temporal_indexing`, on activation/branch-switch/sync) is now locked in as INTENTIONAL by an AST-based structural regression test (`tests/unit/server/services/test_activated_repo_index_manager_branch_delta_semantic_only_1457.py`, parses the real method source via `ast`/`inspect`) rather than remaining an accidental omission a future refactor could silently "complete". This is the WRITE-side fix; the matching READ-side fix (routing activated/`-global` queries to the golden repo's own temporal location instead of the activation's CoW clone) shipped in Bug #1529 -- see that section, not this one.

**AC7 — temporal reconciliation rewritten against the consolidated SQLite chunk store**: `temporal_reconciliation.py`'s `reconcile_shard` now dispatches on `resolve_chunk_layout(shard_dir)` (Story #1456's established dual-mode pattern) — the pre-existing SHARDED_JSON logic (`IDIndexManager().rebuild_from_vectors`) is preserved byte-identically as `_reconcile_shard_legacy`; a new `_reconcile_shard_chunks_db` reads point-id existence via `ChunkStore.all_point_ids()` (the consolidated store's primary key) instead of the retired binary index. Stray-point deletion for the CHUNKS_DB path uses a new, purely-additive `ChunkStore.delete_stray_points_fail_closed(point_ids)` method (`storage/sqlite_chunk_store.py`) — a SINGLE transaction, `PRAGMA synchronous=FULL` for a durable commit, explicit rollback on ANY failure (never partial deletion), re-raising the original exception so `reconcile_shard` can translate it into the existing `StrayDeleteFailedError` — preserving the SAME transactional-delete + fail-closed + durable-fsync safety contract the legacy path already had. `id_index.bin` is not produced for CHUNKS_DB temporal shards, matching Story #1456's semantic-collection retirement (same reason, not a semantic/temporal distinction). Note: `sqlite3.Connection` cannot be mocked or monkeypatched (it is a read-only C extension type — verified empirically) — the rollback/fail-closed tests use two genuinely REAL fault-injection mechanisms instead: an OS-level read-only file permission (forces a "before commit" DELETE-statement failure) and a second connection holding an open read transaction (a SHARED lock blocks only the EXCLUSIVE lock commit needs, not the RESERVED lock a DELETE needs — forcing a "during commit" failure specifically).

### Fleet Migration (Story #1458, Epic #1454)

Story #1458 ("Fleet Migration") is a large, multi-AC story. This pass implements and tests the CORE consolidation engine, temporal-bootstrap completion, per-repo orchestration sequence (AC1/AC1a/AC2/AC3/AC4/AC6/AC7/AC8/AC9/AC10/AC12), AC11 (FSV cache-key discriminator + activation_id tokens), AC13 (deactivation refcount-aware drain), and dashboard/`BackgroundJobManager` wiring (a real scheduler, not just the orchestrator function), all with real filesystem/SQLite/JobTracker-DB-proven infrastructure. **NOT YET DONE**: a bespoke admin REST/MCP trigger+stats endpoint (the scheduler's `get_stats()`/`trigger_now()` exist and are tested, but nothing HTTP-exposes them yet — dashboard *visibility* of jobs it submits works today via the generic mechanism described below, independent of that gap), and a live E2E run against a real staging repo (Testing Requirements' unchecked E2E items).

**Canonical discriminator schema correction (AC3 Finding 4)**: see the "Chunk Storage Layout" section above — Story #1456's originally pulled-forward `{"enabled": true, "version": 1}` shape is now formally superseded by this story's canonical `{"version": <int>}` contract; the resolver's validity rule was corrected accordingly (backward-compatible: old writes with an extra `enabled` key still validate purely on `version`).

**Per-collection consolidation engine (AC3/AC4/AC6)** — `src/code_indexer/storage/shared/collection_migration.py`'s `consolidate_collection_in_place(collection_dir)`: (1) reads the legacy sharded `vector_*.json` files via `IDIndexManager.scan_vectors_for_id_map()` (Story #1458 AC3 step 1 -- a NEW side-effect-free scan primitive extracted from `rebuild_from_vectors()`'s scan loop, which `rebuild_from_vectors()` now delegates to; the full `rebuild_from_vectors()` must NEVER be called here, since it atomically writes `id_index.bin` as a side effect and would silently recreate the file Story #1456 retires); (2) writes a new `chunks.db` via `ChunkStore` as a pure addition (a `os.statvfs`-based disk-headroom preflight skips consolidation with a logged ERROR, leaving the collection untouched, if free space looks insufficient -- fails OPEN on a `statvfs` failure itself, never blocking a legitimate migration on a broken advisory check); (3) read-back verifies every record field-for-field (id, vector as float32, and every other passthrough field including the `chunk_text`-vs-`git_blob_hash` content-variant distinction) BEFORE anything is deleted or flagged, raising `ConsolidationVerificationError` on any mismatch; (4) durably flips the `chunks_db` discriminator via the existing `write_chunks_db_discriminator()`; (5) deletes the old `vector_*.json` files individually, removes now-empty hash-shard subdirectories bottom-up, and unlinks the retired `id_index.bin` -- NEVER the collection root. Crash-safety (AC4) is discriminator-driven and requires no special-casing: `resolve_chunk_layout()` already resolves `CHUNKS_DB` only after the flip, so a re-invocation before the flip cleanly restarts from step 1 (a pure-addition rewrite is always safe/idempotent, `ChunkStore.write_batch` is `INSERT OR REPLACE`), and a re-invocation after the flip skips straight to step-5 cleanup, never re-doing the write/verify. **E2E-proven against the real CLI**: a real `cidx init`/`cidx index` run (real VoyageAI embeddings) followed by a real `consolidate_collection_in_place()` call produced byte-identical `cidx query` results (same scores, same ranking) before and after, plus idempotent no-op resume on a second call.

**Temporal bootstrap-to-sister (AC1a) — RETIRED.** `bootstrap_temporal_namespace_to_sister` and the publish/relocate primitives it composed belong to the retired Story #1457 mechanism (see its section above) and are no longer called from production: Bug #1528 rule 4 routes every temporal namespace through the SAME in-place `_consolidate_collections` helper semantic collections use.

**Completion gate (AC1/AC10)** — `src/code_indexer/server/services/fleet_migration/completion_gate.py`. The original `repo_has_zero_residual_temporal_dirs` physical-absence predicate is GONE: in-place migration legitimately keeps the directory, so absence became unreachable (Bug #1528 rule 5). The live predicate is `repo_temporal_dirs_fully_consolidated(index_path)`, a LAYOUT check -- every parseable temporal shard must pass `verify_collection_fully_migrated`, and both the metadata-less case and the bare `code-indexer-temporal` name are classified by ROW PRESENCE (rowless artifact/bookkeeping directory: skip; rows still in the legacy layout: fail, Bug #1529 item 5).

**AC10 post-consolidation snapshot trigger** — `src/code_indexer/server/services/fleet_migration/snapshot_trigger.py`'s `trigger_post_consolidation_snapshot(refresh_scheduler, alias, source_path)` calls the LOW-LEVEL publication primitives DIRECTLY on the REAL `RefreshScheduler` instance migration already holds (`_snapshot_manager.create_snapshot()` + `alias_manager.swap_alias()`/`create_alias()`), NEVER the scheduler-level `trigger_refresh_for_repo()`/`_execute_refresh()` wrapper -- that wrapper self-skips on a held write lock and would never publish while migration is itself the lock holder. Retention is reused, not reimplemented: `refresh_scheduler._enforce_retention(alias_name, new_target)` is invoked immediately after the swap, identical to the scheduler's own post-swap call.

**Per-repo orchestrator (AC1/AC2/AC7/AC8/AC9)** — `src/code_indexer/server/services/fleet_migration/orchestrator.py`'s `run_fleet_migration_for_repo(...)`: acquires `refresh_scheduler.write_lock_manager` directly (bypassing `acquire_write_lock()`'s hardcoded 3600s-default wrapper) with an explicit, justified `MIGRATION_LOCK_TTL_SECONDS = 24h` (AC8 approach (a) -- proven by a test that backdates `acquired_at` 2h past the base 3600s default and confirms a concurrent acquire is still refused); then calls `refresh_scheduler.check_refresh_not_in_progress(bare_alias)` (AC9 -- closes the write-lock-alone TOCTOU gap against an ALREADY-running refresh, via the existing `JobTracker`-backed primitive) BEFORE touching the base clone, releasing the lock and returning `"refresh_in_flight"` immediately (never an in-call blocking/polling wait) if one is found; then runs semantic consolidation (AC3) THEN temporal bootstrap (AC1a) in that order, firing the AC10 snapshot exactly once ONLY if the completion gate passes (zero residual temporal dirs AND no collection skipped for insufficient disk); the write lock is released in `finally` on every path. **AC7 (activation-during-migration fail-fast) required zero new code** -- it is inherited "for free" because migration acquires the SAME `WriteLockManager` lock file `ActivatedRepoManager`'s existing non-blocking `acquire()` call already checks; proven by a test demonstrating that call returns `False` immediately (never blocks) while migration holds the lock. **AC5 (node-scoped crash recovery)** is designed to be satisfied by routing job submission through the EXISTING generic `JobTracker.cleanup_orphaned_jobs_on_startup()` (already `executing_node`-scoped, already called at every server startup, covers every `operation_type` uniformly) once a real `BackgroundJobManager.submit_job(...)` call site is wired -- that wiring itself (and the live discovery of which repos/collections/temporal namespaces to pass into the orchestrator) is the disclosed remaining gap below.

**AC11 (FSV cache-key structural miss across consolidation AND reactivation)** — `src/code_indexer/storage/filesystem_vector_store.py`'s `_activation_scoped_cache_key(path_str, *, chunk_layout_token=None)` composes the shared HNSW/id_index cache key as `path[:chunk_layout_token][:activation_id]` (either/both suffix optional, byte-identical to the bare path when neither is set — the CLI/solo default). Two INDEPENDENT tokens, both required (the discriminator alone is necessary but not sufficient — Finding 7): (1) `chunk_layout_token` is `resolve_chunk_layout(collection_path).value` ("sharded_json"/"chunks_db"), threaded through `search()`'s ALREADY-fresh-per-call `_search_chunk_layout` (Story #1456 AC7's own uncached resolution) into both the HNSW `get_or_load()` key and the id_index `get_or_load()` key, so a post-consolidation read at the SAME path is a structural cache-miss; `rebuild_hnsw_filtered()`'s two `invalidate()` calls re-derive the SAME token via a fresh `resolve_chunk_layout()` call (NOT the in-memory `_is_chunks_db_collection` override, which can diverge during a fresh-build-in-progress window) so eviction targets the exact stored key, never silently no-opping. (2) `activation_id` is a UUID generated ONCE per activation (`ActivatedRepoManager`, both the composite and single clone-materialization sites, alongside the existing `activated_at` timestamp — `activated_at`'s one-second resolution was explicitly REJECTED as a generation token, collision-prone within one clock tick), persisted in BOTH the JSON-file and PostgreSQL metadata backends (migration `039_activated_repos_activation_id.sql`, `ALTER TABLE ADD COLUMN IF NOT EXISTS`), read via `ActivatedRepoManager.get_activation_id(username, user_alias)`, and threaded through `BackendFactory.create(...)` → `FilesystemBackend` → `FSV.__init__` → every server-side query call site (`search_service.py`, `semantic_query_manager.py`'s `query_user_repositories()` dispatch — `None` for global/explicit-path branches, looked up for the activated-repo branch) — closing the deactivate-then-reactivate hazard (same path, potentially identical discriminator value, genuinely different clone).

**AC13 (deactivation bounded refcount-aware drain)** — `src/code_indexer/server/services/deactivation_query_drain.py`'s `wait_for_activated_repo_query_drain(query_tracker, refcount_key, *, max_wait_seconds=None)`: bounded wait-then-proceed (config-sourced `deactivation_query_drain_max_wait_seconds`, default 30s, Web-UI-configurable; `None` tracker or non-positive bound is a fail-open no-op), logging a WARNING and proceeding on expiry rather than blocking deactivation indefinitely — the SAME accepted bounded/graceful residual Story #1457 AC14 documents. Called in `ActivatedRepoManager._do_deactivate_single`/`_do_deactivate_composite` with the key captured BEFORE the `.trash/` rename (QueryTracker does EXACT-STRING matching, no path normalization — polling the post-rename path would always observe 0 and protect nobody). **Prerequisite wiring** (the refcount had nothing to observe without this): `server/mcp/handlers/search.py`'s `_search_activated_repo()` now wraps `query_user_repositories(...)` in `query_tracker.increment_ref`/`decrement_ref` (try/finally) under the SAME `os.path.join(activated_repos_dir, username, user_alias)` key format the drain polls — previously the ONLY refcount call site was the `-global`/golden-repo branch. `ActivatedRepoManager.set_query_tracker(...)` + `lifespan.py`'s `_wire_query_tracker_into_semantic_query_manager()` extension complete the chain from `app.state.query_tracker` down to both the read (increment/decrement) and write (drain) sides.

**Dashboard/`BackgroundJobManager` wiring** — the missing link between the real, tested `run_fleet_migration_for_repo(...)` orchestrator and a live scheduling/admin surface, closing the CLAUDE.md-mandated background-jobs checklist:
- **Discovery** (`server/services/fleet_migration/discovery.py`) — `enumerate_fleet_migration_candidates(golden_repo_manager)` reuses `list_golden_repos()`/`get_actual_repo_path()` (the SAME primitives `hnsw_orphan_sweep/discovery.py` reuses) to resolve each golden repo's base clone, then walks `.code-indexer/index/` to classify each entry as a semantic collection (has `collection_meta.json`) or a temporal namespace (`code-indexer-temporal-*` prefix, parsed via `parse_physical_temporal_name` and the SAME `pointer_namespace` formula Story #1457's own production `maybe_relocate_shard_to_sister_location` trigger uses — `sister_root`/`sister_alias_manager` are likewise byte-identical constructions: the base clone's own parent dir, `AliasManager(golden_repos_dir/"aliases")`). `is_repo_already_migrated(candidate)` is derived FRESH from disk every call (zero residual temporal dirs + every semantic collection resolves to `CHUNKS_DB`) — deliberately NO separate durable-cursor table, so the scheduler cannot drift from actual on-disk truth.
- **Scheduler** (`server/services/fleet_migration/scheduler.py`) — `FleetMigrationScheduler.trigger_now()` submits ONE job per call via `background_job_manager.submit_job(...)` under a FIXED sentinel `repo_alias` (`"fleet-migration-scheduler"`, mirroring `HNSWOrphanRepairSweepScheduler`'s own `repo_alias="server"` technique) so `register_job_if_no_conflict`'s `idx_active_job_per_repo` unique index serializes the WHOLE FLEET (AC1: "one repo at a time"), not merely per-repo. Each job (`_run_next_candidate`) re-enumerates candidates, skips already-migrated repos, and runs the real orchestrator on the first pending one — deliberately ONE repo per job (unlike HNSW sweep's many-small-items-per-tick batching), since a single repo's migration can legitimately run for hours (no per-job timeout, per this project's indexing-path invariant). `get_stats()` returns live total/migrated/pending counts, derived the same fresh-from-disk way. **Dashboard visibility requires NO new route/template**: `"fleet_migration"` is not added to `dashboard_service.py`'s `_DASHBOARD_HIDDEN_OPERATION_TYPES`, so every submitted job appears in the existing generic admin recent-jobs panel automatically — the SAME already-approved pattern every other scheduler in this codebase uses.
- **Config** (`FleetMigrationConfig` in `config_manager.py`, `fleet_migration_config` on `ServerConfig`) — `enabled: bool = False` (a deliberate DIVERGENCE from `HNSWOrphanRepairSweepConfig`'s `True` default: fleet migration deletes real on-disk sharded files after a verified consolidation, so a fresh deployment must never touch a fleet repo without explicit operator opt-in via the Web UI Config Screen), `tick_interval_minutes: int = 30`. The scheduler's own `_read_cycle_config()` likewise fails CLOSED (`enabled=False`) on a config-read error — the opposite safety direction from the HNSW sweep's fail-open default, since a transient glitch here must never cause an unintended migration to start.
- **Startup wiring** (`lifespan.py`) — constructed/started right after the HNSW orphan sweep scheduler block, guarded on the already-in-scope real `refresh_scheduler` (from `global_lifecycle_manager.refresh_scheduler`), stored at `app.state.fleet_migration_scheduler`, fail-soft (try/except/log, never blocks server startup) — the identical pattern every other scheduler in this function uses.

### Fleet Rollout Safety Gate (Story #1460, Epic #1454 -- FINAL story)

SUPERSEDED IN PART (Bug #1528/#1529): every mention below of a temporal "sister location", the sister-root resolver, or `bootstrap_temporal_namespace_to_sister` describes the RETIRED Story #1457 mechanism -- read that story's section above. The semantic half of this story (the `deletion_authorized` gate on `consolidate_collection_in_place`) is unchanged and live; the temporal half's destructive primitive no longer runs in production at all, so its gate is moot rather than wrong. The operator confirmation procedure at the end of this section is likewise still current.

Story #1460 is the epic's closing safety story: it does NOT reimplement the dual-layout-aware reader (Stories #1455-#1459 already built it) -- its job is to GATE the two destructive deletion primitives Story #1458 built (`consolidate_collection_in_place`'s legacy-file cleanup, and the then-live temporal in-repo-tree reclaim) behind the operator's `fleet_migration_config.enabled` toggle, so no un-upgraded node can ever observe zero chunk files after deletion. **Dual-review correction**: the code stores NO confirmation that the reader has actually rolled out fleet-wide -- it trusts the operator's boolean toggle alone. "Confirmation" is a manual operator PROCEDURE performed BEFORE flipping the flag (documented below, closing AC3's "document the confirmation procedure" requirement), never a coded verification this gate enforces automatically.

**The gate is a `deletion_authorized` parameter, not a new config field** -- AC2's "operator-controlled migration flag, default OFF, `get_config_service()`-backed, Web-UI-configurable" requirement is satisfied by REUSING Story #1458's existing `FleetMigrationConfig.enabled` (already wired end-to-end through `config_service.py`'s `_fleet_migration_settings`/`_update_fleet_migration_setting` and `web/routes.py`'s `_VALID_CONFIG_SECTIONS` -- no new dataclass field, no new wiring mechanism, per this story's own design mandate). What Story #1458 left open was a genuine defense-in-depth gap: `FleetMigrationScheduler._is_enabled_now()` gated only whether the scheduler ever SUBMITS a migration job -- `run_fleet_migration_for_repo()` itself, and the two lower-level deletion primitives it calls, ran unconditionally once invoked, trusting the caller alone. A hypothetical future direct caller (an admin trigger, a test, a bypass of the scheduler) would have deleted unconditionally regardless of config.

**Wiring, innermost-out**:
- `consolidate_collection_in_place(collection_dir, *, deletion_authorized: bool = True)` (`storage/shared/collection_migration.py`) and `bootstrap_temporal_namespace_to_sister(..., deletion_authorized: bool = True)` (`services/temporal/temporal_bootstrap.py`) both default to `True` -- BYTE-IDENTICAL to Story #1458's original unconditional behavior for every one of that story's ~60 pre-existing mechanism tests, which call these functions without the new parameter. When `False`, the check is literally inside each file, immediately before its ONE destructive call site (`_cleanup_old_sharded_files()` in the semantic module -- both the fresh-consolidation path and the already-CHUNKS_DB resume path; `_reclaim_in_repo_tree()` in the temporal module -- gated via a shared `_reclaim_in_repo_tree_if_authorized()` wrapper across all THREE `BootstrapDisposition` branches, ALREADY_PUBLISHED/EMPTY_ARTIFACT/NEEDS_BOOTSTRAP): the non-destructive build/verify/publish work (chunks.db write+read-back-verify+discriminator flip; temporal build+publish to the sister location) still runs in full, but the destructive delete/reclaim step is withheld. Both result dataclasses (`ConsolidationResult`, `BootstrapOutcome`) gained a `deletion_gated: bool = False` field so callers can distinguish "withheld by the gate" from "genuinely nothing to clean up".
- `run_fleet_migration_for_repo(..., deletion_authorized: Optional[bool] = None)` (`server/services/fleet_migration/orchestrator.py`) resolves the gate ONLY once, immediately before `_run_migration_sequence` (never at function entry, so the `lock_held`/`refresh_in_flight`/`refused_immutable_path` early-return paths never touch the config service): an explicit `True`/`False` always wins; `None` (the default -- every real caller today) resolves from `get_config_service().get_config().fleet_migration_config.enabled`. In production this is the identical `ConfigService` instance `lifespan.py` already passes to `FleetMigrationScheduler(config_service=get_config_service())` -- genuine defense-in-depth (the orchestrator no longer blindly trusts an upstream caller), not a second, divergent config source. **Dual-review correction**: this does NOT mean the two reads "can never disagree" -- both are reads of the SAME mutable config at two different points in time (the scheduler's `_is_enabled_now()` check, then moments later the orchestrator's own resolution), so an operator toggling the flag in that narrow window CAN produce a different value at each read. This is a real, honestly-documented, transient race, not a false claim of atomicity -- and it is a BENIGN direction: since `trigger_now()` (`scheduler.py:189`) and `_run_next_candidate()` (`scheduler.py:223`) BOTH independently refuse to even submit/run a job while `enabled=False`, the orchestrator is only ever reached at all when the scheduler most recently observed `True`; the race can therefore only make a specific in-flight pass MORE conservative (operator flips off mid-run, that pass withholds deletion) never less (there is no path where the scheduler observes `False` yet the orchestrator still runs unconditionally through the scheduler-driven path).
- Test-isolation note: `test_scheduler_1458.py`'s `_make_scheduler()` helper now ALSO calls `set_config_service()` with the exact same fake/real config_service object it passes to the scheduler's constructor (mirroring the production wiring above), with an autouse `reset_config_service()` fixture around every test -- required once the orchestrator started independently reading the global singleton.

**The "bake window" is a real, testable mixed-layout state -- but NOT a durable, operator-triggerable production mode reachable through the actual scheduler-driven path today (dual-review correction)**: a collection consolidated with `deletion_authorized=False` resolves `ChunkLayout.CHUNKS_DB` via `resolve_chunk_layout()` (so a new dual-layout-aware reader already sees it) while its legacy `vector_*.json` files remain physically present (so an old, un-upgraded reader that never learned about `chunks.db` still finds them via the unchanged legacy scan path) -- both correct, simultaneously, for the SAME collection. (The temporal counterpart of this mixed state described here -- a bootstrapped-but-not-reclaimed namespace readable via both the published copy and its in-repo legacy directory -- belonged to the retired Story #1457 mechanism and no longer arises; `tests/unit/services/temporal/test_temporal_bootstrap_rollout_gate_1460.py` still exercises that primitive directly.) The semantic proof below is unaffected -- but that proof calls the primitives/orchestrator DIRECTLY with an explicit `deletion_authorized=False` override, exactly as a hypothetical future caller (an admin trigger, a test, anything bypassing the scheduler) would. Today's ONLY production entry point, `FleetMigrationScheduler`, never durably produces this state as an operator-controlled mode: with `enabled=False` it refuses to submit/run a job at all (never reaches the bake window); with `enabled=True` it reaches the orchestrator with `deletion_authorized` resolving `True`, so deletion proceeds normally. The bake window is therefore reachable only via (a) a direct/explicit primitive or orchestrator call bypassing the scheduler, or (b) the narrow transient race described above (an operator disabling the flag mid-run) -- not as a stable state an operator can deliberately hold a repo in today.

**The completion gate naturally refuses to fire AC10's snapshot while the rollout gate is closed -- no extra code needed**: `verify_collection_fully_migrated()` (semantic) explicitly requires ZERO legacy files remaining on disk, and the temporal gate (today `repo_temporal_dirs_fully_consolidated()`, then the retired physical-absence predicate) requires every temporal directory to be off the legacy layout. A `deletion_authorized=False` pass therefore ALWAYS leaves at least one of these false, so `_run_migration_sequence` reports `status="incomplete"`, `is_repo_already_migrated()` (discovery.py) reports `False`, and the AC10 snapshot never publishes -- the SAME completion-gate machinery Story #1458 built for crash-recovery now equally recognizes "deliberately gated" and "genuinely incomplete" as the same not-yet-done state, with zero new completion logic required.

**Operator confirmation procedure (AC3's "document the confirmation procedure" requirement)**: since the code enforces no automatic reader-rollout confirmation (see the correction above), the operator MUST manually verify every fleet node runs the reader release BEFORE flipping `fleet_migration_config.enabled` to `True` via the Web UI Config Screen. Concrete procedure, using this project's existing per-node version-reporting mechanism (no new instrumentation required):
1. Every node's `NodeMetricsWriterService` already writes its own `server_version` (sourced from `code_indexer.__version__`) into the `node_metrics` table (PostgreSQL in cluster mode) on each metrics tick.
2. The admin dashboard's health panel (`web/templates/partials/dashboard_health.html`'s node-metrics carousel, backed by `web/routes.py`'s `dashboard_health_partial` reading `_nm_backend.get_latest_per_node()` into the `node_metrics` template variable) renders this `server_version` per node, next to each node's stale-badge in its own carousel slide -- an operator opens the dashboard and reads each node's reported version directly. **Round-3 dual-review correction**: this render did NOT exist when this section was first written -- `get_latest_per_node()` already included `server_version` in every returned row (both the SQLite and PostgreSQL backends), but the template silently never displayed it, so the procedure as originally documented did not actually work. Fixed by adding the per-node `nm.server_version` badge to the carousel slide (`dashboard_health.html`), proven via a real Jinja2 render test with a multi-node dataset (`tests/unit/server/web/test_dashboard_health_node_version_1460.py`) -- this is NOT the separate `per_node_metrics` projection (`routes.py`'s `dashboard_api_metrics_partial`, which feeds the UNRELATED `dashboard_api_metrics.html` partial and deliberately drops every field except `node_id`/`metrics`); do not conflate the two when touching either.
3. Confirm EVERY node's `server_version` is at or above the version that first shipped the dual-layout-aware reader (Stories #1455-#1459) -- this project's independent per-node auto-updater (60s timers, node-local `DeploymentLock`, no cross-node coordination) means nodes upgrade on their own schedule, so this check can only be done by directly reading each node's reported version, never assumed from a single node or from "enough time has passed".
4. Only once ALL nodes report a sufficient version does the operator flip `fleet_migration_config.enabled` to `True`. This is a manual, human-verified gate -- deliberately NOT automated (building an automatic fleet-version-verification mechanism would be scope beyond AC1/AC2's own text, which describes an operator-controlled flag, not a coded verification system).

**AC3 accepted, out-of-scope edge case (mirrors Story #1361's Risk R6)**: a standalone (non-server) `cidx` CLI process reading a migrated collection over a shared mount it does not itself own is NOT covered by this gate -- the `FleetMigrationConfig.enabled` flag and the scheduler's per-node auto-update timing only coordinate SERVER nodes (the only participants in a cluster's coordinated rollout). A standalone CLI has no equivalent "confirm the reader release before deletion" mechanism and never will under this design: building one would mean inventing cross-process coordination for a deployment topology (a lone CLI pointed at NFS-shared server-owned storage) this project does not support as a first-class scenario. The real, in-scope exposure this story addresses is old SERVER nodes in a coordinated fleet, which the config flag directly protects; a standalone CLI reading server-owned storage out-of-band is an accepted, documented risk, not a defect.

### Bug #1467 (Incremental File-Discovery Scope Divergence) and Bug #1468 (FSV Eager psycopg/FastAPI Leak)

Two pre-existing bugs found and fixed while working on `filesystem_vector_store.py` for AC11 above.

**Bug #1467**: `SmartIndexer._should_index_file` (`smart_indexer.py`) — the filter `_get_git_deltas_since_commit` applies to every `git diff --name-status` line for INCREMENTAL indexing — reimplemented only a SUBSET of `FileFinder`'s exclusion rules (extension allow-list + exclude_dirs component check), never the canonical `exclude_spec` PathSpec that is the ONLY place `.code-indexer-override.yaml` (a `cidx init`-generated artifact) is excluded. Since `yaml` is in the default extension allow-list and the file lives at the repo root (not inside an excluded directory), it silently passed the incomplete filter once `git add -A && git commit` put it under git's radar — a genuine scope-of-discovery divergence between the FULL-walk first index run (`find_files()`, correctly filtered) and the git-diff-based incremental run, not merely a counting bug. Fixed by adding `FileFinder.matches_exclude_pattern(relative_path: str) -> bool` — a new PUBLIC, existence-independent wrapper around `exclude_spec.match_file(...)` (deliberately pure string matching, no `.stat()`/`open()`, so DELETED files — which no longer exist on disk — remain correctly classifiable) — and calling it from `_should_index_file` alongside its existing checks. Any future incremental/git-diff-based discovery path MUST call this SAME method rather than reimplementing exclusion rules a third time.

**Bug #1468**: importing `FilesystemVectorStore` alone (the CLI/solo path's core storage class) eagerly pulled in `psycopg`+`fastapi` (133 transitive modules) via THREE chained module-level imports, none server-mode-gated: (1) `filesystem_vector_store.py`'s top-level `governed_call` import → `coalescer_registry` → `config_service` → `middleware.correlation`, which — because Python always executes a package's `__init__.py` before any submodule — forced `middleware/__init__.py`'s eager `from .error_handler import GlobalErrorHandler` (pulls fastapi); (2) `correlation.py` itself had a top-level `from fastapi import Request` for its (confirmed UNUSED-in-production) `CorrelationContextMiddleware` class; (3) `correlation.py`'s `from .error_formatters import generate_correlation_id` transitively hit `error_formatters.py`'s top-level `from fastapi.responses import JSONResponse`; separately, `embedding_cache_audit` → `search_embed_event_emit` → `search_embed_event_writer` → `connection_pool.py`'s top-level `from psycopg_pool import ConnectionPool` pulled psycopg. Fixed via PEP 562 module-level `__getattr__` lazy resolution in `middleware/__init__.py` (for `GlobalErrorHandler`) and `correlation.py` (for `CorrelationContextMiddleware`, whose class body — including its `fastapi`/`starlette` imports — is now constructed lazily on first attribute access and cached in module globals), plus `TYPE_CHECKING`-guarded imports + a deferred runtime import at the single real construction site in `error_formatters.py` (`create_json_response`) and `connection_pool.py` (`ConnectionPool.__init__`, its only actual `psycopg_pool` use site). Regression-gated by `tests/unit/storage/test_lazy_load_1468.py`, mirroring `tests/unit/xray/test_lazy_load.py`'s subprocess-based methodology (in-process checks are unreliable — earlier test files in the same pytest session may have already loaded these modules).

### Observability/Inspection Call-Site Updates (Story #1459, Epic #1454)

Story #1459 made the codebase's existence/health/status surfaces correctly report reality for both chunk-storage layouts (Story #1456/#1458's `SHARDED_JSON` vs `CHUNKS_DB`) and both temporal physical locations (Story #1457's in-repo-legacy vs golden-owned sister location). Two independent concerns, two independent mechanisms — never conflate them.

**Consolidated-layout false positives (AC1/AC5)**: several existence checks used a bare `rglob("*.json")` or `rglob("vector_*.json")` glob to answer "does this collection have real data" — both are wrong for a `CHUNKS_DB` collection, and the bare `*.json` variant is ALSO wrong for a `SHARDED_JSON` collection with zero real chunks, because `collection_meta.json` itself always exists in every collection directory and alone satisfies a `*.json` glob regardless of whether any real chunk data exists. Every one of these call sites now routes through `resolve_chunk_layout()`/`ChunkLayout` (`storage/shared/chunk_layout.py`, Story #1458 AC12) — never an independent flag/file probe — and, for `CHUNKS_DB`, an AUTHORITATIVE row-existence check — originally `ChunkStore.count() > 0` via `open_chunk_store_for_path`, since superseded by the read-only `chunk_store_has_real_data()` primitive described below (the original had real side-effect-creation and corruption-crash bugs, caught by code review) — never merely "does `chunks.db` exist as a file". Fixed call sites: `golden_repo_manager.py`'s `_index_exists` (new shared `_collection_has_real_chunk_data()` helper, applied to BOTH the "semantic" and "temporal" branches — the temporal branch here only fixes the local-clone glob false-positive, it does NOT reroute through the sister-location resolver below, since `_index_exists("temporal")` is not one of the five AC4 sites); `repository_health_aggregator.py`'s `collection_has_vector_shards`; `temporal_blank_out.py`'s `_is_shared_bookkeeping_directory` (gained a third OR-condition via `_has_chunks_db_real_data()`, alongside the pre-existing `hnsw_index.bin` and `vector_*.json` checks, so a bare-named `code-indexer-temporal` directory with real `chunks.db` rows is never misclassified as the shared bookkeeping directory and hard-deleted). `temporal_metadata_store.py`'s `detect_format`/`hnsw_orphan_sweep/discovery.py` were confirmed ALREADY layout-correct (the former already checks `resolve_chunk_layout` first per Story #1457; the latter already keys on `hnsw_index.bin` + `collection_meta.json` pairs only) — left untouched, per this project's anti-duplication rule.

**id_index.bin retirement is not a warning (AC1)**: `cli.py`'s `_status_impl` treated an absent `id_index.bin` as a warning/rebuild-needed condition ("ID Index: ⚠️ Missing (rebuilds automatically)") for both the semantic and multimodal collection status blocks, and fed a `has_id_index` flag into a downstream recovery-guidance renderer ("• ID Index (affects point lookups)"). For a `CHUNKS_DB` collection, `id_index.bin` is PERMANENTLY, DELIBERATELY never written (Story #1456) — its absence there is the correct, expected state. Both status blocks now call `resolve_chunk_layout(collection_path)` and, when `CHUNKS_DB`, display "ID Index: ➖ N/A (retired -- consolidated chunks.db storage)" instead of the warning, AND skip adding `"id_index"` to `missing_components` so the recovery-guidance renderer never emits the bogus rebuild instruction. A `SHARDED_JSON` collection missing `id_index.bin` is unchanged — still a real warning/recovery instruction.

**Writer/reader `collection_meta.json` contract (AC3)**: `vector_count`, `unique_file_count`, and `points_count` are the three scalar fields every downstream reader trusts without re-deriving. Confirmed (via a real end-to-end write-then-read contract test, `tests/unit/storage/test_chunks_db_meta_contract_1459.py`) that the Story #1456-#1458 writer path already produces these accurately for a `CHUNKS_DB` collection — no bug found, no writer change needed. Treat this as a standing shared contract: any future writer-path change to a `CHUNKS_DB` collection's metadata MUST keep these three fields accurate, since none of the reader call sites re-verify them independently.

**Temporal two-root status rerouting (AC4)**: five status-reporting call sites previously scanned ONLY the golden repo's local clone (`clone/.code-indexer/index/code-indexer-temporal-*`) for temporal presence — so once temporal data lives outside that clone, every one of them incorrectly reports a real, queryable temporal index as "not indexed". The fix is ONE shared helper, `get_temporal_repo_status(golden_repos_dir, repo_alias, legacy_index_path) -> TemporalRepoStatus` (`services/temporal/temporal_status.py`). Bug #1529 REWROTE its internals: the alias-pointer resolver it was originally built on is retired, so it now scans BOTH physical roots directly (the fixed `.temporal/{alias}/` root and the in-repo clone), the fixed root winning when a namespace appears in both, and maps the bare `code-indexer-temporal` legacy name to a reserved sentinel namespace key so a parser-only scan cannot miss it (item 5). Its CONTRACT is unchanged. `TemporalRepoStatus.has_data` (any namespace holds real committed rows, in either root) is DISTINCT from `.is_queryable` (at least one resolved namespace has `ResolvedTemporalShard.is_queryable == True`, i.e. a working `hnsw_index.bin`) — a catalog-present-but-not-yet-queryable namespace (crash-window: real committed rows, no HNSW yet) counts toward `has_data` but never toward `.is_queryable`; callers must never conflate the two. Wired into: `repository_health.py`'s `get_repository_indexes` (the pre-#1290 bare `temporal/` legacy directory check is UNCHANGED — it predates all of this and has no equivalent outside the clone; only the `code-indexer-temporal*` scan is rerouted), `activated_repos.py`'s index-status endpoint, `web/routes.py`'s `_detect_index_flags` (gained an optional `repo_alias` param), `dashboard_service.py`'s `get_temporal_index_status`, and `activated_repo_index_manager.py`'s `_get_temporal_status`. For every activated-repo branch, the underlying golden repo's alias is resolved via `ActivatedRepoManager.get_repository(username, user_alias)["golden_repo_alias"]` (a `_resolve_golden_repo_alias_for_activated_repo()` helper duplicated in `repository_health.py` and `activated_repos.py`, confirmed to exist as a tracked field per Story #1457's own docstring) — resolution failure/absence is caught and logged, never raised, falling back to the pre-existing local-scan-only behavior so a composite repo with no single backing golden repo degrades gracefully rather than breaking status entirely.

**Read-only inspection must never mutate or crash (Issue #1459 remediation, Findings 2-4)**: a dual code review (Claude + Codex) on the AC1/AC5 work above found that the three independent `resolve_chunk_layout()`-then-open-`ChunkStore` call sites (`golden_repo_manager.py`, `repository_health_aggregator.py`, `temporal_blank_out.py`) each had two real defects, empirically reproduced: (1) `ChunkStore.__init__`'s non-immutable path (`sqlite3.connect(str(db_path))`) CREATES the database file + schema if missing — so a mere "does this collection have real data" status probe on a `CHUNKS_DB`-flagged collection with no actual `chunks.db` yet silently created an empty one as a side effect of asking; (2) a genuinely corrupt `chunks.db` raised `sqlite3.DatabaseError` straight through the status/health check, crashing what must be a resilient reporting surface (the pre-#1459 glob-based check never crashed on an unrelated corrupt file — this was a real regression). Fixed by one new shared primitive, `chunk_store_has_real_data(db_path, *, on_error: Literal["treat_absent", "raise"] = "treat_absent") -> bool` (`storage/sqlite_chunk_store.py`), also closing the three-strike duplication (Messi Rule #4) across the three call sites: opens `db_path` via the SQLite URI `mode=ro` (never creates a missing file, unlike a bare `sqlite3.connect()`), and queries `SELECT COUNT(*) FROM chunks` directly (never instantiates a mutating `ChunkStore`). **Correction (round-2 remediation, Codex Finding B)**: `sqlite3.OperationalError` is NOT treated as "no data yet" unconditionally — only a message matching `_MISSING_SCHEMA_SUBSTRING` ("no such table", unambiguous: the file positively opened) means genuine absence and returns `False` regardless of `on_error`; any OTHER `OperationalError` (e.g. "database is locked" from a real concurrent writer, disk I/O errors) is a genuine operational problem and is dispatched through the SAME `on_error` contract as `sqlite3.DatabaseError` below — an earlier version of this primitive swallowed ALL `OperationalError`s unconditionally, which silently defeated `on_error="raise"` for a locked database (empirically reproduced via a real second connection holding `BEGIN EXCLUSIVE`). **Further correction (round-4 remediation)**: the round-2 fix ALSO originally treated `_UNABLE_TO_OPEN_SUBSTRING` ("unable to open database file") as unconditional "no data yet" — this was wrong, since SQLite emits that IDENTICAL message for both a genuinely-missing file and an existing-but-permission-denied file (empirically reproduced via `chmod 000` on a real populated `chunks.db`, which silently returned `False` even with `on_error="raise"`). Fixed with an explicit `Path(db_path).exists()` check inside that branch: genuinely absent still returns `False` unconditionally; an existing-but-unopenable file is now dispatched through `on_error` like any other operational failure. A genuinely corrupt file (`sqlite3.DatabaseError`) is dispatched by `on_error`: `"treat_absent"` (default, used by the two pure reporting call sites) logs a WARNING and returns `False` — never crashes; `"raise"` (used by `temporal_blank_out.py`'s destructive `_has_chunks_db_real_data()`) re-raises unchanged, since that module's existing fail-loud contract (Messi Rule #13) means a corrupt file feeding into a hard-delete classification decision must never be silently treated as "no data, proceed" — it must propagate loudly instead. **URI construction (round-2 remediation, Codex Finding A)**: the SQLite URI is built via `Path(db_path).resolve().as_uri() + "?mode=ro"`, NOT a naive `f"file:{path}?mode=ro"` — the latter mis-parses any path containing `?`/`#`/`%`/spaces/unicode (SQLite's URI parser reads a literal `?` as the start of the query string, truncating the path), which both produced a false negative AND — since the truncated path then opened in the default read-write-CREATE mode instead of the intended `mode=ro` — created stray files, a second instance of the exact "must never create files" violation this primitive exists to prevent. **Same bug, second call site**: `ChunkStore._open_connection`'s own immutable-open branch (`sqlite_chunk_store.py`, used whenever `open_chunk_store_for_path` decides a collection is immutable — e.g. a versioned snapshot) built its `?immutable=1` URI the same unescaped way and was fixed with the identical `Path(self.db_path).resolve().as_uri() + "?immutable=1"` technique; a real reproduction opening an immutable store at a special-character path previously failed with "no such table: chunks" (misparsed to a wrong/empty-schema path) before the fix. Any future read-only inspection call site that needs "does this `CHUNKS_DB` collection have real rows" MUST call this shared primitive, never reimplement the resolve-layout-then-open-store pattern independently.

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

**ABSOLUTE RULE, NO EXCEPTIONS**: any change to how the server/CLI is bootstrapped -- systemd unit content, environment variables, PATH, file locations, service wiring, anything a fresh host needs at install time -- MUST be automated in BOTH places, always:
1. **The installer** (`scripts/install-cidx-server.sh` and/or the relevant template under `src/code_indexer/server/auto_update/templates/`), so a brand-new host gets it correctly from the start.
2. **The auto-updater**, via an idempotent `_ensure_X_config()`-style self-heal method in `deployment_executor.py` (see the many existing `_ensure_*`/`_render_*_content` pairs in that file for the established pattern: read current unit content, detect the specific gap, inject/rewrite only what's needed, write+reload, no-op if already correct), so an ALREADY-DEPLOYED host repairs itself automatically on its next deploy cycle.

A template-only or installer-only fix is NOT sufficient and must never be treated as done. Bug #1440 is the concrete lesson: a template fix for a missing systemd `PATH=` line was correctly designed and reviewed, but it only affects fresh installs -- it left 3 already-running staging cluster nodes permanently broken, silently, because nothing ever re-renders an already-deployed unit file on its own. Production cannot rely on a manual operator re-running an install script. If a bootstrap gap is found on a live host, the fix is incomplete until an automated self-heal path exists that would have (and provably does, verified via the REAL auto-update mechanism firing naturally -- not manual SSH edits) repaired that exact host without any human touching it.

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
