"""Seed patterns written to the ``__any__`` scope of the X-Ray pattern library
(``XrayPatternService.ensure_seed_patterns``)."""

SEED_CATCH_RETHROW = """\
name: catch-rethrow
description: "Detects empty catch blocks that just rethrow the exception"
language: java
tags: [error-handling, anti-pattern]
author: cidx
created_at: "2026-05-28"
evaluator_code: |
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
          .filter(|c| c.kind != "comment" && c.kind != "line_comment"
                    && c.kind != "block_comment")
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
parameters:
  - name: SNIPPET_MAX
    type: usize
    default: 80
    description: "Maximum character length for code snippets in findings"
"""

SEED_DEEP_NESTING = """\
name: deep-nesting
description: "Finds control flow nested N+ levels deep (if/for/while/switch/when)"
language: java
tags: [complexity, code-quality]
author: cidx
created_at: "2026-05-28"
evaluator_code: |
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
parameters:
  - name: DEPTH_THRESHOLD
    type: usize
    default: 4
    description: "Minimum nesting depth to flag"
  - name: SNIPPET_MAX
    type: usize
    default: 80
    description: "Maximum character length for code snippets in findings"
"""
