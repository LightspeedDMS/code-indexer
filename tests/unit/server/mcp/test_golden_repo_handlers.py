"""Unit tests for golden repository MCP handlers parameter mapping and audit trail."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from code_indexer.server.mcp.handlers import (
    add_golden_repo,
    remove_golden_repo,
    refresh_golden_repo,
)
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user for testing."""
    user = Mock(spec=User)
    user.username = "admin"
    user.role = UserRole.ADMIN
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_regular_user():
    """Create a mock regular user for testing."""
    user = Mock(spec=User)
    user.username = "alice"
    user.role = UserRole.USER
    user.has_permission = Mock(return_value=False)
    return user


class TestAddGoldenRepoHandler:
    """Test add_golden_repo handler parameter mapping and audit trail."""

    def test_handler_passes_actual_username(self, mock_admin_user):
        """Test that handler passes actual user username (not hardcoded 'admin')."""
        mcp_params = {
            "url": "https://github.com/user/repo.git",
            "alias": "my-golden-repo",
            "branch": "develop",
        }

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.add_golden_repo = Mock(return_value="test-job-id-12345")

            result = add_golden_repo(mcp_params, mock_admin_user)

            # Verify the handler called the manager with actual username
            mock_manager.add_golden_repo.assert_called_once_with(
                repo_url="https://github.com/user/repo.git",
                alias="my-golden-repo",
                default_branch="develop",
                enable_temporal=False,
                temporal_options=None,
                submitter_username="admin",  # Actual username from user object
            )

            # Verify success response
            assert result["content"][0]["type"] == "text"
            import json

            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True
            assert response_data["job_id"] == "test-job-id-12345"

    def test_handler_passes_different_username(self):
        """Test that handler passes different usernames correctly."""
        # Create user with different username
        user = Mock(spec=User)
        user.username = "bob"
        user.role = UserRole.ADMIN

        mcp_params = {
            "url": "https://github.com/user/repo.git",
            "alias": "my-golden-repo",
            "branch": "main",
        }

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.add_golden_repo = Mock(return_value="job-123")

            add_golden_repo(mcp_params, user)

            # Verify correct username passed
            call_kwargs = mock_manager.add_golden_repo.call_args[1]
            assert call_kwargs["submitter_username"] == "bob"


class TestRemoveGoldenRepoHandler:
    """Test remove_golden_repo handler parameter mapping and audit trail."""

    def test_handler_passes_actual_username(self, mock_admin_user):
        """Test that handler passes actual user username to remove operation."""
        mcp_params = {"alias": "test-repo"}

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.remove_golden_repo = Mock(return_value="test-job-id-67890")

            result = remove_golden_repo(mcp_params, mock_admin_user)

            # Verify the handler called the manager with actual username
            mock_manager.remove_golden_repo.assert_called_once_with(
                "test-repo",
                submitter_username="admin",  # Actual username from user object
            )

            # Verify success response
            assert result["content"][0]["type"] == "text"
            import json

            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True
            assert response_data["job_id"] == "test-job-id-67890"

    def test_handler_passes_different_username(self):
        """Test that handler passes different usernames correctly."""
        # Create user with different username
        user = Mock(spec=User)
        user.username = "charlie"
        user.role = UserRole.ADMIN

        mcp_params = {"alias": "test-repo"}

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.remove_golden_repo = Mock(return_value="job-456")

            remove_golden_repo(mcp_params, user)

            # Verify correct username passed
            mock_manager.remove_golden_repo.assert_called_once_with(
                "test-repo",
                submitter_username="charlie",  # Username from charlie's user object
            )


