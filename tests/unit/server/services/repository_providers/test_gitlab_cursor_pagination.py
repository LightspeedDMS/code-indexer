"""
Tests for Bug #685: GitLab provider cursor-based filter-fill loop.

All tests verify externally visible behavior only:
- Which repositories are returned
- has_next_page, next_cursor, partial_due_to_cap flags
- Correct resumption when the returned next_cursor is fed back in

Mocking: httpx.get (GitLab REST) and httpx.post (GraphQL enrichment) only.
"""

from unittest.mock import MagicMock, patch

import httpx


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_provider(indexed_urls=None):
    """Create GitLabProvider with optionally pre-indexed repos."""
    from code_indexer.server.services.repository_providers.gitlab_provider import (
        GitLabProvider,
    )
    from code_indexer.server.services.ci_token_manager import TokenData

    token_manager = MagicMock()
    token_manager.get_token.return_value = TokenData(
        platform="gitlab", token="dummy", base_url=None
    )
    golden_repo_manager = MagicMock()
    golden_repo_manager.list_golden_repos.return_value = [
        {"repo_url": url} for url in (indexed_urls or [])
    ]
    return GitLabProvider(
        token_manager=token_manager, golden_repo_manager=golden_repo_manager
    )


def _gitlab_project(idx=0):
    """Create a minimal GitLab project dict."""
    return {
        "id": idx,
        "path_with_namespace": f"group/project{idx}",
        "description": f"Project {idx}",
        "http_url_to_repo": f"https://gitlab.com/group/project{idx}.git",
        "ssh_url_to_repo": f"git@gitlab.com:group/project{idx}.git",
        "default_branch": "main",
        "visibility": "private",
        "last_activity_at": "2024-01-15T10:30:00Z",
    }


def _mock_get_response(projects, total=None, total_pages=None, next_page=None):
    """Build a mock httpx.Response for GitLab REST /projects."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = projects
    mock_resp.headers = {
        "x-total": str(total if total is not None else len(projects)),
        "x-total-pages": str(total_pages if total_pages is not None else 1),
        "x-next-page": str(next_page) if next_page else "",
    }
    return mock_resp


def _mock_post_no_enrich():
    """Return a mock httpx.post response that yields empty GraphQL enrichment data."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    return mock_resp


def _sequential(responses):
    """Return a side_effect function that yields responses in order."""
    iterator = iter(responses)

    def side_effect(*args, **kwargs):
        return next(iterator)

    return side_effect


# ---------------------------------------------------------------------------
# Filter-fill loop tests
# ---------------------------------------------------------------------------


