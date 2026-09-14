// X-Ray template: find-path-to-dense-sink (graph mode)
//
// When `fact_graph_complete: false`, an empty or sparse `findings` list
// is untrustworthy. `shortest_path_to_any` traverses the same POST-CAP
// CSR candidate arena as `callers_of`/`callees_of` -- a capped edge is
// invisible to this traversal even though the underlying reference
// exists in source.
//
// Story #1854 remediation (F2/F-codex-3): this template previously
// reported "no path" only for one hardcoded fixture source id, making a
// genuine "no path exists" indistinguishable from "the template never
// evaluated that source". It now emits ONE unconditional census, over
// EVERY non-sink source, before returning -- never a per-source finding,
// since a real repository can have thousands of sources and per-source
// findings would push the census past the MCP front door's inline
// truncation limit.
//
// This runs a bounded BFS (`shortest_path_to_any`) per source, i.e.
// O(N*E) over the whole graph -- termination is guaranteed by the op's
// own visited-set argument, not by `max_depth` (see `ops.rs`'s doc
// comment; passing `g.symbol_count()` as the depth cannot diverge). On a
// large repository this per-source BFS is the dominant cost of the
// `analyze_graph` call and worth weighing against the request's timeout.
//
// `SINK_DENSE_ID` below is a caller-supplied placeholder -- edit it to
// the dense id of the symbol you actually want to reach before running
// this template.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    const SINK_DENSE_ID: u32 = 3;
    let mut sources_scanned: usize = 0;
    let mut paths_found: usize = 0;
    let mut no_path: usize = 0;
    let mut source = 0u32;
    while source < g.symbol_count() as u32 {
        if source != SINK_DENSE_ID {
            sources_scanned += 1;
            match g.shortest_path_to_any(source, &[SINK_DENSE_ID], g.symbol_count()) {
                Some(_) => paths_found += 1,
                None => no_path += 1,
            }
        }
        source += 1;
    }
    result.findings.push(ReduceFinding {
        pattern: "dense_sink_path_census".to_string(),
        message: format!(
            "sources_scanned={} paths_found={} no_path={}",
            sources_scanned, paths_found, no_path
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result
}
