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

    def test_preferred_provider_without_explicit_strategy_routes_to_named_provider(
        self,
    ):
        """preferred_provider alone (no query_strategy='specific') routes to named provider.

        Reproduces the bug where preferred_provider was ignored unless query_strategy='specific'
        was also supplied. With the fix, passing preferred_provider='cohere' with
        query_strategy=None must still call search_repository_path_with_provider with
        provider_name='cohere' and annotate results with source_provider='cohere'.
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
                query_strategy=None,
                preferred_provider="cohere",
            )

        mock_svc.search_repository_path_with_provider.assert_called_once()
        call_kwargs = mock_svc.search_repository_path_with_provider.call_args.kwargs
        assert call_kwargs.get("provider_name") == "cohere"

        assert len(results) == 1
        assert results[0].source_provider == "cohere"

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
# Tests: FAILOVER strategy routing
# ---------------------------------------------------------------------------


class TestFailoverStrategyRouting:
    """Tests for query_strategy='failover' routing in SemanticQueryManager.

    Verifies that execute_failover_query() is called (not bypassed as no-op)
    and that primary/secondary provider callables are wired correctly.
    """

    def setup_method(self):
        self.repo_path = _make_temp_repo()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_failover_strategy_uses_execute_failover_query(self):
        """failover strategy must call execute_failover_query, not fall through to primary_only.

        Observable outcome: execute_failover_query is invoked with two callables.
        Mocks _search_with_provider (real external dep) so no network calls are made.
        """
        manager = _make_manager()

        fake_voyage_result = QueryResult(
            file_path="src/auth.py",
            line_number=5,
            code_snippet="def auth(): pass",
            similarity_score=0.88,
            repository_alias="test-repo",
            source_provider="voyage-ai",
        )

        with patch(
            "code_indexer.server.query.semantic_query_manager.SemanticQueryManager"
            "._search_with_provider",
            return_value=[fake_voyage_result],
        ):
            with patch(
                "code_indexer.services.query_strategy.execute_failover_query",
                wraps=lambda primary_fn, secondary_fn, limit=10: primary_fn()[:limit],
            ) as mock_failover:
                results = manager._search_single_repository(
                    repo_path=self.repo_path,
                    repository_alias="test-repo",
                    query_text="auth",
                    limit=5,
                    min_score=None,
                    file_extensions=None,
                    query_strategy="failover",
                    preferred_provider=None,
                )

        assert mock_failover.call_count == 1, (
            "execute_failover_query must be called for failover strategy"
        )
        assert len(results) == 1
        assert results[0].file_path == "src/auth.py"

    def test_failover_strategy_falls_back_to_secondary_on_primary_error(self):
        """failover strategy must use secondary provider when primary raises.

        When the primary (_search_with_provider with voyage-ai) raises an exception,
        execute_failover_query must try the secondary (cohere) and return its results.
        """
        manager = _make_manager()

        fake_cohere_result = QueryResult(
            file_path="src/auth.py",
            line_number=5,
            code_snippet="def auth(): pass",
            similarity_score=0.75,
            repository_alias="test-repo",
            source_provider="cohere",
        )

        def _side_effect(**kwargs):
            provider = kwargs.get("provider_name", "")
            if provider == "voyage-ai":
                raise RuntimeError("voyage-ai unavailable")
            return [fake_cohere_result]

        with patch(
            "code_indexer.server.query.semantic_query_manager.SemanticQueryManager"
            "._search_with_provider",
            side_effect=_side_effect,
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=5,
                min_score=None,
                file_extensions=None,
                query_strategy="failover",
                preferred_provider=None,
            )

        assert len(results) == 1
        assert results[0].source_provider == "cohere", (
            "failover must return secondary provider results when primary fails"
        )


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


# ---------------------------------------------------------------------------
# Tests: Story #618 - Score/Provenance Transparency
# ---------------------------------------------------------------------------


class TestBothProvidersConfigured:
    """Tests for _both_providers_configured method (Story #618)."""

    def test_returns_true_when_both_providers_configured(self):
        """_both_providers_configured returns True when voyage-ai and cohere both present."""
        manager = _make_manager()

        with patch(
            "code_indexer.server.query.semantic_query_manager.EmbeddingProviderFactory",
            create=True,
        ):
            with patch("code_indexer.config.ConfigManager") as mock_cm_cls:
                mock_config = MagicMock()
                mock_cm = MagicMock()
                mock_cm.get_config.return_value = mock_config
                mock_cm_cls.create_with_backtrack.return_value = mock_cm

                with patch(
                    "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
                    return_value=["voyage-ai", "cohere"],
                ):
                    result = manager._both_providers_configured("/some/repo")

        assert result is True

    def test_returns_false_when_only_voyage_configured(self):
        """_both_providers_configured returns False when only voyage-ai is configured."""
        manager = _make_manager()

        with patch("code_indexer.config.ConfigManager") as mock_cm_cls:
            mock_config = MagicMock()
            mock_cm = MagicMock()
            mock_cm.get_config.return_value = mock_config
            mock_cm_cls.create_with_backtrack.return_value = mock_cm

            with patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
                return_value=["voyage-ai"],
            ):
                result = manager._both_providers_configured("/some/repo")

        assert result is False

    def test_returns_false_when_only_cohere_configured(self):
        """_both_providers_configured returns False when only cohere is configured."""
        manager = _make_manager()

        with patch("code_indexer.config.ConfigManager") as mock_cm_cls:
            mock_config = MagicMock()
            mock_cm = MagicMock()
            mock_cm.get_config.return_value = mock_config
            mock_cm_cls.create_with_backtrack.return_value = mock_cm

            with patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
                return_value=["cohere"],
            ):
                result = manager._both_providers_configured("/some/repo")

        assert result is False

    def test_returns_false_when_config_fails_and_no_env_vars(self):
        """_both_providers_configured returns False when config fails and no env vars set."""
        manager = _make_manager()

        with patch("code_indexer.config.ConfigManager") as mock_cm_cls:
            mock_cm_cls.create_with_backtrack.side_effect = RuntimeError(
                "no config found"
            )
            with patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=[],
            ):
                result = manager._both_providers_configured("/nonexistent/repo")

        assert result is False

    def test_returns_true_when_config_fails_but_env_vars_set(self):
        """_both_providers_configured returns True when config fails but env vars provide both keys."""
        manager = _make_manager()

        with patch("code_indexer.config.ConfigManager") as mock_cm_cls:
            mock_cm_cls.create_with_backtrack.side_effect = RuntimeError(
                "no config found"
            )
            with patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai", "cohere"],
            ):
                result = manager._both_providers_configured("/nonexistent/repo")

        assert result is True


