"""
Test suite for Bug #138 - Missing await in handle_ci_get_job_logs.

This test suite verifies that the handle_ci_get_job_logs unified handler
properly awaits the async GitLabCIClient.get_job_logs() method and returns
serializable data instead of a coroutine object.

Story #991: Migrated from handle_gitlab_ci_get_job_logs to handle_ci_get_job_logs.
Foundation #1 Compliant: Uses real async execution with minimal mocking.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import json

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import handle_ci_get_job_logs


# Repo dict returned by _get_global_repo for GitLab alias resolution
_GITLAB_REPO_DICT = {
    "repo_url": "https://gitlab.com/myorg/myproject.git",
    "alias": "myproject-global",
    "branch": "main",
}

# Resolved 5-tuple for GitLab forge (forge_type, project_identifier, base_url, forge_host, error)
_GITLAB_RESOLVED = (
    "gitlab",
    "myorg/myproject",
    "https://gitlab.com",
    "gitlab.com",
    None,
)


class TestCIGetJobLogsAsyncBug:
    """Test that handle_ci_get_job_logs properly awaits async call."""

    @pytest.fixture
    def mock_user(self):
        """Create a mock user for testing."""
        user = MagicMock(spec=User)
        user.username = "testuser"
        user.role = UserRole.ADMIN
        return user

    @pytest.fixture
    def bypass_alias_resolution(self):
        """Bypass _resolve_repo_alias_for_cicd and access control for async bug tests."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.cicd._resolve_repo_alias_for_cicd",
                return_value=_GITLAB_RESOLVED,
            ),
            patch(
                "code_indexer.server.mcp.handlers.cicd._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers.cicd._resolve_cicd_read_token",
                return_value="test_gitlab_token_123",
            ),
        ):
            yield

    @pytest.mark.asyncio
    async def test_handle_ci_get_job_logs_awaits_async_call(
        self, mock_user, bypass_alias_resolution
    ):
        """
        Test that handler properly awaits async get_job_logs() method.

        This test verifies:
        1. Handler is async (can be awaited)
        2. Handler properly awaits client.get_job_logs()
        3. Response is serializable (not a coroutine)
        4. Response contains expected log data

        Bug #138: Without 'await', client.get_job_logs() returns a coroutine
        object that cannot be JSON serialized, causing TypeError.
        """
        # Arrange
        test_logs = "2024-01-01 10:00:00 Starting job...\n2024-01-01 10:00:05 Job completed successfully"
        test_repository_alias = "myproject-global"
        test_job_id = 12345

        args = {
            "repository_alias": test_repository_alias,
            "job_id": test_job_id,
        }

        # Mock GitLabCIClient with async get_job_logs method
        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            mock_client_instance = MagicMock()
            # CRITICAL: get_job_logs must be an AsyncMock since it's async in real client
            mock_client_instance.get_job_logs = AsyncMock(return_value=test_logs)
            # Mock last_rate_limit attribute that handler uses
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act
            # CRITICAL: This will fail if handler is not async or doesn't await
            response = await handle_ci_get_job_logs(args, mock_user)

            # Assert
            # 1. Verify response is a dict (not a coroutine)
            assert isinstance(response, dict), (
                f"Expected dict response, got {type(response)}"
            )

            # 2. Verify response is JSON serializable (would fail if coroutine)
            try:
                json.dumps(response)
            except TypeError as e:
                pytest.fail(f"Response not JSON serializable: {e}")

            # 3. Verify response structure
            assert "content" in response, "Response missing 'content' field"
            content = response["content"]
            assert len(content) > 0, "Response content is empty"

            # 4. Verify success and log data (parse JSON from MCP response)
            first_item = content[0]
            assert first_item["type"] == "text", "Response type should be 'text'"
            data = json.loads(first_item["text"])
            assert data["success"] is True, "Response indicates failure"
            assert data["logs"] == test_logs, "Logs content mismatch"

            # 5. Verify client.get_job_logs was called with correct args
            mock_client_instance.get_job_logs.assert_awaited_once_with(
                project_id="myorg/myproject", job_id=test_job_id
            )

    @pytest.mark.asyncio
    async def test_handle_ci_get_job_logs_is_awaitable(
        self, mock_user, bypass_alias_resolution
    ):
        """
        Test that handler function is actually async (awaitable).

        Bug #138: Handler was 'def' instead of 'async def', making it
        impossible to properly await the async client call.
        """
        # Arrange
        args = {"repository_alias": "myproject-global", "job_id": 123}

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.get_job_logs = AsyncMock(return_value="test logs")
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act - Try to await the handler
            try:
                result = await handle_ci_get_job_logs(args, mock_user)
                # If we get here, handler is properly async
                assert result is not None
            except TypeError as e:
                # This will happen if handler is not async
                pytest.fail(
                    f"Handler is not async (cannot be awaited): {e}\n"
                    "Expected 'async def handle_ci_get_job_logs' but got 'def'"
                )

    @pytest.mark.asyncio
    async def test_response_does_not_contain_coroutine_object(
        self, mock_user, bypass_alias_resolution
    ):
        """
        Test that response doesn't contain any coroutine objects.

        Bug #138: Without 'await', the response would contain:
        {'logs': <coroutine object GitLabCIClient.get_job_logs at 0x...>}
        which cannot be JSON serialized.
        """
        # Arrange
        args = {"repository_alias": "myproject-global", "job_id": 123}

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.get_job_logs = AsyncMock(return_value="test logs")
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act
            response = await handle_ci_get_job_logs(args, mock_user)

            # Assert - Check that no part of response is a coroutine
            def check_for_coroutines(obj, path="response"):
                """Recursively check for coroutine objects in data structure."""
                import inspect

                if inspect.iscoroutine(obj):
                    pytest.fail(
                        f"Found coroutine object at {path}: {obj}\n"
                        "This indicates missing 'await' in async call"
                    )
                elif isinstance(obj, dict):
                    for key, value in obj.items():
                        check_for_coroutines(value, f"{path}['{key}']")
                elif isinstance(obj, (list, tuple)):
                    for i, value in enumerate(obj):
                        check_for_coroutines(value, f"{path}[{i}]")

            check_for_coroutines(response)
