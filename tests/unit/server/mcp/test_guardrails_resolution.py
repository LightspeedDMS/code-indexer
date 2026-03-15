"""
Unit tests for guardrails resolution - Story #457.

Tests cover:
1. Returns DEFAULT_GUARDRAILS_TEMPLATE when no repo configured
2. Returns DEFAULT_GUARDRAILS_TEMPLATE when repo configured but system-prompt.md not found (with warning logged)
3. Returns resolved template from repo when system-prompt.md exists
4. Returns ("", None) when guardrails_enabled=False
5. Returns correct repo alias when guardrails loaded from repo
6. Returns None for repo alias when using default template
7. Returns formatted package list when multiple language files exist
8. Returns "No pre-approved packages" when packages directory doesn't exist
9. Returns "No pre-approved packages" when packages directory is empty
10. Handles single language file correctly
11. Ignores non-standard language directories
12. Guardrails prepended to prompt before job creation
13. Guardrails repo added to repositories list when guardrails enabled
14. Guardrails repo NOT added when guardrails disabled
15. Prompt sent without guardrails when guardrails_enabled=False
16. Default guardrails used when no repo configured (end-to-end handler test)
17. Contains all 6 safety categories (filesystem, process, git, system, package, secrets)
18. Contains {packages_context} placeholder
19. ClaudeDelegationConfig loads without new fields (defaults applied)
20. ClaudeDelegationConfig loads with new fields
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.config.delegation_config import (
    ClaudeDelegationConfig,
    DEFAULT_GUARDRAILS_TEMPLATE,
)
from code_indexer.server.mcp.handlers import (
    _load_packages_context,
    _resolve_guardrails,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_mcp_data(mcp_response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the JSON payload from an MCP content-array response."""
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        result: Dict[str, Any] = json.loads(content[0]["text"])
        return result
    return {}


def make_power_user() -> User:
    return User(
        username="poweruser",
        password_hash="hashed",
        role=UserRole.POWER_USER,
        created_at=datetime.now(timezone.utc),
    )


def make_delegation_config(**kwargs) -> ClaudeDelegationConfig:
    """Build a ClaudeDelegationConfig with sensible defaults."""
    defaults = dict(
        claude_server_url="http://claude-server:8080",
        claude_server_username="admin",
        claude_server_credential="password",
        guardrails_enabled=True,
        delegation_guardrails_repo="",
    )
    defaults.update(kwargs)
    return ClaudeDelegationConfig(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo(tmp_path):
    """Provide a temporary directory that acts as a golden repo root."""
    return tmp_path


@pytest.fixture
def repo_with_system_prompt(tmp_repo):
    """Temporary repo with a guardrails/system-prompt.md file."""
    guardrails_dir = tmp_repo / "guardrails"
    guardrails_dir.mkdir()
    prompt_file = guardrails_dir / "system-prompt.md"
    prompt_file.write_text(
        "CUSTOM GUARDRAILS\n\n5. PACKAGE SAFETY\n   {packages_context}\n"
    )
    return tmp_repo


@pytest.fixture
def repo_with_packages(tmp_repo):
    """Temporary repo with approved package files for python and nodejs."""
    guardrails_dir = tmp_repo / "guardrails"
    guardrails_dir.mkdir()
    prompt_file = guardrails_dir / "system-prompt.md"
    prompt_file.write_text("GUARDRAILS\n{packages_context}\n")

    packages_dir = tmp_repo / "packages"
    python_dir = packages_dir / "python"
    python_dir.mkdir(parents=True)
    (python_dir / "approved.txt").write_text("requests\nnumpy\npandas\n")

    nodejs_dir = packages_dir / "nodejs"
    nodejs_dir.mkdir(parents=True)
    (nodejs_dir / "approved.txt").write_text("lodash\nexpress\n")

    return tmp_repo


# ---------------------------------------------------------------------------
# Test _load_packages_context
# ---------------------------------------------------------------------------


class TestLoadPackagesContext:
    """Tests for the _load_packages_context helper."""

    def test_returns_no_packages_when_directory_missing(self, tmp_repo):
        """TC-8: No packages directory -> returns 'No pre-approved packages' message."""
        result = _load_packages_context(str(tmp_repo))
        assert "No pre-approved packages" in result

    def test_returns_no_packages_when_directory_empty(self, tmp_repo):
        """TC-9: Empty packages directory -> returns 'No pre-approved packages' message."""
        (tmp_repo / "packages").mkdir()
        result = _load_packages_context(str(tmp_repo))
        assert "No pre-approved packages" in result

    def test_returns_formatted_list_for_multiple_languages(self, repo_with_packages):
        """TC-7: Multiple language approved.txt files -> formatted package list."""
        result = _load_packages_context(str(repo_with_packages))
        assert "python" in result.lower()
        assert "requests" in result
        assert "numpy" in result
        assert "nodejs" in result
        assert "lodash" in result
        assert "express" in result

    def test_handles_single_language_file(self, tmp_repo):
        """TC-10: Single language file -> correctly formatted output."""
        pkg_dir = tmp_repo / "packages" / "python"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "approved.txt").write_text("flask\ndjango\n")

        result = _load_packages_context(str(tmp_repo))
        assert "python" in result.lower()
        assert "flask" in result
        assert "django" in result

    def test_ignores_non_standard_language_directories(self, tmp_repo):
        """TC-11: Non-standard language dir (e.g. 'cobol') not included in output."""
        # Standard language present
        py_dir = tmp_repo / "packages" / "python"
        py_dir.mkdir(parents=True)
        (py_dir / "approved.txt").write_text("requests\n")

        # Non-standard language
        cobol_dir = tmp_repo / "packages" / "cobol"
        cobol_dir.mkdir(parents=True)
        (cobol_dir / "approved.txt").write_text("some-cobol-lib\n")

        result = _load_packages_context(str(tmp_repo))
        assert "python" in result.lower()
        assert "cobol" not in result.lower()
        assert "some-cobol-lib" not in result


