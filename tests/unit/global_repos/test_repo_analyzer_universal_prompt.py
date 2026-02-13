"""
Unit tests for RepoAnalyzer universal prompt functionality (Story #190 AC1, AC6).

Tests that get_prompt() returns a single universal prompt teaching Claude
to discover repo type dynamically by examining folder structure.
"""

import pytest
from pathlib import Path
from code_indexer.global_repos.repo_analyzer import RepoAnalyzer


class TestUniversalPromptGeneration:
    """Test universal prompt generation for create and refresh modes."""

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
        assert "activity" in prompt.lower() or "summary" in prompt.lower()  # activity_summary

        # Verify output format instructions
        assert "repo_type" in prompt.lower()  # Must include repo_type in YAML
        assert "yaml" in prompt.lower() or "frontmatter" in prompt.lower()

    def test_get_prompt_refresh_mode_passes_last_analyzed_and_description(self, tmp_path):
        """AC2: get_prompt('refresh', ...) returns universal refresh prompt with last_analyzed and existing_description."""
        analyzer = RepoAnalyzer(str(tmp_path))

        last_analyzed = "2025-01-15T10:00:00Z"
        existing_description = "Existing repo description with metadata"

        prompt = analyzer.get_prompt(
            mode="refresh",
            last_analyzed=last_analyzed,
            existing_description=existing_description
        )

        # Verify it's a single prompt
        assert isinstance(prompt, str)
        assert len(prompt) > 100

        # Verify last_analyzed timestamp passed to Claude
        assert last_analyzed in prompt or "last analyzed" in prompt.lower()

        # Verify existing_description passed to Claude
        assert existing_description in prompt or "existing description" in prompt.lower()

        # Verify git log instruction for change detection
        assert "git log" in prompt.lower()
        assert "since" in prompt.lower() or "after" in prompt.lower()

        # Verify Langfuse-specific refresh instruction (new files only)
        assert "new" in prompt.lower()  # Focus on new files
        assert "modified after" in prompt.lower() or "files modified" in prompt.lower()

    def test_get_prompt_no_code_branching_on_repo_type(self, tmp_path):
        """AC6: Single universal prompt - NO code branching on repo type."""
        analyzer = RepoAnalyzer(str(tmp_path))

        # Call get_prompt multiple times with different parameters
        prompt_create_1 = analyzer.get_prompt(mode="create")
        prompt_create_2 = analyzer.get_prompt(mode="create")

        # Should return identical prompts for same mode (deterministic)
        assert prompt_create_1 == prompt_create_2

        # Prompt should contain instructions for BOTH repo types (universal)
        assert ".git" in prompt_create_1.lower() or "git" in prompt_create_1.lower()
        assert "uuid" in prompt_create_1.lower()  # Langfuse detection hint

        # Both refresh prompts should be identical too
        prompt_refresh_1 = analyzer.get_prompt(
            mode="refresh",
            last_analyzed="2025-01-01T00:00:00Z",
            existing_description="desc"
        )
        prompt_refresh_2 = analyzer.get_prompt(
            mode="refresh",
            last_analyzed="2025-01-01T00:00:00Z",
            existing_description="desc"
        )
        assert prompt_refresh_1 == prompt_refresh_2

    def test_get_prompt_create_mode_teaches_repo_type_discovery(self, tmp_path):
        """AC6: Prompt explicitly teaches Claude how to discover repo type."""
        analyzer = RepoAnalyzer(str(tmp_path))

        prompt = analyzer.get_prompt(mode="create")

        # Should teach Claude to look for .git directory
        assert ".git" in prompt or "git directory" in prompt.lower()

        # Should teach Claude to look for UUID folders (Langfuse traces)
        assert "uuid" in prompt.lower() or "universally unique identifier" in prompt.lower()

        # Should teach Claude to look for JSON trace files
        assert "json" in prompt.lower()
        assert "trace" in prompt.lower() or "turn" in prompt.lower()

        # Should instruct Claude to set repo_type in output
        assert "repo_type" in prompt

    def test_get_prompt_refresh_mode_instructs_git_change_detection(self, tmp_path):
        """AC2: Refresh prompt instructs Claude to run git log since last_analyzed."""
        analyzer = RepoAnalyzer(str(tmp_path))

        last_analyzed = "2025-01-10T15:30:00Z"
        prompt = analyzer.get_prompt(
            mode="refresh",
            last_analyzed=last_analyzed,
            existing_description="Some description"
        )

        # Should instruct git log with date filter
        assert "git log" in prompt.lower()
        assert last_analyzed in prompt or "since" in prompt.lower()

        # Should instruct Claude to update only if material changes
        assert "material" in prompt.lower() or "significant" in prompt.lower()
        assert "update" in prompt.lower()

    def test_get_prompt_refresh_mode_instructs_langfuse_new_files_only(self, tmp_path):
        """AC3: Refresh prompt instructs Claude to find files modified after last_analyzed."""
        analyzer = RepoAnalyzer(str(tmp_path))

        prompt = analyzer.get_prompt(
            mode="refresh",
            last_analyzed="2025-01-10T00:00:00Z",
            existing_description="Existing"
        )

        # Should explicitly state traces are immutable
        assert "immutable" in prompt.lower() or "unchanging" in prompt.lower() or "established" in prompt.lower()

        # Should instruct focus on new files
        assert "new" in prompt.lower()
        assert "files" in prompt.lower()
        assert "modified after" in prompt.lower() or "after" in prompt.lower()

        # Should instruct merging (not replacing) findings
        assert "merge" in prompt.lower() or "add" in prompt.lower()
        assert "preserve" in prompt.lower() or "keep" in prompt.lower()

    def test_get_prompt_invalid_mode_raises_error(self, tmp_path):
        """get_prompt() raises ValueError for invalid mode."""
        analyzer = RepoAnalyzer(str(tmp_path))

        with pytest.raises(ValueError, match="mode"):
            analyzer.get_prompt(mode="invalid")

    def test_get_prompt_refresh_requires_last_analyzed(self, tmp_path):
        """get_prompt('refresh') raises ValueError if last_analyzed not provided."""
        analyzer = RepoAnalyzer(str(tmp_path))

        with pytest.raises(ValueError, match="last_analyzed"):
            analyzer.get_prompt(mode="refresh", existing_description="desc")

    def test_get_prompt_refresh_requires_existing_description(self, tmp_path):
        """get_prompt('refresh') raises ValueError if existing_description not provided."""
        analyzer = RepoAnalyzer(str(tmp_path))

        with pytest.raises(ValueError, match="existing_description"):
            analyzer.get_prompt(mode="refresh", last_analyzed="2025-01-01T00:00:00Z")
