// X-Ray template: find-node-kind (single-file / legacy mode)
//
// Finds every descendant node of a caller-chosen tree-sitter kind. Node
// kinds are language-specific and change between grammars -- use
// xray_explore's AST dump (xray_dump_ast) to discover the exact kind name
// for the language you are scanning before editing TARGET_KIND below.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    const TARGET_KIND: &str = "class_declaration";
    const MAX_SNIPPET_CHARS: usize = 120;
    let mut findings: Vec<EvalFinding> = Vec::new();
    for hit in node.descendants_of_kind(TARGET_KIND) {
        findings.push(EvalFinding {
            pattern: "node_kind_match".to_string(),
            line: hit.start_line,
            snippet: hit.text().chars().take(MAX_SNIPPET_CHARS).collect(),
        });
    }
    findings
}
