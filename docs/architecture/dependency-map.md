# Dependency Map Architecture

## Depmap Parser Module Split and Anomaly Channels

This document captures the depmap parser architecture invariants extracted from the project CLAUDE.md to keep that file focused on rules and rituals.

The depmap parser was split from a single 1042-line `dep_map_mcp_parser.py` into four cohesive modules under the MESSI rule 6 soft cap (500 lines). Each module has a single responsibility:

| Module | Responsibility | Lines |
|--------|----------------|-------|
| `dep_map_mcp_parser.py` | Orchestration + public API (2-tuple legacy + 4-tuple with-channels) | ~440 |
| `dep_map_parser_tables.py` | Markdown table extraction | ~354 |
| `dep_map_parser_hygiene.py` | Identifier normalization, `AnomalyEntry`/`AnomalyAggregate`/`AnomalyType` dataclasses, dedup + aggregation helpers | ~279 |
| `dep_map_parser_graph.py` | Graph edge aggregation, filter hooks (reserved for future use), channel split | ~445 |

**Public API dual-surface** (both are stable contracts):
- `get_cross_domain_graph(output_dir) -> Tuple[List[Dict], List[Dict[str, str]]]` — legacy 2-tuple, anomalies as `{file, error}` dicts (backward-compat).
- `get_cross_domain_graph_with_channels(output_dir) -> Tuple[List[Dict], List[Union[AnomalyEntry, AnomalyAggregate]], List[Union[AnomalyEntry, AnomalyAggregate]], List[Union[AnomalyEntry, AnomalyAggregate]]]` — rich 4-tuple `(edges, all, parser_anomalies, data_anomalies)` for callers that need channel separation.

**Anomaly channel structure** (response envelope for all 5 `depmap_*` tools):
- `parser_anomalies[]` — structural file defects: malformed YAML, truncated table, unreadable bytes, path-traversal rejected, missing required frontmatter keys, section-present-but-empty.
- `data_anomalies[]` — source-graph drift: bidirectional mismatch, dual-source inconsistency (JSON↔markdown), garbage-domain rejected, self-loop, edge with no derivable types, case normalization applied.
- `anomalies[]` — legacy concatenation of both, preserved for ONE release after the parser module split (to be dropped in vN+1 per the BREAKING CHANGES plan).

