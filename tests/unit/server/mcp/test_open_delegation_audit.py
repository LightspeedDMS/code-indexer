"""
Unit tests for open delegation audit trail.

Story #458: Delegation audit trail enhancement

Every successful open delegation call must produce an audit log entry via
AuditLogService.log() with the required fields.

Tests written FIRST following TDD methodology (tests written before implementation).

Tests cover:
1. Audit entry created on successful delegation with all required fields
2. Prompt truncated to 500 characters when longer
3. Prompt NOT truncated when 500 chars or shorter
4. action_type is "open_delegation_executed"
5. target_type is "delegation"
6. target_id matches job_id
7. details JSON contains all required keys (prompt, engine, mode, repositories,
   guardrails_enabled)
8. admin_id matches authenticated username
9. Audit NOT created when delegation fails (permission denied)
10. Audit NOT created when delegation fails (validation error)
11. Audit service not found (None) — no crash, just skipped
"""

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_httpx import HTTPXMock

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def power_user():
    """Create a power user (has delegate_open permission)."""
    return User(
        username="poweruser",
        password_hash="hashed",
        role=UserRole.POWER_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def normal_user():
    """Create a normal user (no delegate_open permission)."""
    return User(
        username="normaluser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_delegation_config():
    """Create mock delegation config."""
    from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

    return ClaudeDelegationConfig(
        function_repo_alias="test-repo",
        claude_server_url="https://claude-server.example.com",
        claude_server_username="service_user",
        claude_server_credential="service_pass",
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


def _make_mock_audit_service():
    """Return a MagicMock that acts as AuditLogService."""
    return MagicMock()


def _get_audit_call_arg(audit_service: MagicMock, kwarg_name: str, pos: int) -> Any:
    """
    Extract an argument from the most recent audit_service.log() call.

    Checks keyword arguments first, then falls back to positional.
    Using list() on args avoids mypy's static tuple-index-out-of-range check.
    Returns Any so callers can use the value directly (e.g. json.loads, assertions).
    """
    call_kwargs = audit_service.log.call_args
    if call_kwargs is None:
        return None
    kwargs = call_kwargs.kwargs or {}
    if kwarg_name in kwargs:
        return kwargs[kwarg_name]
    args_list = list(call_kwargs.args or ())
    return args_list[pos] if len(args_list) > pos else None


def _setup_http_mocks_for_success(
    httpx_mock: HTTPXMock,
    repo_name: str = "main-app",
    job_id: str = "job-audit-test-123",
) -> None:
    """Register standard HTTP mocks for a successful delegation flow."""
    httpx_mock.add_response(
        method="POST",
        url="https://claude-server.example.com/auth/login",
        json={"access_token": "token", "token_type": "bearer"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"https://claude-server.example.com/repositories/{repo_name}",
        json={"name": repo_name, "cloneStatus": "completed"},
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://claude-server.example.com/jobs",
        json={"jobId": job_id, "status": "created"},
        status_code=201,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"https://claude-server.example.com/jobs/{job_id}/start",
        json={"jobId": job_id, "status": "running"},
    )


class TestAuditEntryCreated:
    """Tests that verify audit entry IS created on successful delegation."""

    @pytest.mark.asyncio
    async def test_audit_log_called_on_success(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Audit log is called after successful job creation.

        Given a valid open delegation request
        When execute_open_delegation succeeds
        Then AuditLogService.log() is called exactly once
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-001")
        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        audit_service.log.assert_called_once()

    @pytest.mark.asyncio
    async def test_action_type_is_open_delegation_executed(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        audit action_type is "open_delegation_executed".

        Given a successful delegation
        When audit log entry is created
        Then action_type == "open_delegation_executed"
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-002")
        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        action_type = _get_audit_call_arg(audit_service, "action_type", 1)
        assert action_type == "open_delegation_executed"

    @pytest.mark.asyncio
    async def test_target_type_is_delegation(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        audit target_type is "delegation".

        Given a successful delegation
        When audit log entry is created
        Then target_type == "delegation"
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-003")
        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        target_type = _get_audit_call_arg(audit_service, "target_type", 2)
        assert target_type == "delegation"

    @pytest.mark.asyncio
    async def test_target_id_matches_job_id(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        audit target_id matches the job_id returned by Claude Server.

        Given a successful delegation returning job_id "job-audit-004"
        When audit log entry is created
        Then target_id == "job-audit-004"
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-004")
        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        target_id = _get_audit_call_arg(audit_service, "target_id", 3)
        assert target_id == "job-audit-004"

    @pytest.mark.asyncio
    async def test_admin_id_matches_username(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        audit admin_id matches the authenticated user's username.

        Given power_user with username "poweruser"
        When audit log entry is created
        Then admin_id == "poweruser"
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-005")
        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        admin_id = _get_audit_call_arg(audit_service, "admin_id", 0)
        assert admin_id == "poweruser"

    @pytest.mark.asyncio
    async def test_details_json_contains_all_required_keys(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        audit details JSON contains all required keys.

        Given a successful delegation
        When audit log entry is created
        Then details JSON contains: prompt, engine, mode, repositories,
             guardrails_enabled
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-006")
        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        details_str = _get_audit_call_arg(audit_service, "details", 4)
        assert details_str is not None, "details should not be None"
        details = json.loads(details_str)

        required_keys = {
            "prompt",
            "engine",
            "mode",
            "repositories",
            "guardrails_enabled",
        }
        missing = required_keys - set(details.keys())
        assert not missing, f"Missing keys in details: {missing}"


class TestPromptTruncation:
    """Tests for prompt truncation in audit details."""

    @pytest.mark.asyncio
    async def test_prompt_truncated_to_500_chars_when_longer(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Prompt is truncated to 500 characters when it exceeds that limit.

        Given a prompt longer than 500 characters
        When audit log entry is created
        Then details["prompt"] has exactly 500 characters
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-trunc-001")
        audit_service = _make_mock_audit_service()

        long_prompt = "X" * 600  # Exceeds 500 char limit

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": long_prompt,
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        details_str = _get_audit_call_arg(audit_service, "details", 4)
        details = json.loads(details_str)
        assert len(details["prompt"]) == 500

    @pytest.mark.asyncio
    async def test_prompt_not_truncated_at_500_chars(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Prompt is not truncated when exactly 500 characters.

        Given a prompt of exactly 500 characters
        When audit log entry is created
        Then details["prompt"] retains all 500 characters
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-trunc-002")
        audit_service = _make_mock_audit_service()

        exact_prompt = "Y" * 500  # Exactly at limit

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": exact_prompt,
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        details_str = _get_audit_call_arg(audit_service, "details", 4)
        details = json.loads(details_str)
        assert len(details["prompt"]) == 500

    @pytest.mark.asyncio
    async def test_prompt_not_truncated_when_shorter_than_500(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Prompt is not truncated when shorter than 500 characters.

        Given a prompt of 10 characters
        When audit log entry is created
        Then details["prompt"] retains all original characters
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-audit-trunc-003")
        audit_service = _make_mock_audit_service()

        short_prompt = "Fix bug!"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            await handle_execute_open_delegation(
                {
                    "prompt": short_prompt,
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        assert audit_service.log.call_args is not None
        details_str = _get_audit_call_arg(audit_service, "details", 4)
        details = json.loads(details_str)
        assert details["prompt"] == short_prompt


class TestAuditNotCreatedOnFailure:
    """Tests that verify audit entry is NOT created when delegation fails."""

    @pytest.mark.asyncio
    async def test_audit_not_created_when_permission_denied(
        self, normal_user, mock_delegation_config
    ):
        """
        Audit log is NOT called when user lacks permission.

        Given a normal_user without delegate_open permission
        When execute_open_delegation is called
        Then AuditLogService.log() is NOT called
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                normal_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        audit_service.log.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_not_created_when_validation_fails(
        self, power_user, mock_delegation_config
    ):
        """
        Audit log is NOT called when parameter validation fails.

        Given a request with missing prompt
        When execute_open_delegation fails validation
        Then AuditLogService.log() is NOT called
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        audit_service = _make_mock_audit_service()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                MagicMock(audit_service=audit_service),
            )

            response = await handle_execute_open_delegation(
                {
                    # Missing prompt
                    "repositories": ["main-app"],
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        audit_service.log.assert_not_called()


class TestAuditServiceMissing:
    """Tests for graceful handling when audit service is unavailable."""

    @pytest.mark.asyncio
    async def test_no_crash_when_audit_service_is_none(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Delegation succeeds even when AuditLogService is not available.

        Given app.state has no audit_service attribute
        When execute_open_delegation is called
        Then delegation succeeds (no crash)
        And the job_id is returned
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        _setup_http_mocks_for_success(httpx_mock, job_id="job-no-audit-001")

        # Simulate app.state without audit_service attribute
        mock_state = MagicMock(spec=[])  # Empty spec — no audit_service attribute

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers.app_module.app.state",
                mock_state,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the critical bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        # Delegation should still succeed even without audit service
        assert data["success"] is True
        assert data["job_id"] == "job-no-audit-001"


class TestBackwardCompatibility:
    """Tests that verify existing delegation tools are unchanged."""

    def test_execute_delegation_function_handler_registered(self):
        """
        Existing execute_delegation_function handler is still registered.

        Backward compatibility: existing delegation handler unchanged.
        """
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "execute_delegation_function" in HANDLER_REGISTRY

    def test_execute_open_delegation_handler_registered(self):
        """execute_open_delegation handler is registered."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "execute_open_delegation" in HANDLER_REGISTRY

    def test_audit_log_service_class_has_log_method(self):
        """
        AuditLogService.log() method signature is unchanged.

        We do not modify AuditLogService — verify the log() method exists with
        the expected signature.
        """
        import inspect

        from code_indexer.server.services.audit_log_service import AuditLogService

        assert hasattr(AuditLogService, "log")
        sig = inspect.signature(AuditLogService.log)
        param_names = list(sig.parameters.keys())
        assert "admin_id" in param_names
        assert "action_type" in param_names
        assert "target_type" in param_names
        assert "target_id" in param_names
        assert "details" in param_names
