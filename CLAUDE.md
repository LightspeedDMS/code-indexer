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
| `fast-automation.sh` | CLI, core logic, chunking, storage | ALL changes | ~21 min (measured: 1272s / 14,558 tests as of 2026-08-10; was 760s / 12,697 tests on 2026-07-13 -- runtime grew 67% while test count grew 15%, so it grows FASTER than the suite; re-measure before trusting this number) |
| `server-fast-automation.sh` | Server (MCP/REST/services/auth/storage) | Touching `src/code_indexer/server/` | ~10-15 min |
| `e2e-automation.sh` | 5-phase E2E: CLI standalone, CLI daemon, server in-process, CLI remote, fault-injection resiliency | Final regression gate -- ALL completed work | ~45-90 min |

`fast-automation.sh` does NOT run server tests -- it ignores `tests/unit/server/` entirely. Touching server code without running `server-fast-automation.sh` = untested changes.

`e2e-automation.sh` (Epic #700) is the final regression gate. No mocks -- real CLI subprocess, FastAPI server, VoyageAI, golden-repo registration. Non-negotiable for epic/story completion. Pure doc/config edits may waive with explicit user approval.

### Hierarchy

1. Targeted tests (seconds): `pytest tests/unit/.../test_X*.py -v --tb=short`
2. Manual testing
3. `fast-automation.sh` (zero failures; a timeout hit here is NOT automatically a hang -- check the actual duration against the current baseline above before assuming one). At the current ~21 min baseline the suite NO LONGER FITS a single foreground run: the Bash tool caps at 600000ms, so a foreground `timeout 900` is silently truncated to 10 min and kills a healthy run. Launch it in the BACKGROUND and poll at bounded intervals instead. When polling, do not use `pgrep -c -f fast-automation` as the liveness test -- the polling shell matches its own pattern and reports the suite alive forever; confirm completion from the log's `EXIT=` line.
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

Every story DoD must require `./lint.sh` to exit 0 BEFORE merging back to `development`.

What CI actually runs (`.github/workflows/main.yml`, the only workflow -- Bug #1552 fixed a case where this description was aspirational and the job did not exist):

| Job | What it runs | Is it a real gate? |
|-----|--------------|--------------------|
| `lint` | full `./lint.sh` (ruff check + ruff format check + mypy across `src/` AND `tests/`, plus the AC15 anti-orphan check), Python 3.9 | YES |
| `test` | a deliberate SMOKE test only -- 3 files (`test_factory.py`, `test_protocol.py`, `test_database_health_cluster.py`) across a 4-version Python matrix | NO -- it is NOT the suite |
| `create-tag` / `create-release` | gated on `[check-version, lint, test]` | tag/release cannot be cut from a red tree |

**A green CI badge does NOT mean the test suite passed** -- it means lint passed and 3 smoke files passed. The real test gates are and remain LOCAL: `fast-automation.sh`, `server-fast-automation.sh`, `e2e-automation.sh`. Anything that skips them reaches `staging` unchecked no matter how green CI looks.

Two sync constraints on the `lint` job, both learned by breaking them:

1. It pins `ruff`/`mypy` to the exact versions `.pre-commit-config.yaml` pins, because `pyproject`'s dev extra only FLOORS them -- an unpinned CI install can pick up a newer ruff whose formatter disagrees with every developer's local one and turn the gate red on a clean tree.
2. It runs on **Python 3.9**, which must track `[tool.mypy] python_version` in `pyproject.toml`. That config also sets `no_site_packages = false`, so mypy PARSES third-party sources under the declared target; running the job on a newer interpreter installs modern dependencies whose syntax a 3.9 target cannot parse, and the gate then dies inside `site-packages` instead of on our code (observed: `anyio/_core/_tasks.py: Pattern matching is only supported in Python 3.10 and greater`, on a tree that was clean locally). Do NOT "modernize" this to 3.12 without also moving the mypy target.

---

## Critical Architecture Invariants

### Production Scale — DESIGN EVERYTHING FOR IT

**Production runs ~900 repositories with ONE operator and no ops team.** Every design decision must
be made against that number, not against whatever the local dev server happens to hold.

**The dev server is ~30 repos — 3% of production. NEVER extrapolate performance or safety from it.**
Work that is instantaneous on 30 repos can freeze production for minutes. A local measurement proves
CORRECTNESS at best; it proves nothing about behaviour at fleet scale.

**Hard rules that follow from the scale:**

| Rule | Why |
|------|-----|
| NEVER call a synchronous filesystem/network function directly inside `async def` | It blocks the WHOLE event loop -- the server answers nothing while it runs. Offload with `anyio.to_thread.run_sync(...)` (the established idiom; see `_run_orphan_sweep` in `startup/lifespan.py`). |
| Any work that is O(number of repos) must be offloaded AND paced | ~18 filesystem ops/repo x 900 repos = ~16,000 NFS metadata ops. At 5ms/op that is ~80s. |
| Never put O(fleet) work on the STARTUP path unbacked | It delays readiness for the whole fleet's scan, and a failure there takes the node down at boot. |
| Treat `hard` NFS as able to block FOREVER | The cow-storage mount is `hard` NFSv3: `os.stat` blocks in UNINTERRUPTIBLE kernel retry when the server is unresponsive -- it never times out. On the event loop that is a permanently hung node, not a slow one. |
| No settings, no manual steps, no babysitting | One operator cannot flip switches or sweep leftovers across 900 repos. See the "no settings to gate a fix" rule. |
| Cleanup/repair must self-heal and converge | Anything left behind is left behind permanently and accumulates with every repo ever touched. |

**Concrete failure this rule exists to prevent (2026-08-12):** the Bug #1567/#1570 versioned-snapshot
sweep was written as a synchronous filesystem walk called directly inside `async def lifespan`. On the
30-repo dev server it completed instantly and looked fine. At 900 repos it would block the event loop
for roughly 80 seconds on every boot, and on an unresponsive NFS host it would hang the node
indefinitely -- while the CORRECT pattern (`anyio.to_thread.run_sync`) already existed 480 lines
above in the same function.

**How to apply:** before shipping anything that touches repos, ask "what does this do at 900?" for
BOTH time and blocking behaviour. In code review, treat a sync I/O call inside `async def` as a
defect regardless of how fast it looks locally.

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

### Module-Level Service Singletons Must Be Lazy (PEP 562) (Bug #1638, Bug #1650)

NEVER bind a heavy service/manager to a bare module-level name (`foo = HeavyService()`) that runs unconditionally at import time -- any bare or transitive import of that module then pays the full construction cost as a side effect, with no explicit opt-in. Observed three times: Bug #1638 (`server/app.py`'s `app = create_app()` triggered ConfigService/SQLite/DependencyLatencyTracker/MCPSelfRegistrationService startup and `primary_instance.lock` contention); Bug #1650 first occurrence (`server/services/git_operations_service.py`'s `git_operations_service = _get_git_operations_service()` triggered `GitOperationsService -> ActivatedRepoManager -> GoldenRepoManager -> SQLite` golden-repo load, spawning `bgm-worker`/`bgm-temporal-worker` threads); Bug #1650 second occurrence (`server/services/file_service.py`'s `file_service = FileListingService()`, an independent duplicate of the identical anti-pattern, found while verifying the fix below actually reached zero side effects).

Thread count is **7 per `ActivatedRepoManager()` instance** on default config, **14 combined** when both #1650 occurrences fire on the same real MCP handler import (measured directly, counting thread objects). `BackgroundJobManager.__init__` (`repositories/background_jobs.py:371,389`) spawns `max_concurrent_background_jobs` `bgm-worker` threads (default 5) **in a loop** plus `temporal_lane_concurrency` `bgm-temporal-worker` threads (default 2) **in a second loop**, so the count is operator-configurable (validated ranges 1-100 and 1-32, `config_manager.py:3431,3442`) and **does vary by deployment** -- always re-measure against the target's config rather than quoting this number. Beware a counting trap: every worker in one pool shares the identical thread name `bgm-worker-{id(self)}`, so a `set`-of-names diff (as in `test_mcp_handler_import_zero_side_effects_1650.py`'s repro) collapses them and reports 2/4 instead of 7/14. That set-diff is correct for its own purpose -- it asserts *zero* new threads -- but must not be read as a thread count.

**CRITICAL TRAP (caused a full review-rejection round on #1650): a pure module-level `__getattr__` deferral of the singleton BINDING is necessary but NOT sufficient.** PEP 562's `__getattr__` fires transparently on `from module import name` too -- so if ANY real consumer does `from package.module import name` at module scope (as `git_operations_service`'s 5 real consumers all did: `routers/git.py`, `mcp/handlers/{git_read,git_write,__init__,_legacy}.py`), importing THAT consumer module still forces full construction via `__getattr__`, unchanged from the pre-fix behavior. The module-level mechanism only protects a bare `import package.module` with no consumer import following it -- a path that may not exist anywhere in a real codebase. **The actual fix must make the CONSTRUCTOR itself cheap** ("Option A"): defer the expensive sub-construction(s) inside `__init__` (e.g. `self.activated_repo_manager = ActivatedRepoManager(...)`, `config_manager.get_config()`) into lazy properties with getters+setters, guarded by a CLASS-LEVEL `threading.RLock` (class-level, not per-instance, so test instances built via `Cls.__new__(Cls)` -- bypassing `__init__` entirely, a common existing test pattern -- still have a lock to synchronize on). This fixes the reported symptom regardless of how many modules bind the singleton name, and needs zero consumer-side changes. Verify with the ISSUE'S OWN REPRO as the acceptance test (e.g. "import a real MCP handler module -> assert zero background threads spawned, zero DB loads logged") -- a test that only checks a bare module import is not discriminating enough to catch this trap (see `tests/unit/server/services/test_mcp_handler_import_zero_side_effects_1650.py`).

**Fix pattern layer 1 (module-level binding, defense-in-depth, keep it)**: an annotation-only module global (no `= value` binding) plus a PEP 562 module-level `__getattr__` that lazily constructs on first genuine access, guarded by:

- `threading.RLock` (NEVER a plain `Lock`) -- a re-entrant probe arriving from within the construction call chain on the SAME thread must be able to re-acquire without deadlocking. A plain Lock self-deadlocking here was #1638's first-round review rejection.
- Explicit `_initialized`/`_initializing` sentinels (not the lock itself) to stop a re-entrant call from recursing into a second construction -- it returns `AttributeError` (-> `None` via `getattr(..., None)`), matching pre-fix unbound semantics exactly.
- A `_lazy_values` snapshot dict so `unittest.mock.patch`'s delattr-then-hasattr teardown sequence resolves cleanly instead of raising `KeyError` and permanently poisoning the module.

Any new fix in this class MUST include a re-entrancy discriminating test (a stand-in for an external dependency in the construction chain that probes the lazy attribute again mid-construction, on the same thread, run via a background thread with a bounded `join(timeout=...)`) -- this is the exact test shape that catches the plain-Lock self-deadlock class of bug before it reaches review. When Option A makes `__init__` itself dependency-free, this probe may need to hook a genuinely external, still-unconditionally-called dependency of `__init__` (e.g. a third-party library call like `cachetools.TTLCache(...)`) rather than the now-lazy sub-construction, since the original hook point can become unreachable once Option A lands -- see `TestReentrantAccessDuringConstructionDoesNotDeadlock` in `test_git_operations_service_lazy_init_1650.py` for the worked example.

Canonical implementations: `src/code_indexer/server/app.py` (Bug #1638, layer 1 only -- its `__init__`-equivalent, `create_app()`, is legitimately expensive and stays eager once reached), `src/code_indexer/server/services/git_operations_service.py` and `src/code_indexer/server/services/file_service.py` (Bug #1650, both layers). Tests: `tests/unit/server/test_app_import_no_side_effects_1638.py`, `tests/unit/server/test_app_lazy_init_repair_1638.py`, `tests/unit/server/services/test_git_operations_service_lazy_init_1650.py` (layer 1), `tests/unit/server/services/test_git_operations_service_deferred_construction_1650.py` and `test_file_service_deferred_construction_1650.py` (layer 2 / Option A), `tests/unit/server/services/test_mcp_handler_import_zero_side_effects_1650.py` (literal acceptance test against the issue's own repro).

This is distinct from the Bug #1467/#1468 PEP 562 note (Indexing Path section below): that one is about avoiding pulling heavy cross-layer IMPORTS (psycopg, fastapi) into CLI/solo-path modules; this one is about avoiding eager service CONSTRUCTION as an import-time side effect.

### Shared-Storage Protocol Is Pinned to NFSv3 — NFSv4 Is Off The Table

Cluster shared mounts (golden-repos, cow-storage) are pinned to NFSv3 (`vers=3,nolock,hard` / `soft,timeo=30,retrans=3`). NFSv4 was deployed and rolled back after three separate live failures (lock loss, git pack corruption, state-recovery hangs) — do not propose NFSv4 without addressing all three. The strategic direction is to need LESS from the filesystem (coordination moved to PostgreSQL), not to find a better protocol; any storage proposal must preserve local `cp --reflink` support.

-> Detail: docs/architecture-invariants.md#shared-storage-protocol-nfs | docs/cluster-setup.md (lines 52, 465-474) | docs/cow-storage-setup.md#294

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

A `golden_repos` row must never end up as a "registry-orphan" (no on-disk clone and/or no alias pointer). Provisioning is all-or-nothing (row+clone+pointer together), removal deletes the registry row BEFORE any file deletion, and `reconcile_golden_repo_registry` self-heals orphans found at startup behind a mandatory health-gate + mass-deletion circuit-breaker (never remove more than half the fleet in one pass) + pointer-repair. A persisted cross-restart confirmation counter lets a genuinely persistent orphan set eventually auto-heal after 3 confirmed sweeps, and every automatic mass-removal is recorded and surfaced on `/health`.

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Temporal Enable-Flag Cross-Table Reconciliation + Real-Data-Presence Detection (Bug #1390)

`enable_temporal` reconciliation between `golden_repos_metadata` and `global_repos` is ONE-WAY: a stored `True` downgrades to `False` when the filesystem shows no real data, but `False` is NEVER auto-flipped to `True` — an operator's explicit disable must never be silently reversed by a scheduled refresh.

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Query & Search Timeouts Consolidation (Issue #1398)

`SearchTimeoutsConfig` is the sole, Web-UI-configurable source for the MCP/query handler + embedding-provider + reranker timeouts — no more hardcoded constants. `regex_search` and its exempt-tool siblings deliberately bypass the dispatcher's `asyncio.wait_for` timeout wrapper (sync-dispatched, governed by ripgrep's own timeout instead); read the documented rationale before changing either half of that pairing.

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

Every HNSW build/finalize path runs detect+repair-orphans before persisting; health checks expose `orphan_count` as a strict binary (0 = OK, >0 = ERROR, no WARNING tier). A missing fork capability (custom hnswlib fork required for `check_integrity`/`repair_orphans`) DEGRADES (skip the orphan pass, log one WARNING, let the build/save proceed) rather than aborting the whole indexing operation — never reintroduce a hard-abort `HNSWCapabilityError`.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations (see "HNSW Finalize-Time Orphan Detect+Repair + Zero-Tolerance Health")

### HNSW Fleet Orphan Repair Sweep (Epic #1333, Story #1360)

Paced, resumable background sweep (`src/code_indexer/server/services/hnsw_orphan_sweep/`) that repairs the PRE-EXISTING fleet backlog of orphaned indexes built before S2's build-path fix existed -- S2 only protects newly-built/rebuilt indexes. Discovery composes the SAME `list_golden_repos()`/`list_all_activated_repositories()` primitives other schedulers reuse (never a third enumeration mechanism), walking `.code-indexer/index/` for `hnsw_index.bin` + `collection_meta.json` pairs and skipping `.versioned/` snapshots via `is_versioned_snapshot`. Repair acquires the SAME `.index_rebuild.lock` `HNSWIndexManager` build/finalize uses, re-checks integrity under the lock immediately before writing, and invalidates the server-side `HNSWIndexCache` singleton on success so a running query-serving process sees the fix without a restart. The durable cursor (`hnsw_orphan_sweep_state` table, SQLite solo / PostgreSQL cluster) is a STRING stable sort key (`"golden:{alias}:{relpath}"` / `"activated:{user}/{alias}:{relpath}"`) — NEVER a numeric offset, since temporal shards and activated repos are created/deleted continuously between ticks. Cluster dedup is `register_job_if_no_conflict` ONLY — deliberately NOT `ShardOwnership.owns()` (that primitive is fail-open cache-locality, not a coverage guarantee; filtering by it here would create a real repair coverage gap). Dashboard pattern: one short job PER TICK (mirrors `ActivatedReaperScheduler`), never one job spanning the whole multi-tick pass; cross-pass stats live on `GET /api/admin/hnsw-orphan-sweep/stats`, independent of JobTracker. Ships ON by default (`batch_size=15`, `tick_interval_minutes=7`), both adjustable via `get_config_service()`.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations (see "HNSW Fleet Orphan Repair Sweep")

### Chunk Storage Layout: SHARDED_JSON vs CHUNKS_DB (Epic #1454, Story #1456/#1455)

Two coexisting chunk layouts are both fully supported simultaneously: legacy SHARDED_JSON (`vector_*.json` files) and CHUNKS_DB (one `chunks.db` SQLite file per collection). `resolve_chunk_layout()` is the SOLE authority on which layout a collection uses — never independently probe for `chunks.db`. `id_index.bin` is permanently retired for CHUNKS_DB collections. Any write/finalize call site MUST use `_is_chunks_db_collection()`, never the bare resolver, to avoid misclassifying a fresh build mid-construction.

-> Detail: docs/architecture-invariants.md#epic-1454-chunk-storage-consolidation-and-fleet-migration

### Temporal Never Writes Legacy JSON (Bug #1528)

Temporal (git-commit-history) indexing writes the consolidated CHUNKS_DB layout by default — it is no longer hard-excluded from Epic #1454's chunk consolidation. Pre-existing legacy shards are migrated in place before any temporal write; the CLI temporal branch, fleet migration, and the completion/reconciliation gates all operate on this in-place model, never a physical-absence check.

-> Detail: docs/architecture-invariants.md#epic-1454-chunk-storage-consolidation-and-fleet-migration

### Server-Context Temporal Data Lives at a Fixed Path Outside the Repo Tree (Bug #1529, partial)

Server-context temporal data for a golden repo lives at a FIXED, deterministic path outside the repo's clone (`{golden_repos_dir}/.temporal/{alias}/...`), resolved by ONE seam (`resolve_temporal_index_dir`) -- never versioned, never alias-pointer-indirected. Both read seams (REST query manager, MCP temporal worker) must derive the location from the GOLDEN alias, never from an activated repo's own CoW clone, and must FAIL LOUD rather than silently fall back to the clone. HNSW cache freshness after an in-place refresh is verified via a stat-based identity fingerprint (mtime+size+inode+dev), not blind trust in mtime alone.

-> Detail: docs/architecture-invariants.md#epic-1454-chunk-storage-consolidation-and-fleet-migration

### Story #1457 Sister-Location Temporal Placement -- RETIRED (superseded by Bug #1528 + Bug #1529)

The versioned-snapshot + alias-pointer + resolver mechanism this story built for temporal placement is RETIRED, superseded by Bug #1528 (write: in-place chunks.db) and Bug #1529 (read: fixed path). Do not resurrect `TemporalShardResolver`, `maybe_relocate_shard_to_sister_location`, or the sister-location bootstrap/publish primitives -- re-wiring the relocation trigger against a chunks.db shard causes silent data loss. A few shared hardening pieces (collision-safe version-id generation, alias-fsync-on-publish, `CleanupManager`'s minimum-retention-age floor) remain live and unrelated to placement.

-> Detail: docs/architecture-invariants.md#epic-1454-chunk-storage-consolidation-and-fleet-migration

### Fleet Migration (Story #1458, Epic #1454)

`run_fleet_migration_for_repo(...)` (per-repo orchestrator, Story #1458) consolidates a golden repo's semantic collections and temporal namespaces to CHUNKS_DB in place, one repo at a time, behind the SAME write-lock activation checks. `consolidate_collection_in_place()` is the core engine: write-verify-flip-discriminator-then-delete-legacy, crash-safe and idempotent at every step. The scheduler submits exactly one job per fleet-wide tick via a fixed sentinel alias so the whole fleet is serialized.

-> Detail: docs/architecture-invariants.md#epic-1454-chunk-storage-consolidation-and-fleet-migration

### Fleet Rollout Safety Gate (Story #1460, Epic #1454 -- FINAL story)

Both destructive deletion primitives (legacy-file cleanup in consolidation, and the now-moot temporal in-repo reclaim) are gated behind `fleet_migration_config.enabled` (default OFF) via a `deletion_authorized` parameter that the orchestrator independently re-resolves from config rather than trusting the caller. Flipping the flag ON requires a MANUAL operator confirmation that every fleet node's reported `server_version` already runs the dual-layout-aware reader -- there is no automated cross-node version gate.

-> Detail: docs/architecture-invariants.md#epic-1454-chunk-storage-consolidation-and-fleet-migration

### Bug #1467 (Incremental File-Discovery Scope Divergence) and Bug #1468 (FSV Eager psycopg/FastAPI Leak)

Bug #1467: incremental (git-diff-based) file discovery MUST use `FileFinder.matches_exclude_pattern()` — the SAME exclusion rules as the full-walk discovery path — never a partial reimplementation, or files like `.code-indexer-override.yaml` silently pass an incomplete filter. Bug #1468: importing `FilesystemVectorStore` alone must never eagerly pull in `psycopg`/`fastapi` — any new module-level import chain from CLI/solo-path storage code into server-only dependencies is a regression; use lazy `TYPE_CHECKING` + PEP 562 module `__getattr__` deferral.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations (see "Bug #1467 and Bug #1468")

### Observability/Inspection Call-Site Updates (Story #1459, Epic #1454)

Every existence/health/status check for chunk data MUST route through `resolve_chunk_layout()` and, for CHUNKS_DB, the read-only `chunk_store_has_real_data()` primitive — never a bare `rglob` glob (wrong for both layouts) and never a mutating `ChunkStore` open (creates a file as a side effect of merely asking). A missing `id_index.bin` on a CHUNKS_DB collection is expected, not a warning. Read-only inspection must degrade (log + return False) on a corrupt store, never crash, unless the call site's own contract requires fail-loud.

-> Detail: docs/architecture-invariants.md#epic-1454-chunk-storage-consolidation-and-fleet-migration

### Database Migrations Must Be Backward Compatible

Rolling restarts mean old and new nodes share schema during upgrade. MigrationRunner auto-runs on startup.

- **Allowed**: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`, new nullable columns / columns with defaults
- **NEVER**: `DROP TABLE`, `DROP COLUMN`, `RENAME TABLE/COLUMN`, `ALTER COLUMN TYPE`, removing NOT NULL

### Migration Concurrent Startup Safety (Story #1164)

Under `uvicorn --workers N` (PostgreSQL), `MigrationRunner.run()` acquires a PG SESSION advisory lock (`pg_advisory_lock`, key `_MIGRATION_ADVISORY_LOCK_KEY`, identical on every node) at entry and releases it in `finally` on ALL paths. Always parameterized `%s`, never f-string. SQLite path never references `pg_advisory_lock`.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations

### JSONB/TEXT Column Normalization (Bug #1622, #1652, #1655)

Any column that is JSONB in PostgreSQL and TEXT in SQLite MUST be read through `parse_json_column(raw, expected_type, field_name)` (`src/code_indexer/server/storage/json_column.py`) -- never a bare `json.loads()`. psycopg already deserializes a JSONB column into a native python object (dict/list) before the row reaches application code, while sqlite3 always returns the TEXT column as a str; unconditional `json.loads()` on the PostgreSQL value raises `TypeError`. This one helper replaced three independently-drifted copies of the same logic (`dependency_map_routes.py`, `wiki_cache.py`, `activated_repo_manager.py`) -- do not reintroduce a fourth.

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

### cidx-meta backup contract (Story #926, superseded by Bug #1555)

Sync runs BEFORE indexing; all git ops on the mutable base path only (`get_cidx_meta_path()`), NEVER inside `.versioned/`. `XrayPatternService` (Bug #1037) shares the coarse `cidx-meta` write lock. Cluster git-remote auth resolves the deploy key via node-local `~/.ssh/config` materialized from PG by `SSHKeySyncService.sync()`.

**Bug #1555 (commit 6a46c996) removed rebase + Claude-CLI conflict resolution entirely.** The remote is a passive BACKUP MIRROR, never a peer whose independent history must be preserved -- `sync()` no longer fetches-then-rebases onto `origin/{branch}`. It commits local changes and publishes local HEAD directly via `git push --force-with-lease`, unconditionally overwriting whatever the remote holds; a diverged remote self-heals on the next cycle with zero conflict-resolution step. `conflict_resolver.py`, its MCP prompt, the quarantine-bookkeeping methods/tables' CRUD paths, and the `/health` quarantine surface were deleted as part of this fix (quarantine tables remain per never-drop-tables, but stay empty). See `src/code_indexer/server/services/cidx_meta_backup/sync.py`'s module docstring for the full rationale.

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

### OTEL Search/FTS/Embedding/Job/Refresh/Span Metrics Wiring (Story #1586, complete)

`ApplicationMetrics`/`JobMetrics` (`server/telemetry/metrics_instrumentation.py` and `job_metrics.py`, Story #698) were built with zero call sites until this story. Story #1586 wires all of AC1-AC6 in one pass.

- **AC1**: `server/mcp/handlers/search.py`'s `_execute_tracked_search` records `cidx.search.*` (via a `_record_search_metric` helper called from the existing `finally` block) and `_execute_regex_search` records `cidx.fts.*` (via a thin `_execute_regex_search` wrapper timing the real `_execute_regex_search_impl` call). Both are no-ops when telemetry/`export_metrics` is off (`ApplicationMetrics.is_active` gate). Both call sites use `peek_telemetry_manager()` (never `get_telemetry_manager()`) so they can never win the "first call wins, disabled fallback" telemetry-init race if fired before real startup config loads. **Remediation (real manual E2E finding, 2026-08-19): the original claim that "REST reaches this through the same MCP handler stack" was FALSE** — `POST /api/query` calls `semantic_query_manager.query_user_repositories()` and a real `TantivyIndexManager` DIRECTLY, never through `_execute_tracked_search`/`_execute_regex_search`; confirmed live, 2 real REST `/api/query` calls left `cidx.search.requests` frozen while `cidx.embedding.requests` rose. Fixed via a deliberate duplicate-instrumentation approach (Option B, not a shared-function refactor): `inline_query.py` has its own `_record_rest_search_metric`/`_record_rest_fts_metric` helpers (identical `peek_telemetry_manager()`/`get_application_metrics()`/`is_active` shape), wired via `try/finally` around both `query_user_repositories()` call sites (default-semantic branch and the hybrid branch) and around the real `tantivy_manager.search()` call in the FTS/hybrid branch — chosen over refactoring because `semantic_query` is a ~600+ line route with sharding/forwarding/async-job logic too risky to restructure this late. See `tests/unit/server/routers/test_inline_query_otel_metrics_wiring_1586.py`.
- **AC2**: new shared module `services/embedding_metrics_telemetry.py` (`record_embedding_provider_call`, `time_and_record_embedding_call`) is the ONE wiring point every embedding provider client calls per REAL outbound HTTP attempt. **Deliberately instrumented at the lowest real-HTTP boundary in each provider** (`voyage_ai.py`'s `_make_sync_request`/`_make_sync_contextualized_request`, `cohere_embedding.py`'s `_make_sync_request`, `cohere_multimodal.py`'s `_make_request`, `voyage_multimodal.py`'s `get_multimodal_embedding`/`_submit_multimodal_batch`) — NOT at the four public methods (`get_embedding`/`get_embeddings_batch`/`*_with_metadata`) the story text names, because those delegate into the same boundary and instrumenting both would double-count one real request. Granularity matches the pre-existing `embedding_call_instrumentation.instrument_call` boundary exactly (one event per real HTTP attempt, including retries) — see `tests/unit/services/test_voyage_ai_embedding_stats_1418.py`'s own documented reasoning. The query-embedding cache (Epic #1103) lives ABOVE these clients and never calls into them on a hit, so cache hits structurally cannot double-count. `VoyageMultimodalClient` gained an `http_client_factory` constructor param (mirroring `VoyageAIClient`) as a prerequisite. Uses `peek_telemetry_manager()`, same race-avoidance rationale as AC1.
- **AC3**: `JobMetrics` observable gauges (`cidx.jobs.active/queued`, `cidx.repos.total/indexed`) wired via module-level factory closures in `startup/lifespan.py` (`_build_job_counts_callback`, `_build_repository_counts_callback`), mirroring the pre-existing `_make_dep_map_repair_invoker_fn` testability pattern. `job_tracker.py`'s `_record_job_metric` (called from `complete_job`/`fail_job`) uses `peek_telemetry_manager()` so a background job-completion callback can never win the telemetry-init race.
- **Repository-counts gauge is a background-refreshed cache, never a synchronous fleet walk**: `_build_repository_counts_callback`'s returned callback is O(1) — it is backed by `_RepositoryCountsCache` (`startup/lifespan.py`), which returns the last-computed `{total, indexed}` immediately and refreshes it on a background daemon thread at most once per `refresh_interval_seconds` (default 900s), single-flighted. This exists because OTEL's `SynchronousMeasurementConsumer.collect()` holds a lock across ALL registered callbacks and enforces `export_timeout_millis` (default 30s, checked only BETWEEN callbacks, never during one) — an unpaced O(fleet) `list_golden_repos()`/`_index_exists()` walk (two separate gauge callbacks each trigger it) would, at production scale (~900 repos), take tens of seconds and can block FOREVER if the `hard` NFSv3 mount wedges, discarding an entire cycle's metrics or hanging `MeterProvider.shutdown()`. A stalled refresh never blocks a caller — it simply serves the last-good (possibly stale) value.
- **AC4**: `RefreshScheduler._execute_refresh` is a thin wrapper (renamed impl: `_execute_refresh_impl`) that times the call and records `cidx.repos.refresh.duration` via `_record_refresh_duration_metric`, deriving `status` from the impl's **returned dict's `success` key** (`"success" if result.get("success") else "error"`), never from whether an exception was raised — a real failure path (e.g. Bug #1253's local-repo repair failure, or an integrity-gate failure) can return `{"success": False, ...}` WITHOUT raising. Skip/no-op early returns (alias not found, write-lock held, no changes detected, etc.) already set `"success": True` by original design — deliberately still recorded as `status="success"` (genuine non-failure outcomes), not a third `"skipped"` state.
- **AC5**: custom spans (`server/telemetry/spans.py`'s `create_span`, no-ops when tracing is unavailable/uninitialized) wrap: `cidx.depmap.run_delta_analysis` (`dependency_map_service.py`), `cidx.golden_repo.cow_snapshot` (`golden_repo_manager.py`) and `cidx.snapshot_manager.create_cow_snapshot` (`storage/shared/snapshot_manager.py` — two DIFFERENT span names, not the same span in two files), `cidx.temporal.index_commits` (`temporal_indexer.py`), `cidx.scip.generate` (`scip/generator.py` — `create_span` is imported function-local there, not at module scope, to avoid pulling server-layer telemetry modules into the CLI's `cidx scip generate` import path), and HNSW build/finalize durability spans (`storage/hnsw_index_manager.py`). All follow the same thin-wrapper pattern: rename the existing body to `_*_impl`, wrap it in `with create_span(...)`. Span-to-log correlation depends on `spans.py`'s `_get_correlation_id()` and `manager.py`'s `get_telemetry_manager()` fallback both importing via the real `code_indexer.*` package path (not a stray `src.`-prefixed alias that doesn't exist in a non-editable production install). **Remediation (real manual E2E finding, 2026-08-19)**: `cidx.snapshot_manager.create_cow_snapshot` originally wrapped only `_create_cow_snapshot`, a fallback branch `create_snapshot()` reaches ONLY when `self._clone_backend is None` — every real deployment constructs a `clone_backend`, so the span was structurally dead in production (confirmed live: the sibling `cidx.golden_repo.cow_snapshot` span fired correctly, this one never did). Fixed by moving the span to wrap `create_snapshot()`'s entire dispatch (all three branches: `clone_backend`, FlexClone, and the CoW fallback) instead of just the unreachable branch; the inner per-branch span on `_create_cow_snapshot` was removed to avoid a confusing duplicate nested span with the same name. **Known subprocess-telemetry limitation**: `cidx.scip.generate`, `cidx.temporal.index_commits`, and the two HNSW spans (`cidx.hnsw.build_index`, `cidx.hnsw.save_incremental_update`) execute inside the `cidx index` CHILD SUBPROCESS during golden-repo registration (the server shells out via `["cidx", "index", ...]`), which has no `TracerProvider` (`get_telemetry_manager()` is only ever constructed in the PARENT process's `startup/lifespan.py`) — these 4 span sites cannot export on that execution path, and fixing it would require propagating OTEL context across the subprocess boundary (out of scope here). `cidx.hnsw.rebuild_from_vectors` DOES export correctly, verified live, via the separate in-server `background_index_rebuilder` path, which never goes through the subprocess.
- **AC6**: `startup/lifespan.py`'s telemetry block constructs both `get_application_metrics(telemetry_manager)` and `get_job_metrics(telemetry_manager)` (gated on `export_metrics`, mirroring the existing `MachineMetricsExporter` gate), storing them at `app.state.application_metrics`/`app.state.job_metrics`.
- **Bug #1606 (pre-existing Story #696 code, fixed opportunistically during #1586 remediation, 2026-08-19)**: `telemetry/machine_metrics.py`'s 7 observable-gauge callbacks used to `yield (value, attrs)` as a plain tuple — the OTEL SDK's real callback contract requires an `Observation` object (`.value`/`.attributes`), so every export cycle logged `AttributeError: 'tuple' object has no attribute 'value'` as an ERROR (confirmed live: 2,269 of 2,270 ERROR log rows in one short manual test session) and silently produced ZERO data points for all 7 gauges. Fixed with a `_create_observation(value, attributes)` helper (lazy `from opentelemetry.metrics import Observation`) mirroring `job_metrics.py`'s own `_create_observation` pattern.
- **Testing**: `tests/unit/server/telemetry/otel_test_support.py` provides `active_application_metrics()`/`active_application_metrics_singleton()`/`active_job_metrics()`/`active_job_metrics_singleton()` — real `MeterProvider`+`InMemoryMetricReader` wired through a small `TelemetryManager` subclass, sidestepping OTEL's genuine one-time-only global-`MeterProvider` constraint so multiple test files can each observe their own metric data points. The `_singleton()` variants also install/clear the real `code_indexer.server.telemetry.manager` process-wide singleton (via `_install_telemetry_manager_singleton`), required because production call sites resolve `peek_telemetry_manager()` directly rather than receiving an instance. Reused directly by the real E2E test `tests/e2e/server/test_20_telemetry_metrics_wiring_1586.py` (real MCP front door, real registered/indexed golden repo, real regex_search call, real `cidx.fts.requests` assertion).

### Log Store correlation_id Column Population (Bug #1641)

`get_correlation_id()` being correctly wired (#1631/#1632) does NOT mean the log store's `correlation_id` column gets populated. `SQLiteLogHandler` (the handler backing `admin_logs_query`) only ever reads `record.correlation_id` -- an attribute that exists on a `LogRecord` ONLY when the logging call site explicitly passed `extra={"correlation_id": ...}` (e.g. via `logging_utils.get_log_extra()`). The overwhelming majority of `logger.info()/warning()/error()` calls across the codebase pass no `extra` at all, so the column stayed NULL for ~97% of rows regardless of how correctly the reader itself resolved.

**Thread-boundary trap**: in production `SQLiteLogHandler` is always installed behind `async_logging.install_queue_logging()`'s `QueueListener` (Bug #1078), so `SQLiteLogHandler.emit()` executes on the LISTENER thread, not the original request thread. A naive fix reading `get_correlation_id()` inside `SQLiteLogHandler.emit()` would still see `None` -- `contextvars` do not propagate across a plain `threading.Thread` boundary. The value must be captured on the ORIGINAL calling thread, before the record crosses into the queue.

**Fix**: `logging_utils.inject_correlation_id(record)` is the single shared helper (never overrides an explicitly-set `record.correlation_id`, never fabricates a value when no context is active). It is called from `async_logging.IdentityQueueHandler.prepare()` (the real production wiring point -- runs synchronously on the request thread inside `Handler.handle()`, before `enqueue()`) and, defensively, from `SQLiteLogHandler.emit()` itself (covers the non-queued direct-attach case, e.g. tests). No per-call-site changes were needed anywhere else in the codebase. `request_path`/`user_id` columns remain unpopulated -- unlike `correlation_id` there is no existing `ContextVar`/reader for either, so wiring them would require a new per-request context mechanism; scoped out as a separate follow-up rather than folded into this fix.

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
