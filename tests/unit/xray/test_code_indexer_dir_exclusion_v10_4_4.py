"""v10.4.4 tests for Finding 3.6: .code-indexer/ directory exclusion in X-Ray Phase 1.

Finding 3.6: When X-Ray searches a repository that contains a .code-indexer/
directory (CIDX's internal index store), files inside that directory were
included in filename-mode and content-mode Phase 1 results, producing false
positives in search results.

Fix:
- _run_phase1_filename: skip any file whose relative path starts with
  '.code-indexer/' (i.e. the directory separator follows the prefix).
- _run_phase1_content: inject '.code-indexer/**' into the exclude_patterns
  list before passing to RegexSearchService, so ripgrep never walks it.

Tests drive XRaySearchEngine.run() directly.  Phase 1 (filename/content scan) runs
real; rust_backend.run_batch is mocked because the Rust transpiler rejects legacy
evaluator format.  Phase 1 logic (the actual fix under test) is exercised in full.
Requires tree_sitter_languages (skips otherwise) and ripgrep for content mode.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from code_indexer.xray.search_engine import XRaySearchEngine


pytestmark = pytest.mark.importorskip("tree_sitter_languages")


def _mock_run_batch(
    evaluator_code,
    file_specs,
    worker_threads=4,
    timeout_seconds=120,
    on_process_spawned=None,
    repo_path=None,
):
    """Mock rust_backend.run_batch — return one match per file."""
    batch: list[Any] = []
    for spec in file_specs:
        fp = spec["file_path"]
        batch.append(
            (
                [
                    {
                        "file_path": fp,
                        "line_number": 1,
                        "evaluator_decision": True,
                        "language": "python",
                    }
                ],
                [],
                None,
            )
        )
    return batch


_SIMPLE_EVALUATOR = (
    'matches = [{"line_number": mp["line_number"]} for mp in match_positions]\n'
    'return {"matches": matches, "value": None}'
)

_FILENAME_EVALUATOR = 'return {"matches": [{"line_number": 1}], "value": None}'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal repo tree with a .code-indexer/ subdirectory.

    Layout:
        sample.py          — normal source file; content: "def target(): pass"
        utils.py           — normal source file; content: "def helper(): pass"
        .code-indexer/
            index.json     — CIDX internal file; content: "def target(): pass"
            meta.json      — CIDX internal file; content: "target"
    """
    (tmp_path / "sample.py").write_text("def target(): pass\n")
    (tmp_path / "utils.py").write_text("def helper(): pass\n")

    cidx_dir = tmp_path / ".code-indexer"
    cidx_dir.mkdir()
    (cidx_dir / "index.json").write_text("def target(): pass\n")
    (cidx_dir / "meta.json").write_text("target\n")

    return tmp_path


# ---------------------------------------------------------------------------
# Finding 3.6 — Filename mode
# ---------------------------------------------------------------------------


class TestCodeIndexerDirExcludedFilenameMode:
    """Files under .code-indexer/ must not appear in filename-mode results."""

    def test_code_indexer_files_excluded_from_filename_search(self, tmp_path):
        """Filename search matching all .json files must NOT return
        .code-indexer/index.json or .code-indexer/meta.json — neither as
        matches nor as evaluation_errors. Phase 1 must exclude the directory
        entirely so Phase 2 never processes those files.
        """
        repo = _make_repo(tmp_path)
        engine = XRaySearchEngine()

        with patch.object(
            engine.rust_backend, "run_batch", side_effect=_mock_run_batch
        ):
            result = engine.run(
                repo_path=repo,
                driver_regex=r"\.json$",
                evaluator_code=_FILENAME_EVALUATOR,
                search_target="filename",
            )

        matched_paths = [m["file_path"] for m in result.get("matches", [])]
        for p in matched_paths:
            assert ".code-indexer" not in p, (
                f"File from .code-indexer/ must not be a match but got: {p!r}"
            )

        # Phase 1 must exclude .code-indexer/ entirely — no UnsupportedLanguage
        # errors from files that should never have been candidates.
        error_paths = [e["file_path"] for e in result.get("evaluation_errors", [])]
        for p in error_paths:
            assert ".code-indexer" not in p, (
                f"File from .code-indexer/ must not reach Phase 2 but got error: {p!r}"
            )

    def test_non_code_indexer_files_included_in_filename_search(self, tmp_path):
        """Filename search matching .py files must still return sample.py and
        utils.py — exclusion of .code-indexer/ must not filter other files.
        """
        repo = _make_repo(tmp_path)
        engine = XRaySearchEngine()

        with patch.object(
            engine.rust_backend, "run_batch", side_effect=_mock_run_batch
        ):
            result = engine.run(
                repo_path=repo,
                driver_regex=r"\.py$",
                evaluator_code=_FILENAME_EVALUATOR,
                search_target="filename",
            )

        assert result.get("evaluation_errors") == [], (
            f"Expected no errors, got: {result.get('evaluation_errors')}"
        )
        matched_paths = [m["file_path"] for m in result.get("matches", [])]
        py_names = [Path(p).name for p in matched_paths]
        assert "sample.py" in py_names, (
            f"sample.py must be in results; got file names: {py_names}"
        )
        assert "utils.py" in py_names, (
            f"utils.py must be in results; got file names: {py_names}"
        )


# ---------------------------------------------------------------------------
# Finding 3.6 — Content mode
# ---------------------------------------------------------------------------


class TestCodeIndexerDirExcludedContentMode:
    """Files under .code-indexer/ must not appear in content-mode results."""

    def test_code_indexer_files_excluded_from_content_search(self, tmp_path):
        """Content search for 'target' must NOT return matches from
        .code-indexer/index.json even though that file contains 'target'.
        Requires real ripgrep.
        """
        if shutil.which("rg") is None:
            pytest.skip("ripgrep (rg) not available on this system")

        repo = _make_repo(tmp_path)
        engine = XRaySearchEngine()

        with patch.object(
            engine.rust_backend, "run_batch", side_effect=_mock_run_batch
        ):
            result = engine.run(
                repo_path=repo,
                driver_regex="target",
                evaluator_code=_SIMPLE_EVALUATOR,
                search_target="content",
            )

        matched_paths = [m["file_path"] for m in result.get("matches", [])]
        for p in matched_paths:
            assert ".code-indexer" not in p, (
                f"File from .code-indexer/ must be excluded but got: {p!r}"
            )

    def test_non_code_indexer_files_included_in_content_search(self, tmp_path):
        """Content search for 'target' must still return sample.py which
        contains 'def target(): pass' — exclusion must not affect other files.
        Requires real ripgrep.
        """
        if shutil.which("rg") is None:
            pytest.skip("ripgrep (rg) not available on this system")

        repo = _make_repo(tmp_path)
        engine = XRaySearchEngine()

        with patch.object(
            engine.rust_backend, "run_batch", side_effect=_mock_run_batch
        ):
            result = engine.run(
                repo_path=repo,
                driver_regex="target",
                evaluator_code=_SIMPLE_EVALUATOR,
                search_target="content",
            )

        assert result.get("evaluation_errors") == [], (
            f"Expected no errors, got: {result.get('evaluation_errors')}"
        )
        matched_paths = [m["file_path"] for m in result.get("matches", [])]
        file_names = [Path(p).name for p in matched_paths]
        assert "sample.py" in file_names, (
            f"sample.py must be in content results; got file names: {file_names}"
        )
