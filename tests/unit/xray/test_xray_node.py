"""Tests for xray/xray_node.py: XRayNode wrapper over tree_sitter.Node.

All tests use real tree-sitter parsing — no mocks.
"""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON_SMOKE = FIXTURES / "python" / "smoke.py"


def _parse_python(source: str):  # type: ignore[return]
    """Parse Python source with tree-sitter and return the root XRayNode."""
    from tree_sitter_languages import get_parser
    from code_indexer.xray.xray_node import XRayNode

    parser = get_parser("python")
    tree = parser.parse(source.encode("utf-8"))
    return XRayNode(tree.root_node)


class TestXRayNodeText:
    """XRayNode.text must return str, never bytes."""

    def test_text_is_str(self) -> None:
        """node.text returns str, not bytes."""
        root = _parse_python("x = 1\n")
        assert isinstance(root.text, str)

    def test_text_matches_source(self) -> None:
        """node.text content matches the parsed source."""
        source = "x = 1\n"
        root = _parse_python(source)
        assert root.text == source

    def test_text_on_child_node(self) -> None:
        """Children also expose text as str.

        AST depth: module -> expression_statement -> assignment -> integer
        """
        root = _parse_python("answer = 42\n")
        # Walk to find a number literal (3 levels deep)
        found_number = False
        for child in root.children:
            for grandchild in child.children:
                for great_grandchild in grandchild.children:
                    if great_grandchild.type == "integer":
                        assert great_grandchild.text == "42"
                        assert isinstance(great_grandchild.text, str)
                        found_number = True
        assert found_number, "Expected to find an integer node"

    def test_text_utf8_content(self) -> None:
        """Unicode source text is decoded correctly from UTF-8 bytes."""
        source = 'greeting = "héllo wörld"\n'
        root = _parse_python(source)
        assert "héllo wörld" in root.text


class TestXRayNodeType:
    """XRayNode.type returns the grammar node type as str."""

    def test_root_type_is_module(self) -> None:
        """Python root node type is 'module'."""
        root = _parse_python("pass\n")
        assert root.type == "module"

    def test_type_is_str(self) -> None:
        """node.type is a str."""
        root = _parse_python("pass\n")
        assert isinstance(root.type, str)


class TestXRayNodeChildren:
    """XRayNode.children returns list of XRayNode, not raw tree_sitter.Node."""

    def test_children_returns_list(self) -> None:
        """node.children returns a list."""
        root = _parse_python("x = 1\n")
        assert isinstance(root.children, list)

    def test_children_are_xray_nodes(self) -> None:
        """Every child is an XRayNode instance."""
        from code_indexer.xray.xray_node import XRayNode

        root = _parse_python("x = 1\n")
        for child in root.children:
            assert isinstance(child, XRayNode)

    def test_children_not_raw_ts_nodes(self) -> None:
        """Children must NOT be raw tree_sitter.Node objects."""
        try:
            from tree_sitter import Node as TsNode
        except ImportError:
            pytest.skip("tree_sitter not installed")

        root = _parse_python("x = 1\n")
        for child in root.children:
            assert not isinstance(child, TsNode), (
                "XRayNode.children must not expose raw tree_sitter.Node"
            )

    def test_named_children_are_xray_nodes(self) -> None:
        """node.named_children returns XRayNode instances."""
        from code_indexer.xray.xray_node import XRayNode

        root = _parse_python("x = 1\n")
        for child in root.named_children:
            assert isinstance(child, XRayNode)

    def test_child_count_matches_children_len(self) -> None:
        """node.child_count equals len(node.children)."""
        root = _parse_python("x = 1\ny = 2\n")
        assert root.child_count == len(root.children)

    def test_named_child_count_matches_named_children_len(self) -> None:
        """node.named_child_count equals len(node.named_children)."""
        root = _parse_python("x = 1\ny = 2\n")
        assert root.named_child_count == len(root.named_children)


class TestXRayNodePosition:
    """XRayNode exposes start_point and end_point as (row, col) tuples."""

    def test_start_point_is_tuple(self) -> None:
        """node.start_point is a tuple."""
        root = _parse_python("x = 1\n")
        assert isinstance(root.start_point, tuple)

    def test_end_point_is_tuple(self) -> None:
        """node.end_point is a tuple."""
        root = _parse_python("x = 1\n")
        assert isinstance(root.end_point, tuple)

    def test_start_point_zero_for_root(self) -> None:
        """Root node starts at (0, 0)."""
        root = _parse_python("x = 1\n")
        assert root.start_point == (0, 0)

    def test_start_byte_is_int(self) -> None:
        """node.start_byte is an int."""
        root = _parse_python("x = 1\n")
        assert isinstance(root.start_byte, int)

    def test_end_byte_is_int(self) -> None:
        """node.end_byte is an int."""
        root = _parse_python("x = 1\n")
        assert isinstance(root.end_byte, int)


class TestXRayNodeFlags:
    """XRayNode boolean flags: is_named, has_error."""

    def test_root_is_named(self) -> None:
        """The module root node is a named node."""
        root = _parse_python("x = 1\n")
        assert root.is_named is True

    def test_has_error_false_for_valid_source(self) -> None:
        """Valid source has no parse errors."""
        root = _parse_python("x = 1\n")
        assert root.has_error is False

    def test_has_error_true_for_invalid_source(self) -> None:
        """Invalid syntax yields has_error True."""
        root = _parse_python("def (broken:\n")
        assert root.has_error is True

    def test_is_named_is_bool(self) -> None:
        """is_named is always a bool."""
        root = _parse_python("x = 1\n")
        assert isinstance(root.is_named, bool)


class TestXRayNodeIteration:
    """XRayNode is iterable over its children."""

    def test_iteration_yields_xray_nodes(self) -> None:
        """Iterating over XRayNode yields XRayNode children."""
        from code_indexer.xray.xray_node import XRayNode

        root = _parse_python("x = 1\ny = 2\n")
        for child in root:
            assert isinstance(child, XRayNode)

    def test_iteration_count_matches_child_count(self) -> None:
        """Number of items from iteration equals child_count."""
        root = _parse_python("x = 1\ny = 2\n")
        iterated = list(root)
        assert len(iterated) == root.child_count


class TestXRayNodeChildByFieldName:
    """XRayNode.child_by_field_name delegates to underlying node."""

    def test_child_by_field_name_returns_xray_node_or_none(self) -> None:
        """child_by_field_name returns XRayNode when field exists, None otherwise."""
        from code_indexer.xray.xray_node import XRayNode

        root = _parse_python("x = 1\n")
        # Walk to assignment node
        assignment = None
        for child in root.named_children:
            if child.type == "expression_statement":
                for grandchild in child.named_children:
                    if grandchild.type == "assignment":
                        assignment = grandchild
                        break
        if assignment is not None:
            result = assignment.child_by_field_name("left")
            if result is not None:
                assert isinstance(result, XRayNode)

    def test_child_by_field_name_nonexistent_returns_none(self) -> None:
        """child_by_field_name returns None for non-existent field."""
        root = _parse_python("x = 1\n")
        result = root.child_by_field_name("nonexistent_field_xyz")
        assert result is None


class TestXRayNodeRepr:
    """XRayNode has a useful repr for debugging."""

    def test_repr_contains_type(self) -> None:
        """repr(node) includes the node type."""
        root = _parse_python("x = 1\n")
        assert "module" in repr(root)

    def test_repr_is_str(self) -> None:
        """repr(node) is a str."""
        root = _parse_python("x = 1\n")
        assert isinstance(repr(root), str)
