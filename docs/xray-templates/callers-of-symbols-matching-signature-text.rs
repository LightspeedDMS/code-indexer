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
// Bug #1929 item 2: `callers_of` returns each DISTINCT caller EXACTLY
// ONCE, never one entry per call site -- `callers_reported` (and the
// per-caller findings this template emits) is therefore a count of
// distinct callers, not call sites, even when a caller invokes the
// matched symbol from several places in its own body.
//
// Bug #1904 / #1929 item 1: a bound Method's `signature_for()` carries
// its declaring type and, where fully known, its real parameter type
// names -- `"Owner.name(ParamType, ...)"`, e.g.
// `"TimeUtil.parse(XMLGregorianCalendar)"`. A varargs parameter renders
// with its REAL per-language spelling (Java `char...`, always last;
// Kotlin `vararg Int`, at whatever position it actually occupies --
// Kotlin allows `vararg` anywhere, unlike Java), never a bare type name
// indistinguishable from a genuine one-arg overload. An anonymous or
// enum-constant-body class's `Owner` renders as
// `Enclosing$<anon@L<line>:<file_id>:<byte>>` -- the enclosing type's
// real name and the anon body's own real source line, human-chaseable.
// It still falls back to an arity-only tail -- `"Owner.name(N params)"`,
// or bare `"name(N params)"` when even the declaring type is unknown --
// whenever the extractor could not read every parameter's type or could
// not determine the enclosing type (never a fabricated guess). It NEVER
// carries annotations: a business-domain substring like `"Repository"`
// or `"Service"` can only match when a symbol's OWN bare name or
// declaring type happens to contain it -- it can never match an
// annotation (`@Repository`) alone. A zero-match census therefore
// proves nothing about whether repositories/services exist in the
// target codebase -- only that none of their NAMES contain your chosen
// substring.
//
// `SIGNATURE_TEXT` below is a caller-supplied PLACEHOLDER, deliberately
// defaulted to `"("` -- the one character present in EVERY shape above
// (widened or fallback) and in no type/field/package signature, so this
// template always produces a REAL, non-zero census out of the box on
// any ordinary repository with at least one callable symbol. That
// default proves the template's wiring end to end; it matches EVERY
// method/constructor and is NOT a meaningful filter on its own. Replace
// it with the actual substring you care about (a declaring-type or
// naming fragment your OWN codebase's methods actually use, e.g.
// `"Repository."` to match every method declared ON a type whose bare
// name is `Repository`) before drawing any conclusion from the results.
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
    const SIGNATURE_TEXT: &str = "(";
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
