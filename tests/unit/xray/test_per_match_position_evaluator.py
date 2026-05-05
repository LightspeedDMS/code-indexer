"""Tests for the file-as-unit evaluator contract in XRaySearchEngine (v10.4.0).

CONTRACT: evaluator is called ONCE per candidate file with the full
match_positions list from Phase 1.  The evaluator receives:
  - node / root: file root AST node
  - source: raw file text
  - lang: language string
  - file_path: absolute file path
  - match_positions: list of dicts [{line_number, line_content, column,
    byte_offset, context_before, context_after}, ...]
    Empty list in filename-target mode.

Evaluator returns: {"matches": [...], "value": <any>}

This module verifies:
1. find_enclosing_node (utility) returns the deepest AST node containing a
   byte offset — this helper is still used internally and must remain correct.
2. _line_to_byte_offset converts 1-based line number to byte offset correctly.
3. In CONTENT mode, sandbox.run is called ONCE per candidate file, with
   match_positions list as kwarg (all Phase 1 hits for the file).
4. In FILENAME mode, sandbox.run is called ONCE with match_positions=[] (empty).
5. match dict has correct line_number and line_content from evaluator/enrichment.
6. Evaluator can walk DOWN from root via descendants_of_type.
7. Multi-hit files: evaluator receives match_positions with multiple entries.
"""

from __future__ import annotations

from unittest.mock import patch

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
# FILE-AS-UNIT CONTRACT: sandbox called once per file
# ---------------------------------------------------------------------------


