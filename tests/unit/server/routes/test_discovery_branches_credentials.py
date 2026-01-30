"""
Tests for credential retrieval in Discovery Branches API Route.

Following TDD methodology - these tests define expected behavior BEFORE implementation.

Story #21: Fix Branch Fetching for Private Repositories

Tests verify that the fetch_discovery_branches route:
1. Retrieves credentials from CITokenManager
2. Passes credentials to RemoteBranchService
3. Uses correct credentials based on platform (github/gitlab)
"""

from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


class TestDiscoveryBranchesCredentialRetrieval:
    """Tests for credential retrieval in the discovery branches route."""

    def test_gitlab_credentials_retrieved_from_token_manager(self):
        """Test that GitLab credentials are retrieved from CITokenManager.

        When fetching branches for a GitLab repository, the route should:
        1. Call _get_token_manager() to get CITokenManager instance
        2. Call get_token("gitlab") to retrieve stored token
        3. Pass the token to RemoteBranchService
        """
        from code_indexer.server.web.routes import web_router
        from code_indexer.server.services.ci_token_manager import TokenData
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        # Mock the session manager to bypass auth
        mock_session = MagicMock()
        mock_session.username = "admin"
        mock_session.role = "admin"

        # Mock token manager with GitLab token
        mock_token_manager = MagicMock()
        mock_token_manager.get_token.return_value = TokenData(
            platform="gitlab",
            token="glpat-test-token-12345",
            base_url=None,
        )

        # Mock RemoteBranchService to capture credentials passed
        mock_service = MagicMock()
        mock_service.fetch_remote_branches.return_value = MagicMock(
            success=True,
            branches=["main", "develop"],
            default_branch="main",
            error=None,
        )

        client = TestClient(app)

        with patch(
            "code_indexer.server.web.routes.get_session_manager"
        ) as mock_get_session_manager, patch(
            "code_indexer.server.web.routes._get_token_manager"
        ) as mock_get_token_manager, patch(
            "code_indexer.server.services.remote_branch_service.RemoteBranchService"
        ) as mock_service_class:
            # Setup mocks
            mock_session_manager = MagicMock()
            mock_session_manager.get_session.return_value = mock_session
            mock_get_session_manager.return_value = mock_session_manager
            mock_get_token_manager.return_value = mock_token_manager
            mock_service_class.return_value = mock_service

            response = client.post(
                "/admin/api/discovery/branches",
                json={
                    "repos": [
                        {
                            "clone_url": "git@gitlab.com:org/private-repo.git",
                            "platform": "gitlab",
                        }
                    ]
                },
            )

            # Verify token manager was called for gitlab
            mock_token_manager.get_token.assert_called_with("gitlab")

            # Verify service was called with credentials
            mock_service.fetch_remote_branches.assert_called_once()
            call_kwargs = mock_service.fetch_remote_branches.call_args

            # The credentials should be passed (not None)
            assert call_kwargs.kwargs.get("credentials") == "glpat-test-token-12345"

    def test_github_credentials_retrieved_from_token_manager(self):
        """Test that GitHub credentials are retrieved from CITokenManager.

        When fetching branches for a GitHub repository, the route should:
        1. Call _get_token_manager() to get CITokenManager instance
        2. Call get_token("github") to retrieve stored token
        3. Pass the token to RemoteBranchService
        """
        from code_indexer.server.web.routes import web_router
        from code_indexer.server.services.ci_token_manager import TokenData
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        # Mock the session manager to bypass auth
        mock_session = MagicMock()
        mock_session.username = "admin"
        mock_session.role = "admin"

        # Mock token manager with GitHub token
        mock_token_manager = MagicMock()
        mock_token_manager.get_token.return_value = TokenData(
            platform="github",
            token="ghp_testtoken1234567890",
            base_url=None,
        )

        # Mock RemoteBranchService
        mock_service = MagicMock()
        mock_service.fetch_remote_branches.return_value = MagicMock(
            success=True,
            branches=["main", "develop"],
            default_branch="main",
            error=None,
        )

        client = TestClient(app)

        with patch(
            "code_indexer.server.web.routes.get_session_manager"
        ) as mock_get_session_manager, patch(
            "code_indexer.server.web.routes._get_token_manager"
        ) as mock_get_token_manager, patch(
            "code_indexer.server.services.remote_branch_service.RemoteBranchService"
        ) as mock_service_class:
            mock_session_manager = MagicMock()
            mock_session_manager.get_session.return_value = mock_session
            mock_get_session_manager.return_value = mock_session_manager
            mock_get_token_manager.return_value = mock_token_manager
            mock_service_class.return_value = mock_service

            response = client.post(
                "/admin/api/discovery/branches",
                json={
                    "repos": [
                        {
                            "clone_url": "https://github.com/org/private-repo.git",
                            "platform": "github",
                        }
                    ]
                },
            )

            # Verify token manager was called for github
            mock_token_manager.get_token.assert_called_with("github")

            # Verify service was called with credentials
            mock_service.fetch_remote_branches.assert_called_once()
            call_kwargs = mock_service.fetch_remote_branches.call_args
            assert call_kwargs.kwargs.get("credentials") == "ghp_testtoken1234567890"

    def test_no_credentials_when_token_not_configured(self):
        """Test that None is passed when no token is configured.

        When no token exists for a platform, credentials should be None.
        """
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        # Mock the session manager to bypass auth
        mock_session = MagicMock()
        mock_session.username = "admin"
        mock_session.role = "admin"

        # Mock token manager with no token configured
        mock_token_manager = MagicMock()
        mock_token_manager.get_token.return_value = None

        # Mock RemoteBranchService
        mock_service = MagicMock()
        mock_service.fetch_remote_branches.return_value = MagicMock(
            success=True,
            branches=["main"],
            default_branch="main",
            error=None,
        )

        client = TestClient(app)

        with patch(
            "code_indexer.server.web.routes.get_session_manager"
        ) as mock_get_session_manager, patch(
            "code_indexer.server.web.routes._get_token_manager"
        ) as mock_get_token_manager, patch(
            "code_indexer.server.services.remote_branch_service.RemoteBranchService"
        ) as mock_service_class:
            mock_session_manager = MagicMock()
            mock_session_manager.get_session.return_value = mock_session
            mock_get_session_manager.return_value = mock_session_manager
            mock_get_token_manager.return_value = mock_token_manager
            mock_service_class.return_value = mock_service

            response = client.post(
                "/admin/api/discovery/branches",
                json={
                    "repos": [
                        {
                            "clone_url": "https://github.com/public/repo.git",
                            "platform": "github",
                        }
                    ]
                },
            )

            # Verify service was called with None credentials
            mock_service.fetch_remote_branches.assert_called_once()
            call_kwargs = mock_service.fetch_remote_branches.call_args
            assert call_kwargs.kwargs.get("credentials") is None

    def test_multiple_repos_use_platform_specific_credentials(self):
        """Test that multiple repos get platform-specific credentials.

        When fetching branches for repos from different platforms,
        each should use the correct platform's credentials.
        """
        from code_indexer.server.web.routes import web_router
        from code_indexer.server.services.ci_token_manager import TokenData
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        mock_session = MagicMock()
        mock_session.username = "admin"
        mock_session.role = "admin"

        # Mock token manager with both tokens
        mock_token_manager = MagicMock()
        def get_token_side_effect(platform):
            if platform == "github":
                return TokenData(platform="github", token="ghp_github_token", base_url=None)
            elif platform == "gitlab":
                return TokenData(platform="gitlab", token="glpat-gitlab_token", base_url=None)
            return None
        mock_token_manager.get_token.side_effect = get_token_side_effect

        # Track credentials passed for each call
        credentials_used = []
        mock_service = MagicMock()
        def fetch_side_effect(clone_url, platform, credentials):
            credentials_used.append((clone_url, platform, credentials))
            return MagicMock(
                success=True,
                branches=["main"],
                default_branch="main",
                error=None,
            )
        mock_service.fetch_remote_branches.side_effect = fetch_side_effect

        client = TestClient(app)

        with patch(
            "code_indexer.server.web.routes.get_session_manager"
        ) as mock_get_session_manager, patch(
            "code_indexer.server.web.routes._get_token_manager"
        ) as mock_get_token_manager, patch(
            "code_indexer.server.services.remote_branch_service.RemoteBranchService"
        ) as mock_service_class:
            mock_session_manager = MagicMock()
            mock_session_manager.get_session.return_value = mock_session
            mock_get_session_manager.return_value = mock_session_manager
            mock_get_token_manager.return_value = mock_token_manager
            mock_service_class.return_value = mock_service

            response = client.post(
                "/admin/api/discovery/branches",
                json={
                    "repos": [
                        {
                            "clone_url": "https://github.com/org/repo1.git",
                            "platform": "github",
                        },
                        {
                            "clone_url": "git@gitlab.com:org/repo2.git",
                            "platform": "gitlab",
                        },
                    ]
                },
            )

            # Verify correct credentials used for each platform
            assert len(credentials_used) == 2

            github_call = next(c for c in credentials_used if c[1] == "github")
            gitlab_call = next(c for c in credentials_used if c[1] == "gitlab")

            assert github_call[2] == "ghp_github_token"
            assert gitlab_call[2] == "glpat-gitlab_token"
