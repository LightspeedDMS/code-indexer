"""
Tests for min_score default consistency between search paths (Bug #255).

Bug Description:
- _omni_search_code (line 915): min_score preserves None when not provided (no filter)
- search_code global-repo path (line 1156): min_score defaults to 0.5
- search_code activated-repo path (line 1269): min_score defaults to 0.5

Same query without explicit min_score returns ALL results via omni-search but only
results >= 0.5 via single-repo search. Both paths should use None when not provided.

Required Fix:
Both single-repo paths must use:
    min_score=_coerce_float(params.get("min_score"), 0.0) if params.get("min_score") is not None else None
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch, call
from code_indexer.server.mcp.handlers import search_code
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create a real User object for testing."""
    return User(
        username="testuser",
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


def _make_empty_query_result():
    """Return a minimal valid result from query_user_repositories."""
    return {
        "results": [],
        "total_results": 0,
        "query_metadata": {
            "query_text": "test query",
            "execution_time_ms": 10,
            "repositories_searched": 1,
            "timeout_occurred": False,
        },
    }


class TestMinScoreDefaultConsistencyActivatedRepo:
    """Tests for the activated-repo path (line 1269) in search_code."""

    def test_no_min_score_passes_none_to_query_user_repositories(self, mock_user):
        """When min_score is not provided, activated-repo path must pass None."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.semantic_query_manager.query_user_repositories.return_value = (
                _make_empty_query_result()
            )

            params = {
                "query_text": "authentication",
                # No min_score provided - should default to None
            }

            search_code(params, mock_user)

            mock_app.semantic_query_manager.query_user_repositories.assert_called_once()
            call_kwargs = (
                mock_app.semantic_query_manager.query_user_repositories.call_args.kwargs
            )

            assert "min_score" in call_kwargs, "min_score must be in call kwargs"
            assert call_kwargs["min_score"] is None, (
                f"Expected min_score=None when not provided, "
                f"got min_score={call_kwargs['min_score']!r}. "
                f"Bug #255: activated-repo path must not default to 0.5"
            )

    def test_explicit_min_score_zero_passed_through(self, mock_user):
        """When min_score=0 is explicitly provided, it must be passed as 0.0."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.semantic_query_manager.query_user_repositories.return_value = (
                _make_empty_query_result()
            )

            params = {
                "query_text": "authentication",
                "min_score": 0,
            }

            search_code(params, mock_user)

            call_kwargs = (
                mock_app.semantic_query_manager.query_user_repositories.call_args.kwargs
            )

            assert call_kwargs["min_score"] == 0.0, (
                f"Expected min_score=0.0 when explicitly set to 0, "
                f"got {call_kwargs['min_score']!r}"
            )

    def test_explicit_min_score_0_7_passed_through(self, mock_user):
        """When min_score=0.7 is explicitly provided, it must be passed as 0.7."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.semantic_query_manager.query_user_repositories.return_value = (
                _make_empty_query_result()
            )

            params = {
                "query_text": "authentication",
                "min_score": 0.7,
            }

            search_code(params, mock_user)

            call_kwargs = (
                mock_app.semantic_query_manager.query_user_repositories.call_args.kwargs
            )

            assert call_kwargs["min_score"] == 0.7, (
                f"Expected min_score=0.7 when explicitly set, "
                f"got {call_kwargs['min_score']!r}"
            )

    def test_explicit_min_score_string_0_5_passed_through(self, mock_user):
        """When min_score='0.5' is provided as string (MCP protocol), it must be coerced to 0.5."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.semantic_query_manager.query_user_repositories.return_value = (
                _make_empty_query_result()
            )

            params = {
                "query_text": "authentication",
                "min_score": "0.5",  # MCP sends strings
            }

            search_code(params, mock_user)

            call_kwargs = (
                mock_app.semantic_query_manager.query_user_repositories.call_args.kwargs
            )

            assert call_kwargs["min_score"] == 0.5, (
                f"Expected min_score=0.5 when string '0.5' provided, "
                f"got {call_kwargs['min_score']!r}"
            )

    def test_explicit_min_score_0_5_not_confused_with_default(self, mock_user):
        """Explicit min_score=0.5 must be treated as user-provided, not the old broken default."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.semantic_query_manager.query_user_repositories.return_value = (
                _make_empty_query_result()
            )

            params_with_explicit = {
                "query_text": "authentication",
                "min_score": 0.5,
            }
            params_without = {
                "query_text": "authentication",
                # No min_score
            }

            search_code(params_with_explicit, mock_user)
            kwargs_explicit = (
                mock_app.semantic_query_manager.query_user_repositories.call_args.kwargs
            )

            mock_app.semantic_query_manager.query_user_repositories.reset_mock()

            search_code(params_without, mock_user)
            kwargs_without = (
                mock_app.semantic_query_manager.query_user_repositories.call_args.kwargs
            )

            # Explicit 0.5 must produce 0.5, no min_score must produce None
            assert kwargs_explicit["min_score"] == 0.5, (
                f"Expected 0.5 when explicitly provided, got {kwargs_explicit['min_score']!r}"
            )
            assert kwargs_without["min_score"] is None, (
                f"Expected None when not provided, got {kwargs_without['min_score']!r}. "
                f"Bug #255: must not default to 0.5"
            )


class TestMinScoreDefaultConsistencyGlobalRepo:
    """Tests for the global-repo path (line 1156) in search_code.

    The global-repo path calls _perform_search directly on semantic_query_manager.
    This path is harder to mock due to AliasManager/registry lookups, so we use
    source inspection to verify the fix.
    """

    def test_global_repo_path_uses_none_default_in_source(self):
        """Verify the global-repo code path uses None-preserving pattern for min_score."""
        import inspect
        from code_indexer.server.mcp import handlers

        source = inspect.getsource(handlers.search_code)

        # The old broken pattern that must NOT appear in the global-repo path:
        # min_score=_coerce_float(params.get("min_score"), 0.5),
        # Check that 0.5 is not used as the default for min_score in _perform_search call
        lines = source.split("\n")

        broken_pattern_lines = [
            line for line in lines
            if "_coerce_float(params.get(\"min_score\"), 0.5)" in line
            and "min_score=" in line
        ]

        assert len(broken_pattern_lines) == 0, (
            f"Bug #255: Found old broken min_score=0.5 default pattern in search_code:\n"
            + "\n".join(broken_pattern_lines)
        )

    def test_global_repo_path_uses_none_preserving_pattern_in_source(self):
        """Verify the global-repo code path uses the None-preserving pattern."""
        import inspect
        from code_indexer.server.mcp import handlers

        source = inspect.getsource(handlers.search_code)

        # The correct pattern (same as omni-search) must appear:
        # min_score=_coerce_float(params.get("min_score"), 0.0) if params.get("min_score") is not None else None
        assert "is not None else None" in source, (
            "Bug #255: search_code must use the None-preserving pattern for min_score "
            "(same as _omni_search_code)"
        )


class TestMinScoreDefaultConsistencyOmniSearch:
    """Tests verifying the omni-search path preserves correct None behavior (regression guard)."""

    def test_omni_search_no_min_score_uses_none(self, mock_user):
        """Omni-search with no min_score must pass None to MultiSearchRequest."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            # No min_score
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": [], "repo2-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0,
                    total_repos_searched=2,
                    execution_time_ms=100,
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            # Patch config service
            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg:
                mock_cfg_svc = Mock()
                mock_cfg_limits = Mock()
                mock_cfg_limits.omni_max_workers = 4
                mock_cfg_limits.omni_per_repo_timeout_seconds = 30
                mock_cfg_limits.multi_search_max_workers = 4
                mock_cfg_limits.multi_search_timeout_seconds = 30
                mock_cfg_obj = Mock()
                mock_cfg_obj.multi_search_limits_config = mock_cfg_limits
                mock_cfg_svc.get_config.return_value = mock_cfg_obj
                mock_cfg.return_value = mock_cfg_svc

                with patch(
                    "code_indexer.server.mcp.handlers._expand_wildcard_patterns",
                    side_effect=lambda patterns: patterns,
                ):
                    _omni_search_code(params, mock_user)

            call_args = mock_service.search.call_args
            request = call_args[0][0]
            assert request.min_score is None, (
                f"Expected omni-search min_score=None when not provided, "
                f"got {request.min_score!r}"
            )

    def test_omni_search_explicit_min_score_passed_through(self, mock_user):
        """Omni-search with explicit min_score must pass it to MultiSearchRequest."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global"],
            "min_score": 0.7,
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0,
                    total_repos_searched=1,
                    execution_time_ms=50,
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg:
                mock_cfg_svc = Mock()
                mock_cfg_limits = Mock()
                mock_cfg_limits.omni_max_workers = 4
                mock_cfg_limits.omni_per_repo_timeout_seconds = 30
                mock_cfg_limits.multi_search_max_workers = 4
                mock_cfg_limits.multi_search_timeout_seconds = 30
                mock_cfg_obj = Mock()
                mock_cfg_obj.multi_search_limits_config = mock_cfg_limits
                mock_cfg_svc.get_config.return_value = mock_cfg_obj
                mock_cfg.return_value = mock_cfg_svc

                with patch(
                    "code_indexer.server.mcp.handlers._expand_wildcard_patterns",
                    side_effect=lambda patterns: patterns,
                ):
                    _omni_search_code(params, mock_user)

            call_args = mock_service.search.call_args
            request = call_args[0][0]
            assert request.min_score == 0.7, (
                f"Expected omni-search min_score=0.7 when explicitly provided, "
                f"got {request.min_score!r}"
            )