class TestAutoParallelDefault:
    """Tests for automatic parallel strategy default when both providers configured (Story #618)."""

    def setup_method(self):
        self.repo_path = _make_temp_repo()
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def test_auto_parallel_when_both_providers_configured(self):
        """When query_strategy=None and both providers configured, uses parallel+rrf."""
        manager = _make_manager()

        manager._both_providers_configured = MagicMock(return_value=True)

        # Create a mock result that _search_with_provider returns
        mock_result = QueryResult(
            file_path="src/auth.py",
            line_number=10,
            code_snippet="def auth(): pass",
            similarity_score=0.85,
            repository_alias="test-repo",
            source_provider="voyage-ai",
        )

        manager._search_with_provider = MagicMock(return_value=[mock_result])

        manager._search_single_repository(
            repo_path=self.repo_path,
            repository_alias="test-repo",
            query_text="auth",
            limit=10,
            min_score=None,
            file_extensions=None,
            query_strategy=None,
            preferred_provider=None,
        )

        # _search_with_provider called — only happens in the parallel branch
        assert manager._search_with_provider.call_count >= 1, (
            "_search_with_provider must be called in parallel path"
        )

    def test_primary_only_fallback_when_single_provider(self):
        """When query_strategy=None and only one provider configured, uses primary_only."""
        manager = _make_manager()

        def mock_one_provider(repo_path):
            return False

        manager._both_providers_configured = mock_one_provider

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService"
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.search_repository_path.return_value = _fake_search_response()
            mock_svc_cls.return_value = mock_svc

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy=None,
                preferred_provider=None,
            )

        # search_repository_path (not with_provider) called — primary_only path
        mock_svc.search_repository_path.assert_called_once()
        assert len(results) == 1

    def test_explicit_primary_only_overrides_auto_parallel(self):
        """Explicit query_strategy='primary_only' skips auto-parallel even when both configured."""
        manager = _make_manager()

        def mock_both_providers(repo_path):
            return True  # Both configured, but explicit primary_only overrides

        manager._both_providers_configured = mock_both_providers

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService"
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.search_repository_path.return_value = _fake_search_response()
            mock_svc_cls.return_value = mock_svc

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="primary_only",
                preferred_provider=None,
            )

        # primary_only path: search_repository_path, not with_provider
        mock_svc.search_repository_path.assert_called_once()
        assert len(results) == 1


