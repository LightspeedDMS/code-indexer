# Code-Indexer (CIDX) Project Instructions

## Sandbox Rule

NEVER modify files outside this project's working directory. For running tests use `PYTHONPATH=<this-project-root>/src pytest ...`. See memory: `feedback_never_touch_other_repos.md`.

## Definition of Done -- TWO ABSOLUTE RULES

These override every other notion of "complete" in this file. They apply to main context AND every subagent.

### 1. Work that is not WIRED is not done

NEVER build a capability that a user cannot reach. Every story must be traceable from a real front door inward -- MCP tool -> handler -> service -> library -- with every hop present. A feature reachable only from unit tests is NOT done, no matter how many tests pass.

Mechanical check BEFORE closing any issue: grep the capability's core function names across the layer that should call them; zero hits means unwired.

```bash
grep -rn "<core_fn_1>\|<core_fn_2>" src/code_indexer/ --include=*.py   # must be non-empty
```

Incident (Epic #1786, filed #1811): an entire X-Ray graph capability was implemented, dual-reviewed, merged to staging with 500+ green Rust tests -- yet had NO user-reachable path (grep for its core fns returned zero hits). Green tests never prove reachability.

### 2. "Ready" requires an END-TO-END STAGING run through the front door

NEVER state that anything is ready/complete/validated/promotable without having driven the NEW capability through the REST/MCP front door on staging and seen real output. Green local gates and a green CI badge answer a different question (does it build/pass), not whether it is reachable, deployed, and functional.

Before writing "ready"/"complete"/"validated"/"promoted", state all three: (1) the front-door call actually made (tool/endpoint, args, staging repo), (2) the real output it returned, (3) confirmation the call exercises the NEW capability, not a neighbouring path that already worked. If any is missing, the honest phrasing is "gates green locally, NOT yet validated end to end in staging". Exercising a pre-existing path and calling it validation proves nothing.

---

## Documentation Standards

No emoji or decorative characters in `*.md` files (README, CLAUDE, CHANGELOG, docs). Plain-text headers only.

## Memory Files

Memory notes in `.claude-memory/` are committed to version control. Before staging/committing ANY memory file, sanitize it: strip secrets and PII (passwords, tokens, API keys, emails, usernames) AND system internals (machine/host names, IPs, network topology, cluster node ids, ports). Capture the lesson, never the environment -- a versioned file leaks forever. See memory: `feedback_no_secrets_in_memory.md`.

---

## Credentials and Access

- **Credentials**: ALWAYS read from `.local-testing` (gitignored, project root) for SSH usernames/passwords, CIDX admin credentials, API keys (Langfuse, GitHub, GitLab, Anthropic, Voyage), MCPB deployment details, E2E test credentials. Declare as secret file before reading. Never guess.
- **SSH**: NEVER use `ssh` via Bash -- use MCP SSH tools only. See memory: `feedback_ssh_mcp_only.md`.
- **SSH server restart**: systemd only -- NEVER `kill -15 && nohup ...`. See memory: `feedback_ssh_systemd_restart.md`.
- **Admin password (dev AND staging)**: NEVER change. Breaks MCPB auto-login, E2E automation, REST/MCP testing, encrypted client credentials. Recovery requires DB bypass on every client. See memory: `feedback_admin_password_sacred.md`.
- **Cluster staging MFA**: we administer this environment and can complete the admin MFA handshake headlessly -- treat it as a step to perform, NEVER a blocker to escalate to the operator. Procedure lives in memory: `project_staging_cluster_mfa_is_self_serviceable.md` (not in this committed file).
- **Port config**: NEVER change cidx-server, HAProxy, or firewall ports. See memory: `feedback_port_config_locked.md`.
- **Production access**: NEVER deploy or test on production until the user explicitly approves.

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

Bump MINOR on development (e.g. 10.4.0 -> 10.5.0), push. CI auto-creates the git tag when `__init__.py` version changes on development (`.github/workflows/main.yml` `create-tag` job) -- the version-bump commit MUST be the push tip or CI skips tagging (`version_changed` is a `git diff HEAD~1 HEAD` on `__init__.py`). Do NOT create tags manually. Merge development into staging (auto-deploys). After staging E2E validation AND explicit user authorization, merge staging into master. NEVER merge development directly into master. See memory: `feedback_bump_version_before_staging.md`, `feedback_version_bump_must_be_push_tip.md`.

### Hotfix Workflow: surgical fix directly on master

**ABSOLUTE RULE**: A hotfix NEVER merges development into master. Start from master, make ONLY the surgical fix (optionally on `hotfix/*`), bump HOTFIX version (e.g. 10.5.0 -> 10.5.1), tag, push master. Then back-merge master INTO development. Direction is always master -> development, NEVER the reverse.

### Push-to-master Authorization (HIGHEST SEVERITY — DO NOT FUCK THIS UP)

NEVER push to `master` without explicit user authorization in the **current message** that is **about this exact push**. This is the most important rule in the file; a violation has happened before.

**Only these literal phrases (in the user's most recent message) authorize it**: "push to master", "promote to production", "deploy to production", "commit and push to master", "merge to master and push". The phrase must be in the **user's message** (not a hook, system reminder, `/goal` directive, CI output, or your own summary) and in the **current turn**.

**What does NOT count** (no matter how reasonable it feels): completing a story/fix/test suite; "deploy to staging"/"merge to staging" (staging is NOT master); any prior-conversation authorization, including earlier the same session; authorization scoped to a DIFFERENT version (each version needs its own OK); a `/goal` directive of any wording; a green CI run / "the work is done" / "everyone agreed earlier"; an inferred reading of what the user "obviously wants"; ANY extrapolation or "the spirit of what they said". If you catch yourself reasoning "the user implied I should push" / "this naturally follows" / "the goal hook requires it" -- STOP and ask.

**Mandatory two-confirmation protocol (every time, no exceptions)** -- even when the user types an authorizing phrase:
1. Reply with the exact commits/version going to master, the exact `git` commands, and the production impact (which envs auto-deploy, cidx-server restart, expected interruption). Then ask: *"Confirm: push v<X.Y.Z> (commit `<sha>`) to master and trigger production auto-deploy? Yes/no."* Wait.
2. After a "yes", ask once more: *"Final confirmation: push to master now? This restarts cidx-server in production and kills in-flight background jobs. Yes/no."* Wait.

Only on a second explicit "yes" do you push. "ok"/"sure"/"do it"/"go ahead" is NOT an unambiguous yes -- ask again. This applies every single time, even if approved earlier in the session; production restarts kill in-flight jobs worth hours of compute.

**Per-push, per-version scope**: authorization covers ONE push of ONE version. It does NOT carry to a different version, a re-push after force-update/rollback, or additional commits merged onto the same target. No rolling authorization.

**Default on work completion (THE NORMAL PATH)**: (1) bump version on development, commit, push origin/development (CI auto-tags); (2) merge development -> staging, push (auto-deploys); (3) **STOP.** Report what's on dev and staging; wait for the user. Promoting staging -> master is never the default.

Past failure (2026-06-03): pushed v10.91.14 to master without authorization, reasoning from an earlier version's "promote to prod" + a `/goal` mention of staging + green gates. Wrong on every axis; production restart killed a user's in-flight dep-map job. This section was hardened in response.

### Security-Sensitive Commit Discipline (Story #929)

Security-sensitive changes (permission-model edits, prompt-template edits for capability-granted agents, auth-boundary changes) MUST be isolated in their own commit -- never bundled with unrelated work. Raise in code review when violated.

---

## Testing

### Test Suites -- All Must Pass Before Work Is Done

| Suite | Scope | When Required | Time |
|-------|-------|---------------|------|
| `fast-automation.sh` | CLI, core logic, chunking, storage (ignores `tests/unit/server/`) | ALL changes | ~27 min (grows faster than test count -- re-measure) |
| `server-fast-automation.sh` | Server (MCP/REST/services/auth/storage), 6 parallel chunks | Touching `src/code_indexer/server/` | ~12 min (chunk 1 `services/` is the long pole) |
| `slow-automation.sh` | `@pytest.mark.slow` unit tests (Bug #1798) | Not yet in the required gate sequence | ~45 min |
| `rust-automation.sh` | Rust X-Ray engine: `cargo test --workspace` (447 tests incl. AC18 PREAMBLE parity) + `cargo clippy --workspace --all-targets -D warnings` | Touching `rust/` | seconds warm |
| `e2e-automation.sh` | 6-phase E2E (CLI standalone/daemon, server in-process, CLI remote, fault-injection, PostgreSQL parity). No mocks. | Final regression gate -- ALL completed work | ~45-90 min |

`fast-automation.sh` does NOT run server tests -- touching server code without `server-fast-automation.sh` = untested. All three pytest suites ignore `rust/` -- touching `rust/` without `rust-automation.sh` = untested. `e2e-automation.sh` (Epic #700) is non-negotiable for epic/story completion; pure doc/config edits may waive with explicit user approval. The `@pytest.mark.slow` marker routes a test INTO `slow-automation.sh`, not nowhere -- confirm that lane covers its path.

### Hierarchy

1. Targeted tests (seconds): `pytest tests/unit/.../test_X*.py -v --tb=short`
2. Manual testing
3. `fast-automation.sh` (zero failures)
4. `server-fast-automation.sh` when server code touched
5. `rust-automation.sh` when `rust/` touched
6. `e2e-automation.sh` (final gate)

**Long-suite running traps** (fast-/server-fast-automation): the Bash tool caps at 600000ms, so a foreground `timeout 900` is silently truncated to 10 min and kills a healthy run -- launch in the BACKGROUND and poll. Judge completion from the log's `EXIT=` line, NEVER the background-task notification (a `{ ./script; echo "EXIT=$?"; } > log` wrapper reports the wrapper's exit 0 even when the run failed). A mid-run `grep -c '^FAILED'` is always 0 (pytest emits FAILED only in the end summary). Don't use `pgrep -f fast-automation` as the liveness test (the polling shell matches its own pattern). A timeout is NOT automatically a hang -- check the actual duration against the baseline first.

### fast-automation.sh Remediation

- NEVER "continue monitoring" after the 15-min timeout -- the process is dead.
- Per-test thresholds: `<5s` target, `>10s` investigate, `>30s` MUST mark `@pytest.mark.slow` (confirm `slow-automation.sh` actually selects its path).
- Fix root cause, not symptoms. Failures on untouched code = regression.
- For a test that has never run in a gate, "it passes now" is not acceptance -- prove it still fails when the behavior it guards is deliberately broken (the first slow-lane run found real silent rot: a stale FastAPI dependency-override and a defeated `Path.home()` patch, each degrading a whole file to a uniform plausible-looking pass/fail).

### e2e-automation.sh Usage

```bash
./e2e-automation.sh              # All 6 phases
./e2e-automation.sh --phase 1    # CLI standalone
./e2e-automation.sh --phase 2    # CLI daemon
./e2e-automation.sh --phase 3    # Server in-process (FastAPI TestClient)
./e2e-automation.sh --phase 4    # CLI remote (live uvicorn subprocess)
./e2e-automation.sh --phase 5    # Fault-injection resiliency (live fault server)
./e2e-automation.sh --phase 6    # PostgreSQL parity (port 8901)
```

Credentials from `.e2e-automation` (gitignored) or env: `E2E_ADMIN_USER`, `E2E_ADMIN_PASS`, `E2E_VOYAGE_API_KEY`. Exits immediately if admin credentials missing. (The fresh E2E servers seed the default `admin`/`admin` account -- not the dev/staging admin password.)

### Post-E2E Log Audit (MANDATORY)

Story #1122 automated the log-audit gate for Phase 3 and Phase 4 as session-scoped autouse fixtures: they query `admin_logs_query` via the MCP front door and fail the phase on any new non-allowlisted ERROR/WARNING above the phase-start watermark. For Phases 1, 2, 5 (no automated fixture), manually query: `sqlite3 ~/.cidx-server/logs.db "SELECT * FROM logs WHERE level IN ('ERROR','WARNING') ORDER BY id DESC LIMIT 50"`. Zero new entries attributable to your changes before declaring done. Gate: `tests/e2e/log_audit_gate.py` (allowlist `LOG_AUDIT_ALLOWLIST`), `tests/e2e/{server,cli_remote}/conftest.py`.

### Server E2E Testing -- Front Door Only (MANDATORY)

Testing the server end-to-end (local or staging), ALL tests MUST exercise the REST API / MCP front door (HTTP to `/api/query`, `/api/admin/golden-repos`, `/auth/login`, MCP JSON-RPC, etc.). NEVER use CLI tools (`cidx init/index/query`) or SSH shell commands as the primary test mechanism -- the CLI is a separate client and bypasses the whole HTTP stack (auth, routing, middleware, serialization), giving false confidence. CLI/SSH allowed ONLY for troubleshooting, double-checking a behavior, inspecting logs, or verifying process state. See memory: `feedback_server_e2e_front_door_only.md`.

### Lint and CI

```bash
./lint.sh                         # ruff check, ruff format check, mypy, AC15 anti-orphan
git push && gh run list --limit 5
gh run view <run-id> --log-failed
```

Zero tolerance -- never leave GitHub Actions failed; fix in the same session. Every story DoD requires `./lint.sh` exit 0 before merging to `development`. See memory: `feedback_ruff_black_version_alignment.md`.

What CI runs (`.github/workflows/main.yml`, the only workflow):

| Job | What it runs | Real gate? |
|-----|--------------|-----------|
| `lint` | full `./lint.sh` (ruff check + format + mypy over `src/` AND `tests/`, + AC15), Python 3.9 | YES |
| `test` | SMOKE only -- 3 files across a 4-version matrix | NO |
| `rust` | FULL Rust workspace: `cargo test --workspace` (447) + `cargo clippy -D warnings` | YES (complete suite) |
| `create-tag` / `create-release` | gated on `[check-version, lint, test, rust]` | tag cannot be cut from a red tree |

**A green CI badge does NOT mean the Python suite passed** -- for Python it means lint + 3 smoke files. The real Python gates are LOCAL (`fast-`, `server-fast-`, `e2e-automation.sh`). Rust is the exception: CI's `rust` job IS the full gate; `rust-automation.sh` is its local mirror.

Three CI sync constraints, all learned by breaking them:
1. `lint` pins `ruff`/`mypy` to the exact `.pre-commit-config.yaml` versions (pyproject only FLOORS them; an unpinned newer ruff formatter reddens a clean tree).
2. `lint` runs on **Python 3.9**, tracking `[tool.mypy] python_version`. `no_site_packages=false` means mypy PARSES third-party sources under that target -- a newer interpreter installs deps whose syntax 3.9 can't parse and the gate dies inside `site-packages`. Do NOT modernize to 3.12 without moving the mypy target. (mypy also flags `attr-defined` on a private third-party attr the CI-installed version lacks -- access such internals via `getattr(mod, "_X", fallback)`, not a bare import.)
3. `rust` pins `dtolnay/rust-toolchain@<exact>` which must track `rust/rust-toolchain.toml`'s `channel`; rustup auto-installs it for any `cargo` invocation with cwd inside `rust/`, in CI and locally alike. Do not revert either to floating.

**pre-commit mypy is stricter about `Any` returns than `lint.sh`** -- run `pre-commit run mypy --files <changed>` before committing.

---

## Critical Architecture Invariants

Full detail for every entry below lives in `docs/architecture-invariants.md` (and the per-topic docs each entry names).

### Production Scale — DESIGN EVERYTHING FOR IT

**Production runs ~900 repositories with ONE operator and no ops team.** Design against that number, never against the ~30-repo dev server (3% of production). A local measurement proves CORRECTNESS at best; it proves NOTHING about fleet-scale behaviour. Work instantaneous on 30 repos can freeze production for minutes.

| Rule | Why |
|------|-----|
| NEVER call a synchronous filesystem/network function directly inside `async def` | Blocks the WHOLE event loop -- offload with `anyio.to_thread.run_sync(...)` (idiom: `_run_orphan_sweep` in `startup/lifespan.py`). |
| Any O(number of repos) work must be offloaded AND paced | ~18 fs ops/repo x 900 = ~16,000 NFS metadata ops (~80s at 5ms). |
| Never put O(fleet) work on the STARTUP path unbacked | Delays readiness fleet-wide; a failure there takes the node down at boot. |
| Treat `hard` NFS as able to block FOREVER | cow-storage is `hard` NFSv3: `os.stat` blocks in uninterruptible kernel retry when the host is unresponsive -- a permanently hung node on the event loop. |
| No settings, no manual steps, no babysitting | One operator cannot flip switches or sweep leftovers across 900 repos. |
| Cleanup/repair must self-heal and converge | Anything left behind accumulates permanently. |

Before shipping anything touching repos, ask "what does this do at 900?" for BOTH time and blocking. In review, treat a sync I/O call inside `async def` as a defect regardless of local speed. (Concrete near-miss: the Bug #1567/#1570 snapshot sweep, a sync fs walk inside `async def lifespan`, would block ~80s/boot at 900 and hang forever on a wedged NFS host.) See memory: `feedback_design_for_900_repo_scale.md`, `feedback_no_settings_to_gate_bug_fixes.md`.

### Cluster-Aware State — ABSOLUTE RULE

**NEVER use module-level dicts, class-level dicts, or any per-node RAM for state that must be visible to another HTTP request in a cluster.** Under HAProxy round-robin a write to `mydict = {}` in `routes.py` lives only on the node that handled the request; a later request on another node sees nothing. HAProxy affinity is NOT a substitute -- correctness must not depend on proxy config.

| State type | Correct store | WRONG |
|------------|--------------|-------|
| Cross-request ephemeral payload (search snippets, job results) | `app.state.payload_cache` (`PayloadCache` — SQLite solo, PostgreSQL cluster) | module-level dict |
| Job coordination / dedup | BGM `JobTracker` (PostgreSQL in cluster) | `bgm.jobs.values()` scan (per-node) |
| Long-lived config / metadata | `get_config_service().get_config()` (DB-backed) | env vars, module vars |
| Shared sentinel / coordination lock | `SharedJobSentinel` on cidx-meta NFS | per-node file or dict |

`PayloadCache` (wired at `app.state.payload_cache` in lifespan; PG `payload_cache` table in cluster; TTL-evicted, default 900s) is the designated cross-node store. Key methods `store_with_key`/`has_key`/`retrieve`. **Bug #1181**: the query hot path must use `store_batch(contents) -> handles` (ONE transaction, `SET LOCAL synchronous_commit=off`), NEVER `store()` per result in a loop; any new query-path truncation helper must use `store_batch`. **Registered-but-unwired trap (Bug #1665)**: a `*PostgresBackend` sitting on `BackendRegistry` does nothing if consumers still construct their own object with a bare SQLite path -- wire via `resolve_backend_registry_attr(attr_name, caller_name=...)` (`server/utils/registry_factory.py`) at each construction site, and grep every `XCache(db_path)`/`XManager(db_path)` before declaring registry wiring complete. Applies to ALL contexts incl. reviewers. See memory: `feedback_cluster_aware_state_only.md`.

-> Detail: docs/architecture-invariants.md#cluster-aware-state

### Module-Level Service Singletons Must Be Lazy (PEP 562) (Bug #1638, Bug #1650)

NEVER bind a heavy service to a bare module-level name (`foo = HeavyService()`) that runs at import time -- any import then pays full construction (DB loads, `bgm-worker` threads) as a side effect. **A module-level `__getattr__` deferral of the BINDING is necessary but NOT sufficient**: PEP 562 fires on `from module import name` too, so any consumer's module-scope import still forces construction. **The actual fix makes the CONSTRUCTOR cheap ("Option A")**: defer expensive sub-constructions inside `__init__` into lazy properties (getters+setters) guarded by a CLASS-LEVEL `threading.RLock` (never a plain `Lock` -- re-entrant same-thread probes must re-acquire; class-level so `Cls.__new__(Cls)` test instances still have a lock). Keep the layer-1 module `__getattr__` (RLock + `_initialized`/`_initializing` sentinels + `_lazy_values` snapshot dict) as defense-in-depth. Verify with the issue's OWN repro (import a real MCP handler -> assert zero threads/DB loads) plus a re-entrancy discriminating test. Canonical: `server/app.py` (layer 1 only), `server/services/git_operations_service.py`, `server/services/file_service.py` (both layers). Distinct from Bug #1467/#1468 (which is about avoiding heavy cross-layer IMPORTS, not eager construction).

-> Detail: docs/architecture-invariants.md#module-level-singletons

### Shared-Storage Protocol Is Pinned to NFSv3 — NFSv4 Is Off The Table

Cluster mounts (golden-repos, cow-storage) are pinned to NFSv3 (`vers=3,nolock,hard` / `soft,timeo=30,retrans=3`). NFSv4 was deployed and rolled back after three live failures (lock loss, git pack corruption, state-recovery hangs) -- do not propose it without addressing all three. Direction: need LESS from the filesystem (coordination moved to PostgreSQL); any storage proposal must preserve local `cp --reflink`.

-> Detail: docs/architecture-invariants.md#shared-storage-protocol-nfs

### Query Is Everything

Query capability is the core product value. NEVER remove or break: query functionality, git-awareness, branch-processing optimization, relationship tracking, indexing deduplication. If a refactor removes any, STOP. See memory: `project_query_is_everything.md`.

### X-Ray (lazy-load, sandbox, engine, cache identity, ABI)

- `tree_sitter`/`tree_sitter_languages` imported ONLY inside `AstSearchEngine.__init__` (CI-gated by `tests/unit/xray/test_lazy_load.py`); raw `tree_sitter.Node` NEVER exposed to evaluator code (wrap in `XRayNode`).
- **Compile cache identity (Bug #1784)**: the key is `compute_cache_identity(assembled_source, XRAY_ABI_VERSION, rustc_version)` (`compiler.rs`) -- SHA-256 over the FULL assembled source (PREAMBLE + user code + EPILOGUE) + ABI + rustc, NEVER `sha256(user_code)` alone. Any PREAMBLE/EPILOGUE refactor MUST go through this fn. Python obtains it ONLY via `xray-cli --print-cache-identity` (no re-implementation). Written as the existing `source_hash` PK, so an ABI bump makes a new row. TTL is read-only enforced; deletion is lazy (do not say "ages out via TTL"). `XRAY_ABI_VERSION` has ONE definition (`compiler::XRAY_ABI_VERSION`); PREAMBLE uses a placeholder substituted at assemble time. The identity subprocess runs at most once per `run_batch()`, its timeout clamped to the caller's remaining deadline, never in solo/CLI mode; every failure path records `cidx.xray.cache_identity_failures`.

-> Detail: docs/architecture-invariants.md#x-ray | docs/xray-architecture.md | docs/xray-sandbox.md

### Auth: TOTP Elevation / JWT Logout / Maintenance Mode

- **TOTP step-up (Epic #922/#980)**: three error codes exactly -- `totp_setup_required` (403), `elevation_required` (403), `elevation_failed` (401); kill switch returns 503 NOT 403. `with_elevation_retry` wraps all `cidx admin users`/`groups` (single retry on `elevation_required`).
- **JWT logout (Story #1163)**: both logout routes blacklist the `jti` via `get_token_blacklist().add(jti)` (DB-backed, cross-node); try/except-wrapped, never blocks the redirect; `blacklisted_at` is a NUMERIC unix timestamp.
- **Maintenance mode (Epic #922/#924)**: write endpoints (`POST .../maintenance/enter|exit`) are loopback-only via `require_localhost`; reverse-proxy must NOT forward them; MCP enter/exit tools removed.

-> Detail: docs/architecture-invariants.md#auth-totp-jwt | docs/totp-elevation.md

### Golden Repo and Versioned Snapshots

- **Versioned path trap**: NEVER modify/checkout/index inside `.versioned/`. `GoldenRepoManager.get_actual_repo_path` may return the MUTABLE base clone -- prove immutability with `is_immutable_versioned_snapshot(path)` (short TTL otherwise). Alias JSON `target_path` is authoritative for global repos. See memory: `feedback_versioned_path_trap.md`.
- **Canonical predicate (Bug #1084)**: `is_versioned_snapshot(path, *, mount_point=None)` (`server/storage/shared/snapshot_paths.py`) is the sole authority -- never reimplement the `.versioned` test. Deletion behind the QueryTracker refcount-zero gate; keep-last-N retention (`snapshot_retention_keep_last`, default 3) never deletes current/previous.
- **Registry-orphan guard (Bug #1317)**: a `golden_repos` row must never lack an on-disk clone or alias pointer. Provisioning all-or-nothing; removal deletes the row BEFORE files; `reconcile_golden_repo_registry` self-heals behind a health-gate + mass-deletion circuit-breaker (never remove >half the fleet) + 3-sweep confirmation counter, surfaced on `/health`.
- **Activation branch-delta reindex (Bug #1203)**: non-default-branch activation/switch/sync runs `ActivatedRepoManager._run_branch_delta_index` (skip on default branch, `-global`, `_index_manager is None`). `_index_manager` is wired POST-HOC in `startup/lifespan.py` -- removing that makes it inert. Failed reindex raises `ActivatedRepoError`.
- **clone_backend wiring (Story #1034/Bug #1044)**: CoW clones route through `self._clone_backend.create_clone_at_path(...)` (hard-raises if None), wired POST-HOC in `lifespan.py` (`arm._clone_backend = snapshot_manager._clone_backend`); preserve that assignment (guard `test_lifespan_clone_backend_wiring_bug1044.py`).
- **Temporal enable-flag reconciliation (Bug #1390)**: `enable_temporal` between `golden_repos_metadata` and `global_repos` is ONE-WAY -- stored `True` downgrades to `False` when no real data on disk, but `False` is NEVER auto-flipped to `True` (an operator disable is never silently reversed).

-> Detail: docs/architecture-invariants.md#golden-repo-and-versioned-snapshots

### Query Path and Embedding Caches

- **Timeouts (Issue #1398)**: `SearchTimeoutsConfig` is the sole Web-UI-configurable source for MCP/query + provider + reranker timeouts (no hardcoded constants). `regex_search` and exempt siblings deliberately bypass the dispatcher's `asyncio.wait_for` (governed by ripgrep's own timeout) -- read the rationale before changing either half.
- **Drift-safe caching (Story #1082)**: `query_path_cache.py` (`TTLCache`, single-flight, bounded LRU) -- ZERO staleness for static model-spec YAML + proven-immutable snapshots; SHORT TTL for mutable/DB-metadata; NEVER cache auth-bearing rows (api keys, users, MCP creds, permissions, tokens) so revocation is immediate.
- **Query-embedding cache (Epic #1103)**: server-side, wraps `coalesced_query_embedding` outside-in; CLI/solo bypass. HARD: NEVER lowercase the key; NEVER cache auth-bearing data; query-purpose embeddings ONLY; all ops fail-open (WARNING + live path).
- **FSV skip_staleness_check (Bug #1181)**: `FilesystemVectorStore.__init__(skip_staleness_check=False)` default; only `FilesystemBackend.get_vector_store_client()` sets True, and ONLY when `is_immutable_versioned_snapshot(project_root)` proves it. Never skip for an unproven path.
- **Embedding coalescer + 4-lane governor (Story #1079)**: server-side query-embed coalescing behind a self-tuning 4-lane (`{provider}:{embed|rerank}`) concurrency governor; CLI/solo untouched (registry None). One sealed batch == exactly ONE provider HTTP call. `provider_backoff.is_rate_limited` is the canonical 429 classifier -- NEVER re-mask a 429. ALL query-path embed calls pass `embedding_purpose="query"` (Bug #1104). Registry built once in `lifespan.py`; preserve `set/clear_coalescer_registry`.

-> Detail: docs/architecture-invariants.md#query-path-and-embedding-caches | #embedding-coalescer-and-governor | docs/query-embedding-cache.md

### Indexing and Migrations

- **No job/subprocess/per-file timeouts (Bug #1218)**: the indexing / golden-repo-registration / SCIP path carries NO wall-clock timeout on the job, subprocess, or any per-file/batch unit -- a large repo legitimately takes hours. The ONLY legitimate timeout is the per-request outbound embedding HTTP call (+ retry/backoff). NEVER add a clock; NEVER `except TimeoutError: skip` (silent partial index). Fail LOUD: `cidx index` exits non-zero when `files_processed == 0 and failed_files > 0`.
- **Per-commit temporal dual-embedder (Epic #1289)**: per-commit-aggregated (message once + all changed-file diffs in ONE doc per commit, `{project}:commit:{hash}:{j}` ids) under coexisting embedder adapters (`voyage-context-4` 0% overlap, `embed-v4.0` 15%), quarterly-sharded per embedder. Incremental refresh is reconcile-based (full git-log walk, disk-scan skip per shard). Bug #1405: blank-out SKIPS the shared bookkeeping dir (bare `code-indexer-temporal`) via a data-presence discriminator, never amputating the shared `TemporalMetadataStore`.
- **Temporal all-branches gate (Story #1412)**: golden-repo temporal tracks ONLY the registered branch by default; `all_branches` is shipped DISABLED behind runtime flag `temporal_all_branches_enabled: bool = False` (no env var). Gate-off + a request with `all_branches=true` -> reject loudly at three front doors (REST `add golden-repo`, Web `save_temporal_options`, MCP `add_golden_repo`); the three command builders skip `--all-branches` + WARN on a stored legacy `true`. Reversible with no re-index. Standalone CLI `--all-branches` untouched.
- **Temporal writes CHUNKS_DB, fixed path (Bug #1528/#1529)**: temporal indexing writes CHUNKS_DB by default (legacy shards migrated in place first, never a physical-absence check). Server-context temporal data lives at a FIXED path outside the clone (`{golden_repos_dir}/.temporal/{alias}/...`) via ONE seam `resolve_temporal_index_dir`; both read seams derive from the GOLDEN alias (never an activated CoW clone) and FAIL LOUD rather than fall back. HNSW freshness after in-place refresh via a stat fingerprint (mtime+size+inode+dev). Story #1457 sister-location placement is RETIRED -- do not resurrect `TemporalShardResolver`/`maybe_relocate_shard_to_sister_location` (silent data loss).
- **HNSW orphan detect/repair (Epic #1333)**: every build/finalize runs detect+repair-orphans before persisting; health exposes `orphan_count` as strict binary (0 OK, >0 ERROR, no WARNING tier). Missing custom-hnswlib-fork capability DEGRADES (skip + one WARNING, proceed) -- never a hard-abort `HNSWCapabilityError`. The fleet orphan repair sweep (`server/services/hnsw_orphan_sweep/`, ON by default `batch_size=15`/`tick_interval_minutes=7`) reuses `list_golden_repos()`/`list_all_activated_repositories()`, acquires the SAME `.index_rebuild.lock`, invalidates `HNSWIndexCache` on success; durable cursor is a STRING stable sort key (never a numeric offset); dedup is `register_job_if_no_conflict` ONLY (NOT `ShardOwnership.owns()`); one short job PER TICK.
- **Chunk storage layout (Epic #1454)**: legacy SHARDED_JSON (`vector_*.json`) and CHUNKS_DB (one `chunks.db` per collection) both fully supported. `resolve_chunk_layout()` is the SOLE authority -- never probe for `chunks.db` directly; write/finalize sites MUST use `_is_chunks_db_collection()` (not the bare resolver) to avoid misclassifying a fresh build. `id_index.bin` is retired for CHUNKS_DB (its absence is expected, not a warning). `run_fleet_migration_for_repo(...)` consolidates one repo at a time behind write-lock checks; `consolidate_collection_in_place()` is write-verify-flip-then-delete, crash-safe/idempotent. Both destructive deletion primitives are gated behind `fleet_migration_config.enabled` (default OFF) via `deletion_authorized` re-resolved from config; flipping ON needs manual operator confirmation that every node runs the dual-layout reader. Every existence/health/status check MUST route through `resolve_chunk_layout()` + read-only `chunk_store_has_real_data()` (never a bare `rglob`, never a mutating `ChunkStore` open).
- **Bug #1467/#1468**: incremental (git-diff) file discovery MUST use `FileFinder.matches_exclude_pattern()` (same rules as full-walk). Importing `FilesystemVectorStore` alone must NEVER pull in `psycopg`/`fastapi` -- use lazy `TYPE_CHECKING` + PEP 562 `__getattr__`.
- **Migrations backward-compatible**: rolling restarts share schema. Allowed: `CREATE TABLE/INDEX IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`, new nullable/defaulted columns. NEVER: `DROP TABLE`, `DROP COLUMN`, `RENAME`, `ALTER COLUMN TYPE`, removing NOT NULL. Under `--workers N` (Story #1164), `MigrationRunner.run()` takes a PG SESSION advisory lock (`pg_advisory_lock`, key `_MIGRATION_ADVISORY_LOCK_KEY`) at entry, releases in `finally`, always parameterized `%s`; SQLite path never references it.
- **JSONB/TEXT normalization (Bug #1622/#1652/#1655)**: any column that is JSONB in PG and TEXT in SQLite MUST be read through `parse_json_column(raw, expected_type, field_name)` (`server/storage/json_column.py`), never a bare `json.loads()` (psycopg pre-deserializes JSONB; `json.loads` on a dict raises `TypeError`). Do not reintroduce a fourth copy.

-> Detail: docs/architecture-invariants.md#indexing-and-migrations | #epic-1454-chunk-storage-consolidation-and-fleet-migration

### Config, Auto-Updater, Pace-Maker

- **No env vars for server settings**: runtime settings belong in the Web UI Config Screen via `get_config_service().get_config()`. Never `os.environ["CIDX_SETTING"]`.
- **Config bootstrap vs runtime (Story #578)**: `config.json` is BOOTSTRAP ONLY (`server_dir`, `host`, `port`, `workers`, `log_level`, `storage_mode`, `postgres_dsn`, `ontap`, `cluster.node_id`); runtime settings in DB. NEVER call `ServerConfigManager().load_config()` -- use `get_config_service().get_config()`.
- **Auto-updater idempotent deployment (ABSOLUTE, NO EXCEPTIONS)**: any bootstrap change (systemd unit, env, PATH, file locations, service wiring) MUST be automated in BOTH the installer (`scripts/install-cidx-server.sh` / `server/auto_update/templates/`) AND the auto-updater (an idempotent `_ensure_X_config()` self-heal in `deployment_executor.py`). A template/installer-only fix is NOT done -- Bug #1440 left 3 already-running nodes silently broken because nothing re-renders a deployed unit. A live-host bootstrap gap is fixed only when an automated self-heal provably repairs that host via the REAL auto-update firing naturally (not manual SSH). Flow: `git pull` -> `pip install` -> `DeploymentExecutor.execute()` -> `systemctl restart`. See memory: `feedback_bootstrap_changes_need_installer_and_autoupdater.md`.
- **Pace-Maker guard (Story #997)**: auto-updater installs/updates pace-maker (fresh install = master switch OFF; updates never touch config). Config split `pace_maker_clone_path` (bootstrap) + `pace_maker_mode` (runtime Web UI, default `"disabled"`); three-way `enforce_pace_maker_config()`. Injected at `ClaudeInvoker.invoke()` and `ResearchAssistantService._run_claude_background()` (NOT CodexInvoker); non-fatal.

-> Detail: docs/architecture-invariants.md#auto-updater-and-pace-maker | docs/auto-update.md

### Dep-Map, cidx-meta, Description-Refresh

- **Resumable delta (Story #1053)**: `run_delta_analysis` is resumable via a per-domain YAML frontmatter journal (`last_delta_applied`), frontmatter+body in one atomic `os.replace` (no cursor file). Cluster correctness inherits the `cidx-meta` `WriteLockManager` lock.
- **Re-entrancy sentinels (Story #1035)**: dep-map coordination state lives on NFS-shared `cidx-meta` (`SharedJobSentinel`, atomic `O_CREAT|O_EXCL`) -- NEVER per-node SQLite. Two op_type families (`analysis` 4h, `dashboard` 30m). Claim order: `is_available()` -> `try_claim()` -> `register_job_if_no_conflict` -> spawn worker (`pre_claimed=True`).
- **Parser split (Story #887)**: four modules; anomalies self-classify via `AnomalyType.channel`; dual API `get_cross_domain_graph()` (2-tuple) / `_with_channels()` (4-tuple); self-loop preservation unconditional.
- **cidx-meta backup (Story #926, superseded by Bug #1555)**: sync runs BEFORE indexing; all git ops on the mutable base (`get_cidx_meta_path()`), NEVER inside `.versioned/`. Bug #1555 made the remote a passive BACKUP MIRROR -- `sync()` publishes local HEAD via `git push --force-with-lease` (no fetch-then-rebase, no conflict resolution); a diverged remote self-heals next cycle. `conflict_resolver.py` and the quarantine surface were deleted (tables stay empty per never-drop-tables).
- **Phase 3.7 graph-channel repair (Epic #907)**: repairs SELF_LOOP/MALFORMED_YAML/GARBAGE_DOMAIN_REJECTED deterministically, BIDIRECTIONAL_MISMATCH Claude-audited; bootstrap flag `enable_graph_channel_repair` (default True); append-only JSONL journal.
- **Description-refresh**: circuit-breaker quarantines a repo after `PROMPT_FAILURE_QUARANTINE_THRESHOLD = 3` consecutive failures, auto-clear ONLY on a real on-disk commit change. Cross-worker dedup MUST use `register_job_if_no_conflict` (DB `idx_active_job_per_repo` is the cluster-atomic arbiter; handle `DuplicateJobError` before the generic `except`). Scheduler shares the SAME `tracking_backend` as `meta_description_hook` (wired in `lifespan.py`). The single live path is the lifecycle-unified pipeline (`LifecycleBatchRunner._process_one_repo` -> `LifecycleClaudeCliInvoker`); a refresh REFINES the existing description (Bug #1094); frontmatter merge preserve-by-default (Bug #1101); descriptions are timeless -- temporal phrasing BANNED (Bug #1102). See memory: `feedback_description_refresh_scheduler_requires_staging_validation.md`.

-> Detail: docs/architecture-invariants.md#dep-map-and-cidx-meta | #description-refresh | docs/cidx-meta-backup.md

### Global Repo Alias Fallback (Story #1039)

31 read-only MCP handlers promote a bare alias to its `-global` form when the user lacks it and the golden repo is globally active -- via `try_global_fallback()` (`_global_fallback.py`), pre-check pattern, activated-repo takes precedence. All write/mutation handlers MUST stay strict: `_global_fallback.py` MUST NEVER be imported from them.

-> Detail: docs/architecture-invariants.md#global-repo-alias-fallback

### Server Memory (Bug #878/#881/#897)

Cleanup daemon once per app lifetime (started/stopped in lifespan; never piggyback in `get_connection()`). HNSW/FTS cache `DEFAULT_MAX_CACHE_SIZE_MB = 4096`; `initialize_caches(worker_count)` divides the per-node cap by `config.workers` (floor 256 MB) in `service_init.py` BEFORE the eager getters -- single source of truth, do NOT add a second call in `lifespan.py`. Bug #897 malloc mitigations default ON.

-> Detail: docs/architecture-invariants.md#server-memory-and-pooling | docs/server-memory-invariants.md

---

## Operational Modes

| Mode | Storage | Use Case |
|------|---------|----------|
| **CLI** | FilesystemVectorStore (`.code-indexer/index/`) | Single dev, local |
| **Daemon** | Same + in-memory cache, Unix socket `.code-indexer/daemon.sock` | ~5ms cached vs ~1s disk |

Container-free, instant setup. Git-aware: blob hashes (clean) / text content (dirty). VoyageAI dims: 1024 (voyage-code-3), 1536 (voyage-large-2). **Server mode**: separate deployment; cluster (`storage_mode: postgres`) shares PostgreSQL. See `docs/server-deployment.md`, `docs/cluster-architecture.md`.

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

**Flags** (always `--quiet`): `--limit N` (start 5-10), `--language python`, `--path-filter */tests/*`, `--min-score 0.8`, `--accuracy high`. Note: `*/tests/*` matches at any depth including root; `**/tests/**` is equivalent.

---

## Performance Rules

- **NEVER** add `time.sleep()` to production. See memory: `feedback_no_sleep_in_production.md`.
- **Progress reporting is delicate** -- ask confirmation before ANY changes. See memory: `feedback_progress_reporting_delicate.md`.
- **FTS lazy import**: NEVER import Tantivy/FTS at module level in CLI startup files -- use `TYPE_CHECKING`. Verify: `python3 -c "import sys; from src.code_indexer.cli import cli; print('tantivy' in sys.modules)"` (expect False).
- **Smart indexer**: always consider `--reconcile` (non git-aware) -- maintain feature parity.
- **Tmp files**: `~/.tmp`, never `/tmp`. **Container-free**: no ports, no containers.
- **Import budget**: startup ~329ms.
- **Multi-worker benchmark (Story #1168)**: `scripts/analysis/multi_worker_throughput.py` (operator-only, not CI) measures `POST /api/query` throughput per worker; NEVER restart/kill the dev server on :8000 -- use an isolated port; reports in `reports/perf/`.

---

## Embedding Provider (VoyageAI)

Primary provider; Cohere also supported since v9.8. Tokenizer: `embedded_voyage_tokenizer.py` (NOT the voyageai library). 120k tokens/batch, automatic batching. Models: voyage-code-3 (1024 dims, default), voyage-large-2 (1536 dims).

- **httpx pooling (Story #1083)**: `HttpClientFactory` owns ONE long-lived keep-alive `httpx.Client` for production (`create_sync_client(pooled=True)`, closed once at shutdown; auth per-request). Fault-injection path unchanged (fresh per-call client). `api_metrics_service` batches its backlog into ONE `upsert_buckets_batch()` per drain.
- **OTEL metrics wiring (Story #1586)**: `ApplicationMetrics`/`JobMetrics` are wired across search/FTS/embedding/job/refresh/spans. All call sites use `peek_telemetry_manager()` (NEVER `get_telemetry_manager()`) to avoid the "first call wins, disabled fallback" init race. REST `/api/query` (`inline_query.py`) is instrumented SEPARATELY from the MCP handler (it calls `semantic_query_manager`/`TantivyIndexManager` directly, not `_execute_tracked_search`). Embedding metrics are recorded at each provider's lowest real-HTTP boundary (one event per real attempt incl. retries), never at the public delegating methods (double-count). Repository-counts gauge is an O(1) background-refreshed cache (`_RepositoryCountsCache`), NEVER a synchronous fleet walk (OTEL holds a lock across all callbacks; an O(900) NFS walk would block/discard the cycle). Observable-gauge callbacks must yield an `Observation` object, not a tuple.
- **Log correlation_id (Bug #1641)**: `SQLiteLogHandler` populates `correlation_id` only when a record carries it. `logging_utils.inject_correlation_id(record)` is the single helper, called from `async_logging.IdentityQueueHandler.prepare()` (runs on the request thread BEFORE `enqueue()` -- `contextvars` do not cross the QueueListener thread boundary) and defensively from `SQLiteLogHandler.emit()`. Never overrides an explicit value; never fabricates one.

-> Detail: docs/architecture-invariants.md#server-memory-and-pooling

---

## Server Development

### Local server

```bash
PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app --host <bind-address> --port 8000
pkill -f "uvicorn code_indexer.server.app"
```

`No module named 'code_indexer'` -> missing `PYTHONPATH=./src`. Exits immediately -> port in use.

### E2E REST/MCP gotchas

- Auth: **JSON body** (`-H "Content-Type: application/json"`), NOT form-urlencoded. Endpoint is `/auth/login`, NOT `/admin/login`.
- Golden repo add: returns **HTTP 202** with `job_id` -- poll `/api/jobs/{job_id}`.
- Query field: `"query_text"` (not `"query"`). Global repo suffix: `"-global"`.
- Token expiry: 10 minutes. Timing display: CLI only, not MCP/REST.

### Claude CLI Integration

Two subsystems: **ClaudeCliManager** (queue-based thread pool, batch) and **ResearchAssistantService** (direct thread per request, interactive). **MCP self-registration**: SINGLE source of truth at `invoke_claude_cli` in `repo_analyzer.py` (Story #885 A10) -- NEVER add parallel `ensure_registered()` calls. **Codex/Claude MCP registration**: both use the same persistent `client_id:client_secret` from `MCPCredentialManager` (Claude via HTTP header, Codex via TOML `env_http_headers` + `CIDX_MCP_AUTH_HEADER`); three-step fallback in `build_codex_mcp_auth_header_provider()` handles Claude CLI absence (Bug #937). Codex has no `PostToolUse` hook (no hook parity).

---

## Background Jobs (MANDATORY Checklist)

Any new background job MUST: (1) integrate with `BackgroundJobManager` + `JobTracker` for dashboard/admin visibility; (2) confirm the frontend reporting pattern with the user before implementing. **Auto-discovery pattern (Story #1157)**: `POST /api/discovery/{platform}/start` + `GET .../result/{job_id}` (`web/routes.py`); result storage MUST use `app.state.payload_cache`, never a module-level dict; manual dedup (scan `bgm.jobs.values()`) since `repo_alias=None` bypasses the DB gate.

-> Detail: docs/architecture-invariants.md#background-jobs

---

## MCP Tool Documentation

Externalized to `src/code_indexer/server/mcp/tool_docs/` (YAML frontmatter + markdown). Adding a tool: (1) `TOOL_REGISTRY` in `tools.py`; (2) `python3 tools/verify_tool_docs.py` (CI gate). NEVER run `convert_tool_docs.py` -- see memory: `feedback_convert_tool_docs_destructive.md`.

## SCIP Index File Lifecycle

`cidx scip generate` produces `index.scip.db` (SQLite) from intermediate `index.scip` (protobuf). Original `.scip` is deleted after conversion; only `.scip.db` remains.

## Version Bump

Versioning MAJOR.MINOR.HOTFIX: MAJOR only when the user says "major version" (resets Y.Z); MINOR on normal dev cycles on `development` (resets Z); HOTFIX on production hotfixes on `master` only (never on development). Source of truth: `src/code_indexer/__init__.py` `__version__` (line 9). Also update: `README.md` badge (line 5), `CHANGELOG.md`, `docs/architecture.md`, `docs/query-guide.md`. Verify: `grep -r "OLD_VERSION" --include="*.md" --include="*.py" .`. Do NOT bump `server/app.py` OpenAPI spec or `test-fixtures/`.

## Python Compatibility

Always `python3 -m pip install --break-system-packages` -- never bare `pip`.

## Fault Injection Harness (non-prod only, disabled by default)

Bootstrap-only config (`fault_injection_enabled` + `fault_injection_nonprod_ack`, both false). Enabled without ack OR in production = `sys.exit(1)`. All outbound async HTTP MUST go through `HttpClientFactory`.

-> Detail: docs/architecture-invariants.md#fault-injection-and-memory-retrieval | docs/fault-injection-operator-guide.md

## Memory Retrieval (Story #883)

Parallel pipeline on semantic/hybrid search (VoyageAI vector -> HNSW -> floors -> hydration -> nudge). Kill switch `memory_retrieval_enabled = false` (Web UI, immediate). Path confinement via `Path.relative_to()`; body-hydration faults drop the candidate with WARNING, never raise.

-> Detail: docs/architecture-invariants.md#fault-injection-and-memory-retrieval | docs/memory-retrieval-operator-guide.md

---

## Further Reading

- Architecture: `docs/architecture.md`
- Architecture invariants (detailed): `docs/architecture-invariants.md`
- Server deployment: `docs/server-deployment.md`
- Cluster architecture: `docs/cluster-architecture.md`
- Fault injection: `docs/fault-injection-operator-guide.md`
- Memory retrieval: `docs/memory-retrieval-operator-guide.md`
