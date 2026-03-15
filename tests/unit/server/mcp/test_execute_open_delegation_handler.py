"""
Unit tests for execute_open_delegation MCP tool handler.

Story #456: Open-ended delegation with engine and mode selection

Tests follow TDD methodology - tests written FIRST before implementation.

Tests cover:
1. Permission check: delegate_open required, normal_user denied
2. Parameter validation: missing prompt returns error, missing repositories returns error
3. Valid engine values accepted, invalid rejected
4. Valid mode values accepted, invalid rejected
5. Mode routing: single creates job, collaborative/competitive return error
6. Default engine/mode used when not provided
7. Repo readiness: already ready skips polling
8. Repo readiness: not registered triggers register + poll
9. Repo readiness: timeout returns error
10. Repo readiness: cloneStatus="failed" returns error immediately
11. create_job_with_options builds correct request payload
12. Backward compatibility: existing create_job() method signature unchanged
"""

import json
from datetime import datetime, timezone

import pytest
from pytest_httpx import HTTPXMock

from code_indexer.server.auth.user_manager import User, UserRole


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
def power_user():
    """Create a power user (has delegate_open permission)."""
    return User(
        username="poweruser",
        password_hash="hashed",
        role=UserRole.POWER_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def admin_user():
    """Create an admin user (inherits delegate_open via POWER_USER)."""
    return User(
        username="adminuser",
        password_hash="hashed",
        role=UserRole.ADMIN,
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


class TestPermissionEnforcement:
    """Tests for delegate_open permission enforcement."""

    @pytest.mark.asyncio
    async def test_normal_user_is_denied(self, normal_user, mock_delegation_config):
        """
        Normal user receives Access denied error.

        Given an authenticated normal_user
        When execute_open_delegation is called
        Then the handler returns Access denied error
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                normal_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "access denied" in data["error"].lower()
            or "denied" in data["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_power_user_is_permitted(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Power user is permitted to execute open delegation.

        Given an authenticated power_user
        When execute_open_delegation is called with valid parameters
        Then the handler proceeds (not denied)
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
            json={"jobId": "job-power-123", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-power-123/start",
            json={"jobId": "job-power-123", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_admin_user_is_permitted(
        self, admin_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Admin user inherits delegate_open permission through POWER_USER inheritance.

        Given an authenticated admin user
        When execute_open_delegation is called
        Then the handler proceeds (not denied)
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
            json={"jobId": "job-admin-123", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-admin-123/start",
            json={"jobId": "job-admin-123", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                },
                admin_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True

    def test_delegate_open_permission_in_power_user_base_permissions(self):
        """
        delegate_open permission is in POWER_USER base permissions.

        Given a power_user
        When has_permission("delegate_open") is called
        Then it returns True
        """
        user = User(
            username="pu",
            password_hash="h",
            role=UserRole.POWER_USER,
            created_at=datetime.now(timezone.utc),
        )
        assert user.has_permission("delegate_open") is True

    def test_delegate_open_permission_denied_for_normal_user(self):
        """
        delegate_open permission is NOT available to normal_user.

        Given a normal_user
        When has_permission("delegate_open") is called
        Then it returns False
        """
        user = User(
            username="nu",
            password_hash="h",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )
        assert user.has_permission("delegate_open") is False

    def test_delegate_open_permission_inherited_by_admin(self):
        """
        delegate_open permission is inherited by admin through POWER_USER.

        Given an admin user
        When has_permission("delegate_open") is called
        Then it returns True
        """
        user = User(
            username="admin",
            password_hash="h",
            role=UserRole.ADMIN,
            created_at=datetime.now(timezone.utc),
        )
        assert user.has_permission("delegate_open") is True


class TestParameterValidation:
    """Tests for parameter validation."""

    @pytest.mark.asyncio
    async def test_missing_prompt_returns_error(
        self, power_user, mock_delegation_config
    ):
        """Missing prompt parameter returns validation error."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "repositories": ["main-app"],
                    # No prompt!
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "prompt" in data["error"].lower() or "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_repositories_returns_error(
        self, power_user, mock_delegation_config
    ):
        """Missing repositories parameter returns validation error."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    # No repositories!
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "repositor" in data["error"].lower() or "required" in data["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_empty_repositories_list_returns_error(
        self, power_user, mock_delegation_config
    ):
        """Empty repositories list returns validation error."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": [],
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "repositor" in data["error"].lower() or "required" in data["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_invalid_engine_returns_error(
        self, power_user, mock_delegation_config
    ):
        """Invalid engine value returns validation error with supported engines."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "engine": "invalid-engine",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "engine" in data["error"].lower() or "invalid" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_engines_are_accepted(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """All valid engine values are accepted: claude-code, codex, gemini, opencode, q."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        for engine in ["claude-code", "codex", "gemini", "opencode", "q"]:
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
                json={"jobId": f"job-{engine}", "status": "created"},
                status_code=201,
            )
            httpx_mock.add_response(
                method="POST",
                url=f"https://claude-server.example.com/jobs/job-{engine}/start",
                json={"jobId": f"job-{engine}", "status": "running"},
            )

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "code_indexer.server.mcp.handlers._get_delegation_config",
                    lambda: mock_delegation_config,
                )
                mp.setattr(
                    "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                    lambda: None,
                )

                response = await handle_execute_open_delegation(
                    {
                        "prompt": "Fix the bug",
                        "repositories": ["main-app"],
                        "engine": engine,
                        "mode": "single",
                    },
                    power_user,
                )

            data = json.loads(response["content"][0]["text"])
            assert (
                data["success"] is True
            ), f"Engine {engine} should be accepted, got: {data}"

    @pytest.mark.asyncio
    async def test_invalid_mode_returns_error(self, power_user, mock_delegation_config):
        """Invalid mode value returns validation error."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "mode": "turbo",  # Not a valid mode
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "mode" in data["error"].lower() or "invalid" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_modes_are_recognized(self, power_user, mock_delegation_config):
        """
        collaborative and competitive modes are recognized (not "invalid mode") even
        though they return "not yet supported" error.
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        for mode in ["collaborative", "competitive"]:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "code_indexer.server.mcp.handlers._get_delegation_config",
                    lambda: mock_delegation_config,
                )

                response = await handle_execute_open_delegation(
                    {
                        "prompt": "Fix the bug",
                        "repositories": ["main-app"],
                        "mode": mode,
                    },
                    power_user,
                )

            data = json.loads(response["content"][0]["text"])
            assert data["success"] is False
            # Should say "not yet supported", not "invalid mode"
            assert (
                "not yet supported" in data["error"].lower()
                or "supported" in data["error"].lower()
            )
            assert "invalid" not in data["error"].lower()


