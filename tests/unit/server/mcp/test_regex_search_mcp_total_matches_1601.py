"""Unit test for Issue #1601 Priority 7 -- the MCP single-repo regex_search
response must propagate the service's own lower-bound ``total_matches``
sentinel, not silently discard it in favor of the returned page size.

``RegexSearchResult.total_matches`` is documented (regex_search.py) to
become a LOWER-BOUND SENTINEL (``max_results + 1``) once the scan stops
early due to ``truncated`` -- deliberately larger than
``len(result.matches)`` (the actual page returned). The MCP handler must
surface that sentinel value, not recompute (and understate) it via
``len(matches)``.
"""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from code_indexer.server.mcp.handlers import handle_regex_search
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _mock_search_result_with_sentinel_total():
    """A capped search result: the service reports 3 matches (the page
    returned to the caller) but a total_matches SENTINEL of 101 (i.e.
    max_results=100 was reached and scanning stopped, per
    RegexSearchResult's own documented lower-bound-sentinel contract)."""
    match = Mock()
    match.file_path = "src/foo.py"
    match.line_number = 1
    match.column = 1
    match.line_content = "def foo():"
    match.context_before = []
    match.context_after = []

    result = Mock()
    result.matches = [match, match, match]  # page of 3
    result.total_matches = 101  # max_results(100) + 1 sentinel
    result.truncated = True
    result.read_capped = False
    result.search_engine = "ripgrep"
    result.search_time_ms = 50
    return result


class TestMcpSingleRepoTotalMatchesSentinel:
    """Priority 7: the MCP single-repo response's total_matches must be
    the service's own sr.total_matches, not len(matches)."""

    @pytest.mark.asyncio
    async def test_total_matches_uses_service_sentinel_not_page_length(
        self, mock_user, tmp_path
    ):
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "def.*test",
        }
        golden_repos_dir = str(tmp_path / "golden-repos")
        repo_path = str(tmp_path / "golden-repos" / "test-repo-global")

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value=golden_repos_dir,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value=repo_path,
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
            patch(
                "code_indexer.global_repos.regex_search.RegexSearchService"
            ) as mock_service_class,
        ):
            mock_service = AsyncMock()
            mock_service.search = AsyncMock(
                return_value=_mock_search_result_with_sentinel_total()
            )
            mock_service_class.return_value = mock_service

            result = await handle_regex_search(args, mock_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert len(data["matches"]) == 3, "the returned page itself is unaffected"
        assert data["total_matches"] == 101, (
            "total_matches must be the service's own lower-bound sentinel "
            "(101), not len(matches) (3) -- discarding it silently drops "
            "the deliberate 'more matches exist than shown' signal"
        )
