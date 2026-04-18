"""
Tests for GitLabProvider.discover_all_repositories (Story #754).

RED phase: written before implementation to drive design.
Mocking strategy:
  - HTTP transport boundary: patch("httpx.get")
  - Constructor collaborators (token_manager, golden_repo_manager): MagicMock test doubles

GitLab signals has_more via: current_page < x-total-pages header.
When x-total-pages == current page number, has_more is False and the loop must stop.
"""

import inspect
import pytest
from unittest.mock import MagicMock, patch
import httpx


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_token_manager():
    from code_indexer.server.services.ci_token_manager import TokenData

    tm = MagicMock()
    tm.get_token.return_value = TokenData(
        platform="gitlab",
        token="test-token",
        base_url=None,
    )
    return tm


def _make_golden_repo_manager(indexed_urls=None):
    grm = MagicMock()
    grm.list_golden_repos.return_value = [{"repo_url": u} for u in (indexed_urls or [])]
    return grm


def _make_provider(indexed_urls=None):
    from code_indexer.server.services.repository_providers.gitlab_provider import (
        GitLabProvider,
    )

    return GitLabProvider(
        token_manager=_make_token_manager(),
        golden_repo_manager=_make_golden_repo_manager(indexed_urls),
    )


def _make_gitlab_project(i, *, visibility="private"):
    return {
        "id": i,
        "path_with_namespace": f"group/project{i}",
        "description": f"Description {i}",
        "http_url_to_repo": f"https://gitlab.com/group/project{i}.git",
        "ssh_url_to_repo": f"git@gitlab.com:group/project{i}.git",
        "default_branch": "main",
        "last_activity_at": "2024-01-15T10:30:00Z",
        "visibility": visibility,
    }


def _make_httpx_response(projects, *, total_pages, total=None, status_code=200):
    """
    Build an httpx.Response mock.

    total_pages: the x-total-pages header value.
    total: x-total header value; defaults to len(projects).
    """
    source_total = total if total is not None else len(projects)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = {
        "x-total": str(source_total),
        "x-total-pages": str(total_pages),
    }
    resp.json.return_value = projects
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# (1) Method signature
# ---------------------------------------------------------------------------


class TestGitLabDiscoverAllSignature:
    """discover_all_repositories must exist with the correct parameter names."""

    def test_method_exists_with_correct_parameters(self):
        """discover_all_repositories must accept indexed_urls and hidden_identifiers."""
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProvider,
        )

        assert hasattr(GitLabProvider, "discover_all_repositories"), (
            "discover_all_repositories not found on GitLabProvider"
        )
        sig = inspect.signature(GitLabProvider.discover_all_repositories)
        param_names = list(sig.parameters.keys())
        assert "indexed_urls" in param_names, f"indexed_urls not in {param_names}"
        assert "hidden_identifiers" in param_names, (
            f"hidden_identifiers not in {param_names}"
        )


# ---------------------------------------------------------------------------
# (2) Return shape
# ---------------------------------------------------------------------------


class TestGitLabDiscoverAllReturnShape:
    """Return dict must contain repositories, total_source, total_unregistered."""

    def test_returns_dict_with_required_keys(self):
        """Must return dict with repositories, total_source, total_unregistered."""
        provider = _make_provider()
        resp = _make_httpx_response([], total_pages=1, total=0)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert "repositories" in result
        assert "total_source" in result
        assert "total_unregistered" in result

    def test_repositories_is_list_of_dicts(self):
        """repositories must be a list of dicts."""
        provider = _make_provider()
        projects = [_make_gitlab_project(0)]
        resp = _make_httpx_response(projects, total_pages=1)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert isinstance(result["repositories"], list)
        assert isinstance(result["repositories"][0], dict)

    def test_repo_dicts_contain_required_fields(self):
        """Each repo dict must have clone_url_https, clone_url_ssh, name, is_hidden, platform."""
        provider = _make_provider()
        projects = [_make_gitlab_project(0)]
        resp = _make_httpx_response(projects, total_pages=1)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        repo = result["repositories"][0]
        for field in (
            "clone_url_https",
            "clone_url_ssh",
            "name",
            "is_hidden",
            "platform",
        ):
            assert field in repo, f"Field '{field}' missing from repo dict"


