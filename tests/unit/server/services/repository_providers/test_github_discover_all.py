"""
Tests for GitHubProvider.discover_all_repositories (Story #754).

RED phase: written before implementation to drive design.
Mocking strategy:
  - HTTP transport boundary: patch("httpx.post") for GraphQL calls
  - Constructor collaborators (token_manager, golden_repo_manager): MagicMock test doubles

GitHub signals has_more via: pageInfo.hasNextPage in GraphQL response.
When hasNextPage=False, the loop must stop.
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
        platform="github",
        token="test-token",
        base_url=None,
    )
    return tm


def _make_golden_repo_manager(indexed_urls=None):
    grm = MagicMock()
    grm.list_golden_repos.return_value = [{"repo_url": u} for u in (indexed_urls or [])]
    return grm


def _make_provider(indexed_urls=None):
    from code_indexer.server.services.repository_providers.github_provider import (
        GitHubProvider,
    )

    return GitHubProvider(
        token_manager=_make_token_manager(),
        golden_repo_manager=_make_golden_repo_manager(indexed_urls),
    )


def _make_graphql_node(i):
    return {
        "name": f"project{i}",
        "nameWithOwner": f"owner/project{i}",
        "description": f"Desc {i}",
        "isPrivate": False,
        "url": f"https://github.com/owner/project{i}",
        "sshUrl": f"git@github.com:owner/project{i}.git",
        "pushedAt": "2024-01-15T10:30:00Z",
        "defaultBranchRef": {
            "name": "main",
            "target": {
                "history": {
                    "nodes": [
                        {
                            "oid": "abc123456789",
                            "author": {"name": "Alice"},
                            "committedDate": "2024-01-15T10:30:00Z",
                        }
                    ]
                }
            },
        },
    }


def _make_graphql_response(
    nodes, *, has_next_page=False, end_cursor=None, total_count=None
):
    """Build an httpx.Response mock wrapping a GitHub GraphQL response."""
    data = {
        "data": {
            "viewer": {
                "repositories": {
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                    "totalCount": total_count
                    if total_count is not None
                    else len(nodes),
                    "nodes": nodes,
                }
            }
        }
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.headers = {}
    resp.json.return_value = data
    return resp


# ---------------------------------------------------------------------------
# (1) Method signature
# ---------------------------------------------------------------------------


class TestGitHubDiscoverAllSignature:
    """discover_all_repositories must exist with the correct parameter names."""

    def test_method_exists_with_correct_parameters(self):
        """discover_all_repositories must accept indexed_urls and hidden_identifiers."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        assert hasattr(GitHubProvider, "discover_all_repositories"), (
            "discover_all_repositories not found on GitHubProvider"
        )
        sig = inspect.signature(GitHubProvider.discover_all_repositories)
        param_names = list(sig.parameters.keys())
        assert "indexed_urls" in param_names, f"indexed_urls not in {param_names}"
        assert "hidden_identifiers" in param_names, (
            f"hidden_identifiers not in {param_names}"
        )


# ---------------------------------------------------------------------------
# (2) Return shape
# ---------------------------------------------------------------------------


class TestGitHubDiscoverAllReturnShape:
    """Return dict must contain repositories, total_source, total_unregistered."""

    def test_returns_dict_with_required_keys(self):
        """Must return dict with repositories, total_source, total_unregistered."""
        provider = _make_provider()
        resp = _make_graphql_response([], has_next_page=False, total_count=0)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert "repositories" in result
        assert "total_source" in result
        assert "total_unregistered" in result

    def test_repositories_is_list_of_dicts(self):
        """repositories must be a list of dicts."""
        provider = _make_provider()
        nodes = [_make_graphql_node(0)]
        resp = _make_graphql_response(nodes, has_next_page=False)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert isinstance(result["repositories"], list)
        assert isinstance(result["repositories"][0], dict)

    def _single_repo_dict(self):
        """Return the first repo dict from a single-node standard discovery call."""
        provider = _make_provider()
        nodes = [_make_graphql_node(0)]
        resp = _make_graphql_response(nodes, has_next_page=False)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        return result["repositories"][0]

    def test_repo_dicts_contain_required_fields(self):
        """Each repo dict must have all expected fields including commit and metadata fields."""
        repo = self._single_repo_dict()
        for field in (
            "clone_url_https",
            "clone_url_ssh",
            "name",
            "is_hidden",
            "platform",
            "description",
            "default_branch",
            "is_private",
            "last_commit_hash",
            "last_commit_author",
            "last_commit_date",
            "last_activity",
        ):
            assert field in repo, f"Field '{field}' missing from repo dict"

    def test_repo_dicts_contain_correct_field_values(self):
        """Expanded fields must carry exact values extracted from the GraphQL fixture."""
        repo = self._single_repo_dict()
        assert repo["description"] == "Desc 0"
        assert repo["default_branch"] == "main"
        assert repo["is_private"] is False
        # commit hash: provider stores first 7 chars of oid "abc123456789"
        assert repo["last_commit_hash"] == "abc1234"
        assert repo["last_commit_author"] == "Alice"
        # datetime fields: provider parses "2024-01-15T10:30:00Z" and re-serialises via .isoformat()
        assert repo["last_commit_date"] == "2024-01-15T10:30:00+00:00"
        assert repo["last_activity"] == "2024-01-15T10:30:00+00:00"


