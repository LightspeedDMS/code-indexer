// Benchmark evaluator: Catch Block Analyzer (Complex)
//
// Finds catch blocks and classifies them:
//   - empty-catch: catch clause with no statements
//   - catch-rethrow: catch clause that immediately rethrows
//   - catch-log-rethrow: catch that logs then rethrows
//
// Exercises: multi-node traversal, text analysis, descendant search,
//   child counting, pattern classification.
// Expected: fewest findings, most analysis per node.

const SNIPPET_MAX: usize = 80;

fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    find_catch_blocks(node, &mut findings);
    findings
}

fn find_catch_blocks(node: &OwnedNode, findings: &mut Vec<EvalFinding>) {
    let is_catch = matches!(
        node.kind.as_str(),
        "catch_clause" | "catch_block" | "except_clause"
    );

    if is_catch {
        classify_catch(node, findings);
    }

    for child in &node.children {
        find_catch_blocks(child, findings);
    }
}

fn classify_catch(node: &OwnedNode, findings: &mut Vec<EvalFinding>) {
    let body = node.child_by_kind("block")
        .or_else(|| node.child_by_kind("catch_body"));

    let body = match body {
        Some(b) => b,
        None => {
            findings.push(EvalFinding {
                pattern: "empty-catch".to_string(),
                line: node.start_line,
                snippet: truncate_snippet(node.text(), SNIPPET_MAX),
            });
            return;
        }
    };

    let stmts: Vec<&OwnedNode> = body.named_children()
        .into_iter()
        .filter(|c| c.kind != "comment" && c.kind != "line_comment" && c.kind != "block_comment")
        .collect();

    if stmts.is_empty() {
        findings.push(EvalFinding {
            pattern: "empty-catch".to_string(),
            line: node.start_line,
            snippet: truncate_snippet(node.text(), SNIPPET_MAX),
        });
        return;
    }

    let has_throw = body.has_descendant_of_kind("throw_statement")
        || body.has_descendant_of_kind("throw_expression");

    if has_throw && stmts.len() == 1 {
        findings.push(EvalFinding {
            pattern: "catch-rethrow".to_string(),
            line: node.start_line,
            snippet: truncate_snippet(node.text(), SNIPPET_MAX),
        });
    } else if has_throw && stmts.len() <= 3 {
        let body_text = body.text();
        let has_log_call = body_text.contains(".log")
            || body_text.contains(".warn")
            || body_text.contains(".error")
            || body_text.contains("LOG.")
            || body_text.contains("logger.");
        if has_log_call {
            findings.push(EvalFinding {
                pattern: "catch-log-rethrow".to_string(),
                line: node.start_line,
                snippet: truncate_snippet(node.text(), SNIPPET_MAX),
            });
        }
    }
}
