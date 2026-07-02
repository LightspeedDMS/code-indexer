"""
Tests for GitLabProvider configuration.

Story #754 removed server-side cursor pagination (discover_repositories).
Tests for discover_repositories, search, exclusion, error handling via
cursor API, and sorting order were deleted along with that feature.
"""

from unittest.mock import MagicMock

_FAKE_GITLAB_TOKEN = "fake-gitlab-token"
_CUSTOM_GITLAB_URL = "https://gitlab.mycompany.com"
_DEFAULT_GITLAB_URL = "https://gitlab.com"


def _make_provider(token=_FAKE_GITLAB_TOKEN, base_url=None):
    """Create a GitLabProvider with a fake token and no indexed repos."""
    from code_indexer.server.services.repository_providers.gitlab_provider import (
        GitLabProvider,
    )
    from code_indexer.server.services.ci_token_manager import TokenData

    token_manager = MagicMock()
    if token is not None:
        token_manager.get_token.return_value = TokenData(
            platform="gitlab",
            token=token,
            base_url=base_url,
        )
    else:
        token_manager.get_token.return_value = None
    golden_repo_manager = MagicMock()
    golden_repo_manager.list_golden_repos.return_value = []
    return GitLabProvider(
        token_manager=token_manager,
        golden_repo_manager=golden_repo_manager,
    )


class TestGitLabProviderPlatform:
    """Tests for GitLabProvider platform identity and is_configured."""

    def test_provider_has_gitlab_platform(self):
        """GitLabProvider reports 'gitlab' as its platform."""
        provider = _make_provider()
        assert provider.platform == "gitlab"

    def test_is_configured_returns_true_when_token_exists(self):
        """is_configured returns True when a GitLab token is present."""
        provider = _make_provider()
        assert provider.is_configured() is True

    def test_is_configured_returns_false_when_no_token(self):
        """is_configured returns False when no GitLab token is configured."""
        provider = _make_provider(token=None)
        assert provider.is_configured() is False


class TestGitLabProviderBaseUrl:
    """Tests for GitLabProvider base URL resolution."""

    def test_uses_custom_base_url_when_provided(self):
        """Provider uses the custom base URL for self-hosted GitLab."""
        provider = _make_provider(base_url=_CUSTOM_GITLAB_URL)
        assert provider._get_base_url() == _CUSTOM_GITLAB_URL

    def test_default_base_url_is_gitlab_com(self):
        """Provider falls back to gitlab.com when no custom URL is set."""
        provider = _make_provider()
        assert provider._get_base_url() == _DEFAULT_GITLAB_URL
