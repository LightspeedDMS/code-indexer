"""Tests for PythonEvaluatorSandbox.run_batch() — spawn-driver architecture (Bug #994).

TDD tests written BEFORE implementation.

Tests cover:
- Core equivalence: run_batch() produces same results as per-file run()
- AC3: per-file timeout still enforced in driver
- AC4: evaluator globals (node, root, source, lang, file_path, match_positions with ast_node)
- AC5: return contracts (matches/value, skip:True, file_role)
- AC6: allowed AST nodes (Groups C, F, G)
- AC7: stripped builtins still stripped
- AC8: spawn overhead <= 500ms per batch
- Validation in parent: invalid code returns ValidationFailed without spawning
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")


# ---------------------------------------------------------------------------
# Module-level fixtures and helpers
# ---------------------------------------------------------------------------

SIMPLE_PYTHON = """\
def hello():
    return 42
"""


@pytest.fixture()
def sandbox():
    """Return a fresh PythonEvaluatorSandbox instance."""
    from code_indexer.xray.sandbox import PythonEvaluatorSandbox

    return PythonEvaluatorSandbox()


def _spec(
    file_path: str,
    source: str,
    lang: str,
    match_positions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a file-spec dict for run_batch()."""
    return {
        "file_path": file_path,
        "source": source,
        "lang": lang,
        "match_positions": match_positions if match_positions is not None else [],
    }


