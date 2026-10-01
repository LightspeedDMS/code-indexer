"""Unit tests for Issue #1601 AC-E -- the read_capped signal must be
threaded through every response surface, not silently dropped.

Covers the MCP regex_search single-repo response here. Omni, REST, CLI,
and xray_search surfaces are covered in their own test files/suites.
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


def _mock_search_result(read_capped: bool):
    result = Mock()
    result.matches = []
    result.total_matches = 0
    result.truncated = False
    result.read_capped = read_capped
    result.search_engine = "ripgrep"
    result.search_time_ms = 50
    return result


class TestMcpSingleRepoReadCappedSurfacing:
    """AC-E: the MCP regex_search single-repo response must expose
    read_capped, not silently drop it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("read_capped_value", [True, False])
    async def test_read_capped_is_surfaced_in_mcp_response(
        self, mock_user, tmp_path, read_capped_value
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
                return_value=_mock_search_result(read_capped_value)
            )
            mock_service_class.return_value = mock_service

            result = await handle_regex_search(args, mock_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["read_capped"] is read_capped_value
