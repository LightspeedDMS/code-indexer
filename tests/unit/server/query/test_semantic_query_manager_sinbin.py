"""Behavioral tests for sin-bin integration in SemanticQueryManager (Bug #678).

Mock boundary: _search_with_provider is the bridge to SemanticSearchService and
is the established mock boundary for this test suite (see test_parallel_query_strategy_bugs_614_615.py).

Tests verify observable behavior:
- Sinbinned providers are skipped (not queried) in parallel dispatch
- Provider failures are recorded in ProviderHealthMonitor after exceptions
- Config-based timeout replaces PARALLEL_TIMEOUT_SECONDS constant
- QueryBudgetExceeded exception is importable and usable
"""

import logging
import shutil
import tempfile
import time
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    QueryResult,
)
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
from code_indexer.server.utils.config_manager import QueryOrchestrationConfig


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_provider_results(
    provider: str, file_path: str, score: float
) -> List[QueryResult]:
    return [
        QueryResult(
            file_path=file_path,
            line_number=1,
            code_snippet=f"code from {provider}",
            similarity_score=score,
            repository_alias="test-repo",
            source_provider=provider,
        )
    ]


def _fast_search(*args, **kwargs) -> List[QueryResult]:
    """Minimal search stub returning one result per provider."""
    provider = kwargs.get("provider_name", "unknown")
    return _make_provider_results(provider, f"src/{provider}.py", 0.75)


def _make_tracking_search(bucket: list):
    """Return a search stub that records each queried provider name into bucket."""

    def _tracking(*args, **kwargs):
        bucket.append(kwargs.get("provider_name"))
        return _fast_search(*args, **kwargs)

    return _tracking


def _patch_health_monitor(monitor: ProviderHealthMonitor):
    """Context manager that injects monitor as the ProviderHealthMonitor singleton.

    Used by all tests to eliminate repeated patch(ProviderHealthMonitor) boilerplate.
    """
    return patch(
        "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor",
        get_instance=MagicMock(return_value=monitor),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_path():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_health_monitor():
    """Reset ProviderHealthMonitor singleton before/after each test."""
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def manager():
    """SemanticQueryManager with mocked infrastructure dependencies."""
    m = SemanticQueryManager.__new__(SemanticQueryManager)
    m.data_dir = "/fake/data"
    m.query_timeout_seconds = 30
    m.max_concurrent_queries_per_user = 5
    m.max_results_per_query = 100
    m._active_queries_per_user = {}
    m.logger = logging.getLogger(__name__)
    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    m.activated_repo_manager = mock_arm
    m.background_job_manager = MagicMock()
    return m


@pytest.fixture
def health_monitor():
    """Fresh ProviderHealthMonitor singleton."""
    return ProviderHealthMonitor.get_instance()


def _run_parallel_query(manager, repo_path):
    """Run _search_single_repository with parallel strategy and default params."""
    return manager._search_single_repository(
        repo_path=repo_path,
        repository_alias="test-repo",
        query_text="authentication",
        limit=10,
        min_score=None,
        file_extensions=None,
        query_strategy="parallel",
    )


# ---------------------------------------------------------------------------
# TestQueryBudgetExceeded
# ---------------------------------------------------------------------------


class TestQueryBudgetExceeded:
    """QueryBudgetExceeded must be importable and behave as an Exception."""

    def test_exception_can_be_raised(self):
        from code_indexer.server.query.semantic_query_manager import QueryBudgetExceeded

        with pytest.raises(QueryBudgetExceeded):
            raise QueryBudgetExceeded("budget exceeded")

    def test_exception_message_preserved(self):
        from code_indexer.server.query.semantic_query_manager import QueryBudgetExceeded

        exc = QueryBudgetExceeded("latency budget of 60s exhausted")
        assert "latency budget" in str(exc)

    def test_exception_is_exception_subclass(self):
        from code_indexer.server.query.semantic_query_manager import QueryBudgetExceeded

        assert issubclass(QueryBudgetExceeded, Exception)


# ---------------------------------------------------------------------------
# TestSinbinnedProviderSkip
# ---------------------------------------------------------------------------


class TestSinbinnedProviderSkip:
    """Sinbinned providers must be skipped (not queried) in parallel dispatch.

    Observable: when a provider is sinbinned, _search_with_provider is called
    exactly once (for the non-sinbinned provider), never for the sinbinned one.
    """

    def test_sinbinned_voyage_ai_not_queried(self, manager, repo_path, health_monitor):
        """When voyage-ai is sinbinned, _search_with_provider is called 0 times for it."""
        health_monitor._sinbin_until["voyage-ai"] = time.monotonic() + 60
        queried: list[Optional[str]] = []

        with patch.object(
            manager, "_search_with_provider", side_effect=_make_tracking_search(queried)
        ):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        assert "voyage-ai" not in queried, (
            f"voyage-ai was queried despite being sinbinned. Providers: {queried}"
        )
        assert queried == ["cohere"], (
            f"Expected exactly ['cohere'] queried, got: {queried}"
        )

    def test_sinbinned_cohere_not_queried(self, manager, repo_path, health_monitor):
        """When cohere is sinbinned, _search_with_provider is called 0 times for it."""
        health_monitor._sinbin_until["cohere"] = time.monotonic() + 60
        queried: list[Optional[str]] = []

        with patch.object(
            manager, "_search_with_provider", side_effect=_make_tracking_search(queried)
        ):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        assert "cohere" not in queried, (
            f"cohere was queried despite being sinbinned. Providers: {queried}"
        )
        assert queried == ["voyage-ai"], (
            f"Expected exactly ['voyage-ai'] queried, got: {queried}"
        )

    def test_neither_sinbinned_both_queried(self, manager, repo_path, health_monitor):
        """When neither provider is sinbinned, both are queried (regression guard)."""
        health_monitor._sinbin_until.clear()
        queried: list[Optional[str]] = []

        with patch.object(
            manager, "_search_with_provider", side_effect=_make_tracking_search(queried)
        ):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        assert len(queried) == 2, (
            f"Expected 2 providers queried, got {len(queried)}: {queried}"
        )
        assert "voyage-ai" in queried
        assert "cohere" in queried

    def test_expired_sinbin_provider_is_queried(
        self, manager, repo_path, health_monitor
    ):
        """A provider whose sin-bin has expired should be queried normally."""
        health_monitor._sinbin_until["voyage-ai"] = time.monotonic() - 1
        queried: list[Optional[str]] = []

        with patch.object(
            manager, "_search_with_provider", side_effect=_make_tracking_search(queried)
        ):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        assert "voyage-ai" in queried, (
            f"voyage-ai with expired sinbin should be queried. Got: {queried}"
        )


# ---------------------------------------------------------------------------
# TestFailureRecordingAfterProviderException
# ---------------------------------------------------------------------------


class TestFailureRecordingAfterProviderException:
    """After a provider raises in parallel dispatch, record_call(success=False) must be called.

    Observable: ProviderHealthMonitor has failed_requests > 0 for the provider
    that raised the exception after the dispatch completes.
    """

    def test_failure_recorded_when_voyage_ai_raises(
        self, manager, repo_path, health_monitor
    ):
        """When voyage-ai raises, its failed_requests count is at least 1."""

        def raising_search(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise RuntimeError("voyage-ai API unavailable")
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_search):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("voyage-ai").get("voyage-ai")
        assert status is not None, (
            "Health status must exist for voyage-ai after failure"
        )
        assert status.failed_requests >= 1, (
            f"Expected failed_requests >= 1 for voyage-ai, got {status.failed_requests}"
        )

    def test_failure_recorded_when_cohere_raises(
        self, manager, repo_path, health_monitor
    ):
        """When cohere raises, its failed_requests count is at least 1."""

        def raising_search(*args, **kwargs):
            if kwargs.get("provider_name") == "cohere":
                raise RuntimeError("cohere API timeout")
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_search):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("cohere").get("cohere")
        assert status is not None, "Health status must exist for cohere after failure"
        assert status.failed_requests >= 1, (
            f"Expected failed_requests >= 1 for cohere, got {status.failed_requests}"
        )

    def test_successful_providers_have_zero_failures(
        self, manager, repo_path, health_monitor
    ):
        """Providers that succeed must not have failed_requests incremented."""
        with patch.object(manager, "_search_with_provider", side_effect=_fast_search):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        for provider in ("voyage-ai", "cohere"):
            status = health_monitor.get_health(provider).get(provider)
            if status is not None:
                assert status.failed_requests == 0, (
                    f"Provider {provider} succeeded but has failed_requests={status.failed_requests}"
                )


