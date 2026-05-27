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

NOTE: Tests that previously patched sandbox.run() to capture kwargs have been
converted to result-based tests.  With the spawn-driver architecture (Bug #994),
sandbox.run() executes inside a child process where parent-side patches are not
visible.  The converted tests instead write evaluator code that introspects the
globals it received and returns diagnostic data, then verify the returned result.
Error-field tests patch sandbox.run_batch() (called in the parent process) to
inject pre-constructed failure tuples directly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers for mocking rust_backend.run_batch
# ---------------------------------------------------------------------------


def _make_rust_batch_result(
    file_specs: List[Dict[str, Any]],
    matches_per_file: Optional[List[List[Dict[str, Any]]]] = None,
    meta_per_file: Optional[List[Optional[Dict[str, Any]]]] = None,
) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """Build a rust_backend.run_batch return value for mocking.

    Produces (enriched_matches, [], meta) tuples — one per file spec.
    Each match gets file_path, language, and line_content from the spec source.
    """
    results: List[
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
    ] = []
    for i, spec in enumerate(file_specs):
        rel_path = spec.get("file_path", "")
        source = spec.get("source", "")
        lang = spec.get("lang", "")
        source_lines = source.splitlines()

        raw_matches = (
            matches_per_file[i]
            if matches_per_file is not None and i < len(matches_per_file)
            else [{"line_number": 1}]
        )

        enriched: List[Dict[str, Any]] = []
        for m in raw_matches:
            entry = dict(m)
            entry.setdefault("file_path", rel_path)
            entry.setdefault("language", lang)
            ln = entry.get("line_number", 1)
            idx = (ln - 1) if ln and ln > 0 else 0
            if "line_content" not in entry:
                entry["line_content"] = (
                    source_lines[idx] if 0 <= idx < len(source_lines) else ""
                )
            enriched.append(entry)

        meta: Optional[Dict[str, Any]] = (
            meta_per_file[i]
            if meta_per_file is not None and i < len(meta_per_file)
            else None
        )
        results.append((enriched, [], meta))
    return results


def _rust_batch_side_effect(
    matches_per_file: Optional[List[List[Dict[str, Any]]]] = None,
    meta_per_file: Optional[List[Optional[Dict[str, Any]]]] = None,
) -> Callable[..., List[Tuple]]:
    """Return a side_effect callable for patching rust_backend.run_batch."""
    outer_matches = matches_per_file
    outer_meta = meta_per_file

    def _bound(
        *,
        evaluator_code: str,
        file_specs: List[Dict[str, Any]],
        worker_threads: int = 4,
        timeout_seconds: int = 60,
        on_process_spawned: Any = None,
        repo_path: Optional[str] = None,
    ) -> List[Tuple]:
        return _make_rust_batch_result(file_specs, outer_matches, outer_meta)

    return _bound


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
# Shared helper for error-injection tests
# ---------------------------------------------------------------------------


def _make_fake_run_batch(
    error_type: str,
    error_message: str,
) -> Callable[..., List[Tuple[List[Any], List[Any], Optional[Any]]]]:
    """Return a fake run_batch side-effect that injects a single error tuple.

    The returned callable reads file_specs[0]["file_path"] so that the
    error dict carries the real path of the candidate file.

    Compatible with rust_backend.run_batch signature (includes repo_path param).

    Args:
        error_type: e.g. "EvaluatorTimeout" or "EvaluatorCrash".
        error_message: Human-readable error description.

    Returns:
        A callable compatible with rust_backend.run_batch's signature.
    """

    def fake_run_batch(
        *,
        evaluator_code: str,
        file_specs: List[Dict[str, Any]],
        worker_threads: int = 2,
        timeout_seconds: int = 120,
        on_process_spawned: Any = None,
        repo_path: Optional[str] = None,
    ) -> List[Tuple[List[Any], List[Any], Optional[Any]]]:
        return [
            (
                [],
                [
                    {
                        "file_path": str(file_specs[0]["file_path"]),
                        "line_number": 0,
                        "error_type": error_type,
                        "error_message": error_message,
                    }
                ],
                None,
            )
        ]

    return fake_run_batch


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
    """In content mode, the evaluator receives node=root (file root).

    Tests use _evaluate_file directly (the lower-level test API that uses sandbox.run)
    because run() now uses rust_backend which does not support legacy evaluator globals.
    _evaluate_file remains the correct API for testing evaluator globals behavior.
    """

    def test_node_kwarg_is_root_in_content_mode(self, search_engine, tmp_path):
        """In content mode, the node passed to the evaluator is the file root."""
        py_file = tmp_path / "test.py"
        source = "def foo():\n    bar()\n"
        py_file.write_text(source)

        # Evaluator returns node.type so we can verify it is the module root.
        evaluator_code = 'return {"matches": [{"line_number": 1}], "value": node.type}'
        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=[
                {
                    "line_number": 2,
                    "line_content": "    bar()",
                    "column": 4,
                    "byte_offset": 15,
                }
            ],
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        # value carries node.type — must be the module root.
        assert meta is not None
        assert meta["value"] == "module"

    def test_node_kwarg_equals_root_kwarg_in_content_mode(
        self, search_engine, tmp_path
    ):
        """node and root must be the same object in content mode."""
        py_file = tmp_path / "test.py"
        source = "foo()\n"
        py_file.write_text(source)

        # Evaluator checks node is root by comparing their type and byte spans.
        evaluator_code = (
            "same = (node.type == root.type "
            "and node.start_byte == root.start_byte "
            "and node.end_byte == root.end_byte)\n"
            'return {"matches": [{"line_number": 1}], "value": same}'
        )
        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=[
                {
                    "line_number": 1,
                    "line_content": "foo()",
                    "column": 0,
                    "byte_offset": 0,
                }
            ],
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert meta is not None
        assert meta["value"] is True

    def test_node_kwarg_is_root_in_filename_mode(self, search_engine, tmp_path):
        """In filename mode, node passed to the evaluator is also the file root."""
        py_file = tmp_path / "foo_module.py"
        source = "x = 1\n"
        py_file.write_text(source)

        # Evaluator returns node.type to verify it is the module root.
        evaluator_code = 'return {"matches": [{"line_number": 1}], "value": node.type}'
        # In filename mode, match_positions is empty.
        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=[],
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert meta is not None
        assert meta["value"] == "module"


# ---------------------------------------------------------------------------
# match_positions list kwarg in sandbox.run
# ---------------------------------------------------------------------------


class TestMatchPositionsKwargInContentMode:
    """In content mode, the evaluator receives match_positions as a list of dicts.

    Tests use _evaluate_file directly (the lower-level test API that uses sandbox.run)
    because run() now uses rust_backend which does not support legacy evaluator globals.
    """

    def test_match_positions_kwarg_is_list_in_content_mode(
        self, search_engine, tmp_path
    ):
        """match_positions received by evaluator must be a list in content mode."""
        fp = tmp_path / "f.py"
        source = "foo()\n"
        fp.write_text(source)
        positions = [
            {"line_number": 1, "line_content": "foo()", "column": 0, "byte_offset": 0}
        ]

        evaluator_code = (
            "is_list = isinstance(match_positions, list)\n"
            "has_items = len(match_positions) >= 1\n"
            'return {"matches": [{"line_number": 1}], "value": is_list and has_items}'
        )
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert meta is not None
        assert meta["value"] is True

    def test_match_positions_entries_are_dicts_with_line_number(
        self, search_engine, tmp_path
    ):
        """Each match_positions entry has line_number key."""
        fp = tmp_path / "f.py"
        source = "a = 1\nfoo()\nb = 3\n"
        fp.write_text(source)
        positions = [
            {"line_number": 2, "line_content": "foo()", "column": 0, "byte_offset": 6}
        ]

        evaluator_code = (
            "entry = match_positions[0]\n"
            "is_dict = isinstance(entry, dict)\n"
            "has_ln = 'line_number' in entry\n"
            "ln_val = entry.get('line_number', -1)\n"
            'return {"matches": [{"line_number": ln_val}], "value": is_dict and has_ln}'
        )
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert len(matches) == 1
        assert matches[0]["line_number"] == 2  # 'foo()' is on line 2
        assert meta is not None
        assert meta["value"] is True

    def test_match_positions_contains_all_hits_for_file(self, search_engine, tmp_path):
        """File with 3 hits passes all 3 in match_positions (one evaluator call)."""
        fp = tmp_path / "f.py"
        source = "foo()\nbar = 1\nfoo()\nbaz = 2\nfoo()\n"
        fp.write_text(source)
        positions = [
            {"line_number": 1, "line_content": "foo()", "column": 0, "byte_offset": 0},
            {"line_number": 3, "line_content": "foo()", "column": 0, "byte_offset": 14},
            {"line_number": 5, "line_content": "foo()", "column": 0, "byte_offset": 28},
        ]

        evaluator_code = (
            "lns = [p['line_number'] for p in match_positions]\n"
            'return {"matches": [{"line_number": 1}], "value": lns}'
        )
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert meta is not None
        # File-as-unit: evaluator called once; value carries all 3 line numbers.
        assert meta["value"] == [1, 3, 5]

    def test_match_positions_is_empty_in_filename_mode(self, search_engine, tmp_path):
        """In filename mode, match_positions is empty — verified via _evaluate_file."""
        fp = tmp_path / "foo_module.py"
        source = "x = 1\n"
        fp.write_text(source)

        evaluator_code = (
            "count = len(match_positions)\n"
            'return {"matches": [{"line_number": 1}], "value": count}'
        )
        # In filename mode, match_positions is [] — pass it explicitly.
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=[],
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert meta is not None
        assert meta["value"] == 0


# ---------------------------------------------------------------------------
# File-as-unit: evaluator called ONCE per file
# ---------------------------------------------------------------------------


class TestEvaluatorCalledOncePerFile:
    """The evaluator must be called exactly once per candidate file.

    Tests that use evaluator globals are rewritten to use _evaluate_file (sandbox path).
    Tests that verify pipeline call-count via file_specs use rust_backend mock.
    """

    def test_sandbox_called_once_for_file_with_multiple_hits(
        self, search_engine, tmp_path
    ):
        """File with 3 driver hits -> run_batch receives one file_spec with 3 match_positions.

        Evidence: run_batch is called once; file_specs has 1 entry with 3 match_positions,
        proving all Phase 1 hits are bundled into one backend call (file-as-unit).
        """
        py_file = tmp_path / "multi.py"
        py_file.write_text("foo()\nfoo()\nfoo()\n")
        captured: list = []

        def _capture(
            *,
            evaluator_code: str,
            file_specs: list,
            worker_threads: int = 4,
            timeout_seconds: int = 60,
            on_process_spawned=None,
            repo_path=None,
        ):
            captured.extend(file_specs)
            return _make_rust_batch_result(
                file_specs,
                [[{"line_number": 1}]],
                [
                    {
                        "file_path": file_specs[0]["file_path"],
                        "value": len(file_specs[0]["match_positions"]),
                    }
                ],
            )

        with patch.object(
            search_engine.rust_backend, "run_batch", side_effect=_capture
        ):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        # run_batch receives exactly 1 file_spec with all 3 hits.
        assert len(captured) == 1
        assert len(captured[0]["match_positions"]) == 3
        # file_metadata entry carries the hit count (3).
        file_meta = result.get("file_metadata", [])
        assert len(file_meta) == 1
        assert file_meta[0]["value"] == 3

    def test_sandbox_called_zero_times_for_file_with_no_phase1_matches(
        self, search_engine, tmp_path
    ):
        """Files that Phase 1 does not select result in zero evaluator calls.

        Evidence: when Phase 1 finds nothing, the result has no matches, no
        evaluation_errors, and no file_metadata entries — the evaluator never ran.
        """
        (tmp_path / "no_match.py").write_text("def bar(): pass\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"XYZZY_NEVER_MATCHES",
            evaluator_code='return {"matches": [], "value": None}',
            search_target="content",
        )

        # No Phase 1 candidates -> evaluator never called -> no results of any kind.
        assert result["matches"] == []
        assert result["evaluation_errors"] == []
        assert result.get("file_metadata", []) == []

    def test_sandbox_called_n_times_for_n_candidate_files(
        self, search_engine, tmp_path
    ):
        """With 3 candidate files, run_batch receives 3 file_specs (one per file).

        Evidence: file_specs captured at run_batch has exactly 3 entries.
        """
        for i in range(3):
            (tmp_path / f"file{i}.py").write_text("foo()\n")

        captured: list = []

        def _capture(
            *,
            evaluator_code: str,
            file_specs: list,
            worker_threads: int = 4,
            timeout_seconds: int = 60,
            on_process_spawned=None,
            repo_path=None,
        ):
            captured.extend(file_specs)
            return _make_rust_batch_result(
                file_specs,
                None,
                [
                    {"file_path": s["file_path"], "value": s["file_path"]}
                    for s in file_specs
                ],
            )

        with patch.object(
            search_engine.rust_backend, "run_batch", side_effect=_capture
        ):
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"foo",
                evaluator_code='return {"matches": [], "value": None}',
                search_target="content",
            )

        # 3 files -> 3 file_specs in run_batch -> 3 file_metadata entries.
        assert len(captured) == 3
        file_meta = result.get("file_metadata", [])
        assert len(file_meta) == 3


