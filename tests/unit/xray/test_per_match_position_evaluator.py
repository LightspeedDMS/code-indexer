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
