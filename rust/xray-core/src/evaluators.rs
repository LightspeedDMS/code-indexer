use crate::finding::EvalFinding;
use crate::owned_node::OwnedNode;
use crate::scanner::Evaluator;

/// Detects allocations inside a try block that also has a finally clause.
///
/// Pattern: try_statement with finally_clause where one of the first 1-3 named
/// statements in the try body is a local_variable_declaration containing an
/// object_creation_expression.
pub struct AllocationInTryEvaluator;

impl Evaluator for AllocationInTryEvaluator {
    fn evaluate_node(&self, root: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for node in root.descendants_of_kind("try_statement") {
            let has_finally = node.children.iter().any(|c| c.kind == "finally_clause");
            if !has_finally {
                continue;
            }

            let body = match node.child_by_kind("block") {
                Some(b) => b,
                None => continue,
            };

            for stmt in body.named_children().into_iter().take(3) {
                if stmt.kind == "local_variable_declaration"
                    && stmt.has_descendant_of_kind("object_creation_expression")
                {
                    findings.push(EvalFinding {
                        pattern: "allocation-in-try".to_string(),
                        line: stmt.start_line,
                        snippet: crate::finding::truncate_snippet(stmt.text(), 80),
                    });
                    break;
                }
            }
        }
        findings
    }
}

/// Detects trivial catch-rethrow: a catch clause whose body contains exactly
/// one named statement — a throw_statement that re-throws the caught identifier
/// unchanged.
///
/// Pattern: catch_clause where body has exactly 1 named child (throw_statement)
/// and the thrown identifier matches the catch parameter name.
pub struct CatchRethrowEvaluator;

