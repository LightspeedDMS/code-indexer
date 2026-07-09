// Benchmark evaluator: Deep Nesting Detector (Complex)
//
// Finds control flow nested 4+ levels deep (if/for/while/switch/when).
// Exercises: recursive depth tracking, multiple node-kind checks per level.
// Expected: fewer findings, more CPU per file due to deep recursion.

const DEPTH_THRESHOLD: usize = 4;
const SNIPPET_MAX: usize = 80;

fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    walk_nesting(node, 0, &mut findings);
    findings
}

fn is_control_flow(kind: &str) -> bool {
    matches!(
        kind,
        "if_statement"
            | "if_expression"
            | "for_statement"
            | "enhanced_for_statement"
            | "while_statement"
            | "do_statement"
            | "switch_expression"
            | "switch_statement"
            | "when_expression"
            | "for_in_statement"
            | "for_of_statement"
    )
}

fn walk_nesting(node: &OwnedNode, depth: usize, findings: &mut Vec<EvalFinding>) {
    let new_depth = if is_control_flow(&node.kind) {
        depth + 1
    } else {
        depth
    };

    if new_depth >= DEPTH_THRESHOLD && is_control_flow(&node.kind) {
        let first_line = node.text().lines().next().unwrap_or("");
        findings.push(EvalFinding {
            pattern: "deep-nesting".to_string(),
            line: node.start_line,
            snippet: truncate_snippet(
                &format!("[depth {}] {}", new_depth, first_line),
                SNIPPET_MAX,
            ),
        });
    }

    for child in &node.children {
        walk_nesting(child, new_depth, findings);
    }
}
