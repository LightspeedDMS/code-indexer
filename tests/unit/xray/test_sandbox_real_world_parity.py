"""User Mandate Section 7 Scenario 5: Cross-Language Parity Tests (Story #970).

Split from test_sandbox_real_world.py to keep each file under the 500-line cap
(MESSI Rule 6).

  Scenario 5: Cross-language parity — method call inside lambda/closure
    - Java:       fixtures/structure/lambda_with_call.java
    - Python:     inline fixture (lambda x: foo.bar(x))
    - TypeScript: inline fixture ((x) => obj.method(x))
    - Evaluator checks is_descendant_of() with language-appropriate ancestor type
    - Expected: all three return True for the call inside the lambda/closure
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.xray.ast_engine import AstSearchEngine
from code_indexer.xray.sandbox import PythonEvaluatorSandbox


FIXTURES_STRUCTURE = Path(__file__).parent / "fixtures" / "structure"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk_nodes(node, target_type: str) -> list:
    """Collect all descendant nodes (including node itself) of the given type."""
    results = []
    if node.type == target_type:
        results.append(node)
    for child in node.named_children:
        results.extend(_walk_nodes(child, target_type))
    return results


# ---------------------------------------------------------------------------
# Scenario 5: Cross-language parity — method call inside lambda/closure
# ---------------------------------------------------------------------------


def test_cross_language_parity_lambda_call() -> None:
    """Same conceptual pattern, three languages: method call inside lambda/closure.

    Each language uses a different AST ancestor node type for the lambda/closure
    construct:
      - Java:       lambda_expression   (tree-sitter Java grammar)
      - Python:     lambda              (tree-sitter Python grammar)
      - TypeScript: arrow_function      (tree-sitter TypeScript grammar)

    And a different call node type:
      - Java:       method_invocation
      - Python:     call
      - TypeScript: call_expression

    The evaluator is is_descendant_of(<ancestor_type>) — identical logic,
    different string argument.  This test proves that the sandbox correctly
    executes language-aware evaluators across all three grammars with a single
    unified API (PythonEvaluatorSandbox.run).

    Java fixture:       fixtures/structure/lambda_with_call.java
    Python fixture:     inline — lambda x: foo.bar(x)
    TypeScript fixture: inline — (x) => obj.method(x)
    """
    engine = AstSearchEngine()
    sb = PythonEvaluatorSandbox()

    # ------------------------------------------------------------------
    # Java: method_invocation inside lambda_expression
    # ------------------------------------------------------------------
    java_source = (FIXTURES_STRUCTURE / "lambda_with_call.java").read_text()
    java_root = engine.parse(java_source, "java")

    java_invocations = _walk_nodes(java_root, "method_invocation")
    java_inside = [
        m for m in java_invocations if m.is_descendant_of("lambda_expression")
    ]
    assert java_inside, (
        "Expected at least one method_invocation inside lambda_expression in Java fixture"
    )

    evaluator_java = 'return node.is_descendant_of("lambda_expression")'
    java_result = sb.run(
        evaluator_java,
        node=java_inside[0],
        root=java_root,
        source=java_source,
        lang="java",
        file_path=str(FIXTURES_STRUCTURE / "lambda_with_call.java"),
    )
    assert java_result.failure is None, (
        f"Java evaluator failed: {java_result.failure!r}, detail={java_result.detail!r}"
    )
    assert java_result.value is True, (
        f"Java: expected True for method_invocation inside lambda_expression, "
        f"got {java_result.value!r}"
    )

    # ------------------------------------------------------------------
    # Python: call inside lambda
    # ------------------------------------------------------------------
    py_source = "f = lambda x: foo.bar(x)"
    py_root = engine.parse(py_source, "python")

    py_calls = _walk_nodes(py_root, "call")
    assert py_calls, "Expected at least one call node in Python lambda fixture"

    evaluator_py = 'return node.is_descendant_of("lambda")'
    py_result = sb.run(
        evaluator_py,
        node=py_calls[0],
        root=py_root,
        source=py_source,
        lang="python",
        file_path="/tmp/lambda_fixture.py",
    )
    assert py_result.failure is None, (
        f"Python evaluator failed: {py_result.failure!r}, detail={py_result.detail!r}"
    )
    assert py_result.value is True, (
        f"Python: expected True for call inside lambda, got {py_result.value!r}"
    )

    # ------------------------------------------------------------------
    # TypeScript: call_expression inside arrow_function
    # ------------------------------------------------------------------
    ts_source = "const f = (x) => obj.method(x);"
    ts_root = engine.parse(ts_source, "typescript")

    ts_calls = _walk_nodes(ts_root, "call_expression")
    assert ts_calls, (
        "Expected at least one call_expression node in TypeScript arrow_function fixture"
    )

    evaluator_ts = 'return node.is_descendant_of("arrow_function")'
    ts_result = sb.run(
        evaluator_ts,
        node=ts_calls[0],
        root=ts_root,
        source=ts_source,
        lang="typescript",
        file_path="/tmp/arrow_fixture.ts",
    )
    assert ts_result.failure is None, (
        f"TypeScript evaluator failed: {ts_result.failure!r}, detail={ts_result.detail!r}"
    )
    assert ts_result.value is True, (
        f"TypeScript: expected True for call_expression inside arrow_function, "
        f"got {ts_result.value!r}"
    )
