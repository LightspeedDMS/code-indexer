"""v10.4.6 tests for Defect 2: .git/ directory exclusion in X-Ray Phase 1.

Defect 2: cidx-meta-global and similar repos that have a .git/ directory had
their git internals (FETCH_HEAD, COMMIT_EDITMSG, objects/, etc.) returned as
Phase 1 candidates, because the Phase 1 filename-walk and content-mode ripgrep
had no exclusion for .git/.

Fix (v10.4.6):
- _run_phase1_filename: skip any file whose relative path starts with '.git/'
  or equals '.git' (mirrors the v10.4.4 .code-indexer/ skip).
- _run_phase1_content: inject '.git/**' into effective_excludes so ripgrep
  never walks .git/ (mirrors the v10.4.4 .code-indexer/** exclusion).

Tests drive XRaySearchEngine.run() directly.  Phase 1 (filename/content scan) runs
real; rust_backend.run_batch is mocked because the Rust transpiler rejects legacy
evaluator format.  Phase 1 logic (the actual fix under test) is exercised in full.
Requires tree_sitter_languages (skips otherwise) and ripgrep for content mode.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List
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
    batch = []
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
    """Create a minimal repo tree with a .git/ subdirectory and a legit file.

    Layout:
        src/main.py            -- normal source file; content: "def target(): pass"
        src/git_helpers.py     -- file whose name contains 'git' but is NOT inside .git/
        .git/HEAD              -- git internal; content: "ref: refs/heads/main"
        .git/FETCH_HEAD        -- git internal; content: "target"
        .git/COMMIT_EDITMSG    -- git internal; content: "target commit message"
        .git/objects/info      -- git internal dir sentinel
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("def target(): pass\n")
    (src / "git_helpers.py").write_text("def clone_repo(): pass\n")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "FETCH_HEAD").write_text("target\n")
    (git_dir / "COMMIT_EDITMSG").write_text("target commit message\n")
    objects = git_dir / "objects"
    objects.mkdir()
    (objects / "info").write_text("")

    return tmp_path


def _assert_no_dot_git_paths(paths: List[str], context: str) -> None:
    """Assert that no path in the list is inside the .git/ directory."""
    for p in paths:
        assert ".git/" not in p and not p.endswith("/.git"), (
            f"{context}: path from .git/ must not appear but got: {p!r}"
        )


# ---------------------------------------------------------------------------
# Defect 2 — Filename mode
# ---------------------------------------------------------------------------


class TestGitDirExcludedFilenameMode:
    """Files under .git/ must not appear in filename-mode Phase 1 results."""

    def test_git_files_excluded_from_filename_search(self, tmp_path):
        """Filename search matching all files must NOT return any path that
        contains '.git/' — neither as matches nor as evaluation_errors.
        Phase 1 must exclude the .git/ directory entirely so Phase 2 never
        processes FETCH_HEAD, COMMIT_EDITMSG, or objects/.

        Also asserts that legitimate source files are still returned (positive
        check guards against a vacuously-passing broken implementation).
        """
        repo = _make_repo(tmp_path)
        engine = XRaySearchEngine()

        # Match every filename (dot-star) to catch any .git/ file that leaks through.
        with patch.object(
            engine.rust_backend, "run_batch", side_effect=_mock_run_batch
        ):
            result = engine.run(
                repo_path=repo,
                driver_regex=r".*",
                evaluator_code=_FILENAME_EVALUATOR,
                search_target="filename",
            )

        matched_paths = [m["file_path"] for m in result.get("matches", [])]
        error_paths = [e["file_path"] for e in result.get("evaluation_errors", [])]

        # Negative assertion: no .git/ path in matches or errors.
        _assert_no_dot_git_paths(matched_paths, "filename matches")
        _assert_no_dot_git_paths(error_paths, "filename evaluation_errors")

        # Positive assertion: at least one expected source file is present.
        file_names = [Path(p).name for p in matched_paths]
        assert "main.py" in file_names, (
            f"main.py must appear in filename results; got: {file_names!r}"
        )


# ---------------------------------------------------------------------------
# Defect 2 — Content mode
# ---------------------------------------------------------------------------


class TestGitDirExcludedContentMode:
    """Files under .git/ must not appear in content-mode Phase 1 results."""

    def test_git_files_excluded_from_content_search(self, tmp_path):
        """Content search for 'target' must NOT return matches from .git/FETCH_HEAD
        or .git/COMMIT_EDITMSG even though those files contain the word 'target'.
        Also asserts that src/main.py (which legitimately contains 'target') IS
        returned — guards against a vacuously-passing broken implementation.
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

        # Negative assertion: no .git/ path in matches.
        _assert_no_dot_git_paths(matched_paths, "content matches")

        # Positive assertion: src/main.py must be found (it contains 'target').
        file_names = [Path(p).name for p in matched_paths]
        assert "main.py" in file_names, (
            f"main.py must appear in content results; got: {file_names!r}"
        )


# ---------------------------------------------------------------------------
# Defect 2 — False-exclusion guard
# ---------------------------------------------------------------------------


class TestGitNamedFilesOutsideDotGitIncluded:
    """Files with 'git' in their name that are NOT inside .git/ must not be excluded."""

    def test_legit_git_named_files_outside_dot_git_still_included(self, tmp_path):
        """A file at src/git_helpers.py (which has 'git' in its name but is NOT
        inside .git/) must NOT be excluded — the exclusion is path-prefix based,
        not a substring-in-name match.
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

        matched_paths = [m["file_path"] for m in result.get("matches", [])]
        file_names = [Path(p).name for p in matched_paths]
        assert "git_helpers.py" in file_names, (
            f"git_helpers.py must NOT be excluded (it is not inside .git/); "
            f"got file names: {file_names!r}"
        )
        assert "main.py" in file_names, (
            f"main.py must be in filename results; got file names: {file_names!r}"
        )
