"""
Unit tests for ConfigFixer project_id detection (Bug #85).

Tests verify that ConfigurationValidator.detect_correct_project_name() uses
FileIdentifier.get_project_id() as the single source of truth, preventing
project_id mismatch for versioned directories (CoW clones).
"""

import subprocess
import pytest

from code_indexer.services.config_fixer import ConfigurationValidator


class TestConfigFixerProjectId:
    """Test ConfigurationValidator.detect_correct_project_name() uses FileIdentifier."""

    @pytest.fixture
    def temp_project(self, tmp_path):
        """Create a temporary git repository in a versioned directory."""
        # Create versioned directory (simulates CoW clone)
        project_dir = tmp_path / "v_1769727231"
        project_dir.mkdir()

        # Create .code-indexer config directory
        config_dir = project_dir / ".code-indexer"
        config_dir.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=project_dir,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=project_dir,
            check=True,
        )

        # Add git remote (real repo name)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/user/evolution.git"],
            cwd=project_dir,
            check=True,
        )

        return {"project_dir": project_dir, "config_dir": config_dir}

    def test_detect_correct_project_name_uses_file_identifier(self, temp_project):
        """Verify detect_correct_project_name() uses FileIdentifier (Bug #85).

        CRITICAL: This test reproduces Bug #85 scenario:
        - Directory name: v_1769727231 (versioned CoW clone)
        - Git remote: evolution
        - Expected project_id: "evolution" (from git remote, NOT directory name)

        Old behavior (WRONG):
        - Used codebase_dir.name -> "v_1769727231"
        - Caused project_id mismatch with smart_indexer
        - Triggered unnecessary full reindex

        New behavior (CORRECT):
        - Uses FileIdentifier.get_project_id() -> "evolution"
        - Matches smart_indexer's project_id
        - No unnecessary full reindex
        """
        config_dir = temp_project["config_dir"]
        project_dir = temp_project["project_dir"]

        validator = ConfigurationValidator(config_dir)

        # Call the method under test
        detected_project_name = validator.detect_correct_project_name()

        # CRITICAL: Must return git repo name, NOT directory name
        assert detected_project_name == "evolution"
        assert detected_project_name != "v_1769727231"
        assert detected_project_name != "v-1769727231"

        # Verify it matches what FileIdentifier would return
        from code_indexer.services.file_identifier import FileIdentifier
        file_identifier = FileIdentifier(project_dir)
        expected_project_id = file_identifier.get_project_id()

        assert detected_project_name == expected_project_id

    def test_detect_correct_project_name_non_git_fallback(self, tmp_path):
        """Verify detect_correct_project_name() falls back to directory name for non-git."""
        # Create non-git directory
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()

        config_dir = project_dir / ".code-indexer"
        config_dir.mkdir()

        validator = ConfigurationValidator(config_dir)

        detected_project_name = validator.detect_correct_project_name()

        # Should use directory name when no git remote
        assert detected_project_name == "my-project"

        # Verify consistency with FileIdentifier
        from code_indexer.services.file_identifier import FileIdentifier
        file_identifier = FileIdentifier(project_dir)
        expected_project_id = file_identifier.get_project_id()

        assert detected_project_name == expected_project_id
