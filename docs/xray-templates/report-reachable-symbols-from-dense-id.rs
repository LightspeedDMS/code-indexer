// X-Ray template: report-reachable-symbols-from-dense-id (graph mode)
//
// When `fact_graph_complete: false`, an empty or sparse `findings` list
// is untrustworthy. `reachable_from` traverses the same POST-CAP CSR
// candidate arena as `callers_of`/`callees_of` -- a capped edge is
// invisible to this traversal even though the underlying reference
// exists in source.
//
// Story #1854 remediation (F1/F9/F-codex-3): the out-of-range root check
// is pushed to `result.findings` immediately, before any per-root
// finding is buffered -- so `reachable_root_out_of_range` always lands
// ahead of the per-root findings regardless of which array position the
// out-of-range root occupies, never past the MCP front door's inline
// truncation window.
//
// `OUT_OF_RANGE_ROOT` is computed from `g.symbol_count()`, never a
// hardcoded literal -- on a graph with more symbols than an old
// hardcoded bound, a fixed literal silently stops being out-of-range and
// the demonstration disappears. The `reached.len() == 1` negative
// control fires for ANY root that reaches nothing else, not one
// hardcoded root id.
//
// `reachable_from`'s `max_depth` is `g.symbol_count()`, never a small
// hardcoded literal -- `ops.rs`'s own doc comment proves termination via
// the bounded visited set, not via `max_depth`, so passing the graph's
// own symbol count cannot diverge and never silently truncates a real
// traversal the way a small hardcoded depth would.
//
// `ROOT_DENSE_ID` and `SECOND_ROOT_DENSE_ID` below are caller-supplied
// placeholders -- edit them to the dense ids you actually want to probe
// before running this template.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

fn process_root(g: &GraphHandle<'_>, root: u32, buffered: &mut Vec<ReduceFinding>) -> Option<ReduceFinding> {
    if root as usize >= g.symbol_count() {
        return Some(ReduceFinding {
            pattern: "reachable_root_out_of_range".to_string(),
            message: format!("root_dense_id={} symbol_count={}", root, g.symbol_count()),
            involved: Vec::new(),
            signatures: Vec::new(),
        });
    }
    let reached = g.reachable_from(&[root], g.symbol_count());
    let mut unresolved = 0usize;
    let mut involved: Vec<u64> = Vec::new();
    let mut signatures: Vec<String> = Vec::new();
    for dense_id in &reached {
        match g.resolve_symbol(*dense_id) {
            Some(symbol) => {
                involved.push(symbol);
                signatures.push(g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string());
            }
            None => unresolved += 1,
        }
    }
    buffered.push(ReduceFinding {
        pattern: "reachable_symbols".to_string(),
        message: format!("root_dense_id={} reached_total={} unresolved={}", root, reached.len(), unresolved),
        involved,
        signatures,
    });
    if reached.len() == 1 {
        buffered.push(ReduceFinding {
            pattern: "reachable_negative_control".to_string(),
            message: "a valid isolated root reaches no other symbol".to_string(),
            involved: Vec::new(),
            signatures: Vec::new(),
        });
    }
    None
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    const ROOT_DENSE_ID: u32 = 0;
    const SECOND_ROOT_DENSE_ID: u32 = 4;
    let out_of_range_root: u32 = g.symbol_count() as u32;
    let roots = [ROOT_DENSE_ID, SECOND_ROOT_DENSE_ID, out_of_range_root];
    let mut buffered: Vec<ReduceFinding> = Vec::new();
    for root in roots {
        if let Some(out_of_range_finding) = process_root(g, root, &mut buffered) {
            result.findings.push(out_of_range_finding);
        }
    }
    result.findings.append(&mut buffered);
    result
}
