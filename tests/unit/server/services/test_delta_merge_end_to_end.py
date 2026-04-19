"""
Service-level regression tests for Bug #834 — delta merge frontmatter duplication.

Tests `_update_domain_file` end-to-end to verify the final file written to disk
has exactly one YAML frontmatter block (2 `---` markers).  Regression guard
that exercises the full call chain:

    _update_domain_file
        → build_delta_merge_prompt  (uses body-only existing_content)
        → invoke_delta_merge_file   (writes body-only to temp; strips Claude output)
        → _update_frontmatter_timestamp  (rebuilds final file with one frontmatter block)
        → domain_file.write_text()

Mocks:
- subprocess.run: the external Claude CLI process (unavoidable external boundary).
- golden_repos_manager, config_manager, tracking_backend: unused service constructor
  dependencies stubbed minimally for construction only — not called by this code path.
- config: lightweight stub carrying only the three scalar settings read by
  _update_domain_file (timeout, max_turns, fact_check_enabled).
"""

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.services.dependency_map_service import DependencyMapService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUBPROCESS_PATH = "code_indexer.global_repos.dependency_map_analyzer.subprocess.run"
_MTIME_TICK_S = 0.02

_EXISTING_FULL_CONTENT = (
    "---\n"
    "domain: cloud-infrastructure-platform\n"
    "last_analyzed: 2026-01-01T00:00:00+00:00\n"
    "---\n\n"
    "# Cloud Infrastructure Platform\n\n"
    "## Overview\n\n"
    "Existing analysis content for cloud infrastructure.\n\n"
    "## Repository Roles\n\n"
    "### my-repo\n\n"
    "Primary infrastructure repository.\n"
)

# Body that Claude returns after delta merge (no frontmatter — correct Claude behaviour)
_CLAUDE_UPDATED_BODY = (
    "# Cloud Infrastructure Platform\n\n"
    "## Overview\n\n"
    "Updated analysis content after delta merge.\n\n"
    "## Repository Roles\n\n"
    "### my-repo\n\n"
    "Primary infrastructure repository — updated.\n"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_domain_file(tmp_path) -> Path:
    """Create a domain file with frontmatter on disk."""
    domain_file = tmp_path / "cloud-infrastructure-platform.md"
    domain_file.write_text(_EXISTING_FULL_CONTENT)
    return domain_file


@pytest.fixture
def analyzer(tmp_path) -> DependencyMapAnalyzer:
    """Real DependencyMapAnalyzer with temp directories."""
    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()
    cidx_meta_path = tmp_path / "cidx-meta"
    cidx_meta_path.mkdir()
    return DependencyMapAnalyzer(
        golden_repos_root=golden_repos_root,
        cidx_meta_path=cidx_meta_path,
        pass_timeout=600,
    )


@pytest.fixture
def service(analyzer) -> DependencyMapService:
    """DependencyMapService with real analyzer.
    Unused constructor dependencies are stubbed minimally — not called by this path.
    """
    return DependencyMapService(
        golden_repos_manager=MagicMock(),
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=analyzer,
    )


@pytest.fixture
def config() -> Any:
    """Minimal config stub carrying only the three scalars read by _update_domain_file."""
    cfg = MagicMock()
    cfg.dependency_map_pass_timeout_seconds = 60
    cfg.dependency_map_delta_max_turns = 5
    cfg.dep_map_fact_check_enabled = False
    return cfg


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_update_domain_file_ends_with_exactly_two_frontmatter_markers(
    service, tmp_domain_file, config
):
    """Bug #834 regression guard: the final file written to disk must contain
    exactly 2 `---` markers (one opening, one closing frontmatter pair).

    Scenario:
    - Existing file on disk has YAML frontmatter (realistic shape).
    - Mocked Claude returns body-only content (correct Claude behaviour).
    - After _update_domain_file completes, the written file must have
      exactly one frontmatter block — i.e., exactly 2 occurrences of `---`
      on their own line.

    Without the Bug #834 fix, the prompt embedded existing_content (with
    frontmatter) and the final file was reconstructed with double frontmatter,
    producing quadruple `---` delimiters.
    """

    def _claude_returns_updated_body(domain_file_parent: Path):
        """Build a subprocess.run side_effect that writes _CLAUDE_UPDATED_BODY."""

        def _side_effect(*args, **kwargs):
            matched = list(domain_file_parent.glob("_delta_merge_*.md"))
            assert len(matched) == 1, f"Expected 1 temp file, got: {matched}"
            time.sleep(_MTIME_TICK_S)
            matched[0].write_text(_CLAUDE_UPDATED_BODY)
            import subprocess as _subprocess

            return _subprocess.CompletedProcess(
                args=[], returncode=0, stdout="FILE_EDIT_COMPLETE", stderr=""
            )

        return _side_effect

    with patch(
        _SUBPROCESS_PATH,
        side_effect=_claude_returns_updated_body(tmp_domain_file.parent),
    ):
        service._update_domain_file(
            domain_name="cloud-infrastructure-platform",
            domain_file=tmp_domain_file,
            changed_repos=["my-repo"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cloud-infrastructure-platform"],
            config=config,
        )

    final_content = tmp_domain_file.read_text()

    # Count standalone `---` lines (frontmatter delimiters)
    delimiter_count = sum(
        1 for line in final_content.splitlines() if line.strip() == "---"
    )

    assert delimiter_count == 2, (
        f"Expected exactly 2 '---' frontmatter delimiters in final file, "
        f"got {delimiter_count}.\n\nFinal content:\n{final_content}"
    )

    # Verify the body content was actually updated
    assert "Updated analysis content after delta merge." in final_content, (
        "Updated body content not found in final file"
    )

    # Verify last_analyzed was refreshed (not left as the original 2026-01-01 date)
    assert "last_analyzed: 2026-01-01T00:00:00+00:00" not in final_content, (
        "Frontmatter timestamp was not updated"
    )
