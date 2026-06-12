"""
Unit tests for RepoAnalyzer universal prompt functionality (Story #190 AC1, AC6).

Tests that get_prompt() returns a single universal prompt teaching Claude
to discover repo type dynamically by examining folder structure.

#1094: refresh mode was removed from get_prompt() — refresh-aware description
refinement now lives in the lifecycle-unified path.  Only "create" mode remains.
"""

import pytest
from code_indexer.global_repos.repo_analyzer import RepoAnalyzer


class TestUniversalPromptGeneration:
    """Test universal prompt generation for create mode (#1094: create-only)."""

    def test_get_prompt_create_mode_returns_universal_prompt(self, tmp_path):
        """AC1: get_prompt('create') returns universal initial prompt with repo type discovery hints."""
        analyzer = RepoAnalyzer(str(tmp_path))

        prompt = analyzer.get_prompt(mode="create")

        # Verify it's a single prompt (string)
        assert isinstance(prompt, str)
        assert len(prompt) > 100  # Should be substantial

        # Verify repo type discovery instructions present
        assert ".git" in prompt.lower() or "git directory" in prompt.lower()
        assert "uuid" in prompt.lower()  # Langfuse folder detection
        assert "json" in prompt.lower()  # Trace file detection

        # Verify Langfuse-specific extraction instructions
        assert "user" in prompt.lower()  # user_identity
        assert "project" in prompt.lower()  # projects_detected
        assert (
            "activity" in prompt.lower() or "summary" in prompt.lower()
        )  # activity_summary

        # Verify output format instructions
        assert "repo_type" in prompt.lower()  # Must include repo_type in YAML
        assert "yaml" in prompt.lower() or "frontmatter" in prompt.lower()

    def test_get_prompt_create_mode_is_deterministic(self, tmp_path):
        """AC6: Single universal prompt - identical output for the same mode."""
        analyzer = RepoAnalyzer(str(tmp_path))

        prompt_create_1 = analyzer.get_prompt(mode="create")
        prompt_create_2 = analyzer.get_prompt(mode="create")

        # Should return identical prompts for same mode (deterministic)
        assert prompt_create_1 == prompt_create_2

        # Prompt should contain instructions for BOTH repo types (universal)
        assert ".git" in prompt_create_1.lower() or "git" in prompt_create_1.lower()
        assert "uuid" in prompt_create_1.lower()  # Langfuse detection hint

    def test_get_prompt_create_mode_teaches_repo_type_discovery(self, tmp_path):
        """AC6: Prompt explicitly teaches Claude how to discover repo type."""
        analyzer = RepoAnalyzer(str(tmp_path))

        prompt = analyzer.get_prompt(mode="create")

        # Should teach Claude to look for .git directory
        assert ".git" in prompt or "git directory" in prompt.lower()

        # Should teach Claude to look for UUID folders (Langfuse traces)
        assert (
            "uuid" in prompt.lower()
            or "universally unique identifier" in prompt.lower()
        )

        # Should teach Claude to look for JSON trace files
        assert "json" in prompt.lower()
        assert "trace" in prompt.lower() or "turn" in prompt.lower()

        # Should instruct Claude to set repo_type in output
        assert "repo_type" in prompt

    def test_get_prompt_invalid_mode_raises_error(self, tmp_path):
        """get_prompt() raises ValueError for an unknown mode."""
        analyzer = RepoAnalyzer(str(tmp_path))

        with pytest.raises(ValueError, match="mode"):
            analyzer.get_prompt(mode="invalid")

    def test_get_prompt_refresh_mode_now_rejected(self, tmp_path):
        """#1094: refresh mode was removed — get_prompt('refresh') raises ValueError."""
        analyzer = RepoAnalyzer(str(tmp_path))

        with pytest.raises(ValueError, match="mode"):
            analyzer.get_prompt(mode="refresh")
