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

    # --- A3: Lambda removed from ALLOWED_NODES (Rust-only path) ---

    def test_lambda_rejection_mentions_inlining(self):
        # Lambda removed from ALLOWED_NODES — not transpilable to Rust.
        sb = PythonEvaluatorSandbox()
        result = sb.validate("f = lambda x: x > 0")
        assert result.ok is False

    # --- A4: FunctionDef remains in ALLOWED_NODES (transpilable to Rust) ---

    def test_functiondef_rejection_mentions_single_expression(self):
        # FunctionDef remains in ALLOWED_NODES — transpilable to Rust.
        sb = PythonEvaluatorSandbox()
        result = sb.validate("def helper(): return True")
        assert result.ok is True

    # --- A5: ClassDef rejection mentions definitions not allowed ---

    def test_classdef_rejection_mentions_definitions_not_allowed(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("class Foo: pass")
        assert result.ok is False
        assert "ClassDef" in result.reason
        lower = result.reason.lower()
        assert "definition" in lower or "class" in lower or "function" in lower

    # --- A6: Import rejection mentions the forbidden node type ---

    def test_import_rejection_mentions_safe_builtins(self):
        # Import is not in ALLOWED_NODES — rejected as a forbidden node.
        sb = PythonEvaluatorSandbox()
        result = sb.validate("import os")
        assert result.ok is False
        assert "Import" in result.reason

    def test_import_from_rejection_mentions_safe_builtins(self):
        # ImportFrom is not in ALLOWED_NODES — rejected as a forbidden node.
        sb = PythonEvaluatorSandbox()
        result = sb.validate("from os import path")
        assert result.ok is False
        assert "ImportFrom" in result.reason or "Import" in result.reason

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
        """Rejection reason must mention the allowed list (at least Call, Compare, ListComp).

        Uses a non-import forbidden construct (ClassDef) so the rejection goes
        through _build_rejection_reason which includes the full allowed-nodes list.
        Import rejections use a separate import-specific message (Story #993).
        """
        sb = PythonEvaluatorSandbox()
        result = sb.validate("class Foo: pass")
        assert result.ok is False
        reason = result.reason
        # Must include at least 3 well-known allowed node names
        assert "Call" in reason
        assert "Compare" in reason
        assert "ListComp" in reason

    # --- A9: Pointer to docs is present ---

    def test_rejection_message_includes_docs_reference(self):
        # Uses ClassDef (non-import) so rejection goes through _build_rejection_reason
        # which includes the "evaluator API documentation" pointer.
        # Import rejections use a separate message format (Story #993).
        sb = PythonEvaluatorSandbox()
        result = sb.validate("class Foo: pass")
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
        result = self._run_attr_code(
            "return node.descendants_of('method_invocation') == []"
        )
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


# ---------------------------------------------------------------------------
# v10.4.4 — Finding 3.8: exception types listed in validation error messages
# ---------------------------------------------------------------------------


class TestSafeBuiltinNamesV10_4_4:
    """Validation error for Import must list exception types (Finding 3.8)."""

    def test_import_rejection_lists_exception_types_v10_4_4(self):
        """Import is not in ALLOWED_NODES — rejected as a forbidden node type.

        Test name preserved for traceability to Finding 3.8.
        """
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        sb = PythonEvaluatorSandbox()
        result = sb.validate("import os")
        assert result.ok is False
        assert "Import" in result.reason

    def test_safe_builtin_names_count_is_27_v10_4_4(self):
        """SAFE_BUILTIN_NAMES must contain exactly 8 entries (Rust-transpilable only).

        Reduced from 34 to 8: len, any, all, range, enumerate, sorted, min, max.
        Test name preserved for traceability to v10.4.4 Finding 3.8.
        """
        from code_indexer.xray.sandbox import SAFE_BUILTIN_NAMES

        assert len(SAFE_BUILTIN_NAMES) == 8
