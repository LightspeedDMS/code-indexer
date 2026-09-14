// X-Ray template: find-definitely-dead-symbols (graph mode)
//
// No caller-supplied placeholder constants: this template scans every
// dense id in `0..symbol_count()` unconditionally, nothing to edit.
// When `fact_graph_complete: false`, the graph extraction hit a gap
// (`max_files` truncation, a parse error, or similar); an empty or
// sparse `findings` list is untrustworthy under that condition.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// Story #1854 remediation (F1/F7/F8/F-codex-1): the census is computed
// first and emitted as findings[0], before any per-symbol finding, so it
// always lands inside a truncated inline response (a real repo's
// per-symbol findings previously pushed the census past the MCP front
// door's inline truncation limit, making it structurally unreachable).
//
// `referenced` reads the pre-cap referenced bit (`is_symbol_referenced`):
// true once ANY raw candidate named this dense symbol id, independent of
// whether that candidate survived budget-ladder capping into the CSR
// arena. A `referenced` symbol can still have zero post-cap callers
// (`callers_of() == []`) when its only inbound edge was capped -- do not
// read `referenced` as "has callers".
//
// `undecidable` (`None`) covers any non-private visibility -- `Public`,
// `Protected`, AND `Unknown` (no modifier evidence, or a declaration
// shape/language the extractor does not classify) -- and on real Java
// source `Unknown` usually dominates this count. Do not read
// `undecidable` as "public API"; per-visibility counts are not broken out.
//
// `definitely_dead` (`Some(true)`) proves only "unreferenced AND
// explicitly declared private" -- the verdict remains falsifiable by
// reflection, JNI, or dependency injection, none of which leave an
// in-repo reference edge this graph can see, so a `Some(true)` verdict
// is not an unconditional deletion-safety proof.
struct DeadCodeCensus {
    definitely_dead: usize,
    undecidable: usize,
    referenced: usize,
    unresolved: usize,
    findings: Vec<ReduceFinding>,
}

fn scan_dead_code(g: &GraphHandle<'_>) -> DeadCodeCensus {
    let mut census =
        DeadCodeCensus { definitely_dead: 0, undecidable: 0, referenced: 0, unresolved: 0, findings: Vec::new() };
    let mut dense_id: usize = 0;
    while dense_id < g.symbol_count() {
        let d = dense_id as u32;
        let symbol = match g.resolve_symbol(d) {
            Some(s) => s,
            None => {
                census.unresolved += 1;
                dense_id += 1;
                continue;
            }
        };
        match g.is_definitely_dead_code(d) {
            Some(true) => {
                census.definitely_dead += 1;
                let signature = g.signature_for(d).unwrap_or("<no-signature>");
                census.findings.push(ReduceFinding {
                    pattern: "definitely_dead_symbol".to_string(),
                    message: format!("dense_id={} signature={}", d, signature),
                    involved: vec![symbol],
                    signatures: vec![signature.to_string()],
                });
            }
            Some(false) => census.referenced += 1,
            None => census.undecidable += 1,
        }
        dense_id += 1;
    }
    census
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let mut census = scan_dead_code(g);
    result.findings.push(ReduceFinding {
        pattern: "dead_code_scan_census".to_string(),
        message: format!(
            "definitely_dead={} undecidable={} referenced={} unresolved={}",
            census.definitely_dead, census.undecidable, census.referenced, census.unresolved
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result.findings.append(&mut census.findings);
    result
}
