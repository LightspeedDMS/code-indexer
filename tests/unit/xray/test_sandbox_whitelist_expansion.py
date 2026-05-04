"""Whitelist expansion tests for PythonEvaluatorSandbox (field-feedback fix #7).

Tests for Group A (local variables: Assign, AugAssign) and Group B
(comprehensions and ternaries: comprehension, GeneratorExp, ListComp,
SetComp, DictComp, IfExp) additions to ALLOWED_NODES.

Sections:
  1. Positive tests — new constructs must now PASS validation and execute.
  2. Security canaries — dunder blocklist still blocks escapes INSIDE new constructs.
  3. Boundary tests — for-loop, FunctionDef, ClassDef, Import, Lambda remain REJECTED.
"""

from __future__ import annotations

from unittest.mock import patch

from code_indexer.xray.sandbox import EvalResult, PythonEvaluatorSandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_root(source: str = "def foo(): pass", lang: str = "python"):
    """Return (node, root) for the given source."""
    from code_indexer.xray.ast_engine import AstSearchEngine

    engine = AstSearchEngine()
    root = engine.parse(source, lang)
    return root, root


def _validate_ok(code: str) -> None:
    """Assert that code passes validation (ok=True)."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate(code)
    assert result.ok is True, (
        f"Expected ok=True but got ok=False, reason={result.reason!r} "
        f"for code: {code!r}"
    )


def _validate_fails(code: str, expected_fragment: str) -> None:
    """Assert that code fails validation and reason contains expected_fragment."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate(code)
    assert result.ok is False, (
        f"Expected ok=False but got ok=True for code: {code!r}"
    )
    assert expected_fragment in (result.reason or ""), (
        f"Expected {expected_fragment!r} in reason {result.reason!r} "
        f"for code: {code!r}"
    )


def _run_ok(code: str, source: str = "def foo(): pass") -> EvalResult:
    """Run code and assert no failure."""
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root(source)
    result = sb.run(
        code,
        node=node,
        root=root,
        source=source,
        lang="python",
        file_path="/src/main.py",
    )
    assert result.failure is None, (
        f"Expected no failure but got failure={result.failure!r}, "
        f"detail={result.detail!r} for code: {code!r}"
    )
    return result


def _run_expecting_validation_failed(code: str) -> EvalResult:
    """Run code asserting validation_failed and no subprocess spawned."""
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()

    def _raise_if_called(*args, **kwargs):
        raise RuntimeError(
            "multiprocessing.get_context called — subprocess was attempted "
            "despite validation failure"
        )

    with patch(
        "code_indexer.xray.sandbox.multiprocessing.get_context", _raise_if_called
    ):
        result = sb.run(
            code,
            node=node,
            root=root,
            source="def foo(): pass",
            lang="python",
            file_path="/src/main.py",
        )

    assert result.failure == "validation_failed", (
        f"Expected validation_failed but got failure={result.failure!r}, "
        f"detail={result.detail!r} for code: {code!r}"
    )
    return result


# ---------------------------------------------------------------------------
# Section 1: Positive tests — new constructs pass validation and execute
# ---------------------------------------------------------------------------


class TestGroupALocalVariables:
    """Group A: Assign and AugAssign allow local variable binding."""

    def test_assign_validates_ok(self):
        """Assign node is now in ALLOWED_NODES."""
        _validate_ok("x = node.named_children\nreturn len(x) >= 0")

    def test_assign_executes_correctly(self):
        """Local variable via Assign can be used and returns correct result."""
        result = _run_ok(
            "named = node.named_children\n"
            "return any(c.type == 'function_definition' for c in named)",
            source="def foo(): pass",
        )
        assert result.value is True

    def test_augassign_validates_ok(self):
        """AugAssign node is now in ALLOWED_NODES."""
        _validate_ok("count = len(node.named_children)\ncount += 1\nreturn count > 0")

    def test_augassign_executes_correctly(self):
        """AugAssign correctly increments a local variable."""
        result = _run_ok(
            "count = len(node.named_children)\ncount += 1\nreturn count > 0",
            source="def foo(): pass",
        )
        assert result.value is True

    def test_augassign_with_explicit_zero(self):
        """AugAssign: count starts at 0, += 1 makes it 1, which is > 0."""
        result = _run_ok(
            "count = 0\ncount += 1\nreturn count > 0",
            source="def foo(): pass",
        )
        assert result.value is True