# ---------------------------------------------------------------------------
# (3) Exhaustive pagination — stops exactly when hasNextPage=False
# ---------------------------------------------------------------------------


class TestGitHubDiscoverAllPagination:
    """
    Exhaustive fetch loop must iterate until hasNextPage=False.
    Tests verify the loop passes the endCursor from page 1 to page 2,
    and stops when page 2 returns hasNextPage=False.
    """

    def _extract_after_cursor(self, call_args):
        """Extract the 'after' cursor from the GraphQL query JSON body."""
        # httpx.post(url, headers=..., json={"query": "...", ...}, timeout=...)
        json_body = call_args.kwargs.get("json") or {}
        query = json_body.get("query", "")
        # The after cursor appears as: after: "CURSOR_VALUE"
        import re

        match = re.search(r'after:\s*"([^"]+)"', query)
        return match.group(1) if match else None

    def test_stops_when_has_next_page_is_false(self):
        """
        Loop must stop after one call when hasNextPage=False.
        Verifies only one POST is issued.
        """
        provider = _make_provider()
        nodes = [_make_graphql_node(i) for i in range(3)]
        resp = _make_graphql_response(nodes, has_next_page=False, total_count=3)
        with patch("httpx.post", return_value=resp) as mock_post:
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert mock_post.call_count == 1
        assert len(result["repositories"]) == 3

    def test_fetches_page_2_with_end_cursor_when_has_next_page_true(self):
        """
        When page 1 returns hasNextPage=True with endCursor='cursor1',
        page 2 must be requested with after='cursor1' in the GraphQL query.
        Loop stops when page 2 returns hasNextPage=False.
        """
        provider = _make_provider()
        page1_nodes = [_make_graphql_node(i) for i in range(2)]
        page2_nodes = [_make_graphql_node(i) for i in range(2, 4)]

        resp_page1 = _make_graphql_response(
            page1_nodes, has_next_page=True, end_cursor="cursor1", total_count=4
        )
        resp_page2 = _make_graphql_response(
            page2_nodes, has_next_page=False, end_cursor=None, total_count=4
        )

        with patch("httpx.post", side_effect=[resp_page1, resp_page2]) as mock_post:
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )

        assert mock_post.call_count == 2

        # First call must NOT have an after cursor (first page)
        first_cursor = self._extract_after_cursor(mock_post.call_args_list[0])
        assert first_cursor is None, (
            f"First page must have no 'after' cursor, got {first_cursor!r}"
        )

        # Second call must pass after="cursor1"
        second_cursor = self._extract_after_cursor(mock_post.call_args_list[1])
        assert second_cursor == "cursor1", (
            f"Second page must pass after='cursor1', got {second_cursor!r}"
        )

        assert result["total_source"] == 4
        assert len(result["repositories"]) == 4

    def test_does_not_fetch_beyond_last_page(self):
        """
        With 2 pages only, loop must issue exactly 2 POSTs, not 3.
        A third response is provided but must never be consumed.
        """
        provider = _make_provider()
        page1_nodes = [_make_graphql_node(i) for i in range(2)]
        page2_nodes = [_make_graphql_node(i) for i in range(2, 4)]
        page3_should_not_be_called = [_make_graphql_node(99)]

        responses = [
            _make_graphql_response(
                page1_nodes, has_next_page=True, end_cursor="c1", total_count=4
            ),
            _make_graphql_response(
                page2_nodes, has_next_page=False, end_cursor=None, total_count=4
            ),
            _make_graphql_response(
                page3_should_not_be_called, has_next_page=False, total_count=4
            ),
        ]
        with patch("httpx.post", side_effect=responses) as mock_post:
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert mock_post.call_count == 2
        repo_names = [r["name"] for r in result["repositories"]]
        assert "owner/project99" not in repo_names


