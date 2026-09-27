"""``XRaySearchEngine`` filename-target-mode candidate walk
(``_run_phase1_filename``) must never yield a candidate whose resolved
location lies outside the repository root.

Filename-target mode must yield only candidates that resolve inside the
repository root, matching content-target mode.

These tests drive the REAL ``XRaySearchEngine`` (no mocking of core
logic, per this module's existing convention) over real temporary
directories containing real symlinks (CLAUDE.md Foundation #1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")


def _engine():
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


_ALWAYS_MATCH_EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "any".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


class TestFilenameModeRootContainment:
    def test_symlink_to_outside_file_is_not_a_candidate(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "legit.py").write_text("print('legit')\n")

        outside_target = tmp_path / "outside_target.py"
        outside_target.write_text("print('outside content')\n")
        escape_link = repo / "escape_link.py"
        escape_link.symlink_to(outside_target)

        engine = _engine()
        candidates = engine._run_phase1_filename(
            repo_path=repo,
            driver_regex=r".*",
            include_patterns=[],
            exclude_patterns=[],
        )
        rel_paths = {str(p.relative_to(repo)) for p in candidates}

        assert "escape_link.py" not in rel_paths, (
            f"A symlink resolving outside the repository root must not be "
            f"a filename-mode candidate. Candidates: {rel_paths}"
        )
        assert "legit.py" in rel_paths

    def test_symlink_to_inside_file_is_still_a_candidate(self, tmp_path: Path) -> None:
        """No regression: a symlink resolving inside the repository root
        keeps today's behaviour."""
        repo = tmp_path / "repo"
        repo.mkdir()
        real_target = repo / "real.py"
        real_target.write_text("print('inside')\n")
        inside_link = repo / "inside_link.py"
        inside_link.symlink_to(real_target)

        engine = _engine()
        candidates = engine._run_phase1_filename(
            repo_path=repo,
            driver_regex=r".*",
            include_patterns=[],
            exclude_patterns=[],
        )
        rel_paths = {str(p.relative_to(repo)) for p in candidates}

        assert "inside_link.py" in rel_paths, (
            f"A symlink resolving inside the repository root must remain "
            f"a filename-mode candidate (no regression). Candidates: {rel_paths}"
        )

    def test_symlink_loop_is_skipped_without_raising(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "legit.py").write_text("print('legit')\n")
        loop_a = repo / "loop_a.py"
        loop_b = repo / "loop_b.py"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)

        engine = _engine()
        candidates = engine._run_phase1_filename(
            repo_path=repo,
            driver_regex=r".*",
            include_patterns=[],
            exclude_patterns=[],
        )
        rel_paths = {str(p.relative_to(repo)) for p in candidates}

        assert "legit.py" in rel_paths
        assert "loop_a.py" not in rel_paths
        assert "loop_b.py" not in rel_paths


class TestFilenameModeRootContainmentEndToEnd:
    """Drives the full two-phase pipeline (real xray-cli binary) with an
    evaluator that matches every AST node, so a candidate that reaches
    Phase 2 always produces a match carrying that file's own content."""

    def test_outside_symlink_content_never_appears_in_matches(
        self, tmp_path: Path
    ) -> None:
        from tests.unit.xray.conftest import require_xray_cli_binary

        require_xray_cli_binary()

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "legit.py").write_text("legit_marker_value = 1\n")

        outside_target = tmp_path / "outside_target.py"
        outside_target.write_text("outside_marker_value = 2\n")
        escape_link = repo / "escape_link.py"
        escape_link.symlink_to(outside_target)

        engine = _engine()
        result = engine.run(
            repo_path=repo,
            driver_regex=r".*",
            evaluator_code=_ALWAYS_MATCH_EVALUATOR,
            search_target="filename",
        )

        matched_files = {m["file_path"] for m in result["matches"]}
        matched_content = " ".join(m.get("line_content", "") for m in result["matches"])

        assert "escape_link.py" not in matched_files, (
            f"A symlink resolving outside the repository root produced a "
            f"match. Matches: {result['matches']}"
        )
        assert "outside_marker_value" not in matched_content, (
            f"The outside file's content must never appear in a match's "
            f"line_content. Matches: {result['matches']}"
        )
        assert "legit.py" in matched_files
        assert "legit_marker_value" in matched_content

    def test_inside_symlink_content_still_matches(self, tmp_path: Path) -> None:
        """No regression: a symlink resolving inside the repository root
        still produces a match carrying its (real, in-tree) content."""
        from tests.unit.xray.conftest import require_xray_cli_binary

        require_xray_cli_binary()

        repo = tmp_path / "repo"
        repo.mkdir()
        real_target = repo / "real.py"
        real_target.write_text("inside_marker_value = 3\n")
        inside_link = repo / "inside_link.py"
        inside_link.symlink_to(real_target)

        engine = _engine()
        result = engine.run(
            repo_path=repo,
            driver_regex=r".*",
            evaluator_code=_ALWAYS_MATCH_EVALUATOR,
            search_target="filename",
        )

        matched_files = {m["file_path"] for m in result["matches"]}
        matched_content = " ".join(m.get("line_content", "") for m in result["matches"])

        assert "inside_link.py" in matched_files, (
            f"A symlink resolving inside the repository root must still "
            f"produce a match (no regression). Matches: {result['matches']}"
        )
        assert "inside_marker_value" in matched_content