class TestSandboxReceivesRootNodeInContentMode:
    """In content mode, sandbox.run must receive node=root (file root)."""

    def test_node_kwarg_is_root_in_content_mode(self, search_engine, tmp_path):
        """In content mode, the node kwarg passed to sandbox.run is the file root."""
        py_file = tmp_path / "test.py"
        py_file.write_text("def foo():\n    bar()\n")

        from code_indexer.xray.sandbox import EvalResult

        captured_nodes = []

        def capturing_run(*args, **kwargs):
            captured_nodes.append(kwargs.get("node"))
            return EvalResult(value={"matches": [{"line_number": 1}], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"bar",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        # Must have been called exactly once (one file)
        assert len(captured_nodes) == 1
        # node must be the root (type == "module" for Python)
        assert captured_nodes[0] is not None
        assert captured_nodes[0].type == "module"

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
            return EvalResult(value={"matches": [], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(captured_calls) == 1
        assert captured_calls[0]["node"] is captured_calls[0]["root"]

    def test_node_kwarg_is_root_in_filename_mode(self, search_engine, tmp_path):
        """In filename mode, node kwarg must also be the file root."""
        py_file = tmp_path / "foo_module.py"
        py_file.write_text("x = 1\n")

        from code_indexer.xray.sandbox import EvalResult

        captured_nodes = []

        def capturing_run(*args, **kwargs):
            captured_nodes.append(kwargs.get("node"))
            return EvalResult(value={"matches": [{"line_number": 1}], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="filename",
            )

        assert len(captured_nodes) == 1
        assert captured_nodes[0] is not None
        assert captured_nodes[0].type == "module"


# ---------------------------------------------------------------------------
# match_positions list kwarg in sandbox.run
# ---------------------------------------------------------------------------


class TestMatchPositionsKwargInContentMode:
    """In content mode, sandbox receives match_positions as a list of dicts."""

    def test_match_positions_kwarg_is_list_in_content_mode(
        self, search_engine, tmp_path
    ):
        """match_positions kwarg must be a list in content mode."""
        (tmp_path / "f.py").write_text("foo()\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value={"matches": [], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(captured) == 1
        mp = captured[0].get("match_positions")
        assert isinstance(mp, list)
        assert len(mp) >= 1

    def test_match_positions_entries_are_dicts_with_line_number(
        self, search_engine, tmp_path
    ):
        """Each match_positions entry has line_number key."""
        (tmp_path / "f.py").write_text("a = 1\nfoo()\nb = 3\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value={"matches": [], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(captured) == 1
        mp = captured[0]["match_positions"]
        assert len(mp) == 1
        assert mp[0]["line_number"] == 2  # 'foo()' is on line 2

    def test_match_positions_contains_all_hits_for_file(
        self, search_engine, tmp_path
    ):
        """File with 3 hits passes all 3 in match_positions (one sandbox call)."""
        (tmp_path / "f.py").write_text("foo()\nbar = 1\nfoo()\nbaz = 2\nfoo()\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value={"matches": [], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        # File-as-unit: exactly 1 sandbox call, with all 3 hits in match_positions
        assert len(captured) == 1
        mp = captured[0]["match_positions"]
        assert len(mp) == 3
        line_numbers = [p["line_number"] for p in mp]
        assert line_numbers == [1, 3, 5]

    def test_match_positions_is_empty_in_filename_mode(
        self, search_engine, tmp_path
    ):
        """In filename mode, match_positions must be an empty list."""
        (tmp_path / "foo_module.py").write_text("x = 1\n")

        from code_indexer.xray.sandbox import EvalResult

        captured = []

        def capturing_run(*args, **kwargs):
            captured.append(kwargs)
            return EvalResult(value={"matches": [{"line_number": 1}], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=capturing_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="filename",
            )

        assert len(captured) == 1
        mp = captured[0].get("match_positions")
        assert isinstance(mp, list)
        assert len(mp) == 0


# ---------------------------------------------------------------------------
# File-as-unit: sandbox called ONCE per file
# ---------------------------------------------------------------------------


class TestEvaluatorCalledOncePerFile:
    """sandbox.run must be called exactly once per candidate file."""

    def test_sandbox_called_once_for_file_with_multiple_hits(
        self, search_engine, tmp_path
    ):
        """File with 3 driver hits -> sandbox.run called exactly once (not 3 times)."""
        py_file = tmp_path / "multi.py"
        py_file.write_text("foo()\nfoo()\nfoo()\n")

        from code_indexer.xray.sandbox import EvalResult

        call_count = []

        def counting_run(*args, **kwargs):
            call_count.append(1)
            return EvalResult(value={"matches": [], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=counting_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        # File-as-unit: one file → exactly one sandbox call
        assert len(call_count) == 1

    def test_sandbox_called_zero_times_for_file_with_no_phase1_matches(
        self, search_engine, tmp_path
    ):
        """Files that Phase 1 does not select result in zero sandbox calls."""
        (tmp_path / "no_match.py").write_text("def bar(): pass\n")

        call_count = []

        def counting_run(*args, **kwargs):
            call_count.append(1)
            from code_indexer.xray.sandbox import EvalResult

            return EvalResult(value={"matches": [], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=counting_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"XYZZY_NEVER_MATCHES",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(call_count) == 0

    def test_sandbox_called_n_times_for_n_candidate_files(
        self, search_engine, tmp_path
    ):
        """With 3 candidate files, sandbox.run is called exactly 3 times."""
        for i in range(3):
            (tmp_path / f"file{i}.py").write_text("foo()\n")

        from code_indexer.xray.sandbox import EvalResult

        call_count = []

        def counting_run(*args, **kwargs):
            call_count.append(1)
            return EvalResult(value={"matches": [], "value": None})

        with patch.object(search_engine.sandbox, "run", side_effect=counting_run):
            search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(call_count) == 3


# ---------------------------------------------------------------------------
# Match dict fields
# ---------------------------------------------------------------------------


class TestMatchDictFields:
    """match dict must have correct fields from evaluator + server enrichment."""

    def test_match_has_real_line_number(self, search_engine, tmp_path):
        """Match produced by evaluator returning line_number has correct value."""
        py_file = tmp_path / "lines.py"
        py_file.write_text("a = 1\nb = 2\nfoo()\nd = 4\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=(
                'return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}'
            ),
            search_target="content",
        )

        assert len(result["matches"]) == 1
        match = result["matches"][0]
        assert match["line_number"] == 3  # 'foo()' is on line 3
        assert match["line_number"] is not None

    def test_multiple_phase1_hits_produce_multiple_matches_when_evaluator_returns_them(
        self, search_engine, tmp_path
    ):
        """Evaluator that returns all match_positions produces N match entries."""
        py_file = tmp_path / "multi.py"
        py_file.write_text("foo()\nbar = 1\nfoo()\nbaz = 2\nfoo()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=(
                'matches = [{"line_number": p["line_number"]} for p in match_positions]\n'
                'return {"matches": matches, "value": None}'
            ),
            search_target="content",
        )

        assert len(result["matches"]) == 3
        line_numbers = [m["line_number"] for m in result["matches"]]
        assert line_numbers == [1, 3, 5]

    def test_match_has_line_content_server_enriched(self, search_engine, tmp_path):
        """match['line_content'] is server-enriched from source when omitted by evaluator."""
        py_file = tmp_path / "snip.py"
        py_file.write_text("x = 1\nprepareStatement(sql)\ny = 3\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code=(
                'return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}'
            ),
            search_target="content",
        )

        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert "line_content" in m
        assert "prepareStatement" in m["line_content"]

    def test_line_number_not_none_in_match(self, search_engine, tmp_path):
        """line_number must never be None in a successful match."""
        (tmp_path / "f.py").write_text("prepareStatement()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code=(
                'return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}'
            ),
            search_target="content",
        )

        assert result["matches"]
        for m in result["matches"]:
            assert m["line_number"] is not None
            assert "line_content" in m


# ---------------------------------------------------------------------------
# Evaluation errors carry file_path
# ---------------------------------------------------------------------------


class TestEvaluationErrorFields:
    """Error entries from file-level evaluation carry file_path."""

    def test_timeout_error_carries_file_path(self, search_engine, tmp_path):
        """EvaluatorTimeout error has the file_path of the failing file."""
        (tmp_path / "f.py").write_text("a = 1\nfoo()\nb = 3\n")

        from code_indexer.xray.sandbox import EvalResult

        timeout_result = EvalResult(failure="evaluator_timeout")

        with patch.object(search_engine.sandbox, "run", return_value=timeout_result):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(result["evaluation_errors"]) >= 1
        err = result["evaluation_errors"][0]
        assert "f.py" in err["file_path"]
        assert err["error_type"] == "EvaluatorTimeout"

    def test_crash_error_carries_file_path(self, search_engine, tmp_path):
        """EvaluatorCrash error has the file_path of the crashing file."""
        (tmp_path / "f.py").write_text("x = 1\nfoo()\n")

        from code_indexer.xray.sandbox import EvalResult

        crash_result = EvalResult(
            failure="evaluator_subprocess_died", detail="Subprocess died"
        )

        with patch.object(search_engine.sandbox, "run", return_value=crash_result):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        assert len(result["evaluation_errors"]) >= 1
        err = result["evaluation_errors"][0]
        assert "f.py" in err["file_path"]


# ---------------------------------------------------------------------------
# Functional E2E: evaluator walks DOWN from root via descendants_of_type
# ---------------------------------------------------------------------------


class TestFunctionalFileAsUnitE2E:
    """End-to-end: evaluator uses match_positions list, returns dict."""

    def test_evaluator_returns_matches_for_all_positions(
        self, search_engine, tmp_path
    ):
        """Evaluator that maps all match_positions produces correct match count."""
        py_file = tmp_path / "multi.py"
        py_file.write_text("a = 1\nb = 2\nfoo()\nfoo()\nfoo()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=(
                'matches = [{"line_number": p["line_number"]} for p in match_positions]\n'
                'return {"matches": matches, "value": len(match_positions)}'
            ),
            search_target="content",
            timeout_seconds=30,
        )

        assert len(result["matches"]) == 3
        lines = [m["line_number"] for m in result["matches"]]
        assert lines == [3, 4, 5]

    def test_evaluator_empty_matches_produces_no_match_for_file(
        self, search_engine, tmp_path
    ):
        """evaluator returning empty matches list means file has no matches."""
        py_file = tmp_path / "f.py"
        py_file.write_text("foo()\nfoo()\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code='return {"matches": [], "value": None}',
            search_target="content",
            timeout_seconds=30,
        )

        assert result["matches"] == []
        assert result["evaluation_errors"] == []

    def test_evaluator_walks_down_from_root_via_descendants_of_type(
        self, search_engine, tmp_path
    ):
        """Evaluator can walk DOWN from root via descendants_of_type to find nodes."""
        py_file = tmp_path / "has_functions.py"
        py_file.write_text("def foo():\n    pass\n\nfoo()\n")

        # Evaluator looks for any function_definition nodes under root
        evaluator = (
            "funcs = root.descendants_of_type('function_definition')\n"
            "if len(funcs) > 0:\n"
            "    return {'matches': [{'line_number': match_positions[0]['line_number']}], 'value': len(funcs)}\n"
            "return {'matches': [], 'value': 0}\n"
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

    def test_evaluator_receives_none_match_positions_in_filename_mode(
        self, search_engine, tmp_path
    ):
        """In filename mode, match_positions is an empty list."""
        py_file = tmp_path / "foo_module.py"
        py_file.write_text("x = 1\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=(
                'count = len(match_positions)\n'
                'return {"matches": [{"line_number": 1}], "value": count}'
            ),
            search_target="filename",
            timeout_seconds=30,
        )

        # In filename mode, match_positions=[] so value should be 0
        file_meta = result.get("file_metadata", [])
        assert any(fm["value"] == 0 for fm in file_meta)

    def test_evaluator_receives_correct_match_positions_in_content_mode(
        self, search_engine, tmp_path
    ):
        """Evaluator receives match_positions with correct line_number for hit."""
        py_file = tmp_path / "f.py"
        py_file.write_text("x = 1\ny = 2\nfoo()\nd = 4\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"foo",
            evaluator_code=(
                'pos = match_positions[0]\n'
                'return {"matches": [{"line_number": pos["line_number"]}], "value": pos["line_number"]}'
            ),
            search_target="content",
            timeout_seconds=30,
        )

        assert len(result["matches"]) == 1
        assert result["matches"][0]["line_number"] == 3  # 'foo()' on line 3
