"""
Unit tests for Claude Server proxy MCP tool handlers.

Story #460: Claude Server proxy tools - register repos, list repos, check health.

Tests follow TDD methodology - tests written FIRST before implementation.

Tests cover:
cs_register_repository:
1. Permission: delegate_open required (normal_user denied)
2. Missing alias parameter returns error
3. Invalid alias (not in golden repos) returns error
4. Already registered repo returns current status without re-registering
5. Not registered (404) triggers registration, returns cloneStatus
6. Claude Server unreachable returns error
7. Delegation not configured returns error

cs_list_repositories:
8. Returns formatted list from Claude Server
9. Empty list when no repos registered
10. Delegation not configured returns error

cs_check_health:
11. Returns health data when Claude Server healthy
12. Returns error when Claude Server unreachable
13. Delegation not configured returns error

Backward compatibility:
14. Existing delegation handlers still registered
15. Existing ClaudeServerClient methods unchanged
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

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
def mock_delegation_config():
    """Create mock delegation config."""
    from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

    return ClaudeDelegationConfig(
        function_repo_alias="test-repo",
        claude_server_url="https://claude-server.example.com",
        claude_server_username="service_user",
        claude_server_credential="service_pass",
    )


@pytest.fixture
def mock_golden_repo():
    """Create a mock golden repo with repo_url and default_branch."""
    repo = MagicMock()
    repo.repo_url = "git@github.com:org/my-repo.git"
    repo.default_branch = "main"
    return repo


@pytest.fixture
def mock_app_module_with_golden_repo(mock_golden_repo):
    """Create a mock app_module that has a golden_repo_manager returning one repo."""
    app_module = MagicMock()
    grm = MagicMock()
    grm.get_golden_repo.return_value = mock_golden_repo
    app_module.golden_repo_manager = grm
    return app_module


@pytest.fixture
def mock_app_module_no_golden_repo():
    """Create a mock app_module where alias is not found in golden_repo_manager."""
    app_module = MagicMock()
    grm = MagicMock()
    grm.get_golden_repo.return_value = None
    app_module.golden_repo_manager = grm
    return app_module


# ===========================================================================
# cs_register_repository tests
# ===========================================================================


class TestCsRegisterRepository:
    """Tests for handle_cs_register_repository handler."""

    @pytest.mark.asyncio
    async def test_normal_user_is_denied(self, normal_user, mock_delegation_config):
        """
        Normal user receives Access denied error.

        Given an authenticated normal_user
        When cs_register_repository is called
        Then the handler returns Access denied error
        """
        from code_indexer.server.mcp.handlers import handle_cs_register_repository

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_register_repository(
                {"alias": "my-repo"},
                normal_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "denied" in data["error"].lower() or "access" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_alias_returns_error(
        self, power_user, mock_delegation_config
    ):
        """
        Missing alias parameter returns validation error.

        Given a power_user
        When cs_register_repository is called without alias
        Then an error is returned mentioning alias
        """
        from code_indexer.server.mcp.handlers import handle_cs_register_repository

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_register_repository(
                {},  # No alias!
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "alias" in data["error"].lower() or "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_alias_not_in_golden_repos_returns_error(
        self,
        power_user,
        mock_delegation_config,
        mock_app_module_no_golden_repo,
    ):
        """
        Alias that does not exist in CIDX golden repos returns error.

        Given an alias not registered as a CIDX golden repo
        When cs_register_repository is called
        Then an error is returned indicating the alias is not found
        """
        from code_indexer.server.mcp import handlers as handlers_module
        from code_indexer.server.mcp.handlers import handle_cs_register_repository

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(handlers_module, "app_module", mock_app_module_no_golden_repo)

            response = await handle_cs_register_repository(
                {"alias": "nonexistent-repo"},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "nonexistent-repo" in data["error"]
            or "not found" in data["error"].lower()
            or "alias" in data["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_already_registered_returns_current_status(
        self,
        power_user,
        mock_delegation_config,
        mock_app_module_with_golden_repo,
        httpx_mock: HTTPXMock,
    ):
        """
        Already registered repo returns current status without re-registering.

        Given a repo already registered on Claude Server (GET returns 200)
        When cs_register_repository is called
        Then it returns the current status and does NOT call POST /repositories/register
        """
        from code_indexer.server.mcp import handlers as handlers_module
        from code_indexer.server.mcp.handlers import handle_cs_register_repository

        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories/my-repo",
            json={
                "name": "my-repo",
                "cloneStatus": "completed",
                "cidxAware": True,
                "gitUrl": "git@github.com:org/my-repo.git",
                "branch": "main",
            },
            status_code=200,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(handlers_module, "app_module", mock_app_module_with_golden_repo)

            response = await handle_cs_register_repository(
                {"alias": "my-repo"},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert data["clone_status"] == "completed"

        # Verify no POST /repositories/register was made
        requests = httpx_mock.get_requests()
        register_requests = [
            r for r in requests if "/repositories/register" in str(r.url)
        ]
        assert len(register_requests) == 0

    @pytest.mark.asyncio
    async def test_not_registered_triggers_registration(
        self,
        power_user,
        mock_delegation_config,
        mock_app_module_with_golden_repo,
        httpx_mock: HTTPXMock,
    ):
        """
        Not registered (404) triggers registration, returns cloneStatus.

        Given a repo not registered on Claude Server (GET returns 404)
        When cs_register_repository is called
        Then it calls POST /repositories/register and returns cloneStatus
        """
        from code_indexer.server.mcp import handlers as handlers_module
        from code_indexer.server.mcp.handlers import handle_cs_register_repository

        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories/my-repo",
            status_code=404,
            json={"detail": "Not found"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/repositories/register",
            json={
                "name": "my-repo",
                "cloneStatus": "cloning",
                "cidxAware": True,
            },
            status_code=201,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(handlers_module, "app_module", mock_app_module_with_golden_repo)

            response = await handle_cs_register_repository(
                {"alias": "my-repo"},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert data["clone_status"] == "cloning"

        # Verify POST /repositories/register was called
        requests = httpx_mock.get_requests()
        register_requests = [
            r for r in requests if "/repositories/register" in str(r.url)
        ]
        assert len(register_requests) == 1

    @pytest.mark.asyncio
    async def test_claude_server_unreachable_returns_error(
        self,
        power_user,
        mock_delegation_config,
        mock_app_module_with_golden_repo,
        httpx_mock: HTTPXMock,
    ):
        """
        Claude Server unreachable returns a clear error.

        Given the Claude Server is unreachable (connection error)
        When cs_register_repository is called
        Then an error is returned with a clear message
        """
        import httpx as httpx_lib

        from code_indexer.server.mcp import handlers as handlers_module
        from code_indexer.server.mcp.handlers import handle_cs_register_repository

        httpx_mock.add_exception(
            httpx_lib.ConnectError("Connection refused"),
            method="POST",
            url="https://claude-server.example.com/auth/login",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )
            mp.setattr(handlers_module, "app_module", mock_app_module_with_golden_repo)

            response = await handle_cs_register_repository(
                {"alias": "my-repo"},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert len(data["error"]) > 0

    @pytest.mark.asyncio
    async def test_delegation_not_configured_returns_error(self, power_user):
        """
        Delegation not configured returns error.

        Given delegation is not configured
        When cs_register_repository is called
        Then an error is returned indicating not configured
        """
        from code_indexer.server.mcp.handlers import handle_cs_register_repository

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: None,
            )

            response = await handle_cs_register_repository(
                {"alias": "my-repo"},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "not configured" in data["error"].lower()


# ===========================================================================
# cs_list_repositories tests
# ===========================================================================


class TestCsListRepositories:
    """Tests for handle_cs_list_repositories handler."""

    @pytest.mark.asyncio
    async def test_normal_user_is_denied(self, normal_user, mock_delegation_config):
        """Normal user receives Access denied error for cs_list_repositories."""
        from code_indexer.server.mcp.handlers import handle_cs_list_repositories

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_list_repositories(
                {},
                normal_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "denied" in data["error"].lower() or "access" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_returns_formatted_list_from_claude_server(
        self,
        power_user,
        mock_delegation_config,
        httpx_mock: HTTPXMock,
    ):
        """
        Returns formatted list from Claude Server.

        Given Claude Server has repositories registered
        When cs_list_repositories is called
        Then a list of repositories is returned with expected fields
        """
        from code_indexer.server.mcp.handlers import handle_cs_list_repositories

        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories",
            json=[
                {
                    "name": "repo-one",
                    "cloneStatus": "completed",
                    "cidxAware": True,
                    "gitUrl": "git@github.com:org/repo-one.git",
                    "branch": "main",
                    "currentBranch": "main",
                    "registeredAt": "2026-01-01T00:00:00Z",
                },
                {
                    "name": "repo-two",
                    "cloneStatus": "cloning",
                    "cidxAware": False,
                    "gitUrl": "git@github.com:org/repo-two.git",
                    "branch": "develop",
                    "currentBranch": "develop",
                    "registeredAt": "2026-01-02T00:00:00Z",
                },
            ],
            status_code=200,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_list_repositories(
                {},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert len(data["repositories"]) == 2
        assert data["repositories"][0]["name"] == "repo-one"
        assert data["repositories"][0]["clone_status"] == "completed"
        assert data["repositories"][1]["name"] == "repo-two"
        assert data["repositories"][1]["clone_status"] == "cloning"

    @pytest.mark.asyncio
    async def test_empty_list_when_no_repos_registered(
        self,
        power_user,
        mock_delegation_config,
        httpx_mock: HTTPXMock,
    ):
        """
        Empty list returned when no repositories are registered.

        Given Claude Server has no repositories
        When cs_list_repositories is called
        Then an empty list is returned
        """
        from code_indexer.server.mcp.handlers import handle_cs_list_repositories

        httpx_mock.add_response(
            method="POST",
            url="https://claude-server.example.com/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/repositories",
            json=[],
            status_code=200,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_list_repositories(
                {},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert data["repositories"] == []

    @pytest.mark.asyncio
    async def test_delegation_not_configured_returns_error(self, power_user):
        """
        Delegation not configured returns error.

        Given delegation is not configured
        When cs_list_repositories is called
        Then an error is returned indicating not configured
        """
        from code_indexer.server.mcp.handlers import handle_cs_list_repositories

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: None,
            )

            response = await handle_cs_list_repositories(
                {},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "not configured" in data["error"].lower()


# ===========================================================================
# cs_check_health tests
# ===========================================================================


class TestCsCheckHealth:
    """Tests for handle_cs_check_health handler."""

    @pytest.mark.asyncio
    async def test_normal_user_is_denied(self, normal_user, mock_delegation_config):
        """Normal user receives Access denied error for cs_check_health."""
        from code_indexer.server.mcp.handlers import handle_cs_check_health

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_check_health(
                {},
                normal_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "denied" in data["error"].lower() or "access" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_returns_health_data_when_server_healthy(
        self,
        power_user,
        mock_delegation_config,
        httpx_mock: HTTPXMock,
    ):
        """
        Returns health data when Claude Server is healthy.

        Given Claude Server is reachable and healthy
        When cs_check_health is called
        Then health data is returned including status and component checks
        """
        from code_indexer.server.mcp.handlers import handle_cs_check_health

        httpx_mock.add_response(
            method="GET",
            url="https://claude-server.example.com/health",
            json={
                "status": "healthy",
                "nodeId": "node-001",
                "version": "2.0.0",
                "checks": {
                    "database": "ok",
                    "storage": "ok",
                    "queueService": "ok",
                },
                "metrics": {
                    "queueDepth": 0,
                    "runningJobs": 2,
                },
            },
            status_code=200,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_check_health(
                {},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert data["health"]["status"] == "healthy"
        assert data["health"]["nodeId"] == "node-001"

    @pytest.mark.asyncio
    async def test_returns_error_when_server_unreachable(
        self,
        power_user,
        mock_delegation_config,
        httpx_mock: HTTPXMock,
    ):
        """
        Returns clear error when Claude Server is unreachable.

        Given the Claude Server is unreachable
        When cs_check_health is called
        Then an error is returned with a clear message
        """
        import httpx as httpx_lib

        from code_indexer.server.mcp.handlers import handle_cs_check_health

        httpx_mock.add_exception(
            httpx_lib.ConnectError("Connection refused"),
            method="GET",
            url="https://claude-server.example.com/health",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_cs_check_health(
                {},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert len(data["error"]) > 0

    @pytest.mark.asyncio
    async def test_delegation_not_configured_returns_error(self, power_user):
        """
        Delegation not configured returns error.

        Given delegation is not configured
        When cs_check_health is called
        Then an error is returned indicating not configured
        """
        from code_indexer.server.mcp.handlers import handle_cs_check_health

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: None,
            )

            response = await handle_cs_check_health(
                {},
                power_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "not configured" in data["error"].lower()


# ===========================================================================
# Backward compatibility tests
# ===========================================================================


class TestBackwardCompatibility:
    """Tests for backward compatibility with existing handlers and client methods."""

    def test_existing_delegation_handlers_still_registered(self):
        """
        Existing delegation handlers remain registered.

        Given the handlers module is loaded
        When HANDLER_REGISTRY is inspected
        Then execute_delegation_function and execute_open_delegation are present
        """
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "execute_delegation_function" in HANDLER_REGISTRY
        assert "execute_open_delegation" in HANDLER_REGISTRY
        assert "poll_delegation_job" in HANDLER_REGISTRY

    def test_new_proxy_tools_registered(self):
        """
        New proxy tools are registered in HANDLER_REGISTRY.

        Given the handlers module is loaded
        When HANDLER_REGISTRY is inspected
        Then all 3 new proxy tools are present
        """
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "cs_register_repository" in HANDLER_REGISTRY
        assert "cs_list_repositories" in HANDLER_REGISTRY
        assert "cs_check_health" in HANDLER_REGISTRY

    def test_existing_claude_server_client_methods_unchanged(self):
        """
        Existing ClaudeServerClient methods are unchanged.

        Given the ClaudeServerClient class
        When inspecting its methods
        Then check_repository_exists, register_repository, create_job_with_options,
        get_repo_status, wait_for_repo_ready are all present with correct signatures
        """
        import inspect

        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        assert hasattr(ClaudeServerClient, "check_repository_exists")
        assert hasattr(ClaudeServerClient, "register_repository")
        assert hasattr(ClaudeServerClient, "create_job_with_options")
        assert hasattr(ClaudeServerClient, "get_repo_status")
        assert hasattr(ClaudeServerClient, "wait_for_repo_ready")

        # Verify register_repository signature: alias, remote, branch
        sig = inspect.signature(ClaudeServerClient.register_repository)
        params = list(sig.parameters.keys())
        assert "alias" in params
        assert "remote" in params
        assert "branch" in params

    def test_new_claude_server_client_methods_present(self):
        """
        New ClaudeServerClient methods are present.

        Given the ClaudeServerClient class
        When inspecting its methods
        Then list_repositories and get_health are present
        """
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        assert hasattr(ClaudeServerClient, "list_repositories")
        assert hasattr(ClaudeServerClient, "get_health")
