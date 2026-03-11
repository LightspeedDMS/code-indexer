"""
Test suite for Story #403 - Fix MCP Parameter Name Mismatches (P0: base_url).

Verifies that all 6 GitLab CI handlers read base_url from args and pass it
to GitLabCIClient constructor.

Handlers under test:
1. handle_gitlab_ci_list_pipelines
2. handle_gitlab_ci_get_pipeline
3. handle_gitlab_ci_search_logs
4. handle_gitlab_ci_get_job_logs
5. handle_gitlab_ci_retry_pipeline
6. handle_gitlab_ci_cancel_pipeline

Foundation #1 Compliant: Uses real handler execution with minimal mocking.
Only GitLabCIClient is mocked (requires real network / GitLab token).
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import (
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_cancel_pipeline,
)

CUSTOM_BASE_URL = "https://gitlab.mycompany.com"
DEFAULT_BASE_URL = "https://gitlab.com"
TEST_TOKEN = "test_gitlab_token_abc123"


def _parse_mcp_response(response: dict) -> dict:
    """Extract the inner data dict from the MCP response envelope.

    Handlers return: {"content": [{"type": "text", "text": "<json-string>"}]}
    This helper decodes the inner JSON so tests can assert on data fields.
    """
    content = response.get("content", [])
    assert len(content) > 0, f"Empty MCP response content: {response}"
    return json.loads(content[0]["text"])


@pytest.fixture
def mock_user():
    """Create a mock authenticated user."""
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.ADMIN
    return user


@pytest.fixture
def mock_gitlab_token():
    """Mock TokenAuthenticator to return a test token without env var."""
    with patch(
        "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
        return_value=TEST_TOKEN,
    ):
        yield


def _make_mock_client(return_values: dict):
    """
    Build a GitLabCIClient mock with async methods returning given values.

    Args:
        return_values: mapping of method_name -> return_value
    """
    mock_instance = MagicMock()
    mock_instance.last_rate_limit = None
    for method_name, return_value in return_values.items():
        setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
    return mock_instance


# =============================================================================
# handle_gitlab_ci_list_pipelines
# =============================================================================


class TestListPipelinesBaseUrl:
    """Tests that handle_gitlab_ci_list_pipelines passes base_url to client."""

    @pytest.mark.asyncio
    async def test_custom_base_url_passed_to_client(self, mock_user, mock_gitlab_token):
        """Handler must pass custom base_url to GitLabCIClient constructor."""
        pipelines = [{"id": 1, "status": "success", "ref": "main"}]
        mock_instance = _make_mock_client({"list_pipelines": pipelines})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_list_pipelines(
                {"project_id": "org/repo", "base_url": CUSTOM_BASE_URL},
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=CUSTOM_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True

    @pytest.mark.asyncio
    async def test_default_base_url_when_not_provided(self, mock_user, mock_gitlab_token):
        """Handler must use default base_url when not in args."""
        pipelines: list = []
        mock_instance = _make_mock_client({"list_pipelines": pipelines})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_list_pipelines(
                {"project_id": "org/repo"},
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=DEFAULT_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True


# =============================================================================
# handle_gitlab_ci_get_pipeline
# =============================================================================


class TestGetPipelineBaseUrl:
    """Tests that handle_gitlab_ci_get_pipeline passes base_url to client."""

    @pytest.mark.asyncio
    async def test_custom_base_url_passed_to_client(self, mock_user, mock_gitlab_token):
        """Handler must pass custom base_url to GitLabCIClient constructor."""
        pipeline_info = {"id": 42, "status": "running", "ref": "main"}
        mock_instance = _make_mock_client({"get_pipeline": pipeline_info})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_get_pipeline(
                {
                    "project_id": "org/repo",
                    "pipeline_id": 42,
                    "base_url": CUSTOM_BASE_URL,
                },
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=CUSTOM_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True

    @pytest.mark.asyncio
    async def test_default_base_url_when_not_provided(self, mock_user, mock_gitlab_token):
        """Handler must use default base_url when not in args."""
        pipeline_info = {"id": 42, "status": "success"}
        mock_instance = _make_mock_client({"get_pipeline": pipeline_info})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_get_pipeline(
                {"project_id": "org/repo", "pipeline_id": 42},
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=DEFAULT_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True


# =============================================================================
# handle_gitlab_ci_search_logs
# =============================================================================


class TestSearchLogsBaseUrl:
    """Tests that handle_gitlab_ci_search_logs passes base_url to client."""

    @pytest.mark.asyncio
    async def test_custom_base_url_passed_to_client(self, mock_user, mock_gitlab_token):
        """Handler must pass custom base_url to GitLabCIClient constructor."""
        matches = [{"job_id": 1, "line": "ERROR: test failed", "line_number": 42}]
        mock_instance = _make_mock_client({"search_logs": matches})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_search_logs(
                {
                    "project_id": "org/repo",
                    "pipeline_id": 100,
                    "pattern": "ERROR",
                    "base_url": CUSTOM_BASE_URL,
                },
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=CUSTOM_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True

    @pytest.mark.asyncio
    async def test_default_base_url_when_not_provided(self, mock_user, mock_gitlab_token):
        """Handler must use default base_url when not in args."""
        matches: list = []
        mock_instance = _make_mock_client({"search_logs": matches})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_search_logs(
                {"project_id": "org/repo", "pipeline_id": 100, "pattern": "ERROR"},
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=DEFAULT_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True


# =============================================================================
# handle_gitlab_ci_get_job_logs
# =============================================================================


class TestGetJobLogsBaseUrl:
    """Tests that handle_gitlab_ci_get_job_logs passes base_url to client."""

    @pytest.mark.asyncio
    async def test_custom_base_url_passed_to_client(self, mock_user, mock_gitlab_token):
        """Handler must pass custom base_url to GitLabCIClient constructor."""
        log_content = "Running tests...\nAll passed."
        mock_instance = _make_mock_client({"get_job_logs": log_content})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_get_job_logs(
                {
                    "project_id": "org/repo",
                    "job_id": 999,
                    "base_url": CUSTOM_BASE_URL,
                },
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=CUSTOM_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True

    @pytest.mark.asyncio
    async def test_default_base_url_when_not_provided(self, mock_user, mock_gitlab_token):
        """Handler must use default base_url when not in args."""
        log_content = "Build successful."
        mock_instance = _make_mock_client({"get_job_logs": log_content})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_get_job_logs(
                {"project_id": "org/repo", "job_id": 999},
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=DEFAULT_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True


# =============================================================================
# handle_gitlab_ci_retry_pipeline
# =============================================================================


class TestRetryPipelineBaseUrl:
    """Tests that handle_gitlab_ci_retry_pipeline passes base_url to client."""

    @pytest.mark.asyncio
    async def test_custom_base_url_passed_to_client(self, mock_user, mock_gitlab_token):
        """Handler must pass custom base_url to GitLabCIClient constructor."""
        retry_result = {"id": 201, "status": "pending"}
        mock_instance = _make_mock_client({"retry_pipeline": retry_result})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_retry_pipeline(
                {
                    "project_id": "org/repo",
                    "pipeline_id": 200,
                    "base_url": CUSTOM_BASE_URL,
                },
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=CUSTOM_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True

    @pytest.mark.asyncio
    async def test_default_base_url_when_not_provided(self, mock_user, mock_gitlab_token):
        """Handler must use default base_url when not in args."""
        retry_result = {"id": 201, "status": "pending"}
        mock_instance = _make_mock_client({"retry_pipeline": retry_result})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_retry_pipeline(
                {"project_id": "org/repo", "pipeline_id": 200},
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=DEFAULT_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True


# =============================================================================
# handle_gitlab_ci_cancel_pipeline
# =============================================================================


class TestCancelPipelineBaseUrl:
    """Tests that handle_gitlab_ci_cancel_pipeline passes base_url to client."""

    @pytest.mark.asyncio
    async def test_custom_base_url_passed_to_client(self, mock_user, mock_gitlab_token):
        """Handler must pass custom base_url to GitLabCIClient constructor."""
        cancel_result = {"id": 300, "status": "canceled"}
        mock_instance = _make_mock_client({"cancel_pipeline": cancel_result})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_cancel_pipeline(
                {
                    "project_id": "org/repo",
                    "pipeline_id": 300,
                    "base_url": CUSTOM_BASE_URL,
                },
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=CUSTOM_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True

    @pytest.mark.asyncio
    async def test_default_base_url_when_not_provided(self, mock_user, mock_gitlab_token):
        """Handler must use default base_url when not in args."""
        cancel_result = {"id": 300, "status": "canceled"}
        mock_instance = _make_mock_client({"cancel_pipeline": cancel_result})

        with patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
        ) as MockClient:
            MockClient.return_value = mock_instance

            result = await handle_gitlab_ci_cancel_pipeline(
                {"project_id": "org/repo", "pipeline_id": 300},
                mock_user,
            )

        MockClient.assert_called_once_with(TEST_TOKEN, base_url=DEFAULT_BASE_URL)
        assert _parse_mcp_response(result)["success"] is True