# ---------------------------------------------------------------------------
# TestConfigBasedTimeout
# ---------------------------------------------------------------------------


class TestConfigBasedTimeout:
    """The parallel dispatch timeout must come from orchestration config, not a hardcoded constant.

    Observable: when config sets timeout=1s and cohere sleeps 3s, dispatch
    completes within CONFIG_TIMEOUT + 0.5s, proving the config value is used
    instead of waiting for the full 3s provider sleep.
    The slow (timed-out) provider's result does not appear in the output.
    """

    SLOW_SLEEP = 3.0
    CONFIG_TIMEOUT = 1

    def _make_config_service(self) -> MagicMock:
        """Build a config-service mock with CONFIG_TIMEOUT as the parallel timeout."""
        orch_cfg = QueryOrchestrationConfig(
            parallel_query_orchestrator_timeout_seconds=self.CONFIG_TIMEOUT,
            max_query_latency_budget_seconds=60,
            all_providers_sinbinned_retry_limit=2,
        )
        mock_config = MagicMock()
        mock_config.query_orchestration = orch_cfg
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = mock_config
        return mock_svc

    def test_config_timeout_cuts_off_slow_provider(
        self, manager, repo_path, health_monitor
    ):
        """With 1s config timeout and 3s cohere sleep, dispatch completes within 1.5s.

        Proves config timeout (1s) is respected; if the dispatch waited the full
        cohere sleep (3s), elapsed would exceed CONFIG_TIMEOUT + 0.5s.
        """

        def slow_cohere_search(*args, **kwargs):
            if kwargs.get("provider_name") == "cohere":
                time.sleep(self.SLOW_SLEEP)
            return _fast_search(*args, **kwargs)

        with patch.object(
            manager, "_search_with_provider", side_effect=slow_cohere_search
        ):
            with _patch_health_monitor(health_monitor):
                with patch(
                    "code_indexer.server.services.config_service.get_config_service",
                    return_value=self._make_config_service(),
                ):
                    start = time.monotonic()
                    results = _run_parallel_query(manager, repo_path)
                    elapsed = time.monotonic() - start

        assert elapsed < self.CONFIG_TIMEOUT + 0.5, (
            f"Dispatch took {elapsed:.2f}s — expected under {self.CONFIG_TIMEOUT + 0.5}s "
            f"(config timeout={self.CONFIG_TIMEOUT}s + 0.5s executor overhead). "
            f"Slow provider sleep was {self.SLOW_SLEEP}s."
        )
        cohere_results = [r for r in results if r.source_provider == "cohere"]
        assert cohere_results == [], (
            f"Timed-out cohere results must not appear in output, got: {cohere_results}"
        )