class TestGitLabFilterFillLoop:
    def test_empty_filter_returns_full_page(self):
        """No indexed repos: single API call fills the page."""
        provider = _make_provider()
        projects = [_gitlab_project(i) for i in range(3)]
        get_resp = _mock_get_response(projects, total=3, total_pages=1)
        post_resp = _mock_post_no_enrich()

        with (
            patch("httpx.get", return_value=get_resp),
            patch("httpx.post", return_value=post_resp),
        ):
            result = provider.discover_repositories(cursor=None, page_size=3)

        assert len(result.repositories) == 3
        assert result.has_next_page is False
        assert result.next_cursor is None
        assert result.partial_due_to_cap is False

    def test_all_indexed_first_page_fetches_next_page(self):
        """When all repos on page 1 are indexed, loop fetches page 2."""
        indexed = [f"https://gitlab.com/group/project{i}.git" for i in range(2)]
        provider = _make_provider(indexed_urls=indexed)

        first_projects = [_gitlab_project(i) for i in range(2)]
        second_projects = [_gitlab_project(10), _gitlab_project(11)]

        get_resps = [
            _mock_get_response(first_projects, total=4, total_pages=2, next_page=2),
            _mock_get_response(second_projects, total=4, total_pages=2),
        ]
        post_resp = _mock_post_no_enrich()

        with (
            patch("httpx.get", side_effect=_sequential(get_resps)),
            patch("httpx.post", return_value=post_resp),
        ):
            result = provider.discover_repositories(cursor=None, page_size=2)

        assert len(result.repositories) == 2
        assert result.repositories[0].name == "group/project10"
        assert result.repositories[1].name == "group/project11"
        assert result.has_next_page is False

    def test_mid_batch_cursor_allows_resumption(self):
        """page_size=3 from 5-item batch: cursor allows resuming at item 3."""
        provider = _make_provider()
        projects_5 = [_gitlab_project(i) for i in range(5)]
        get_resp_5 = _mock_get_response(projects_5, total=5, total_pages=1)
        post_resp = _mock_post_no_enrich()

        with (
            patch("httpx.get", return_value=get_resp_5),
            patch("httpx.post", return_value=post_resp),
        ):
            first_result = provider.discover_repositories(cursor=None, page_size=3)

        assert len(first_result.repositories) == 3
        assert first_result.has_next_page is True
        assert first_result.next_cursor is not None

        # Resume: same batch served again; items 3 and 4 must come back
        get_resp_5_again = _mock_get_response(projects_5, total=5, total_pages=1)
        with (
            patch("httpx.get", return_value=get_resp_5_again),
            patch("httpx.post", return_value=post_resp),
        ):
            second_result = provider.discover_repositories(
                cursor=first_result.next_cursor, page_size=3
            )

        assert second_result.repositories[0].name == "group/project3"
        assert second_result.repositories[1].name == "group/project4"

    def test_source_exhausted_no_next(self):
        """Source exhausted: has_next_page=False, next_cursor=None."""
        provider = _make_provider()
        projects = [_gitlab_project(i) for i in range(2)]
        get_resp = _mock_get_response(projects, total=2, total_pages=1)
        post_resp = _mock_post_no_enrich()

        with (
            patch("httpx.get", return_value=get_resp),
            patch("httpx.post", return_value=post_resp),
        ):
            result = provider.discover_repositories(cursor=None, page_size=50)

        assert result.has_next_page is False
        assert result.next_cursor is None
        assert result.partial_due_to_cap is False

    def test_safety_cap_returns_partial(self):
        """Safety cap hit: partial_due_to_cap=True, has_next_page=True."""
        indexed = [f"https://gitlab.com/group/project{i}.git" for i in range(0, 40, 2)]
        provider = _make_provider(indexed_urls=indexed)

        call_idx = [0]
        post_resp = _mock_post_no_enrich()

        def get_side_effect(*args, **kwargs):
            start = call_idx[0] * 4
            projects = [_gitlab_project(start + j) for j in range(4)]
            call_idx[0] += 1
            return _mock_get_response(
                projects, total=100, total_pages=25, next_page=call_idx[0] + 1
            )

        with (
            patch("httpx.get", side_effect=get_side_effect),
            patch("httpx.post", return_value=post_resp),
        ):
            result = provider.discover_repositories(cursor=None, page_size=50)

        assert result.partial_due_to_cap is True
        assert result.has_next_page is True
        assert result.next_cursor is not None
        assert 0 < len(result.repositories) <= 50

    def test_invalid_cursor_silent_restart_from_beginning(self):
        """Invalid cursor: silent restart returns repos from the source beginning."""
        provider = _make_provider()
        projects = [_gitlab_project(0), _gitlab_project(1)]
        get_resp = _mock_get_response(projects, total=2, total_pages=1)
        post_resp = _mock_post_no_enrich()

        with (
            patch("httpx.get", return_value=get_resp),
            patch("httpx.post", return_value=post_resp),
        ):
            result = provider.discover_repositories(
                cursor="garbage_cursor_xyz", page_size=50
            )

        assert len(result.repositories) == 2
        assert result.repositories[0].name == "group/project0"
        assert result.repositories[1].name == "group/project1"
