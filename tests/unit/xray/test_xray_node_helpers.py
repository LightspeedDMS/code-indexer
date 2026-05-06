"""Tests for XRayNode helper methods: is_in_try_resources, enclosing_method_body.

All tests use real tree-sitter Java parsing — no mocks.

Story #993 Improvement 2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.xray.xray_node import XRayNode


def _parse_java(source: bytes) -> "XRayNode":
    """Parse Java source with tree-sitter and return the root XRayNode."""
    from code_indexer.xray.ast_engine import AstSearchEngine

    engine = AstSearchEngine()
    return engine.parse(source, "java")


# Java source with try-with-resources and a bare assignment.
# Two pool.getConnection() invocations:
#   1. Inside the try-with-resources resource specification (line 3)
#   2. In the bare assignment statement (line 6)
_JAVA_SRC = b"""\
class Demo {
    void process() {
        try (Connection c = pool.getConnection()) {
            c.execute();
        }
        Connection bare = pool.getConnection();
    }
}
"""

# Byte offset of the first "pool.getConnection()" (inside try-with-resources)
_TRY_RESOURCES_OFFSET = _JAVA_SRC.index(b"pool.getConnection()")

# Byte offset of the second "pool.getConnection()" (bare assignment)
_BARE_ASSIGNMENT_OFFSET = _JAVA_SRC.index(
    b"pool.getConnection()", _TRY_RESOURCES_OFFSET + 1
)


class TestIsInTryResources:
    """is_in_try_resources() detects Java try-with-resources scope."""

    def _get_invocation_at(self, offset: int) -> "XRayNode":
        """Find the method_invocation node at the given byte offset."""
        root = _parse_java(_JAVA_SRC)
        invocations = root.descendants_of_type("method_invocation")
        for node in invocations:
            if node.start_byte <= offset < node.end_byte:
                return node
        raise AssertionError(f"No method_invocation node found at byte offset {offset}")

    def test_invocation_inside_try_resources_returns_true(self) -> None:
        """AC2.1: pool.getConnection() inside resource specification -> True."""
        node = self._get_invocation_at(_TRY_RESOURCES_OFFSET)
        assert node.is_in_try_resources() is True, (
            f"Expected is_in_try_resources()=True for node at offset "
            f"{_TRY_RESOURCES_OFFSET}, text={node.text!r}"
        )

    def test_bare_assignment_invocation_returns_false(self) -> None:
        """AC2.2: pool.getConnection() in bare assignment -> False."""
        node = self._get_invocation_at(_BARE_ASSIGNMENT_OFFSET)
        assert node.is_in_try_resources() is False, (
            f"Expected is_in_try_resources()=False for node at offset "
            f"{_BARE_ASSIGNMENT_OFFSET}, text={node.text!r}"
        )


class TestEnclosingMethodBody:
    """enclosing_method_body() finds the body block of the enclosing method."""

    def test_statement_inside_method_returns_block(self) -> None:
        """AC2.3: method_invocation inside method -> enclosing_method_body returns block."""
        from code_indexer.xray.xray_node import XRayNode

        root = _parse_java(_JAVA_SRC)
        invocations = root.descendants_of_type("method_invocation")
        assert invocations, "Expected at least one method_invocation"

        # Use the first invocation (inside the method body)
        node = invocations[0]
        body = node.enclosing_method_body()
        assert body is not None, (
            f"Expected enclosing_method_body() to return a node, got None "
            f"for node type={node.type!r}, text={node.text!r}"
        )
        assert isinstance(body, XRayNode)
        assert body.type == "block", f"Expected body.type == 'block', got {body.type!r}"

    def test_root_node_returns_none(self) -> None:
        """AC2.4: root node (compilation_unit) -> enclosing_method_body returns None."""
        root = _parse_java(_JAVA_SRC)
        body = root.enclosing_method_body()
        assert body is None, (
            f"Expected enclosing_method_body() to return None for root node "
            f"(type={root.type!r}), got {body!r}"
        )
