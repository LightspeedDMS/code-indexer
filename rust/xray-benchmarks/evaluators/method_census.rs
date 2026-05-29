// Benchmark evaluator: Method Census (Simple)
//
// Walks the entire AST tree to find all method/function declarations.
// Exercises: full recursive tree walk, text extraction, truncate_snippet.
// Expected: many findings per file — high finding-collection overhead.
//
// Supported node kinds:
//   Java:       method_declaration, constructor_declaration
//   Kotlin:     function_declaration
//   Python:     function_definition
//   Go:         function_declaration, method_declaration
//   JS/TS:      function_declaration, method_definition
//   C#:         method_declaration, constructor_declaration

fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    collect_methods(node, &mut findings);
    findings
}

fn collect_methods(node: &OwnedNode, findings: &mut Vec<EvalFinding>) {
    let dominated = matches!(
        node.kind.as_str(),
        "method_declaration"
            | "constructor_declaration"
            | "function_declaration"
            | "function_definition"
            | "method_definition"
    );
    if dominated {
        let first_line = node.text().lines().next().unwrap_or("");
        findings.push(EvalFinding {
            pattern: "method-census".to_string(),
            line: node.start_line,
            snippet: truncate_snippet(first_line, 80),
        });
    }
    for child in &node.children {
        collect_methods(child, findings);
    }
}
