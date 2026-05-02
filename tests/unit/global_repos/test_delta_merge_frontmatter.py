"""
Unit tests for Bug #834 — delta merge frontmatter duplication fix.

Tests the `_strip_leading_yaml_frontmatter` helper and its integration
with `invoke_delta_merge_file` to prevent quadruple `---` delimiters in
staged temp files passed to Claude CLI.

All tests in this file are unit tests. The invoke_delta_merge_file tests
mock subprocess.run (the external Claude CLI process), which is the same
pattern used throughout test_dependency_map_analyzer_delta.py for this
class of tests.
"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    _strip_leading_yaml_frontmatter,
)

# ---------------------------------------------------------------------------
# Shared test constants and helpers
# ---------------------------------------------------------------------------

_SUBPROCESS_PATH = "code_indexer.global_repos.dependency_map_analyzer.subprocess.run"
_TEST_TIMEOUT = 60
_TEST_MAX_TURNS = 5
_MTIME_TICK_S = (
    0.1  # Sleep duration to ensure mtime advances even under heavy system load
)


def _make_subprocess_result(stdout: str = "FILE_EDIT_COMPLETE") -> object:
    import subprocess as _subprocess

    return _subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _make_capture_and_edit_side_effect(
    temp_glob: str, new_content: str, tmp_path: Path, captured: list
):
    """Return a subprocess.run side_effect that:
    1. Reads and stores the current temp file contents (before edit)
    2. Sleeps briefly to advance mtime (required by _verify_file_modified)
    3. Writes new_content to the temp file
    """

    def _side_effect(*args, **kwargs):
        matched = list(tmp_path.glob(temp_glob))
        assert len(matched) == 1, (
            f"Expected 1 temp file matching {temp_glob}, got {matched}"
        )
        captured.append(matched[0].read_text())
        time.sleep(_MTIME_TICK_S)
        matched[0].write_text(new_content)
        return _make_subprocess_result()

    return _side_effect


@pytest.fixture
def analyzer(tmp_path):
    """Create DependencyMapAnalyzer instance."""
    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()
    cidx_meta_path = tmp_path / "cidx-meta"
    cidx_meta_path.mkdir()
    return DependencyMapAnalyzer(
        golden_repos_root=golden_repos_root,
        cidx_meta_path=cidx_meta_path,
        pass_timeout=600,
    )


# ---------------------------------------------------------------------------
# Tests for _strip_leading_yaml_frontmatter
# ---------------------------------------------------------------------------


class TestStripLeadingYamlFrontmatter:
    """Unit tests for the module-level helper function."""

    def test_strip_leading_yaml_frontmatter_removes_frontmatter(self):
        """Given content with frontmatter, strips to body only."""
        content = "---\ndomain: auth\nlast_analyzed: 2026-01-01\n---\n\n# body"
        result = _strip_leading_yaml_frontmatter(content)
        assert result == "# body"

    def test_strip_leading_yaml_frontmatter_preserves_content_without_frontmatter(
        self,
    ):
        """Given content without frontmatter, returns it unchanged."""
        content = "# body\n\nSome text here."
        result = _strip_leading_yaml_frontmatter(content)
        assert result == content

    def test_strip_leading_yaml_frontmatter_handles_malformed_frontmatter(self):
        """Given frontmatter with no closing ---, returns content unchanged (no loop)."""
        content = "---\nno closing delimiter here\nstill going"
        result = _strip_leading_yaml_frontmatter(content)
        assert result == content

    def test_strip_leading_yaml_frontmatter_only_strips_first_block(self):
        """Given two frontmatter blocks, strips only the first one.

        After the first block is stripped, the second block remains as body content.
        """
        content = "---\na: 1\n---\n---\nb: 2\n---\n\nbody"
        result = _strip_leading_yaml_frontmatter(content)
        assert result == "---\nb: 2\n---\n\nbody"

    def test_strip_leading_yaml_frontmatter_handles_crlf(self):
        """Given CRLF line endings, returns content unchanged.

        LF is the only supported format. CRLF content does not start with '---\\n'
        so the function returns it unchanged — this is the documented safe behaviour.
        """
        content = "---\r\ndomain: auth\r\n---\r\n\r\nbody"
        result = _strip_leading_yaml_frontmatter(content)
        assert result == content

    def test_strip_leading_yaml_frontmatter_no_blank_line_after_closing(self):
        """Given frontmatter with no blank line after closing ---, strips to body."""
        content = "---\ndomain: auth\n---\n# body"
        result = _strip_leading_yaml_frontmatter(content)
        assert result == "# body"

    def test_strip_leading_yaml_frontmatter_empty_string(self):
        """Empty string returns empty string."""
        assert _strip_leading_yaml_frontmatter("") == ""


# ---------------------------------------------------------------------------
# Unit tests: invoke_delta_merge_file writes body-only to temp file
# ---------------------------------------------------------------------------

_EXISTING_WITH_FRONTMATTER = (
    "---\n"
    "domain: cloud-infrastructure-platform\n"
    "last_analyzed: 2026-01-01T00:00:00+00:00\n"
    "---\n\n"
    "# Cloud Infrastructure\n\n"
    "Some existing analysis content."
)
_EXISTING_WITHOUT_FRONTMATTER = (
    "# Cloud Infrastructure\n\nExisting analysis without frontmatter."
)
_UPDATED_BODY = "# Cloud Infrastructure\n\nUpdated analysis content."


@pytest.mark.parametrize(
    "existing_content,expect_no_delimiters",
    [
        pytest.param(
            _EXISTING_WITH_FRONTMATTER,
            True,
            id="strips_frontmatter_before_writing_temp_file",
        ),
        pytest.param(
            _EXISTING_WITHOUT_FRONTMATTER,
            False,
            id="preserves_content_without_frontmatter",
        ),
    ],
)
def test_invoke_delta_merge_file_temp_file_body_only(
    analyzer, tmp_path, existing_content, expect_no_delimiters
):
    """Temp file written to disk must contain body-only (no --- delimiters) when
    existing_content has frontmatter. When no frontmatter is present, the temp
    file content is unchanged (regression guard).

    Bug #834: without the fix, a file with frontmatter would cause the temp file
    to also contain frontmatter, resulting in quadruple --- when Claude re-adds
    the block it was told not to add.
    """
    captured_temp_contents: list = []

    with patch(
        _SUBPROCESS_PATH,
        side_effect=_make_capture_and_edit_side_effect(
            "_delta_merge_*.md",
            _UPDATED_BODY,
            tmp_path,
            captured_temp_contents,
        ),
    ):
        result = analyzer.invoke_delta_merge_file(
            domain_name="cloud-infrastructure-platform",
            existing_content=existing_content,
            merge_prompt="merge prompt",
            timeout=_TEST_TIMEOUT,
            max_turns=_TEST_MAX_TURNS,
            temp_dir=tmp_path,
        )

    assert result == _UPDATED_BODY
    assert len(captured_temp_contents) == 1
    temp_content = captured_temp_contents[0]

    if expect_no_delimiters:
        assert "---" not in temp_content, (
            f"Temp file must NOT contain frontmatter delimiters, got:\n{temp_content}"
        )
    else:
        assert temp_content == existing_content


# ---------------------------------------------------------------------------
# Test: no-op edit with body-only baseline returns None
# ---------------------------------------------------------------------------


def test_no_op_edit_with_frontmatter_existing_returns_none(analyzer, tmp_path):
    """When the caller passes body-only existing_content and Claude returns the same
    body (no changes), invoke_delta_merge_file must return None (no-op short-circuit).

    Bug #834 regression: the comparison baseline in _read_file_if_changed must be
    body-only (not body-with-frontmatter).  If the comparison used full content
    (frontmatter + body), `updated.strip() == existing.strip()` would evaluate False
    even when nothing changed, causing spurious rewrites.
    """
    body_only = "# Cloud Infrastructure\n\nExisting analysis without frontmatter."

    def _noop_side_effect(*args, **kwargs):
        """Claude receives body-only temp file; writes back the same body (no-op)."""
        import time

        matched = list(tmp_path.glob("_delta_merge_*.md"))
        assert len(matched) == 1
        time.sleep(_MTIME_TICK_S)
        # Write back the EXACT same body — no changes
        matched[0].write_text(body_only)
        return _make_subprocess_result()

    with patch(_SUBPROCESS_PATH, side_effect=_noop_side_effect):
        result = analyzer.invoke_delta_merge_file(
            domain_name="cloud-infrastructure-platform",
            existing_content=body_only,
            merge_prompt="merge prompt",
            timeout=_TEST_TIMEOUT,
            max_turns=_TEST_MAX_TURNS,
            temp_dir=tmp_path,
        )

    assert result is None, f"Expected None for no-op edit, got: {result!r}"


# ---------------------------------------------------------------------------
# Test: Claude inadvertently returns frontmatter — must be stripped + WARNING logged
# ---------------------------------------------------------------------------


def test_invoke_delta_merge_strips_frontmatter_claude_returns_inadvertently(
    analyzer, tmp_path, caplog
):
    """When Claude writes content that starts with YAML frontmatter into the temp file
    (which it should not), invoke_delta_merge_file must:
    1. Strip the frontmatter from the returned value.
    2. Log a WARNING about the unexpected frontmatter.

    This validates Step 3 of the codex fix (defensive sanitization of Claude output).
    """
    import logging

    body_only = "# Cloud Infrastructure\n\nExisting analysis without frontmatter."
    # Simulate Claude accidentally writing frontmatter back into the temp file
    inadvertent_frontmatter_response = (
        "---\n"
        "domain: cloud-infrastructure-platform\n"
        "last_analyzed: 2026-01-01T00:00:00+00:00\n"
        "---\n\n"
        "# Cloud Infrastructure\n\nUpdated analysis with inadvertent frontmatter."
    )
    expected_stripped = (
        "# Cloud Infrastructure\n\nUpdated analysis with inadvertent frontmatter."
    )

    def _claude_returns_frontmatter_side_effect(*args, **kwargs):
        import time

        matched = list(tmp_path.glob("_delta_merge_*.md"))
        assert len(matched) == 1
        time.sleep(_MTIME_TICK_S)
        matched[0].write_text(inadvertent_frontmatter_response)
        return _make_subprocess_result()

    with caplog.at_level(
        logging.WARNING, logger="code_indexer.global_repos.dependency_map_analyzer"
    ):
        with patch(
            _SUBPROCESS_PATH, side_effect=_claude_returns_frontmatter_side_effect
        ):
            result = analyzer.invoke_delta_merge_file(
                domain_name="cloud-infrastructure-platform",
                existing_content=body_only,
                merge_prompt="merge prompt",
                timeout=_TEST_TIMEOUT,
                max_turns=_TEST_MAX_TURNS,
                temp_dir=tmp_path,
            )

    # Result must be stripped of frontmatter
    assert result is not None, "Expected non-None result for changed content"
    assert not result.startswith("---"), (
        f"Result must NOT start with frontmatter delimiters, got:\n{result!r}"
    )
    assert result.strip() == expected_stripped.strip(), (
        f"Result body mismatch.\nExpected: {expected_stripped!r}\nGot: {result!r}"
    )
    # A WARNING must have been logged about the stripped frontmatter
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("frontmatter" in r.message.lower() for r in warning_records), (
        f"Expected a WARNING about inadvertent frontmatter, got records: {[r.message for r in warning_records]}"
    )
