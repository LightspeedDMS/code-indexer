"""Tests for improved sandbox error messages (field-feedback fix #18).

Covers two improvements:
A. AST validation error messages: forbidden node type + full allowed list + workaround hint.
B. Evaluator-runtime AttributeError suggestions via difflib.
"""

from __future__ import annotations

from code_indexer.xray.sandbox import EvalResult, PythonEvaluatorSandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_root(source: str = "x = 1", language: str = "python"):
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine().parse(source, language)


# ---------------------------------------------------------------------------
# A. Validation error message improvements
# ---------------------------------------------------------------------------


class TestValidationErrorMessages:
    """Validation error messages include the forbidden node, allowed list, and hints."""

    # --- A1: For loop rejection includes 'comprehension' workaround hint ---

    def test_for_loop_rejection_mentions_comprehension(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("for x in items: pass")
        assert result.ok is False
        assert "For" in result.reason
        assert "comprehension" in result.reason.lower()

    # --- A2: While loop rejection includes a workaround hint ---

    def test_while_loop_rejection_has_workaround_hint(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("while True: pass")
        assert result.ok is False
        assert "While" in result.reason
        # Hint should mention comprehension or count_descendants_of_type
        lower = result.reason.lower()
        assert "comprehension" in lower or "count_descendants_of_type" in lower

    # --- A3: Lambda rejection mentions inlining ---

    def test_lambda_rejection_mentions_inlining(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("f = lambda x: x > 0")
        assert result.ok is False
        assert "Lambda" in result.reason
        lower = result.reason.lower()
        assert "inline" in lower or "boolean" in lower or "expression" in lower

    # --- A4: FunctionDef rejection mentions single expression ---

    def test_functiondef_rejection_mentions_single_expression(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("def helper(): return True")
        assert result.ok is False
        assert "FunctionDef" in result.reason
        lower = result.reason.lower()
        assert "single" in lower or "expression" in lower or "statement" in lower

    # --- A5: ClassDef rejection mentions definitions not allowed ---

    def test_classdef_rejection_mentions_definitions_not_allowed(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("class Foo: pass")
        assert result.ok is False
        assert "ClassDef" in result.reason
        lower = result.reason.lower()
        assert "definition" in lower or "class" in lower or "function" in lower

    # --- A6: Import rejection mentions SAFE_BUILTIN_NAMES ---

    def test_import_rejection_mentions_safe_builtins(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("import os")
        assert result.ok is False
        assert "Import" in result.reason
        # Must mention at least one safe builtin to orient the user
        reason = result.reason
        assert any(name in reason for name in ("len", "str", "min", "max", "sorted"))

    def test_import_from_rejection_mentions_safe_builtins(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("from os import path")
        assert result.ok is False
        assert "ImportFrom" in result.reason or "Import" in result.reason
        reason = result.reason
        assert any(name in reason for name in ("len", "str", "min", "max", "sorted"))

    # --- A7: Global/Nonlocal rejections mention local assignments ---

    def test_global_rejection_mentions_local_assignments(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("global x")
        assert result.ok is False
        assert "Global" in result.reason
        lower = result.reason.lower()
        assert "local" in lower or "assignment" in lower or "=" in result.reason

    def test_nonlocal_rejection_mentions_local_assignments(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("nonlocal x")
        assert result.ok is False
        assert "Nonlocal" in result.reason
        lower = result.reason.lower()
        assert "local" in lower or "assignment" in lower or "=" in result.reason

    # --- A8: Error message always includes the allowed-nodes list ---

    def test_rejection_message_includes_allowed_node_names(self):
        """Rejection reason must mention the allowed list (at least Call, Compare, ListComp)."""
        sb = PythonEvaluatorSandbox()
        result = sb.validate("import os")
        assert result.ok is False
        reason = result.reason
        # Must include at least 3 well-known allowed node names
        assert "Call" in reason
        assert "Compare" in reason
        assert "ListComp" in reason

    def test_for_loop_rejection_includes_allowed_node_names(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("for x in items: pass")
        assert result.ok is False
        reason = result.reason
        assert "Call" in reason
        assert "Name" in reason

    # --- A9: Pointer to docs is present ---

    def test_rejection_message_includes_docs_reference(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("import sys")
        assert result.ok is False
        lower = result.reason.lower()
        assert "doc" in lower or "api" in lower or "evaluator" in lower


# ---------------------------------------------------------------------------
# B. AttributeError suggestions via difflib
# ---------------------------------------------------------------------------


class TestAttributeErrorSuggestions:
    """Runtime AttributeErrors on XRayNode include 'Did you mean' suggestions."""

    def _run_attr_code(self, attr_code: str) -> EvalResult:
        root = _make_root("x = 1")
        node = root.named_children[0] if root.named_children else root
        sb = PythonEvaluatorSandbox()
        return sb.run(
            attr_code,
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/test.py",
        )

    # --- B1: children_named typo → suggests children (closest match by difflib) ---
    # Note: difflib scores children_named → children (0.727) and
    # children_by_field_name (0.722) above the 0.6 cutoff.
    # named_children scores 0.571 (below cutoff) so it is NOT suggested —
    # but "children" is a valid correction and is correctly surfaced.

    def test_children_named_suggests_children(self):
        result = self._run_attr_code("return node.children_named is not None")
        assert result.failure == "evaluator_subprocess_died"
        assert result.detail is not None
        assert "Did you mean" in result.detail
        assert "children" in result.detail

    # --- B2: descendants_of (missing _type) → suggests descendants_of_type ---

    def test_descendants_of_suggests_descendants_of_type(self):
        result = self._run_attr_code("return node.descendants_of('method_invocation') == []")
        assert result.failure == "evaluator_subprocess_died"
        assert result.detail is not None
        assert "Did you mean" in result.detail
        assert "descendants_of_type" in result.detail

    # --- B3: parnt typo → suggests parent ---

    def test_parnt_suggests_parent(self):
        result = self._run_attr_code("return node.parnt is None")
        assert result.failure == "evaluator_subprocess_died"
        assert result.detail is not None
        assert "Did you mean" in result.detail
        assert "parent" in result.detail

    # --- B4: totally_unknown_xyz → no spurious 'Did you mean' ---

    def test_totally_unknown_attribute_has_no_suggestion(self):
        result = self._run_attr_code("return node.totally_unknown_xyz_attribute == 42")
        assert result.failure == "evaluator_subprocess_died"
        assert result.detail is not None
        # Should NOT emit "Did you mean" for completely unknown names
        assert "Did you mean" not in result.detail
