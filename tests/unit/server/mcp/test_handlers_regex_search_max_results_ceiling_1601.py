"""Issue #1601 remediation round 5, Priority 1 (REQUIRED -- Codex Critical
finding, third sub-finding).

The tool doc/schema (``src/code_indexer/server/mcp/tool_docs/search/
regex_search.md``) documents ``max_results``'s ``inputSchema`` as
``minimum: 1, maximum: 1000``. But ``_execute_regex_search_impl`` in
``search.py`` only ever clamped the LOWER bound:

    max_results = max(1, _coerce_int(args.get("max_results"), _DEFAULT_REGEX_MAX_RESULTS))

There is no corresponding upper clamp. A caller requesting an absurdly
large ``max_results`` (e.g. 1_000_000) is passed straight through to
``RegexSearchService.search()`` unclamped -- defeating any per-request
memory budget a caller might rely on the documented ceiling to provide
(including the new aggregate result-content budget from this same
remediation round, since a much larger ``max_results`` means many more
matches can accumulate before that budget itself trips).

This test proves the gap by mocking ``RegexSearchService`` entirely
(mirroring the established pattern in
``test_handlers_regex_search_validation.py``) and asserting on the
ACTUAL ``max_results`` value the handler passed to ``service.search()``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import handle_regex_search

_DOCUMENTED_MAX_RESULTS_CEILING = 1000


@pytest.fixture
def mock_user():
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _mock_regex_search_result():
    mock_result = Mock()
    mock_result.matches = []
    mock_result.total_matches = 0
    mock_result.truncated = False
    mock_result.read_capped = False
    mock_result.search_engine = "test"
    mock_result.search_time_ms = 100
    return mock_result


async def _invoke_and_capture_max_results(args, mock_user) -> int:
    """Call handle_regex_search with RegexSearchService mocked out entirely
    (mirrors test_handlers_regex_search_validation.py's established
    pattern) and return the ACTUAL max_results value the handler passed
    to service.search()."""
    mock_service = AsyncMock()
    mock_service.search = AsyncMock(return_value=_mock_regex_search_result())

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
            "code_indexer.global_repos.regex_search.RegexSearchService",
            return_value=mock_service,
        ),
    ):
        await handle_regex_search(args, mock_user)

    assert mock_service.search.await_args is not None, (
        "RegexSearchService.search() was never called"
    )
    return int(mock_service.search.await_args.kwargs["max_results"])


class TestRegexSearchMaxResultsCeiling:
    """Priority 1 (third sub-finding): the documented max_results ceiling
    (1000) must be enforced at the MCP handler boundary, not just the
    lower bound."""

    @pytest.mark.asyncio
    async def test_absurdly_large_max_results_is_clamped_to_documented_ceiling(
        self, mock_user
    ):
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "test.*pattern",
            "max_results": 1_000_000,
        }

        called_max_results = await _invoke_and_capture_max_results(args, mock_user)

        assert called_max_results == _DOCUMENTED_MAX_RESULTS_CEILING, (
            f"max_results={called_max_results} was passed to "
            f"RegexSearchService.search() -- expected it clamped to "
            f"exactly {_DOCUMENTED_MAX_RESULTS_CEILING} "
            f"(regex_search.md inputSchema.max_results.maximum)"
        )

    @pytest.mark.asyncio
    async def test_max_results_within_ceiling_passes_through_unchanged(self, mock_user):
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "test.*pattern",
            "max_results": 250,
        }

        called_max_results = await _invoke_and_capture_max_results(args, mock_user)

        assert called_max_results == 250


_DOCUMENTED_CONTEXT_LINES_CEILING = 10


async def _invoke_and_capture_context_lines(args, mock_user) -> int:
    """Call handle_regex_search with RegexSearchService mocked out entirely
    and return the ACTUAL context_lines value the handler passed to
    service.search()."""
    mock_service = AsyncMock()
    mock_service.search = AsyncMock(return_value=_mock_regex_search_result())

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
            "code_indexer.global_repos.regex_search.RegexSearchService",
            return_value=mock_service,
        ),
    ):
        await handle_regex_search(args, mock_user)

    assert mock_service.search.await_args is not None, (
        "RegexSearchService.search() was never called"
    )
    return int(mock_service.search.await_args.kwargs["context_lines"])


class TestRegexSearchContextLinesCeiling:
    """Issue #1601 remediation round 5 (Codex finding, other half of the
    multiplicand): the documented context_lines ceiling (10, per
    regex_search.md's inputSchema.context_lines.maximum and the REST
    route's Field(le=10)) must be enforced at the MCP handler boundary,
    not just the lower bound."""

    @pytest.mark.asyncio
    async def test_absurdly_large_context_lines_is_clamped_to_documented_ceiling(
        self, mock_user
    ):
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "test.*pattern",
            "context_lines": 1_000_000,
        }

        called_context_lines = await _invoke_and_capture_context_lines(args, mock_user)

        assert called_context_lines == _DOCUMENTED_CONTEXT_LINES_CEILING, (
            f"context_lines={called_context_lines} was passed to "
            f"RegexSearchService.search() -- expected it clamped to "
            f"exactly {_DOCUMENTED_CONTEXT_LINES_CEILING} "
            f"(regex_search.md inputSchema.context_lines.maximum)"
        )

    @pytest.mark.asyncio
    async def test_context_lines_within_ceiling_passes_through_unchanged(
        self, mock_user
    ):
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "test.*pattern",
            "context_lines": 3,
        }

        called_context_lines = await _invoke_and_capture_context_lines(args, mock_user)

        assert called_context_lines == 3
