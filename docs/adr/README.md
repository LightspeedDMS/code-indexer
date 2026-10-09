# Architecture Decision Records

Each ADR records one decision as it was taken. ADRs are not edited after acceptance: when an implementation has moved
on, the current state is noted here and documented in the architecture pages, not by rewriting the ADR.

| ADR | Decision | Status | Date |
|-----|----------|--------|------|
| [ADR-001](ADR-001-xray-evaluator-execution-modes.md) | X-Ray supports exactly two evaluator execution modes: single-file `evaluate_node` (frozen compatibility mode) and graph mode (`collect_facts` + `analyze_graph`, the only extensible mode) | Accepted | 2026-09-06 (no date in the record; date it was added to the repository) |
| [ADR-002](ADR-002-xray-graph-handle-ffi.md) | Graph evaluators receive an opaque `GraphHandle` plus accessor functions instead of a mirrored `CodeGraph` layout in the PREAMBLE | Accepted | 2026-09-06 |
| [ADR-003](ADR-003-graph-memory-governor-integration.md) | The X-Ray graph build is admitted, bounded and observed through the existing `MemoryGovernor` without new governor API: admission gates, an injectable cgroup `MemoryCeiling`, a Python-side graph cache proxy composed with the HNSW cache, and extended governor counters | Accepted | 2026-09-06 |

## Current implementation status

- **ADR-001**: both modes are live. Mode detection is `compiler::detect_evaluator_mode`
  (`rust/xray-core/src/compiler.rs`); `validate_rust_evaluator()` (`src/code_indexer/xray/sandbox.py`) rejects a source
  that satisfies neither mode.
- **ADR-002**: implemented. `GraphHandle` is exported from `rust/xray-core/src/graph/csr/mod.rs`; the evaluator-side
  accessors are declared in `rust/xray-core/src/compiler/graph_preamble.rs`. The ABI number and the future-tense
  sequencing in the ADR describe the state when it was written.
- **ADR-003**: implemented. The composite cache is wired in `src/code_indexer/server/startup/service_init.py`, the
  cache proxy is `src/code_indexer/server/services/xray_graph_governor/cache_proxy.py`, and the cgroup ceiling is
  `rust/xray-core/src/graph/analyze/memory_ceiling.rs`. The MCP front door the ADR lists as future work exists:
  `handle_analyze_graph` in `src/code_indexer/server/mcp/handlers/xray_graph/__init__.py`.

The current X-Ray design is described in [architecture/xray/architecture.md](../architecture/xray/architecture.md).