# ---------------------------------------------------------------------------
# (4) Indexed repo filtering
# ---------------------------------------------------------------------------


class TestGitHubDiscoverAllFiltering:
    """Already-indexed repos must not appear in the result repositories list."""

    def test_indexed_repos_excluded_from_repositories_list(self):
        """Repos matching indexed_urls must not appear in result.

        indexed_urls must be canonical forms (no scheme, no .git) because
        _is_repo_indexed normalizes repo URLs before comparing.
        Canonical form of https://github.com/owner/project0.git is
        github.com/owner/project0.
        """
        canonical_url = "github.com/owner/project0"
        provider = _make_provider(indexed_urls=[canonical_url])
        nodes = [_make_graphql_node(0), _make_graphql_node(1)]
        resp = _make_graphql_response(nodes, has_next_page=False, total_count=2)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls={canonical_url},
                hidden_identifiers=set(),
            )
        repo_urls = [r["clone_url_https"] for r in result["repositories"]]
        assert "https://github.com/owner/project0.git" not in repo_urls
        assert result["total_source"] == 2
        assert len(result["repositories"]) == 1


# ---------------------------------------------------------------------------
# (5) is_hidden flag
# ---------------------------------------------------------------------------


class TestGitHubDiscoverAllHiddenFlag:
    """is_hidden computation checks both SSH and HTTPS compound identifiers."""

    def test_is_hidden_false_when_not_in_set(self):
        """is_hidden must be False when repo is not in hidden_identifiers."""
        provider = _make_provider()
        nodes = [_make_graphql_node(0)]
        resp = _make_graphql_response(nodes, has_next_page=False)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=set()
            )
        assert result["repositories"][0]["is_hidden"] is False

    def test_is_hidden_true_for_ssh_identifier(self):
        """is_hidden must be True when platform:clone_url_ssh is in hidden_identifiers."""
        provider = _make_provider()
        nodes = [_make_graphql_node(0)]
        hidden = {"github:git@github.com:owner/project0.git"}
        resp = _make_graphql_response(nodes, has_next_page=False)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=hidden
            )
        assert result["repositories"][0]["is_hidden"] is True

    def test_is_hidden_true_for_https_identifier(self):
        """is_hidden must be True when platform:clone_url_https is in hidden_identifiers."""
        provider = _make_provider()
        nodes = [_make_graphql_node(0)]
        hidden = {"github:https://github.com/owner/project0.git"}
        resp = _make_graphql_response(nodes, has_next_page=False)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=hidden
            )
        assert result["repositories"][0]["is_hidden"] is True

    def test_total_unregistered_counts_non_hidden(self):
        """total_unregistered must count repos where is_hidden=False."""
        provider = _make_provider()
        nodes = [_make_graphql_node(0), _make_graphql_node(1)]
        hidden = {"github:git@github.com:owner/project0.git"}
        resp = _make_graphql_response(nodes, has_next_page=False, total_count=2)
        with patch("httpx.post", return_value=resp):
            result = provider.discover_all_repositories(
                indexed_urls=set(), hidden_identifiers=hidden
            )
        assert result["total_unregistered"] == 1


# ---------------------------------------------------------------------------
# (6) Error propagation — no partial results
# ---------------------------------------------------------------------------


class TestGitHubDiscoverAllErrors:
    """Upstream errors must raise GitHubProviderError; no partial results returned."""

    def test_first_page_error_raises_github_provider_error(self):
        """On first-page HTTP error, raise GitHubProviderError."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProviderError,
        )

        provider = _make_provider()
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 500
        error_resp.headers = {}
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 error", request=MagicMock(), response=error_resp
        )
        with patch("httpx.post", return_value=error_resp):
            with pytest.raises(GitHubProviderError):
                provider.discover_all_repositories(
                    indexed_urls=set(), hidden_identifiers=set()
                )

    def test_mid_pagination_error_raises_no_partial_results(self):
        """Error on page 2 must raise, not return the page-1 results."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProviderError,
        )

        provider = _make_provider()
        page1_nodes = [_make_graphql_node(i) for i in range(3)]
        ok_resp = _make_graphql_response(
            page1_nodes, has_next_page=True, end_cursor="cursor1", total_count=6
        )
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 500
        error_resp.headers = {}
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 error", request=MagicMock(), response=error_resp
        )
        with patch("httpx.post", side_effect=[ok_resp, error_resp]):
            with pytest.raises(GitHubProviderError):
                provider.discover_all_repositories(
                    indexed_urls=set(), hidden_identifiers=set()
                )
