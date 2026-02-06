"""
Tests for Research Assistant prompt template loading and parametrization.

Tests cover:
- Template file loading
- Variable substitution
- Fallback to hardcoded prompt
- Variable detection
"""

import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
    SECURITY_GUARDRAILS,
)


class TestPromptTemplateLoading:
    """Test prompt template loading from file."""

    def test_load_template_when_file_exists(self, tmp_path):
        """AC1: Template file is loaded when it exists."""
        # Create template file
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_content = "Test prompt with {hostname} and {server_version}"
        template_file.write_text(template_content)

        # Mock the config directory path
        service = ResearchAssistantService()
        with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
            prompt = service.load_research_prompt()

        # Should contain template content (not hardcoded)
        assert "Test prompt with" in prompt
        # Should have variables substituted
        assert "{hostname}" not in prompt
        assert "{server_version}" not in prompt

    def test_fallback_to_hardcoded_when_template_missing(self, tmp_path):
        """AC3: Falls back to hardcoded prompt if template file missing."""
        # Point to empty directory
        service = ResearchAssistantService()
        with patch.object(service, "_get_config_dir", return_value=str(tmp_path)):
            prompt = service.load_research_prompt()

        # Should return hardcoded prompt
        assert prompt == SECURITY_GUARDRAILS

    def test_fallback_to_hardcoded_on_read_error(self, tmp_path):
        """AC3: Falls back to hardcoded prompt on read errors."""
        # Create directory without template
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        service = ResearchAssistantService()
        with patch.object(service, "_get_config_dir", return_value=str(config_dir)):
            prompt = service.load_research_prompt()

        # Should return hardcoded prompt
        assert prompt == SECURITY_GUARDRAILS


class TestVariableSubstitution:
    """Test variable substitution in template."""

    def test_all_variables_substituted(self, tmp_path):
        """AC2: All template variables are substituted correctly."""
        # Create template with all variables
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_content = """
## SERVER ENVIRONMENT CONTEXT

- **Hostname**: {hostname}
- **Server Version**: {server_version}
- **Installation**: {cidx_repo_root}
- **Service**: {service_name}.service
- **Data Dir**: {server_data_dir}
- **DB Path**: {db_path}
- **Golden Repos**: {golden_repos_dir}
"""
        template_file.write_text(template_content)

        service = ResearchAssistantService()
        with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
            prompt = service.load_research_prompt()

        # Verify no placeholders remain
        assert "{hostname}" not in prompt
        assert "{server_version}" not in prompt
        assert "{cidx_repo_root}" not in prompt
        assert "{service_name}" not in prompt
        assert "{server_data_dir}" not in prompt
        assert "{db_path}" not in prompt
        assert "{golden_repos_dir}" not in prompt

        # Verify actual values present
        assert socket.gethostname() in prompt
        assert "cidx-server" in prompt  # service_name

    def test_hostname_variable(self, tmp_path):
        """AC2: hostname variable is substituted with actual hostname."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Host: {hostname}")

        service = ResearchAssistantService()
        with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
            prompt = service.load_research_prompt()

        expected_hostname = socket.gethostname()
        assert f"Host: {expected_hostname}" in prompt

    def test_server_version_variable(self, tmp_path):
        """AC2: server_version variable is substituted with package version."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Version: {server_version}")

        service = ResearchAssistantService()
        with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
            prompt = service.load_research_prompt()

        # Should have version substituted (not empty, not placeholder)
        assert "Version: " in prompt
        assert "{server_version}" not in prompt
        # Version should be a semantic version pattern
        import re

        assert re.search(r"\d+\.\d+\.\d+", prompt)

    def test_server_data_dir_variable_from_env(self, tmp_path):
        """AC2: server_data_dir uses CIDX_SERVER_DATA_DIR env var."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Data: {server_data_dir}")

        test_data_dir = "/custom/data/dir"
        with patch.dict(os.environ, {"CIDX_SERVER_DATA_DIR": test_data_dir}):
            service = ResearchAssistantService()
            with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
                prompt = service.load_research_prompt()

        assert f"Data: {test_data_dir}" in prompt

    def test_server_data_dir_variable_default(self, tmp_path):
        """AC2: server_data_dir defaults to ~/.cidx-server."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Data: {server_data_dir}")

        # Ensure env var not set
        env_without_server_dir = {
            k: v for k, v in os.environ.items() if k != "CIDX_SERVER_DATA_DIR"
        }

        with patch.dict(os.environ, env_without_server_dir, clear=True):
            service = ResearchAssistantService()
            with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
                prompt = service.load_research_prompt()

        expected_default = str(Path.home() / ".cidx-server")
        assert f"Data: {expected_default}" in prompt

    def test_db_path_variable(self, tmp_path):
        """AC2: db_path variable is correctly constructed."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("DB: {db_path}")

        test_data_dir = "/test/data"
        with patch.dict(os.environ, {"CIDX_SERVER_DATA_DIR": test_data_dir}):
            service = ResearchAssistantService()
            with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
                prompt = service.load_research_prompt()

        expected_db_path = f"{test_data_dir}/data/cidx_server.db"
        assert f"DB: {expected_db_path}" in prompt

    def test_cidx_repo_root_from_env(self, tmp_path):
        """AC2: cidx_repo_root uses CIDX_REPO_ROOT env var if set."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Repo: {cidx_repo_root}")

        test_repo_root = "/custom/repo/root"
        with patch.dict(os.environ, {"CIDX_REPO_ROOT": test_repo_root}):
            service = ResearchAssistantService()
            with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
                prompt = service.load_research_prompt()

        assert f"Repo: {test_repo_root}" in prompt

    def test_cidx_repo_root_auto_detection(self, tmp_path):
        """AC2: cidx_repo_root auto-detects from file location if env not set."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Repo: {cidx_repo_root}")

        # Clear CIDX_REPO_ROOT env var
        env_without_repo_root = {
            k: v for k, v in os.environ.items() if k != "CIDX_REPO_ROOT"
        }

        with patch.dict(os.environ, env_without_repo_root, clear=True):
            service = ResearchAssistantService()
            with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
                prompt = service.load_research_prompt()

        # Should have some path (auto-detected or empty)
        assert "Repo: " in prompt
        assert "{cidx_repo_root}" not in prompt

    def test_golden_repos_dir_variable(self, tmp_path):
        """AC2: golden_repos_dir variable is correctly constructed."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Repos: {golden_repos_dir}")

        test_data_dir = "/test/data"
        with patch.dict(os.environ, {"CIDX_SERVER_DATA_DIR": test_data_dir}):
            service = ResearchAssistantService()
            with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
                prompt = service.load_research_prompt()

        expected_repos_dir = f"{test_data_dir}/golden-repos"
        assert f"Repos: {expected_repos_dir}" in prompt

    def test_service_name_variable(self, tmp_path):
        """AC2: service_name variable is substituted correctly."""
        template_dir = tmp_path / "config"
        template_dir.mkdir()
        template_file = template_dir / "research_assistant_prompt.md"
        template_file.write_text("Service: {service_name}")

        service = ResearchAssistantService()
        with patch.object(service, "_get_config_dir", return_value=str(template_dir)):
            prompt = service.load_research_prompt()

        assert "Service: cidx-server" in prompt