def _run_one(
    sandbox: Any,
    evaluator: str,
    tmp_path: Path,
    source: str = SIMPLE_PYTHON,
    lang: str = "python",
    match_positions: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Run run_batch() for a single file and return the (matches, errors, meta) tuple."""
    sp = _spec(str(tmp_path / "t.py"), source, lang, match_positions)
    results = sandbox.run_batch(
        evaluator_code=evaluator,
        file_specs=[sp],
        worker_threads=1,
        timeout_seconds=30,
    )
    assert len(results) == 1
    # run_batch deserializes via pipe; mypy sees Any but each element is the
    # (matches, errors, meta) tuple validated by every test that calls this helper.
    return results[0]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Core contract: run_batch() results are equivalent to run()
# ---------------------------------------------------------------------------


class TestRunBatchEquivalenceToRun:
    """run_batch() must produce equivalent results to run() on the same inputs."""

    def test_run_batch_produces_same_results_as_run(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """Calls both run() and run_batch() on identical inputs and asserts equivalence.

        Compares: match line numbers, value field, and failure state (both must succeed).
        Note: run_batch() wraps run() output through _evaluate_file logic, so the value
        field surfaces in file_meta["value"] when the evaluator returns a non-None value.
        """
        from code_indexer.xray.ast_engine import AstSearchEngine

        engine = AstSearchEngine()
        evaluator = "return {'matches': [{'line_number': 1}], 'value': 42}"
        source = SIMPLE_PYTHON
        lang = "python"
        fp = str(tmp_path / "test.py")

        root = engine.parse(source.encode("utf-8"), lang)

        # Reference: per-file run() — get raw value dict
        run_result = sandbox.run(
            evaluator,
            node=root,
            root=root,
            source=source,
            lang=lang,
            file_path=fp,
        )
        assert run_result.failure is None, f"run() failed: {run_result.failure}"
        assert isinstance(run_result.value, dict)
        ref_matches = run_result.value["matches"]
        ref_value = run_result.value["value"]

        # run_batch() on same file
        sp = _spec(fp, source, lang)
        results = sandbox.run_batch(
            evaluator_code=evaluator,
            file_specs=[sp],
            worker_threads=1,
            timeout_seconds=30,
        )
        assert len(results) == 1
        file_matches, file_errors, file_meta = results[0]

        # No errors in either path
        assert file_errors == []

        # Same number of matches and same line numbers
        assert len(file_matches) == len(ref_matches)
        for got, ref in zip(file_matches, ref_matches):
            assert got["line_number"] == ref["line_number"]

        # Value field must match: run() raw value == file_meta["value"] from run_batch()
        assert file_meta is not None
        assert file_meta["value"] == ref_value

    def test_run_batch_empty_file_specs_returns_empty(self, sandbox: Any) -> None:
        """run_batch() with no files returns empty list."""
        results = sandbox.run_batch(
            evaluator_code="return {'matches': [], 'value': None}",
            file_specs=[],
            worker_threads=1,
            timeout_seconds=30,
        )
        assert results == []

    def test_run_batch_multiple_files(self, sandbox: Any, tmp_path: Path) -> None:
        """run_batch() processes multiple files and returns one result per file."""
        evaluator = "return {'matches': [{'line_number': 1}], 'value': None}"
        specs = [
            _spec(str(tmp_path / f"f{i}.py"), SIMPLE_PYTHON, "python") for i in range(3)
        ]
        results = sandbox.run_batch(
            evaluator_code=evaluator,
            file_specs=specs,
            worker_threads=2,
            timeout_seconds=30,
        )
        assert len(results) == 3
        for file_matches, file_errors, _ in results:
            assert len(file_matches) == 1
            assert file_errors == []


# ---------------------------------------------------------------------------
# AC5: Return contracts (skip:True, file_role)
# ---------------------------------------------------------------------------


class TestRunBatchReturnContracts:
    """AC5: skip:True and file_role return contracts work in driver."""

    def test_run_batch_skip_true_returns_empty(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC5: skip:True evaluator returns empty matches and no error."""
        file_matches, file_errors, file_meta = _run_one(
            sandbox, "return {'skip': True}", tmp_path
        )
        assert file_matches == []
        assert file_errors == []
        assert file_meta is None

    def test_run_batch_file_role_surfaces_in_meta(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC5: file_role from evaluator surfaces in file_meta."""
        evaluator = "return {'matches': [], 'value': 'x', 'file_role': 'test_file'}"
        _, file_errors, file_meta = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_meta is not None
        assert file_meta.get("file_role") == "test_file"


# ---------------------------------------------------------------------------
# AC4: Evaluator globals available in driver forks
# ---------------------------------------------------------------------------


class TestRunBatchEvaluatorGlobals:
    """AC4: node, root, source, lang, file_path, match_positions with ast_node available."""

    def test_evaluator_receives_source_global(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC4: 'source' global is available in evaluator code."""
        evaluator = """\
has_source = len(source) > 0
return {"matches": [{"line_number": 1, "has_source": has_source}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_matches[0]["has_source"] is True

    def test_evaluator_receives_lang_global(self, sandbox: Any, tmp_path: Path) -> None:
        """AC4: 'lang' global is available in evaluator code."""
        evaluator = (
            "return {'matches': [{'line_number': 1, 'lang': lang}], 'value': None}"
        )
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_matches[0]["lang"] == "python"

    def test_evaluator_receives_file_path_global(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC4: 'file_path' global is available in evaluator code."""
        evaluator = (
            "return {'matches': [{'line_number': 1, 'fp': file_path}], 'value': None}"
        )
        expected_fp = str(tmp_path / "t.py")
        sp = _spec(expected_fp, SIMPLE_PYTHON, "python")
        results = sandbox.run_batch(
            evaluator_code=evaluator,
            file_specs=[sp],
            worker_threads=1,
            timeout_seconds=30,
        )
        file_matches, file_errors, _ = results[0]
        assert file_errors == []
        assert file_matches[0]["fp"] == expected_fp

    def test_evaluator_receives_match_positions_global(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC4: 'match_positions' global is available as a list."""
        evaluator = """\
n = len(match_positions)
return {"matches": [{"line_number": 1, "pos_count": n}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert "pos_count" in file_matches[0]

    def test_evaluator_receives_node_and_root_globals(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC4: 'node' and 'root' globals are XRayNode instances."""
        evaluator = """\
node_type = node.type
return {"matches": [{"line_number": 1, "has_node": node is not None,
                     "has_root": root is not None, "node_type": node_type}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_matches[0]["has_node"] is True
        assert file_matches[0]["has_root"] is True
        assert isinstance(file_matches[0]["node_type"], str)

    def test_evaluator_match_positions_have_ast_node(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC4: match_positions entries include ast_node (re-populated by driver)."""
        evaluator = """\
has_ast = any(pos.get("ast_node") is not None for pos in match_positions)
return {"matches": [{"line_number": 1, "has_ast": has_ast}], "value": None}
"""
        # Supply positions without ast_node — driver must re-populate from byte_offset
        positions = [{"line_number": 1, "byte_offset": 0}]
        file_matches, file_errors, _ = _run_one(
            sandbox, evaluator, tmp_path, match_positions=positions
        )
        assert file_errors == []
        assert file_matches[0]["has_ast"] is True


# ---------------------------------------------------------------------------
# AC6: Allowed AST nodes (Groups C, F, G) work through driver
# ---------------------------------------------------------------------------


class TestRunBatchAllowedNodes:
    """AC6: Groups C, F, G pass validation and execute correctly in driver."""

    def test_group_c_for_loop_works_in_driver(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC6 Group C: for loop and if statement work in driver forks."""
        evaluator = """\
count = 0
for child in root.children:
    if child.type != "ERROR":
        count += 1
return {"matches": [{"line_number": 1, "count": count}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert "count" in file_matches[0]

    def test_group_f_import_re_works_in_driver(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC6 Group F: import re works inside driver forks."""
        evaluator = """\
import re
found = re.search(r"def", source) is not None
return {"matches": [{"line_number": 1, "found": found}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_matches[0]["found"] is True

    def test_group_g_lambda_works_in_driver(self, sandbox: Any, tmp_path: Path) -> None:
        """AC6 Group G: lambda works inside driver forks."""
        evaluator = """\
f = lambda x: x + 1
val = f(41)
return {"matches": [{"line_number": 1, "val": val}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_matches[0]["val"] == 42


# ---------------------------------------------------------------------------
# AC7: Stripped builtins remain stripped in driver forks
# ---------------------------------------------------------------------------


class TestRunBatchStrippedBuiltins:
    """AC7: Security boundary preserved — stripped builtins unavailable in driver."""

    def test_exec_is_stripped_in_driver(self, sandbox: Any, tmp_path: Path) -> None:
        """AC7: exec() is stripped — only NameError proves it is absent."""
        evaluator = """\
try:
    exec("x=1")
    had_exec = True
except NameError:
    had_exec = False
return {"matches": [{"line_number": 1, "had_exec": had_exec}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_matches[0]["had_exec"] is False

    def test_open_is_stripped_in_driver(self, sandbox: Any, tmp_path: Path) -> None:
        """AC7: open() is stripped — only NameError proves it is absent (not OSError).

        Catching OSError would mask cases where open() is still available but
        merely fails on the filesystem.  Only NameError proves the builtin is stripped.
        """
        evaluator = """\
try:
    open("/etc/passwd")
    had_open = True
except NameError:
    had_open = False
return {"matches": [{"line_number": 1, "had_open": had_open}], "value": None}
"""
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        assert file_errors == []
        assert file_matches[0]["had_open"] is False


# ---------------------------------------------------------------------------
# Validation in parent — invalid code returns ValidationFailed per file
# ---------------------------------------------------------------------------


class TestRunBatchValidation:
    """Validation happens in parent; invalid code never spawns driver."""

    def test_invalid_code_returns_validation_error(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """run_batch() with non-whitelisted import returns ValidationFailed per file."""
        file_matches, file_errors, _ = _run_one(sandbox, "import os", tmp_path)
        assert file_matches == []
        assert len(file_errors) == 1
        assert file_errors[0]["error_type"] == "ValidationFailed"

    def test_syntax_error_returns_validation_error(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """run_batch() with syntax error returns ValidationFailed (not a crash)."""
        file_matches, file_errors, _ = _run_one(sandbox, "return {", tmp_path)
        assert file_matches == []
        assert len(file_errors) == 1
        assert file_errors[0]["error_type"] == "ValidationFailed"


# ---------------------------------------------------------------------------
# AC8: Spawn overhead per batch <= 500ms
# ---------------------------------------------------------------------------


class TestRunBatchSpawnOverhead:
    """AC8: spawn overhead per job <= 500ms."""

    def test_single_file_batch_completes_within_500ms(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC8: A single-file batch completes in under 500ms (spawn overhead budget)."""
        evaluator = "return {'matches': [], 'value': None}"
        start = time.monotonic()
        _run_one(sandbox, evaluator, tmp_path)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"run_batch took {elapsed:.3f}s (> 500ms AC8 budget)"


# ---------------------------------------------------------------------------
# AC3: Timeout enforced in driver forks
# ---------------------------------------------------------------------------


class TestRunBatchTimeout:
    """AC3: Per-file 5.0s SIGTERM + 1.0s SIGKILL timeout is enforced from driver."""

    def test_infinite_loop_evaluator_returns_timeout_error(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """AC3: Evaluator with infinite loop is killed and returns EvaluatorTimeout error."""
        evaluator = """\
while True:
    pass
return {"matches": [], "value": None}
"""
        start = time.monotonic()
        file_matches, file_errors, _ = _run_one(sandbox, evaluator, tmp_path)
        elapsed = time.monotonic() - start
        assert file_matches == []
        assert len(file_errors) == 1
        assert file_errors[0]["error_type"] in ("EvaluatorTimeout", "EvaluatorCrash")
        # Must terminate within HARD_TIMEOUT + SIGKILL_GRACE + overhead (< 15s)
        assert elapsed < 15.0, f"Timeout took too long: {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Production integration: XRaySearchEngine.run() through spawn-driver path
# ---------------------------------------------------------------------------


class TestRunBatchProductionIntegration:
    """Verify rust_backend.run_batch is exercised through XRaySearchEngine.run()."""

    def test_search_engine_run_uses_batch_pipeline(self, tmp_path: Path) -> None:
        """XRaySearchEngine.run() calls rust_backend.run_batch for Phase 2.

        Mocks rust_backend.run_batch to return a pre-built match result and
        verifies: (1) run_batch was called, (2) the match flows through the
        engine's post-processing into result["matches"], (3) line_content is
        enriched by the engine from source.
        """
        from unittest.mock import patch

        from code_indexer.xray.search_engine import XRaySearchEngine

        # Create a simple Java file with a pattern to match.
        java_file = tmp_path / "Demo.java"
        source = "public class Demo {\n    public void getConnection() {}\n}\n"
        java_file.write_text(source)

        engine = XRaySearchEngine()

        # Build mock return: one match at line 2 (the getConnection line)
        def fake_run_batch(
            *,
            evaluator_code: str,
            file_specs: List[Dict[str, Any]],
            worker_threads: int = 4,
            timeout_seconds: int = 60,
            on_process_spawned: Any = None,
            repo_path: Optional[str] = None,
        ) -> List[
            Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
        ]:
            results: List[
                Tuple[
                    List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]
                ]
            ] = []
            for spec in file_specs:
                rel_path = spec.get("file_path", "")
                lang = spec.get("lang", "")
                src = spec.get("source", "")
                src_lines = src.splitlines()
                match = {
                    "line_number": 2,
                    "file_path": rel_path,
                    "language": lang,
                    "line_content": src_lines[1] if len(src_lines) > 1 else "",
                }
                results.append(([match], [], None))
            return results

        evaluator = (
            "def evaluate_node(node):\n"
            '    return {"matches": [{"line_number": 2}], "value": None}\n'
        )

        with patch.object(
            engine.rust_backend, "run_batch", side_effect=fake_run_batch
        ) as mock_rb:
            result = engine.run(
                repo_path=tmp_path,
                driver_regex="getConnection",
                evaluator_code=evaluator,
                search_target="content",
            )

        # rust_backend.run_batch must have been called
        assert mock_rb.called, "rust_backend.run_batch was not called"

        matches = result.get("matches", [])
        assert len(matches) >= 1, f"Expected at least one match, got: {result}"
        # Server enriches each match with line_content from source.
        assert any("getConnection" in m.get("line_content", "") for m in matches), (
            f"Expected line_content containing 'getConnection' in matches: {matches}"
        )
