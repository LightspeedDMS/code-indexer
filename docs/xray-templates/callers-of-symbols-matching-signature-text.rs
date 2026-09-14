// X-Ray template: callers-of-symbols-matching-signature-text (graph mode)
//
// When `fact_graph_complete: false`, an empty or sparse `findings` list
// is untrustworthy.
//
// Story #1854 remediation (F3/F-codex-7): this template previously
// silently dropped four distinct blind spots -- a symbol with no cached
// signature, a matched symbol whose own dense id fails to resolve, a
// matched symbol with zero callers, and a caller whose dense id fails to
// resolve -- with no count anywhere proving how many were skipped. It
// now emits an unconditional census FIRST, before any per-caller
// finding, so the census always lands inside a truncated inline
// response on a real repo.
//
// This is TEXT MATCHING over `signature_for()`, not name resolution: it
// can over-match any cached signature that merely contains the
// substring, and it under-matches a symbol with no cached signature at
// all (counted as `missing_signature`, never silently skipped).
// `callers_of` reads the POST-CAP candidate arena, so a matched symbol
// can legitimately have zero callers even when it is referenced --
// counted as `matched_with_zero_callers`, not conflated with "no match".
//
// `SIGNATURE_TEXT` below is a caller-supplied placeholder -- edit it to
// the substring you actually want to match before running this template.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

struct SignatureMatchCensus {
    scanned: usize,
    matched: usize,
    missing_signature: usize,
    matched_with_zero_callers: usize,
    unresolved_targets: usize,
    unresolved_callers: usize,
    callers_reported: usize,
    findings: Vec<ReduceFinding>,
}

fn scan_signature_matches(g: &GraphHandle<'_>, signature_text: &str) -> SignatureMatchCensus {
    let mut census = SignatureMatchCensus {
        scanned: 0,
        matched: 0,
        missing_signature: 0,
        matched_with_zero_callers: 0,
        unresolved_targets: 0,
        unresolved_callers: 0,
        callers_reported: 0,
        findings: Vec::new(),
    };
    let mut dense_id = 0usize;
    while dense_id < g.symbol_count() {
        census.scanned += 1;
        let d = dense_id as u32;
        let signature = match g.signature_for(d) {
            Some(signature) => signature,
            None => {
                census.missing_signature += 1;
                dense_id += 1;
                continue;
            }
        };
        if signature.contains(signature_text) {
            record_match(g, d, signature, signature_text, &mut census);
        }
        dense_id += 1;
    }
    census
}

fn record_match(g: &GraphHandle<'_>, d: u32, signature: &str, signature_text: &str, census: &mut SignatureMatchCensus) {
    census.matched += 1;
    let target = match g.resolve_symbol(d) {
        Some(symbol) => symbol,
        None => {
            census.unresolved_targets += 1;
            return;
        }
    };
    let callers = g.callers_of(d);
    if callers.is_empty() {
        census.matched_with_zero_callers += 1;
    }
    for caller in callers {
        match g.resolve_symbol(caller) {
            Some(caller_symbol) => {
                census.callers_reported += 1;
                census.findings.push(ReduceFinding {
                    pattern: "caller_of_signature_match".to_string(),
                    message: format!(
                        "signature_text={} target_signature={} caller_dense_id={}",
                        signature_text, signature, caller
                    ),
                    involved: vec![caller_symbol, target],
                    signatures: vec![g.signature_for(caller).unwrap_or("<no-signature>").to_string(), signature.to_string()],
                });
            }
            None => census.unresolved_callers += 1,
        }
    }
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    const SIGNATURE_TEXT: &str = "Repository";
    let mut census = scan_signature_matches(g, SIGNATURE_TEXT);
    result.findings.push(ReduceFinding {
        pattern: "signature_match_census".to_string(),
        message: format!(
            "scanned={} matched={} missing_signature={} matched_with_zero_callers={} unresolved_targets={} unresolved_callers={} callers={}",
            census.scanned,
            census.matched,
            census.missing_signature,
            census.matched_with_zero_callers,
            census.unresolved_targets,
            census.unresolved_callers,
            census.callers_reported
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result.findings.append(&mut census.findings);
    result
}
