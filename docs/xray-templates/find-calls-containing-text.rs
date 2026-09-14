// X-Ray template: find-calls-containing-text (single-file / legacy mode)
//
// Finds every call-expression node whose source text contains a
// caller-chosen substring. TARGET_KIND is language-specific (e.g. Java's
// "method_invocation", Python's "call", JavaScript/TypeScript's
// "call_expression") -- use xray_explore to find the right kind name for
// your language before editing TARGET_KIND and TARGET_TEXT below.
//
// This is TEXT MATCHING over each call node's raw source text, not name
// resolution: it over-matches (any call whose text contains the substring,
// regardless of which symbol it actually resolves to) and under-matches
// (a call written across formatting that splits the substring). Use it to
// locate candidates for inspection, not as an authoritative call list.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    const TARGET_KIND: &str = "method_invocation";
    const TARGET_TEXT: &str = "rawDelete";
    const MAX_SNIPPET_CHARS: usize = 120;
    let mut findings: Vec<EvalFinding> = Vec::new();
    for call in node.descendants_of_kind(TARGET_KIND) {
        if call.text().contains(TARGET_TEXT) {
            findings.push(EvalFinding {
                pattern: "call_containing_text".to_string(),
                line: call.start_line,
                snippet: call.text().chars().take(MAX_SNIPPET_CHARS).collect(),
            });
        }
    }
    findings
}
