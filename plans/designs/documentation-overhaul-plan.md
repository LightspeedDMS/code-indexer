# Documentation Overhaul Plan

Status: COMPLETE on `feature/docs-overhaul` (2026-10-06). Every row below is VERIFIED: reworked to its disposition,
fact-checked claim by claim against the code by an independent reviewer, and committed. Dispositions were validated
by Codex (5 runs) before execution. Open decisions in section 10. Defects found during the rewrite were filed
(public issues and security findings; ids in the gitignored companion file, section D).
Baseline: v12.83.0, 2026-10-06.
Validation reports (gitignored): `reports/reviews/doc-overhaul-validation-{A,B,C,D1,D2}-2026-10-06.md`.

Decisions taken (2026-10-06): folder layout for EVERY doc; epic and stories in the PRIVATE repo (`origin`);
MCP tool docs in scope, including a re-review of each tool's implementation.
Execution (2026-10-06): all work on branch `feature/docs-overhaul` (from `development` @ 2991bc1d5), merged later,
after the parallel security remediation lands. Codex is unavailable (credits): every review gate uses Claude
reviewers (code-reviewer for code/tooling, fact-checker for doc accuracy). CLI examples are executed against THIS
tree (`PYTHONPATH=<repo>/src python3 -m code_indexer.cli ...`), never the editable install (it points at another clone).

## 1. Disposition vocabulary

Every tracked doc gets exactly ONE disposition code.

| Code | Meaning | Obligation |
|------|---------|------------|
| UPDATE | Keep, correct to current behaviour | every claim verified against code |
| REDUCE | Keep, cut to current scope (accuracy fixes implied) | remove history narration, deprecated sections, duplicated content |
| REORG | Keep, restructure, split or move | each resulting doc listed in the target column |
| MERGE | Content folded into another doc, file removed | surviving content moves first; inbound refs repointed |
| DROP | Delete; content obsolete | prove nothing live depends on it (refs, tests, error messages, include_str!) |
| ARCHIVE | Point-in-time doc, not maintained | disclosure scrub FIRST, then move to `docs/archive/` with a "historical, not maintained" banner; otherwise verbatim |
| AS-IS | Keep unchanged (verify only) | immutable by convention (ADRs) or out of scope |
| NEW | Doc to be created | listed in section 4 |
| GEN | Reference produced from code, not hand-written | generator script + lint/CI drift check |

## 2. Target information architecture

```
README.md, CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, CHANGELOG.md   stay at root (GitHub conventions)
docs/README.md                 NEW: documentation map, one line per doc, by audience
docs/getting-started/          installation, configuration (CLI), operating modes, teach-ai, MCP registration
docs/guides/                   query, temporal, SCIP, X-Ray cookbook, meta-repo discovery, remote CLI, forge/write tools, providers
docs/server/                   operator: deployment, upgrading, cluster, CoW storage, auto-update, hnswlib build, admin, jobs, observability
docs/server/auth/              accounts and access, login and elevation, OIDC
docs/server/siem/              operations, secops-guide, curl-runbook, event-catalog (GEN)
docs/reference/                GEN: CLI, MCP tools, server settings (bootstrap vs runtime), error codes; REST guide
docs/architecture/             overview, invariants, indexing, storage, repository lifecycle, refresh recovery, query path,
                               jobs and cluster state, config state, dependency map, cluster, xray/
docs/adr/                      AS-IS + status index
docs/archive/                  ARCHIVE
docs/xray-templates/           STAYS (compiled into the Rust binary via include_str!)
prompts/ai_instructions/       STAYS (installed by `cidx teach-ai`)
```

## 3. Per-doc tracker

Columns: proposed (pre-validation) | Codex verdict | VALIDATED disposition | target | key evidence / inbound deps | depth of
validation (SHALLOW rows need a full claim-by-claim review during the rewrite) | status.

Status values: VALIDATED -> IN-PROGRESS -> DONE -> VERIFIED.

### 3.1 Root (run A / C)