# ---------------------------------------------------------------------------
# Test _resolve_guardrails
# ---------------------------------------------------------------------------


class TestResolveGuardrails:
    """Tests for the _resolve_guardrails function."""

    def test_returns_default_when_no_repo_configured(self):
        """TC-1: No repo configured -> returns resolved default guardrails, None alias."""
        config = make_delegation_config(delegation_guardrails_repo="")
        golden_repo_manager = MagicMock()

        text, alias = _resolve_guardrails(config, golden_repo_manager)

        # The returned text is the resolved template (placeholder already substituted)
        assert "SAFETY GUARDRAILS" in text
        assert "FILESYSTEM" in text.upper()
        assert "{packages_context}" not in text
        assert alias is None

    def test_returns_empty_when_guardrails_disabled(self):
        """TC-4: guardrails_enabled=False -> returns ("", None)."""
        config = make_delegation_config(
            guardrails_enabled=False, delegation_guardrails_repo="my-guardrails"
        )
        golden_repo_manager = MagicMock()

        text, alias = _resolve_guardrails(config, golden_repo_manager)

        assert text == ""
        assert alias is None

    def test_returns_default_and_logs_warning_when_system_prompt_missing(
        self, tmp_repo, caplog
    ):
        """TC-2: Repo configured but system-prompt.md absent -> default template + warning."""
        config = make_delegation_config(delegation_guardrails_repo="my-guardrails")
        golden_repo_manager = MagicMock()
        golden_repo_manager.get_actual_repo_path.return_value = str(tmp_repo)

        with caplog.at_level(logging.WARNING):
            text, alias = _resolve_guardrails(config, golden_repo_manager)

        # Returns resolved default template (placeholder already substituted)
        assert "SAFETY GUARDRAILS" in text
        assert "FILESYSTEM" in text.upper()
        assert "{packages_context}" not in text
        # alias is returned so agent can still access packages/ from the repo
        assert alias == "my-guardrails"
        # A warning must have been logged
        assert any(
            "system-prompt.md" in record.message
            or "guardrail" in record.message.lower()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )

    def test_returns_resolved_template_from_repo(self, repo_with_system_prompt):
        """TC-3: Repo has system-prompt.md -> returns resolved template content."""
        config = make_delegation_config(delegation_guardrails_repo="my-guardrails")
        golden_repo_manager = MagicMock()
        golden_repo_manager.get_actual_repo_path.return_value = str(
            repo_with_system_prompt
        )

        text, alias = _resolve_guardrails(config, golden_repo_manager)

        assert "CUSTOM GUARDRAILS" in text
        # {packages_context} placeholder should be replaced
        assert "{packages_context}" not in text

    def test_returns_correct_alias_when_loaded_from_repo(self, repo_with_system_prompt):
        """TC-5: Repo loaded successfully -> returned alias matches config repo."""
        config = make_delegation_config(delegation_guardrails_repo="my-guardrails")
        golden_repo_manager = MagicMock()
        golden_repo_manager.get_actual_repo_path.return_value = str(
            repo_with_system_prompt
        )

        _text, alias = _resolve_guardrails(config, golden_repo_manager)

        assert alias == "my-guardrails"

    def test_returns_none_alias_when_using_default_template(self):
        """TC-6: No repo configured -> alias is None."""
        config = make_delegation_config(delegation_guardrails_repo="")
        golden_repo_manager = MagicMock()

        _text, alias = _resolve_guardrails(config, golden_repo_manager)

        assert alias is None

    def test_packages_context_interpolated_when_packages_present(
        self, repo_with_packages
    ):
        """TC-3 extension: packages context is interpolated into system-prompt."""
        config = make_delegation_config(delegation_guardrails_repo="my-guardrails")
        golden_repo_manager = MagicMock()
        golden_repo_manager.get_actual_repo_path.return_value = str(repo_with_packages)

        text, _alias = _resolve_guardrails(config, golden_repo_manager)

        # Should contain actual package names, not the placeholder
        assert "{packages_context}" not in text
        assert "requests" in text or "lodash" in text