class TestGroupBComprehensionsAndTernaries:
    """Group B: comprehension, GeneratorExp, ListComp, SetComp, DictComp, IfExp."""

    # --- GeneratorExp ---

    def test_generator_exp_validates_ok(self):
        """GeneratorExp (the canonical field-feedback broken example)."""
        _validate_ok(
            "return any(n.type == 'function_definition' "
            "for n in node.named_children)"
        )

    def test_generator_exp_executes_correctly(self):
        """GeneratorExp: any() with generator over named_children works."""
        result = _run_ok(
            "return any(n.type == 'function_definition' "
            "for n in node.named_children)",
            source="def foo(): pass",
        )
        assert result.value is True

    def test_generator_exp_false_case(self):
        """GeneratorExp returns False when no children match."""
        result = _run_ok(
            "return any(n.type == 'class_definition' "
            "for n in node.named_children)",
            source="def foo(): pass",
        )
        assert result.value is False

    # --- ListComp ---

    def test_list_comp_validates_ok(self):
        """ListComp is now in ALLOWED_NODES."""
        _validate_ok(
            "return len([c for c in node.named_children "
            "if c.type == 'function_definition']) >= 1"
        )

    def test_list_comp_executes_correctly(self):
        """ListComp collects matching children; len check returns True."""
        result = _run_ok(
            "return len([c for c in node.named_children "
            "if c.type == 'function_definition']) >= 1",
            source="def foo(): pass",
        )
        assert result.value is True

    def test_list_comp_empty_result(self):
        """ListComp returns empty list when no children match; len == 0."""
        result = _run_ok(
            "return len([c for c in node.named_children "
            "if c.type == 'class_definition']) == 0",
            source="def foo(): pass",
        )
        assert result.value is True

    # --- SetComp ---

    def test_set_comp_validates_ok(self):
        """SetComp is now in ALLOWED_NODES."""
        _validate_ok(
            "types = {c.type for c in node.named_children}\n"
            "return 'function_definition' in types"
        )

    def test_set_comp_executes_correctly(self):
        """SetComp collects unique types; membership check works."""
        result = _run_ok(
            "types = {c.type for c in node.named_children}\n"
            "return 'function_definition' in types",
            source="def foo(): pass",
        )
        assert result.value is True

    # --- DictComp ---

    def test_dict_comp_validates_ok(self):
        """DictComp is now in ALLOWED_NODES."""
        _validate_ok(
            "counts = {c.type: 1 for c in node.named_children}\n"
            "return 'function_definition' in counts"
        )

    def test_dict_comp_executes_correctly(self):
        """DictComp builds a type-to-count mapping; key membership check works."""
        result = _run_ok(
            "counts = {c.type: 1 for c in node.named_children}\n"
            "return 'function_definition' in counts",
            source="def foo(): pass",
        )
        assert result.value is True

    # --- IfExp (ternary) ---

    def test_ifexp_validates_ok(self):
        """IfExp (ternary a if cond else b) is now in ALLOWED_NODES."""
        _validate_ok(
            "result = True if node.type == 'module' else False\n"
            "return result"
        )

    def test_ifexp_executes_true_branch(self):
        """IfExp: condition is true — returns the true branch value."""
        result = _run_ok(
            "result = True if node.type == 'module' else False\n"
            "return result",
            source="def foo(): pass",
        )
        assert result.value is True

    def test_ifexp_executes_false_branch(self):
        """IfExp: condition is false — returns the false branch value."""
        result = _run_ok(
            "result = True if node.type == 'class_definition' else False\n"
            "return result",
            source="def foo(): pass",
        )
        assert result.value is False

    # --- comprehension clause node ---

    def test_comprehension_clause_validates_ok(self):
        """comprehension clause node is used inside ListComp/GeneratorExp."""
        _validate_ok(
            "return len([c for c in node.named_children]) >= 0"
        )

    def test_comprehension_with_if_guard_validates_ok(self):
        """comprehension clause with an if-guard validates correctly."""
        _validate_ok(
            "return len([c for c in node.named_children "
            "if c.type == 'identifier']) >= 0"
        )

    # --- Combine Assign + GeneratorExp (canonical field-feedback pattern) ---

    def test_assign_plus_generator_exp_canonical_pattern(self):
        """Canonical pattern: bind children to var, then use generator over it."""
        result = _run_ok(
            "named = node.named_children\n"
            "return any(c.type == 'method_invocation' for c in named)",
            source="def foo(): pass",
        )
        assert result.value is False

    def test_assign_plus_generator_exp_with_match(self):
        """Canonical pattern that matches: generator over assigned var returns True."""
        result = _run_ok(
            "named = node.named_children\n"
            "return any(c.type == 'function_definition' for c in named)",
            source="def foo(): pass",
        )
        assert result.value is True

    def test_augassign_combined_with_list_comp(self):
        """AugAssign + ListComp: count matching nodes after initial assignment."""
        result = _run_ok(
            "matches = [c for c in node.named_children "
            "if c.type == 'function_definition']\n"
            "count = len(matches)\n"
            "count += 1\n"
            "return count >= 2",
            source="def foo(): pass",
        )
        # 1 function_definition + 1 (augassign) = 2, so >= 2 is True
        assert result.value is True


