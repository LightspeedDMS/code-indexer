"""Bug #1804 -- when every dispatched embedding provider fails in the
"parallel" query_strategy branch, `_search_single_repository` must still
raise (Bug #1760's contract, unchanged -- see
test_bug1760_parallel_dispatch_total_failure_surfaces_error.py), but it must
ALSO record an explicit completeness marker via the `_provider_completeness_out`
out-param BEFORE raising.

Why an out-param and not an exception attribute: the raised exception is
re-wrapped into a brand-new plain `Exception`/`SemanticQueryError` at two
points upstream (`_perform_search`'s per-repo loop collapsing all_results/
repo_errors into `raise Exception(repo_errors[-1])`, and
`query_user_repositories`'s `except Exception as e: raise
SemanticQueryError(f"Search failed: {str(e)}")`), which discards any
attribute set on the original exception object. A mutable out-param
(mirroring the existing `_degraded_repos_out`/`_temporal_warning_out`
convention in this same file) survives both re-wraps because the caller
inspects the SAME dict object it passed in, independent of what gets raised.

This out-param is the mechanism the MCP handler layer (search.py) uses to
build a graceful `success: true, completeness: "providers_unavailable"`
response instead of propagating the hard failure -- see
tests/unit/server/mcp/test_bug1804_search_provider_unavailable_marker.py.
"""

import logging
import shutil
import tempfile
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    SemanticQueryError,
)
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


def _patch_health_monitor(monitor: ProviderHealthMonitor):
    return patch(
        "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor",
        get_instance=MagicMock(return_value=monitor),
    )


def _run_parallel_query(manager, repo_path, completeness_out: Dict[str, Any]):
    return manager._search_single_repository(
        repo_path=repo_path,
        repository_alias="test-repo",
        query_text="authentication",
        limit=10,
        min_score=None,
        file_extensions=None,
        query_strategy="parallel",
        _provider_completeness_out=completeness_out,
    )


@pytest.fixture
def repo_path():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_health_monitor():
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def health_monitor():
    return ProviderHealthMonitor.get_instance()


@pytest.fixture
def manager():
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


class TestHardFailureRecordsCompletenessMarker:
    """Every dispatched provider hard-fails (e.g. both return 503) -- the
    out-param must record the marker AND per-provider error detail before
    the (still-required, Bug #1760) raise."""

    def test_both_providers_503_records_marker_before_raising(
        self, manager, repo_path, health_monitor
    ):
        def raising_both(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            raise RuntimeError(f"{provider}: 503 Service Unavailable")

        completeness_out: Dict[str, Any] = {}

        with patch.object(manager, "_search_with_provider", side_effect=raising_both):
            with _patch_health_monitor(health_monitor):
                with pytest.raises(SemanticQueryError):
                    _run_parallel_query(manager, repo_path, completeness_out)

        assert completeness_out.get("completeness") == "providers_unavailable"
        provider_errors = completeness_out.get("provider_errors", {})
        assert "voyage-ai" in provider_errors
        assert "cohere" in provider_errors
        assert "503" in provider_errors["voyage-ai"]
        assert "503" in provider_errors["cohere"]


class TestPreSkipSinbinnedRecordsCompletenessMarker:
    """Every provider pre-skipped as sin-binned -- the out-param must record
    the marker before the (still-required, Bug #1760 Finding 2) raise."""

    def test_both_providers_sinbinned_records_marker_before_raising(
        self, manager, repo_path, health_monitor
    ):
        health_monitor.sinbin("voyage-ai")
        health_monitor.sinbin("cohere")

        completeness_out: Dict[str, Any] = {}

        with patch.object(manager, "_search_with_provider") as mock_search:
            with _patch_health_monitor(health_monitor):
                with pytest.raises(SemanticQueryError):
                    _run_parallel_query(manager, repo_path, completeness_out)

        mock_search.assert_not_called()
        assert completeness_out.get("completeness") == "providers_unavailable"
        provider_errors = completeness_out.get("provider_errors", {})
        assert "voyage-ai" in provider_errors
        assert "cohere" in provider_errors


class TestPartialFailureDoesNotSetMarker:
    """A partial failure (one provider succeeds) must NOT set the marker --
    it is not a total-unavailability condition."""

    def test_one_hard_failure_one_success_does_not_set_marker(
        self, manager, repo_path, health_monitor
    ):
        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise RuntimeError("voyage-ai unavailable")
            return []

        completeness_out: Dict[str, Any] = {}

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path, completeness_out)

        assert completeness_out == {}
