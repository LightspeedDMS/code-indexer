// X-Ray template: find-function-definitions (single-file / legacy mode)
//
// Finds every function/method definition node in a file. Node kinds are
// language-specific: tree-sitter's Java grammar uses "method_declaration" and
// "constructor_declaration", its Python grammar uses "function_definition",
// its JavaScript/TypeScript grammars use "function_declaration" and
// "method_definition", and so on. Use xray_explore (or xray_dump_ast) to
// discover the exact kind name for the language you are scanning, then edit
// TARGET_KINDS below to match.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    const TARGET_KINDS: [&str; 2] = ["method_declaration", "function_declaration"];
    const MAX_SNIPPET_CHARS: usize = 120;
    let mut findings: Vec<EvalFinding> = Vec::new();
    for kind in TARGET_KINDS {
        for def in node.descendants_of_kind(kind) {
            findings.push(EvalFinding {
                pattern: "function_definition".to_string(),
                line: def.start_line,
                snippet: def.text().chars().take(MAX_SNIPPET_CHARS).collect(),
            });
        }
    }
    findings
}