# ---------------------------------------------------------------------------
# Match dict fields
# ---------------------------------------------------------------------------


class TestMatchDictFields:
    """match dict must have correct fields from evaluator + backend enrichment.

    Tests use _evaluate_file directly (sandbox path) since they verify evaluator
    globals (match_positions) — a sandbox concept not supported by rust_backend.
    """

    def test_match_has_real_line_number(self, search_engine, tmp_path):
        """Match produced by evaluator returning line_number has correct value."""
        fp = tmp_path / "lines.py"
        source = "a = 1\nb = 2\nfoo()\nd = 4\n"
        fp.write_text(source)
        positions = [
            {"line_number": 3, "line_content": "foo()", "column": 0, "byte_offset": 12}
        ]

        evaluator_code = 'return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}'
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert len(matches) == 1
        assert matches[0]["line_number"] == 3  # 'foo()' is on line 3
        assert matches[0]["line_number"] is not None

    def test_multiple_phase1_hits_produce_multiple_matches_when_evaluator_returns_them(
        self, search_engine, tmp_path
    ):
        """Evaluator that returns all match_positions produces N match entries."""
        fp = tmp_path / "multi.py"
        source = "foo()\nbar = 1\nfoo()\nbaz = 2\nfoo()\n"
        fp.write_text(source)
        positions = [
            {"line_number": 1, "line_content": "foo()", "column": 0, "byte_offset": 0},
            {"line_number": 3, "line_content": "foo()", "column": 0, "byte_offset": 14},
            {"line_number": 5, "line_content": "foo()", "column": 0, "byte_offset": 28},
        ]

        evaluator_code = (
            'matches = [{"line_number": p["line_number"]} for p in match_positions]\n'
            'return {"matches": matches, "value": None}'
        )
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert len(matches) == 3
        line_numbers = [m["line_number"] for m in matches]
        assert line_numbers == [1, 3, 5]

    def test_match_has_line_content_server_enriched(self, search_engine, tmp_path):
        """match['line_content'] is enriched from source when omitted by evaluator."""
        fp = tmp_path / "snip.py"
        source = "x = 1\nprepareStatement(sql)\ny = 3\n"
        fp.write_text(source)
        positions = [
            {
                "line_number": 2,
                "line_content": "prepareStatement(sql)",
                "column": 0,
                "byte_offset": 6,
            }
        ]

        evaluator_code = 'return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}'
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert len(matches) == 1
        m = matches[0]
        assert "line_content" in m
        assert "prepareStatement" in m["line_content"]

    def test_line_number_not_none_in_match(self, search_engine, tmp_path):
        """line_number must never be None in a successful match."""
        fp = tmp_path / "f.py"
        source = "prepareStatement()\n"
        fp.write_text(source)
        positions = [
            {
                "line_number": 1,
                "line_content": "prepareStatement()",
                "column": 0,
                "byte_offset": 0,
            }
        ]

        evaluator_code = 'return {"matches": [{"line_number": match_positions[0]["line_number"]}], "value": None}'
        matches, errors, meta = search_engine._evaluate_file(
            fp,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )
        assert errors == []
        assert matches
        for m in matches:
            assert m["line_number"] is not None
            assert "line_content" in m