# ---------------------------------------------------------------------------
# (3) Exhaustive pagination — stops exactly when has_more becomes False
# ---------------------------------------------------------------------------


class TestGitLabDiscoverAllPagination:
    """
    Exhaustive fetch loop must iterate until has_more=False.
    GitLab: has_more = (current_page < x-total-pages).
    Tests verify the outgoing page= parameters via mock_get.call_args_list.
    """

    def _extract_page_param(self, call_args):
        """Extract the 'page' param value from an httpx.get call's kwargs."""
        # httpx.get(url, headers=..., params={"page": N, ...}, timeout=...)
        params = call_args.kwargs.get("params") or {}
        return params.get("page")

    def test_stops_when_total_pages_equals_one(self):
        """
        Loop must stop after page 1 when x-total-pages=1 (has_more=False immediately).
        Verifies request uses page=1 and no further calls follow.
        """
        provider = _make_provider()
        projects = [_make_gitlab_project(i) for i in range(3)]
        resp = _make_httpx_response(projects, total_pages=1, total=3)
        with patch("httpx.get", return_value=resp) as mock_get:
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert mock_get.call_count == 1
        page_used = self._extract_page_param(mock_get.call_args_list[0])
        assert page_used == 1, f"Expected page=1 for first call, got {page_used}"
        assert len(result["repositories"]) == 3

    def test_fetches_page_1_then_page_2_when_total_pages_is_2(self):
        """
        With x-total-pages=2:
          - Call 1 uses page=1 (has_more=True because 1 < 2)
          - Call 2 uses page=2 (has_more=False because 2 < 2 is False)
          - No further calls after page 2.
        """
        provider = _make_provider()
        page1_projects = [_make_gitlab_project(i) for i in range(3)]
        page2_projects = [_make_gitlab_project(i) for i in range(3, 5)]

        resp_page1 = _make_httpx_response(page1_projects, total_pages=2, total=5)
        resp_page2 = _make_httpx_response(page2_projects, total_pages=2, total=5)

        with patch("httpx.get", side_effect=[resp_page1, resp_page2]) as mock_get:
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )

        assert mock_get.call_count == 2

        page_1_used = self._extract_page_param(mock_get.call_args_list[0])
        page_2_used = self._extract_page_param(mock_get.call_args_list[1])
        assert page_1_used == 1, f"First call must use page=1, got {page_1_used}"
        assert page_2_used == 2, f"Second call must use page=2, got {page_2_used}"

        assert result["total_source"] == 5
        assert len(result["repositories"]) == 5

    def test_does_not_fetch_beyond_last_page(self):
        """
        With total_pages=2 the loop must issue exactly 2 GETs, not 3.
        A third response is provided but must never be consumed.
        """
        provider = _make_provider()
        page1 = [_make_gitlab_project(i) for i in range(2)]
        page2 = [_make_gitlab_project(i) for i in range(2, 4)]
        page3_should_not_be_called = [_make_gitlab_project(99)]

        responses = [
            _make_httpx_response(page1, total_pages=2, total=4),
            _make_httpx_response(page2, total_pages=2, total=4),
            _make_httpx_response(page3_should_not_be_called, total_pages=2, total=4),
        ]
        with patch("httpx.get", side_effect=responses) as mock_get:
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert mock_get.call_count == 2
        repo_names = [r["name"] for r in result["repositories"]]
        assert "group/project99" not in repo_names


# ---------------------------------------------------------------------------
# (4) Indexed repo filtering
# ---------------------------------------------------------------------------