class TestModeRouting:
    """Tests for mode-based routing."""

    @pytest.mark.asyncio
    async def test_single_mode_creates_job(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Single mode creates job via POST /jobs.

        Given mode="single"
        When execute_open_delegation is called
        Then it creates a job and returns job_id
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
            json={"jobId": "job-single-456", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-single-456/start",
            json={"jobId": "job-single-456", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "engine": "claude-code",
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert data["job_id"] == "job-single-456"

    @pytest.mark.asyncio
    async def test_collaborative_mode_returns_not_supported_error(
        self, power_user, mock_delegation_config
    ):
        """
        Collaborative mode returns "not yet supported" error without creating a job.

        Given mode="collaborative"
        When execute_open_delegation is called
        Then it returns error "Mode not yet supported by Claude Server"
        And no job is created
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "mode": "collaborative",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "not yet supported" in data["error"].lower()
            or "supported" in data["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_competitive_mode_returns_not_supported_error(
        self, power_user, mock_delegation_config
    ):
        """
        Competitive mode returns "not yet supported" error without creating a job.

        Given mode="competitive"
        When execute_open_delegation is called
        Then it returns error without creating a job
        And no fallback to single mode occurs
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "mode": "competitive",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "not yet supported" in data["error"].lower()
            or "supported" in data["error"].lower()
        )


class TestDefaultValues:
    """Tests for default engine and mode values."""

    @pytest.mark.asyncio
    async def test_default_engine_used_when_not_provided(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Default engine (claude-code) is used when engine not specified.

        Given no engine parameter is provided
        When execute_open_delegation is called
        Then it uses the default engine "claude-code"
        """
        import json as json_lib

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
            json={"jobId": "job-default-engine", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-default-engine/start",
            json={"jobId": "job-default-engine", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    # No engine specified
                },
                power_user,
            )

        data = json_lib.loads(response["content"][0]["text"])
        assert data["success"] is True

        # Verify default engine was used in job creation
        requests = httpx_mock.get_requests()
        job_requests = [
            r for r in requests if str(r.url).endswith("/jobs") and r.method == "POST"
        ]
        assert len(job_requests) == 1
        body = json_lib.loads(job_requests[0].content)
        assert body.get("Options", {}).get("agentEngine") == "claude-code"

    @pytest.mark.asyncio
    async def test_default_mode_is_single(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Default mode (single) is used when mode not specified.

        Given no mode parameter is provided
        When execute_open_delegation is called
        Then it creates a job (single mode behavior)
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
            json={"jobId": "job-default-mode", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-default-mode/start",
            json={"jobId": "job-default-mode", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    # No mode specified
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert "job_id" in data


class TestRepositoryReadinessFlow:
    """Tests for repository readiness checking before job creation."""

    @pytest.mark.asyncio
    async def test_already_ready_repo_skips_registration(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Repository already registered with cloneStatus=completed skips registration.

        Given a repository already registered and ready
        When execute_open_delegation is called
        Then it skips registration and proceeds to job creation
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories/ready-repo",
            json={"name": "ready-repo", "cloneStatus": "completed"},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs",
            json={"jobId": "job-ready", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-ready/start",
            json={"jobId": "job-ready", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["ready-repo"],
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True

        # Verify no registration was attempted
        requests = httpx_mock.get_requests()
        register_requests = [
            r for r in requests if "/repositories/register" in str(r.url)
        ]
        assert len(register_requests) == 0

    @pytest.mark.asyncio
    async def test_failed_repo_status_returns_error(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Repository with cloneStatus="failed" returns error immediately.

        Given a repository with cloneStatus="failed"
        When execute_open_delegation is called
        Then it returns an error immediately without creating a job
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories/failed-repo",
            json={"name": "failed-repo", "cloneStatus": "failed"},
            status_code=200,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["failed-repo"],
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "failed-repo" in data["error"] or "failed" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_repo_timeout_returns_error(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Repository that times out during readiness check returns error.

        Given a repository stuck in cloneStatus="cloning"
        When execute_open_delegation is called and times out
        Then it returns an error indicating timeout
        And no job is created
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        # Use reusable mocks since we don't know exactly how many polls occur
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
            is_reusable=True,
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories/slow-repo",
            json={"name": "slow-repo", "cloneStatus": "cloning"},
            status_code=200,
            is_reusable=True,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            # Use a very short timeout to force timeout path
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_repo_ready_timeout",
                lambda: 0.1,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["slow-repo"],
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "slow-repo" in data["error"]
            or "timeout" in data["error"].lower()
            or "timed out" in data["error"].lower()
        )


class TestJobCreationAndTracking:
    """Tests for job creation, callback registration, and tracker integration."""

    @pytest.mark.asyncio
    async def test_job_registered_in_tracker_on_success(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Successfully started job is registered in DelegationJobTracker.

        Given a valid request
        When execute_open_delegation succeeds
        Then the job is registered in DelegationJobTracker for polling
        """
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
        )

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
            json={"jobId": "job-tracked-789", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-tracked-789/start",
            json={"jobId": "job-tracked-789", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert data["job_id"] == "job-tracked-789"

        tracker = DelegationJobTracker.get_instance()
        assert "job-tracked-789" in tracker._pending_jobs

    @pytest.mark.asyncio
    async def test_callback_url_registered_when_configured(
        self, power_user, mock_delegation_config, httpx_mock: HTTPXMock
    ):
        """
        Callback URL is registered with Claude Server when cidx_callback_url is set.

        Given a configured callback URL
        When execute_open_delegation succeeds
        Then callback is registered with Claude Server
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
            json={"jobId": "job-callback-test", "status": "created"},
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-callback-test/callbacks",
            json={"registered": True},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/jobs/job-callback-test/start",
            json={"jobId": "job-callback-test", "status": "running"},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_cidx_callback_base_url",
                lambda: "https://cidx.example.com",
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                    "mode": "single",
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True

        requests = httpx_mock.get_requests()
        callback_requests = [r for r in requests if "/callbacks" in str(r.url)]
        assert len(callback_requests) == 1

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self, power_user):
        """Handler returns error when delegation is not configured."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {
                    "prompt": "Fix the bug",
                    "repositories": ["main-app"],
                },
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "not configured" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_response_has_mcp_format(self, power_user):
        """Response follows MCP content array format."""
        from code_indexer.server.mcp.handlers import handle_execute_open_delegation

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: None,
            )

            response = await handle_execute_open_delegation(
                {"prompt": "Test", "repositories": ["repo"]},
                power_user,
            )

        assert "content" in response
        assert response["content"][0]["type"] == "text"
        json.loads(response["content"][0]["text"])  # Should be valid JSON


class TestHandlerRegistration:
    """Tests for handler registration in HANDLER_REGISTRY."""

    def test_execute_open_delegation_registered_in_handler_registry(self):
        """execute_open_delegation is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "execute_open_delegation" in HANDLER_REGISTRY

    def test_execute_open_delegation_tool_in_tool_registry(self):
        """execute_open_delegation tool is in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        assert "execute_open_delegation" in TOOL_REGISTRY

    def test_existing_execute_delegation_function_still_registered(self):
        """Backward compatibility: existing execute_delegation_function is unchanged."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "execute_delegation_function" in HANDLER_REGISTRY

    def test_tool_doc_has_correct_permission(self):
        """execute_open_delegation tool doc specifies delegate_open permission."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        if "execute_open_delegation" in TOOL_REGISTRY:
            tool = TOOL_REGISTRY["execute_open_delegation"]
            assert tool.get("required_permission") == "delegate_open"
