"""
Tests for Phase B (create path) and Phase C (atomic write helper).

Covers:
1.  Output is a valid YAML frontmatter document: parseable dict + non-empty body (line-aware split)
2.  atomic_write_description creates target file with correct content
3.  atomic_write_description replaces existing file atomically
4.  No .tmp files remain after a successful write (no partial state)
5.  atomic_write_description acquires and releases the cidx-meta write lock
6.  Lock is released even when the write itself fails

Note: Phase 2 lifecycle detection was removed from _generate_repo_description
as part of Story #876 — lifecycle detection is now handled by LifecycleBatchRunner.
MCP registration tests for _generate_repo_description are also removed because
mcp_registration_service was only needed before Phase 2.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
import pytest

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """Minimal fake repo directory with a README.md."""
    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("# Fake Repo\n\nA repo for unit testing.\n")
    return repo_dir


# ---------------------------------------------------------------------------
# Frontmatter / body split — line-aware so '---' inside YAML values is safe
# ---------------------------------------------------------------------------


def _split_frontmatter_lines(content_str):
    """
    Split a ---\\n...\\n---\\n<body> document into (fm_dict, body) using
    line-by-line parsing so that '---' appearing inside a YAML value does
    not confuse the fence detection.

    Returns:
        (fm_dict, body) where fm_dict is the parsed YAML mapping and body
        is the text after the closing fence line.

    Raises:
        AssertionError if the document does not start with a standalone '---'
        line or has no closing '---' fence.
    """
    lines = content_str.splitlines(keepends=True)
    assert lines and lines[0].rstrip("\n") == "---", (
        "Document must open with a standalone --- line"
    )

    fm_lines = []
    closing_index = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\n") == "---":
            closing_index = i
            break
        fm_lines.append(line)

    assert closing_index is not None, "No closing standalone --- line found"

    fm_text = "".join(fm_lines)
    body = "".join(lines[closing_index + 1 :])
    fm_dict = yaml.safe_load(fm_text)
    return fm_dict, body


# ---------------------------------------------------------------------------
# Phase B — _generate_repo_description output structure test
# ---------------------------------------------------------------------------


class TestGenerateRepoDescriptionPhaseB:
    """Output structure of _generate_repo_description (Phase 1 only)."""

    def test_output_is_valid_yaml_frontmatter_plus_body(self, fake_repo):
        """
        Output has a parseable YAML frontmatter dict and a non-empty body.

        Uses line-aware splitting so '---' inside YAML values cannot
        accidentally be treated as the closing fence.
        """
        from code_indexer.global_repos.meta_description_hook import (
            _generate_repo_description,
        )

        mock_info = MagicMock()
        mock_info.technologies = ["Python"]
        mock_info.purpose = "library"
        mock_info.summary = "A test library"
        mock_info.features = ["feature1"]
        mock_info.use_cases = ["use case 1"]

        with patch(
            "code_indexer.global_repos.meta_description_hook.RepoAnalyzer"
        ) as MockAnalyzer:
            MockAnalyzer.return_value.extract_info.return_value = mock_info
            content = _generate_repo_description(
                "fake-repo",
                "https://github.com/test/fake-repo",
                str(fake_repo),
            )

        assert isinstance(content, str), (
            "_generate_repo_description must return a str (not a tuple)"
        )

        fm_dict, body = _split_frontmatter_lines(content)

        assert isinstance(fm_dict, dict), "Frontmatter must parse as a YAML dict"
        assert len(fm_dict) > 0, "Frontmatter dict must not be empty"
        assert body.strip(), "Body after closing --- must be non-empty"


# ---------------------------------------------------------------------------
# Phase C — atomic_write_description helper tests
# ---------------------------------------------------------------------------


class TestAtomicWriteDescription:
    """Tests for the atomic_write_description helper."""

    def test_atomic_write_creates_file(self, tmp_path):
        """atomic_write_description creates the target file with the given content."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "test.md"
        content = "# Hello\n\nAtomic write test.\n"

        atomic_write_description(target, content)

        assert target.exists()
        assert target.read_text(encoding="utf-8") == content

    def test_atomic_write_replaces_existing(self, tmp_path):
        """atomic_write_description overwrites an existing file atomically."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "existing.md"
        target.write_text("old content", encoding="utf-8")

        atomic_write_description(target, "new content")

        assert target.read_text(encoding="utf-8") == "new content"

    def test_atomic_write_no_partial_state(self, tmp_path):
        """No .tmp files remain in the directory after a successful write."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "partial_test.md"
        content = "X" * 10_000

        atomic_write_description(target, content)

        remaining_tmp = list(tmp_path.glob("*.tmp"))
        assert remaining_tmp == [], f"Leftover .tmp files: {remaining_tmp}"
        assert target.read_text(encoding="utf-8") == content

    def test_atomic_write_acquires_and_releases_lock(self, tmp_path):
        """atomic_write_description acquires and releases the cidx-meta write lock."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "locked.md"
        mock_scheduler = MagicMock()

        atomic_write_description(target, "content", refresh_scheduler=mock_scheduler)

        mock_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="lifecycle_writer"
        )
        mock_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="lifecycle_writer"
        )

    def test_atomic_write_releases_lock_even_on_write_failure(self, tmp_path):
        """Lock is released even when os.replace raises."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "fail.md"
        mock_scheduler = MagicMock()

        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write_description(
                    target, "content", refresh_scheduler=mock_scheduler
                )

        mock_scheduler.release_write_lock.assert_called_once()
