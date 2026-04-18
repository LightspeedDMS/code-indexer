"""
Tests for GitHubProvider configuration.

Story #754 removed server-side cursor pagination (discover_repositories).
Tests for discover_repositories, search, exclusion, error handling via
cursor API, and link-header parsing were deleted along with that feature.
"""

import pytest
from unittest.mock import MagicMock

_FAKE_GITHUB_TOKEN = "fake-github-token"


class TestGitHubProviderConfiguration:
    """Tests for GitHubProvider configuration handling."""

    def test_provider_has_github_platform(self):
        """Test that GitHubProvider reports github as its platform."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider.platform == "github"

    @pytest.mark.asyncio
    async def test_is_configured_returns_true_when_token_exists(self):
        """Test is_configured returns True when GitHub token is configured."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token=_FAKE_GITHUB_TOKEN,
            base_url=None,
        )
        golden_repo_manager = MagicMock()

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider.is_configured() is True

    @pytest.mark.asyncio
    async def test_is_configured_returns_false_when_no_token(self):
        """Test is_configured returns False when no GitHub token is configured."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        token_manager.get_token.return_value = None
        golden_repo_manager = MagicMock()

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider.is_configured() is False

    def test_default_base_url_is_github_api(self):
        """Test that default base URL is api.github.com."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token=_FAKE_GITHUB_TOKEN,
            base_url=None,
        )
        golden_repo_manager = MagicMock()

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider._get_base_url() == "https://api.github.com"
