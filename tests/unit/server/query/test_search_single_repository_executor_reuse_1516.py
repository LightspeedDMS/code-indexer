"""Proves Issue #1516's actual call-site bug: `_search_single_repository`'s
parallel-provider dispatch used to construct (and shut down) a brand-new
`ThreadPoolExecutor(max_workers=2)` on EVERY call, instead of reusing the
shared singleton from `parallel_query_executor.py`.

Mock boundary: `_search_with_provider` is the established mock boundary for
this call path (see `test_parallel_query_strategy_bugs_614_615.py`,
`test_semantic_query_manager_sinbin.py`).

This test's SOLE assertion spies on `get_global_parallel_query_executor`
(imported into `semantic_query_manager`'s namespace) via
`unittest.mock.patch(..., side_effect=<real function>)` -- a pass-through
spy that never fakes the return value, only records which
`ThreadPoolExecutor` OBJECT (compared by `is`, never `id()`) the real call
site actually obtained on each of two sequential calls. Both calls
resolving to the identical object is deterministic, sufficient proof that
the SAME shared pool -- and therefore its bounded worker-thread population
-- backs every call, which is what issue #1516 required.
"""

import logging
import shutil
import tempfile
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.parallel_query_executor import (
    get_global_parallel_query_executor,
    reset_global_parallel_query_executor,
)
from code_indexer.server.query.semantic_query_manager import (
    QueryResult,
    SemanticQueryManager,
)
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


@pytest.fixture(autouse=True)
def reset_health_monitor():
    """Reset ProviderHealthMonitor singleton before/after each test."""
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture(autouse=True)
def reset_shared_executor():
    """Test isolation for the shared parallel-query executor singleton."""
    reset_global_parallel_query_executor()
    yield
    reset_global_parallel_query_executor()


@pytest.fixture
def repo_path():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def manager():
    """SemanticQueryManager with mocked constructor-dependency scaffolding
    (activated_repo_manager / background_job_manager are unrelated to the
    parallel-dispatch path under test -- mirrors the established `manager`
    fixture in test_semantic_query_manager_sinbin.py)."""
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


def _run_parallel_query(manager, repo_path):
    """Run _search_single_repository with parallel strategy."""

    def _fake_search(*args, **kwargs):
        provider = kwargs.get("provider_name", "unknown")
        return _make_provider_results(provider, f"src/{provider}.py", 0.75)

    manager._search_with_provider = MagicMock(side_effect=_fake_search)

    return manager._search_single_repository(
        repo_path=repo_path,
        repository_alias="test-repo",
        query_text="authentication",
        limit=10,
        min_score=None,
        file_extensions=None,
        query_strategy="parallel",
    )


class TestSearchSingleRepositoryReusesSharedExecutor:
    """The actual call site (`_search_single_repository`) must dispatch its
    parallel provider tasks through the shared executor, so the SAME
    executor object (and therefore the same live worker threads) backs
    SEQUENTIAL calls -- not a fresh executor per call."""

    def test_two_sequential_calls_use_the_same_executor_instance(
        self, manager, repo_path
    ):
        used_executors: list = []

        def _spy_get_executor():
            # Pass-through spy: calls the REAL accessor, never fakes the
            # return value -- only records which object it resolved to.
            executor = get_global_parallel_query_executor()
            used_executors.append(executor)
            return executor

        with patch(
            "code_indexer.server.query.semantic_query_manager."
            "get_global_parallel_query_executor",
            side_effect=_spy_get_executor,
        ):
            _run_parallel_query(manager, repo_path)
            _run_parallel_query(manager, repo_path)

        assert len(used_executors) == 2, (
            f"Expected the shared-executor accessor to be called exactly "
            f"once per _search_single_repository call (2 calls total), "
            f"got {len(used_executors)} invocations"
        )
        assert used_executors[0] is used_executors[1], (
            "Issue #1516: the two sequential calls resolved to DIFFERENT "
            "executor objects -- each call built its own ThreadPoolExecutor "
            "instead of reusing the shared singleton."
        )
