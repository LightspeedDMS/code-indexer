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
        """A pattern with no real match anywhere in the repo emits a warning.

        Bug #1876 item 3: a leading `*/` is now normalized to `**/` (any-depth,
        matching CLAUDE.md's documented `*/tests/*` convention), so a pattern
        like `*/time.py` DOES reach a file at src/foo/time.py -- it is no
        longer "one directory level deep only". This test now uses a filename
        that does not exist anywhere in the repo, so the pattern remains a
        genuine zero-match regardless of depth.
        """
        subdir = tmp_path / "src" / "foo"
        subdir.mkdir(parents=True)
        (subdir / "time.py").write_text("def tick(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["*/nonexistent_file.py"],
        )

        assert "warnings" in result
        assert len(result["warnings"]) == 1
        warning = result["warnings"][0]
        assert warning["type"] == "zero_match_include_pattern"
        assert warning["pattern"] == "*/nonexistent_file.py"

    def test_multiple_warnings_for_multiple_zero_match_patterns(
        self, search_engine, tmp_path
    ):
        """Each zero-match include_pattern produces its own warning entry.

        Bug #1876 item 3: patterns use filenames that do not exist anywhere
        in the repo, since a leading `*/` now matches at any depth (see
        test_warning_on_zero_match_single_segment_pattern) and would
        otherwise reach x.py/y.py below.
        """
        subdir = tmp_path / "deep" / "nested"
        subdir.mkdir(parents=True)
        (subdir / "x.py").write_text("def a(): prepareStatement()")
        (subdir / "y.py").write_text("def b(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["*/x_missing.py", "*/y_missing.py"],
        )

        assert "warnings" in result
        assert len(result["warnings"]) == 2
        warned_patterns = {w["pattern"] for w in result["warnings"]}
        assert warned_patterns == {"*/x_missing.py", "*/y_missing.py"}


class TestIncludePatternZeroMatchWarningContent:
    """Warning content and suppression behaviour for zero-match include patterns."""

    def test_mixed_healthy_and_zero_match_pattern(self, search_engine, tmp_path):
        """Healthy pattern finds files; zero-match pattern produces exactly one warning.

        Bug #1876 item 3: the zero-match pattern uses a filename that does not
        exist anywhere in the repo, since a leading `*/` now matches at any
        depth and `*/hidden.py` would otherwise reach deep/nested/hidden.py.
        """
        subdir = tmp_path / "deep" / "nested"
        subdir.mkdir(parents=True)
        (tmp_path / "top.py").write_text("def main(): prepareStatement()")
        (subdir / "hidden.py").write_text("def hidden(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["**/*.py", "*/hidden_missing.py"],
        )

        # Healthy pattern finds files
        assert result["files_total"] >= 1
        # Exactly one warning for the zero-match pattern
        assert "warnings" in result
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["pattern"] == "*/hidden_missing.py"

    def test_warning_hint_mentions_glob_difference(self, search_engine, tmp_path):
        """Warning hint explains the * vs ** difference for user guidance.

        Bug #1876 item 3: uses a nonexistent filename since a leading `*/` now
        matches at any depth and would otherwise reach a/b/time.py below.
        """
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        (subdir / "time.py").write_text("def tick(): prepareStatement()")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"prepareStatement",
            evaluator_code="return True",
            search_target="content",
            include_patterns=["*/nonexistent_time.py"],
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
