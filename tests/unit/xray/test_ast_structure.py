"""AST structure assertion tests for X-Ray AST engine.

User Mandate Section 2: validate the AST is navigable in the ways the evaluator
API will need. These tests go beyond "parses without error" to assert specific
structural properties: ancestor relationships, field access, byte-range integrity,
parent chain termination, and body field access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
STRUCTURE_FIXTURES = FIXTURES / "structure"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():  # type: ignore[return]
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine()


def _parse(source: bytes, lang: str):  # type: ignore[return]
    engine = _make_engine()
    return engine.parse(source, lang)


def _find_first(node, type_name: str):  # type: ignore[return]
    """Return the first descendant (or self) matching type_name, else None."""
    if node.type == type_name:
        return node
    for child in node.children:
        result = _find_first(child, type_name)
        if result is not None:
            return result
    return None


def _find_all(node, type_name: str) -> list:  # type: ignore[type-arg]
    """Return all descendants (including self) matching type_name."""
    results = []
    if node.type == type_name:
        results.append(node)
    for child in node.children:
        results.extend(_find_all(child, type_name))
    return results


def _collect_up_to_n(node, count: int) -> list:  # type: ignore[type-arg]
    """Collect up to `count` nodes via depth-first walk."""
    results: list[Any] = []

    def walk(n):  # type: ignore[no-untyped-def]
        if len(results) >= count:
            return
        results.append(n)
        for child in n.children:
            walk(child)

    walk(node)
    return results


# ---------------------------------------------------------------------------
# Section 2.1: is_descendant_of with Java lambda + method_invocation
# ---------------------------------------------------------------------------


class TestIsDescendantOf:
    """is_descendant_of walks ancestors and matches by type name."""

    def test_method_invocation_inside_lambda_returns_true(self) -> None:
        """method_invocation inside lambda_expression: is_descendant_of returns True."""
        source = (STRUCTURE_FIXTURES / "lambda_with_call.java").read_bytes()
        engine = _make_engine()
        root = engine.parse(source, "java")

        # Find method_invocations that are inside lambda_expression
        all_invocations = _find_all(root, "method_invocation")
        assert len(all_invocations) > 0, "No method_invocation found in fixture"

        lambda_invocations = [
            m for m in all_invocations if m.is_descendant_of("lambda_expression")
        ]
        assert len(lambda_invocations) > 0, (
            "No method_invocation found that is a descendant of lambda_expression"
        )
        for node in lambda_invocations:
            assert node.is_descendant_of("lambda_expression") is True

    def test_method_invocation_in_lambda_not_class_body(self) -> None:
        """method_invocation NOT inside lambda_expression returns False for that type."""
        source = (STRUCTURE_FIXTURES / "lambda_with_call.java").read_bytes()
        engine = _make_engine()
        root = engine.parse(source, "java")

        all_invocations = _find_all(root, "method_invocation")
        # Find one that is NOT inside a lambda
        non_lambda_invocations = [
            m for m in all_invocations if not m.is_descendant_of("lambda_expression")
        ]
        assert len(non_lambda_invocations) > 0, (
            "All method_invocations are inside lambdas — fixture needs a call outside lambda"
        )
        for node in non_lambda_invocations:
            assert node.is_descendant_of("lambda_expression") is False

    def test_is_descendant_of_matches_parent_walk(self) -> None:
        """is_descendant_of result matches manual parent chain walk."""
        source = (STRUCTURE_FIXTURES / "lambda_with_call.java").read_bytes()
        engine = _make_engine()
        root = engine.parse(source, "java")

        all_invocations = _find_all(root, "method_invocation")
        for node in all_invocations:
            # Manual walk: collect all ancestor types
            ancestor_types = set()
            parent = node.parent
            while parent is not None:
                ancestor_types.add(parent.type)
                parent = parent.parent

            assert node.is_descendant_of("lambda_expression") == (
                "lambda_expression" in ancestor_types
            ), (
                f"is_descendant_of('lambda_expression') disagreed with manual walk "
                f"for node {node.type!r} text={node.text[:30]!r}"
            )

    def test_is_descendant_of_root_returns_false(self) -> None:
        """Root node is_descendant_of anything returns False (has no ancestors)."""
        root = _parse(b"x = 1\n", "python")
        assert root.is_descendant_of("module") is False
        assert root.is_descendant_of("expression_statement") is False

    def test_is_descendant_of_nonexistent_type_returns_false(self) -> None:
        """is_descendant_of with a type that does not exist returns False."""
        source = b"def foo():\n    return 1\n"
        root = _parse(source, "python")
        func = _find_first(root, "function_definition")
        assert func is not None
        assert func.is_descendant_of("nonexistent_xyz_type") is False


# ---------------------------------------------------------------------------
# Section 2.2: children_by_field_name("name") for function declarations
# ---------------------------------------------------------------------------


class TestChildrenByFieldName:
    """children_by_field_name returns correct named field children."""

    _FUNCTION_PARAMS = [
        (
            "java",
            b"public class T { void myMethod() {} }",
            "method_declaration",
            "myMethod",
        ),
        ("kotlin", b"fun myFun() {}", "function_declaration", "myFun"),
        ("go", b"package p\nfunc myFunc() {}", "function_declaration", "myFunc"),
        ("python", b"def my_func():\n    pass\n", "function_definition", "my_func"),
        ("typescript", b"function myFunc() {}", "function_declaration", "myFunc"),
        ("javascript", b"function myFunc() {}", "function_declaration", "myFunc"),
    ]

    @pytest.mark.parametrize(
        "lang,source,decl_type,expected_name",
        _FUNCTION_PARAMS,
        ids=[p[0] for p in _FUNCTION_PARAMS],
    )
    def test_name_field_returns_nonempty_list(
        self, lang: str, source: bytes, decl_type: str, expected_name: str
    ) -> None:
        """children_by_field_name('name') returns non-empty list for function declarations."""
        root = _parse(source, lang)
        decl = _find_first(root, decl_type)
        assert decl is not None, f"{lang}: {decl_type!r} not found in AST"

        name_nodes = decl.children_by_field_name("name")
        assert len(name_nodes) > 0, (
            f"{lang}: children_by_field_name('name') returned empty list for {decl_type!r}"
        )

    @pytest.mark.parametrize(
        "lang,source,decl_type,expected_name",
        _FUNCTION_PARAMS,
        ids=[p[0] for p in _FUNCTION_PARAMS],
    )
    def test_name_field_text_matches_source(
        self, lang: str, source: bytes, decl_type: str, expected_name: str
    ) -> None:
        """First name field child text equals the function name from source."""
        root = _parse(source, lang)
        decl = _find_first(root, decl_type)
        assert decl is not None

        name_nodes = decl.children_by_field_name("name")
        assert len(name_nodes) > 0

        first_name = name_nodes[0].text
        assert isinstance(first_name, str), (
            f"{lang}: name node text type is {type(first_name)}, expected str"
        )
        assert first_name == expected_name, (
            f"{lang}: name text {first_name!r} != expected {expected_name!r}"
        )

    def test_nonexistent_field_returns_empty_list(self) -> None:
        """children_by_field_name with non-existent field returns empty list."""
        root = _parse(b"def foo():\n    pass\n", "python")
        func = _find_first(root, "function_definition")
        assert func is not None
        result = func.children_by_field_name("nonexistent_field_xyz_abc")
        assert result == []

    def test_returns_xray_nodes(self) -> None:
        """children_by_field_name returns a list of XRayNode instances."""
        from code_indexer.xray.xray_node import XRayNode

        root = _parse(b"def foo():\n    pass\n", "python")
        func = _find_first(root, "function_definition")
        assert func is not None
        name_nodes = func.children_by_field_name("name")
        for node in name_nodes:
            assert isinstance(node, XRayNode)


# ---------------------------------------------------------------------------
# Section 2.3: Byte-range round-trip integrity
# ---------------------------------------------------------------------------


class TestByteRangeIntegrity:
    """source[start_byte:end_byte] must equal node.text for all nodes."""

    _SOURCES = [
        ("python", b"def foo(x):\n    return x + 1\n"),
        ("java", b"public class T { void foo() { return; } }"),
        ("typescript", b"function foo(x: number): number { return x; }"),
        ("go", b"package main\nfunc foo(x int) int { return x }\n"),
        ("kotlin", b"fun foo(x: Int): Int { return x }\n"),
    ]

    @pytest.mark.parametrize(
        "lang,source",
        _SOURCES,
        ids=[p[0] for p in _SOURCES],
    )
    def test_byte_range_round_trip(self, lang: str, source: bytes) -> None:
        """For each node: source[start_byte:end_byte] decodes to node.text."""
        root = _parse(source, lang)
        nodes = _collect_up_to_n(root, 15)

        assert len(nodes) >= 3, f"{lang}: expected at least 3 nodes, got {len(nodes)}"

        mismatches = []
        for node in nodes:
            sliced = source[node.start_byte : node.end_byte].decode(
                "utf-8", errors="replace"
            )
            if sliced != node.text:
                mismatches.append(
                    f"  type={node.type!r} start={node.start_byte} end={node.end_byte} "
                    f"sliced={sliced!r} text={node.text!r}"
                )

        assert not mismatches, f"{lang}: byte-range round-trip failures:\n" + "\n".join(
            mismatches
        )


# ---------------------------------------------------------------------------
# Section 2.4: parent chain terminates at root (parent of root is None)
# ---------------------------------------------------------------------------


class TestParentChain:
    """parent chain terminates at root for every language."""

    @pytest.mark.parametrize(
        "lang,source",
        [
            ("python", b"def foo():\n    return 1\n"),
            ("java", b"public class T { void m() {} }"),
            ("typescript", b"function foo() { return 1; }"),
        ],
        ids=["python", "java", "typescript"],
    )
    def test_parent_chain_terminates_at_root(self, lang: str, source: bytes) -> None:
        """Walking parent from any node eventually reaches root (parent is None)."""
        root = _parse(source, lang)

        # Pick a deep node to walk from
        # Collect all nodes and pick one from the middle
        all_nodes = _collect_up_to_n(root, 20)
        # Use a node from the second half (deeper in tree)
        start_node = (
            all_nodes[len(all_nodes) // 2] if len(all_nodes) > 2 else all_nodes[-1]
        )

        # Walk up — must terminate in bounded steps (guard against infinite loop)
        max_steps = 1000
        current = start_node
        steps = 0
        while current.parent is not None:
            current = current.parent
            steps += 1
            assert steps < max_steps, (
                f"{lang}: parent chain did not terminate within {max_steps} steps"
            )

        # At the end, parent must be None
        assert current.parent is None, (
            f"{lang}: root node's parent is not None: {current.parent}"
        )

    def test_root_parent_is_none_python(self) -> None:
        """Root node parent is None (Python)."""
        root = _parse(b"x = 1\n", "python")
        assert root.parent is None

    def test_root_parent_is_none_java(self) -> None:
        """Root node parent is None (Java)."""
        root = _parse(b"public class T {}", "java")
        assert root.parent is None

    def test_root_parent_is_none_go(self) -> None:
        """Root node parent is None (Go)."""
        root = _parse(b"package main\n", "go")
        assert root.parent is None

    def test_parent_returns_xray_node(self) -> None:
        """parent property returns an XRayNode instance (not raw tree_sitter.Node)."""
        from code_indexer.xray.xray_node import XRayNode

        root = _parse(b"x = 1\n", "python")
        child = root.children[0] if root.children else None
        if child is not None:
            parent = child.parent
            assert parent is not None
            assert isinstance(parent, XRayNode)


# ---------------------------------------------------------------------------
# Section 2.5: children_by_field_name("body") for function body access
# ---------------------------------------------------------------------------


class TestBodyField:
    """children_by_field_name('body') returns expected body child."""

    def test_python_function_body_field(self) -> None:
        """Python function_definition has a 'body' field returning a block node."""
        root = _parse(b"def foo(x):\n    return x\n", "python")
        func = _find_first(root, "function_definition")
        assert func is not None, "function_definition not found"

        body_nodes = func.children_by_field_name("body")
        assert len(body_nodes) > 0, "Python function 'body' field returned empty list"
        assert body_nodes[0].type == "block", (
            f"Expected 'block', got {body_nodes[0].type!r}"
        )

    def test_java_method_body_field(self) -> None:
        """Java method_declaration has a 'body' field returning a block node."""
        root = _parse(b"public class T { void foo() { return; } }", "java")
        method = _find_first(root, "method_declaration")
        assert method is not None, "method_declaration not found"

        body_nodes = method.children_by_field_name("body")
        assert len(body_nodes) > 0, "Java method 'body' field returned empty list"
        assert body_nodes[0].type == "block", (
            f"Expected 'block', got {body_nodes[0].type!r}"
        )

    def test_typescript_function_body_field(self) -> None:
        """TypeScript function_declaration has a 'body' field returning statement_block."""
        root = _parse(b"function foo(x: number): number { return x; }", "typescript")
        func = _find_first(root, "function_declaration")
        assert func is not None, "function_declaration not found"

        body_nodes = func.children_by_field_name("body")
        assert len(body_nodes) > 0, (
            "TypeScript function 'body' field returned empty list"
        )
        assert body_nodes[0].type == "statement_block", (
            f"Expected 'statement_block', got {body_nodes[0].type!r}"
        )