class TestRefreshGoldenRepoHandler:
    """Test refresh_golden_repo handler response shape and error handling."""

    def test_handler_returns_success_response_shape(self, mock_admin_user):
        """Test that a successful refresh returns the expected MCP response structure."""
        mcp_params = {"alias": "test-repo"}

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_refresh_for_repo = MagicMock(return_value="test-job-id-11111")

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.golden_repos = {"test-repo": MagicMock()}
            with patch(
                "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
                return_value=mock_scheduler,
            ):
                result = refresh_golden_repo(mcp_params, mock_admin_user)

        assert result["content"][0]["type"] == "text"
        response_data = json.loads(result["content"][0]["text"])
        assert response_data["success"] is True
        assert response_data["job_id"] == "test-job-id-11111"
        assert "test-repo" in response_data["message"]

    def test_handler_returns_error_for_unknown_repo(self, mock_admin_user):
        """Test that an unknown alias results in success=False with job_id=None."""
        mcp_params = {"alias": "bad-repo"}

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.golden_repos = {}  # repo not found

            result = refresh_golden_repo(mcp_params, mock_admin_user)

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["success"] is False
        assert response_data["job_id"] is None


class TestRefreshGoldenRepoHandlerViaScheduler:
    """Tests for the new behavior where refresh_golden_repo delegates to RefreshScheduler.

    These tests verify the redirected refresh path that uses the versioned
    CoW snapshot pipeline (RefreshScheduler._execute_refresh) instead of the old
    GoldenRepoManager.refresh_golden_repo() method.
    """

    @pytest.fixture
    def mock_admin_user(self):
        """Create a mock admin user for testing."""
        user = Mock(spec=User)
        user.username = "admin"
        user.role = UserRole.ADMIN
        user.has_permission = Mock(return_value=True)
        return user

    def test_refresh_delegates_to_refresh_scheduler(self, mock_admin_user):
        """Test that refresh_golden_repo calls RefreshScheduler, not GoldenRepoManager."""
        mcp_params = {"alias": "my-repo"}

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_refresh_for_repo = MagicMock(return_value="sched-job-001")

        mock_golden_repos = {"my-repo": MagicMock()}

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.golden_repos = mock_golden_repos
            with patch(
                "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
                return_value=mock_scheduler,
            ):
                result = refresh_golden_repo(mcp_params, mock_admin_user)

        # RefreshScheduler must be called with bare alias and submitter_username
        mock_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "my-repo", submitter_username="admin"
        )

        # GoldenRepoManager.refresh_golden_repo must NOT be called
        mock_manager.refresh_golden_repo.assert_not_called()

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["success"] is True
        assert response_data["job_id"] == "sched-job-001"
        assert "my-repo" in response_data["message"]

    def test_refresh_rejects_unknown_alias(self, mock_admin_user):
        """Test that refresh rejects unknown alias before calling RefreshScheduler."""
        mcp_params = {"alias": "nonexistent-repo"}

        mock_scheduler = MagicMock()

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.golden_repos = {}  # Empty - repo not registered
            with patch(
                "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
                return_value=mock_scheduler,
            ):
                result = refresh_golden_repo(mcp_params, mock_admin_user)

        # Scheduler must NOT be called when alias is invalid
        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["success"] is False
        assert "nonexistent-repo" in response_data["error"]

    def test_refresh_fails_when_scheduler_unavailable(self, mock_admin_user):
        """Test that refresh returns error when RefreshScheduler is not available."""
        mcp_params = {"alias": "my-repo"}

        mock_golden_repos = {"my-repo": MagicMock()}

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.golden_repos = mock_golden_repos
            with patch(
                "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
                return_value=None,  # Scheduler not available
            ):
                result = refresh_golden_repo(mcp_params, mock_admin_user)

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["success"] is False
        assert "RefreshScheduler" in response_data["error"]

    def test_refresh_uses_global_alias_convention(self, mock_admin_user):
        """Test that the bare alias is converted to alias-global for RefreshScheduler."""
        mcp_params = {"alias": "code-indexer"}

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_refresh_for_repo = MagicMock(return_value="job-xyz")

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.golden_repos = {"code-indexer": MagicMock()}
            with patch(
                "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
                return_value=mock_scheduler,
            ):
                refresh_golden_repo(mcp_params, mock_admin_user)

        # Verify bare alias is passed to scheduler (resolution happens inside scheduler)
        call_args = mock_scheduler.trigger_refresh_for_repo.call_args
        assert call_args[0][0] == "code-indexer"
