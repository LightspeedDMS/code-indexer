"""
Test suite for Bug #160 - Missing async/await in 5 GitLab CI handlers.

This test suite verifies that the following handlers properly await async
GitLabCIClient methods and return serializable data instead of coroutine objects:
1. handle_gitlab_ci_list_pipelines
2. handle_gitlab_ci_get_pipeline
3. handle_gitlab_ci_search_logs
4. handle_gitlab_ci_retry_pipeline
5. handle_gitlab_ci_cancel_pipeline

Foundation #1 Compliant: Uses real async execution with minimal mocking.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import json
import inspect

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import (
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_cancel_pipeline,
)


class TestGitLabCIHandlersAsyncBug160:
    """Test that all 5 GitLab CI handlers properly await async calls."""

    @pytest.fixture
    def mock_user(self):
        """Create a mock user for testing."""
        user = MagicMock(spec=User)
        user.username = "testuser"
        user.role = UserRole.ADMIN
        return user

    @pytest.fixture
    def mock_gitlab_token(self):
        """Mock the TokenAuthenticator.resolve_token to return a test token."""
        with patch(
            "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
            return_value="test_gitlab_token_123"
        ):
            yield

    # ==================== handle_gitlab_ci_list_pipelines ====================

    @pytest.mark.asyncio
    async def test_list_pipelines_awaits_async_call(self, mock_user, mock_gitlab_token):
        """
        Test that handle_gitlab_ci_list_pipelines properly awaits async list_pipelines().

        Bug #160: Without 'await', client.list_pipelines() returns a coroutine
        object that cannot be JSON serialized, causing TypeError.
        """
        # Arrange
        test_pipelines = [
            {"id": 1, "status": "success", "ref": "main"},
            {"id": 2, "status": "running", "ref": "develop"}
        ]
        test_project_id = "myorg/myproject"

        args = {
            "project_id": test_project_id,
            "ref": "main",
            "status": "success",
            "limit": 10,
        }

        # Mock GitLabCIClient with async list_pipelines method
        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.list_pipelines = AsyncMock(return_value=test_pipelines)
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act
            response = await handle_gitlab_ci_list_pipelines(args, mock_user)

            # Assert
            assert isinstance(response, dict), f"Expected dict response, got {type(response)}"

            # Verify response is JSON serializable
            try:
                json.dumps(response)
            except TypeError as e:
                pytest.fail(f"Response not JSON serializable: {e}")

            # Verify response structure
            assert "content" in response, "Response missing 'content' field"
            content = response["content"]
            assert len(content) > 0, "Response content is empty"

            # Verify success and pipeline data
            first_item = content[0]
            assert first_item["type"] == "text", "Response type should be 'text'"
            data = json.loads(first_item["text"])
            assert data["success"] is True, "Response indicates failure"
            assert data["pipelines"] == test_pipelines, "Pipelines content mismatch"

            # Verify client.list_pipelines was called with correct args
            mock_client_instance.list_pipelines.assert_awaited_once_with(
                project_id=test_project_id,
                ref="main",
                status="success"
            )

    @pytest.mark.asyncio
    async def test_list_pipelines_is_awaitable(self, mock_user, mock_gitlab_token):
        """Test that handle_gitlab_ci_list_pipelines is async (awaitable)."""
        args = {"project_id": "test/project"}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.list_pipelines = AsyncMock(return_value=[])
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            try:
                result = await handle_gitlab_ci_list_pipelines(args, mock_user)
                assert result is not None
            except TypeError as e:
                pytest.fail(
                    f"Handler is not async (cannot be awaited): {e}\n"
                    "Expected 'async def handle_gitlab_ci_list_pipelines' but got 'def'"
                )

    @pytest.mark.asyncio
    async def test_list_pipelines_no_coroutine_in_response(self, mock_user, mock_gitlab_token):
        """Test that list_pipelines response doesn't contain coroutine objects."""
        args = {"project_id": "test/project"}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.list_pipelines = AsyncMock(return_value=[])
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            response = await handle_gitlab_ci_list_pipelines(args, mock_user)
            self._check_for_coroutines(response)

    # ==================== handle_gitlab_ci_get_pipeline ====================

    @pytest.mark.asyncio
    async def test_get_pipeline_awaits_async_call(self, mock_user, mock_gitlab_token):
        """
        Test that handle_gitlab_ci_get_pipeline properly awaits async get_pipeline().

        Bug #160: Without 'await', client.get_pipeline() returns a coroutine object.
        """
        # Arrange
        test_pipeline_info = {
            "id": 123,
            "status": "success",
            "ref": "main",
            "jobs": [{"id": 1, "name": "test", "status": "success"}]
        }
        test_project_id = "myorg/myproject"
        test_pipeline_id = 123

        args = {
            "project_id": test_project_id,
            "pipeline_id": test_pipeline_id,
        }

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.get_pipeline = AsyncMock(return_value=test_pipeline_info)
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act
            response = await handle_gitlab_ci_get_pipeline(args, mock_user)

            # Assert
            assert isinstance(response, dict), f"Expected dict response, got {type(response)}"

            try:
                json.dumps(response)
            except TypeError as e:
                pytest.fail(f"Response not JSON serializable: {e}")

            content = response["content"]
            first_item = content[0]
            data = json.loads(first_item["text"])
            assert data["success"] is True, "Response indicates failure"
            assert data["pipeline"] == test_pipeline_info, "Pipeline info mismatch"

            mock_client_instance.get_pipeline.assert_awaited_once_with(
                project_id=test_project_id,
                pipeline_id=test_pipeline_id
            )

    @pytest.mark.asyncio
    async def test_get_pipeline_is_awaitable(self, mock_user, mock_gitlab_token):
        """Test that handle_gitlab_ci_get_pipeline is async (awaitable)."""
        args = {"project_id": "test/project", "pipeline_id": 123}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.get_pipeline = AsyncMock(return_value={})
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            try:
                result = await handle_gitlab_ci_get_pipeline(args, mock_user)
                assert result is not None
            except TypeError as e:
                pytest.fail(f"Handler is not async: {e}")

    @pytest.mark.asyncio
    async def test_get_pipeline_no_coroutine_in_response(self, mock_user, mock_gitlab_token):
        """Test that get_pipeline response doesn't contain coroutine objects."""
        args = {"project_id": "test/project", "pipeline_id": 123}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.get_pipeline = AsyncMock(return_value={})
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            response = await handle_gitlab_ci_get_pipeline(args, mock_user)
            self._check_for_coroutines(response)

    # ==================== handle_gitlab_ci_search_logs ====================

    @pytest.mark.asyncio
    async def test_search_logs_awaits_async_call(self, mock_user, mock_gitlab_token):
        """
        Test that handle_gitlab_ci_search_logs properly awaits async search_logs().

        Bug #160: Without 'await', client.search_logs() returns a coroutine object.
        """
        # Arrange
        test_matches = [
            {"job_id": 1, "job_name": "test", "line": "ERROR: test failed", "line_number": 42}
        ]
        test_project_id = "myorg/myproject"
        test_pipeline_id = 123
        test_pattern = "ERROR"

        args = {
            "project_id": test_project_id,
            "pipeline_id": test_pipeline_id,
            "pattern": test_pattern,
            "case_sensitive": True,
        }

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.search_logs = AsyncMock(return_value=test_matches)
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act
            response = await handle_gitlab_ci_search_logs(args, mock_user)

            # Assert
            assert isinstance(response, dict), f"Expected dict response, got {type(response)}"

            try:
                json.dumps(response)
            except TypeError as e:
                pytest.fail(f"Response not JSON serializable: {e}")

            content = response["content"]
            first_item = content[0]
            data = json.loads(first_item["text"])
            assert data["success"] is True, "Response indicates failure"
            assert data["matches"] == test_matches, "Matches content mismatch"

            mock_client_instance.search_logs.assert_awaited_once_with(
                project_id=test_project_id,
                pipeline_id=test_pipeline_id,
                pattern=test_pattern,
                case_sensitive=True
            )

    @pytest.mark.asyncio
    async def test_search_logs_is_awaitable(self, mock_user, mock_gitlab_token):
        """Test that handle_gitlab_ci_search_logs is async (awaitable)."""
        args = {"project_id": "test/project", "pipeline_id": 123, "pattern": "ERROR"}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.search_logs = AsyncMock(return_value=[])
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            try:
                result = await handle_gitlab_ci_search_logs(args, mock_user)
                assert result is not None
            except TypeError as e:
                pytest.fail(f"Handler is not async: {e}")

    @pytest.mark.asyncio
    async def test_search_logs_no_coroutine_in_response(self, mock_user, mock_gitlab_token):
        """Test that search_logs response doesn't contain coroutine objects."""
        args = {"project_id": "test/project", "pipeline_id": 123, "pattern": "ERROR"}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.search_logs = AsyncMock(return_value=[])
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            response = await handle_gitlab_ci_search_logs(args, mock_user)
            self._check_for_coroutines(response)

    # ==================== handle_gitlab_ci_retry_pipeline ====================

    @pytest.mark.asyncio
    async def test_retry_pipeline_awaits_async_call(self, mock_user, mock_gitlab_token):
        """
        Test that handle_gitlab_ci_retry_pipeline properly awaits async retry_pipeline().

        Bug #160: Without 'await', client.retry_pipeline() returns a coroutine object.
        """
        # Arrange
        test_result = {"id": 123, "status": "pending"}
        test_project_id = "myorg/myproject"
        test_pipeline_id = 123

        args = {
            "project_id": test_project_id,
            "pipeline_id": test_pipeline_id,
        }

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.retry_pipeline = AsyncMock(return_value=test_result)
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act
            response = await handle_gitlab_ci_retry_pipeline(args, mock_user)

            # Assert
            assert isinstance(response, dict), f"Expected dict response, got {type(response)}"

            try:
                json.dumps(response)
            except TypeError as e:
                pytest.fail(f"Response not JSON serializable: {e}")

            content = response["content"]
            first_item = content[0]
            data = json.loads(first_item["text"])
            assert data["success"] is True, "Response indicates failure"
            assert data["result"] == test_result, "Result content mismatch"

            mock_client_instance.retry_pipeline.assert_awaited_once_with(
                project_id=test_project_id,
                pipeline_id=test_pipeline_id
            )

    @pytest.mark.asyncio
    async def test_retry_pipeline_is_awaitable(self, mock_user, mock_gitlab_token):
        """Test that handle_gitlab_ci_retry_pipeline is async (awaitable)."""
        args = {"project_id": "test/project", "pipeline_id": 123}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.retry_pipeline = AsyncMock(return_value={})
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            try:
                result = await handle_gitlab_ci_retry_pipeline(args, mock_user)
                assert result is not None
            except TypeError as e:
                pytest.fail(f"Handler is not async: {e}")

    @pytest.mark.asyncio
    async def test_retry_pipeline_no_coroutine_in_response(self, mock_user, mock_gitlab_token):
        """Test that retry_pipeline response doesn't contain coroutine objects."""
        args = {"project_id": "test/project", "pipeline_id": 123}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.retry_pipeline = AsyncMock(return_value={})
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            response = await handle_gitlab_ci_retry_pipeline(args, mock_user)
            self._check_for_coroutines(response)

    # ==================== handle_gitlab_ci_cancel_pipeline ====================

    @pytest.mark.asyncio
    async def test_cancel_pipeline_awaits_async_call(self, mock_user, mock_gitlab_token):
        """
        Test that handle_gitlab_ci_cancel_pipeline properly awaits async cancel_pipeline().

        Bug #160: Without 'await', client.cancel_pipeline() returns a coroutine object.
        """
        # Arrange
        test_result = {"id": 123, "status": "canceled"}
        test_project_id = "myorg/myproject"
        test_pipeline_id = 123

        args = {
            "project_id": test_project_id,
            "pipeline_id": test_pipeline_id,
        }

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.cancel_pipeline = AsyncMock(return_value=test_result)
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            # Act
            response = await handle_gitlab_ci_cancel_pipeline(args, mock_user)

            # Assert
            assert isinstance(response, dict), f"Expected dict response, got {type(response)}"

            try:
                json.dumps(response)
            except TypeError as e:
                pytest.fail(f"Response not JSON serializable: {e}")

            content = response["content"]
            first_item = content[0]
            data = json.loads(first_item["text"])
            assert data["success"] is True, "Response indicates failure"
            assert data["result"] == test_result, "Result content mismatch"

            mock_client_instance.cancel_pipeline.assert_awaited_once_with(
                project_id=test_project_id,
                pipeline_id=test_pipeline_id
            )

    @pytest.mark.asyncio
    async def test_cancel_pipeline_is_awaitable(self, mock_user, mock_gitlab_token):
        """Test that handle_gitlab_ci_cancel_pipeline is async (awaitable)."""
        args = {"project_id": "test/project", "pipeline_id": 123}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.cancel_pipeline = AsyncMock(return_value={})
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            try:
                result = await handle_gitlab_ci_cancel_pipeline(args, mock_user)
                assert result is not None
            except TypeError as e:
                pytest.fail(f"Handler is not async: {e}")

    @pytest.mark.asyncio
    async def test_cancel_pipeline_no_coroutine_in_response(self, mock_user, mock_gitlab_token):
        """Test that cancel_pipeline response doesn't contain coroutine objects."""
        args = {"project_id": "test/project", "pipeline_id": 123}

        with patch("code_indexer.server.clients.gitlab_ci_client.GitLabCIClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.cancel_pipeline = AsyncMock(return_value={})
            mock_client_instance.last_rate_limit = None
            MockClient.return_value = mock_client_instance

            response = await handle_gitlab_ci_cancel_pipeline(args, mock_user)
            self._check_for_coroutines(response)

    # ==================== Helper Methods ====================

    def _check_for_coroutines(self, obj, path="response"):
        """Recursively check for coroutine objects in data structure."""
        if inspect.iscoroutine(obj):
            pytest.fail(
                f"Found coroutine object at {path}: {obj}\n"
                "This indicates missing 'await' in async call"
            )
        elif isinstance(obj, dict):
            for key, value in obj.items():
                self._check_for_coroutines(value, f"{path}['{key}']")
        elif isinstance(obj, (list, tuple)):
            for i, value in enumerate(obj):
                self._check_for_coroutines(value, f"{path}[{i}]")
