// Benchmark evaluator: Long Method Detector (Medium)
//
// Finds methods/functions longer than LINE_THRESHOLD lines.
// Exercises: recursive tree walk, line-span arithmetic, filtering.
// Expected: moderate findings — most methods are short.

const LINE_THRESHOLD: usize = 30;
const SNIPPET_MAX: usize = 80;

fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    find_long_methods(node, &mut findings);
    findings
}

fn find_long_methods(node: &OwnedNode, findings: &mut Vec<EvalFinding>) {
    let is_method = matches!(
        node.kind.as_str(),
        "method_declaration"
            | "constructor_declaration"
            | "function_declaration"
            | "function_definition"
            | "method_definition"
    );
    if is_method {
        let node_text = node.text();
        let line_count = node_text.lines().count();
        if line_count > LINE_THRESHOLD {
            let first_line = node_text.lines().next().unwrap_or("");
            findings.push(EvalFinding {
                pattern: "long-method".to_string(),
                line: node.start_line,
                snippet: truncate_snippet(
                    &format!("[{} lines] {}", line_count, first_line),
                    SNIPPET_MAX,
                ),
            });
        }
    }
    for child in &node.children {
        find_long_methods(child, findings);
    }
}
