// X-Ray template: find-reference-cycles (graph mode)
//
// No caller-supplied placeholder constants: this template walks every
// strongly-connected component unconditionally, nothing to edit. When
// `fact_graph_complete: false`, an empty or sparse `findings` list is
// untrustworthy. `strongly_connected_components` traverses the same
// POST-CAP CSR candidate arena as `callers_of`/`callees_of` below.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// Story #1854 remediation (F1/F6/F-codex-2): the negative control is
// computed and emitted BEFORE any per-component finding, so it always
// lands in findings[0] regardless of how many components the graph has
// (a real repo's per-component findings previously pushed it past the
// MCP front door's inline truncation limit).
//
// A singleton SCC with a genuine self-edge
// (`g.callees_of(*dense_id).contains(dense_id)`) is a real one-node
// cycle, not a suppressed acyclic node -- Tarjan cannot distinguish the
// two by component length alone, so this template checks the edge
// itself before treating `component.len() < 2` as "nothing to report".
//
// `possible_candidate_cycle` (not `reference_cycle`): `callers_of`/
// `callees_of` read the POST-CAP candidate arena, where a reference's
// candidate window may include more than one proposed target when the
// binder could not disambiguate -- an SCC over these edges is a possible
// cycle among proposed candidates, not a confirmed source-level
// reference cycle.
fn analyze_component(
    g: &GraphHandle<'_>,
    component: &[u32],
    acyclic_singletons: &mut usize,
    self_loop_singletons: &mut usize,
) -> Option<ReduceFinding> {
    if component.len() < 2 {
        for dense_id in component {
            if !g.callees_of(*dense_id).contains(dense_id) {
                *acyclic_singletons += 1;
                return None;
            }
            *self_loop_singletons += 1;
            return g.resolve_symbol(*dense_id).map(|symbol| ReduceFinding {
                pattern: "possible_candidate_cycle".to_string(),
                message: "component_size=1 unresolved_drop_count=0 self_loop=true".to_string(),
                involved: vec![symbol],
                signatures: vec![g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string()],
            });
        }
        return None;
    }
    let true_size = component.len();
    let mut involved: Vec<u64> = Vec::new();
    let mut signatures: Vec<String> = Vec::new();
    for dense_id in component {
        if let Some(symbol) = g.resolve_symbol(*dense_id) {
            involved.push(symbol);
            signatures.push(g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string());
        }
    }
    let unresolved_drop_count = true_size - involved.len();
    Some(ReduceFinding {
        pattern: "possible_candidate_cycle".to_string(),
        message: format!("component_size={} unresolved_drop_count={}", true_size, unresolved_drop_count),
        involved,
        signatures,
    })
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let components = g.strongly_connected_components();
    let mut acyclic_singletons: usize = 0;
    let mut self_loop_singletons: usize = 0;
    let mut buffered: Vec<ReduceFinding> = Vec::new();
    for component in &components {
        if let Some(finding) = analyze_component(g, component, &mut acyclic_singletons, &mut self_loop_singletons) {
            buffered.push(finding);
        }
    }
    result.findings.push(ReduceFinding {
        pattern: "reference_cycle_negative_control".to_string(),
        message: format!(
            "acyclic_singletons_suppressed={} self_loop_singletons={}",
            acyclic_singletons, self_loop_singletons
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result.findings.append(&mut buffered);
    result
}