# ---------------------------------------------------------------------------
# Test DEFAULT_GUARDRAILS_TEMPLATE
# ---------------------------------------------------------------------------


class TestDefaultGuardrailsTemplate:
    """Tests for the DEFAULT_GUARDRAILS_TEMPLATE constant."""

    def test_contains_filesystem_safety_category(self):
        """TC-17: FILESYSTEM SAFETY must appear in default template."""
        assert "FILESYSTEM" in DEFAULT_GUARDRAILS_TEMPLATE.upper()

    def test_contains_process_safety_category(self):
        """TC-17: PROCESS SAFETY must appear in default template."""
        assert "PROCESS" in DEFAULT_GUARDRAILS_TEMPLATE.upper()

    def test_contains_git_safety_category(self):
        """TC-17: GIT SAFETY must appear in default template."""
        assert "GIT" in DEFAULT_GUARDRAILS_TEMPLATE.upper()

    def test_contains_system_safety_category(self):
        """TC-17: SYSTEM SAFETY must appear in default template."""
        assert "SYSTEM" in DEFAULT_GUARDRAILS_TEMPLATE.upper()

    def test_contains_package_safety_category(self):
        """TC-17: PACKAGE SAFETY must appear in default template."""
        assert "PACKAGE" in DEFAULT_GUARDRAILS_TEMPLATE.upper()

    def test_contains_secrets_safety_category(self):
        """TC-17: SECRETS SAFETY must appear in default template."""
        assert "SECRET" in DEFAULT_GUARDRAILS_TEMPLATE.upper()

    def test_contains_packages_context_placeholder(self):
        """TC-18: {packages_context} placeholder must be present for interpolation."""
        assert "{packages_context}" in DEFAULT_GUARDRAILS_TEMPLATE


# ---------------------------------------------------------------------------
# Test ClaudeDelegationConfig backward compatibility
# ---------------------------------------------------------------------------


class TestClaudeDelegationConfigBackwardCompatibility:
    """Tests for config field defaults - backward compatibility."""

    def test_loads_without_new_fields(self):
        """TC-19: Config loads without guardrails fields -> defaults applied."""
        # Simulate loading a config dict that doesn't have new guardrails fields
        config = ClaudeDelegationConfig(
            claude_server_url="http://example.com",
            claude_server_username="admin",
            claude_server_credential="secret",
        )
        # New fields should have sensible defaults
        assert config.guardrails_enabled is True
        assert config.delegation_guardrails_repo == ""

    def test_loads_with_new_fields(self):
        """TC-20: Config loads with all new guardrails fields set."""
        config = ClaudeDelegationConfig(
            claude_server_url="http://example.com",
            claude_server_username="admin",
            claude_server_credential="secret",
            guardrails_enabled=False,
            delegation_guardrails_repo="my-guardrails-repo",
        )
        assert config.guardrails_enabled is False
        assert config.delegation_guardrails_repo == "my-guardrails-repo"

    def test_default_guardrails_enabled_is_true(self):
        """TC-19: guardrails_enabled defaults to True (security-first)."""
        config = ClaudeDelegationConfig()
        assert config.guardrails_enabled is True

    def test_default_delegation_guardrails_repo_is_empty(self):
        """TC-19: delegation_guardrails_repo defaults to empty string."""
        config = ClaudeDelegationConfig()
        assert config.delegation_guardrails_repo == ""


# ---------------------------------------------------------------------------
# Test handler integration (prompt prepending, repo list modification)
# ---------------------------------------------------------------------------


