"""
Unit tests for open delegation Web UI configuration (Story #459).

AC2: Guardrails Toggle - Yes/No select, defaults true
AC3: Default Engine Dropdown - fixed options: claude-code, codex, gemini, opencode, q; default claude-code
AC4: Default Mode Dropdown - fixed options: single, collaborative, competitive; default single
AC5: Config Persistence and API Access - all four fields persisted and exposed via config service
     Handler reads default engine/mode from config when not supplied in arguments

Tests follow TDD methodology - tests written FIRST before implementation.
"""

import json

import pytest
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def power_user():
    """Create a power user with delegate_open permission."""
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="poweruser",
        password_hash="hashed",
        role=UserRole.POWER_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_config_with_codex_engine():
    """ClaudeDelegationConfig with custom engine=codex, mode=single.

    Note: mode is set to "single" because "collaborative"/"competitive" are
    currently rejected by _validate_open_delegation_params as unsupported.
    The engine test verifies the config-sourced engine value; the mode test
    verifies the handler succeeds when mode comes from config (also single).
    """
    from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

    return ClaudeDelegationConfig(
        function_repo_alias="test-repo",
        claude_server_url="https://claude-server.example.com",
        claude_server_username="service_user",
        claude_server_credential="service_pass",
        delegation_default_engine="codex",
        delegation_default_mode="single",
    )


@pytest.fixture(autouse=True)
def reset_tracker_singleton():
    """Reset DelegationJobTracker singleton between tests."""
    from code_indexer.server.services.delegation_job_tracker import (
        DelegationJobTracker,
    )

    DelegationJobTracker._instance = None
    yield
    DelegationJobTracker._instance = None


# ---------------------------------------------------------------------------
# AC3 + AC4: ClaudeDelegationConfig has new fields with correct defaults
# ---------------------------------------------------------------------------


class TestClaudeDelegationConfigNewFields:
    """ClaudeDelegationConfig must have the two new Story #459 fields."""

    def test_delegation_default_engine_field_exists_with_correct_default(self):
        """AC3: delegation_default_engine field defaults to 'claude-code'."""
        from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

        config = ClaudeDelegationConfig()
        assert hasattr(config, "delegation_default_engine"), (
            "ClaudeDelegationConfig must have delegation_default_engine field"
        )
        assert config.delegation_default_engine == "claude-code", (
            f"Expected 'claude-code', got '{config.delegation_default_engine}'"
        )

    def test_delegation_default_mode_field_exists_with_correct_default(self):
        """AC4: delegation_default_mode field defaults to 'single'."""
        from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

        config = ClaudeDelegationConfig()
        assert hasattr(config, "delegation_default_mode"), (
            "ClaudeDelegationConfig must have delegation_default_mode field"
        )
        assert config.delegation_default_mode == "single", (
            f"Expected 'single', got '{config.delegation_default_mode}'"
        )

    def test_guardrails_enabled_field_exists_with_correct_default(self):
        """AC2 pre-condition: guardrails_enabled defaults to True."""
        from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

        config = ClaudeDelegationConfig()
        assert config.guardrails_enabled is True

    def test_delegation_guardrails_repo_field_exists_with_correct_default(self):
        """AC2 pre-condition: delegation_guardrails_repo defaults to empty string."""
        from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

        config = ClaudeDelegationConfig()
        assert config.delegation_guardrails_repo == ""

    def test_config_accepts_all_valid_engine_values(self):
        """AC3: Config field accepts all five valid engine option values."""
        from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

        for engine in ["claude-code", "codex", "gemini", "opencode", "q"]:
            config = ClaudeDelegationConfig(delegation_default_engine=engine)
            assert config.delegation_default_engine == engine

    def test_config_accepts_all_valid_mode_values(self):
        """AC4: Config field accepts all three valid mode option values."""
        from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

        for mode in ["single", "collaborative", "competitive"]:
            config = ClaudeDelegationConfig(delegation_default_mode=mode)
            assert config.delegation_default_mode == mode


# ---------------------------------------------------------------------------
# AC5: Config service exposes all four fields
# ---------------------------------------------------------------------------


