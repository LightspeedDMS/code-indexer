"""
Unit tests for Story #593: Multi-Provider Query Strategy Controls.

Tests cover:
- source_provider field added to QueryResult dataclass (default value, setter, to_dict)
- preferred_provider parameter wiring through query_user_repositories, _perform_search,
  and _search_single_repository signatures
- SPECIFIC strategy routing: valid preferred_provider routes to named provider and
  annotates results; missing/empty preferred_provider raises ValueError
- primary_only strategy: results are annotated with non-empty source_provider
- MCP handler search_code: preferred_provider extracted from params and passed through
- Error responses for missing preferred_provider in SPECIFIC strategy

Design note: Only true external dependencies (SemanticSearchService) are mocked.
Real temp directories (no proxy_mode config) ensure _is_composite_repository
returns False naturally without any SUT mocking.
"""

import os
import pytest
import tempfile
import shutil
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    QueryResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_repo() -> str:
    """Create a temp directory that acts as a normal (non-composite) repo.

    _is_composite_repository reads .code-indexer/config.json for proxy_mode.
    A directory without that file returns False naturally.
    """
    return tempfile.mkdtemp()


def _make_manager() -> SemanticQueryManager:
    """Create a SemanticQueryManager with mocked infrastructure dependencies."""
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.data_dir = "/fake/data"
    manager.query_timeout_seconds = 30
    manager.max_concurrent_queries_per_user = 5
    manager.max_results_per_query = 100
    manager._active_queries_per_user = {}
    import logging

    manager.logger = logging.getLogger(__name__)

    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    manager.activated_repo_manager = mock_arm
    manager.background_job_manager = MagicMock()
    return manager


def _make_fake_user():
    """Create a valid User object for MCP handler tests."""
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="testuser",
        role=UserRole.ADMIN,
        email="test@example.com",
        password_hash="fakehash",
        created_at=datetime.now(timezone.utc),
    )


def _fake_search_response(query: str = "auth", file_path: str = "src/auth.py"):
    """Build a SemanticSearchResponse with one result item."""
    from code_indexer.server.models.api_models import (
        SemanticSearchResponse,
        SearchResultItem,
    )

    item = SearchResultItem(
        file_path=file_path,
        line_start=10,
        line_end=12,
        score=0.85,
        content="def authenticate(): pass",
        language="python",
    )
    return SemanticSearchResponse(query=query, results=[item], total=1)


# ---------------------------------------------------------------------------
# Tests: QueryResult.source_provider field
# ---------------------------------------------------------------------------


