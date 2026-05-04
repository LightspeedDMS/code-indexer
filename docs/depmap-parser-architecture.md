# Depmap Parser Module Split and Anomaly Channels (Story #887, Epic #886)

This document captures the depmap parser architecture invariants extracted from the project CLAUDE.md to keep that file focused on rules and rituals.

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
