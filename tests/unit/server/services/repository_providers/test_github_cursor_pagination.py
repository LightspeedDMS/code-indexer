"""
Tests for Bug #685: GitHub provider cursor-based filter-fill loop.

All tests verify externally visible behavior only:
- Which repositories are returned
- has_next_page, next_cursor, partial_due_to_cap flags
- Correct resumption when the returned next_cursor is fed back in

Mocking: only httpx.post (the external HTTP boundary) is patched.
"""

from unittest.mock import MagicMock, patch

import httpx


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_provider(indexed_urls=None):
    """Create GitHubProvider with optionally pre-indexed repos."""
    from code_indexer.server.services.repository_providers.github_provider import (
        GitHubProvider,
    )
    from code_indexer.server.services.ci_token_manager import TokenData

    token_manager = MagicMock()
    token_manager.get_token.return_value = TokenData(
        platform="github", token="dummy", base_url=None
    )
    golden_repo_manager = MagicMock()
    golden_repo_manager.list_golden_repos.return_value = [
        {"repo_url": url} for url in (indexed_urls or [])
    ]
    return GitHubProvider(
        token_manager=token_manager, golden_repo_manager=golden_repo_manager
    )


def _repo_node(idx=0):
    """Create a minimal GraphQL repository node for repo at position idx."""
    return {
        "nameWithOwner": f"owner/repo{idx}",
        "name": f"repo{idx}",
        "description": f"Repo {idx}",
        "isPrivate": False,
        "url": f"https://github.com/owner/repo{idx}",
        "sshUrl": f"git@github.com:owner/repo{idx}.git",
        "pushedAt": "2024-01-15T10:30:00Z",
        "defaultBranchRef": {
            "name": "main",
            "target": {"history": {"nodes": []}},
        },
    }


def _mock_httpx_response(nodes, has_next=False, end_cursor=None, total=None):
    """Build a mock httpx.Response for a GraphQL repos query."""
    payload = {
        "data": {
            "viewer": {
                "repositories": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                    "totalCount": total if total is not None else len(nodes),
                    "nodes": nodes,
                }
            }
        }
    }
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = payload
    return mock_resp


def _sequential(responses):
    """Return a side_effect iterator over the given responses."""
    iterator = iter(responses)

    def side_effect(*args, **kwargs):
        return next(iterator)

    return side_effect


# ---------------------------------------------------------------------------
# Filter-fill loop tests
# ---------------------------------------------------------------------------