# ---------------------------------------------------------------------------
# Section 2: Security canaries — dunder blocklist still blocks inside new nodes
# ---------------------------------------------------------------------------


class TestDunderBlocklistInsideNewConstructs:
    """Dunder escape attempts inside newly-allowed constructs must still fail."""

    def test_dunder_class_inside_list_comp(self):
        """[x.__class__ for x in items] -> validation_failed."""
        result = _run_expecting_validation_failed(
            "return [x.__class__ for x in node.named_children]"
        )
        assert result.detail is not None

    def test_dunder_globals_inside_generator_exp(self):
        """any(x.__globals__ for x in items) -> validation_failed."""
        result = _run_expecting_validation_failed(
            "return any(x.__globals__ for x in node.named_children)"
        )
        assert result.detail is not None

    def test_dunder_init_inside_dict_comp(self):
        """{k: v.__init__ for k, v in d} -> validation_failed."""
        result = _run_expecting_validation_failed(
            "return {c.type: c.__init__ for c in node.named_children}"
        )
        assert result.detail is not None

    def test_dunder_builtins_inside_ifexp(self):
        """node.__builtins__ if cond else y -> validation_failed."""
        result = _run_expecting_validation_failed(
            "return node.__builtins__ if node.type == 'module' else False"
        )
        assert result.detail is not None

    def test_dunder_class_inside_set_comp(self):
        """{x.__class__ for x in items} -> validation_failed."""
        result = _run_expecting_validation_failed(
            "return {x.__class__ for x in node.named_children}"
        )
        assert result.detail is not None

    def test_dunder_dict_inside_assign(self):
        """x = node.__dict__ -> validation_failed."""
        result = _run_expecting_validation_failed(
            "x = node.__dict__\nreturn x is not None"
        )
        assert result.detail is not None

    def test_dunder_class_inside_augassign_chain(self):
        """count = node.__class__ -> validation_failed (dunder in rhs of Assign)."""
        result = _run_expecting_validation_failed(
            "count = node.__class__\nreturn count is not None"
        )
        assert result.detail is not None


# ---------------------------------------------------------------------------
# Section 3: Boundary tests — these must remain REJECTED after expansion
# ---------------------------------------------------------------------------


class TestBoundaryRejections:
    """Constructs that must remain blocked even after whitelist expansion.

    Comprehensions are allowed; top-level for statements are NOT.
    Local variables via = are allowed; global/nonlocal declarations are NOT.
    """

    def test_for_statement_still_rejected(self):
        """Top-level for x in y: pass (ast.For) must remain blocked.

        The boundary is: 'comprehension' AST node (comprehension clause inside
        a ListComp/GeneratorExp/etc.) is allowed, but ast.For (statement) is not.
        """
        _validate_fails(
            "for x in node.named_children:\n    pass",
            "For",
        )

    def test_class_def_still_rejected(self):
        """ClassDef must remain blocked after expansion."""
        _validate_fails("class Foo: pass", "ClassDef")

    def test_function_def_still_rejected(self):
        """FunctionDef must remain blocked after expansion."""
        _validate_fails("def foo(): pass", "FunctionDef")

    def test_import_still_rejected(self):
        """Import must remain blocked after expansion."""
        _validate_fails("import os", "Import")

    def test_lambda_still_rejected(self):
        """Lambda must remain blocked after expansion."""
        _validate_fails("lambda x: x", "Lambda")
