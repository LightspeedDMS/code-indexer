"""Unit tests for regex_search handler input validation.

Tests for GitHub Issue #130 - regex_search handler must validate input types
for include_patterns and exclude_patterns to prevent TypeError crashes.
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
def base_args():
    """Base arguments for regex_search with required parameters."""
    return {
        "repository_alias": "test-repo-global",
        "pattern": "test.*pattern"
    }


class TestRegexSearchInputValidation:
    """Test input validation for include_patterns and exclude_patterns parameters."""

    @pytest.mark.asyncio
    async def test_include_patterns_float_returns_error(self, mock_user, base_args):
        """Float value for include_patterns should return error, not crash with TypeError."""
        # GIVEN: include_patterns is a float (invalid type)
        args = {**base_args, "include_patterns": 123.45}

        # Mock dependencies to reach validation code
        with patch('code_indexer.server.mcp.handlers._get_golden_repos_dir', return_value="/tmp/test"), \
             patch('code_indexer.server.mcp.handlers._resolve_repo_path', return_value="/tmp/test/repo"), \
             patch('code_indexer.server.mcp.handlers.get_config_service'):

            # WHEN: handle_regex_search is called
            result = await handle_regex_search(args, mock_user)

        # THEN: Should return error response, not crash
        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        assert parsed["success"] is False
        assert "error" in parsed
        assert "include_patterns" in parsed["error"].lower()
        assert "list" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_include_patterns_string_returns_error(self, mock_user, base_args):
        """String value for include_patterns should return error (expects list)."""
        # GIVEN: include_patterns is a string (invalid type)
        args = {**base_args, "include_patterns": "*.py"}

        # Mock dependencies to reach validation code
        with patch('code_indexer.server.mcp.handlers._get_golden_repos_dir', return_value="/tmp/test"), \
             patch('code_indexer.server.mcp.handlers._resolve_repo_path', return_value="/tmp/test/repo"), \
             patch('code_indexer.server.mcp.handlers.get_config_service'):

            # WHEN: handle_regex_search is called
            result = await handle_regex_search(args, mock_user)

        # THEN: Should return error response
        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        assert parsed["success"] is False
        assert "error" in parsed
        assert "include_patterns" in parsed["error"].lower()
        assert "list" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_exclude_patterns_float_returns_error(self, mock_user, base_args):
        """Float value for exclude_patterns should return error, not crash with TypeError."""
        # GIVEN: exclude_patterns is a float (invalid type)
        args = {**base_args, "exclude_patterns": 456.78}

        # Mock dependencies to reach validation code
        with patch('code_indexer.server.mcp.handlers._get_golden_repos_dir', return_value="/tmp/test"), \
             patch('code_indexer.server.mcp.handlers._resolve_repo_path', return_value="/tmp/test/repo"), \
             patch('code_indexer.server.mcp.handlers.get_config_service'):

            # WHEN: handle_regex_search is called
            result = await handle_regex_search(args, mock_user)

        # THEN: Should return error response, not crash
        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        assert parsed["success"] is False
        assert "error" in parsed
        assert "exclude_patterns" in parsed["error"].lower()
        assert "list" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_exclude_patterns_string_returns_error(self, mock_user, base_args):
        """String value for exclude_patterns should return error (expects list)."""
        # GIVEN: exclude_patterns is a string (invalid type)
        args = {**base_args, "exclude_patterns": "*.pyc"}

        # Mock dependencies to reach validation code
        with patch('code_indexer.server.mcp.handlers._get_golden_repos_dir', return_value="/tmp/test"), \
             patch('code_indexer.server.mcp.handlers._resolve_repo_path', return_value="/tmp/test/repo"), \
             patch('code_indexer.server.mcp.handlers.get_config_service'):

            # WHEN: handle_regex_search is called
            result = await handle_regex_search(args, mock_user)

        # THEN: Should return error response
        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        assert parsed["success"] is False
        assert "error" in parsed
        assert "exclude_patterns" in parsed["error"].lower()
        assert "list" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_list_patterns_succeeds(self, mock_user, base_args):
        """Valid list values for patterns should not trigger validation errors."""
        # GIVEN: Valid list values for both patterns
        args = {
            **base_args,
            "include_patterns": ["*.py", "*.js"],
            "exclude_patterns": ["*.pyc", "*.min.js"]
        }

        # Mock the underlying service to avoid actual search
        mock_result = Mock()
        mock_result.matches = []
        mock_result.total_matches = 0
        mock_result.truncated = False
        mock_result.search_engine = "test"
        mock_result.search_time_ms = 100

        with patch('code_indexer.server.mcp.handlers._get_golden_repos_dir', return_value="/tmp/test"), \
             patch('code_indexer.server.mcp.handlers._resolve_repo_path', return_value="/tmp/test/repo"), \
             patch('code_indexer.server.mcp.handlers.get_config_service'), \
             patch('code_indexer.global_repos.regex_search.RegexSearchService') as mock_service_class:

            mock_service = AsyncMock()
            mock_service.search = AsyncMock(return_value=mock_result)
            mock_service_class.return_value = mock_service

            # WHEN: handle_regex_search is called with valid lists
            result = await handle_regex_search(args, mock_user)

        # THEN: Should NOT return validation error
        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        # If validation passed, error should not mention patterns validation
        if not parsed.get("success"):
            error_msg = parsed.get("error", "")
            assert not ("include_patterns" in error_msg.lower() and "list" in error_msg.lower())
            assert not ("exclude_patterns" in error_msg.lower() and "list" in error_msg.lower())

    @pytest.mark.asyncio
    async def test_none_patterns_succeeds(self, mock_user, base_args):
        """None values (omitted parameters) should be valid."""
        # GIVEN: No include_patterns or exclude_patterns provided (None)
        args = {**base_args}  # Only required parameters

        # Mock the underlying service
        mock_result = Mock()
        mock_result.matches = []
        mock_result.total_matches = 0
        mock_result.truncated = False
        mock_result.search_engine = "test"
        mock_result.search_time_ms = 100

        with patch('code_indexer.server.mcp.handlers._get_golden_repos_dir', return_value="/tmp/test"), \
             patch('code_indexer.server.mcp.handlers._resolve_repo_path', return_value="/tmp/test/repo"), \
             patch('code_indexer.server.mcp.handlers.get_config_service'), \
             patch('code_indexer.global_repos.regex_search.RegexSearchService') as mock_service_class:

            mock_service = AsyncMock()
            mock_service.search = AsyncMock(return_value=mock_result)
            mock_service_class.return_value = mock_service

            # WHEN: handle_regex_search is called with no patterns
            result = await handle_regex_search(args, mock_user)

        # THEN: Should NOT return validation error
        assert result is not None
        data = result.get("content", [{}])[0].get("text", "{}")
        parsed = json.loads(data)
        # If validation passed, error should not mention patterns validation
        if not parsed.get("success"):
            error_msg = parsed.get("error", "")
            assert not ("include_patterns" in error_msg.lower() and "list" in error_msg.lower())
            assert not ("exclude_patterns" in error_msg.lower() and "list" in error_msg.lower())
