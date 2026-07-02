"""Unit tests for Bug #477: regex_search handler passes context_lines as string.

Bug: TypeError: '>' not supported between instances of 'str' and 'int'
Root cause: MCP tool arguments come as strings from JSON. context_lines must
be converted to int() before passing to the search service.
"""

import json
import pytest
from unittest.mock import Mock, patch, AsyncMock
from code_indexer.server.mcp.handlers import handle_regex_search
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create a mock user with query permissions."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_search_result():
    """Create a mock search result with zero matches."""
    result = Mock()
    result.matches = []
    result.total_matches = 0
    result.truncated = False
    result.search_engine = "ripgrep"
    result.search_time_ms = 50
    return result


class TestRegexSearchContextLinesStringType:
    """Test that context_lines as a string does not cause TypeError (Bug #477)."""

    @pytest.mark.asyncio
    async def test_context_lines_string_does_not_raise_typeerror(
        self, mock_user, mock_search_result
    ):
        """Bug #477: context_lines="3" (string from MCP JSON) must not raise TypeError.

        The handler must convert context_lines to int before passing to service.
        """
        # GIVEN: context_lines passed as a string (as MCP JSON args provide them)
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "def.*test",
            "context_lines": "3",  # String, not int - as MCP JSON args arrive
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/tmp/test",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value="/tmp/test/repo",
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
            patch(
                "code_indexer.global_repos.regex_search.RegexSearchService"
            ) as mock_service_class,
        ):
            mock_service = AsyncMock()
            mock_service.search = AsyncMock(return_value=mock_search_result)
            mock_service_class.return_value = mock_service

            # WHEN: handle_regex_search is called with string context_lines
            # THEN: Should NOT raise TypeError
            result = await handle_regex_search(args, mock_user)

        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        # Should not be a TypeError failure
        if not parsed.get("success"):
            error_msg = parsed.get("error", "")
            assert "typeerror" not in error_msg.lower(), f"Got TypeError: {error_msg}"
            assert "'>' not supported" not in error_msg.lower(), (
                f"Got comparison TypeError: {error_msg}"
            )

    @pytest.mark.asyncio
    async def test_context_lines_string_zero_does_not_raise(
        self, mock_user, mock_search_result
    ):
        """Bug #477: context_lines="0" (string) must not raise TypeError."""
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "def.*test",
            "context_lines": "0",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/tmp/test",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value="/tmp/test/repo",
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
            patch(
                "code_indexer.global_repos.regex_search.RegexSearchService"
            ) as mock_service_class,
        ):
            mock_service = AsyncMock()
            mock_service.search = AsyncMock(return_value=mock_search_result)
            mock_service_class.return_value = mock_service

            result = await handle_regex_search(args, mock_user)

        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        if not parsed.get("success"):
            error_msg = parsed.get("error", "")
            assert "'>' not supported" not in error_msg.lower()

    @pytest.mark.asyncio
    async def test_context_lines_int_still_works(self, mock_user, mock_search_result):
        """Regression: context_lines as int must continue to work after fix."""
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "def.*test",
            "context_lines": 5,  # Normal int usage
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/tmp/test",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value="/tmp/test/repo",
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
            patch(
                "code_indexer.global_repos.regex_search.RegexSearchService"
            ) as mock_service_class,
        ):
            mock_service = AsyncMock()
            mock_service.search = AsyncMock(return_value=mock_search_result)
            mock_service_class.return_value = mock_service

            result = await handle_regex_search(args, mock_user)

        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        if not parsed.get("success"):
            error_msg = parsed.get("error", "")
            assert "'>' not supported" not in error_msg.lower()

    @pytest.mark.asyncio
    async def test_context_lines_service_receives_int_not_string(
        self, mock_user, mock_search_result
    ):
        """Bug #477: The service must receive context_lines as int, not string."""
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "def.*test",
            "context_lines": "7",  # String input
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/tmp/test",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value="/tmp/test/repo",
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
            patch(
                "code_indexer.global_repos.regex_search.RegexSearchService"
            ) as mock_service_class,
        ):
            mock_service = AsyncMock()
            mock_service.search = AsyncMock(return_value=mock_search_result)
            mock_service_class.return_value = mock_service

            await handle_regex_search(args, mock_user)

            # THEN: service.search must have been called with int, not string
            mock_service.search.assert_called_once()
            call_kwargs = mock_service.search.call_args.kwargs
            context_lines_passed = call_kwargs.get("context_lines")
            assert isinstance(context_lines_passed, int), (
                f"Expected int but got {type(context_lines_passed)}: {context_lines_passed}"
            )
            assert context_lines_passed == 7
