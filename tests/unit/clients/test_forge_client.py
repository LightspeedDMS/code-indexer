"""
Unit tests for ForgeClient implementations.

Story #386: Git Credential Management with Identity Discovery

Tests GitHub and GitLab forge clients for identity validation,
using mocked httpx responses.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestGitHubForgeClient:
    """Tests for GitHubForgeClient identity validation."""

    @pytest.mark.asyncio
    async def test_validate_and_discover_returns_identity_on_success(self):
        """Valid GitHub PAT returns git_user_name, git_user_email, forge_username."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        client = GitHubForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "octocat",
            "name": "The Octocat",
            "email": "octocat@github.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            result = await client.validate_and_discover("ghp_test123", "github.com")

        assert result["forge_username"] == "octocat"
        assert result["git_user_name"] == "The Octocat"
        assert result["git_user_email"] == "octocat@github.com"

    @pytest.mark.asyncio
    async def test_validate_and_discover_raises_on_401(self):
        """Invalid GitHub PAT raises ValueError with clear message."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        client = GitHubForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Bad credentials"}

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            with pytest.raises(ForgeAuthenticationError) as exc_info:
                await client.validate_and_discover("invalid_token", "github.com")

        assert "Invalid" in str(exc_info.value) or "401" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_and_discover_uses_correct_github_api_url(self):
        """GitHub client calls /user endpoint on correct host."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        client = GitHubForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "testuser",
            "name": "Test User",
            "email": "test@example.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            await client.validate_and_discover("ghp_test123", "github.com")

            call_args = mock_async_client.get.call_args
            assert "https://api.github.com/user" == call_args[0][0]

    @pytest.mark.asyncio
    async def test_validate_and_discover_uses_token_auth_header(self):
        """GitHub client sends Authorization: token {pat} header."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        client = GitHubForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "testuser",
            "name": "Test User",
            "email": "test@example.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            await client.validate_and_discover("ghp_mytoken", "github.com")

            call_args = mock_async_client.get.call_args
            headers = call_args[1].get("headers", {})
            assert headers.get("Authorization") == "token ghp_mytoken"

    @pytest.mark.asyncio
    async def test_validate_and_discover_supports_github_enterprise_host(self):
        """GitHub Enterprise custom host uses api.{host}/user endpoint."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        client = GitHubForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "enterpriseuser",
            "name": "Enterprise User",
            "email": "user@corp.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            await client.validate_and_discover("ghp_test", "github.corp.com")

            call_args = mock_async_client.get.call_args
            url = call_args[0][0]
            assert url == "https://github.corp.com/api/v3/user"

    @pytest.mark.asyncio
    async def test_validate_and_discover_handles_null_email(self):
        """GitHub client handles users with no public email (email=None)."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        client = GitHubForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "noemail_user",
            "name": "No Email User",
            "email": None,
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            result = await client.validate_and_discover("ghp_test", "github.com")

        assert result["forge_username"] == "noemail_user"
        assert result["git_user_email"] is None or result["git_user_email"] == ""


class TestGitLabForgeClient:
    """Tests for GitLabForgeClient identity validation."""

    @pytest.mark.asyncio
    async def test_validate_and_discover_returns_identity_on_success(self):
        """Valid GitLab PAT returns git_user_name, git_user_email, forge_username."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        client = GitLabForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "username": "gitlabuser",
            "name": "GitLab User",
            "email": "user@gitlab.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            result = await client.validate_and_discover("glpat-test123", "gitlab.com")

        assert result["forge_username"] == "gitlabuser"
        assert result["git_user_name"] == "GitLab User"
        assert result["git_user_email"] == "user@gitlab.com"

    @pytest.mark.asyncio
    async def test_validate_and_discover_raises_on_401(self):
        """Invalid GitLab PAT raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        client = GitLabForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "401 Unauthorized"}

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            with pytest.raises(ForgeAuthenticationError):
                await client.validate_and_discover("glpat-invalid", "gitlab.com")

    @pytest.mark.asyncio
    async def test_validate_and_discover_uses_correct_gitlab_api_url(self):
        """GitLab client calls /api/v4/user on correct host."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        client = GitLabForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "username": "testuser",
            "name": "Test User",
            "email": "test@gitlab.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            await client.validate_and_discover("glpat-test", "gitlab.com")

            call_args = mock_async_client.get.call_args
            url = call_args[0][0]
            assert "https://gitlab.com/api/v4/user" == url

    @pytest.mark.asyncio
    async def test_validate_and_discover_uses_private_token_header(self):
        """GitLab client sends PRIVATE-TOKEN header."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        client = GitLabForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "username": "testuser",
            "name": "Test User",
            "email": "test@gitlab.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            await client.validate_and_discover("glpat-mytoken", "gitlab.com")

            call_args = mock_async_client.get.call_args
            headers = call_args[1].get("headers", {})
            assert headers.get("PRIVATE-TOKEN") == "glpat-mytoken"

    @pytest.mark.asyncio
    async def test_validate_and_discover_supports_self_hosted_gitlab(self):
        """Self-hosted GitLab uses https://{host}/api/v4/user."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        client = GitLabForgeClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "username": "selfhosted_user",
            "name": "Self Hosted User",
            "email": "user@corp.gitlab.com",
        }

        with patch(
            "code_indexer.server.clients.forge_client.httpx.AsyncClient"
        ) as mock_client_class:
            mock_async_client = AsyncMock()
            mock_async_client.__aenter__.return_value = mock_async_client
            mock_async_client.__aexit__.return_value = None
            mock_async_client.get.return_value = mock_response
            mock_client_class.return_value = mock_async_client

            await client.validate_and_discover("glpat-test", "gitlab.corp.com")

            call_args = mock_async_client.get.call_args
            url = call_args[0][0]
            assert "https://gitlab.corp.com/api/v4/user" == url


class TestForgeClientFactory:
    """Tests for the forge client factory function."""

    def test_get_forge_client_returns_github_for_github_type(self):
        """get_forge_client('github') returns GitHubForgeClient."""
        from code_indexer.server.clients.forge_client import (
            get_forge_client,
            GitHubForgeClient,
        )

        client = get_forge_client("github")
        assert isinstance(client, GitHubForgeClient)

    def test_get_forge_client_returns_gitlab_for_gitlab_type(self):
        """get_forge_client('gitlab') returns GitLabForgeClient."""
        from code_indexer.server.clients.forge_client import (
            get_forge_client,
            GitLabForgeClient,
        )

        client = get_forge_client("gitlab")
        assert isinstance(client, GitLabForgeClient)

    def test_get_forge_client_raises_for_unknown_type(self):
        """get_forge_client('unknown') raises ValueError."""
        from code_indexer.server.clients.forge_client import get_forge_client

        with pytest.raises(ValueError) as exc_info:
            get_forge_client("bitbucket")

        assert "bitbucket" in str(exc_info.value).lower() or "unsupported" in str(
            exc_info.value
        ).lower()
