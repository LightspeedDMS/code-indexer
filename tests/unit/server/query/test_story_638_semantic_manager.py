"""
Tests for Story #638: Dual-Provider Fusion Quality Improvements
— semantic_query_manager.py integration.

Verifies:
- Over-fetch dispatch: each provider receives limit * PARALLEL_FETCH_MULTIPLIER
  (capped at MAX_PARALLEL_FETCH) instead of user's limit
- Score gate is applied before fusion (apply_score_gate called)
- Parallel timeout is PARALLEL_TIMEOUT_SECONDS (20s)
"""

import logging
import tempfile
from unittest.mock import MagicMock, patch

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    QueryResult,
)
from code_indexer.services.query_strategy import (
    PARALLEL_FETCH_MULTIPLIER,
    MAX_PARALLEL_FETCH,
    PARALLEL_TIMEOUT_SECONDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> SemanticQueryManager:
    """Create a SemanticQueryManager with mocked infrastructure dependencies."""
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.data_dir = "/fake/data"
    manager.query_timeout_seconds = 30
    manager.max_concurrent_queries_per_user = 5
    manager.max_results_per_query = 100
    manager._active_queries_per_user = {}
    manager.logger = logging.getLogger(__name__)

    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    manager.activated_repo_manager = mock_arm
    manager.background_job_manager = MagicMock()
    return manager


def _make_query_result(file_path: str, score: float, provider: str) -> QueryResult:
    return QueryResult(
        file_path=file_path,
        line_number=1,
        code_snippet="code",
        similarity_score=score,
        repository_alias="repo",
        source_provider=provider,
    )


# ---------------------------------------------------------------------------
# Test: Over-fetch dispatch — provider receives provider_fetch_limit
# ---------------------------------------------------------------------------


class TestOverFetchDispatch:
    """Verify parallel dispatch sends provider_fetch_limit to each provider."""

    def test_provider_receives_over_fetch_limit_not_user_limit(self):
        """
        When limit=6, each provider should be called with limit=12
        (PARALLEL_FETCH_MULTIPLIER=2, 6*2=12 < MAX_PARALLEL_FETCH=40).
        """
        manager = _make_manager()
        user_limit = 6
        expected_provider_limit = min(
            user_limit * PARALLEL_FETCH_MULTIPLIER, MAX_PARALLEL_FETCH
        )
        assert expected_provider_limit == 12

        captured_limits = []

        def fake_search_with_provider(**kwargs):
            captured_limits.append(kwargs.get("limit"))
            return [_make_query_result("a.py", 0.9, kwargs.get("provider_name", "p"))]

        with tempfile.TemporaryDirectory() as repo_path:
            manager._both_providers_configured = MagicMock(return_value=True)
            manager._search_with_provider = fake_search_with_provider

            with patch(
                "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor"
            ) as mock_phm:
                mock_phm.get_instance.return_value.get_health.return_value = {}
                manager._search_single_repository(
                    repo_path=repo_path,
                    repository_alias="repo",
                    query_text="auth",
                    limit=user_limit,
                    min_score=None,
                    file_extensions=None,
                    query_strategy="parallel",
                    score_fusion="rrf",
                )

        # Each provider should have been called with provider_fetch_limit=12, not 6
        assert all(lim == expected_provider_limit for lim in captured_limits), (
            f"Expected all provider calls with limit={expected_provider_limit}, "
            f"but got: {captured_limits}"
        )

    def test_provider_fetch_limit_capped_at_max(self):
        """
        When limit=30, provider_fetch_limit = min(30*2, 40) = 40.
        """
        user_limit = 30
        provider_fetch_limit = min(
            user_limit * PARALLEL_FETCH_MULTIPLIER, MAX_PARALLEL_FETCH
        )
        assert provider_fetch_limit == 40

        manager = _make_manager()
        captured_limits = []

        def fake_search_with_provider(**kwargs):
            captured_limits.append(kwargs.get("limit"))
            return [_make_query_result("a.py", 0.9, kwargs.get("provider_name", "p"))]

        with tempfile.TemporaryDirectory() as repo_path:
            manager._both_providers_configured = MagicMock(return_value=True)
            manager._search_with_provider = fake_search_with_provider

            with patch(
                "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor"
            ) as mock_phm:
                mock_phm.get_instance.return_value.get_health.return_value = {}
                manager._search_single_repository(
                    repo_path=repo_path,
                    repository_alias="repo",
                    query_text="auth",
                    limit=user_limit,
                    min_score=None,
                    file_extensions=None,
                    query_strategy="parallel",
                    score_fusion="rrf",
                )

        assert all(lim == 40 for lim in captured_limits), (
            f"Expected all provider calls with limit=40 (capped), but got: {captured_limits}"
        )

    def test_final_results_respect_user_limit(self):
        """Even when providers return more results, final output is capped at user limit."""
        manager = _make_manager()
        user_limit = 3

        def fake_search_with_provider(**kwargs):
            # Return more than user_limit items
            return [
                _make_query_result(
                    f"file{i}.py", 0.9 - i * 0.05, kwargs.get("provider_name", "p")
                )
                for i in range(8)
            ]

        with tempfile.TemporaryDirectory() as repo_path:
            manager._both_providers_configured = MagicMock(return_value=True)
            manager._search_with_provider = fake_search_with_provider

            with patch(
                "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor"
            ) as mock_phm:
                mock_phm.get_instance.return_value.get_health.return_value = {}
                results = manager._search_single_repository(
                    repo_path=repo_path,
                    repository_alias="repo",
                    query_text="auth",
                    limit=user_limit,
                    min_score=None,
                    file_extensions=None,
                    query_strategy="parallel",
                    score_fusion="rrf",
                )

        assert len(results) <= user_limit, (
            f"Expected at most {user_limit} results, got {len(results)}"
        )


# ---------------------------------------------------------------------------
# Test: Score gate applied before fusion
# ---------------------------------------------------------------------------


class TestScoreGateIntegration:
    """Verify apply_score_gate is called in parallel dispatch before fusion."""

    def test_score_gate_called_in_parallel_dispatch(self):
        """apply_score_gate must be called during parallel dispatch."""
        manager = _make_manager()

        def fake_search_with_provider(**kwargs):
            provider = kwargs.get("provider_name", "p")
            if provider == "voyage-ai":
                return [
                    _make_query_result("a.py", 0.95, "voyage-ai"),
                    _make_query_result("b.py", 0.90, "voyage-ai"),
                ]
            else:
                return [
                    _make_query_result("c.py", 0.40, "cohere"),
                    _make_query_result("d.py", 0.30, "cohere"),
                ]

        with tempfile.TemporaryDirectory() as repo_path:
            manager._both_providers_configured = MagicMock(return_value=True)
            manager._search_with_provider = fake_search_with_provider

            with patch(
                "code_indexer.server.query.semantic_query_manager.apply_score_gate"
            ) as mock_gate:
                # Return inputs unchanged so the rest of the dispatch works.
                # Must accept score_attr kwarg since semantic manager passes it.
                mock_gate.side_effect = lambda p, s, score_attr="score": (p, s)

                with patch(
                    "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor"
                ) as mock_phm:
                    mock_phm.get_instance.return_value.get_health.return_value = {}
                    manager._search_single_repository(
                        repo_path=repo_path,
                        repository_alias="repo",
                        query_text="auth",
                        limit=5,
                        min_score=None,
                        file_extensions=None,
                        query_strategy="parallel",
                        score_fusion="rrf",
                    )

            mock_gate.assert_called_once()

    def test_score_gate_weak_provider_culled_before_fusion(self):
        """When cohere scores far below voyage-ai, its results are gated out."""
        manager = _make_manager()

        def fake_search_with_provider(**kwargs):
            provider = kwargs.get("provider_name", "p")
            if provider == "voyage-ai":
                return [
                    _make_query_result("a.py", 0.95, "voyage-ai"),
                ]
            else:
                # cohere max=0.40 < voyage_max(0.95) * 0.80=0.76 → gate fires
                return [
                    _make_query_result("c.py", 0.40, "cohere"),
                    _make_query_result("d.py", 0.20, "cohere"),
                ]

        with tempfile.TemporaryDirectory() as repo_path:
            manager._both_providers_configured = MagicMock(return_value=True)
            manager._search_with_provider = fake_search_with_provider

            with patch(
                "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor"
            ) as mock_phm:
                mock_phm.get_instance.return_value.get_health.return_value = {}
                # Bug #678: is_sinbinned must return False so providers are dispatched
                mock_phm.get_instance.return_value.is_sinbinned.return_value = False
                results = manager._search_single_repository(
                    repo_path=repo_path,
                    repository_alias="repo",
                    query_text="auth",
                    limit=10,
                    min_score=None,
                    file_extensions=None,
                    query_strategy="parallel",
                    score_fusion="rrf",
                )

        # All cohere results below floor 0.95*0.70=0.665 should be culled
        result_files = {r.file_path for r in results}
        assert "c.py" not in result_files, (
            "Weak cohere result c.py should have been gated"
        )
        assert "d.py" not in result_files, (
            "Weak cohere result d.py should have been gated"
        )
        assert "a.py" in result_files, "Strong voyage-ai result a.py should remain"


# ---------------------------------------------------------------------------
# Test: Parallel timeout is PARALLEL_TIMEOUT_SECONDS (20s)
# ---------------------------------------------------------------------------


class TestParallelTimeout:
    """Verify parallel dispatch uses PARALLEL_TIMEOUT_SECONDS=20 not 15."""

    def test_parallel_timeout_constant_is_20(self):
        """PARALLEL_TIMEOUT_SECONDS must be 20."""
        assert PARALLEL_TIMEOUT_SECONDS == 20

    def test_parallel_dispatch_uses_20s_timeout(self):
        """as_completed timeout in parallel dispatch must be PARALLEL_TIMEOUT_SECONDS."""
        manager = _make_manager()

        timeout_values = []

        import concurrent.futures

        original_as_completed = concurrent.futures.as_completed

        def capture_timeout_as_completed(futures, timeout=None):
            timeout_values.append(timeout)
            return original_as_completed(futures, timeout=timeout)

        def fake_search_with_provider(**kwargs):
            return [_make_query_result("a.py", 0.9, kwargs.get("provider_name", "p"))]

        with tempfile.TemporaryDirectory() as repo_path:
            manager._both_providers_configured = MagicMock(return_value=True)
            manager._search_with_provider = fake_search_with_provider

            with patch(
                "code_indexer.server.query.semantic_query_manager.as_completed",
                side_effect=capture_timeout_as_completed,
            ):
                with patch(
                    "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor"
                ) as mock_phm:
                    mock_phm.get_instance.return_value.get_health.return_value = {}
                    manager._search_single_repository(
                        repo_path=repo_path,
                        repository_alias="repo",
                        query_text="auth",
                        limit=5,
                        min_score=None,
                        file_extensions=None,
                        query_strategy="parallel",
                        score_fusion="rrf",
                    )

        assert any(t == PARALLEL_TIMEOUT_SECONDS for t in timeout_values), (
            f"Expected timeout={PARALLEL_TIMEOUT_SECONDS} but got: {timeout_values}"
        )