class TestQueryResultSourceProvider:
    """Tests that QueryResult has source_provider field."""

    def test_query_result_has_source_provider_field(self):
        """QueryResult dataclass must have source_provider field."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
        )
        assert hasattr(result, "source_provider"), (
            "QueryResult must have source_provider field"
        )

    def test_query_result_source_provider_defaults_to_empty_string(self):
        """source_provider defaults to empty string when not set."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
        )
        assert result.source_provider == ""

    def test_query_result_source_provider_can_be_set(self):
        """source_provider can be set to a provider name."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
            source_provider="voyage-ai",
        )
        assert result.source_provider == "voyage-ai"

    def test_query_result_to_dict_includes_source_provider(self):
        """to_dict() must include source_provider field."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
            source_provider="cohere",
        )
        d = result.to_dict()
        assert "source_provider" in d
        assert d["source_provider"] == "cohere"

    def test_query_result_to_dict_source_provider_empty_by_default(self):
        """to_dict() includes source_provider as empty string by default."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
        )
        d = result.to_dict()
        assert "source_provider" in d
        assert d["source_provider"] == ""


# ---------------------------------------------------------------------------
# Tests: SPECIFIC strategy routing
# ---------------------------------------------------------------------------


class TestSpecificStrategyRouting:
    """Tests for query_strategy='specific' routing in SemanticQueryManager."""

    def setup_method(self):
        self.repo_path = _make_temp_repo()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_specific_strategy_raises_value_error_when_no_preferred_provider(self):
        """SPECIFIC strategy without preferred_provider raises ValueError.

        _is_composite_repository returns False naturally (no config file present).
        The ValueError is raised before any external call is made.
        """
        manager = _make_manager()

        with pytest.raises(
            ValueError, match="preferred_provider required for specific strategy"
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="specific",
                preferred_provider=None,
            )

    def test_specific_strategy_raises_value_error_when_empty_preferred_provider(self):
        """SPECIFIC strategy with empty string preferred_provider raises ValueError."""
        manager = _make_manager()

        with pytest.raises(
            ValueError, match="preferred_provider required for specific strategy"
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="specific",
                preferred_provider="",
            )

    def test_specific_strategy_routes_to_named_provider_via_search_service(self):
        """SPECIFIC strategy calls search_repository_path_with_provider with named provider.

        SemanticSearchService is the external dependency being mocked.
        Observable outcome: search_repository_path_with_provider called with provider_name.
        """
        manager = _make_manager()

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService"
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.search_repository_path_with_provider.return_value = (
                _fake_search_response()
            )
            mock_svc_cls.return_value = mock_svc

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="specific",
                preferred_provider="cohere",
            )

        mock_svc.search_repository_path_with_provider.assert_called_once()
        call_kwargs = mock_svc.search_repository_path_with_provider.call_args.kwargs
        assert call_kwargs.get("provider_name") == "cohere"

        assert len(results) == 1
        assert results[0].source_provider == "cohere"

    def test_specific_strategy_sets_source_provider_on_all_results(self):
        """SPECIFIC strategy annotates every result with source_provider=provider_name."""
        from code_indexer.server.models.api_models import (
            SemanticSearchResponse,
            SearchResultItem,
        )

        manager = _make_manager()

        multi_response = SemanticSearchResponse(
            query="auth",
            results=[
                SearchResultItem(
                    file_path="a.py",
                    line_start=1,
                    line_end=2,
                    score=0.9,
                    content="a",
                    language="python",
                ),
                SearchResultItem(
                    file_path="b.py",
                    line_start=1,
                    line_end=2,
                    score=0.8,
                    content="b",
                    language="python",
                ),
            ],
            total=2,
        )

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService"
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.search_repository_path_with_provider.return_value = multi_response
            mock_svc_cls.return_value = mock_svc

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="specific",
                preferred_provider="voyage-ai",
            )

        assert len(results) == 2
        for r in results:
            assert r.source_provider == "voyage-ai", (
                f"Expected source_provider='voyage-ai', got '{r.source_provider}'"
            )

    def test_primary_only_strategy_annotates_results_with_source_provider(self):
        """primary_only strategy annotates results with non-empty source_provider.

        Mocks SemanticSearchService (true external dependency) to return one result,
        then asserts the returned QueryResult has non-empty source_provider.
        """
        manager = _make_manager()

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService"
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.search_repository_path.return_value = _fake_search_response()
            mock_svc_cls.return_value = mock_svc

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="primary_only",
                preferred_provider=None,
            )

        assert len(results) == 1
        assert results[0].source_provider != "", (
            "primary_only results must have non-empty source_provider"
        )
        mock_svc.search_repository_path.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: parameter wiring through the call chain
# ---------------------------------------------------------------------------


class TestPreferredProviderParameterWiring:
    """Tests that preferred_provider is wired through the call chain signatures."""

    def test_query_user_repositories_accepts_preferred_provider(self):
        """query_user_repositories signature must accept preferred_provider param."""
        import inspect

        sig = inspect.signature(SemanticQueryManager.query_user_repositories)
        assert "preferred_provider" in sig.parameters, (
            "query_user_repositories must accept preferred_provider parameter"
        )

    def test_perform_search_accepts_preferred_provider(self):
        """_perform_search signature must accept preferred_provider param."""
        import inspect

        sig = inspect.signature(SemanticQueryManager._perform_search)
        assert "preferred_provider" in sig.parameters, (
            "_perform_search must accept preferred_provider parameter"
        )

    def test_search_single_repository_accepts_preferred_provider(self):
        """_search_single_repository signature must accept preferred_provider param."""
        import inspect

        sig = inspect.signature(SemanticQueryManager._search_single_repository)
        assert "preferred_provider" in sig.parameters, (
            "_search_single_repository must accept preferred_provider parameter"
        )


# ---------------------------------------------------------------------------
# Tests: MCP handler preferred_provider extraction
# ---------------------------------------------------------------------------


class TestMcpHandlerPreferredProviderExtraction:
    """Tests that the MCP search_code handler extracts preferred_provider."""

    def test_search_code_passes_preferred_provider_to_perform_search(self):
        """search_code handler must pass preferred_provider to _perform_search.

        Uses real temp directories so:
        - os.path.exists(global_repo_path) passes
        - AliasManager.__init__'s mkdir(parents=True) succeeds
        Returns QueryResult objects so r.to_dict() succeeds in the handler.
        """
        from code_indexer.server.mcp import handlers as h

        user = _make_fake_user()

        base_dir = tempfile.mkdtemp()
        repo_dir = os.path.join(base_dir, "repo")
        os.makedirs(repo_dir)
        golden_dir = os.path.join(base_dir, "golden")
        os.makedirs(golden_dir)

        try:
            fake_results = [
                QueryResult(
                    file_path="src/auth.py",
                    line_number=1,
                    code_snippet="def auth(): pass",
                    similarity_score=0.9,
                    repository_alias="test-global",
                    source_provider="cohere",
                )
            ]

            mock_manager = MagicMock()
            mock_manager._perform_search.return_value = fake_results

            params = {
                "query_text": "authentication",
                "repository_alias": "test-global",
                "query_strategy": "specific",
                "preferred_provider": "cohere",
                "score_fusion": None,
            }

            mock_alias_manager = MagicMock()
            mock_alias_manager.read_alias.return_value = repo_dir
            mock_repo_entry = {"alias_name": "test-global", "repo_name": "test"}

            with patch.object(h, "_list_global_repos", return_value=[mock_repo_entry]):
                with patch.object(h, "_get_golden_repos_dir", return_value=golden_dir):
                    with patch(
                        "code_indexer.global_repos.alias_manager.AliasManager",
                        return_value=mock_alias_manager,
                    ):
                        with patch(
                            "code_indexer.server.mcp.handlers.app_module"
                        ) as mock_app:
                            mock_app.semantic_query_manager = mock_manager
                            with patch.object(
                                h, "_get_query_tracker", return_value=None
                            ):
                                with patch.object(
                                    h,
                                    "_get_access_filtering_service",
                                    return_value=None,
                                ):
                                    with patch.object(
                                        h,
                                        "_apply_payload_truncation",
                                        side_effect=lambda x: x,
                                    ):
                                        with patch.object(
                                            h, "_is_temporal_query", return_value=False
                                        ):
                                            with patch.object(
                                                h,
                                                "_get_wiki_enabled_repos",
                                                return_value=set(),
                                            ):
                                                h.search_code(params, user)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        assert mock_manager._perform_search.called, (
            "_perform_search must have been called"
        )
        call_kwargs = mock_manager._perform_search.call_args.kwargs
        assert "preferred_provider" in call_kwargs, (
            "preferred_provider must be in _perform_search kwargs"
        )
        assert call_kwargs["preferred_provider"] == "cohere"

    def test_search_code_handles_missing_preferred_provider(self):
        """search_code handler must handle absent preferred_provider gracefully."""
        from code_indexer.server.mcp import handlers as h

        user = _make_fake_user()

        params = {
            "query_text": "authentication",
            "repository_alias": "test-global",
            "query_strategy": "specific",
            # preferred_provider intentionally absent
        }

        mock_manager = MagicMock()
        mock_manager._perform_search.side_effect = ValueError(
            "preferred_provider required for specific strategy"
        )

        mock_alias_manager = MagicMock()
        mock_alias_manager.read_alias.return_value = "/fake/path/to/repo"
        mock_repo_entry = {"alias_name": "test-global", "repo_name": "test"}

        with patch.object(h, "_list_global_repos", return_value=[mock_repo_entry]):
            with patch.object(h, "_get_golden_repos_dir", return_value="/fake/golden"):
                with patch(
                    "code_indexer.global_repos.alias_manager.AliasManager",
                    return_value=mock_alias_manager,
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers.Path"
                    ) as mock_path_cls:
                        mock_path_instance = MagicMock()
                        mock_path_instance.exists.return_value = True
                        mock_path_cls.return_value = mock_path_instance
                        with patch(
                            "code_indexer.server.mcp.handlers.app_module"
                        ) as mock_app:
                            mock_app.semantic_query_manager = mock_manager
                            with patch.object(
                                h, "_get_query_tracker", return_value=None
                            ):
                                with patch.object(
                                    h,
                                    "_get_access_filtering_service",
                                    return_value=None,
                                ):
                                    response = h.search_code(params, user)

        # Should return an error response, not raise
        assert response is not None


# ---------------------------------------------------------------------------
# Tests: error response content for missing preferred_provider
# ---------------------------------------------------------------------------


class TestSpecificStrategyErrorResponses:
    """Tests for error response behavior when preferred_provider is missing."""

    def setup_method(self):
        self.repo_path = _make_temp_repo()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_specific_strategy_none_provider_raises_clear_error(self):
        """SPECIFIC strategy with None preferred_provider raises descriptive ValueError."""
        manager = _make_manager()

        with pytest.raises(ValueError) as exc_info:
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="specific",
                preferred_provider=None,
            )
        assert "preferred_provider" in str(exc_info.value).lower()

    def test_specific_strategy_message_mentions_required(self):
        """Error message for missing preferred_provider says 'required'."""
        manager = _make_manager()

        with pytest.raises(ValueError) as exc_info:
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="specific",
                preferred_provider=None,
            )
        assert "required" in str(exc_info.value).lower()
