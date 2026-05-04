# Phase 3.7 Dep-Map Graph-Channel Repair (Stories #908/#910/#911/#912, Epic #907)

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
- `src/code_indexer/server/services/dep_map_repair_executor.py` — orchestration, phase shims (~1040 lines)
- `src/code_indexer/server/services/dep_map_repair_phase37.py` — journal types (`Action`, `JournalEntry`, `RepairJournal`), SELF_LOOP step functions, byte-level helpers (`body_byte_offset`, `reemit_frontmatter_from_domain_info`) (~472 lines)
- `src/code_indexer/server/services/dep_map_repair_malformed_yaml.py` — MALFORMED_YAML repair cluster: `run_malformed_yaml_repairs`, `repair_single_malformed_yaml_anomaly`, `resolve_malformed_yaml_target`, `rewrite_malformed_yaml_file`, `apply_malformed_yaml_fallback` (~238 lines)
- `src/code_indexer/server/services/dep_map_repair_bidirectional.py` — BIDIRECTIONAL_MISMATCH orchestration + re-exports; public entry point `audit_one_bidirectional_mismatch` (~370 lines)
- `src/code_indexer/server/services/dep_map_repair_bidirectional_parser.py` — `CitationLine`, `EdgeAuditVerdict` dataclasses; `parse_audit_verdict` parser (~200 lines)
- `src/code_indexer/server/services/dep_map_repair_bidirectional_verify.py` — `run_verification_gate`: AC6 (file existence), AC7 (source reverse check), AC10 (rg timeout), AC11 (repo membership) (~150 lines)

**BIDIRECTIONAL_MISMATCH audit pipeline** (Story #912): `_run_phase37` invokes `audit_one_bidirectional_mismatch` for each BIDIRECTIONAL_MISMATCH anomaly **only when `invoke_claude_fn` is not None** (executors without Claude DI skip the pass). DI parameters `repo_path_resolver: Callable[[str], str]` and `invoke_claude_fn: Callable[[str, str, int, int], Tuple[bool, str]]` are passed to `DepMapRepairExecutor.__init__`. Prompt template externalized to `src/code_indexer/server/mcp/prompts/bidirectional_mismatch_audit.md`. Timeouts overridable via `CIDX_BIDI_CLAUDE_SHELL_TIMEOUT` and `CIDX_BIDI_CLAUDE_OUTER_TIMEOUT` env vars (defaults 270s/330s).

The executor re-exports `Action`, `JournalEntry`, `RepairJournal` from phase37 for backward compat. Tests that import these symbols from the executor continue to work.

`_repair_self_loop` stays on the executor class (tests call it there). `run_phase37` in phase37 module is the SELF_LOOP orchestrator. `_run_phase37` in executor is a thin shim that checks the enable flag, delegates to `run_phase37`, then calls `run_malformed_yaml_repairs`, then processes GARBAGE_DOMAIN_REJECTED and BIDIRECTIONAL_MISMATCH in a single anomaly loop.

Tests: `tests/unit/server/services/test_dep_map_908_*.py` (29 tests, 8 ACs); `tests/unit/server/services/test_dep_map_910_*.py` (24 tests, 5 ACs + builder/helpers); `tests/unit/server/services/test_dep_map_912_*.py` (44 tests, 5 ACs: AC1 prompt template, AC2 handler, AC4 parser, AC5/AC6/AC7/AC10/AC11 verification gate, executor wiring).
