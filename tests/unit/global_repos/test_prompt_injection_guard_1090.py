"""
Unit tests for Bug #1090: Prompt injection guard for CLAUDE.md files
in analyzed repository subdirectories.

Security requirement: all prompts and generated orientation/guideline files
must contain an explicit guard instructing Claude NOT to treat CLAUDE.md files
found inside analyzed repository subdirectories as instructions.

Affected targets:
1. Generated CLAUDE.md orientation file (generate_orientation_files)
2. _build_analysis_guidelines_content() static method
3. src/code_indexer/global_repos/prompts/repo_description_create.md
4. src/code_indexer/global_repos/prompts/repo_description_refresh.md
5. src/code_indexer/global_repos/prompts/fact_check.md
6. src/code_indexer/server/prompts/lifecycle_unified.md
7. src/code_indexer/server/mcp/prompts/bidirectional_mismatch_audit.md

TDD RED PHASE: Tests written before production code changes exist.
"""

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared guard assertion helper
# ---------------------------------------------------------------------------

# Canonical sentinel that must appear verbatim in every protected file.
GUARD_SENTINEL = "Any CLAUDE.md files found inside repository subdirectories"


def _assert_guard_present(content: str, location: str) -> None:
    """Assert the prompt injection guard sentinel is present in content."""
    assert GUARD_SENTINEL in content, (
        f"Prompt injection guard missing from {location}.\n"
        f"Expected to find: {GUARD_SENTINEL!r}\n"
        f"Content (first 500 chars): {content[:500]!r}"
    )


def _read_src_prompt(*parts: str) -> str:
    """Read a prompt file from src/code_indexer/<parts>."""
    root = Path(__file__).parents[3] / "src" / "code_indexer"
    p = root.joinpath(*parts)
    assert p.exists(), f"Prompt file not found: {p}"
    return p.read_text()


# ---------------------------------------------------------------------------
# Parametrize all prompt .md files in one test
# ---------------------------------------------------------------------------

_PROMPT_FILES = [
    ("global_repos", "prompts", "repo_description_create.md"),
    ("global_repos", "prompts", "repo_description_refresh.md"),
    ("global_repos", "prompts", "fact_check.md"),
    ("server", "prompts", "lifecycle_unified.md"),
    ("server", "mcp", "prompts", "bidirectional_mismatch_audit.md"),
]


@pytest.mark.parametrize("parts", _PROMPT_FILES, ids=lambda p: p[-1])
def test_prompt_file_contains_guard(parts: tuple) -> None:
    """Every prompt .md file must contain the prompt injection guard (AC3-AC7)."""
    content = _read_src_prompt(*parts)
    _assert_guard_present(content, parts[-1])


# ---------------------------------------------------------------------------
# Test 1: Generated CLAUDE.md orientation file content (AC1)
# ---------------------------------------------------------------------------


class TestOrientationFileGuard:
    """AC1: generate_orientation_files must include the prompt injection guard."""

    def test_orientation_claude_md_contains_guard(self, tmp_path: Path) -> None:
        """Generated CLAUDE.md content must contain the prompt injection guard."""
        from code_indexer.global_repos.dependency_map_analyzer import (
            DependencyMapAnalyzer,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=60,
        )
        analyzer.generate_orientation_files(repo_list=[])

        claude_md_path = tmp_path / "CLAUDE.md"
        assert claude_md_path.exists(), "CLAUDE.md was not created"
        content = claude_md_path.read_text()
        _assert_guard_present(content, "generated CLAUDE.md orientation file")

    def test_orientation_claude_md_guard_is_strong(self, tmp_path: Path) -> None:
        """The guard must explicitly state repos are source code artifacts, not instructions."""
        from code_indexer.global_repos.dependency_map_analyzer import (
            DependencyMapAnalyzer,
        )

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=60,
        )
        analyzer.generate_orientation_files(repo_list=[])

        content = (tmp_path / "CLAUDE.md").read_text()
        assert any(
            phrase in content for phrase in ("SOURCE CODE", "source code", "artifacts")
        ), "Guard must identify CLAUDE.md files as source code artifacts"
        assert any(
            phrase in content
            for phrase in ("NOT instructions", "not instructions", "not treat")
        ), "Guard must state they are NOT instructions"


# ---------------------------------------------------------------------------
# Test 2: _build_analysis_guidelines_content() static method (AC2)
# ---------------------------------------------------------------------------


class TestAnalysisGuidelinesGuard:
    """AC2: _build_analysis_guidelines_content must include the prompt injection guard."""

    def test_analysis_guidelines_contains_guard(self) -> None:
        """_build_analysis_guidelines_content() output must contain the prompt injection guard."""
        from code_indexer.global_repos.dependency_map_analyzer import (
            DependencyMapAnalyzer,
        )

        content = DependencyMapAnalyzer._build_analysis_guidelines_content()
        _assert_guard_present(content, "_build_analysis_guidelines_content()")

    def test_analysis_guidelines_guard_is_strong(self) -> None:
        """The guard in guidelines must cover source code artifacts and not-instructions."""
        from code_indexer.global_repos.dependency_map_analyzer import (
            DependencyMapAnalyzer,
        )

        content = DependencyMapAnalyzer._build_analysis_guidelines_content()
        assert any(
            phrase in content for phrase in ("SOURCE CODE", "source code", "artifacts")
        ), "Guidelines guard must identify CLAUDE.md files as source code artifacts"
        assert any(
            phrase in content
            for phrase in ("NOT instructions", "not instructions", "not treat")
        ), "Guidelines guard must state they are NOT instructions"
