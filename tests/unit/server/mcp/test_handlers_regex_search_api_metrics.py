"""Unit tests for Bug #721: handle_regex_search must call increment_regex_search.

Bug: regex_search is in _SELF_TRACKING_TOOLS so the protocol skips
increment_other_api_call(), but handle_regex_search() never called
increment_regex_search() either — every regex_search call was invisible
in metrics dashboards.

AC1: handle_regex_search() calls api_metrics_service.increment_regex_search(
       username=user.username) exactly once on a successful single-repo call.
AC2: The metric is NOT called when _validate_regex_args fails (early-return path).
AC3: _omni_regex_search loops back through handle_regex_search per repo, so
     N repos => N calls to increment_regex_search.
"""

import json
import pytest
from unittest.mock import Mock, AsyncMock, patch
from code_indexer.server.mcp.handlers import handle_regex_search
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_user():
    """Real-ish User for testing."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_search_result():
    """Minimal successful SearchResult-like object."""
    result = Mock()
    result.matches = []
    result.total_matches = 0
    result.truncated = False
    result.search_engine = "ripgrep"
    result.search_time_ms = 10
    return result


@pytest.fixture
def rerank_meta():
    """Default no-op rerank metadata."""
    return {
        "reranker_used": False,
        "reranker_provider": None,
        "rerank_time_ms": 0,
    }


@pytest.fixture
def mock_legacy():
    """Mock _legacy module with repo path resolution."""
    legacy = Mock()
    legacy._resolve_repo_path.return_value = "/tmp/test/repo"
    return legacy


# ---------------------------------------------------------------------------
# Shared infrastructure fixtures — own the common patch surface
# ---------------------------------------------------------------------------


@pytest.fixture
def base_patches():
    """Patch the two dependencies shared by ALL three test scenarios:
    _get_golden_repos_dir and api_metrics_service.

    Yields: mock_metrics (the patched api_metrics_service mock).
    """
    with (
        patch(
            "code_indexer.server.mcp.handlers.search._get_golden_repos_dir",
            return_value="/tmp/test",
        ),
        patch(
            "code_indexer.server.mcp.handlers.search.api_metrics_service"
        ) as mock_metrics,
    ):
        yield mock_metrics


@pytest.fixture
def success_path_patches(base_patches, mock_legacy, mock_search_result, rerank_meta):
    """Layer the success-path patches on top of base_patches.

    Patches _get_legacy, get_config_service, and _execute_regex_search so that
    the single-repo success path completes without real I/O.

    Yields: mock_metrics (same object as base_patches).
    """
    with (
        patch(
            "code_indexer.server.mcp.handlers.search._get_legacy",
            return_value=mock_legacy,
        ),
        patch("code_indexer.server.mcp.handlers.search.get_config_service"),
        patch(
            "code_indexer.server.mcp.handlers.search._execute_regex_search",
            new_callable=AsyncMock,
            return_value=([], rerank_meta, mock_search_result),
        ),
    ):
        yield base_patches  # propagate the metrics mock to callers


# ---------------------------------------------------------------------------
# AC1: increment_regex_search called once on successful single-repo call
# ---------------------------------------------------------------------------


class TestHandleRegexSearchIncrementsMetric:
    """AC1: Successful single-repo call must invoke increment_regex_search once."""

    @pytest.mark.asyncio
    async def test_handle_regex_search_increments_metric_on_success(
        self, mock_user, success_path_patches
    ):
        """
        handle_regex_search() must call api_metrics_service.increment_regex_search(
        username=user.username) exactly once when the call succeeds.
        """
        args = {"repository_alias": "test-repo-global", "pattern": "def.*test"}
        mock_metrics = success_path_patches

        result = await handle_regex_search(args, mock_user)

        # THEN: increment_regex_search called exactly once with correct username
        mock_metrics.increment_regex_search.assert_called_once_with(
            username=mock_user.username
        )
        # AND: the call succeeded
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True


# ---------------------------------------------------------------------------
# AC2: Metric NOT called when validation fails (early-return path)
# ---------------------------------------------------------------------------


class TestHandleRegexSearchDoesNotIncrementOnValidationFailure:
    """AC2: Metric must NOT be incremented when validation fails."""

    @pytest.mark.asyncio
    async def test_handle_regex_search_does_not_increment_metric_on_validation_failure(
        self, mock_user, base_patches
    ):
        """
        When _validate_regex_args returns an error (early return),
        increment_regex_search must NOT be called.
        """
        # include_patterns must be a list — passing a float causes validation failure
        args = {
            "repository_alias": "test-repo-global",
            "pattern": "def.*test",
            "include_patterns": 123.45,  # Invalid — triggers early-return error
        }
        mock_metrics = base_patches

        result = await handle_regex_search(args, mock_user)

        # THEN: metric NOT called
        mock_metrics.increment_regex_search.assert_not_called()
        # AND: error response returned
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False


# ---------------------------------------------------------------------------
# AC3: omni path — N repos => N increments
# ---------------------------------------------------------------------------


class TestOmniRegexSearchIncrementsMetricPerRepo:
    """AC3: _omni_regex_search loops through handle_regex_search, so each
    repo visit increments the metric exactly once."""

    @pytest.mark.asyncio
    async def test_omni_regex_search_increments_metric_per_repo(
        self, mock_user, success_path_patches
    ):
        """
        When repository_alias is a list of 3 repos, increment_regex_search
        should be called exactly 3 times (once per repo in the loop).
        """
        repo_list = ["repo-a-global", "repo-b-global", "repo-c-global"]
        args = {"repository_alias": repo_list, "pattern": "def.*test"}
        mock_metrics = success_path_patches

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._expand_wildcard_patterns",
                side_effect=lambda aliases, user: aliases,
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._filter_errors_for_user",
                return_value={},
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._format_omni_response",
                return_value={
                    "success": True,
                    "results": [],
                    "total_results": 0,
                    "total_repos_searched": 3,
                    "errors": {},
                },
            ),
        ):
            result = await handle_regex_search(args, mock_user)

        # THEN: increment_regex_search called exactly 3 times (once per repo)
        assert mock_metrics.increment_regex_search.call_count == 3
        for call in mock_metrics.increment_regex_search.call_args_list:
            assert call.kwargs["username"] == mock_user.username
        # AND: overall response is successful
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