class TestGitLabDiscoverAllFiltering:
    """Already-indexed repos must not appear in the result repositories list."""

    def test_indexed_repos_excluded_from_repositories_list(self):
        """
        Repos matching indexed_urls must not appear in result.

        indexed_urls must contain canonical URL forms (as produced by
        GitUrlNormalizer.get_canonical_form), not raw HTTPS URLs.
        GitLabProvider._is_repo_indexed normalizes before comparing.
        """
        raw_url = "https://gitlab.com/group/project0.git"
        # GitUrlNormalizer strips scheme, .git suffix, and trailing slash
        canonical_url = "gitlab.com/group/project0"
        provider = _make_provider(indexed_urls=[raw_url])
        projects = [_make_gitlab_project(0), _make_gitlab_project(1)]
        resp = _make_httpx_response(projects, total_pages=1, total=2)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls={canonical_url},
                hidden_identifiers=set(),
            )
        repo_urls = [r["clone_url_https"] for r in result["repositories"]]
        assert raw_url not in repo_urls
        assert result["total_source"] == 2
        assert len(result["repositories"]) == 1


# ---------------------------------------------------------------------------
# (5) is_hidden flag
# ---------------------------------------------------------------------------


class TestGitLabDiscoverAllHiddenFlag:
    """is_hidden computation checks both SSH and HTTPS compound identifiers."""

    def test_is_hidden_false_when_not_in_set(self):
        """is_hidden must be False when repo is not in hidden_identifiers."""
        provider = _make_provider()
        projects = [_make_gitlab_project(0)]
        resp = _make_httpx_response(projects, total_pages=1)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert result["repositories"][0]["is_hidden"] is False

    def test_is_hidden_true_for_ssh_identifier(self):
        """is_hidden must be True when platform:clone_url_ssh is in hidden_identifiers."""
        provider = _make_provider()
        projects = [_make_gitlab_project(0)]
        hidden = {"gitlab:git@gitlab.com:group/project0.git"}
        resp = _make_httpx_response(projects, total_pages=1)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=hidden
            )
        assert result["repositories"][0]["is_hidden"] is True

    def test_is_hidden_true_for_https_identifier(self):
        """is_hidden must be True when platform:clone_url_https is in hidden_identifiers."""
        provider = _make_provider()
        projects = [_make_gitlab_project(0)]
        hidden = {"gitlab:https://gitlab.com/group/project0.git"}
        resp = _make_httpx_response(projects, total_pages=1)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=hidden
            )
        assert result["repositories"][0]["is_hidden"] is True

    def test_total_unregistered_counts_non_hidden(self):
        """total_unregistered must count repos where is_hidden=False."""
        provider = _make_provider()
        projects = [_make_gitlab_project(0), _make_gitlab_project(1)]
        hidden = {"gitlab:git@gitlab.com:group/project0.git"}
        resp = _make_httpx_response(projects, total_pages=1, total=2)
        with patch("httpx.get", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=hidden
            )
        assert result["total_unregistered"] == 1


# ---------------------------------------------------------------------------
# (6) Error propagation — no partial results
# ---------------------------------------------------------------------------


class TestGitLabDiscoverAllErrors:
    """Upstream errors must raise GitLabProviderError; no partial results returned."""

    def test_first_page_error_raises_gitlab_provider_error(self):
        """On first-page HTTP error, raise GitLabProviderError."""
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProviderError,
        )

        provider = _make_provider()
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 500
        error_resp.headers = {"x-total": "0", "x-total-pages": "1"}
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 error", request=MagicMock(), response=error_resp
        )
        with patch("httpx.get", return_value=error_resp):
            with pytest.raises(GitLabProviderError):
                provider.discover_all_repositories(
                    indexed_urls=set(), hidden_identifiers=set()
                )

    def test_mid_pagination_error_raises_no_partial_results(self):
        """Error on page 2 must raise, not return the page-1 results."""
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProviderError,
        )

        provider = _make_provider()
        page1 = [_make_gitlab_project(i) for i in range(3)]
        ok_resp = _make_httpx_response(page1, total_pages=2, total=6)

        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 500
        error_resp.headers = {"x-total": "0", "x-total-pages": "2"}
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 error", request=MagicMock(), response=error_resp
        )

        with patch("httpx.get", side_effect=[ok_resp, error_resp]):
            with pytest.raises(GitLabProviderError):
                provider.discover_all_repositories(
                    indexed_urls=set(), hidden_identifiers=set()
                )
