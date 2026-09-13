# X-Ray Evaluator Cookbook

This phase of the cookbook provides routing and contract guidance, not evaluator
recipes; evaluator templates are deliberately deferred to Phase 2 (story
#1854). The current MCP and REST evaluator contract is Rust-based and is
documented by the live tool documentation for
[xray_search](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md).
The examples below are request shapes and contract guidance only.

## Reusing a stored pattern

Before writing `evaluator_code` inline, check the stored pattern library. Use
`browse_directory('cidx-meta-global', path='xray-patterns')` to list available
patterns, then pass the selected name as `pattern_name` in the MCP request.
Stored patterns avoid repeating evaluator code and may define typed parameters
through `pattern_params`.

## Single-file structural search

Use MCP `xray_search` when the question can be answered by inspecting one
candidate file at a time. Phase 1 selects candidate files with a regular
expression. The Rust evaluator then runs once for each candidate file and
receives its root `OwnedNode`.

The evaluator entry point is `fn evaluate_node(node: &OwnedNode) ->
Vec<EvalFinding>`. `OwnedNode` and `EvalFinding` are supplied by the compiler;
do not define them in the evaluator. `kind` and `start_line` are fields on an
owned node. `EvalFinding` contains `pattern`, `line`, and `snippet`; it does
not contain a `message` field.

An empty `Vec<EvalFinding>` means that the file matched Phase 1 but the
evaluator found nothing to report. The evaluator is file-as-unit: it does not
receive a separate callback for every regular-expression match.

## MCP request fields

MCP requests use `repository_alias`, `pattern`, `search_target`, and optional
`evaluator_code`. For a first call, omit `evaluator_code`; the server supplies
the default evaluator. Use `max_results` to cap the number of candidate files.
Begin with a small `max_results` value while checking a search, then increase
it when the result shape is understood. Include and exclude patterns,
language-specific paths, and `context_lines` can further focus the search.

The full MCP schema, evaluator rules, output fields, timeout behavior, and
security restrictions are maintained in the [xray_search tool
documentation](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md).

## REST field names

The REST endpoint `POST /api/xray/search` exposes the same single-file
capability but retains its REST field names. Send `driver_regex` instead of
the MCP `pattern`, and `max_files` instead of the MCP `max_results`. Do not
copy REST field names into an MCP request. Refer to the [REST section of the
live xray_search contract](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md)
when building an HTTP request.

## Graph mode

Use [analyze_graph](../src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md)
for questions that require relationships among files, such as reachability,
dead code, layering, or blast radius. Graph mode builds a cross-file reference
graph and is a different execution mode from single-file `xray_search`.

Graph mode requires both `fn collect_facts` and `fn analyze_graph`; neither is
optional. A graph evaluator must not define `fn evaluate_node`. `collect_facts`
runs per file to collect auxiliary evidence, while `analyze_graph` reduces the
completed graph. The graph extractor currently supports Java only.

Always inspect `fact_graph_complete` and the degradation counters before
treating an empty `findings` list as a verified negative. When
`fact_graph_complete` is false, an empty result means that the graph was too
incomplete to trust as a clean bill of health.

Read the [graph-mode tool documentation](../src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md)
for the `UserFact`, `GraphResult`, `ReduceFinding`, graph-handle, and
completeness contracts.

## Choosing the mode

Choose single-file mode when the evidence is local to each file and the
question is naturally expressed as findings attached to syntax nodes. Choose
graph mode when the answer depends on callers, callees, symbol identity, or a
path spanning multiple files. Both modes use the Rust evaluator engine, but
their required function contracts must not be mixed.

## Related documentation

- [X-Ray Architecture](xray-architecture.md) describes the engine and its two
  execution modes.
- [X-Ray Sandbox](xray-sandbox.md) describes the retained internal Python
  module and its non-contract status.
- [xray_search MCP contract](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md)
  defines the single-file request and evaluator schema.
- [analyze_graph MCP contract](../src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md)
  defines graph extraction, reduction, and completeness semantics.