class TestGitHubFilterFillLoop:
    def test_empty_filter_returns_full_page(self):
        """No indexed repos: single batch fills the page entirely."""
        provider = _make_provider()
        nodes = [_repo_node(i) for i in range(3)]
        resp = _mock_httpx_response(nodes, has_next=False, total=3)

        with patch("httpx.post", return_value=resp):
            result = provider.discover_repositories(cursor=None, page_size=3)

        assert len(result.repositories) == 3
        assert result.has_next_page is False
        assert result.next_cursor is None
        assert result.partial_due_to_cap is False

    def test_all_indexed_first_batch_fetches_next_batch(self):
        """When first batch is all indexed, loop pulls the next batch."""
        indexed = [f"https://github.com/owner/repo{i}.git" for i in range(2)]
        provider = _make_provider(indexed_urls=indexed)

        first_nodes = [_repo_node(i) for i in range(2)]
        second_nodes = [_repo_node(10), _repo_node(11)]

        resps = [
            _mock_httpx_response(first_nodes, has_next=True, end_cursor="c2", total=4),
            _mock_httpx_response(second_nodes, has_next=False, total=4),
        ]

        with patch("httpx.post", side_effect=_sequential(resps)):
            result = provider.discover_repositories(cursor=None, page_size=2)

        assert len(result.repositories) == 2
        assert result.repositories[0].name == "owner/repo10"
        assert result.repositories[1].name == "owner/repo11"
        assert result.has_next_page is False

    def test_target_mid_batch_produces_resumable_cursor(self):
        """page_size=3 from 5-node batch: cursor allows resuming at item 3."""
        provider = _make_provider()
        # First call: 5 nodes, page_size=3, returns 3 repos + cursor
        nodes_5 = [_repo_node(i) for i in range(5)]
        resp_5 = _mock_httpx_response(
            nodes_5, has_next=True, end_cursor="end_c", total=10
        )

        with patch("httpx.post", return_value=resp_5):
            first_result = provider.discover_repositories(cursor=None, page_size=3)

        assert len(first_result.repositories) == 3
        assert first_result.has_next_page is True
        assert first_result.next_cursor is not None

        # Second call with that cursor: same batch still available; must return items 3 and 4
        resp_5_again = _mock_httpx_response(
            nodes_5, has_next=False, end_cursor=None, total=10
        )
        with patch("httpx.post", return_value=resp_5_again):
            second_result = provider.discover_repositories(
                cursor=first_result.next_cursor, page_size=3
            )

        assert second_result.repositories[0].name == "owner/repo3"
        assert second_result.repositories[1].name == "owner/repo4"

    def test_target_on_last_item_cursor_fetches_next_source_batch(self):
        """Hitting page_size on the last item of a batch: next call fetches next source batch."""
        provider = _make_provider()
        # Exactly 3 nodes, page_size=3: last item consumed, cursor must advance source
        nodes_3 = [_repo_node(i) for i in range(3)]
        next_nodes = [_repo_node(i) for i in range(3, 6)]

        first_resp = _mock_httpx_response(
            nodes_3, has_next=True, end_cursor="next_c", total=6
        )

        with patch("httpx.post", return_value=first_resp):
            first_result = provider.discover_repositories(cursor=None, page_size=3)

        assert len(first_result.repositories) == 3
        assert first_result.has_next_page is True
        assert first_result.next_cursor is not None

        # Second call must fetch next batch (not replay the first 3 items)
        second_resp = _mock_httpx_response(next_nodes, has_next=False, total=6)
        with patch("httpx.post", return_value=second_resp):
            second_result = provider.discover_repositories(
                cursor=first_result.next_cursor, page_size=3
            )

        assert second_result.repositories[0].name == "owner/repo3"
        assert second_result.repositories[1].name == "owner/repo4"
        assert second_result.repositories[2].name == "owner/repo5"

    def test_safety_cap_returns_partial(self):
        """Safety cap hit: partial page with partial_due_to_cap=True and has_next_page=True."""
        # Index even-numbered repos so each batch of 4 only yields 2 unindexed
        indexed = [f"https://github.com/owner/repo{i}.git" for i in range(0, 40, 2)]
        provider = _make_provider(indexed_urls=indexed)

        call_idx = [0]

        def post_side_effect(*args, **kwargs):
            start = call_idx[0] * 4
            nodes = [_repo_node(start + j) for j in range(4)]
            call_idx[0] += 1
            return _mock_httpx_response(
                nodes, has_next=True, end_cursor=f"c{call_idx[0]}", total=100
            )

        with patch("httpx.post", side_effect=post_side_effect):
            result = provider.discover_repositories(cursor=None, page_size=50)

        assert result.partial_due_to_cap is True
        assert result.has_next_page is True
        assert result.next_cursor is not None
        assert 0 < len(result.repositories) <= 50

    def test_source_exhausted_no_next(self):
        """Source exhausted: has_next_page=False, next_cursor=None."""
        provider = _make_provider()
        nodes = [_repo_node(i) for i in range(2)]
        resp = _mock_httpx_response(nodes, has_next=False, total=2)

        with patch("httpx.post", return_value=resp):
            result = provider.discover_repositories(cursor=None, page_size=50)

        assert result.has_next_page is False
        assert result.next_cursor is None
        assert result.partial_due_to_cap is False

    def test_invalid_cursor_silent_restart_from_beginning(self):
        """Invalid cursor: silent restart; returns repos starting from source beginning."""
        provider = _make_provider()
        nodes = [_repo_node(0), _repo_node(1)]
        resp = _mock_httpx_response(nodes, has_next=False, total=2)

        with patch("httpx.post", return_value=resp):
            result = provider.discover_repositories(
                cursor="garbage_cursor_xyz", page_size=50
            )

        # Must not raise; must return repos from position 0 of the source
        assert len(result.repositories) == 2
        assert result.repositories[0].name == "owner/repo0"
        assert result.repositories[1].name == "owner/repo1"
