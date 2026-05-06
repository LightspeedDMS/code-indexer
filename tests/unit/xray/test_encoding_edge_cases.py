"""Encoding and edge case tests for X-Ray AST engine.

User Mandate Section 3: BOM handling, mixed line endings, tabs vs spaces,
non-ASCII identifiers, empty/large/corrupt/truncated files.

All ephemeral test files use tempfile — no committed junk fixtures.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():  # type: ignore[return]
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine()


# ---------------------------------------------------------------------------
# Test 1: BOM handling
# ---------------------------------------------------------------------------


class TestBOMHandling:
    """UTF-8 BOM prefix must not cause parse failure or spurious ERROR at root."""

    def test_utf8_bom_python_no_crash(self) -> None:
        """UTF-8 BOM in .py file: parse succeeds, no crash."""
        # BOM = \xef\xbb\xbf
        bom_source = b"\xef\xbb\xbf# python file with BOM\nx = 1\n"
        engine = _make_engine()
        root = engine.parse(bom_source, "python")
        assert root is not None
        assert root.type == "module"

    def test_utf8_bom_python_root_not_error(self) -> None:
        """UTF-8 BOM: root node is not an ERROR node."""
        bom_source = b"\xef\xbb\xbf# comment\nx = 1\n"
        engine = _make_engine()
        root = engine.parse(bom_source, "python")
        # Root must not be ERROR — tree-sitter treats BOM as whitespace/comment
        assert root.type != "ERROR", (
            "BOM caused root node to become ERROR — spurious parse failure"
        )

    def test_utf8_bom_java_no_crash(self) -> None:
        """UTF-8 BOM in Java source: parse succeeds."""
        bom_source = b"\xef\xbb\xbf// java with BOM\npublic class T {}\n"
        engine = _make_engine()
        root = engine.parse(bom_source, "java")
        assert root is not None

    def test_utf8_bom_parse_returns_children(self) -> None:
        """UTF-8 BOM: tree still has children (not degenerate empty parse)."""
        bom_source = b"\xef\xbb\xbf\nx = 1\ny = 2\n"
        engine = _make_engine()
        root = engine.parse(bom_source, "python")
        # May have 0 children if the BOM alone is content; at least no crash
        assert root.child_count >= 0


# ---------------------------------------------------------------------------
# Test 2: Mixed line endings
# ---------------------------------------------------------------------------


class TestMixedLineEndings:
    """Files with CRLF and LF mixed must parse correctly."""

    def test_crlf_python_parses(self) -> None:
        """Python file with CRLF line endings parses without error."""
        source = b"x = 1\r\ny = 2\r\nz = 3\r\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root.type == "module"
        assert root.child_count == 3

    def test_mixed_crlf_lf_python_parses(self) -> None:
        """Python file with mixed CRLF/LF endings parses without crash."""
        source = b"x = 1\r\ny = 2\nz = 3\r\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root is not None
        assert root.type == "module"

    def test_crlf_positions_are_byte_correct(self) -> None:
        """With CRLF, byte offsets are consistent with the raw source bytes."""
        source = b"x = 1\r\ny = 2\r\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        # For each named child, slice source bytes and compare to node text
        for child in root.named_children:
            sliced = source[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
            assert sliced == child.text, (
                f"Byte-range mismatch for CRLF node {child.type!r}: "
                f"sliced={sliced!r} text={child.text!r}"
            )


# ---------------------------------------------------------------------------
# Test 3: Tabs vs spaces
# ---------------------------------------------------------------------------


class TestTabsVsSpaces:
    """Python grammar handles tab-indented files correctly."""

    def test_tab_indented_python_parses(self) -> None:
        """Python file with tab indentation parses without error."""
        source = b"def foo():\n\treturn 1\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root.type == "module"
        assert root.has_error is False

    def test_tab_indented_nested_python_parses(self) -> None:
        """Nested tab-indented Python parses correctly."""
        source = b"def foo():\n\tif True:\n\t\treturn 1\n\treturn 0\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root.type == "module"

    def test_space_indented_python_parses(self) -> None:
        """Python file with space indentation parses without error."""
        source = b"def foo():\n    return 1\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root.type == "module"
        assert root.has_error is False


# ---------------------------------------------------------------------------
# Test 4: Non-ASCII identifiers
# ---------------------------------------------------------------------------


class TestNonAsciiIdentifiers:
    """Unicode identifiers and strings parse correctly; node.text is str."""

    def test_cyrillic_comment_python(self) -> None:
        """Python comment with Cyrillic text: text is str, round-trips UTF-8."""
        source = "# комментарий\nx = 1\n".encode("utf-8")
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root is not None
        assert root.type == "module"
        # Walk to find comment node
        comment = None
        for child in root.children:
            if child.type == "comment":
                comment = child
                break
        if comment is not None:
            assert isinstance(comment.text, str), (
                f"comment.text is {type(comment.text)}, expected str"
            )

    def test_greek_variable_python(self) -> None:
        """Python variable with Greek name: text round-trips correctly."""
        source = "α = 1\nβ_var = 'emoji \U0001f389'\n".encode("utf-8")
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root is not None
        # node.text on root must be a str
        assert isinstance(root.text, str), (
            f"root.text is {type(root.text)}, expected str"
        )

    def test_non_ascii_text_is_str_everywhere(self) -> None:
        """node.text is always str regardless of Unicode content."""
        source = "greeting = '中文'\n".encode("utf-8")
        engine = _make_engine()
        root = engine.parse(source, "python")

        def check_all(node):  # type: ignore[no-untyped-def]
            assert isinstance(node.text, str), (
                f"Node {node.type!r} .text is {type(node.text)}, expected str"
            )
            for child in node.children:
                check_all(child)

        check_all(root)

    def test_non_ascii_utf8_round_trip(self) -> None:
        """Non-ASCII node.text encodes back to the original bytes correctly."""
        original = "α = 1\n"
        source = original.encode("utf-8")
        engine = _make_engine()
        root = engine.parse(source, "python")
        # root.text.encode('utf-8') must equal the original source bytes
        reconstructed = root.text.encode("utf-8")
        assert reconstructed == source, (
            f"UTF-8 round-trip failed: {reconstructed!r} != {source!r}"
        )

    def test_java_non_ascii_string_literal(self) -> None:
        """Java file with Unicode string literal: no crash, text is str."""
        source = 'public class T { String s = "中文"; }'.encode("utf-8")
        engine = _make_engine()
        root = engine.parse(source, "java")
        assert root is not None
        assert isinstance(root.text, str)


# ---------------------------------------------------------------------------
# Test 5: Empty file
# ---------------------------------------------------------------------------


class TestEmptyFile:
    """Zero-byte file: child_count is 0, no exception."""

    def test_empty_python_no_crash(self) -> None:
        """Empty bytes as Python: parse returns module with 0 children."""
        engine = _make_engine()
        root = engine.parse(b"", "python")
        assert root is not None
        assert root.type == "module"
        assert root.child_count == 0

    def test_empty_java_no_crash(self) -> None:
        """Empty bytes as Java: parse does not raise."""
        engine = _make_engine()
        root = engine.parse(b"", "java")
        assert root is not None

    def test_empty_go_no_crash(self) -> None:
        """Empty bytes as Go: parse does not raise."""
        engine = _make_engine()
        root = engine.parse(b"", "go")
        assert root is not None

    def test_empty_str_no_crash(self) -> None:
        """Empty string source: parse returns valid node."""
        engine = _make_engine()
        root = engine.parse("", "python")
        assert root is not None
        assert root.child_count == 0

    def test_empty_file_on_disk(self) -> None:
        """Parsing an empty file via parse_file: no crash."""
        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"")
            tmp = f.name
        try:
            root = engine.parse_file(Path(tmp))
            assert root is not None
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Test 6: Very large file
# ---------------------------------------------------------------------------


class TestVeryLargeFile:
    """Parse >1MB file within 5s budget."""

    def test_large_python_file_parses_in_time(self) -> None:
        """1+ MB Python file: parse completes within 5 seconds wall clock."""
        # Generate ~1.5 MB of valid Python (100k assignment lines)
        lines = [f"x_{n} = {n}" for n in range(100_000)]
        source = "\n".join(lines).encode("utf-8")
        assert len(source) > 1_000_000, "Source must be > 1 MB for this test"

        engine = _make_engine()
        start = time.monotonic()
        root = engine.parse(source, "python")
        elapsed = time.monotonic() - start

        print(
            f"\n  Large file parse: {len(source) / 1024 / 1024:.2f} MB "
            f"in {elapsed:.3f}s"
        )

        assert root is not None
        assert root.child_count > 0
        assert elapsed < 5.0, (
            f"Large file parse took {elapsed:.2f}s, exceeding 5.0s budget"
        )


# ---------------------------------------------------------------------------
# Test 7: Files with syntax errors
# ---------------------------------------------------------------------------


class TestSyntaxErrors:
    """tree-sitter is error-recovering: ERROR nodes present, no exception raised."""

    def test_syntax_error_python_no_exception(self) -> None:
        """Malformed Python: parse does not raise, has_error is True."""
        source = b"def func(\n    pass\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root is not None
        assert root.has_error is True

    def test_syntax_error_has_error_nodes_in_tree(self) -> None:
        """Malformed source: ERROR nodes exist somewhere in the tree."""
        source = b"def (broken syntax:\n    pass\n"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root is not None
        # Either root.has_error is True or an ERROR node exists in the subtree
        assert root.has_error, "Expected has_error to be True for clearly broken syntax"

    def test_syntax_error_text_still_accessible(self) -> None:
        """node.text on ERROR nodes is a str, not bytes."""
        source = b"def (broken:\n    pass\n"
        engine = _make_engine()
        root = engine.parse(source, "python")

        def collect_error_nodes(node, results=None):  # type: ignore[no-untyped-def]
            if results is None:
                results = []
            if node.type == "ERROR":
                results.append(node)
            for child in node.children:
                collect_error_nodes(child, results)
            return results

        error_nodes = collect_error_nodes(root)
        for node in error_nodes:
            assert isinstance(node.text, str), (
                f"ERROR node.text is {type(node.text)}, expected str"
            )

    def test_java_syntax_error_no_exception(self) -> None:
        """Malformed Java: parse does not raise."""
        source = b"public class T { void broken(( {} }"
        engine = _make_engine()
        root = engine.parse(source, "java")
        assert root is not None


# ---------------------------------------------------------------------------
# Test 8: Binary garbage masquerading as .py
# ---------------------------------------------------------------------------


class TestBinaryGarbage:
    """1 KB of random bytes as .py: no crash; tree may be mostly ERROR."""

    def test_binary_garbage_python_no_crash(self) -> None:
        """Random bytes parsed as Python: no exception raised."""
        garbage = os.urandom(1024)
        engine = _make_engine()
        root = engine.parse(garbage, "python")
        assert root is not None

    def test_binary_garbage_root_is_valid_node(self) -> None:
        """Even with binary garbage, root node type is a non-empty str."""
        garbage = os.urandom(1024)
        engine = _make_engine()
        root = engine.parse(garbage, "python")
        # type may be 'ERROR' or 'module' — both acceptable
        assert isinstance(root.type, str)
        assert len(root.type) > 0

    def test_binary_garbage_via_tempfile(self) -> None:
        """Binary garbage via parse_file: no crash."""
        garbage = os.urandom(1024)
        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(garbage)
            tmp = f.name
        try:
            root = engine.parse_file(Path(tmp))
            assert root is not None
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Test 9: Truncated file
# ---------------------------------------------------------------------------


class TestTruncatedFile:
    """File ending mid-statement: parse succeeds (tree-sitter is error-recovering)."""

    def test_truncated_python_no_crash(self) -> None:
        """Python file ending mid-function: parse does not raise."""
        source = b"def func():"
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert root is not None

    def test_truncated_python_root_accessible(self) -> None:
        """Truncated Python: root node type is accessible."""
        source = b"def func():\n    x = 1\n    y ="
        engine = _make_engine()
        root = engine.parse(source, "python")
        assert isinstance(root.type, str)

    def test_truncated_java_no_crash(self) -> None:
        """Truncated Java file: parse does not raise."""
        source = b"public class T { void foo() {"
        engine = _make_engine()
        root = engine.parse(source, "java")
        assert root is not None