class TestHandlerIntegration:
    """Integration tests for guardrails within handle_execute_open_delegation."""

    def _make_job_result(self, job_id="test-job-123"):
        return {"jobId": job_id}

    @pytest.fixture
    def power_user(self):
        return make_power_user()

    @pytest.fixture
    def base_delegation_config(self):
        return make_delegation_config(
            guardrails_enabled=True, delegation_guardrails_repo=""
        )

    @pytest.fixture
    def no_guardrails_config(self):
        return make_delegation_config(
            guardrails_enabled=False, delegation_guardrails_repo=""
        )

    @pytest.mark.asyncio
    async def test_guardrails_prepended_to_prompt(self, power_user):
        """TC-12: Guardrails text prepended to prompt before job creation."""
        captured_prompts = []

        async def fake_create_job(prompt, repositories, engine, model, timeout):
            captured_prompts.append(prompt)
            return {"jobId": "job-abc"}

        config = make_delegation_config(
            guardrails_enabled=True, delegation_guardrails_repo=""
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                return_value=config,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_repo_ready_timeout",
                return_value=1.0,
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.clients.claude_server_client.ClaudeServerClient"
            ) as mock_client_cls,
        ):
            mock_app.golden_repo_manager = None
            mock_client = AsyncMock()
            mock_client.wait_for_repo_ready = AsyncMock(return_value=True)
            mock_client.create_job_with_options = AsyncMock(side_effect=fake_create_job)
            mock_client.register_callback = AsyncMock()
            mock_client.start_job = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tracker = AsyncMock()
            tracker.register_job = AsyncMock()
            with patch(
                "code_indexer.server.services.delegation_job_tracker.DelegationJobTracker"
            ) as mock_tracker_cls:
                mock_tracker_cls.get_instance.return_value = tracker

                from code_indexer.server.mcp.handlers import (
                    handle_execute_open_delegation,
                )

                await handle_execute_open_delegation(
                    args={
                        "prompt": "Do the task",
                        "repositories": ["repo-a"],
                    },
                    user=power_user,
                )

        assert len(captured_prompts) == 1
        sent_prompt = captured_prompts[0]
        # Default guardrails should be prepended
        assert "SAFETY GUARDRAILS" in sent_prompt or "FILESYSTEM" in sent_prompt.upper()
        # Original user prompt should still be present
        assert "Do the task" in sent_prompt

    @pytest.mark.asyncio
    async def test_prompt_not_modified_when_guardrails_disabled(self, power_user):
        """TC-15: When guardrails disabled, prompt sent as-is."""
        captured_prompts = []

        async def fake_create_job(prompt, repositories, engine, model, timeout):
            captured_prompts.append(prompt)
            return {"jobId": "job-abc"}

        config = make_delegation_config(
            guardrails_enabled=False, delegation_guardrails_repo=""
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                return_value=config,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_repo_ready_timeout",
                return_value=1.0,
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.clients.claude_server_client.ClaudeServerClient"
            ) as mock_client_cls,
        ):
            mock_app.golden_repo_manager = None
            mock_client = AsyncMock()
            mock_client.wait_for_repo_ready = AsyncMock(return_value=True)
            mock_client.create_job_with_options = AsyncMock(side_effect=fake_create_job)
            mock_client.register_callback = AsyncMock()
            mock_client.start_job = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tracker = AsyncMock()
            tracker.register_job = AsyncMock()
            with patch(
                "code_indexer.server.services.delegation_job_tracker.DelegationJobTracker"
            ) as mock_tracker_cls:
                mock_tracker_cls.get_instance.return_value = tracker

                from code_indexer.server.mcp.handlers import (
                    handle_execute_open_delegation,
                )

                await handle_execute_open_delegation(
                    args={
                        "prompt": "Do the task",
                        "repositories": ["repo-a"],
                    },
                    user=power_user,
                )

        assert len(captured_prompts) == 1
        sent_prompt = captured_prompts[0]
        # Prompt should be exactly as provided (no guardrails)
        assert sent_prompt == "Do the task"

    @pytest.mark.asyncio
    async def test_guardrails_repo_added_to_repositories_when_enabled(
        self, power_user, tmp_path
    ):
        """TC-13: When guardrails loaded from repo, alias added to repositories list."""
        captured_repos = []

        # Write a system-prompt.md in temp dir
        guardrails_dir = tmp_path / "guardrails"
        guardrails_dir.mkdir()
        (guardrails_dir / "system-prompt.md").write_text(
            "GUARDRAILS\n{packages_context}\n"
        )

        async def fake_create_job(prompt, repositories, engine, model, timeout):
            captured_repos.extend(repositories)
            return {"jobId": "job-abc"}

        config = make_delegation_config(
            guardrails_enabled=True,
            delegation_guardrails_repo="my-guardrails",
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                return_value=config,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_repo_ready_timeout",
                return_value=1.0,
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.clients.claude_server_client.ClaudeServerClient"
            ) as mock_client_cls,
        ):
            mock_grm = MagicMock()
            mock_grm.get_actual_repo_path.return_value = str(tmp_path)
            mock_grm.get_golden_repo.return_value = None
            mock_app.golden_repo_manager = mock_grm

            mock_client = AsyncMock()
            mock_client.wait_for_repo_ready = AsyncMock(return_value=True)
            mock_client.create_job_with_options = AsyncMock(side_effect=fake_create_job)
            mock_client.register_callback = AsyncMock()
            mock_client.start_job = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tracker = AsyncMock()
            tracker.register_job = AsyncMock()
            with patch(
                "code_indexer.server.services.delegation_job_tracker.DelegationJobTracker"
            ) as mock_tracker_cls:
                mock_tracker_cls.get_instance.return_value = tracker

                from code_indexer.server.mcp.handlers import (
                    handle_execute_open_delegation,
                )

                await handle_execute_open_delegation(
                    args={
                        "prompt": "Do the task",
                        "repositories": ["repo-a"],
                    },
                    user=power_user,
                )

        assert "my-guardrails" in captured_repos

    @pytest.mark.asyncio
    async def test_guardrails_repo_not_added_when_disabled(self, power_user):
        """TC-14: When guardrails disabled, repo alias NOT added to repositories list."""
        captured_repos = []

        async def fake_create_job(prompt, repositories, engine, model, timeout):
            captured_repos.extend(repositories)
            return {"jobId": "job-abc"}

        config = make_delegation_config(
            guardrails_enabled=False,
            delegation_guardrails_repo="my-guardrails",
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                return_value=config,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_repo_ready_timeout",
                return_value=1.0,
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.clients.claude_server_client.ClaudeServerClient"
            ) as mock_client_cls,
        ):
            mock_app.golden_repo_manager = None
            mock_client = AsyncMock()
            mock_client.wait_for_repo_ready = AsyncMock(return_value=True)
            mock_client.create_job_with_options = AsyncMock(side_effect=fake_create_job)
            mock_client.register_callback = AsyncMock()
            mock_client.start_job = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tracker = AsyncMock()
            tracker.register_job = AsyncMock()
            with patch(
                "code_indexer.server.services.delegation_job_tracker.DelegationJobTracker"
            ) as mock_tracker_cls:
                mock_tracker_cls.get_instance.return_value = tracker

                from code_indexer.server.mcp.handlers import (
                    handle_execute_open_delegation,
                )

                await handle_execute_open_delegation(
                    args={
                        "prompt": "Do the task",
                        "repositories": ["repo-a"],
                    },
                    user=power_user,
                )

        assert "my-guardrails" not in captured_repos

    @pytest.mark.asyncio
    async def test_default_guardrails_used_when_no_repo_configured(self, power_user):
        """TC-16: No guardrails repo configured -> default guardrails prepended."""
        captured_prompts = []

        async def fake_create_job(prompt, repositories, engine, model, timeout):
            captured_prompts.append(prompt)
            return {"jobId": "job-abc"}

        config = make_delegation_config(
            guardrails_enabled=True, delegation_guardrails_repo=""
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                return_value=config,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_repo_ready_timeout",
                return_value=1.0,
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.clients.claude_server_client.ClaudeServerClient"
            ) as mock_client_cls,
        ):
            mock_app.golden_repo_manager = None
            mock_client = AsyncMock()
            mock_client.wait_for_repo_ready = AsyncMock(return_value=True)
            mock_client.create_job_with_options = AsyncMock(side_effect=fake_create_job)
            mock_client.register_callback = AsyncMock()
            mock_client.start_job = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tracker = AsyncMock()
            tracker.register_job = AsyncMock()
            with patch(
                "code_indexer.server.services.delegation_job_tracker.DelegationJobTracker"
            ) as mock_tracker_cls:
                mock_tracker_cls.get_instance.return_value = tracker

                from code_indexer.server.mcp.handlers import (
                    handle_execute_open_delegation,
                )

                await handle_execute_open_delegation(
                    args={
                        "prompt": "Do the task",
                        "repositories": ["repo-a"],
                    },
                    user=power_user,
                )

        assert len(captured_prompts) == 1
        sent_prompt = captured_prompts[0]
        # Default guardrails template should appear in the sent prompt
        assert "SAFETY GUARDRAILS" in sent_prompt
        assert "Do the task" in sent_prompt
        # Separator should be present
        assert "USER OBJECTIVE" in sent_prompt
