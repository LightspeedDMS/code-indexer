"""Tests for the per-match-position evaluator contract in XRaySearchEngine.

Verifies that:
1. find_enclosing_node (utility) returns the deepest AST node containing a
   byte offset — this helper is still used internally and must remain correct.
2. _line_to_byte_offset converts 1-based line number to byte offset correctly.
3. In CONTENT mode, sandbox.run is called with node=root (NOT the enclosing node)
   and receives match_byte_offset / match_line_number / match_line_content globals.
4. In FILENAME mode, sandbox.run is called with node=root and
   match_byte_offset=None / match_line_number=None / match_line_content=None.
5. match dict has real line_number and code_snippet from Phase 1 positions.
6. Error entries carry real line_number (not 0 or None).
7. Evaluator code can walk DOWN from root via descendants_of_type to find
   specific nodes — the canonical field-use case.

All tests use real tree-sitter parsing via AstSearchEngine — no mocks for AST.
Sandbox is mocked only where behaviour testing requires controlling its output.
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest


@pytest.fixture
def search_engine():
    """XRaySearchEngine instance; skip if tree-sitter extras not installed."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


@pytest.fixture
def ast_engine():
    """AstSearchEngine instance; skip if tree-sitter extras not installed."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine()


# ---------------------------------------------------------------------------
# find_enclosing_node helper (utility still exists and must be correct)
# ---------------------------------------------------------------------------


class TestFindEnclosingNode:
    """find_enclosing_node returns the deepest node enclosing a byte offset."""

    def test_byte_offset_zero_returns_node_containing_start(self, ast_engine):
        """Byte offset 0 should return a node that starts at or before byte 0."""
        from code_indexer.xray.ast_engine import find_enclosing_node

        source = b"x = 1\n"
        root = ast_engine.parse(source, "python")
        node = find_enclosing_node(root, 0)
        assert node is not None
        assert node.start_byte <= 0

    def test_byte_offset_inside_identifier_returns_identifier_node(self, ast_engine):
        """Byte offset inside 'foo' in 'foo()' should return a node for that identifier."""
        from code_indexer.xray.ast_engine import find_enclosing_node

        source = b"foo()\n"
        root = ast_engine.parse(source, "python")
        # 'foo' starts at byte 0
        node = find_enclosing_node(root, 0)
        # Must be a node that starts at 0
        assert node.start_byte <= 0
        assert node.end_byte >= 1

    def test_byte_offset_in_nested_call_returns_narrower_node(self, ast_engine):
        """Byte offset inside a nested call returns a narrower node than root."""
        from code_indexer.xray.ast_engine import find_enclosing_node

        source = b"def foo():\n    bar()\n"
        root = ast_engine.parse(source, "python")
        bar_offset = source.index(b"bar")
        node = find_enclosing_node(root, bar_offset)
        # Must be narrower (smaller span) than root
        root_span = root.end_byte - root.start_byte
        node_span = node.end_byte - node.start_byte
        assert node_span <= root_span
        # And the node must actually contain the offset
        assert node.start_byte <= bar_offset < node.end_byte

    def test_byte_offset_past_file_end_returns_root(self, ast_engine):
        """Byte offset past end of file should return root (defensive fallback)."""
        from code_indexer.xray.ast_engine import find_enclosing_node

        source = b"x = 1\n"
        root = ast_engine.parse(source, "python")
        node = find_enclosing_node(root, 99999)
        # Should return root (no child contains offset 99999)
        assert node.type == root.type
        assert node.start_byte == root.start_byte

    def test_find_enclosing_node_always_returns_xray_node(self, ast_engine):
        """find_enclosing_node always returns an XRayNode, never None."""
        from code_indexer.xray.ast_engine import find_enclosing_node
        from code_indexer.xray.xray_node import XRayNode

        source = b"a = 1\n"
        root = ast_engine.parse(source, "python")
        for offset in [0, 1, 3, 5, 9999]:
            result = find_enclosing_node(root, offset)
            assert isinstance(result, XRayNode)


# ---------------------------------------------------------------------------
# _line_to_byte_offset helper
# ---------------------------------------------------------------------------


class TestLineToBytOffset:
    """_line_to_byte_offset converts 1-indexed line number to byte start offset."""

    def test_line_1_returns_0(self):
        """Line 1 starts at byte 0."""
        from code_indexer.xray.search_engine import _line_to_byte_offset

        source = "hello\nworld\n"
        assert _line_to_byte_offset(source, 1) == 0

    def test_line_2_returns_after_first_newline(self):
        """Line 2 starts at the byte after the first newline."""
        from code_indexer.xray.search_engine import _line_to_byte_offset

        source = "hello\nworld\n"
        # 'hello\n' is 6 bytes
        assert _line_to_byte_offset(source, 2) == 6

    def test_line_3_sums_first_two_lines(self):
        """Line 3 offset = len(line1) + 1 + len(line2) + 1."""
        from code_indexer.xray.search_engine import _line_to_byte_offset

        source = "ab\ncd\nef\n"
        # line1='ab\n'=3, line2='cd\n'=3, line3 at byte 6
        assert _line_to_byte_offset(source, 3) == 6

    def test_line_number_past_end_returns_source_length(self):
        """Line number beyond file line count returns len(source)."""
        from code_indexer.xray.search_engine import _line_to_byte_offset

        source = "x\n"
        result = _line_to_byte_offset(source, 9999)
        assert result == len(source)

    def test_line_number_zero_or_negative_returns_0(self):
        """Line number <= 0 is clamped to byte 0 (line 1)."""
        from code_indexer.xray.search_engine import _line_to_byte_offset

        source = "x\ny\n"
        assert _line_to_byte_offset(source, 0) == 0
        assert _line_to_byte_offset(source, -1) == 0


# ---------------------------------------------------------------------------
# New contract: sandbox called with node=root in content mode
# ---------------------------------------------------------------------------


class TestSandboxReceivesRootNodeInContentMode:
    """In content mode, sandbox.run must receive node=root, not the enclosing node."""

    def test_node_kwarg_is_root_in_content_mode(self, search_engine, tmp_path):
        """In content mode, the node kwarg passed to sandbox.run is the file root."""
        py_file = tmp_path / "test.py"
        py_file.write_text("def foo():\n    bar()\n")

        from code_indexer.xray.sandbox import EvalResult

        captured_nodes = []

        def capturing_run(*args, **kwargs):
            captured_nodes.append(kwargs.get("node"))
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"bar",
                evaluator_code="return True",
                search_target="content",
            )

        # Must have been called at least once
        assert len(captured_nodes) >= 1
        # node must be the root (type == "module" for Python)
        for node in captured_nodes:
            assert node is not None
            assert node.type == "module"

    def test_node_kwarg_equals_root_kwarg_in_content_mode(
        self, search_engine, tmp_path
    ):
        """node kwarg and root kwarg must be the same object in content mode."""
        py_file = tmp_path / "test.py"
        py_file.write_text("foo()\n")

        from code_indexer.xray.sandbox import EvalResult

        captured_calls = []

        def capturing_run(*args, **kwargs):
            captured_calls.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(captured_calls) >= 1
        for kw in captured_calls:
            assert kw["node"] is kw["root"]

    def test_node_kwarg_is_root_in_filename_mode(self, search_engine, tmp_path):
        """In filename mode, node kwarg must also be the file root."""
        py_file = tmp_path / "foo_module.py"
        py_file.write_text("x = 1\n")

        from code_indexer.xray.sandbox import EvalResult

        captured_nodes = []

        def capturing_run(*args, **kwargs):
            captured_nodes.append(kwargs.get("node"))
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="filename",
            )

        assert len(captured_nodes) >= 1
        for node in captured_nodes:
            assert node is not None
            assert node.type == "module"


# ---------------------------------------------------------------------------
# New contract: match_byte_offset, match_line_number, match_line_content globals
# ---------------------------------------------------------------------------


class TestMatchPositionGlobalsInContentMode:
    """In content mode, sandbox receives match_byte_offset / match_line_number /
    match_line_content as non-None kwargs."""

    def test_match_byte_offset_is_not_none_in_content_mode(
        self, search_engine, tmp_path
    ):
        """match_byte_offset kwarg must be a non-None int in content mode."""
        (tmp_path / "f.py").write_text("foo()\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(captured) >= 1
        for kw in captured:
            assert "match_byte_offset" in kw
            assert kw["match_byte_offset"] is not None
            assert isinstance(kw["match_byte_offset"], int)

    def test_match_line_number_matches_regex_hit_line(self, search_engine, tmp_path):
        """match_line_number kwarg must equal the line number of the regex hit."""
        # 'foo' is on line 3
        (tmp_path / "f.py").write_text("a = 1\nb = 2\nfoo()\nd = 4\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(captured) == 1
        assert captured[0]["match_line_number"] == 3

    def test_match_line_content_contains_matched_text(self, search_engine, tmp_path):
        """match_line_content kwarg must contain the text of the matching line."""
        (tmp_path / "f.py").write_text("x = 1\nprepareStatement(sql)\ny = 3\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"prepareStatement",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(captured) == 1
        assert "prepareStatement" in captured[0]["match_line_content"]

    def test_multiple_matches_each_get_correct_line_number(
        self, search_engine, tmp_path
    ):
        """Three regex hits get three distinct match_line_number values."""
        (tmp_path / "f.py").write_text("foo()\nbar = 1\nfoo()\nbaz = 2\nfoo()\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(captured) == 3
        line_numbers = [kw["match_line_number"] for kw in captured]
        assert line_numbers == [1, 3, 5]


class TestMatchPositionGlobalsInFilenameMode:
    """In filename mode, sandbox receives None for all match_* globals."""

    def test_match_byte_offset_is_none_in_filename_mode(
        self, search_engine, tmp_path
    ):
        """match_byte_offset must be None in filename mode."""
        (tmp_path / "foo_module.py").write_text("x = 1\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="filename",
            )

        assert len(captured) >= 1
        for kw in captured:
            assert kw.get("match_byte_offset") is None

    def test_match_line_number_is_none_in_filename_mode(
        self, search_engine, tmp_path
    ):
        """match_line_number must be None in filename mode."""
        (tmp_path / "foo_module.py").write_text("x = 1\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="filename",
            )

        assert len(captured) >= 1
        for kw in captured:
            assert kw.get("match_line_number") is None

    def test_match_line_content_is_none_in_filename_mode(
        self, search_engine, tmp_path
    ):
        """match_line_content must be None in filename mode."""
        (tmp_path / "foo_module.py").write_text("x = 1\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="filename",
            )

        assert len(captured) >= 1
        for kw in captured:
            assert kw.get("match_line_content") is None


# ---------------------------------------------------------------------------
# Per-match-position: sandbox called once per position, not once per file
# ---------------------------------------------------------------------------


class TestEvaluatorCalledPerMatchPosition:
    """sandbox.run must be called once per Phase 1 match position."""

    def test_sandbox_called_once_per_match_position(self, search_engine, tmp_path):
        """File with 3 driver matches -> sandbox.run called exactly 3 times."""
        py_file = tmp_path / "multi.py"
        py_file.write_text("foo()\nfoo()\nfoo()\n")

        from code_indexer.xray.sandbox import EvalResult

        call_count = []

        def counting_run(*args, **kwargs):
            call_count.append(1)
            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=counting_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="content",
            )

        # 3 matches in the file => sandbox called 3 times
        assert len(call_count) == 3

    def test_sandbox_called_zero_times_for_file_with_no_phase1_matches(
        self, search_engine, tmp_path
    ):
        """Files that Phase 1 does not select result in zero sandbox calls."""
        (tmp_path / "no_match.py").write_text("def bar(): pass\n")

        call_count = []

        def counting_run(*args, **kwargs):
            call_count.append(1)
            from code_indexer.xray.sandbox import EvalResult

            return EvalResult(value=True)

        with patch.object(search_engine.sandbox, "run", side_effect=counting_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"XYZZY_NEVER_MATCHES",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(call_count) == 0


class TestMatchDictHasRealLineNumber:
    """match dict must contain the real line_number from Phase 1 positions."""

    def test_match_has_real_line_number(self, search_engine, tmp_path):
        """Match produced by evaluator=True contains the actual driver-match line."""
        py_file = tmp_path / "lines.py"
        py_file.write_text("a = 1\nb = 2\nfoo()\nd = 4\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code="return True",
            search_target="content",
        )

        assert len(result["matches"]) == 1
        match = result["matches"][0]
        assert match["line_number"] == 3  # 'foo()' is on line 3
        assert match["line_number"] is not None

    def test_multiple_matches_each_have_distinct_line_numbers(
        self, search_engine, tmp_path
    ):
        """Three matches on different lines => three distinct line_number values."""
        py_file = tmp_path / "multi.py"
        py_file.write_text("foo()\nbar = 1\nfoo()\nbaz = 2\nfoo()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code="return True",
            search_target="content",
        )

        assert len(result["matches"]) == 3
        line_numbers = [m["line_number"] for m in result["matches"]]
        # Lines 1, 3, 5 contain 'foo()'
        assert line_numbers == [1, 3, 5]

    def test_match_has_real_code_snippet(self, search_engine, tmp_path):
        """match['code_snippet'] contains the actual line text from Phase 1."""
        py_file = tmp_path / "snip.py"
        py_file.write_text("x = 1\nprepareStatement(sql)\ny = 3\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
        )

        assert len(result["matches"]) == 1
        snippet = result["matches"][0]["code_snippet"]
        assert snippet is not None
        assert "prepareStatement" in snippet

    def test_line_number_not_none_in_match(self, search_engine, tmp_path):
        """line_number must never be None in a successful match."""
        (tmp_path / "f.py").write_text("prepareStatement()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
        )

        assert result["matches"]
        for m in result["matches"]:
            assert m["line_number"] is not None
            assert m["code_snippet"] is not None


class TestEvaluationErrorCarriesLineNumber:
    """Error entries from per-position evaluation carry the real line_number."""

    def test_timeout_error_carries_real_line_number(self, search_engine, tmp_path):
        """EvaluatorTimeout error has the line number of the failing position."""
        (tmp_path / "f.py").write_text("a = 1\nfoo()\nb = 3\n")

        from code_indexer.xray.sandbox import EvalResult

        timeout_result = EvalResult(failure="evaluator_timeout")

        with patch.object(search_engine.sandbox, "run", return_value=timeout_result):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(result["evaluation_errors"]) >= 1
        err = result["evaluation_errors"][0]
        assert err["line_number"] == 2  # 'foo()' is on line 2
        assert err["error_type"] == "EvaluatorTimeout"

    def test_crash_error_carries_real_line_number(self, search_engine, tmp_path):
        """EvaluatorCrash error has the line number of the crashing position."""
        (tmp_path / "f.py").write_text("x = 1\nfoo()\n")

        from code_indexer.xray.sandbox import EvalResult

        crash_result = EvalResult(
            failure="evaluator_subprocess_died", detail="Subprocess died"
        )

        with patch.object(search_engine.sandbox, "run", return_value=crash_result):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code="return True",
                search_target="content",
            )

        assert len(result["evaluation_errors"]) >= 1
        err = result["evaluation_errors"][0]
        assert err["line_number"] == 2  # 'foo()' is on line 2


# ---------------------------------------------------------------------------
# Functional E2E: evaluator can walk DOWN from root via descendants_of_type
# ---------------------------------------------------------------------------


class TestFunctionalPerPositionE2E:
    """End-to-end: multiple matches in one file produces one result per match."""

    def test_three_matches_yield_three_result_entries(self, search_engine, tmp_path):
        """A file with 3 driver hits and evaluator=True produces exactly 3 matches."""
        py_file = tmp_path / "multi.py"
        py_file.write_text("a = 1\nb = 2\nfoo()\nfoo()\nfoo()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code="return True",
            search_target="content",
            timeout_seconds=30,
        )

        assert len(result["matches"]) == 3
        lines = [m["line_number"] for m in result["matches"]]
        assert lines == [3, 4, 5]
        snippets = [m["code_snippet"] for m in result["matches"]]
        for s in snippets:
            assert "foo" in s

    def test_evaluator_false_produces_no_match_for_position(
        self, search_engine, tmp_path
    ):
        """evaluator returning False means the position is not in the result."""
        py_file = tmp_path / "f.py"
        py_file.write_text("foo()\nfoo()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code="return False",
            search_target="content",
            timeout_seconds=30,
        )

        assert result["matches"] == []
        assert result["evaluation_errors"] == []

    def test_evaluator_walks_down_from_root_via_descendants_of_type(
        self, search_engine, tmp_path
    ):
        """Evaluator can walk DOWN from root via descendants_of_type to find nodes.

        This is the canonical field use-case: the evaluator receives the file
        root and uses descendants_of_type to find all function definitions,
        returning True when any are found. This pattern is natural and does NOT
        require inverting predicates.
        """
        py_file = tmp_path / "has_functions.py"
        py_file.write_text("def foo():\n    pass\n\nfoo()\n")

        # Evaluator looks for any function_definition nodes under root
        evaluator = (
            "funcs = root.descendants_of_type('function_definition')\n"
            "return len(funcs) > 0\n"
        )

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=evaluator,
            search_target="content",
            timeout_seconds=30,
        )

        # Should produce matches because the file has a function_definition
        assert len(result["matches"]) > 0

    def test_evaluator_uses_match_byte_offset_to_locate_context(
        self, search_engine, tmp_path
    ):
        """Evaluator can use match_byte_offset global (not None) in content mode.

        This validates that match_byte_offset is a real int exposed to the
        evaluator, which the evaluator can inspect. Here we simply verify
        the evaluator receives a non-None int and can reason about it.
        """
        (tmp_path / "f.py").write_text("foo()\n")

        # Evaluator checks that match_byte_offset is not None and is an int
        evaluator = "return match_byte_offset is not None\n"

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=evaluator,
            search_target="content",
            timeout_seconds=30,
        )

        assert len(result["matches"]) == 1

    def test_evaluator_receives_none_match_byte_offset_in_filename_mode(
        self, search_engine, tmp_path
    ):
        """In filename mode, match_byte_offset is None in the evaluator globals."""
        (tmp_path / "foo_module.py").write_text("x = 1\n")

        # Evaluator verifies match_byte_offset is None (filename mode)
        evaluator = "return match_byte_offset is None\n"

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=evaluator,
            search_target="filename",
            timeout_seconds=30,
        )

        assert len(result["matches"]) == 1

    def test_evaluator_receives_correct_match_line_number(
        self, search_engine, tmp_path
    ):
        """Evaluator can use match_line_number to filter by line number."""
        # foo() appears on lines 1 and 3; evaluator accepts only line 3
        (tmp_path / "f.py").write_text("foo()\nbar()\nfoo()\n")

        evaluator = "return match_line_number == 3\n"

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=evaluator,
            search_target="content",
            timeout_seconds=30,
        )

        assert len(result["matches"]) == 1
        assert result["matches"][0]["line_number"] == 3