impl Evaluator for CatchRethrowEvaluator {
    fn evaluate_node(&self, root: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for node in root.descendants_of_kind("catch_clause") {
            let param = match node
                .children
                .iter()
                .find(|c| c.kind == "catch_formal_parameter")
            {
                Some(p) => p,
                None => continue,
            };

            let param_name = match param
                .children
                .iter()
                .rfind(|c| c.kind == "identifier")
            {
                Some(id) => id.text().to_string(),
                None => continue,
            };

            let body = match node.child_by_kind("block") {
                Some(b) => b,
                None => continue,
            };

            let named = body.named_children();
            if named.len() != 1 {
                continue;
            }

            let stmt = named[0];
            if stmt.kind != "throw_statement" {
                continue;
            }

            let expr_named: Vec<&OwnedNode> =
                stmt.children.iter().filter(|c| c.is_named).collect();
            if expr_named.is_empty() {
                continue;
            }

            let expr = expr_named[0];
            if expr.kind == "identifier" && expr.text() == param_name {
                findings.push(EvalFinding {
                    pattern: "catch-rethrow".to_string(),
                    line: stmt.start_line,
                    snippet: crate::finding::truncate_snippet(stmt.text(), 80),
                });
            }
        }
        findings
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::owned_node::OwnedNode;

    fn leaf(kind: &str, text: &str, is_named: bool) -> OwnedNode {
        OwnedNode::new_leaf_for_test(kind, text, 1, is_named)
    }

    fn node(kind: &str, children: Vec<OwnedNode>) -> OwnedNode {
        OwnedNode::new_node_for_test(kind, "", 1, 0, 0, children, true)
    }

    fn node_with_text(kind: &str, text: &str, children: Vec<OwnedNode>) -> OwnedNode {
        OwnedNode::new_node_for_test(kind, text, 5, 0, text.len(), children, true)
    }

    // --- AllocationInTryEvaluator tests ---

    #[test]
    fn test_allocation_in_try_matches() {
        let evaluator = AllocationInTryEvaluator;

        let decl = node_with_text(
            "local_variable_declaration",
            "SomeType x = new SomeType()",
            vec![node("object_creation_expression", vec![])],
        );
        let body = node("block", vec![decl]);
        let finally = node("finally_clause", vec![]);
        let try_node = node("try_statement", vec![body, finally]);
        let root = node("program", vec![try_node]);

        let findings = evaluator.evaluate_node(&root);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].pattern, "allocation-in-try");
    }

    #[test]
    fn test_allocation_in_try_no_finally_no_match() {
        let evaluator = AllocationInTryEvaluator;

        let decl = node(
            "local_variable_declaration",
            vec![node("object_creation_expression", vec![])],
        );
        let body = node("block", vec![decl]);
        let try_node = node("try_statement", vec![body]);
        let root = node("program", vec![try_node]);

        let findings = evaluator.evaluate_node(&root);
        assert!(findings.is_empty());
    }

    #[test]
    fn test_allocation_in_try_wrong_node_kind_no_match() {
        let evaluator = AllocationInTryEvaluator;
        let other = node("if_statement", vec![]);
        let root = node("program", vec![other]);
        assert!(evaluator.evaluate_node(&root).is_empty());
    }

    #[test]
    fn test_allocation_in_try_no_allocation_no_match() {
        let evaluator = AllocationInTryEvaluator;

        // try with finally but body has only a method_invocation, not allocation
        let stmt = node("expression_statement", vec![node("method_invocation", vec![])]);
        let body = node("block", vec![stmt]);
        let finally = node("finally_clause", vec![]);
        let try_node = node("try_statement", vec![body, finally]);
        let root = node("program", vec![try_node]);

        let findings = evaluator.evaluate_node(&root);
        assert!(findings.is_empty());
    }

    // --- CatchRethrowEvaluator tests ---

    #[test]
    fn test_catch_rethrow_matches() {
        let evaluator = CatchRethrowEvaluator;

        let param_name_id = leaf("identifier", "e", true);
        let catch_param = node("catch_formal_parameter", vec![
            leaf("type_identifier", "Exception", true),
            param_name_id,
        ]);
        let thrown_id = leaf("identifier", "e", true);
        let throw_stmt = OwnedNode::new_node_for_test(
            "throw_statement",
            "throw e;",
            10,
            0,
            8,
            vec![leaf("throw", "throw", false), thrown_id],
            true,
        );
        let body = node("block", vec![throw_stmt]);
        let catch_node = node("catch_clause", vec![catch_param, body]);
        let root = node("program", vec![catch_node]);

        let findings = evaluator.evaluate_node(&root);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].pattern, "catch-rethrow");
        assert_eq!(findings[0].line, 10);
    }

    #[test]
    fn test_catch_rethrow_different_identifier_no_match() {
        let evaluator = CatchRethrowEvaluator;

        let param_name_id = leaf("identifier", "e", true);
        let catch_param = node("catch_formal_parameter", vec![
            leaf("type_identifier", "Exception", true),
            param_name_id,
        ]);
        // Throws "ex" but caught "e" — not a plain rethrow
        let thrown_id = leaf("identifier", "ex", true);
        let throw_stmt = OwnedNode::new_node_for_test(
            "throw_statement",
            "throw ex;",
            10,
            0,
            9,
            vec![leaf("throw", "throw", false), thrown_id],
            true,
        );
        let body = node("block", vec![throw_stmt]);
        let catch_node = node("catch_clause", vec![catch_param, body]);
        let root = node("program", vec![catch_node]);

        let findings = evaluator.evaluate_node(&root);
        assert!(findings.is_empty());
    }

    #[test]
    fn test_catch_rethrow_multiple_statements_no_match() {
        let evaluator = CatchRethrowEvaluator;

        let param_name_id = leaf("identifier", "e", true);
        let catch_param = node("catch_formal_parameter", vec![
            leaf("type_identifier", "Exception", true),
            param_name_id,
        ]);
        let thrown_id = leaf("identifier", "e", true);
        let throw_stmt = OwnedNode::new_node_for_test(
            "throw_statement",
            "throw e;",
            10,
            0,
            8,
            vec![leaf("throw", "throw", false), thrown_id],
            true,
        );
        // Body has 2 statements — not a plain rethrow
        let log_stmt = node("expression_statement", vec![]);
        let body = node("block", vec![log_stmt, throw_stmt]);
        let catch_node = node("catch_clause", vec![catch_param, body]);
        let root = node("program", vec![catch_node]);

        let findings = evaluator.evaluate_node(&root);
        assert!(findings.is_empty());
    }

    #[test]
    fn test_catch_rethrow_wrong_node_kind_no_match() {
        let evaluator = CatchRethrowEvaluator;
        let other = node("try_statement", vec![]);
        let root = node("program", vec![other]);
        assert!(evaluator.evaluate_node(&root).is_empty());
    }

    #[test]
    fn test_catch_rethrow_multiple_identifier_children_picks_last() {
        let evaluator = CatchRethrowEvaluator;

        // catch_formal_parameter has TWO "identifier"-kind children: an
        // annotation identifier appearing first, and the real binding
        // identifier last. The parameter-name extraction must pick the LAST
        // matching child — picking the first one instead would extract
        // "Nonnull" and fail to match the rethrow of "e".
        let annotation_id = leaf("identifier", "Nonnull", true);
        let type_id = leaf("type_identifier", "Exception", true);
        let param_name_id = leaf("identifier", "e", true);
        let catch_param = node(
            "catch_formal_parameter",
            vec![annotation_id, type_id, param_name_id],
        );
        let thrown_id = leaf("identifier", "e", true);
        let throw_stmt = OwnedNode::new_node_for_test(
            "throw_statement",
            "throw e;",
            10,
            0,
            8,
            vec![leaf("throw", "throw", false), thrown_id],
            true,
        );
        let body = node("block", vec![throw_stmt]);
        let catch_node = node("catch_clause", vec![catch_param, body]);
        let root = node("program", vec![catch_node]);

        let findings = evaluator.evaluate_node(&root);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].pattern, "catch-rethrow");
    }
}