**AnomalyType self-classifying enum**: each variant carries a bound `channel: Literal["parser", "data"]` attribute. Routing is `AnomalyType.channel` lookup — no manual classification logic. Aggregates route identically (the aggregate's `.type.channel` determines the channel).

**Frozenset-keyed bidirectional dedup**: `_check_bidirectional_consistency` aggregates by `frozenset({normalize(source), normalize(target)})` so one anomaly emits per unordered edge pair. Prevents the pre-split pattern of ~170 anomalies for ~150 edges. Both sides of the frozenset are normalized (strip_backticks + lowercase) to prevent case/backtick drift from producing false mismatches.

**Invariants (MESSI rule 15, stripped under `python -O`)**:
- `strip_backticks()` postcondition: `assert not s.startswith("\`") and not s.endswith("\`")` — all wrapper backticks stripped via `while` loops (not just one pair).
- Self-loop preservation unconditional: `finalize_graph_edges()` excludes self-loops from the empty-types drop filter (self-loops with empty types still emit the `GARBAGE_DOMAIN_REJECTED` anomaly AND are preserved as edges).
- Late-anomaly routing: `finalize_graph_edges()` anomalies flow through `aggregate_anomalies()` + channel split before response assembly — no silent drops (MESSI rule 13).

**Handler serialization**: `src/code_indexer/server/mcp/handlers/depmap.py::_anomaly_to_dict()` handles both `AnomalyEntry` and `AnomalyAggregate` — the same helper is reused at every response assembly site. Aggregates serialize as `{"file": "<aggregated>", "error": "N occurrences: <type>"}`.

Files: `src/code_indexer/server/services/dep_map_{mcp_parser,parser_tables,parser_hygiene,parser_graph}.py`, `src/code_indexer/server/mcp/handlers/depmap.py`. Tests: `tests/unit/server/services/test_dep_map_887_*.py` (70 tests across 8 ACs + 4 remediation blocker files).

## Phase 3.7 Dep-Map Graph-Channel Repair (Stories #908/#910/#911/#912, Epic #907)

This document captures the Phase 3.7 dep-map graph-channel repair architecture invariants extracted from the project CLAUDE.md to keep that file focused on rules and rituals.

Phase 3.7 is inserted in `_run_branch_a_dep_map` between Phase 3.5 (metadata backfill) and Phase 4 (index regeneration), at progress percent 78. It repairs graph-channel anomalies detected by the dep-map parser (SELF_LOOP in Story #908; MALFORMED_YAML in Story #910; GARBAGE_DOMAIN_REJECTED in Story #911; BIDIRECTIONAL_MISMATCH in Story #912).

**Bootstrap flag**: `enable_graph_channel_repair` in `config.json` (bootstrap-only, not DB). Default `True`. Pattern follows Bug #897 `enable_malloc_trim`. When `False`, `_run_phase37` returns immediately without reading parser anomalies or touching the journal. Passed to `DepMapRepairExecutor.__init__` as `enable_graph_channel_repair: bool = True`.

**Journal**: Append-only JSONL at `~/.cidx-server/dep_map_repair_journal.jsonl` (CIDX_DATA_DIR env var honored per Bug #879 IPC alignment). Each line is a 12-field JSON object: `timestamp`, `anomaly_type`, `source_domain`, `target_domain`, `source_repos`, `target_repos`, `verdict`, `action`, `citations`, `file_writes`, `claude_response_raw`, `effective_mode`. Atomic per-line writes via module-scope `_write_lock` (threading.Lock). `RepairJournal` class in `dep_map_repair_phase37.py`.

**Action enum master list** (grows per story):
- `self_loop_deleted` (Story #908) — deterministic; no Claude involved
- `malformed_yaml_reemitted` (Story #910) — deterministic surgical frontmatter re-emit from `_domains.json`
- `auto_backfilled` (Story #912) — Claude CONFIRMED; mirror row written to target incoming table
- `claude_refuted_pending_operator_approval` (Story #912) — Claude REFUTED; no file written
- `inconclusive_manual_review` (Story #912) — Claude INCONCLUSIVE; no file written
- `claude_cited_but_unverifiable` (Story #912) — CONFIRMED but cited file absent; downgraded
- `pleaser_effect_caught` (Story #912) — CONFIRMED but symbol absent from source repos; downgraded
- `repo_not_in_domain` (Story #912) — cited repo not a member of either domain; downgraded
- `verification_timeout` (Story #912) — rg subprocess timed out during AC6/AC7 check
- `claude_output_unparseable` (Story #912) — Claude response did not match expected format

**Verdict enum**: `CONFIRMED | REFUTED | INCONCLUSIVE | N_A` (deterministic repairs use `N_A`).

**MALFORMED_YAML repair** (Story #910): `run_malformed_yaml_repairs()` in `dep_map_repair_malformed_yaml.py` called by `_run_phase37` after SELF_LOOP pass. Uses `_domains.json` as authoritative source for `name`/`participating_repos`/`last_analyzed`. Preserves body bytes using `body_byte_offset()` byte-level splice (mixed line-endings safe). Falls back to Phase 1 full re-analysis when `_locate_frontmatter_bounds` returns `None` (body unrecoverable). Body of `_repair_malformed_yaml` in executor is a thin shim (~12 lines) that delegates to `repair_single_malformed_yaml_anomaly()` — no orchestration logic in the executor.

**File split** (MESSI Rule 6 extraction):
- `src/code_indexer/server/services/dep_map_repair_executor.py` — orchestration, phase shims (~1590 lines)
- `src/code_indexer/server/services/dep_map_repair_phase37.py` — journal types (`Action`, `JournalEntry`, `RepairJournal`), SELF_LOOP step functions, byte-level helpers (`body_byte_offset`, `reemit_frontmatter_from_domain_info`) (~616 lines)
- `src/code_indexer/server/services/dep_map_repair_malformed_yaml.py` — MALFORMED_YAML repair cluster: `run_malformed_yaml_repairs`, `repair_single_malformed_yaml_anomaly`, `resolve_malformed_yaml_target`, `rewrite_malformed_yaml_file`, `apply_malformed_yaml_fallback` (~323 lines)
- `src/code_indexer/server/services/dep_map_repair_bidirectional.py` — BIDIRECTIONAL_MISMATCH orchestration + re-exports; public entry point `audit_one_bidirectional_mismatch` (~529 lines)
- `src/code_indexer/server/services/dep_map_repair_bidirectional_parser.py` — `CitationLine`, `EdgeAuditVerdict` dataclasses; `parse_audit_verdict` parser (~200 lines)
- `src/code_indexer/server/services/dep_map_repair_bidirectional_verify.py` — `run_verification_gate`: AC6 (file existence), AC7 (source reverse check), AC10 (rg timeout), AC11 (repo membership) (~294 lines)

**BIDIRECTIONAL_MISMATCH audit pipeline** (Story #912): `_run_phase37` invokes `audit_one_bidirectional_mismatch` for each BIDIRECTIONAL_MISMATCH anomaly **only when `invoke_claude_fn` is not None** (executors without Claude DI skip the pass). DI parameters `repo_path_resolver: Callable[[str], str]` and `invoke_claude_fn: Callable[[str, str, int, int], Tuple[bool, str]]` are passed to `DepMapRepairExecutor.__init__`. Prompt template externalized to `src/code_indexer/server/mcp/prompts/bidirectional_mismatch_audit.md`. Timeouts overridable via `CIDX_BIDI_CLAUDE_SHELL_TIMEOUT` and `CIDX_BIDI_CLAUDE_OUTER_TIMEOUT` env vars (defaults 270s/330s).

The executor re-exports `Action`, `JournalEntry`, `RepairJournal` from phase37 for backward compat. Tests that import these symbols from the executor continue to work.

`_repair_self_loop` stays on the executor class (tests call it there). `run_phase37` in phase37 module is the SELF_LOOP orchestrator. `_run_phase37` in executor is a thin shim that checks the enable flag, delegates to `run_phase37`, then calls `run_malformed_yaml_repairs`, then processes GARBAGE_DOMAIN_REJECTED and BIDIRECTIONAL_MISMATCH in a single anomaly loop.

Tests: `tests/unit/server/services/test_dep_map_908_*.py` (29 tests, 8 ACs); `tests/unit/server/services/test_dep_map_910_*.py` (24 tests, 5 ACs + builder/helpers); `tests/unit/server/services/test_dep_map_912_*.py` (44 tests, 5 ACs: AC1 prompt template, AC2 handler, AC4 parser, AC5/AC6/AC7/AC10/AC11 verification gate, executor wiring).

## Resumable Delta Dep-Map Analysis Architecture

### Problem this design solves

`run_delta_analysis` invokes the `claude` CLI once per affected domain plus one monolithic Claude call for new-repo discovery. On a large change set (e.g. 33 affected domains + 12 new repos) the total wall-clock and token cost is multi-hour. If the cidx-server process dies mid-flight — auto-updater `systemctl restart`, OOM, `pkill -KILL`, manual restart, machine reboot — the prior naive implementation re-ran from scratch on the next trigger, throwing away every domain Claude had already finished.

This document describes the resume mechanism that eliminates that waste.

### High-level approach

**The artefact IS the journal.** Each `cidx-meta/dependency-map/<domain>.md` carries YAML frontmatter at the top of the file recording which delta was last applied to it. On a resumed run, the per-domain loop reads each affected file's frontmatter and skips domains whose `last_delta_applied` matches the current delta's fingerprint.

There is **no separate cursor file**. The cursor-vs-file ambiguity window (file written successfully but cursor save fails before crash) is eliminated by writing the frontmatter and body together in a single atomic `os.replace`.

### Five primitives

All implemented in `src/code_indexer/server/services/dep_map_delta_journal.py`.

#### 1. `compute_delta_fingerprint(changed, new, removed) -> str`

```
sha256(
  json.dumps(
    {"changed": sorted([r.alias for r in changed]),
     "new":     sorted([r.alias for r in new]),
     "removed": sorted(removed)},
    sort_keys=True
  ).encode()
).hexdigest()
```

Deterministic across runs. Order-independent within each list. Used as the resume key. A different repo set → different fingerprint → journal invalidated, fresh run.

#### 2. `parse_frontmatter(md_text) -> (dict, str)`

Extracts the YAML frontmatter block delimited by `---\n`/`---\n` at the start of the file. Returns `({}, original_text)` on malformed YAML or absent frontmatter (with a structured WARNING log line in the malformed case). Tolerant by design: corruption recovers automatically by treating the file as "no journal recorded, must re-process".

#### 3. `render_md(frontmatter: dict, body: str) -> str`

`"---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body`. Order of frontmatter keys is preserved (operator-managed keys round-trip).

#### 4. `write_atomic(path: Path, content: str) -> None`

The central correctness primitive:

```
tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
try:
    with os.fdopen(tmp_fd, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, str(path))
except Exception:
    try:
        os.unlink(tmp_path)
    finally:
        raise
```

- **Same parent directory**: `os.replace` is atomic only within a single filesystem; using the same parent guarantees this on local FS and on NFSv4.
- **`fsync` before `os.replace`**: ensures the temp file's bytes are flushed to the NFS server's stable storage before the rename.
- **Temp-file cleanup on failure**: no orphan temp files on disk.

#### 5. `all_new_repos_have_domain_assignments(new_repos, domains_json_path) -> bool`

Returns True iff every alias in `new_repos` appears as a member of some entry in `_domains.json`. Defensive against three corruption modes:

| Condition | Result |
|---|---|
| File missing | False |
| `json.JSONDecodeError` (truncated / bad UTF-8) | False (+ structured WARNING log) |
| Wrong shape (top-level is not list-of-dicts) | False (+ structured WARNING log) |
| Valid but incomplete | False |
| Valid and complete | True |

Returning False forces the monolithic new-repo discovery Claude call to re-run, which overwrites `_domains.json` cleanly.

### The resume loop

In `dependency_map_service.py::_update_affected_domains` (the existing per-affected-domain refinement loop), when called with `fingerprint != None`:

```python
for i, domain_name in enumerate(sorted(affected_domains)):
    if _cancel_event.is_set():
        break  # pause; frontmatter preserves what's done

    domain_file = dependency_map_dir / f"{domain_name}.md"
    existing_text = domain_file.read_text() if domain_file.exists() else ""
    fm, body = parse_frontmatter(existing_text)

    if fm.get("last_delta_applied") == fingerprint:
        activity_journal.log(f"Resume: skipping {domain_name} (already applied)")
        continue

    # Invoke Claude with existing body as baseline (no special prompt hint —
    # the file content IS the input; Claude treats it the same regardless of
    # whether it came from pre-delta or post-partial-prior-run).
    claude_raw = invoke_claude_cli(prompt, ...)

    # Strip any frontmatter Claude echoed back (it often reproduces the
    # entire file as part of its output; without this strip we would stack
    # frontmatter blocks).
    _, new_body = parse_frontmatter(claude_raw)

    # Empty / whitespace-only Claude response = failed domain; do NOT write
    # frontmatter; do NOT advance the journal.
    if not new_body.strip():
        errors.append(f"{domain_name}: empty Claude response")
        continue

    # Build new frontmatter: preserve operator-added keys, overwrite only the
    # two journal keys.
    new_fm = {k: v for k, v in fm.items()
              if k not in ("last_delta_applied", "last_applied_at")}
    new_fm["domain"] = domain_name
    new_fm["last_delta_applied"] = fingerprint
    new_fm["last_applied_at"] = datetime.now(timezone.utc).isoformat()

    write_atomic(domain_file, render_md(new_fm, new_body))
```

### Cluster correctness

Resumability is single-writer-safe because the entire delta run executes inside the existing **`cidx-meta` write lock** acquired via `RefreshScheduler.acquire_write_lock("cidx-meta")`. That lock is backed by `WriteLockManager` (atomic `os.open(O_CREAT|O_EXCL|O_WRONLY)` on the NFS-shared `cidx-meta` filesystem — NFSv4-safe per RFC 7530). The same lock is already in production use across `MemoryStoreService`, `XrayPatternService`, dep-map full analysis, and the dashboard sentinel.

Two concurrent runs (delta or full) cannot interleave because the second blocks on lock acquisition. Without this lock, two runs with different fingerprints could write conflicting `last_delta_applied` markers to the same domain file — so the per-domain frontmatter approach **depends on** the single-writer guarantee, it does NOT replace it.

### Crash-durability scope (honest)

The atomic co-write of frontmatter and body guarantees that completed-domain state survives:

| Failure mode | Survives? |
|---|---|
| Process crash | ✅ |
| `pkill -KILL` | ✅ |
| `systemctl restart cidx-server` (auto-updater path) | ✅ |
| Graceful node reboot | ✅ |
| **Sudden node power loss while writes are in-flight** | ⚠️ NFS server export-mode dependent |
| **NFS server crash during a write RPC** | ⚠️ `soft,timeo=30` returns an error rather than hanging — completed prior domains remain durable but the in-flight one is lost |

Parent-directory `fsync(2)` after `os.replace` is intentionally **NOT** added. NFS client support for directory fsync is implementation-defined; adding it would create a false sense of safety without a real guarantee. The honest scope statement above is the chosen design.

The recovery path for the unsupported failure modes is the same as any in-flight crash: the resumed run re-processes one domain at worst.

### What the design does NOT do (rejected during 4 rounds of design + Codex pressure-test review)

These were considered and explicitly rejected. Re-introducing any of them is a regression:

- **No backup-by-N domains on resume.** A "redo the last N completed domains defensively" mechanism was proposed and rejected because (a) atomic co-write eliminates the cursor-vs-file window the backup was meant to defend against, (b) it wastes Claude calls re-doing work that was already correctly applied.
- **No prompt context hint to Claude.** Telling Claude "the file may be from a partial prior run" adds prompt tokens for no behaviour change — the file IS the input either way.
- **No separate cursor file.** This is the alternative the design was specifically chosen against. The cursor-vs-file atomicity window the cursor introduces is exactly what frontmatter eliminates.
- **No batched / per-repo new-repo discovery.** The monolithic Claude call stays monolithic; skip-or-redo only.
- **No fingerprint intersection / partial credit.** When the delta set changes between runs (e.g., an additional repo had a refresh-pulled commit), the entire journal is invalidated and a fresh run starts. No half-credit.
- **No `run_full_analysis` hardening.** Out of scope. Full analysis has its own resume mechanism (separate journal under `cidx-meta/dependency-map.staging/`).
- **No parent-directory `fsync`** — see scope statement above.

### Regression guards

| Layer | Location |
|---|---|
| Unit + integration tests (40 tests) | `tests/unit/server/services/test_dep_map_1053_delta_journal.py` |
| Multi-domain delta fixture provisioner (`--dry-run` capable) | `tests/e2e/manual/provision_delta_fixture.sh` |
| Cidx-server process-tree audit (matches `claude .*--print`, optional `--port` narrowing) | `tests/e2e/manual/audit_processes.sh` |
| Manual E2E with SIGKILL (Scenario 16) | Trigger delta, wait for "Delta: domain 2/N complete", `sudo systemctl kill -s KILL cidx-server`, verify ALL DOWN, restart, re-trigger, assert wall-clock reduction + skip log lines + frontmatter fingerprint correctness |

### How a resumed run is observed in production

1. **Activity journal** (`<cidx-meta>/.scratch/dep_map_repair_journal.jsonl`) carries one `Resume: skipping {domain} (already applied)` line per skipped domain, plus a `Resume: skipping new-repo discovery (already complete)` line when Phase C is skipped.
2. **Wall-clock**: a resumed run with K of N domains already completed takes proportionally less time than the first run (subject to Claude latency variance).
3. **On-disk evidence**: every affected `<domain>.md` ends with frontmatter `last_delta_applied = <current_fingerprint>` after the resumed run completes, and exactly two `---\n` delimiters (no double frontmatter — the Claude-echo strip is the safeguard).

### File layout

- `src/code_indexer/server/services/dep_map_delta_journal.py` — helper module (the 5 primitives above)
- `src/code_indexer/server/services/dependency_map_service.py` — modified `_update_affected_domains` to accept an optional `fingerprint` and engage the journal path when present
- `tests/unit/server/services/test_dep_map_1053_delta_journal.py` — 40 tests
- `tests/e2e/manual/provision_delta_fixture.sh` — multi-domain fixture provisioner
- `tests/e2e/manual/audit_processes.sh` — process-tree audit

## cidx-meta backup contract

This document describes the cidx-meta backup contract invariants for the continuous git backup feature.

The server can maintain a continuous git backup of the cidx-meta directory to a remote repository. Key invariants:

**Mutable base path only**: All git operations (bootstrap, sync, rebase, push) execute against `<server_data_dir>/data/golden-repos/cidx-meta/`. NEVER operate inside `.versioned/cidx-meta/v_{timestamp}/` snapshot directories. Use `get_cidx_meta_path(server_data_dir)` from `src/code_indexer/server/services/cidx_meta_backup/paths.py` — single source of truth for both the route and the refresh scheduler.

**Index always runs after sync**: `CidxMetaBackupSync.sync()` runs BEFORE indexing in the refresh path. If sync succeeds (or partially fails with push-only error), indexing still runs. This is the deferred-failure pattern — a push failure becomes a `sync_failure` on the `SyncResult`, which causes the job to be marked FAILED after indexing completes.

**Push/fetch failure is deferred, conflict failure is immediate**: Network errors (fetch fail, push fail) are captured in `SyncResult.sync_failure` and surfaced as `RuntimeError` at the end of the refresh job. Conflict resolution failure raises `RuntimeError` immediately (after `git rebase --abort`) and short-circuits indexing.

**URL-change idempotency**: Changing the remote URL in the Web UI triggers `CidxMetaBackupBootstrap.bootstrap()` at Save time. The refresh scheduler also calls bootstrap at the start of every backup-enabled refresh cycle (idempotent — reads `git remote get-url origin`, no-ops on match). URL changes applied via direct DB edits are thus applied on the next refresh without requiring a Save.

**Externalized conflict-resolution prompt**: `src/code_indexer/server/mcp/prompts/cidx_meta_conflict_resolution.md` — editable by operators. Must contain `{conflict_files}`, `{branch}`, and `{repo_path}` format placeholders.

**Claude CLI routing**: Conflict resolution invokes Claude via `invoke_claude_cli()` in `src/code_indexer/global_repos/repo_analyzer.py`. On 600 s timeout, SIGTERM is sent first; SIGKILL follows after `_CLAUDE_TERMINATION_GRACE_PERIOD_SECONDS` (30 s).

**Branch detection**: `detect_default_branch(master_path)` from `src/code_indexer/server/services/cidx_meta_backup/branch_detect.py` is called at the start of each backup sync to support remotes with `main` as default. Falls back to `"master"` when detection fails.

Files: `src/code_indexer/server/services/cidx_meta_backup/` (bootstrap, sync, conflict_resolver, branch_detect, paths), `src/code_indexer/global_repos/refresh_scheduler.py` (backup branch), `src/code_indexer/server/web/routes.py` (config save route).
