"""Tests for zero-match include_pattern warning in XRaySearchEngine.

When an include_patterns entry matches zero files in Phase 1, the engine
should surface a warning in the result envelope rather than silently
returning files_total=0.

Story: field-feedback fix #3.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def search_engine():
    """Instantiate XRaySearchEngine, skipping if tree-sitter extras not installed."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


class TestIncludePatternZeroMatchWarningSurface:
    """Warnings appear (or not) based on whether include_patterns match any file."""

    def test_no_warning_on_healthy_include_pattern(self, search_engine, tmp_path):
        """No warnings key when all include_patterns match at least one file."""
        (tmp_path / "utils.py").write_text("def foo(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["**/*.py"],
        )

        assert "warnings" not in result

    def test_warning_on_zero_match_single_segment_pattern(
        self, search_engine, tmp_path
    ):
        """A single-segment pattern that misses a deeply-nested file emits a warning.

        File is at src/foo/time.py — reachable via **/time.py but NOT */time.py
        (which only matches one directory level deep from repo root).
        """
        subdir = tmp_path / "src" / "foo"
        subdir.mkdir(parents=True)
        (subdir / "time.py").write_text("def tick(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["*/time.py"],
        )

        assert "warnings" in result
        assert len(result["warnings"]) == 1
        warning = result["warnings"][0]
        assert warning["type"] == "zero_match_include_pattern"
        assert warning["pattern"] == "*/time.py"

    def test_multiple_warnings_for_multiple_zero_match_patterns(
        self, search_engine, tmp_path
    ):
        """Each zero-match include_pattern produces its own warning entry."""
        subdir = tmp_path / "deep" / "nested"
        subdir.mkdir(parents=True)
        (subdir / "x.py").write_text("def a(): prepareStatement()")
        (subdir / "y.py").write_text("def b(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["*/x.py", "*/y.py"],
        )

        assert "warnings" in result
        assert len(result["warnings"]) == 2
        warned_patterns = {w["pattern"] for w in result["warnings"]}
        assert warned_patterns == {"*/x.py", "*/y.py"}


class TestIncludePatternZeroMatchWarningContent:
    """Warning content and suppression behaviour for zero-match include patterns."""

    def test_mixed_healthy_and_zero_match_pattern(self, search_engine, tmp_path):
        """Healthy pattern finds files; zero-match pattern produces exactly one warning."""
        subdir = tmp_path / "deep" / "nested"
        subdir.mkdir(parents=True)
        (tmp_path / "top.py").write_text("def main(): prepareStatement()")
        (subdir / "hidden.py").write_text("def hidden(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["**/*.py", "*/hidden.py"],
        )

        # Healthy pattern finds files
        assert result["files_total"] >= 1
        # Exactly one warning for the zero-match pattern
        assert "warnings" in result
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["pattern"] == "*/hidden.py"

    def test_warning_hint_mentions_glob_difference(self, search_engine, tmp_path):
        """Warning hint explains the * vs ** difference for user guidance."""
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        (subdir / "time.py").write_text("def tick(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["*/time.py"],
        )

        assert "warnings" in result
        hint = result["warnings"][0]["hint"]
        assert "**" in hint

    def test_no_warning_when_pattern_matches_files_but_regex_finds_none(
        self, search_engine, tmp_path
    ):
        """Pattern is healthy (matches files in filesystem walk) but regex finds nothing.

        The include_pattern itself is not the cause of zero results; the driver
        regex is. No zero-match warning should be emitted.
        """
        (tmp_path / "utils.py").write_text("def helper(): return 42")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"XYZZY_PATTERN_THAT_NEVER_EXISTS",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["**/*.py"],
        )

        assert result["files_total"] == 0
        assert "warnings" not in result