| Doc | Proposed | Codex | Disposition | Target | Evidence / deps | Depth | Status |
|-----|----------|-------|-------------|--------|-----------------|-------|--------|
| README.md | UPDATE+REDUCE | AGREE | REDUCE | README.md (pitch, quickstart, link to docs map) | many fixture strings (noise) | FULL | VERIFIED |
| CONTRIBUTING.md | UPDATE | AGREE | UPDATE | CONTRIBUTING.md; absorbs dev half of dependencies.md | 3 stale claims; lifecycle_unified.md prompt cites it | SHALLOW | VERIFIED |
| SECURITY.md | AS-IS | CHANGE->UPDATE (rejected) | UPDATE (links only) | SECURITY.md | route already = GitHub private vulnerability reporting (enabled, verified via API); Codex conflated it with the internal tracking repo. Only repoint its doc links after moves (D-1 resolved) | FULL | VERIFIED |
| CODE_OF_CONDUCT.md | AS-IS | AGREE | UPDATE | root | personal contact removed, reports go to GitHub Discussions / GitHub report-abuse (D-2 resolved) | FULL | VERIFIED |
| CHANGELOG.md | AS-IS | - | AS-IS | root | historical record | - | VERIFIED |
| CLAUDE.md | out of scope | - | AS-IS (repoint `-> Detail:` links only) | root | 18+ doc pointers | - | VERIFIED |

### 3.2 Getting started and guides (run A)

| Doc | Proposed | Codex | Disposition | Target | Evidence / deps | Depth | Status |
|-----|----------|-------|-------------|--------|-----------------|-------|--------|
| installation.md | UPDATE+REDUCE | AGREE | REDUCE | getting-started/installation.md | deprecated global-registry section; 2 stale claims | SHALLOW | VERIFIED |
| environment-setup.md | MERGE | AGREE | MERGE | -> getting-started/configuration.md | orphan | FULL | VERIFIED |
| dependencies.md | MERGE | AGREE | MERGE | users -> installation; devs -> CONTRIBUTING | orphan | FULL | VERIFIED |
| configuration.md | REORG | AGREE | REORG | CLI -> getting-started/configuration.md; server -> reference/server-settings.md (GEN) | 2 stale claims | SHALLOW | VERIFIED |
| operating-modes.md | UPDATE+REDUCE | AGREE | REDUCE | getting-started/operating-modes.md | test_docs_operating_modes_745.py pins path+content | SHALLOW | VERIFIED |
| ai-integration.md | UPDATE | CHANGE->REORG | REORG | getting-started/teach-ai.md + getting-started/mcp-registration.md | local skill install vs remote MCP are different contracts | SHALLOW | VERIFIED |
| mcp-registration-guide.md | MERGE | AGREE (as sub-page) | MERGE | -> getting-started/mcp-registration.md | orphan | SHALLOW | VERIFIED |
| technical-details.md | REORG then DROP | CHANGE->MERGE | MERGE | CLI ref -> reference/cli.md (GEN); rest -> configuration / architecture | - | SHALLOW | VERIFIED |
| src/code_indexer/query/QUERY_PARAMETERS.md | MERGE | AGREE | MERGE | -> guides/query.md + reference/cli.md | no runtime loader | FULL | VERIFIED |
| query-guide.md | UPDATE | AGREE | UPDATE | guides/query.md | example combines --regex with --semantic (CLI rejects); CLAUDE.md, score_threshold_research.py cite it | SHALLOW | VERIFIED |
| temporal-search.md | UPDATE | AGREE | UPDATE | guides/temporal-search.md | stale shard layout | SHALLOW | VERIFIED |
| scip/README.md | UPDATE | AGREE | UPDATE | guides/scip.md | recommends callchain depth 5-20; cap is 3 (`scip_client.py:19`, MAX_DEPTH_CAP) | SHALLOW | VERIFIED |
| meta-repo-discovery.md | UPDATE | AGREE | UPDATE | guides/meta-repo-discovery.md | `cidx global init-meta` x6 -- command no longer exists | SHALLOW | VERIFIED |

### 3.3 AI instruction content (run A) -- stays at source path

| Doc | Proposed | Codex | Disposition | Evidence | Depth | Status |
|-----|----------|-------|-------------|----------|-------|--------|
| prompts/ai_instructions/cidx_instructions.md | UPDATE | CHANGE->DROP | DROP | not loaded by teach-ai (no src reference to the file) | FULL | VERIFIED |
| prompts/ai_instructions/awareness/awareness.md | UPDATE | AGREE | UPDATE | loaded by teach_ai_templates.py | FULL | VERIFIED |
| prompts/ai_instructions/skills/cidx/SKILL.md | UPDATE | AGREE | UPDATE | installed skill | SHALLOW | VERIFIED |
| .../reference/semantic-search.md | UPDATE | AGREE | UPDATE | | SHALLOW | VERIFIED |
| .../reference/fts-search.md | UPDATE | AGREE | UPDATE | 1 stale claim | SHALLOW | VERIFIED |
| .../reference/temporal-search.md | UPDATE | AGREE | UPDATE | 1 stale claim | SHALLOW | VERIFIED |
| .../reference/scip-intelligence.md | UPDATE | AGREE | UPDATE | same invalid callchain depths as scip guide | SHALLOW | VERIFIED |