# ---------------------------------------------------------------------------
# Evaluation errors carry file_path
# ---------------------------------------------------------------------------


class TestEvaluationErrorFields:
    """Error entries from file-level evaluation carry file_path.

    Converted from sandbox.run-patching to sandbox.run_batch-patching (Bug #994):
    run_batch() is called in the parent process, so patches on it ARE visible.
    The shared _make_fake_run_batch helper builds the side-effect callable to
    avoid duplicating the injection logic across tests.
    """

    def test_timeout_error_carries_file_path(self, search_engine, tmp_path):
        """EvaluatorTimeout error has the file_path of the failing file."""
        (tmp_path / "f.py").write_text("a = 1\nfoo()\nb = 3\n")

        with patch.object(
            search_engine.rust_backend,
            "run_batch",
            side_effect=_make_fake_run_batch(
                "EvaluatorTimeout", "evaluator exceeded 5s sandbox limit"
            ),
        ):
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

        with patch.object(
            search_engine.rust_backend,
            "run_batch",
            side_effect=_make_fake_run_batch("EvaluatorCrash", "Subprocess died"),
        ):
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
    """End-to-end: evaluator uses match_positions list, returns dict.

    These tests verify evaluator globals behavior (match_positions, root, etc.)
    using _evaluate_file directly, since that path exercises sandbox.run with
    the real globals contract.  Legacy evaluator code without
    'def evaluate_node(node):' hits TranspileError on rust_backend — these
    tests intentionally bypass rust_backend to verify the sandbox contract.
    """

    def test_evaluator_returns_matches_for_all_positions(self, search_engine, tmp_path):
        """Evaluator that maps all match_positions produces correct match count."""
        py_file = tmp_path / "multi.py"
        source = "a = 1\nb = 2\nfoo()\nfoo()\nfoo()\n"
        py_file.write_text(source)

        positions = [
            {"line_number": 3, "line_content": "foo()", "column": 0, "byte_offset": 12},
            {"line_number": 4, "line_content": "foo()", "column": 0, "byte_offset": 18},
            {"line_number": 5, "line_content": "foo()", "column": 0, "byte_offset": 24},
        ]
        evaluator_code = (
            'matches = [{"line_number": p["line_number"]} for p in match_positions]\n'
            'return {"matches": matches, "value": len(match_positions)}'
        )

        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )

        assert errors == []
        assert len(matches) == 3
        lines = [m["line_number"] for m in matches]
        assert lines == [3, 4, 5]

    def test_evaluator_empty_matches_produces_no_match_for_file(
        self, search_engine, tmp_path
    ):
        """evaluator returning empty matches list means file has no matches."""
        py_file = tmp_path / "f.py"
        source = "foo()\nfoo()\n"
        py_file.write_text(source)

        positions = [
            {"line_number": 1, "line_content": "foo()", "column": 0, "byte_offset": 0},
            {"line_number": 2, "line_content": "foo()", "column": 0, "byte_offset": 6},
        ]

        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            'return {"matches": [], "value": None}',
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )

        assert matches == []
        assert errors == []

    def test_evaluator_walks_down_from_root_via_descendants_of_type(
        self, search_engine, tmp_path
    ):
        """Evaluator can walk DOWN from root via descendants_of_type to find nodes."""
        py_file = tmp_path / "has_functions.py"
        source = "def foo():\n    pass\n\nfoo()\n"
        py_file.write_text(source)

        positions = [
            {"line_number": 4, "line_content": "foo()", "column": 0, "byte_offset": 21},
        ]
        # Evaluator looks for any function_definition nodes under root
        evaluator = (
            "funcs = root.descendants_of_type('function_definition')\n"
            "if len(funcs) > 0:\n"
            "    return {'matches': [{'line_number': match_positions[0]['line_number']}], 'value': len(funcs)}\n"
            "return {'matches': [], 'value': 0}\n"
        )

        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            evaluator,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )

        # Should produce matches because the file has a function_definition
        assert errors == []
        assert len(matches) > 0

    def test_evaluator_receives_none_match_positions_in_filename_mode(
        self, search_engine, tmp_path
    ):
        """In filename mode, match_positions is an empty list."""
        py_file = tmp_path / "foo_module.py"
        source = "x = 1\n"
        py_file.write_text(source)

        # Filename mode passes empty match_positions=[]
        evaluator_code = (
            "count = len(match_positions)\n"
            'return {"matches": [{"line_number": 1}], "value": count}'
        )

        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=[],  # filename mode: empty list
            lang="python",
            source=source,
            context_lines=0,
        )

        assert errors == []
        # match_positions=[] so evaluator computes count=0, value should be 0
        assert meta is not None
        assert meta["value"] == 0

    def test_evaluator_receives_correct_match_positions_in_content_mode(
        self, search_engine, tmp_path
    ):
        """Evaluator receives match_positions with correct line_number for hit."""
        py_file = tmp_path / "f.py"
        source = "x = 1\ny = 2\nfoo()\nd = 4\n"
        py_file.write_text(source)

        positions = [
            {"line_number": 3, "line_content": "foo()", "column": 0, "byte_offset": 12},
        ]
        evaluator_code = (
            "pos = match_positions[0]\n"
            'return {"matches": [{"line_number": pos["line_number"]}], "value": pos["line_number"]}'
        )

        matches, errors, meta = search_engine._evaluate_file(
            py_file,
            evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=positions,
            lang="python",
            source=source,
            context_lines=0,
        )

        assert errors == []
        assert len(matches) == 1
        assert matches[0]["line_number"] == 3  # 'foo()' on line 3