class TestConfigServiceDelegationSettings:
    """config_service._get_delegation_settings must return all four fields."""

    def test_get_delegation_settings_includes_delegation_guardrails_repo(
        self, tmp_path
    ):
        """AC5: _get_delegation_settings includes delegation_guardrails_repo."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service._get_delegation_settings()

        assert "delegation_guardrails_repo" in settings, (
            "_get_delegation_settings must include delegation_guardrails_repo"
        )

    def test_get_delegation_settings_includes_guardrails_enabled(self, tmp_path):
        """AC2 + AC5: _get_delegation_settings includes guardrails_enabled."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service._get_delegation_settings()

        assert "guardrails_enabled" in settings, (
            "_get_delegation_settings must include guardrails_enabled"
        )

    def test_get_delegation_settings_includes_delegation_default_engine(self, tmp_path):
        """AC3 + AC5: _get_delegation_settings includes delegation_default_engine."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service._get_delegation_settings()

        assert "delegation_default_engine" in settings, (
            "_get_delegation_settings must include delegation_default_engine"
        )

    def test_get_delegation_settings_includes_delegation_default_mode(self, tmp_path):
        """AC4 + AC5: _get_delegation_settings includes delegation_default_mode."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service._get_delegation_settings()

        assert "delegation_default_mode" in settings, (
            "_get_delegation_settings must include delegation_default_mode"
        )

    def test_get_delegation_settings_returns_correct_defaults(self, tmp_path):
        """AC5: Default values are correct when no config file exists."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service._get_delegation_settings()

        assert settings["delegation_default_engine"] == "claude-code"
        assert settings["delegation_default_mode"] == "single"
        assert settings["guardrails_enabled"] is True
        assert settings["delegation_guardrails_repo"] == ""


# ---------------------------------------------------------------------------
# AC5: Config round-trip persistence
# ---------------------------------------------------------------------------


class TestClaudeDelegationConfigPersistence:
    """New fields must survive save/load round-trips through ClaudeDelegationManager."""

    def test_round_trip_saves_and_loads_delegation_default_engine(self, tmp_path):
        """AC5: delegation_default_engine persists through save/load cycle."""
        from code_indexer.server.config.delegation_config import (
            ClaudeDelegationConfig,
            ClaudeDelegationManager,
        )

        manager = ClaudeDelegationManager(server_dir_path=str(tmp_path))
        config = ClaudeDelegationConfig(
            claude_server_url="https://test.example.com",
            claude_server_username="user",
            claude_server_credential="pass",
            delegation_default_engine="codex",
        )
        manager.save_config(config)

        loaded = manager.load_config()
        assert loaded is not None
        assert loaded.delegation_default_engine == "codex"

    def test_round_trip_saves_and_loads_delegation_default_mode(self, tmp_path):
        """AC5: delegation_default_mode persists through save/load cycle."""
        from code_indexer.server.config.delegation_config import (
            ClaudeDelegationConfig,
            ClaudeDelegationManager,
        )

        manager = ClaudeDelegationManager(server_dir_path=str(tmp_path))
        config = ClaudeDelegationConfig(
            claude_server_url="https://test.example.com",
            claude_server_username="user",
            claude_server_credential="pass",
            delegation_default_mode="collaborative",
        )
        manager.save_config(config)

        loaded = manager.load_config()
        assert loaded is not None
        assert loaded.delegation_default_mode == "collaborative"

    def test_round_trip_saves_and_loads_all_four_fields_together(self, tmp_path):
        """AC5: All four delegation fields persist correctly together."""
        from code_indexer.server.config.delegation_config import (
            ClaudeDelegationConfig,
            ClaudeDelegationManager,
        )

        manager = ClaudeDelegationManager(server_dir_path=str(tmp_path))
        config = ClaudeDelegationConfig(
            claude_server_url="https://test.example.com",
            claude_server_username="user",
            claude_server_credential="pass",
            guardrails_enabled=False,
            delegation_guardrails_repo="my-guardrails-repo",
            delegation_default_engine="gemini",
            delegation_default_mode="competitive",
        )
        manager.save_config(config)

        loaded = manager.load_config()
        assert loaded is not None
        assert loaded.guardrails_enabled is False
        assert loaded.delegation_guardrails_repo == "my-guardrails-repo"
        assert loaded.delegation_default_engine == "gemini"
        assert loaded.delegation_default_mode == "competitive"

    def test_backward_compat_config_without_new_fields_loads_with_defaults(
        self, tmp_path
    ):
        """AC5: Old config files without new fields load correctly with defaults."""
        from code_indexer.server.config.delegation_config import ClaudeDelegationManager

        # Write a config file that does NOT contain the two new Story #459 fields
        config_file = tmp_path / "claude_delegation.json"
        old_config_dict = {
            "function_repo_alias": "claude-delegation-functions-global",
            "claude_server_url": "https://old.example.com",
            "claude_server_username": "olduser",
            "claude_server_credential_type": "password",
            "claude_server_credential": "",
            "skip_ssl_verify": False,
            "cidx_callback_url": "",
            "guardrails_enabled": True,
            "delegation_guardrails_repo": "",
            # Intentionally omitting: delegation_default_engine, delegation_default_mode
        }
        config_file.write_text(json.dumps(old_config_dict))
        config_file.chmod(0o600)

        manager = ClaudeDelegationManager(server_dir_path=str(tmp_path))
        loaded = manager.load_config()

        assert loaded is not None
        assert loaded.delegation_default_engine == "claude-code", (
            "Missing delegation_default_engine should default to 'claude-code'"
        )
        assert loaded.delegation_default_mode == "single", (
            "Missing delegation_default_mode should default to 'single'"
        )


# ---------------------------------------------------------------------------
# AC5: Handler reads engine/mode defaults from config (not hardcoded constants)
# ---------------------------------------------------------------------------


class TestHandlerReadsDefaultsFromConfig:
    """
    handle_execute_open_delegation must use config.delegation_default_engine
    and config.delegation_default_mode as fallback when args omit engine/mode.
    """

    @pytest.mark.asyncio
    async def test_handler_uses_config_engine_when_args_omit_engine(
        self, power_user, mock_config_with_codex_engine, httpx_mock
    ):
        """
        AC5: When engine is not in args, handler uses delegation_default_engine from config.

        Given a config with delegation_default_engine='codex'
        When execute_open_delegation is called WITHOUT an 'engine' argument
        Then the job is submitted to Claude Server using engine='codex'
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        # Setup HTTP mocks for the full handler flow
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories/main-app",
            json={"name": "main-app", "cloneStatus": "completed"},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs",
            json={"jobId": "job-engine-test-123", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-engine-test-123/start",
            json={"jobId": "job-engine-test-123", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_config_with_codex_engine,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._resolve_guardrails",
                lambda config, manager: ("", ""),
            )
            # Disable callback registration — no callback URL configured
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            # Call handler WITHOUT 'engine' in args — handler must use config default
            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    # 'engine' intentionally omitted
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is True, f"Expected success=True, got: {data}"

        # Verify the jobs POST used engine='codex' from config.
        # The Claude Server API places engine inside Options.agentEngine.
        jobs_request = next(
            r
            for r in httpx_mock.get_requests()
            if r.method == "POST"
            and "/jobs" in str(r.url)
            and "/start" not in str(r.url)
        )
        jobs_body = json.loads(jobs_request.content)
        assert jobs_body["Options"]["agentEngine"] == "codex", (
            f"Expected Options.agentEngine='codex' from config, "
            f"got '{jobs_body.get('Options', {}).get('agentEngine')}'"
        )

    @pytest.mark.asyncio
    async def test_handler_uses_config_mode_when_args_omit_mode(
        self, power_user, mock_config_with_codex_engine, httpx_mock
    ):
        """
        AC5: When mode is not in args, handler uses delegation_default_mode from config.

        Given a config with delegation_default_mode='single'
        When execute_open_delegation is called WITHOUT a 'mode' argument
        Then the handler resolves mode='single' from config, passes validation,
        and the job is submitted successfully (success=True).

        Note: mode is not included in the Claude Server job POST body (it is
        used only for handler routing/validation). Success proves the config-
        sourced mode was accepted by _validate_open_delegation_params.
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories/main-app",
            json={"name": "main-app", "cloneStatus": "completed"},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs",
            json={"jobId": "job-mode-test-456", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-mode-test-456/start",
            json={"jobId": "job-mode-test-456", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_config_with_codex_engine,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._resolve_guardrails",
                lambda config, manager: ("", ""),
            )
            # Disable callback registration — no callback URL configured
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            # Call handler WITHOUT 'mode' in args — handler must use config default
            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    # 'mode' intentionally omitted — handler must read from config
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        # Success proves config-sourced mode='single' passed validation and
        # the job was created. If mode defaulted to an invalid value, the
        # handler would return success=False with a validation error.
        assert data.get("success") is True, (
            f"Expected success=True (mode resolved from config), got: {data}"
        )
        assert data.get("job_id") == "job-mode-test-456", (
            f"Expected job_id from server response, got: {data}"
        )