### 3.4 Architecture (run B)

| Doc | Proposed | Codex | Disposition | Target | Evidence / deps | Depth | Status |
|-----|----------|-------|-------------|--------|-----------------|-------|--------|
| architecture.md | REORG | AGREE | REORG | architecture/overview.md | outdated storage layout; CLAUDE.md x2, xray tool_docs x2 | SHALLOW | VERIFIED |
| architecture-invariants.md | UPDATE | CHANGE->REORG | REORG | architecture/invariants.md | CLAUDE.md x18, Python and SQL comments cite path | SHALLOW | VERIFIED |
| server-memory-invariants.md | MERGE | AGREE | MERGE | -> architecture/invariants.md | CLAUDE.md x1 | FULL | VERIFIED |
| repository-topology.md | UPDATE | CHANGE->REORG | REORG | architecture/repository-lifecycle.md | misses immutable snapshots, chunks.db | FULL | VERIFIED |
| algorithms.md | DROP | AGREE | DROP | live bits -> architecture/indexing.md | self-declared deprecated | SHALLOW | VERIFIED |
| INDEXING_ALGORITHM.md | MERGE+rewrite | AGREE | MERGE | -> architecture/indexing.md | outdated | SHALLOW | VERIFIED |
| depmap-parser-architecture.md | MERGE | AGREE | MERGE | -> architecture/dependency-map.md | README link | FULL | VERIFIED |
| depmap-phase37-architecture.md | MERGE | AGREE | MERGE | -> architecture/dependency-map.md | | FULL | VERIFIED |
| depmap-resumable-delta-architecture.md | MERGE | AGREE | MERGE | -> architecture/dependency-map.md | | FULL | VERIFIED |
| cidx-meta-backup.md | MERGE | AGREE | MERGE | -> architecture/dependency-map.md | still describes rebase/conflict resolution; code uses --force-with-lease; CLAUDE.md x1 | FULL | VERIFIED |
| query-embedding-cache.md | UPDATE | CHANGE->REORG | REORG | architecture/query-path.md + operator section in reference/server-settings.md | serves maintainers and operators | SHALLOW | VERIFIED |
| query-embedding-cache-empirical-study.md | ARCHIVE | AGREE after scrub | ARCHIVE | archive/ | point-in-time study | SHALLOW | VERIFIED |
| research/hnsw-temporal-orphans-1330.md | ARCHIVE | AGREE after scrub | ARCHIVE | archive/ | tests/utils/hnsw_orphan_corpus.py + test cite it | SHALLOW | VERIFIED |
| hnswlib-custom-build.md | UPDATE | CHANGE->REORG | REORG | server/hnswlib-custom-build.md | path in runtime error strings (3 src files) + 2 tests; disclosure review | SHALLOW | VERIFIED |
| xray-architecture.md | UPDATE | CHANGE->REORG | REORG | architecture/xray/architecture.md | "Java only" -- Kotlin also supported; ~15 Rust refs | SHALLOW | VERIFIED |
| xray-graph-binder-internals.md | UPDATE | AGREE | UPDATE | architecture/xray/graph-binder-internals.md | analyze_graph tool doc cites it | SHALLOW | VERIFIED |
| xray-sandbox.md | UPDATE | CHANGE->REDUCE | REDUCE | architecture/xray/sandbox.md | mostly historical scope; CLAUDE.md x1 | FULL | VERIFIED |
| xray-cookbook.md | UPDATE | AGREE | UPDATE | guides/xray-cookbook.md | COMPILED INTO the Rust binary: `rust/xray-core/src/dynlib.rs:1906` include_str!; move requires Rust edit + rust-automation | SHALLOW | VERIFIED |
| docs/xray-templates/*.rs (8 files) | (not inventoried) | - | AS-IS (stay at path) | docs/xray-templates/ | include_str! in dynlib.rs:1872-1888 | - | VERIFIED |
| adr/ADR-001..003 | AS-IS | AGREE | AS-IS + status index | adr/ | Rust + src refs to ADR-002/003 | SHALLOW/FULL | VERIFIED |
| error-codes.md | UPDATE or GEN | CHANGE->GEN | GEN | reference/error-codes.md | registry exists but logging does not look codes up through it; stale coverage figure | FULL | VERIFIED |

### 3.5 Server operator (run C)

| Doc | Proposed | Codex | Disposition | Target | Evidence / deps | Depth | Status |
|-----|----------|-------|-------------|--------|-----------------|-------|--------|
| server-deployment.md | UPDATE+REDUCE | AGREE | REDUCE | server/deployment.md | 3+ stale claims; CLAUDE.md x2, progress_subprocess_runner.py | SHALLOW | VERIFIED |
| auto-update.md | UPDATE | AGREE | UPDATE | server/auto-update.md | self-heal model missing; CLAUDE.md x1 | FULL | VERIFIED |
| cluster-architecture.md | UPDATE | CHANGE->REORG | REORG | architecture/cluster.md (+ operator bits -> server/cluster-setup.md) | 30-min live-job timeout no longer exists; CLAUDE.md x2 | SHALLOW | VERIFIED |
| cluster-setup.md | UPDATE | CHANGE->REORG | REORG | server/cluster-setup.md | golden-repo mount layout conflicts with cow-storage-setup.md (decision D-3); pg_parity e2e test cites it | SHALLOW | VERIFIED |
| cow-storage-setup.md | UPDATE | AGREE | UPDATE | server/cow-storage-setup.md | mount conflict (D-3); install-cidx-server.sh cites it | SHALLOW | VERIFIED |
| oidc-setup-and-configuration.md | UPDATE | AGREE | UPDATE | server/auth/oidc.md | tells operators to edit config.json for DB-backed runtime settings | SHALLOW | VERIFIED |
| totp-elevation.md | UPDATE | CHANGE->REDUCE | REDUCE | -> server/auth/login-and-elevation.md (NEW) | CLAUDE.md x1 | FULL | VERIFIED |
| security/admin-mutation-routes.md | UPDATE or DROP | CHANGE->MERGE | MERGE | policy -> server/admin-guide.md; route list stays in code + its gate test | stale | FULL | VERIFIED |
| fault-injection-operator-guide.md | UPDATE | AGREE | UPDATE | server/fault-injection.md | path in fault_injection/router.py response; CLAUDE.md x2 | SHALLOW | VERIFIED |
| memory-retrieval-operator-guide.md | UPDATE | AGREE | UPDATE | server/memory-retrieval.md | wrong enabled default; CLAUDE.md x2 | FULL | VERIFIED |
| langfuse-trace-sync.md | UPDATE | AGREE | UPDATE | server/langfuse-trace-sync.md | live feature; obsolete setting names + trace filenames | FULL | VERIFIED |
| migration-playbook.md | UPDATE | CHANGE->REDUCE | REDUCE | server/data-migration-playbook.md | remove staging anecdotes + false config-write warning | FULL | VERIFIED |
| migration-to-v10.md | UPDATE | CHANGE->ARCHIVE | ARCHIVE | archive/ (one current server/upgrading.md entry point instead) | | SHALLOW | VERIFIED |
| migration-to-v8.md | ARCHIVE | AGREE | ARCHIVE | archive/ | config.py error messages x5 must be repointed | SHALLOW | VERIFIED |
| siem-delivery.md | REORG | AGREE | REORG | server/siem/operations.md; mechanics -> architecture | CLAUDE.md x1 | SHALLOW | VERIFIED |
| siem-secops-guide.md | REORG | CHANGE->REDUCE | REDUCE | server/siem/secops-guide.md | - | SHALLOW | VERIFIED |
| siem-secops-curl-runbook.md | UPDATE | AGREE | UPDATE | server/siem/curl-runbook.md | 2 stale claims | SHALLOW | VERIFIED |
| siem-secops-event-catalog.md | UPDATE or GEN | CHANGE->GEN | UPDATE (hand-written; no generator, decided 2026-10-06) | server/siem/event-catalog.md | - | SHALLOW | VERIFIED |
| docs/CHANGELOG.md | DROP | AGREE | DROP | - | stub; create_test_repo.py fixture string | FULL | VERIFIED |
| .github/workflows-disabled/ (README + publish.yml) | DROP? | CHANGE->DROP | DROP | - | not active | FULL | VERIFIED |
| dev/tools/README.md | AS-IS | CHANGE->UPDATE | UPDATE | dev/tools/README.md | 1 stale claim | FULL | VERIFIED |

### 3.6 Out of scope

skills/CLAUDE.md, runtime prompt templates under src/, tests/**/README.md, .github/ISSUE_TEMPLATE/* (AS-IS).

### 3.7 MCP tool docs (runs D1 + D2) -- 148 tools, separate track

| Category | Tools | OK | UPDATE | REWRITE | Impl concerns |
|----------|-------|----|--------|---------|---------------|
| admin, repos, files, ssh, tracing (D1) | 66 | 56 | 9 | 1 (`discover_repositories`: documents external discovery + `source_type`; handler lists registered golden repos and ignores it) | P1 x1, P2 x1, P3 x2 |
| git, search, scip, cicd, depmap, guides, memory (D2) | 82 | 73 | 9 | 0 | P1 x10, P2 x1 |
| Total | 148 | 129 | 18 | 1 | |

Every one of the 148 docs has a HANDLER_REGISTRY entry (no orphans). Many rows are SHALLOW (mechanical schema check only).
Per-tool rows live in the D1/D2 reports; they are copied into the tool-docs story when it is opened.

Status: VERIFIED. 79 tool docs were corrected (prose only; frontmatter schema structure proven unchanged against HEAD
by script) and fact-checked against their handlers; the rest matched. Schema-versus-handler mismatches that need a
schema edit are filed as a code issue, not fixed here.

## 4. New docs

| New doc | Content | Kind |
|---------|---------|------|
| docs/README.md | documentation map by audience | NEW |
| getting-started/teach-ai.md | awareness/skill install, --skills-only, targets, overwrite/refresh contract | NEW (from ai-integration) |
| getting-started/mcp-registration.md | registering the server as an MCP server | NEW (from ai-integration + mcp-registration-guide) |
| guides/remote-cli.md | remote-mode command groups: auth, server, repos, jobs, admin, global, keys, ssh-key, system, files, git, cicd, xray | NEW |
| guides/forge-and-write-tools.md | write mode, file CRUD, git write tools (two-call token handshake), git credentials, SSH keys, CI tools, pull-request tools | NEW |
| guides/embedding-providers-and-reranking.md | Voyage/Cohere, provider indexes, reranking, provider health | NEW |
| reference/cli.md | every command and flag | GEN (click) |
| reference/mcp-tools.md | catalog from frontmatter, incl. effective role/elevation rules, not just required_permission | GEN |
| reference/server-settings.md | every setting with bootstrap vs runtime column, default, restart requirement | NEW, hand-written (decided 2026-10-06: generators limited to CLI, MCP tools, error codes) |
| reference/error-codes.md | from the registry | GEN |
| reference/rest-api.md | endpoint guide pointing at OpenAPI | NEW (thin) |
| server/admin-guide.md | short landing page for Web UI admin areas | NEW |
| server/auth/accounts-and-access.md | users, groups, repo permissions, API keys, MCP credentials, SSO linkage, impersonation | NEW |
| server/auth/login-and-elevation.md | password+MFA and SSO challenge flows, TOTP elevation, 429 Retry-After / 503 busy, no permanent lock | NEW (absorbs totp-elevation) |
| server/maintenance-and-jobs.md | localhost-only maintenance writes, drain, jobs dashboard, cancellation, cluster ownership | NEW |
| server/observability.md | /healthz vs /health vs /api/system/health, logs DB, admin logs, OTEL metrics, trace ids | NEW |
| server/upgrading.md | single current upgrade entry point | NEW |
| server/wiki.md, server/research-assistant.md, server/self-monitoring.md | per-feature operator docs | NEW |
| architecture/indexing.md | discovery, hashing, chunking, batching, dedup, publish | NEW (from INDEXING_ALGORITHM + algorithms) |
| architecture/storage.md | SHARDED_JSON vs CHUNKS_DB contracts, layout authority, repair, fleet migration, HNSW orphan sweep | NEW |
| architecture/repository-lifecycle.md | golden clone, aliases, immutable snapshots, publication, retention, activation | NEW (from repository-topology) |
| architecture/refresh-recovery.md | exit 86/87, backoff, strikes, deferred triggers, generation-safe settlement | NEW |
| architecture/query-path.md | embedding cache, coalescer, 4-lane governor, provider calls, REST/MCP seams | NEW (from query-embedding-cache) |
| architecture/jobs-and-cluster-state.md | BGM/JobTracker, cancellation, payload cache, cross-node results | NEW |
| architecture/config-state.md | runtime-row compare-and-set, adoption, committed reads, callbacks | NEW |
| architecture/dependency-map.md | from 3 depmap docs + cidx-meta-backup | NEW |

## 5. Safety checks (Wave 0, before any edit)

1. Reference checker in `lint.sh`: relative Markdown links PLUS every literal `docs/...` path string in src/, rust/ (incl. `include_str!`), tests/, scripts/, SQL migrations, router response fields, error messages, installer scripts, CLAUDE.md, tool_docs. Every move ships its reference sweep in the same commit.
2. Doc-pinning tests: `tests/unit/test_docs_operating_modes_745.py` (path + content), hnswlib tests, hnsw orphan corpus test. Re-scan before each wave.
3. Runtime strings that print doc paths: `config.py:158-209` (migration-to-v8.md x5), `fault_injection/router.py:53`, hnswlib capability check / deployment executor / hnsw_index_manager.
4. Compile-time: `rust/xray-core/src/dynlib.rs` include_str! of `docs/xray-cookbook.md` and `docs/xray-templates/*.rs` -- a move requires the Rust edit and `rust-automation.sh`.
5. Disclosure: the former tree-wide banned-literal checker was DELETED 2026-10-06 (owner decision, no replacement) because it stored leaked values in plain text. Disclosure protection is the per-diff review scan plus the Wave 1 scrub (section 7). Never record a leaked value in any tracked file.

## 6. Execution order

| Wave | Scope |
|------|-------|
| 0 | Safety checks (section 5); file the implementation findings (section 8) |
| 1 | Disclosure scrub (section 7); DROP / MERGE / ARCHIVE |
| 2 | Front door: README, docs/README.md, installation, configuration, operating modes, teach-ai, MCP registration, query, SCIP, meta-repo discovery (highest-impact false examples first) |
| 3 | GEN references + drift checks (CLI, MCP tools, server settings, error codes, SIEM event catalog) |
| 4 | Server operator set (resolve D-3 first) |
| 5 | Architecture set |
| 6 | Remaining NEW docs; MCP tool-docs track (after the implementation fixes land); ai_instructions |
| 7 | Final cross-link, docs map, full verification |

## 7. Disclosure scrub list (Wave 1)

Kept OUT of this tracked plan on purpose: a list of unscrubbed leak locations is itself a disclosure map.
The list lives in the gitignored `.analysis/doc-overhaul-sensitive-findings-2026-10-06.md` (section A).

## 8. Implementation findings from the tool-docs re-review (fix before Wave 6)

Filed in the security tracker and the issue tracker (ids recorded in the gitignored companion file, section B).
Tool-doc rewrites for affected tools wait until their fixes land.

## 9. Writing standards and DoD

- One doc = one audience + one purpose. Current state only; history belongs in CHANGELOG (no "v8.8+" section tags).
- Every behavioural claim traceable to code; every command example executed (CLI locally; server examples through the REST/MCP front door).
- No emoji or decorative characters; neutral sample data only.
- Per-doc DoD: claims verified (SHALLOW rows get a full review), examples executed, reference check passes, disclosure clean, independent review (Claude fact-checker; Codex unavailable) approved.
- After the overhaul: "user-visible behaviour change updates the affected doc" joins the story DoD.

## 10. Open decisions

| Id | Decision |
|----|----------|
| D-1 | RESOLVED 2026-10-06: GitHub private vulnerability reporting (already in SECURITY.md, enabled on the public repo). |
| D-2 | RESOLVED 2026-10-06: personal contact removed; conduct reports go to GitHub Discussions (enabled) or GitHub report-abuse. |
| D-3 | RESOLVED in Wave 4 from the installer code: one CoW mount (`vers=3,nolock,soft,timeo=30,retrans=3`) with golden-repos and activated-repos symlinked into it; ONTAP joins mount `hard`. |
| D-4 | RESOLVED 2026-10-06: banned-literal checker deleted outright; no replacement. |
| D-5 | RESOLVED 2026-10-06: file the implementation findings now (security tracker + public issues per routing rule). |
| D-6 | OPEN: author attribution (name in LICENSE / pyproject / `__author__`; personal email in pyproject `authors`) -- keep or neutralize? |