class TestQueryResultFusionFields:
    """Tests for fusion_score and contributing_providers in QueryResult (Story #618)."""

    def test_query_result_has_fusion_score_field(self):
        """QueryResult must have fusion_score field defaulting to None."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
        )
        assert hasattr(result, "fusion_score")
        assert result.fusion_score is None

    def test_query_result_has_contributing_providers_field(self):
        """QueryResult must have contributing_providers field defaulting to None."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
        )
        assert hasattr(result, "contributing_providers")
        assert result.contributing_providers is None

    def test_to_dict_omits_fusion_score_when_none(self):
        """to_dict() must NOT include fusion_score key when fusion_score is None."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
        )
        d = result.to_dict()
        assert "fusion_score" not in d

    def test_to_dict_omits_contributing_providers_when_none(self):
        """to_dict() must NOT include contributing_providers key when it is None."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
        )
        d = result.to_dict()
        assert "contributing_providers" not in d

    def test_to_dict_includes_fusion_score_when_set(self):
        """to_dict() must include fusion_score when it has a value."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
            fusion_score=0.032,
        )
        d = result.to_dict()
        assert "fusion_score" in d
        assert abs(d["fusion_score"] - 0.032) < 1e-10

    def test_to_dict_includes_contributing_providers_when_set(self):
        """to_dict() must include contributing_providers when it has a value."""
        result = QueryResult(
            file_path="src/foo.py",
            line_number=1,
            code_snippet="def foo(): pass",
            similarity_score=0.9,
            repository_alias="my-repo",
            contributing_providers=["cohere", "voyage-ai"],
        )
        d = result.to_dict()
        assert "contributing_providers" in d
        assert d["contributing_providers"] == ["cohere", "voyage-ai"]


# ---------------------------------------------------------------------------
# Story #619 Gap 1: Health-gated parallel dispatch tests
# ---------------------------------------------------------------------------


class TestHealthGatedParallelDispatch:
    """Tests that 'down' providers are skipped in parallel dispatch (Story #619 Gap 1)."""

    def setup_method(self):
        self.repo_path = _make_temp_repo()
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def test_parallel_skips_down_provider(self):
        """When cohere is 'down', parallel dispatch must NOT call _search_with_provider for cohere."""
        from code_indexer.services.provider_health_monitor import (
            ProviderHealthStatus,
        )

        manager = _make_manager()
        dispatched_providers = []

        def fake_search(
            repo_path,
            repository_alias,
            query_text,
            limit,
            min_score,
            file_extensions,
            language,
            exclude_language,
            path_filter,
            exclude_path,
            accuracy,
            provider_name,
        ):
            dispatched_providers.append(provider_name)
            return []

        down_status = ProviderHealthStatus(
            provider="cohere",
            status="down",
            health_score=0.0,
            p50_latency_ms=0.0,
            p95_latency_ms=0.0,
            p99_latency_ms=0.0,
            error_rate=1.0,
            availability=0.0,
            total_requests=10,
            successful_requests=0,
            failed_requests=10,
            window_minutes=60,
        )
        healthy_status = ProviderHealthStatus(
            provider="voyage-ai",
            status="healthy",
            health_score=1.0,
            p50_latency_ms=100.0,
            p95_latency_ms=200.0,
            p99_latency_ms=300.0,
            error_rate=0.0,
            availability=1.0,
            total_requests=10,
            successful_requests=10,
            failed_requests=0,
            window_minutes=60,
        )

        def fake_get_health(provider=None):
            if provider == "cohere":
                return {"cohere": down_status}
            if provider == "voyage-ai":
                return {"voyage-ai": healthy_status}
            return {"voyage-ai": healthy_status, "cohere": down_status}

        mock_monitor = MagicMock()
        mock_monitor.get_health.side_effect = fake_get_health
        # Bug #678: is_sinbinned must return False so providers are not skipped
        mock_monitor.is_sinbinned.return_value = False

        with (
            patch.object(manager, "_search_with_provider", side_effect=fake_search),
            patch.object(manager, "_both_providers_configured", return_value=True),
            patch(
                "code_indexer.services.provider_health_monitor.ProviderHealthMonitor.get_instance",
                return_value=mock_monitor,
            ),
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert "cohere" not in dispatched_providers, (
            "cohere is 'down' and must NOT be dispatched in parallel query"
        )
        assert "voyage-ai" in dispatched_providers, (
            "voyage-ai is healthy and must be dispatched"
        )

    def test_parallel_dispatches_all_when_healthy(self):
        """When both providers are healthy, both must be dispatched."""
        from code_indexer.services.provider_health_monitor import (
            ProviderHealthStatus,
        )

        manager = _make_manager()
        dispatched_providers = []

        def fake_search(
            repo_path,
            repository_alias,
            query_text,
            limit,
            min_score,
            file_extensions,
            language,
            exclude_language,
            path_filter,
            exclude_path,
            accuracy,
            provider_name,
        ):
            dispatched_providers.append(provider_name)
            return []

        healthy_status_voyage = ProviderHealthStatus(
            provider="voyage-ai",
            status="healthy",
            health_score=1.0,
            p50_latency_ms=100.0,
            p95_latency_ms=200.0,
            p99_latency_ms=300.0,
            error_rate=0.0,
            availability=1.0,
            total_requests=10,
            successful_requests=10,
            failed_requests=0,
            window_minutes=60,
        )
        healthy_status_cohere = ProviderHealthStatus(
            provider="cohere",
            status="healthy",
            health_score=1.0,
            p50_latency_ms=100.0,
            p95_latency_ms=200.0,
            p99_latency_ms=300.0,
            error_rate=0.0,
            availability=1.0,
            total_requests=10,
            successful_requests=10,
            failed_requests=0,
            window_minutes=60,
        )

        def fake_get_health(provider=None):
            if provider == "cohere":
                return {"cohere": healthy_status_cohere}
            if provider == "voyage-ai":
                return {"voyage-ai": healthy_status_voyage}
            return {"voyage-ai": healthy_status_voyage, "cohere": healthy_status_cohere}

        mock_monitor = MagicMock()
        mock_monitor.get_health.side_effect = fake_get_health
        # Bug #678: is_sinbinned must return False so providers are not skipped
        mock_monitor.is_sinbinned.return_value = False

        with (
            patch.object(manager, "_search_with_provider", side_effect=fake_search),
            patch.object(manager, "_both_providers_configured", return_value=True),
            patch(
                "code_indexer.services.provider_health_monitor.ProviderHealthMonitor.get_instance",
                return_value=mock_monitor,
            ),
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert "voyage-ai" in dispatched_providers, "voyage-ai must be dispatched"
        assert "cohere" in dispatched_providers, "cohere must be dispatched"


# ---------------------------------------------------------------------------
# Story #619 Gap 3: as_completed timeout handling tests
# ---------------------------------------------------------------------------


class TestAsCompletedTimeout:
    """Tests that TimeoutError from as_completed is handled gracefully (Story #619 Gap 3)."""

    def setup_method(self):
        self.repo_path = _make_temp_repo()
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def test_as_completed_timeout_returns_fast_provider_results(self):
        """TimeoutError from as_completed must not propagate — returns empty list gracefully."""
        import concurrent.futures

        manager = _make_manager()

        def fake_search(**kwargs):
            return []

        def raising_as_completed(futures, timeout=None):
            raise concurrent.futures.TimeoutError("simulated timeout")

        with (
            patch.object(manager, "_search_with_provider", side_effect=fake_search),
            patch.object(manager, "_both_providers_configured", return_value=True),
            patch(
                "code_indexer.server.query.semantic_query_manager.as_completed",
                side_effect=raising_as_completed,
            ),
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert isinstance(results, list), (
            "TimeoutError from as_completed must be handled; result must be a list"
        )
        assert len(results) == 0, "With all futures timed out, result must be empty"


# ---------------------------------------------------------------------------
# Story #619 Gap 5: Surface degraded_providers to API consumers
# ---------------------------------------------------------------------------


class TestDegradedProviders:
    """Tests that skipped providers are recorded in _last_query_degraded_providers (Story #619 Gap 5)."""

    def setup_method(self):
        self.repo_path = _make_temp_repo()
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()

    def test_degraded_providers_populated_when_provider_skipped(self):
        """When cohere is 'down' and skipped, _last_query_degraded_providers must contain 'cohere'."""
        from code_indexer.services.provider_health_monitor import (
            ProviderHealthStatus,
        )

        manager = _make_manager()

        def fake_search(**kwargs):
            return []

        down_status = ProviderHealthStatus(
            provider="cohere",
            status="down",
            health_score=0.0,
            p50_latency_ms=0.0,
            p95_latency_ms=0.0,
            p99_latency_ms=0.0,
            error_rate=1.0,
            availability=0.0,
            total_requests=10,
            successful_requests=0,
            failed_requests=10,
            window_minutes=60,
        )
        healthy_status = ProviderHealthStatus(
            provider="voyage-ai",
            status="healthy",
            health_score=1.0,
            p50_latency_ms=100.0,
            p95_latency_ms=200.0,
            p99_latency_ms=300.0,
            error_rate=0.0,
            availability=1.0,
            total_requests=10,
            successful_requests=10,
            failed_requests=0,
            window_minutes=60,
        )

        def fake_get_health(provider=None):
            if provider == "cohere":
                return {"cohere": down_status}
            if provider == "voyage-ai":
                return {"voyage-ai": healthy_status}
            return {"voyage-ai": healthy_status, "cohere": down_status}

        mock_monitor = MagicMock()
        mock_monitor.get_health.side_effect = fake_get_health

        with (
            patch.object(manager, "_search_with_provider", side_effect=fake_search),
            patch.object(manager, "_both_providers_configured", return_value=True),
            patch(
                "code_indexer.services.provider_health_monitor.ProviderHealthMonitor.get_instance",
                return_value=mock_monitor,
            ),
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert hasattr(manager, "_last_query_degraded_providers"), (
            "_last_query_degraded_providers must exist on manager"
        )
        assert "cohere" in manager._last_query_degraded_providers, (
            "Skipped 'down' provider must appear in _last_query_degraded_providers"
        )

    def test_degraded_providers_empty_when_all_healthy(self):
        """When all providers are healthy, _last_query_degraded_providers must be empty."""
        from code_indexer.services.provider_health_monitor import (
            ProviderHealthStatus,
        )

        manager = _make_manager()

        def fake_search(**kwargs):
            return []

        def fake_get_health(provider=None):
            healthy = ProviderHealthStatus(
                provider=provider or "voyage-ai",
                status="healthy",
                health_score=1.0,
                p50_latency_ms=100.0,
                p95_latency_ms=200.0,
                p99_latency_ms=300.0,
                error_rate=0.0,
                availability=1.0,
                total_requests=10,
                successful_requests=10,
                failed_requests=0,
                window_minutes=60,
            )
            key = provider or "voyage-ai"
            return {key: healthy}

        mock_monitor = MagicMock()
        mock_monitor.get_health.side_effect = fake_get_health
        mock_monitor.is_sinbinned.return_value = False  # Bug #678: not sin-binned

        with (
            patch.object(manager, "_search_with_provider", side_effect=fake_search),
            patch.object(manager, "_both_providers_configured", return_value=True),
            patch(
                "code_indexer.services.provider_health_monitor.ProviderHealthMonitor.get_instance",
                return_value=mock_monitor,
            ),
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="auth",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert hasattr(manager, "_last_query_degraded_providers"), (
            "_last_query_degraded_providers must exist on manager"
        )
        assert len(manager._last_query_degraded_providers) == 0, (
            "_last_query_degraded_providers must be empty when all providers are healthy"
        )
