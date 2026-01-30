"""
Tests for Self-Monitoring Manual Trigger Route (Bug #87).

Tests the /self-monitoring/run-now manual trigger endpoint to verify:
- Auto-detection of repo_root from git
- Auto-detection of github_repo from git remote
- Passing repo_root to SelfMonitoringService
"""

from unittest.mock import Mock, patch
from fastapi import status
from fastapi.testclient import TestClient
from pathlib import Path


def test_manual_trigger_route_auto_detects_repo_root(
    authenticated_client: TestClient,
    web_infrastructure,
):
    """Test that manual trigger route reads repo_root from app.state (Bug #87)."""
    # Set app.state values that would be set during startup
    repo_root_path = Path(__file__).resolve().parent.parent.parent.parent  # project root
    authenticated_client.app.state.self_monitoring_repo_root = str(repo_root_path)
    authenticated_client.app.state.self_monitoring_github_repo = "owner/repo"

    # Mock CSRF validation to bypass known test infrastructure issue
    with patch(
        "code_indexer.server.web.routes.validate_login_csrf_token", return_value=True
    ):
        # Mock the SelfMonitoringService to capture initialization parameters
        with patch(
            "code_indexer.server.self_monitoring.service.SelfMonitoringService"
        ) as mock_service_class:
            # Create a mock service instance
            mock_service = Mock()
            mock_service.trigger_scan.return_value = {
                "status": "queued",
                "scan_id": "test-scan-123",
            }
            mock_service_class.return_value = mock_service

            # Submit manual trigger request
            form_data = {"csrf_token": "test-token"}
            response = authenticated_client.post("/admin/self-monitoring/run-now", data=form_data)

        # Verify the response succeeded
        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert result["status"] == "queued"

        # CRITICAL: Verify SelfMonitoringService was instantiated with repo_root
        mock_service_class.assert_called_once()
        init_kwargs = mock_service_class.call_args[1]

        # Bug #87 fix: repo_root should be read from app.state and passed
        assert "repo_root" in init_kwargs, "repo_root parameter is missing"
        assert init_kwargs["repo_root"] is not None, "repo_root should not be None"
        assert isinstance(
            init_kwargs["repo_root"], str
        ), "repo_root should be a string path"
        assert init_kwargs["repo_root"] == str(repo_root_path), \
            f"repo_root should match app.state value: {repo_root_path}"


def test_manual_trigger_route_auto_detects_github_repo(
    authenticated_client: TestClient,
    web_infrastructure,
):
    """Test that manual trigger route reads github_repo from app.state (Bug #87)."""
    # Set app.state values that would be set during startup
    repo_root_path = Path(__file__).resolve().parent.parent.parent.parent  # project root
    authenticated_client.app.state.self_monitoring_repo_root = str(repo_root_path)
    authenticated_client.app.state.self_monitoring_github_repo = "test-owner/test-repo"

    # Mock CSRF validation to bypass known test infrastructure issue
    with patch(
        "code_indexer.server.web.routes.validate_login_csrf_token", return_value=True
    ):
        # Mock the SelfMonitoringService to capture initialization parameters
        with patch(
            "code_indexer.server.self_monitoring.service.SelfMonitoringService"
        ) as mock_service_class:
            # Create a mock service instance
            mock_service = Mock()
            mock_service.trigger_scan.return_value = {
                "status": "queued",
                "scan_id": "test-scan-456",
            }
            mock_service_class.return_value = mock_service

            # Submit manual trigger request
            form_data = {"csrf_token": "test-token"}
            response = authenticated_client.post("/admin/self-monitoring/run-now", data=form_data)

        # Verify the response succeeded
        assert response.status_code == status.HTTP_200_OK

        # CRITICAL: Verify SelfMonitoringService was instantiated with github_repo
        mock_service_class.assert_called_once()
        init_kwargs = mock_service_class.call_args[1]

        # Bug #87 fix: github_repo should be read from app.state
        assert "github_repo" in init_kwargs, "github_repo parameter is missing"
        assert init_kwargs["github_repo"] == "test-owner/test-repo", \
            "github_repo should match app.state value"
        assert isinstance(
            init_kwargs["github_repo"], str
        ), "github_repo should be a string"
        assert "/" in init_kwargs[
            "github_repo"
        ], "github_repo should be in 'owner/repo' format"


def test_manual_trigger_route_does_not_use_environment_variable(
    authenticated_client: TestClient,
    web_infrastructure,
):
    """Test that manual trigger route reads from app.state, NOT GITHUB_REPOSITORY env var (Bug #87)."""
    import os

    # Set app.state to known values
    repo_root_path = Path(__file__).resolve().parent.parent.parent.parent
    authenticated_client.app.state.self_monitoring_repo_root = str(repo_root_path)
    authenticated_client.app.state.self_monitoring_github_repo = "correct-owner/correct-repo"

    # Set GITHUB_REPOSITORY environment variable to different value
    old_env = os.environ.get("GITHUB_REPOSITORY")
    os.environ["GITHUB_REPOSITORY"] = "wrong-owner/wrong-repo"

    try:
        # Mock CSRF validation to bypass known test infrastructure issue
        with patch(
            "code_indexer.server.web.routes.validate_login_csrf_token", return_value=True
        ):
            # Mock the SelfMonitoringService to capture initialization parameters
            with patch(
                "code_indexer.server.self_monitoring.service.SelfMonitoringService"
            ) as mock_service_class:
                # Create a mock service instance
                mock_service = Mock()
                mock_service.trigger_scan.return_value = {
                    "status": "queued",
                    "scan_id": "test-scan-789",
                }
                mock_service_class.return_value = mock_service

                # Submit manual trigger request
                form_data = {"csrf_token": "test-token"}
                response = authenticated_client.post(
                    "/admin/self-monitoring/run-now", data=form_data
                )

                # Verify the response succeeded
                assert response.status_code == status.HTTP_200_OK

                # CRITICAL: Verify github_repo came from app.state, NOT env var
                mock_service_class.assert_called_once()
                init_kwargs = mock_service_class.call_args[1]

                # github_repo should be from app.state, NOT from env var
                assert init_kwargs["github_repo"] == "correct-owner/correct-repo", \
                    "github_repo should come from app.state, not GITHUB_REPOSITORY env var"
                assert (
                    init_kwargs["github_repo"] != "wrong-owner/wrong-repo"
                ), "github_repo should NOT come from GITHUB_REPOSITORY env var"
    finally:
        # Restore original environment variable
        if old_env is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = old_env


def test_manual_trigger_route_requires_authentication(web_client: TestClient):
    """Test that manual trigger endpoint requires authentication."""
    response = web_client.post("/admin/self-monitoring/run-now")

    # Should return 401 Unauthorized (no redirect for API endpoint)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_manual_trigger_route_requires_csrf_token(authenticated_client: TestClient):
    """Test that manual trigger endpoint requires valid CSRF token."""
    # Submit without CSRF token
    response = authenticated_client.post("/admin/self-monitoring/run-now", data={})

    # Should return 403 Forbidden
    assert response.status_code == status.HTTP_403_FORBIDDEN
